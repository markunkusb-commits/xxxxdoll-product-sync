"""Pure-local Batch Frozen Plan V1 construction."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_apply_plan as apply_plan
from . import woocommerce_batch_workspace as batch_workspace
from . import woocommerce_product_apply as apply_core
from . import woocommerce_target_snapshot as target_snapshot
from .report import sanitize_report_data
from .sanitization import Redactor


INPUT_POLICY_VERSION = "xxxxdoll-woo-batch-plan-input-v1"
POLICY_VERSION = "xxxxdoll-woo-batch-frozen-plan-v1"
REPORT_FILENAME = batch_workspace.BATCH_REPORT_FILENAME
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_BATCH_ITEMS = 999_999

EXECUTION_POLICY = {
    "mode": "sequential",
    "concurrency": 1,
    "failure_policy": "stop_on_non_success",
}
_ZERO_COUNTERS = apply_plan._ZERO_COUNTERS
_SEMANTIC_FIELDS = (
    "policy_version",
    "target",
    "execution_policy",
    "items",
)


class WooBatchPlanError(ValueError):
    """Base class for fixed-code Batch Freeze failures."""


class WooBatchPlanInputError(WooBatchPlanError):
    """Unsafe manifest, path, JSON, or input contract."""


class WooBatchItemPlanError(WooBatchPlanError):
    """One Single Product Frozen Plan is not eligible for a batch."""


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    sequence: int
    sku: str
    plan_hash: str
    raw_bytes: bytes
    raw_sha256: str

    def report_item(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "sku": self.sku,
            "plan_hash": self.plan_hash,
            "source_plan": {
                "basename": batch_workspace.PLAN_FILENAME,
                "sha256": self.raw_sha256,
            },
        }

    def plan_copy(self) -> batch_workspace.BatchPlanCopy:
        return batch_workspace.BatchPlanCopy(
            self.sequence,
            self.raw_bytes,
            self.raw_sha256,
        )


def _read_manifest(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    try:
        local = package_io._local_path(path, require_file=True)
        size = local.stat().st_size
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise WooBatchPlanInputError("woo_batch_manifest_size_invalid")
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooBatchPlanInputError:
        raise
    except target_snapshot.WooTargetSnapshotInputError:
        raise WooBatchPlanInputError(
            "woo_batch_manifest_duplicate_json_key"
        ) from None
    except package_io.SingleProductStagingPackageInputError:
        raise WooBatchPlanInputError("woo_batch_local_manifest_required") from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooBatchPlanInputError("woo_batch_manifest_json_invalid") from None
    if not isinstance(value, Mapping):
        raise WooBatchPlanInputError("woo_batch_manifest_root_invalid")
    return (
        value,
        {"basename": local.name, "sha256": hashlib.sha256(raw).hexdigest()},
        local,
    )


def _manifest_entries(
    manifest: Mapping[str, object],
) -> list[tuple[int, str]]:
    if (
        set(manifest) != {"policy_version", "items"}
        or manifest.get("policy_version") != INPUT_POLICY_VERSION
    ):
        raise WooBatchPlanInputError("woo_batch_manifest_contract_invalid")
    raw_items = manifest.get("items")
    if (
        not isinstance(raw_items, list)
        or not raw_items
        or len(raw_items) > MAX_BATCH_ITEMS
    ):
        raise WooBatchPlanInputError("woo_batch_manifest_items_invalid")
    entries: list[tuple[int, str]] = []
    for item in raw_items:
        if not isinstance(item, Mapping) or set(item) != {"sequence", "plan_path"}:
            raise WooBatchPlanInputError("woo_batch_manifest_item_invalid")
        sequence = item.get("sequence")
        plan_path = item.get("plan_path")
        if (
            type(sequence) is not int
            or sequence <= 0
            or sequence > MAX_BATCH_ITEMS
            or type(plan_path) is not str
            or not plan_path.strip()
            or "\x00" in plan_path
        ):
            raise WooBatchPlanInputError("woo_batch_manifest_item_invalid")
        entries.append((sequence, plan_path))
    return entries


def _resolve_plan_path(manifest_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    try:
        return package_io._local_path(candidate, require_file=True)
    except package_io.SingleProductStagingPackageInputError:
        raise WooBatchPlanInputError("woo_batch_local_plan_required") from None


def _read_item_plan(path: Path, sequence: int) -> _PreparedItem:
    try:
        value, first_source, local = apply_core._read_plan(path)
        raw = local.read_bytes()
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if raw_sha256 != first_source.get("sha256"):
            raise WooBatchItemPlanError("woo_batch_item_plan_changed")
        current = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
        if not isinstance(current, Mapping) or dict(current) != dict(value):
            raise WooBatchItemPlanError("woo_batch_item_plan_changed")
        sku, _, plan_hash = apply_core.validate_frozen_plan_integrity(current)
    except WooBatchItemPlanError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        apply_core.WooProductApplyError,
        target_snapshot.WooTargetSnapshotInputError,
    ):
        raise WooBatchItemPlanError("woo_batch_item_plan_invalid") from None
    return _PreparedItem(sequence, sku, plan_hash, raw, raw_sha256)


def semantic_batch_body(report: Mapping[str, object]) -> dict[str, object]:
    if any(field not in report for field in _SEMANTIC_FIELDS):
        raise WooBatchPlanInputError("woo_batch_semantic_body_invalid")
    return {field: copy.deepcopy(report[field]) for field in _SEMANTIC_FIELDS}


def canonical_batch_bytes(semantic_body: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            semantic_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise WooBatchPlanInputError("woo_batch_canonicalization_failed") from None


def compute_batch_hash(semantic_body: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_batch_bytes(semantic_body)).hexdigest()


def _base_report(
    source_manifest: Mapping[str, str],
) -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": POLICY_VERSION,
        "batch_hash": None,
        "target": dict(apply_core._TARGET),
        "execution_policy": dict(EXECUTION_POLICY),
        "source_manifest": dict(source_manifest),
        "items": [],
        "blocking_issues": [],
        "write_authorized": False,
        **dict.fromkeys(_ZERO_COUNTERS, 0),
    }


def _blocked_report(
    source_manifest: Mapping[str, str],
    issues: Sequence[str],
) -> dict[str, object]:
    report = _base_report(source_manifest)
    report["status"] = "blocked"
    report["blocking_issues"] = list(dict.fromkeys(issues))
    return report


def _duplicate_issues(items: Sequence[_PreparedItem]) -> list[str]:
    issues: list[str] = []
    projections = (
        ("woo_batch_duplicate_sequence", [item.sequence for item in items]),
        ("woo_batch_duplicate_sku", [item.sku for item in items]),
        ("woo_batch_duplicate_plan_hash", [item.plan_hash for item in items]),
        ("woo_batch_duplicate_raw_plan_sha", [item.raw_sha256 for item in items]),
    )
    for code, values in projections:
        if len(values) != len(set(values)):
            issues.append(code)
    return issues


def build_frozen_batch_plan(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path,
    source_manifest: Mapping[str, str],
) -> tuple[dict[str, object], tuple[batch_workspace.BatchPlanCopy, ...]]:
    """Build a non-authorizing Batch Plan and exact Plan-copy inputs."""

    entries = _manifest_entries(manifest)
    sequence_values = [sequence for sequence, _ in entries]
    sequence_issues: list[str] = []
    if len(sequence_values) != len(set(sequence_values)):
        sequence_issues.append("woo_batch_duplicate_sequence")
    if sequence_values != list(range(1, len(entries) + 1)):
        sequence_issues.append("woo_batch_sequence_invalid")
    if sequence_issues:
        return _blocked_report(source_manifest, sequence_issues), ()

    prepared: list[_PreparedItem] = []
    for sequence, raw_path in entries:
        plan_path = _resolve_plan_path(manifest_path, raw_path)
        try:
            prepared.append(_read_item_plan(plan_path, sequence))
        except WooBatchItemPlanError:
            return _blocked_report(
                source_manifest,
                ["woo_batch_item_plan_invalid"],
            ), ()

    duplicate_issues = _duplicate_issues(prepared)
    if duplicate_issues:
        return _blocked_report(source_manifest, duplicate_issues), ()

    report = _base_report(source_manifest)
    report["items"] = [item.report_item() for item in prepared]
    report["batch_hash"] = compute_batch_hash(semantic_batch_body(report))
    safe = sanitize_report_data(report, Redactor())
    if not isinstance(safe, dict) or safe != report:
        raise WooBatchPlanInputError("woo_batch_report_sanitization_changed")
    return (
        json.loads(json.dumps(safe, ensure_ascii=False, sort_keys=True)),
        tuple(item.plan_copy() for item in prepared),
    )


def freeze_woo_batch_plan(
    manifest_path: Path,
    *,
    output_root: Path,
) -> tuple[dict[str, object], Path | None, bool]:
    """Freeze and publish one local-only Batch Plan workspace."""

    manifest, source_manifest, local_manifest = _read_manifest(manifest_path)
    report, copies = build_frozen_batch_plan(
        manifest,
        manifest_path=local_manifest,
        source_manifest=source_manifest,
    )
    if report.get("status") != "ok":
        return report, None, False
    batch_hash = report.get("batch_hash")
    if not isinstance(batch_hash, str):  # pragma: no cover - structural guard
        raise AssertionError("Actionable Batch Plan must have BATCH_HASH")
    try:
        published = batch_workspace.publish_batch_workspace(
            output_root,
            batch_hash,
            report,
            copies,
        )
    except batch_workspace.WooBatchWorkspaceError as error:
        raise WooBatchPlanInputError(str(error)) from None
    return published.persisted_report, published.path, published.reused
