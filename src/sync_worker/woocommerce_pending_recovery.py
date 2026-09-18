"""Read-only decision engine for one uncertain Woo apply Pending journal."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from . import woocommerce_product_apply as apply_core
from . import woocommerce_target_snapshot as target_snapshot
from .sanitization import Redactor
from .woocommerce_category_discovery import WooCategoryCredentials


MAX_PENDING_BYTES = 16 * 1024 * 1024

EXIT_BLOCKED = 1
EXIT_PRE_WRITE_ERROR = 2
EXIT_RECOVERY_REQUIRED = 3

_PENDING_FIELDS = frozenset(
    {
        "plan_hash",
        "source_plan",
        "target",
        "operation",
        "manual_confirmation_verified",
        "status",
        "policy_version",
        "post_state",
        "post_attempts_started",
        # The existing POC-03C serializer injects this zero-valued audit field.
        "write_requests_performed",
    }
)


class WooPendingRecoveryError(ValueError):
    """Fixed-code local Pending decision failure."""


class WooPendingStateUnsupported(WooPendingRecoveryError):
    """The Pending is validly bound but outside the attempting=1 scope."""


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


def _read_pending(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    """Read and raw-byte fingerprint the already-hardened canonical Pending."""

    try:
        local = target_snapshot._safe_local_json_file(path)
        size = local.stat().st_size
        if size <= 0 or size > MAX_PENDING_BYTES:
            raise WooPendingRecoveryError("woo_apply_pending_size_invalid")
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooPendingRecoveryError:
        raise
    except target_snapshot.WooTargetSnapshotInputError as error:
        code = (
            "woo_apply_pending_duplicate_json_key"
            if str(error) == "woo_target_snapshot_duplicate_json_key"
            else "woo_apply_pending_local_json_required"
        )
        raise WooPendingRecoveryError(code) from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooPendingRecoveryError("woo_apply_pending_json_invalid") from None
    if not isinstance(value, Mapping):
        raise WooPendingRecoveryError("woo_apply_pending_root_invalid")
    return (
        value,
        {"basename": local.name, "sha256": hashlib.sha256(raw).hexdigest()},
        local,
    )


def _validate_pending(
    pending: Mapping[str, object],
    *,
    plan_source: Mapping[str, str],
    plan_hash: str,
    sku: str,
    payload: Mapping[str, object],
) -> None:
    """Cross-bind one canonical Pending to the exact current frozen Plan."""

    if (
        set(pending) != _PENDING_FIELDS
        or pending.get("status") != "pending"
        or pending.get("policy_version") != apply_core.PENDING_POLICY_VERSION
        or pending.get("plan_hash") != plan_hash
        or pending.get("source_plan") != dict(plan_source)
        or pending.get("target") != apply_core._TARGET
        or pending.get("manual_confirmation_verified") is not True
        or type(pending.get("write_requests_performed")) is not int
        or pending.get("write_requests_performed") != 0
    ):
        raise WooPendingRecoveryError("woo_apply_pending_contract_invalid")

    operation = pending.get("operation")
    if not isinstance(operation, Mapping) or set(operation) != {
        "action",
        "sku",
        "payload_sha256",
    }:
        raise WooPendingRecoveryError("woo_apply_pending_operation_invalid")
    if operation != {
        "action": "create",
        "sku": sku,
        "payload_sha256": apply_core.canonical_payload_hash(payload),
    }:
        raise WooPendingRecoveryError("woo_apply_pending_operation_mismatch")

    if (
        pending.get("post_state") != "attempting"
        or type(pending.get("post_attempts_started")) is not int
        or pending.get("post_attempts_started") != 1
    ):
        raise WooPendingStateUnsupported("woo_apply_pending_state_not_supported")


def inspect_woo_apply_pending(
    plan_report_path: Path,
    confirmed_plan_hash: str,
    base_url: str,
    *,
    project_root: Path,
    credential_loader: CredentialLoader = apply_core._default_credential_loader,
    get_transport_factory: GetTransportFactory = (
        target_snapshot.StdlibWooProductTargetTransport
    ),
) -> dict[str, object]:
    """Observe remote state for attempting=1 without mutating runtime state."""

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
            "woo_apply_pending_plan_or_target_invalid",
        )

    try:
        pending_path, receipt_path, lock_path = apply_core._runtime_paths(project_root)
    except (OSError, RuntimeError, apply_core.WooProductApplyError):
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_runtime_state_requires_recovery",
        )

    try:
        if lock_path.exists():
            return _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_pending_runtime_state_requires_recovery",
            )
        if receipt_path.exists():
            return _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_pending_runtime_state_requires_recovery",
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
            "woo_apply_pending_runtime_state_requires_recovery",
        )

    try:
        pending, pending_source, _ = _read_pending(pending_path)
        _validate_pending(
            pending,
            plan_source=plan_source,
            plan_hash=plan_hash,
            sku=sku,
            payload=payload,
        )
    except WooPendingStateUnsupported:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_state_not_supported",
        )
    except WooPendingRecoveryError:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_pending_contract_invalid",
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
            raise WooPendingRecoveryError("woo_apply_pending_transport_invalid")
    except Exception:
        return _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_pending_get_setup_failed",
        )

    try:
        products, raw_products = apply_core._collect_exact(transport, sku)
    except Exception:
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_remote_get_failed",
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
            "woo_apply_pending_transport_not_read_only",
            transport=transport,
        )

    if len(products) == 0 and len(raw_products) == 0:
        result = _result(
            "recovery_observation",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_remote_absent",
            transport=transport,
        )
        result["decision"] = "not_applied_observed"
        result["source_pending"] = dict(pending_source)
        return result

    if (
        len(products) == 1
        and len(raw_products) == 1
        and apply_core._payload_projection_matches(payload, raw_products[0])
    ):
        product_id = products[0]["id"]
        result = _result(
            "recovery_observation",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_remote_exact",
            transport=transport,
        )
        result["decision"] = "applied_reconcilable"
        result["product"] = {"id": product_id, "sku": sku}
        result["source_pending"] = dict(pending_source)
        return result

    return _result(
        "recovery_required",
        EXIT_RECOVERY_REQUIRED,
        "woo_apply_pending_remote_state_inconsistent",
        transport=transport,
    )
