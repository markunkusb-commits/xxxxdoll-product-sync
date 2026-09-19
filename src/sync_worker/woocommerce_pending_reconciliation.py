"""Local reconciliation of an uncertain Pending after exact remote readback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_pending_recovery as pending_core
from . import woocommerce_product_apply as apply_core
from . import woocommerce_target_snapshot as target_snapshot
from .report import SafeWriteAuditJsonReportWriter
from .sanitization import Redactor
from .woocommerce_category_discovery import WooCategoryCredentials


RECOVERY_RECEIPT_POLICY_VERSION = (
    "xxxxdoll-woo-product-recovery-receipt-v1"
)
MAX_RECEIPT_BYTES = 16 * 1024 * 1024

EXIT_RECONCILED = 0
EXIT_BLOCKED = 1
EXIT_PRE_WRITE_ERROR = 2
EXIT_RECOVERY_REQUIRED = 3

_RECOVERY_RECEIPT_FIELDS = frozenset(
    {
        "status",
        "policy_version",
        "plan_hash",
        "source_plan",
        "source_pending",
        "target",
        "operation",
        "product",
        "manual_confirmation_verified",
        "pending_confirmation_verified",
        "readback_verified",
        "reconciliation",
        "network_requests_performed",
        "woocommerce_requests_performed",
        "woocommerce_write_requests_performed",
        "wordpress_requests_performed",
        "external_write_requests_performed",
        "write_requests_performed",
    }
)
_RECONCILIATION = {
    "kind": "pending_attempting_remote_exact",
    "pending_post_state": "attempting",
    "pending_post_attempts_started": 1,
    "original_post_outcome": "unknown",
    "create_retry_performed": False,
}


class WooPendingReconciliationError(ValueError):
    """Fixed-code reconciliation contract or filesystem failure."""


CredentialLoader = Callable[[], tuple[WooCategoryCredentials, Redactor]]
GetTransportFactory = Callable[
    [str, WooCategoryCredentials], target_snapshot.WooProductTargetTransport
]
ReceiptWriter = Callable[[Path, Mapping[str, object], Redactor], None]
LockAcquirer = Callable[[Path, Mapping[str, object]], None]
FileUnlinker = Callable[[Path], None]
Hook = Callable[[], None]


def _result(
    status: str,
    exit_code: int,
    result_code: str,
    *,
    transport: target_snapshot.WooProductTargetTransport | None = None,
) -> dict[str, object]:
    network_requests = (
        transport.network_requests_performed if transport is not None else 0
    )
    return {
        "status": status,
        "exit_code": exit_code,
        "result_code": result_code,
        "network_requests_performed": network_requests,
        "woocommerce_requests_performed": network_requests,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }


def _safe_fingerprint(value: object, *, basename: str | None = None) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != {"basename", "sha256"}:
        return None
    candidate_basename = value.get("basename")
    digest = value.get("sha256")
    if (
        not isinstance(candidate_basename, str)
        or not candidate_basename
        or (basename is not None and candidate_basename != basename)
        or type(digest) is not str
        or apply_core._HASH_PATTERN.fullmatch(digest) is None
    ):
        return None
    return {"basename": candidate_basename, "sha256": digest}


def validate_recovery_receipt(
    receipt: Mapping[str, object],
    *,
    plan_source: Mapping[str, str],
    plan_hash: str,
    sku: str,
    payload: Mapping[str, object],
    expected_pending_source: Mapping[str, str] | None = None,
) -> int:
    """Validate the exact recovery Receipt contract and return product ID."""

    if (
        set(receipt) != _RECOVERY_RECEIPT_FIELDS
        or receipt.get("status") != "applied"
        or receipt.get("policy_version") != RECOVERY_RECEIPT_POLICY_VERSION
        or receipt.get("plan_hash") != plan_hash
        or receipt.get("source_plan") != dict(plan_source)
        or receipt.get("target") != apply_core._TARGET
        or receipt.get("manual_confirmation_verified") is not True
        or receipt.get("pending_confirmation_verified") is not True
        or receipt.get("readback_verified") is not True
    ):
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_contract_invalid"
        )

    reconciliation = receipt.get("reconciliation")
    if (
        not isinstance(reconciliation, Mapping)
        or set(reconciliation) != set(_RECONCILIATION)
        or reconciliation.get("kind") != _RECONCILIATION["kind"]
        or reconciliation.get("pending_post_state") != "attempting"
        or type(reconciliation.get("pending_post_attempts_started")) is not int
        or reconciliation.get("pending_post_attempts_started") != 1
        or reconciliation.get("original_post_outcome") != "unknown"
        or reconciliation.get("create_retry_performed") is not False
    ):
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_reconciliation_invalid"
        )

    source_pending = _safe_fingerprint(
        receipt.get("source_pending"),
        basename=apply_core.PENDING_FILENAME,
    )
    if source_pending is None or (
        expected_pending_source is not None
        and source_pending != dict(expected_pending_source)
    ):
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_pending_source_invalid"
        )

    operation = receipt.get("operation")
    if not isinstance(operation, Mapping) or set(operation) != {
        "action",
        "sku",
        "payload_sha256",
    }:
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_operation_invalid"
        )
    if operation != {
        "action": "create",
        "sku": sku,
        "payload_sha256": apply_core.canonical_payload_hash(payload),
    }:
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_operation_mismatch"
        )

    product = receipt.get("product")
    if not isinstance(product, Mapping) or set(product) != {
        "id",
        "sku",
        "name",
        "type",
        "status",
    }:
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_product_invalid"
        )
    product_id = product.get("id")
    if type(product_id) is not int or product_id <= 0:
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_product_id_invalid"
        )
    if product != {
        "id": product_id,
        "sku": sku,
        "name": payload.get("name"),
        "type": payload.get("type"),
        "status": payload.get("status"),
    }:
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_product_mismatch"
        )

    network_count = receipt.get("network_requests_performed")
    if (
        type(network_count) is not int
        or network_count <= 0
        or type(receipt.get("woocommerce_requests_performed")) is not int
        or receipt.get("woocommerce_requests_performed") != network_count
        or type(receipt.get("woocommerce_write_requests_performed")) is not int
        or receipt.get("woocommerce_write_requests_performed") != 0
        or type(receipt.get("wordpress_requests_performed")) is not int
        or receipt.get("wordpress_requests_performed") != 0
        or type(receipt.get("external_write_requests_performed")) is not int
        or receipt.get("external_write_requests_performed") != 0
        or type(receipt.get("write_requests_performed")) is not int
        or receipt.get("write_requests_performed") != 0
    ):
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_counters_invalid"
        )
    return product_id


def _build_recovery_receipt(
    *,
    plan_source: Mapping[str, str],
    pending_source: Mapping[str, str],
    plan_hash: str,
    sku: str,
    payload: Mapping[str, object],
    product_id: int,
    network_requests: int,
) -> dict[str, object]:
    return {
        "status": "applied",
        "policy_version": RECOVERY_RECEIPT_POLICY_VERSION,
        "plan_hash": plan_hash,
        "source_plan": dict(plan_source),
        "source_pending": dict(pending_source),
        "target": dict(apply_core._TARGET),
        "operation": {
            "action": "create",
            "sku": sku,
            "payload_sha256": apply_core.canonical_payload_hash(payload),
        },
        "product": {
            "id": product_id,
            "sku": sku,
            "name": payload["name"],
            "type": payload["type"],
            "status": payload["status"],
        },
        "manual_confirmation_verified": True,
        "pending_confirmation_verified": True,
        "readback_verified": True,
        "reconciliation": dict(_RECONCILIATION),
        "network_requests_performed": network_requests,
        "woocommerce_requests_performed": network_requests,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }


def _default_receipt_writer(
    path: Path,
    receipt: Mapping[str, object],
    redactor: Redactor,
) -> None:
    SafeWriteAuditJsonReportWriter(path, redactor).write(receipt)


def _default_unlinker(path: Path) -> None:
    path.unlink()


def _read_recovery_receipt_with_source(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    try:
        local = target_snapshot._safe_local_json_file(path)
        size = local.stat().st_size
        if size <= 0 or size > MAX_RECEIPT_BYTES:
            raise WooPendingReconciliationError(
                "woo_apply_recovery_receipt_size_invalid"
            )
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooPendingReconciliationError:
        raise
    except target_snapshot.WooTargetSnapshotInputError:
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_local_json_required"
        ) from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_json_invalid"
        ) from None
    if not isinstance(value, Mapping):
        raise WooPendingReconciliationError(
            "woo_apply_recovery_receipt_root_invalid"
        )
    source = {
        "basename": local.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return value, source, local


def _read_recovery_receipt(path: Path) -> Mapping[str, object]:
    value, _, _ = _read_recovery_receipt_with_source(path)
    return value


def _lock_sha256(path: Path) -> str:
    try:
        local = package_io._local_path(path, require_file=True)
        raw = local.read_bytes()
    except (OSError, package_io.SingleProductStagingPackageInputError):
        raise WooPendingReconciliationError(
            "woo_apply_reconciliation_lock_unverifiable"
        ) from None
    if not raw:
        raise WooPendingReconciliationError(
            "woo_apply_reconciliation_lock_unverifiable"
        )
    return hashlib.sha256(raw).hexdigest()


def _release_owned_lock(
    lock_path: Path,
    owned_lock_sha256: str,
    unlinker: FileUnlinker,
) -> bool:
    try:
        if _lock_sha256(lock_path) != owned_lock_sha256:
            return False
        unlinker(lock_path)
        return not lock_path.exists()
    except (OSError, RuntimeError, WooPendingReconciliationError):
        return False


def _release_or_recovery(
    result: dict[str, object],
    *,
    lock_path: Path,
    owned_lock_sha256: str,
    unlinker: FileUnlinker,
) -> dict[str, object]:
    if _release_owned_lock(lock_path, owned_lock_sha256, unlinker):
        return result
    result["status"] = "recovery_required"
    result["exit_code"] = EXIT_RECOVERY_REQUIRED
    result["result_code"] = "woo_apply_reconciliation_lock_cleanup_failed"
    return result


def _pending_still_exact(
    pending_path: Path,
    *,
    plan_source: Mapping[str, str],
    pending_source: Mapping[str, str],
    plan_hash: str,
    sku: str,
    payload: Mapping[str, object],
) -> bool:
    try:
        pending, current_source, _ = pending_core._read_pending(pending_path)
        pending_core._validate_pending(
            pending,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
        )
    except pending_core.WooPendingRecoveryError:
        return False
    return current_source == dict(pending_source)


def reconcile_woo_apply_pending(
    plan_report_path: Path,
    confirmed_plan_hash: str,
    confirmed_pending_sha256: str,
    base_url: str,
    *,
    project_root: Path,
    credential_loader: CredentialLoader = apply_core._default_credential_loader,
    get_transport_factory: GetTransportFactory = (
        target_snapshot.StdlibWooProductTargetTransport
    ),
    receipt_writer: ReceiptWriter = _default_receipt_writer,
    lock_acquirer: LockAcquirer = apply_core._acquire_lock,
    unlinker: FileUnlinker = _default_unlinker,
    after_lock_hook: Hook | None = None,
    after_get_hook: Hook | None = None,
    before_pending_cleanup_hook: Hook | None = None,
) -> dict[str, object]:
    """Reconcile one attempting Pending without any remote write operation."""

    try:
        frozen_plan, plan_source, _ = apply_core._read_plan(plan_report_path)
        sku, payload, plan_hash = apply_core.validate_frozen_plan(
            frozen_plan,
            confirmed_plan_hash,
        )
        normalized_base_url = apply_core.validate_apply_base_url(base_url)
    except (OSError, RuntimeError, apply_core.WooProductApplyError):
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_reconciliation_plan_or_target_invalid",
        )

    try:
        pending_path, receipt_path, lock_path = apply_core._runtime_paths(project_root)
    except (OSError, RuntimeError, apply_core.WooProductApplyError):
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_reconciliation_runtime_state_invalid",
        )

    try:
        if lock_path.exists() or receipt_path.exists():
            return _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_runtime_state_requires_recovery",
            )
        if not pending_path.exists():
            return _result(
                "blocked",
                EXIT_BLOCKED,
                "woo_apply_pending_not_found",
            )
    except (OSError, RuntimeError):
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_reconciliation_runtime_state_invalid",
        )

    try:
        pending, pending_source, _ = pending_core._read_pending(pending_path)
        pending_core._validate_pending(
            pending,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
        )
    except pending_core.WooPendingStateUnsupported:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_state_not_supported",
        )
    except pending_core.WooPendingRecoveryError:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_pending_contract_invalid",
        )

    actual_pending_sha256 = pending_source["sha256"]
    if (
        type(confirmed_pending_sha256) is not str
        or apply_core._HASH_PATTERN.fullmatch(confirmed_pending_sha256) is None
        or confirmed_pending_sha256 != actual_pending_sha256
    ):
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_pending_confirmation_mismatch",
        )

    lock_value = {
        "status": "locked",
        "policy_version": apply_core.LOCK_POLICY_VERSION,
        "plan_hash": plan_hash,
        "source_plan": dict(plan_source),
        "sku": sku,
        "purpose": "pending_reconciliation",
        "source_pending": dict(pending_source),
    }
    try:
        lock_acquirer(lock_path, lock_value)
    except Exception:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_reconciliation_lock_acquire_failed",
        )
    try:
        owned_lock_sha256 = _lock_sha256(lock_path)
    except WooPendingReconciliationError:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_reconciliation_lock_unverifiable",
        )

    try:
        if after_lock_hook is not None:
            after_lock_hook()
        post_lock_valid = (
            not receipt_path.exists()
            and pending_path.exists()
            and _lock_sha256(lock_path) == owned_lock_sha256
            and _pending_still_exact(
                pending_path,
                plan_source=plan_source,
                pending_source=pending_source,
                plan_hash=plan_hash,
                sku=sku,
                payload=payload,
            )
        )
    except Exception:
        post_lock_valid = False
    if not post_lock_valid:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_post_lock_state_changed",
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    try:
        credentials, redactor = credential_loader()
        transport = get_transport_factory(normalized_base_url, credentials)
        transport_base_url = apply_core.validate_apply_base_url(transport.base_url)
        if (
            transport_base_url != normalized_base_url
            or type(transport.network_requests_performed) is not int
            or transport.network_requests_performed < 0
            or type(transport.write_requests_performed) is not int
            or transport.write_requests_performed != 0
        ):
            raise WooPendingReconciliationError(
                "woo_apply_reconciliation_transport_invalid"
            )
    except Exception:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_get_setup_failed",
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    try:
        products, raw_products = apply_core._collect_exact(transport, sku)
    except Exception:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_remote_get_failed",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    if (
        type(transport.network_requests_performed) is not int
        or transport.network_requests_performed <= 0
        or type(transport.write_requests_performed) is not int
        or transport.write_requests_performed != 0
    ):
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_transport_not_read_only",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    if len(products) == 0 and len(raw_products) == 0:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_remote_absent",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )
    if (
        len(products) != 1
        or len(raw_products) != 1
        or not apply_core._payload_projection_matches(payload, raw_products[0])
    ):
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_remote_state_inconsistent",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )
    product_id = products[0]["id"]

    try:
        if after_get_hook is not None:
            after_get_hook()
        final_pending_valid = (
            not receipt_path.exists()
            and pending_path.exists()
            and _lock_sha256(lock_path) == owned_lock_sha256
            and _pending_still_exact(
                pending_path,
                plan_source=plan_source,
                pending_source=pending_source,
                plan_hash=plan_hash,
                sku=sku,
                payload=payload,
            )
        )
    except Exception:
        final_pending_valid = False
    if not final_pending_valid:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_pre_receipt_state_changed",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    receipt = _build_recovery_receipt(
        plan_source=plan_source,
        pending_source=pending_source,
        plan_hash=plan_hash,
        sku=sku,
        payload=payload,
        product_id=product_id,
        network_requests=transport.network_requests_performed,
    )
    try:
        receipt_writer(receipt_path, receipt, redactor)
        (
            persisted_receipt,
            persisted_receipt_source,
            _,
        ) = _read_recovery_receipt_with_source(receipt_path)
        validate_recovery_receipt(
            persisted_receipt,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
            expected_pending_source=pending_source,
        )
    except Exception:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_receipt_write_or_verify_failed",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    try:
        if before_pending_cleanup_hook is not None:
            before_pending_cleanup_hook()
        if (
            not lock_path.exists()
            or _lock_sha256(lock_path) != owned_lock_sha256
            or not pending_path.exists()
            or not receipt_path.exists()
        ):
            raise WooPendingReconciliationError(
                "woo_apply_reconciliation_pre_cleanup_state_changed"
            )
        (
            current_receipt,
            current_receipt_source,
            _,
        ) = _read_recovery_receipt_with_source(receipt_path)
        validate_recovery_receipt(
            current_receipt,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
            expected_pending_source=pending_source,
        )
        pre_cleanup_valid = (
            current_receipt_source == persisted_receipt_source
            and _pending_still_exact(
                pending_path,
                plan_source=plan_source,
                pending_source=pending_source,
                plan_hash=plan_hash,
                sku=sku,
                payload=payload,
            )
        )
    except Exception:
        pre_cleanup_valid = False
    if not pre_cleanup_valid:
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_pre_cleanup_state_changed",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )
    try:
        unlinker(pending_path)
        if pending_path.exists():
            raise OSError("pending cleanup incomplete")
    except (OSError, RuntimeError):
        return _release_or_recovery(
            _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_reconciliation_pending_cleanup_failed",
                transport=transport,
            ),
            lock_path=lock_path,
            owned_lock_sha256=owned_lock_sha256,
            unlinker=unlinker,
        )

    if not _release_owned_lock(lock_path, owned_lock_sha256, unlinker):
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_reconciliation_lock_cleanup_failed",
            transport=transport,
        )

    try:
        (
            final_receipt,
            final_receipt_source,
            _,
        ) = _read_recovery_receipt_with_source(receipt_path)
        validate_recovery_receipt(
            final_receipt,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
            expected_pending_source=pending_source,
        )
        final_state_valid = (
            receipt_path.exists()
            and final_receipt_source == persisted_receipt_source
            and not pending_path.exists()
            and not lock_path.exists()
        )
    except Exception:
        final_state_valid = False
    if not final_state_valid:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_reconciliation_final_state_invalid",
            transport=transport,
        )

    result = _result(
        "reconciled",
        EXIT_RECONCILED,
        "woo_apply_pending_reconciled",
        transport=transport,
    )
    result["product"] = {"id": product_id, "sku": sku}
    result["source_pending"] = dict(pending_source)
    return result
