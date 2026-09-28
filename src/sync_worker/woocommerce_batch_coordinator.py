"""Sequential, stop-on-non-success orchestration of the existing Item Adapter.

All state here is invocation-local. There is no cursor, batch journal, cleanup,
retry, credential loading or transport. Runtime/Adapter retain their validation
authority; this module only binds snapshots and checks returned result envelopes.
One caller per workspace is an operational requirement, not a batch-wide lock.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Callable, Literal, Protocol

from . import woocommerce_batch_item_adapter as adapter
from . import woocommerce_batch_runtime as runtime_core
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


BatchOperation = Literal["apply", "verify_receipt"]
BatchStatus = Literal["completed", "blocked", "local_error", "recovery_required"]
BatchItemStatus = Literal[
    "NOT_DISPATCHED", "APPLIED", "ALREADY_APPLIED", "BLOCKED", "RECOVERY_REQUIRED"
]
AuthorizationProvider = Callable[
    [str, runtime_core.BatchItemRuntime, BatchOperation],
    adapter.ManualAuthorizationContext | None,
]
RuntimeLoader = Callable[[Path], runtime_core.BatchRuntime]
_OPERATIONS = {"NOT_STARTED": "apply", "ALREADY_APPLIED_OBSERVED": "verify_receipt"}
_SUCCESS_STATES = {"apply": "APPLIED", "verify_receipt": "ALREADY_APPLIED"}
_DISPOSITIONS = {0: "success", 1: "blocked", 2: "local_error", 3: "recovery_required"}
_BATCH_STATUSES = {0: "completed", 1: "blocked", 2: "local_error", 3: "recovery_required"}


class ItemAdapter(Protocol):
    def __call__(
        self, item: runtime_core.BatchItemRuntime,
        authorization: adapter.ManualAuthorizationContext,
        *, operation: BatchOperation, base_url: str,
    ) -> adapter.ItemExecutionResult: ...


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    sequence: int
    sku: str
    plan_hash: str
    observed_state: adapter.ItemState
    status: BatchItemStatus
    operation: BatchOperation | None
    dispatched: bool
    result_code: str
    execution_result: adapter.ItemExecutionResult | None


@dataclass(frozen=True, slots=True)
class BatchExecutionResult:
    batch_hash: str | None
    status: BatchStatus
    exit_code: int
    result_code: str
    items: tuple[BatchItemResult, ...]
    total_items: int
    dispatched_items: int
    successful_items: int
    stopped_sequence: int | None
    counters: adapter.ItemCounters
    counters_complete: bool


class _RefreshError(Exception):
    def __init__(self, exit_code: int, code: str) -> None:
        self.exit_code = exit_code
        self.code = code


def _identity(value: runtime_core.BatchRuntime) -> tuple[object, ...]:
    """Compare frozen membership/order, never compare stale observed states.

    This is a dependency envelope check, not another Plan/path/hash validator.
    Only the Runtime Loader verifies authorities and filesystem safety.
    """
    if (type(value) is not runtime_core.BatchRuntime
            or type(value.batch_hash) is not str
            or not isinstance(value.workspace_root, Path)
            or type(value.items) is not tuple or not value.items):
        raise ValueError
    for item in value.items:
        if (type(item) is not runtime_core.BatchItemRuntime
                or type(item.sequence) is not int
                or type(item.sku) is not str or type(item.plan_hash) is not str
                or not isinstance(item.plan_path, Path)):
            raise ValueError
    return (
        value.batch_hash, value.workspace_root,
        tuple((item.sequence, item.sku, item.plan_hash, item.plan_path) for item in value.items),
    )


def _reload(expected: runtime_core.BatchRuntime, loader: RuntimeLoader) -> runtime_core.BatchRuntime:
    try:
        identity = _identity(expected)
        fresh = loader(expected.workspace_root)
        fresh_identity = _identity(fresh)
    except runtime_core.WooBatchRuntimeError:
        raise _RefreshError(1, "woo_batch_runtime_rejected") from None
    except (Exception, KeyboardInterrupt):
        raise _RefreshError(2, "woo_batch_runtime_local_error") from None
    if fresh_identity != identity:
        raise _RefreshError(1, "woo_batch_identity_mismatch")
    return fresh


def _observe(rows: list[BatchItemResult], fresh: runtime_core.BatchRuntime) -> None:
    for index, item in enumerate(fresh.items):
        rows[index] = replace(rows[index], observed_state=item.state)


def _safe_text(value: object) -> bool:
    return (type(value) is str and Redactor().text(value) == value
            and REPORT_SECRET_SCAN_PATTERN.search(value) is None)


def _valid_result(
    value: object, batch_hash: str, item: runtime_core.BatchItemRuntime, operation: BatchOperation,
) -> bool:
    """Check dispatch identity/outcome shape, not Core business validation/codes."""
    if (type(value) is not adapter.ItemExecutionResult
            or type(value.sequence) is not int or value.sequence != item.sequence
            or value.sku != item.sku or value.plan_hash != item.plan_hash
            or value.operation != operation
            or type(value.exit_code) is not int or value.exit_code not in _DISPOSITIONS
            or value.disposition != _DISPOSITIONS[value.exit_code]
            or type(value.provenance) is not adapter.ItemProvenance
            or value.provenance.batch_hash != batch_hash
            or value.provenance.adapter != adapter.ADAPTER_VERSION
            or type(value.counters) is not adapter.ItemCounters):
        return False
    if any(count is not None and (type(count) is not int or count < 0)
           for count in (getattr(value.counters, field.name) for field in fields(adapter.ItemCounters))):
        return False
    if value.state_before not in {*_OPERATIONS, "RECOVERY_REQUIRED", "BLOCKED"}:
        return False
    if value.exit_code == 0:
        if value.state_before != item.state or value.state_after != _SUCCESS_STATES[operation]:
            return False
    elif value.state_after != ("RECOVERY_REQUIRED" if value.exit_code == 3 else "BLOCKED"):
        return False
    # The Adapter already projects safe metadata. Fail closed if an injected or
    # broken dependency instead returns sensitive text; do not echo that result.
    texts = (value.sku, value.plan_hash, value.result_code, value.provenance.batch_hash,
             value.provenance.adapter)
    optional = (value.provenance.core_function, value.provenance.core_status,
                value.provenance.core_result_code)
    return all(_safe_text(text) for text in texts) and all(
        text is None or _safe_text(text) for text in optional
    )


def _add_counters(left: adapter.ItemCounters, right: adapter.ItemCounters) -> adapter.ItemCounters:
    values = {}
    for field in fields(adapter.ItemCounters):
        a, b = getattr(left, field.name), getattr(right, field.name)
        values[field.name] = None if a is None or b is None else a + b
    return adapter.ItemCounters(**values)


def _summary(
    batch_hash: str | None, rows: list[BatchItemResult], exit_code: int, code: str,
    counters: adapter.ItemCounters, stopped_sequence: int | None = None,
) -> BatchExecutionResult:
    return BatchExecutionResult(
        batch_hash, _BATCH_STATUSES[exit_code], exit_code, code, tuple(rows), len(rows),
        sum(row.dispatched for row in rows),
        sum(row.status in {"APPLIED", "ALREADY_APPLIED"} for row in rows),
        stopped_sequence, counters,
        all(getattr(counters, field.name) is not None for field in fields(adapter.ItemCounters)),
    )


def run_woo_batch(
    runtime: runtime_core.BatchRuntime,
    authorization_provider: AuthorizationProvider,
    *, base_url: str, item_adapter: ItemAdapter | None = None,
    runtime_loader: RuntimeLoader | None = None,
) -> BatchExecutionResult:
    """Start at sequence one on every call; synchronously dispatch once per item.

    Providers must return pre-existing manual approvals without external effects.
    No confirmation is constructed here. Adapter alone validates authorization.
    Counters concern this invocation, never previous Receipt/summary counters.
    ``dispatched`` counts Adapter attempts, not Core calls or network requests.
    """
    loader = runtime_core.load_woo_batch_runtime if runtime_loader is None else runtime_loader
    dispatch = adapter.execute_batch_item if item_adapter is None else item_adapter
    counters = adapter.ItemCounters()
    try:
        frozen = _reload(runtime, loader)
    except _RefreshError as error:
        return _summary(None, [], error.exit_code, error.code, counters)
    rows = [BatchItemResult(
        item.sequence, item.sku, item.plan_hash, item.state, "NOT_DISPATCHED", None,
        False, "woo_batch_not_dispatched", None,
    ) for item in frozen.items]

    def stop(index: int, exit_code: int, code: str, *, item_code: str | None = None) -> BatchExecutionResult:
        rows[index] = replace(
            rows[index], status="RECOVERY_REQUIRED" if exit_code == 3 else "BLOCKED",
            result_code=item_code or code,
        )
        return _summary(frozen.batch_hash, rows, exit_code, code, counters, rows[index].sequence)

    for index, _ in enumerate(frozen.items):
        try:
            fresh = _reload(frozen, loader)
        except _RefreshError as error:
            return stop(index, error.exit_code, error.code)
        _observe(rows, fresh)
        item = fresh.items[index]
        if item.state == "RECOVERY_REQUIRED":
            return stop(index, 3, "woo_batch_item_recovery_required")
        if item.state == "BLOCKED":
            return stop(index, 1, "woo_batch_item_blocked")
        if type(item.state) is not str or item.state not in _OPERATIONS:
            return stop(index, 2, "woo_batch_runtime_state_invalid")
        operation = _OPERATIONS[item.state]
        rows[index] = replace(rows[index], operation=operation)
        try:
            authorization = authorization_provider(frozen.batch_hash, item, operation)
        except (Exception, KeyboardInterrupt):
            return stop(index, 2, "woo_batch_authorization_provider_failed")
        if authorization is None:
            return stop(index, 1, "woo_batch_authorization_missing")
        if type(authorization) is not adapter.ManualAuthorizationContext:
            return stop(index, 2, "woo_batch_authorization_provider_invalid")

        rows[index] = replace(rows[index], dispatched=True)
        try:
            result = dispatch(item, authorization, operation=operation, base_url=base_url)
            valid = _valid_result(result, frozen.batch_hash, item, operation)
        except (Exception, KeyboardInterrupt):
            counters = _add_counters(counters, adapter.ItemCounters.unknown())
            return stop(index, 3, "woo_batch_adapter_exception")
        if not valid:
            # Do not attribute an unrelated/malformed result's counters or expose
            # its contents. This invocation may already have had external effects.
            counters = _add_counters(counters, adapter.ItemCounters.unknown())
            return stop(index, 3, "woo_batch_adapter_result_invalid")
        counters = _add_counters(counters, result.counters)
        rows[index] = replace(rows[index], execution_result=result, result_code=result.result_code)
        if result.exit_code != 0:
            return stop(index, result.exit_code, "woo_batch_item_failed", item_code=result.result_code)

        # Presence-only consistency check after success; never validate or create
        # a Receipt here. Fresh remote proof still belongs exclusively to Core.
        try:
            after = _reload(frozen, loader)
        except _RefreshError:
            return stop(index, 3, "woo_batch_post_dispatch_runtime_unconfirmed")
        _observe(rows, after)
        if after.items[index].state != "ALREADY_APPLIED_OBSERVED":
            return stop(index, 3, "woo_batch_post_dispatch_runtime_unconfirmed")
        rows[index] = replace(rows[index], status=result.state_after)

    return _summary(frozen.batch_hash, rows, 0, "woo_batch_completed", counters)
