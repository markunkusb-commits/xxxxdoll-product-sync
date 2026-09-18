"""Read-only verification of an applied Woo receipt against fresh remote state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_product_apply as apply_core
from . import woocommerce_target_snapshot as target_snapshot
from .sanitization import Redactor
from .woocommerce_category_discovery import WooCategoryCredentials


MAX_RECEIPT_BYTES = 16 * 1024 * 1024

EXIT_ALREADY_APPLIED = 0
EXIT_BLOCKED = 1
EXIT_PRE_WRITE_ERROR = 2
EXIT_RECOVERY_REQUIRED = 3

_RECEIPT_FIELDS = frozenset(
    {
        "status",
        "policy_version",
        "plan_hash",
        "source_plan",
        "target",
        "operation",
        "product",
        "manual_confirmation_verified",
        "readback_verified",
        "post_transport_completed_without_error",
        "network_requests_performed",
        "woocommerce_requests_performed",
        "woocommerce_write_requests_performed",
        "wordpress_requests_performed",
        "external_write_requests_performed",
        "write_requests_performed",
    }
)


class WooApplyIdempotencyError(ValueError):
    """Fixed-code local receipt verification failure."""


class WooApplyReceiptNotFound(WooApplyIdempotencyError):
    """The requested safe local receipt path does not exist."""


CredentialLoader = Callable[[], tuple[WooCategoryCredentials, Redactor]]
GetTransportFactory = Callable[
    [str, WooCategoryCredentials], target_snapshot.WooProductTargetTransport
]


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


def _read_receipt(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    """Read and fingerprint one hardened local receipt without modifying it."""

    try:
        candidate = package_io._local_path(Path(path), require_file=False)
    except package_io.SingleProductStagingPackageInputError:
        raise WooApplyIdempotencyError(
            "woo_apply_receipt_local_json_required"
        ) from None
    try:
        if not candidate.exists():
            raise WooApplyReceiptNotFound("woo_apply_receipt_not_found")
    except WooApplyReceiptNotFound:
        raise
    except OSError:
        raise WooApplyIdempotencyError(
            "woo_apply_receipt_local_json_required"
        ) from None
    try:
        local = target_snapshot._safe_local_json_file(candidate)
        size = local.stat().st_size
        if size <= 0 or size > MAX_RECEIPT_BYTES:
            raise WooApplyIdempotencyError("woo_apply_receipt_size_invalid")
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooApplyIdempotencyError:
        raise
    except target_snapshot.WooTargetSnapshotInputError as error:
        code = (
            "woo_apply_receipt_duplicate_json_key"
            if str(error) == "woo_target_snapshot_duplicate_json_key"
            else "woo_apply_receipt_local_json_required"
        )
        raise WooApplyIdempotencyError(code) from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooApplyIdempotencyError("woo_apply_receipt_json_invalid") from None
    if not isinstance(value, Mapping):
        raise WooApplyIdempotencyError("woo_apply_receipt_root_invalid")
    return (
        value,
        {"basename": local.name, "sha256": hashlib.sha256(raw).hexdigest()},
        local,
    )


def _validate_receipt(
    receipt: Mapping[str, object],
    *,
    plan_source: Mapping[str, str],
    plan_hash: str,
    sku: str,
    payload: Mapping[str, object],
) -> int:
    """Cross-bind the receipt to the exact current frozen Plan and payload."""

    if (
        set(receipt) != _RECEIPT_FIELDS
        or receipt.get("status") != "applied"
        or receipt.get("policy_version") != apply_core.RECEIPT_POLICY_VERSION
        or receipt.get("plan_hash") != plan_hash
        or receipt.get("source_plan") != dict(plan_source)
        or receipt.get("target") != apply_core._TARGET
        or receipt.get("manual_confirmation_verified") is not True
        or receipt.get("readback_verified") is not True
        or type(receipt.get("post_transport_completed_without_error")) is not bool
    ):
        raise WooApplyIdempotencyError("woo_apply_receipt_contract_invalid")

    operation = receipt.get("operation")
    if not isinstance(operation, Mapping) or set(operation) != {
        "action",
        "sku",
        "payload_sha256",
    }:
        raise WooApplyIdempotencyError("woo_apply_receipt_operation_invalid")
    if operation != {
        "action": "create",
        "sku": sku,
        "payload_sha256": apply_core.canonical_payload_hash(payload),
    }:
        raise WooApplyIdempotencyError("woo_apply_receipt_operation_mismatch")

    product = receipt.get("product")
    if not isinstance(product, Mapping) or set(product) != {
        "id",
        "sku",
        "name",
        "type",
        "status",
    }:
        raise WooApplyIdempotencyError("woo_apply_receipt_product_invalid")
    product_id = product.get("id")
    if type(product_id) is not int or product_id <= 0:
        raise WooApplyIdempotencyError("woo_apply_receipt_product_id_invalid")
    if product != {
        "id": product_id,
        "sku": sku,
        "name": payload.get("name"),
        "type": payload.get("type"),
        "status": payload.get("status"),
    }:
        raise WooApplyIdempotencyError("woo_apply_receipt_product_mismatch")

    network_count = receipt.get("network_requests_performed")
    if (
        type(network_count) is not int
        or network_count < 3
        or receipt.get("woocommerce_requests_performed") != network_count
        or type(receipt.get("woocommerce_requests_performed")) is not int
        or receipt.get("woocommerce_write_requests_performed") != 1
        or type(receipt.get("woocommerce_write_requests_performed")) is not int
        or receipt.get("wordpress_requests_performed") != 0
        or type(receipt.get("wordpress_requests_performed")) is not int
        or receipt.get("external_write_requests_performed") != 1
        or type(receipt.get("external_write_requests_performed")) is not int
        or receipt.get("write_requests_performed") != 1
        or type(receipt.get("write_requests_performed")) is not int
    ):
        raise WooApplyIdempotencyError("woo_apply_receipt_counters_invalid")
    return product_id


def run_woo_apply_receipt_verification(
    plan_report_path: Path,
    receipt_report_path: Path,
    confirmed_plan_hash: str,
    base_url: str,
    *,
    project_root: Path,
    credential_loader: CredentialLoader = apply_core._default_credential_loader,
    get_transport_factory: GetTransportFactory = (
        target_snapshot.StdlibWooProductTargetTransport
    ),
) -> dict[str, object]:
    """Verify one success receipt using only a fresh exact-SKU GET."""

    try:
        frozen_plan, plan_source, _ = apply_core._read_plan(plan_report_path)
        sku, payload, plan_hash = apply_core.validate_frozen_plan(
            frozen_plan,
            confirmed_plan_hash,
        )
        normalized_base_url = apply_core.validate_apply_base_url(base_url)
    except apply_core.WooProductApplyError:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_receipt_plan_or_target_invalid",
        )

    try:
        receipt, receipt_source, _ = _read_receipt(receipt_report_path)
    except WooApplyReceiptNotFound:
        return _result(
            "blocked",
            EXIT_BLOCKED,
            "woo_apply_receipt_not_found",
        )
    except WooApplyIdempotencyError:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_receipt_input_invalid",
        )

    try:
        receipt_product_id = _validate_receipt(
            receipt,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
        )
    except WooApplyIdempotencyError:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_receipt_contract_invalid",
        )

    try:
        pending_path, _, lock_path = apply_core._runtime_paths(project_root)
        unresolved_runtime_state = pending_path.exists() or lock_path.exists()
    except (OSError, apply_core.WooProductApplyError):
        unresolved_runtime_state = True
    if unresolved_runtime_state:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_receipt_runtime_state_requires_recovery",
        )

    try:
        credentials, _ = credential_loader()
        transport = get_transport_factory(normalized_base_url, credentials)
        transport_base_url = apply_core.validate_apply_base_url(transport.base_url)
        if (
            transport_base_url != normalized_base_url
            or type(transport.network_requests_performed) is not int
            or transport.network_requests_performed < 0
            or type(transport.write_requests_performed) is not int
            or transport.write_requests_performed != 0
        ):
            raise WooApplyIdempotencyError("woo_apply_receipt_transport_invalid")
    except Exception:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_receipt_get_setup_failed",
        )

    try:
        products, raw_products = apply_core._collect_exact(transport, sku)
    except Exception:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_receipt_remote_get_failed",
            transport=transport,
        )

    if (
        type(transport.network_requests_performed) is not int
        or transport.network_requests_performed <= 0
        or type(transport.write_requests_performed) is not int
        or transport.write_requests_performed != 0
    ):
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_receipt_transport_not_read_only",
            transport=transport,
        )
    if len(products) != 1 or len(raw_products) != 1:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_receipt_remote_match_count_invalid",
            transport=transport,
        )
    if (
        products[0].get("id") != receipt_product_id
        or not apply_core._payload_projection_matches(payload, raw_products[0])
    ):
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_receipt_remote_state_mismatch",
            transport=transport,
        )

    result = _result(
        "already_applied",
        EXIT_ALREADY_APPLIED,
        "woo_apply_already_applied",
        transport=transport,
    )
    result["product"] = {"id": receipt_product_id, "sku": sku}
    result["source_receipt"] = dict(receipt_source)
    return result
