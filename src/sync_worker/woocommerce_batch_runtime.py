"""Read-only validation and local observation of a frozen batch workspace.

Observations are snapshots, never execution authorization or remote proof.
Invalid workspaces raise a BLOCKED error without returning partial results.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_batch_plan as batch_plan
from . import woocommerce_batch_workspace as workspace
from . import woocommerce_product_apply as apply_core
from .report import sanitize_report_data
from .sanitization import Redactor


ObservedItemState = Literal[
    "NOT_STARTED", "ALREADY_APPLIED_OBSERVED", "RECOVERY_REQUIRED"
]
_BATCH_FIELDS = frozenset(
    {
        "status", "policy_version", "batch_hash", "target", "execution_policy",
        "source_manifest", "items", "blocking_issues", "write_authorized",
        *batch_plan._ZERO_COUNTERS,
    }
)
_RUNTIME_FILENAMES = frozenset(
    {apply_core.PENDING_FILENAME, apply_core.RECEIPT_FILENAME, apply_core.LOCK_FILENAME}
)


class WooBatchRuntimeError(ValueError):
    """A fixed safe error code; no partial runtime is eligible for use."""

    state: Literal["BLOCKED"] = "BLOCKED"


@dataclass(frozen=True, slots=True)
class BatchItemRuntime:
    """One bound Plan and presence-only observation of its runtime files."""

    sequence: int
    sku: str
    plan_hash: str
    plan_path: Path
    state: ObservedItemState
    runtime_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchRuntime:
    """Immutable local observations in frozen sequence order."""

    batch_hash: str
    workspace_root: Path
    items: tuple[BatchItemRuntime, ...]


def _directory_entries(path: Path) -> frozenset[str]:
    if package_io._has_link_or_reparse(path) or not path.is_dir():
        raise WooBatchRuntimeError("woo_batch_runtime_directory_invalid")
    return frozenset(entry.name for entry in path.iterdir())


def _require_directory(path: Path, expected: frozenset[str]) -> None:
    if _directory_entries(path) != expected:
        raise WooBatchRuntimeError("woo_batch_runtime_structure_invalid")


def _require_file(path: Path) -> Path:
    local = package_io._local_path(path, require_file=True)
    if not workspace._regular_unlinked_file(local):
        raise WooBatchRuntimeError("woo_batch_runtime_file_invalid")
    return local


def _validate_batch_report(
    report: Mapping[str, object],
    root: Path,
) -> list[Mapping[str, object]]:
    if (
        set(report) != _BATCH_FIELDS
        or report.get("status") != "ok"
        or report.get("policy_version") != batch_plan.POLICY_VERSION
        or report.get("blocking_issues") != []
        or report.get("write_authorized") is not False
        or report.get("target") != apply_core._TARGET
        or any(
            type(report.get(field)) is not int or report[field] != 0
            for field in batch_plan._ZERO_COUNTERS
        )
        or not workspace._safe_fingerprint(report.get("source_manifest"))
    ):
        raise WooBatchRuntimeError("woo_batch_runtime_batch_contract_invalid")
    policy = report.get("execution_policy")
    if (
        not isinstance(policy, Mapping)
        or dict(policy) != batch_plan.EXECUTION_POLICY
        or type(policy.get("concurrency")) is not int
    ):
        raise WooBatchRuntimeError("woo_batch_runtime_execution_policy_invalid")
    batch_hash = report.get("batch_hash")
    if (
        type(batch_hash) is not str
        or workspace._HASH_PATTERN.fullmatch(batch_hash) is None
        or root.name != batch_hash
    ):
        raise WooBatchRuntimeError("woo_batch_runtime_batch_hash_invalid")

    items = report.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= batch_plan.MAX_BATCH_ITEMS:
        raise WooBatchRuntimeError("woo_batch_runtime_items_invalid")
    seen_skus: set[str] = set()
    seen_plan_hashes: set[str] = set()
    seen_source_hashes: set[str] = set()
    for sequence, item in enumerate(items, start=1):
        if not isinstance(item, Mapping) or set(item) != {
            "sequence", "sku", "plan_hash", "source_plan"
        }:
            raise WooBatchRuntimeError("woo_batch_runtime_item_contract_invalid")
        source = item.get("source_plan")
        sku = item.get("sku")
        plan_hash = item.get("plan_hash")
        if (
            type(item.get("sequence")) is not int
            or item["sequence"] != sequence
            or type(sku) is not str
            or not sku.strip()
            or type(plan_hash) is not str
            or workspace._HASH_PATTERN.fullmatch(plan_hash) is None
            or not workspace._safe_fingerprint(source)
            or source["basename"] != workspace.PLAN_FILENAME
        ):
            raise WooBatchRuntimeError("woo_batch_runtime_item_contract_invalid")
        if (
            sku in seen_skus
            or plan_hash in seen_plan_hashes
            or source["sha256"] in seen_source_hashes
        ):
            raise WooBatchRuntimeError("woo_batch_runtime_duplicate_item")
        seen_skus.add(sku)
        seen_plan_hashes.add(plan_hash)
        seen_source_hashes.add(source["sha256"])

    recomputed = batch_plan.compute_batch_hash(batch_plan.semantic_batch_body(report))
    if recomputed != batch_hash:
        raise WooBatchRuntimeError("woo_batch_runtime_batch_hash_mismatch")
    if sanitize_report_data(report, Redactor()) != report:
        raise WooBatchRuntimeError("woo_batch_runtime_batch_contract_invalid")
    return items


def _observe_runtime(reports_root: Path) -> tuple[ObservedItemState, tuple[str, ...]]:
    names = _directory_entries(reports_root)
    if not names <= _RUNTIME_FILENAMES:
        raise WooBatchRuntimeError("woo_batch_runtime_unknown_file")
    for name in sorted(names):
        _require_file(reports_root / name)
    # Only presence is observed: do not open or interpret journal contents.
    if apply_core.PENDING_FILENAME in names or apply_core.LOCK_FILENAME in names:
        state: ObservedItemState = "RECOVERY_REQUIRED"
    elif apply_core.RECEIPT_FILENAME in names:
        state = "ALREADY_APPLIED_OBSERVED"
    else:
        state = "NOT_STARTED"
    return state, tuple(sorted(names))


def load_woo_batch_runtime(workspace_root: Path) -> BatchRuntime:
    """Validate existing authorities and observe files without changing them.

    The existing hardened JSON path helper also validates every root ancestor.
    The Batch Plan and copied Plans must remain bound by both raw and semantic
    hashes. Runtime file contents are deliberately outside this loader's scope.
    """

    try:
        report_path = package_io._local_path(
            Path(workspace_root) / workspace.BATCH_REPORT_FILENAME,
            require_file=True,
        )
        root = report_path.parent
        _require_directory(
            root, frozenset({workspace.BATCH_REPORT_FILENAME, workspace.ITEMS_DIRECTORY})
        )
        _require_file(report_path)
        report, _ = workspace._read_report(report_path)
        items = _validate_batch_report(report, root)
        items_root = root / workspace.ITEMS_DIRECTORY
        _require_directory(
            items_root,
            frozenset(workspace.item_directory_name(item["sequence"]) for item in items),
        )

        observed: list[BatchItemRuntime] = []
        for item in items:
            sequence = item["sequence"]
            item_root = items_root / workspace.item_directory_name(sequence)
            _require_directory(
                item_root,
                frozenset({workspace.AUTHORITIES_DIRECTORY, workspace.REPORTS_DIRECTORY}),
            )
            authorities = item_root / workspace.AUTHORITIES_DIRECTORY
            _require_directory(authorities, frozenset({workspace.PLAN_FILENAME}))
            plan_path = _require_file(authorities / workspace.PLAN_FILENAME)
            # Reuses validate_frozen_plan_integrity through the existing reader;
            # that reader binds its parsed value to the exact raw bytes as well.
            prepared = batch_plan._read_item_plan(plan_path, sequence)
            if prepared.raw_sha256 != item["source_plan"]["sha256"]:
                raise WooBatchRuntimeError("woo_batch_runtime_plan_sha_mismatch")
            if prepared.sku != item["sku"] or prepared.plan_hash != item["plan_hash"]:
                raise WooBatchRuntimeError("woo_batch_runtime_plan_binding_mismatch")
            state, runtime_files = _observe_runtime(item_root / workspace.REPORTS_DIRECTORY)
            observed.append(
                BatchItemRuntime(
                    sequence, prepared.sku, prepared.plan_hash, plan_path,
                    state, runtime_files,
                )
            )
        return BatchRuntime(report["batch_hash"], root, tuple(observed))
    except WooBatchRuntimeError:
        raise
    except batch_plan.WooBatchItemPlanError:
        raise WooBatchRuntimeError("woo_batch_runtime_plan_invalid") from None
    except (
        OSError, RuntimeError, TypeError, ValueError,
        package_io.SingleProductStagingPackageInputError,
        workspace.WooBatchWorkspaceError,
        batch_plan.WooBatchPlanError,
    ):
        raise WooBatchRuntimeError("woo_batch_runtime_input_invalid") from None
