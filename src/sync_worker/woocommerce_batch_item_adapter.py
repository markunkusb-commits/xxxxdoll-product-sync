"""One explicitly authorized item invocation; no scheduling or transaction logic.

Runtime observations are re-bound to the frozen workspace before dispatch. Only
the existing Single Product functions own credentials, transport and journals.
There is no retry, fallback, auto-recovery, or authorization generated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_apply_idempotency as receipt_core
from . import woocommerce_batch_runtime as runtime
from . import woocommerce_batch_workspace as workspace
from . import woocommerce_pending_reconciliation as reconciliation_core
from . import woocommerce_pending_recovery as recovery_core
from . import woocommerce_product_apply as apply_core


ADAPTER_VERSION = "woo-batch-item-adapter-v1"
Operation = Literal["apply", "verify_receipt", "inspect_pending", "reconcile_pending"]
ItemState = Literal[
    "NOT_STARTED", "ALREADY_APPLIED_OBSERVED", "RECOVERY_REQUIRED", "BLOCKED",
    "APPLIED", "ALREADY_APPLIED",
]
_FUNCTIONS = {
    "apply": "run_woo_product_apply",
    "verify_receipt": "run_woo_apply_receipt_verification",
    "inspect_pending": "inspect_woo_apply_pending",
    "reconcile_pending": "reconcile_woo_apply_pending",
}
_STATE_OPERATIONS = {
    "NOT_STARTED": frozenset({"apply"}),
    "ALREADY_APPLIED_OBSERVED": frozenset({"verify_receipt"}),
    "RECOVERY_REQUIRED": frozenset({"inspect_pending", "reconcile_pending"}),
    "BLOCKED": frozenset(),
}
_SUCCESS = {
    "apply": ("applied", "woo_apply_applied", "APPLIED"),
    "verify_receipt": ("already_applied", "woo_apply_already_applied", "ALREADY_APPLIED"),
    "reconcile_pending": ("reconciled", "woo_apply_pending_reconciled", "APPLIED"),
}
_FAILURE_STATUSES = {
    1: frozenset({"blocked", "blocked_pre_write"}),
    2: frozenset({"pre_write_error"}),
    3: frozenset({"recovery_required", "recovery_observation"}),
}
_OPERATION_FAILURE_CODES = {
    "apply": frozenset(
        {
            "woo_apply_target_sku_already_exists",
            "woo_apply_target_sku_ambiguous",
            "woo_apply_preflight_get_failed",
            "woo_apply_attempt_marker_failed",
            "woo_apply_readback_failed",
            "woo_apply_post_count_invalid",
            "woo_apply_readback_mismatch",
            "woo_apply_pending_prepare_failed",
            "woo_apply_pending_state_uncertain",
            "woo_apply_pending_cleanup_failed",
            "woo_apply_receipt_write_failed",
        }
    ),
    "verify_receipt": frozenset(
        {
            "woo_apply_receipt_not_found",
            "woo_apply_receipt_plan_or_target_invalid",
            "woo_apply_receipt_input_invalid",
            "woo_apply_receipt_contract_invalid",
            "woo_apply_receipt_runtime_state_requires_recovery",
            "woo_apply_receipt_get_setup_failed",
            "woo_apply_receipt_remote_get_failed",
            "woo_apply_receipt_transport_not_read_only",
            "woo_apply_receipt_remote_state_mismatch",
        }
    ),
    "inspect_pending": frozenset(
        {
            "woo_apply_pending_not_found",
            "woo_apply_pending_plan_or_target_invalid",
            "woo_apply_pending_runtime_state_requires_recovery",
            "woo_apply_pending_contract_invalid",
            "woo_apply_pending_get_setup_failed",
            "woo_apply_pending_remote_get_failed",
            "woo_apply_pending_transport_not_read_only",
            "woo_apply_pending_remote_absent",
            "woo_apply_pending_remote_exact",
            "woo_apply_pending_remote_state_inconsistent",
        }
    ),
    "reconcile_pending": frozenset(
        {
            "woo_apply_reconciliation_plan_or_target_invalid",
            "woo_apply_reconciliation_runtime_state_invalid",
            "woo_apply_reconciliation_runtime_state_requires_recovery",
            "woo_apply_pending_confirmation_mismatch",
            "woo_apply_reconciliation_lock_acquire_failed",
            "woo_apply_reconciliation_lock_unverifiable",
            "woo_apply_reconciliation_post_lock_state_changed",
            "woo_apply_reconciliation_get_setup_failed",
            "woo_apply_reconciliation_remote_get_failed",
            "woo_apply_reconciliation_transport_not_read_only",
            "woo_apply_reconciliation_remote_absent",
            "woo_apply_reconciliation_remote_state_inconsistent",
            "woo_apply_reconciliation_pre_receipt_state_changed",
            "woo_apply_reconciliation_receipt_write_or_verify_failed",
            "woo_apply_reconciliation_pre_cleanup_state_changed",
            "woo_apply_reconciliation_pending_cleanup_failed",
            "woo_apply_reconciliation_lock_cleanup_failed",
            "woo_apply_reconciliation_final_state_invalid",
        }
    ),
}
# Only fixed Core audit codes may cross the adapter boundary. Unknown codes,
# exception text and arbitrary extra response fields are never serialized.
_CORE_CODES = frozenset("""
    woo_apply_applied woo_apply_already_applied woo_apply_pending_reconciled
    woo_apply_existing_runtime_state woo_apply_receipt_already_exists
    woo_apply_existing_lock woo_apply_runtime_state_race
    woo_apply_lock_cleanup_failed woo_apply_preflight_get_failed
    woo_apply_target_sku_already_exists woo_apply_target_sku_ambiguous
    woo_apply_pending_prepare_failed woo_apply_pending_state_uncertain
    woo_apply_attempt_marker_failed woo_apply_readback_failed
    woo_apply_post_count_invalid woo_apply_readback_mismatch
    woo_apply_receipt_write_failed woo_apply_pending_cleanup_failed
    woo_apply_receipt_plan_or_target_invalid woo_apply_receipt_not_found
    woo_apply_receipt_input_invalid woo_apply_receipt_contract_invalid
    woo_apply_receipt_runtime_state_requires_recovery
    woo_apply_receipt_get_setup_failed woo_apply_receipt_remote_get_failed
    woo_apply_receipt_transport_not_read_only
    woo_apply_receipt_remote_match_count_invalid woo_apply_receipt_remote_state_mismatch
    woo_apply_pending_plan_or_target_invalid
    woo_apply_pending_runtime_state_requires_recovery woo_apply_pending_not_found
    woo_apply_pending_state_not_supported woo_apply_pending_contract_invalid
    woo_apply_pending_get_setup_failed woo_apply_pending_remote_get_failed
    woo_apply_pending_transport_not_read_only woo_apply_pending_remote_absent
    woo_apply_pending_remote_exact woo_apply_pending_remote_state_inconsistent
    woo_apply_reconciliation_plan_or_target_invalid
    woo_apply_reconciliation_runtime_state_invalid
    woo_apply_reconciliation_runtime_state_requires_recovery
    woo_apply_pending_confirmation_mismatch woo_apply_reconciliation_lock_acquire_failed
    woo_apply_reconciliation_lock_unverifiable woo_apply_reconciliation_post_lock_state_changed
    woo_apply_reconciliation_get_setup_failed woo_apply_reconciliation_remote_get_failed
    woo_apply_reconciliation_transport_not_read_only woo_apply_reconciliation_remote_absent
    woo_apply_reconciliation_remote_state_inconsistent
    woo_apply_reconciliation_pre_receipt_state_changed
    woo_apply_reconciliation_receipt_write_or_verify_failed
    woo_apply_reconciliation_pre_cleanup_state_changed
    woo_apply_reconciliation_pending_cleanup_failed
    woo_apply_reconciliation_lock_cleanup_failed woo_apply_reconciliation_final_state_invalid
""".split())


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualAuthorizationContext:
    """Caller-supplied confirmations, never inferred from Plans or Pending bytes."""

    batch_hash: str
    sequence: int
    sku: str
    confirmed_plan_hash: str
    allowed_operations: frozenset[Operation]
    confirmed_pending_sha256: str | None = None

    def __post_init__(self) -> None:
        # Copy mutable collections rather than retaining a caller-owned set.
        if not isinstance(self.allowed_operations, (set, frozenset, tuple, list)):
            raise ValueError("woo_batch_item_authorization_invalid")
        if any(type(value) is not str or value not in _FUNCTIONS
               for value in self.allowed_operations):
            raise ValueError("woo_batch_item_authorization_invalid")
        object.__setattr__(self, "allowed_operations", frozenset(self.allowed_operations))


@dataclass(frozen=True, slots=True)
class ItemProvenance:
    batch_hash: str | None
    adapter: str = ADAPTER_VERSION
    core_function: str | None = None
    core_status: str | None = None
    core_result_code: str | None = None


@dataclass(frozen=True, slots=True)
class ItemCounters:
    """Current invocation only. None means unknown, not a claim of zero."""

    network_requests_performed: int | None = 0
    woocommerce_requests_performed: int | None = 0
    woocommerce_write_requests_performed: int | None = 0
    wordpress_requests_performed: int | None = 0
    external_write_requests_performed: int | None = 0
    write_requests_performed: int | None = 0

    @classmethod
    def unknown(cls) -> ItemCounters:
        return cls(**{field.name: None for field in fields(cls)})


@dataclass(frozen=True, slots=True)
class ItemExecutionResult:
    """Safe outcome projection, not a replacement for a Receipt or runtime scan."""

    sequence: int | None
    sku: str | None
    plan_hash: str | None
    state_before: ItemState
    state_after: ItemState
    operation: Operation | None
    disposition: Literal["success", "blocked", "local_error", "recovery_required"]
    exit_code: int
    result_code: str
    provenance: ItemProvenance
    counters: ItemCounters


@dataclass(frozen=True, slots=True)
class _Binding:
    batch_hash: str
    item: runtime.BatchItemRuntime
    item_root: Path
    receipt_path: Path


def _bind(item: runtime.BatchItemRuntime) -> _Binding:
    # Reuse the hardened path policy and Runtime Loader, including raw Plan and
    # batch membership validation. A freely constructed dataclass is not proof.
    if not isinstance(item, runtime.BatchItemRuntime) or type(item.sequence) is not int:
        raise ValueError("woo_batch_item_binding_invalid")
    plan = package_io._local_path(item.plan_path, require_file=True)
    observed = runtime.load_woo_batch_runtime(plan.parents[3])
    current = next(value for value in observed.items if value.sequence == item.sequence)
    if (plan != current.plan_path or item.sku != current.sku
            or item.plan_hash != current.plan_hash):
        raise ValueError("woo_batch_item_binding_invalid")
    item_root = current.plan_path.parent.parent
    _, receipt, _ = apply_core._runtime_paths(item_root)
    return _Binding(observed.batch_hash, current, item_root, receipt)


def _outcome(
    bound: _Binding | None, operation: Operation | None, exit_code: int, code: str,
    *, state_before: ItemState | None = None, state_after: ItemState | None = None,
    core_function: str | None = None, core_status: str | None = None,
    core_result_code: str | None = None, counters: ItemCounters = ItemCounters(),
) -> ItemExecutionResult:
    item = bound.item if bound else None
    before = state_before or (item.state if item else "BLOCKED")
    after = state_after or ("RECOVERY_REQUIRED" if exit_code == 3 else "BLOCKED")
    return ItemExecutionResult(
        item.sequence if item else None, item.sku if item else None,
        item.plan_hash if item else None, before, after, operation,
        {0: "success", 1: "blocked", 2: "local_error", 3: "recovery_required"}[exit_code],
        exit_code, code,
        ItemProvenance(bound.batch_hash if bound else None, ADAPTER_VERSION,
                       core_function, core_status, core_result_code),
        counters,
    )


def _authorized(bound: _Binding, auth: ManualAuthorizationContext, operation: Operation) -> bool:
    return (
        isinstance(auth, ManualAuthorizationContext)
        and type(auth.sequence) is int and auth.sequence == bound.item.sequence
        and type(auth.batch_hash) is str and auth.batch_hash == bound.batch_hash
        and type(auth.sku) is str and auth.sku == bound.item.sku
        and type(auth.confirmed_plan_hash) is str
        and auth.confirmed_plan_hash == bound.item.plan_hash
        and operation in auth.allowed_operations
        and (operation != "reconcile_pending" or (
            type(auth.confirmed_pending_sha256) is str
            and workspace._HASH_PATTERN.fullmatch(auth.confirmed_pending_sha256) is not None
        ))
    )

def _convert(bound: _Binding, operation: Operation, value: object) -> ItemExecutionResult:
    function = _FUNCTIONS[operation]
    counters = ItemCounters.unknown()

    valid = isinstance(value, Mapping)

    if valid:
        counts = {
            field.name: value.get(field.name)
            for field in fields(ItemCounters)
        }
        valid = all(
            type(count) is int and count >= 0
            for count in counts.values()
        )
        if valid:
            counters = ItemCounters(**counts)

    status = value.get("status") if isinstance(value, Mapping) else None
    code = value.get("result_code") if isinstance(value, Mapping) else None
    exit_code = value.get("exit_code") if isinstance(value, Mapping) else None

    valid = (
        valid
        and type(status) is str
        and type(code) is str
        and code in _CORE_CODES
        and type(exit_code) is int
    )

    after = None

    if valid and exit_code == 0:
        expected = _SUCCESS.get(operation)
        valid = (
            expected is not None
            and (status, code) == expected[:2]
        )
        if valid:
            after = expected[2]

    elif valid:
        valid = (
            code
            in _OPERATION_FAILURE_CODES.get(
                operation,
                frozenset(),
            )
        )

    if not valid:
        return _outcome(
            bound,
            operation,
            3,
            "woo_batch_item_core_result_invalid",
            core_function=function,
            counters=counters,
        )

    return _outcome(
        bound,
        operation,
        exit_code,
        code,
        state_after=after,
        core_function=function,
        core_status=status,
        core_result_code=code,
        counters=counters,
    )


def execute_batch_item(
    item: runtime.BatchItemRuntime,
    authorization: ManualAuthorizationContext,
    *, operation: Operation | None, base_url: str,
) -> ItemExecutionResult:
    """Dispatch at most one explicit Core call, using only item-local paths.

    No operation is inferred from a state or granted by allowed_operations alone.
    A changed observation must be reviewed/reloaded by the caller, not retried.
    Success states reflect Core's validated result; future invocations re-observe
    the item. Inspect never authorizes reconciliation, even for a remote match.
    """
    safe_operation = operation if type(operation) is str and operation in _FUNCTIONS else None
    try:
        bound = _bind(item)
    except Exception:
        return _outcome(None, safe_operation, 2, "woo_batch_item_binding_invalid")
    if item.state == "BLOCKED":
        return _outcome(bound, safe_operation, 1, "woo_batch_item_blocked",
                        state_before="BLOCKED")
    if item.state != bound.item.state or item.runtime_files != bound.item.runtime_files:
        return _outcome(bound, safe_operation,
                        3 if bound.item.state == "RECOVERY_REQUIRED" else 2,
                        "woo_batch_item_observation_changed")
    if safe_operation is None or safe_operation not in _STATE_OPERATIONS[bound.item.state]:
        return _outcome(bound, safe_operation,
                        3 if bound.item.state == "RECOVERY_REQUIRED" else 1,
                        "woo_batch_item_operation_not_allowed")
    if not _authorized(bound, authorization, safe_operation):
        return _outcome(bound, safe_operation, 1, "woo_batch_item_authorization_invalid")
    try:
        target = apply_core.validate_apply_base_url(base_url)
    except (TypeError, ValueError, apply_core.WooProductApplyError):
        return _outcome(bound, safe_operation, 2, "woo_batch_item_target_invalid")

    # Confirmation is forwarded from the caller, never manufactured from item.
    common = dict(plan_report_path=bound.item.plan_path,
                  confirmed_plan_hash=authorization.confirmed_plan_hash,
                  base_url=target, project_root=bound.item_root)
    try:
        if safe_operation == "apply":
            value = apply_core.run_woo_product_apply(**common)
        elif safe_operation == "verify_receipt":
            value = receipt_core.run_woo_apply_receipt_verification(
                **common, receipt_report_path=bound.receipt_path)
        elif safe_operation == "inspect_pending":
            value = recovery_core.inspect_woo_apply_pending(**common)
        else:
            value = reconciliation_core.reconcile_woo_apply_pending(
                **common, confirmed_pending_sha256=authorization.confirmed_pending_sha256)
        return _convert(bound, safe_operation, value)
    except apply_core.WooProductApplyPreWriteError:
        return _outcome(bound, safe_operation, 2, "woo_batch_item_core_pre_write_error",
                        core_function=_FUNCTIONS[safe_operation], counters=ItemCounters.unknown())
    except (Exception, KeyboardInterrupt):
        # A Core call may already have side effects. Do not claim zero, echo the
        # exception, retry Apply, or attempt recovery without fresh authorization.
        return _outcome(bound, safe_operation, 3, "woo_batch_item_core_exception",
                        core_function=_FUNCTIONS[safe_operation], counters=ItemCounters.unknown())
