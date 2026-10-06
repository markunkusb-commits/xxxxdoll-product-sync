"""Strict, invocation-local manual approvals for normal batch execution.

Workspace values are comparison authority only. Every authorization value is
supplied by the operator, and the provider retains those immutable objects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_batch_workspace as workspace
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .woocommerce_batch_item_adapter import ManualAuthorizationContext
from .woocommerce_batch_runtime import BatchItemRuntime, BatchRuntime


POLICY_VERSION = "xxxxdoll-woo-batch-manual-authorization-v1"
AUTHORIZATION_INPUT_INVALID = "woo_batch_authorization_input_invalid"
AUTHORIZATION_BINDING_BLOCKED = "woo_batch_authorization_binding_blocked"
NORMAL_OPERATIONS = frozenset({"apply", "verify_receipt"})
MAX_AUTHORIZATION_BYTES = workspace.MAX_BATCH_REPORT_BYTES
_ROOT_FIELDS = frozenset({"policy_version", "batch_hash", "items"})
_ITEM_FIELDS = frozenset({"sequence", "sku", "confirmed_plan_hash", "allowed_operations"})


class WooBatchAuthorizationError(ValueError):
    """Fixed safe outcomes; input contents and paths never enter the message."""

    def __init__(self, *, blocked: bool = False) -> None:
        self.exit_code = 1 if blocked else 2
        self.result_code = AUTHORIZATION_BINDING_BLOCKED if blocked else AUTHORIZATION_INPUT_INVALID
        super().__init__(self.result_code)


def _safe_sku(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and not any(char.isspace() or ord(char) < 32 or char in "/\\:" for char in value)
        and Redactor().text(value) == value
        and REPORT_SECRET_SCAN_PATTERN.search(value) is None
    )


@dataclass(frozen=True, slots=True)
class BatchManualAuthorization:
    batch_hash: str
    items: tuple[ManualAuthorizationContext, ...]
    _by_sequence: Mapping[int, ManualAuthorizationContext] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_sequence", MappingProxyType({
            item.sequence: item for item in self.items
        }))

    def __call__(
        self, batch_hash: str, item: BatchItemRuntime, operation: str,
    ) -> ManualAuthorizationContext | None:
        approval = self._by_sequence.get(item.sequence)
        if (batch_hash != self.batch_hash or approval is None
                or approval.sku != item.sku or approval.confirmed_plan_hash != item.plan_hash
                or operation not in NORMAL_OPERATIONS):
            return None
        # Adapter checks the requested operation against the unchanged approval.
        return approval


def parse_woo_batch_authorization(value: object, runtime: BatchRuntime) -> BatchManualAuthorization:
    """Validate the entire input before making any approvals available."""
    if (not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS
            or value.get("policy_version") != POLICY_VERSION
            or type(value.get("batch_hash")) is not str
            or workspace._HASH_PATTERN.fullmatch(value["batch_hash"]) is None
            or not isinstance(value.get("items"), list)):
        raise WooBatchAuthorizationError()

    approvals = []
    seen = set()
    for entry in value["items"]:
        if not isinstance(entry, Mapping) or set(entry) != _ITEM_FIELDS:
            raise WooBatchAuthorizationError()
        sequence = entry["sequence"]
        operations = entry["allowed_operations"]
        if (type(sequence) is not int or sequence <= 0 or sequence in seen
                or not _safe_sku(entry["sku"])
                or type(entry["confirmed_plan_hash"]) is not str
                or workspace._HASH_PATTERN.fullmatch(entry["confirmed_plan_hash"]) is None
                or not isinstance(operations, list) or not operations
                or any(type(operation) is not str or operation not in NORMAL_OPERATIONS
                       for operation in operations)
                or len(operations) != len(set(operations))):
            raise WooBatchAuthorizationError()
        seen.add(sequence)
        approvals.append(ManualAuthorizationContext(
            batch_hash=value["batch_hash"], sequence=sequence, sku=entry["sku"],
            confirmed_plan_hash=entry["confirmed_plan_hash"],
            allowed_operations=frozenset(operations),
        ))

    expected = {item.sequence: item for item in runtime.items}
    if value["batch_hash"] != runtime.batch_hash or seen != set(expected):
        raise WooBatchAuthorizationError(blocked=True)
    for approval in approvals:
        item = expected[approval.sequence]
        if approval.sku != item.sku or approval.confirmed_plan_hash != item.plan_hash:
            raise WooBatchAuthorizationError(blocked=True)
    return BatchManualAuthorization(value["batch_hash"], tuple(approvals))


def _reject_nonfinite(value: str) -> None:
    raise WooBatchAuthorizationError()


def load_woo_batch_authorization(path: Path, runtime: BatchRuntime) -> BatchManualAuthorization:
    """Read and decode one safe local approvals file, exactly once."""
    try:
        local = package_io._local_path(path, require_file=True)
        if (local.is_relative_to(runtime.workspace_root)
                or not workspace._regular_unlinked_file(local)):
            raise WooBatchAuthorizationError()
        size = local.stat().st_size
        if not 0 < size <= MAX_AUTHORIZATION_BYTES:
            raise WooBatchAuthorizationError()
        raw = local.read_bytes()
        if not 0 < len(raw) <= MAX_AUTHORIZATION_BYTES:
            raise WooBatchAuthorizationError()
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=package_io._json_object_no_duplicates,
            parse_constant=_reject_nonfinite,
        )
        return parse_woo_batch_authorization(value, runtime)
    except WooBatchAuthorizationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, RecursionError):
        raise WooBatchAuthorizationError() from None
