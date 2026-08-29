"""Offline adapter from Image Quality Dry Run to Image Selection Policy.

The adapter validates one sanitized quality report, restores the formal
quality and selection domains, delegates every selection decision to the
existing Selection Core, verifies the returned invariants, and writes one
fixed local JSON audit report.  It has no provider, media, configuration or
network authority.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import (
    folder_role_policy,
    image_asset_type_policy,
    image_quality_policy,
    image_selection_policy,
    unified_image_eligibility_policy,
)
from .image_asset_type_dry_run import (
    ImageAssetTypeDryRunInputError,
    _assert_safe_metadata,
    _local_path,
)
from .image_mapping import ProductSourceRange
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor


REPORT_FILENAME = "image-selection-dry-run.json"
REQUEST_COUNTERS = (
    "network_requests_performed",
    "download_requests_performed",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "write_requests_performed",
)

_REPORT_FIELDS = frozenset({
    "status", "policy_version", "source_unified_policy_version",
    "source_asset_policy_version", "summary", "results", *REQUEST_COUNTERS,
})
_RESULT_FIELDS = frozenset({
    "sku", "product_source", "source_manifest_kind", "depth",
    "safe_folder_name", "parent_safe_folder_name", "safe_name", "folder_role",
    "unified_image_eligible", "requires_deeper_inventory", "image_width",
    "image_height", "short_edge", "long_edge", "pixel_count", "megapixels",
    "size_bytes", "orientation", "quality_eligible", "quality_reason",
    "min_short_edge_px", "min_megapixels", "quality_policy_version", "warnings",
    "blocking_issues", "join_status",
})
_SUMMARY_FIELDS = frozenset({
    "total_unified_assets", "upstream_eligible_candidates",
    "skipped_upstream_ineligible", "quality_metadata_joined",
    "quality_metadata_missing", "quality_metadata_ambiguous", "quality_evaluated",
    "quality_pass", "quality_fail", "fail_short_edge", "fail_megapixels",
    "fail_metadata_missing", "fail_metadata_invalid", "fail_upstream",
    "fail_invalid_policy_input", "portrait", "landscape", "square",
    "storefront_quality_pass", "factory_quality_pass",
    "requires_deeper_inventory_quality_pass", "assets_with_warnings",
    "blocking_assets", *REQUEST_COUNTERS,
})
_OUTPUT_ITEM_FIELDS = (
    "image_width", "image_height", "short_edge", "long_edge", "pixel_count",
    "megapixels", "size_bytes", "orientation", "quality_reason",
    "min_short_edge_px", "min_megapixels",
)
_DEPTHS = {"root": 0, "nested": 1, "depth2": 2}
_ISSUE = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_FORBIDDEN_INPUT_NAMES = frozenset({
    "unified-image-eligibility-dry-run.json",
    "image-asset-type-dry-run.json",
    "folder-role-dry-run.json",
    "webp-output-policy-dry-run.json",
    "google-drive-folder-manifest-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
    REPORT_FILENAME,
})


class ImageSelectionDryRunInputError(ValueError):
    """Fixed safe error codes only; never paths or untrusted report values."""


@dataclass(frozen=True, slots=True)
class _RestoredQualityItem:
    candidate: image_selection_policy.ImageSelectionCandidate
    quality_audit: dict[str, object]


def _schema(value: object, fields: frozenset[str], code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value) or set(value) != fields:
        raise ImageSelectionDryRunInputError(code)
    return value


def _safe_metadata(value: object) -> None:
    try:
        _assert_safe_metadata(value)
    except (ImageAssetTypeDryRunInputError, RecursionError):
        raise ImageSelectionDryRunInputError("unsafe_quality_report") from None


def _counter(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise ImageSelectionDryRunInputError(code)
    return value


def _issues(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not _ISSUE.fullmatch(item) for item in value):
        raise ImageSelectionDryRunInputError("invalid_quality_issues")
    return tuple(value)


def _text(value: object, code: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 500:
        raise ImageSelectionDryRunInputError(code)
    if any(unicodedata.category(char) == "Cc" and not char.isspace() for char in value):
        raise ImageSelectionDryRunInputError(code)
    _safe_metadata(value)
    return value


def _optional_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ImageSelectionDryRunInputError(code)
    return value


def _optional_float(value: object, code: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ImageSelectionDryRunInputError(code)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ImageSelectionDryRunInputError(code) from None
    if not math.isfinite(number):
        raise ImageSelectionDryRunInputError(code)
    return number


def _rows(value: object, *, optional: bool) -> ProductSourceRange | None:
    if optional and value is None:
        return None
    source = _schema(value, frozenset({"start_row", "end_row"}), "invalid_product_source")
    start = _counter(source["start_row"], "invalid_product_source")
    end = _counter(source["end_row"], "invalid_product_source")
    if start == 0 or end < start:
        raise ImageSelectionDryRunInputError("invalid_product_source")
    return ProductSourceRange(start, end)


def _restore_quality_result(item: Mapping[str, object]) -> image_quality_policy.ImageQualityPolicyResult:
    if item["quality_policy_version"] != image_quality_policy.POLICY_VERSION:
        raise ImageSelectionDryRunInputError("quality_policy_version_mismatch")
    if item["min_short_edge_px"] != image_quality_policy.MIN_SHORT_EDGE_PX:
        raise ImageSelectionDryRunInputError("quality_threshold_mismatch")
    if item["min_megapixels"] != image_quality_policy.MIN_MEGAPIXELS:
        raise ImageSelectionDryRunInputError("quality_threshold_mismatch")
    try:
        reason = image_quality_policy.ImageQualityReason(item["quality_reason"])
        orientation = (
            None if item["orientation"] is None
            else image_quality_policy.ImageOrientation(item["orientation"])
        )
    except (TypeError, ValueError):
        raise ImageSelectionDryRunInputError("invalid_quality_result") from None
    if type(item["quality_eligible"]) is not bool:
        raise ImageSelectionDryRunInputError("invalid_quality_result")
    result = image_quality_policy.ImageQualityPolicyResult(
        quality_eligible=item["quality_eligible"],
        quality_reason=reason,
        image_width=_optional_int(item["image_width"], "invalid_quality_metrics"),
        image_height=_optional_int(item["image_height"], "invalid_quality_metrics"),
        short_edge=_optional_int(item["short_edge"], "invalid_quality_metrics"),
        long_edge=_optional_int(item["long_edge"], "invalid_quality_metrics"),
        pixel_count=_optional_int(item["pixel_count"], "invalid_quality_metrics"),
        megapixels=_optional_float(item["megapixels"], "invalid_quality_metrics"),
        size_bytes=_optional_int(item["size_bytes"], "invalid_quality_metrics"),
        orientation=orientation,
        warnings=_issues(item["warnings"]),
        blocking_issues=_issues(item["blocking_issues"]),
    )
    if result.quality_eligible != (result.quality_reason is image_quality_policy.ImageQualityReason.QUALITY_PASS):
        raise ImageSelectionDryRunInputError("invalid_quality_result")
    if result.blocking_issues:
        raise ImageSelectionDryRunInputError("quality_report_contains_blockers")
    metrics = (
        result.image_width, result.image_height, result.short_edge, result.long_edge,
        result.pixel_count, result.megapixels, result.size_bytes, result.orientation,
    )
    if any(value is None for value in metrics):
        raise ImageSelectionDryRunInputError("invalid_quality_metrics")
    assert result.image_width is not None and result.image_height is not None
    expected_short, expected_long = sorted((result.image_width, result.image_height))
    expected_pixels = result.image_width * result.image_height
    expected_orientation = (
        image_quality_policy.ImageOrientation.SQUARE
        if result.image_width == result.image_height else
        image_quality_policy.ImageOrientation.PORTRAIT
        if result.image_width < result.image_height else
        image_quality_policy.ImageOrientation.LANDSCAPE
    )
    if (
        result.image_width <= 0 or result.image_height <= 0
        or result.size_bytes is None or result.size_bytes <= 0
        or result.image_width > image_quality_policy.MAX_SAFE_DIMENSION_PX
        or result.image_height > image_quality_policy.MAX_SAFE_DIMENSION_PX
        or result.size_bytes > image_quality_policy.MAX_SAFE_SIZE_BYTES
        or result.short_edge != expected_short or result.long_edge != expected_long
        or result.pixel_count != expected_pixels
        or result.megapixels != expected_pixels / 1_000_000
        or result.orientation is not expected_orientation
    ):
        raise ImageSelectionDryRunInputError("invalid_quality_metrics")
    if result.quality_reason is image_quality_policy.ImageQualityReason.QUALITY_PASS and (
        result.short_edge < image_quality_policy.MIN_SHORT_EDGE_PX
        or result.megapixels < image_quality_policy.MIN_MEGAPIXELS
    ):
        raise ImageSelectionDryRunInputError("invalid_quality_result")
    if result.quality_reason is image_quality_policy.ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM and (
        result.short_edge >= image_quality_policy.MIN_SHORT_EDGE_PX
    ):
        raise ImageSelectionDryRunInputError("invalid_quality_result")
    if result.quality_reason is image_quality_policy.ImageQualityReason.MEGAPIXELS_BELOW_MINIMUM and (
        result.short_edge < image_quality_policy.MIN_SHORT_EDGE_PX
        or result.megapixels >= image_quality_policy.MIN_MEGAPIXELS
    ):
        raise ImageSelectionDryRunInputError("invalid_quality_result")
    if result.quality_reason not in {
        image_quality_policy.ImageQualityReason.QUALITY_PASS,
        image_quality_policy.ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM,
        image_quality_policy.ImageQualityReason.MEGAPIXELS_BELOW_MINIMUM,
    }:
        raise ImageSelectionDryRunInputError("invalid_quality_result")
    return result


def _restore_item(value: object) -> _RestoredQualityItem:
    item = _schema(value, _RESULT_FIELDS, "invalid_quality_item")
    if item["join_status"] != "joined" or item["unified_image_eligible"] is not True:
        raise ImageSelectionDryRunInputError("invalid_quality_item_contract")
    kind, depth = item["source_manifest_kind"], item["depth"]
    if type(kind) is not str or kind not in _DEPTHS or type(depth) is not int or depth != _DEPTHS[kind]:
        raise ImageSelectionDryRunInputError("invalid_source_hierarchy")
    sku = _text(item["sku"], "invalid_sku")
    safe_name = _text(item["safe_name"], "invalid_safe_name")
    folder = _text(item["safe_folder_name"], "invalid_safe_folder_name", optional=kind == "root")
    parent = _text(item["parent_safe_folder_name"], "invalid_parent_safe_folder_name", optional=kind != "depth2")
    if (
        (kind == "root" and (folder is not None or parent is not None))
        or (kind == "nested" and (folder is None or parent is not None))
        or (kind == "depth2" and (folder is None or parent is None))
    ):
        raise ImageSelectionDryRunInputError("invalid_source_hierarchy")
    source = _rows(item["product_source"], optional=kind == "root")
    if kind != "root" and source is None:
        raise ImageSelectionDryRunInputError("invalid_product_source")
    try:
        role = folder_role_policy.FolderRole(item["folder_role"])
    except (TypeError, ValueError):
        raise ImageSelectionDryRunInputError("invalid_folder_role") from None
    if role not in {
        folder_role_policy.FolderRole.STOREFRONT_PHOTOS,
        folder_role_policy.FolderRole.FACTORY_PHOTOS,
    }:
        raise ImageSelectionDryRunInputError("invalid_folder_role")
    if type(item["requires_deeper_inventory"]) is not bool:
        raise ImageSelectionDryRunInputError("invalid_deeper_inventory_flag")
    quality = _restore_quality_result(item)
    candidate = image_selection_policy.ImageSelectionCandidate(
        sku=sku, folder_role=role, safe_name=safe_name,
        source_manifest_kind=kind, depth=depth,
        safe_folder_name=folder, parent_safe_folder_name=parent,
        quality_result=quality, product_source=source,
        requires_deeper_inventory=item["requires_deeper_inventory"],
    )
    try:
        image_selection_policy._validate_candidate(candidate, 0)
    except image_selection_policy.ImageSelectionPolicyError:
        raise ImageSelectionDryRunInputError("invalid_selection_candidate") from None
    quality_data = quality.to_dict()
    audit = {key: quality_data[key] for key in _OUTPUT_ITEM_FIELDS}
    audit["quality_policy_version"] = quality_data["policy_version"]
    return _RestoredQualityItem(candidate, audit)


def _restore_report(value: object) -> tuple[_RestoredQualityItem, ...]:
    report = _schema(value, _REPORT_FIELDS, "invalid_quality_report")
    _safe_metadata(report)
    if report["status"] != "ok":
        raise ImageSelectionDryRunInputError("quality_report_status_not_ok")
    if report["policy_version"] != image_quality_policy.POLICY_VERSION:
        raise ImageSelectionDryRunInputError("quality_policy_version_mismatch")
    if report["source_unified_policy_version"] != unified_image_eligibility_policy.POLICY_VERSION:
        raise ImageSelectionDryRunInputError("source_unified_policy_version_mismatch")
    if report["source_asset_policy_version"] != image_asset_type_policy.POLICY_VERSION:
        raise ImageSelectionDryRunInputError("source_asset_policy_version_mismatch")
    summary = _schema(report["summary"], _SUMMARY_FIELDS, "invalid_quality_summary")
    for value in summary.values():
        _counter(value, "invalid_quality_summary")
    for counter in REQUEST_COUNTERS:
        if report[counter] != 0 or summary[counter] != 0:
            raise ImageSelectionDryRunInputError("quality_report_not_offline")
    if summary["blocking_assets"] != 0:
        raise ImageSelectionDryRunInputError("quality_report_contains_blockers")
    if not isinstance(report["results"], list):
        raise ImageSelectionDryRunInputError("invalid_quality_results")
    restored = tuple(_restore_item(item) for item in report["results"])
    quality_pass = sum(item.candidate.quality_result.quality_eligible for item in restored)
    expected_summary = {
        "upstream_eligible_candidates": len(restored),
        "quality_evaluated": len(restored),
        "quality_metadata_joined": len(restored),
        "quality_metadata_missing": 0, "quality_metadata_ambiguous": 0,
        "quality_pass": quality_pass, "quality_fail": len(restored) - quality_pass,
        "fail_short_edge": sum(item.candidate.quality_result.quality_reason is image_quality_policy.ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM for item in restored),
        "fail_megapixels": sum(item.candidate.quality_result.quality_reason is image_quality_policy.ImageQualityReason.MEGAPIXELS_BELOW_MINIMUM for item in restored),
        "fail_metadata_missing": 0, "fail_metadata_invalid": 0,
        "fail_upstream": 0, "fail_invalid_policy_input": 0,
        "portrait": sum(item.candidate.quality_result.orientation is image_quality_policy.ImageOrientation.PORTRAIT for item in restored),
        "landscape": sum(item.candidate.quality_result.orientation is image_quality_policy.ImageOrientation.LANDSCAPE for item in restored),
        "square": sum(item.candidate.quality_result.orientation is image_quality_policy.ImageOrientation.SQUARE for item in restored),
        "storefront_quality_pass": sum(item.candidate.quality_result.quality_eligible and item.candidate.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS for item in restored),
        "factory_quality_pass": sum(item.candidate.quality_result.quality_eligible and item.candidate.folder_role is folder_role_policy.FolderRole.FACTORY_PHOTOS for item in restored),
        "requires_deeper_inventory_quality_pass": sum(item.candidate.quality_result.quality_eligible and item.candidate.requires_deeper_inventory for item in restored),
        "assets_with_warnings": sum(bool(item.candidate.quality_result.warnings) for item in restored),
        "blocking_assets": 0,
    }
    if (
        summary["total_unified_assets"] < len(restored)
        or summary["skipped_upstream_ineligible"] != summary["total_unified_assets"] - len(restored)
        or any(summary[key] != expected for key, expected in expected_summary.items())
    ):
        raise ImageSelectionDryRunInputError("quality_summary_mismatch")
    return restored


def _item_key_from_candidate(candidate: image_selection_policy.ImageSelectionCandidate) -> tuple[object, ...]:
    source = candidate.product_source
    return (
        candidate.sku, candidate.folder_role, candidate.safe_name,
        candidate.source_manifest_kind, candidate.depth, candidate.safe_folder_name,
        candidate.parent_safe_folder_name,
        None if source is None else source.start_row,
        None if source is None else source.end_row,
        candidate.requires_deeper_inventory,
        candidate.quality_result.quality_eligible,
    )


def _item_key_from_result(item: image_selection_policy.ImageSelectionItem) -> tuple[object, ...]:
    source = item.product_source
    quality_pass = item.selection_reason is not image_selection_policy.ImageSelectionReason.NOT_SELECTED_QUALITY_INELIGIBLE
    return (
        item.sku, item.folder_role, item.safe_name, item.source_manifest_kind,
        item.depth, item.safe_folder_name, item.parent_safe_folder_name,
        None if source is None else source.start_row,
        None if source is None else source.end_row,
        item.requires_deeper_inventory, quality_pass,
    )


def _take_audit(
    item: image_selection_policy.ImageSelectionItem,
    audits: dict[tuple[object, ...], deque[dict[str, object]]],
) -> dict[str, object]:
    key = _item_key_from_result(item)
    queue = audits.get(key)
    if queue:
        return queue.popleft()
    # A malformed mocked Core role must still reach the explicit role contract
    # instead of failing during audit projection.  The fallback is allowed only
    # when all non-role provenance fields identify exactly one remaining queue.
    roleless = key[:1] + key[2:]
    matches = [
        values for candidate_key, values in audits.items()
        if values and candidate_key[:1] + candidate_key[2:] == roleless
    ]
    if len(matches) != 1:
        raise ImageSelectionDryRunInputError("selection_item_audit_mismatch")
    return matches[0].popleft()


def _merge_codes(*groups: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(code for group in groups for code in group))


def _serialize_batch(
    batch: image_selection_policy.ImageSelectionBatchResult,
    audits: dict[tuple[object, ...], deque[dict[str, object]]],
) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for result_item in batch.items:
        audit = _take_audit(result_item, audits)
        core_data = result_item.to_dict()
        items.append({
            "sku": core_data["sku"], "product_source": core_data["product_source"],
            "source_manifest_kind": core_data["source_manifest_kind"],
            "depth": core_data["depth"], "safe_folder_name": core_data["safe_folder_name"],
            "parent_safe_folder_name": core_data["parent_safe_folder_name"],
            "safe_name": core_data["safe_name"], "folder_role": core_data["folder_role"],
            **audit, "quality_eligible": core_data["quality_eligible"],
            "selected": core_data["selected"],
            "selection_position": core_data["selection_position"],
            "image_role": core_data["image_role"],
            "selection_reason": core_data["selection_reason"],
            "selection_policy_version": core_data["policy_version"],
            "requires_deeper_inventory": core_data["requires_deeper_inventory"],
            "warnings": core_data["warnings"],
            "blocking_issues": core_data["blocking_issues"],
        })

    batch_codes: list[str] = list(batch.blocking_issues)
    selected = [item for item in items if item["selected"]]

    def block(group: Sequence[dict[str, object]], code: str) -> None:
        batch_codes.append(code)
        for target in group:
            target["blocking_issues"] = _merge_codes(target["blocking_issues"], (code,))

    positions = [item["selection_position"] for item in selected]
    if positions != list(range(len(selected))):
        block(selected, "selection_position_contract_violation")
    primary = [item for item in selected if item["image_role"] == "primary"]
    if (selected and (len(primary) != 1 or primary[0]["selection_position"] != 0)) or (not selected and primary):
        block(selected, "selection_position_contract_violation")
    for item in selected:
        if item["folder_role"] not in {"storefront_photos", "factory_photos"}:
            item["blocking_issues"] = _merge_codes(item["blocking_issues"], ("invalid_selected_folder_role",))
            batch_codes.append("invalid_selected_folder_role")
        if not item["quality_eligible"] or item["quality_reason"] != "quality_pass":
            item["blocking_issues"] = _merge_codes(item["blocking_issues"], ("selected_quality_ineligible",))
            batch_codes.append("selected_quality_ineligible")
    if len(selected) > image_selection_policy.MAX_IMAGES_PER_SKU:
        block(selected, "selection_limit_contract_violation")
    if (
        batch.total_candidates != len(items)
        or batch.selected_count != len(selected)
        or batch.primary_count != len(primary)
        or batch.gallery_count != sum(item["image_role"] == "gallery" for item in selected)
    ):
        block(items, "selection_count_contract_violation")
    return {
        "sku": batch.sku, "total_candidates": batch.total_candidates,
        "quality_candidates": batch.quality_candidates,
        "storefront_candidates": batch.storefront_candidates,
        "factory_candidates": batch.factory_candidates,
        "selected_count": batch.selected_count,
        "selected_storefront": batch.selected_storefront,
        "selected_factory": batch.selected_factory,
        "primary_count": batch.primary_count, "gallery_count": batch.gallery_count,
        "warnings": list(batch.warnings),
        "blocking_issues": _merge_codes(batch_codes),
        "items": items,
    }


def _summary(results: Sequence[Mapping[str, object]]) -> dict[str, int]:
    items = [item for batch in results for item in batch["items"]]
    selected = [item for item in items if item["selected"]]
    return {
        "total_quality_items": len(items), "total_skus": len(results),
        "selected_total": len(selected), "not_selected_total": len(items) - len(selected),
        "selected_storefront": sum(item["folder_role"] == "storefront_photos" for item in selected),
        "selected_factory": sum(item["folder_role"] == "factory_photos" for item in selected),
        "primary_total": sum(item["image_role"] == "primary" for item in selected),
        "gallery_total": sum(item["image_role"] == "gallery" for item in selected),
        "skus_at_limit": sum(batch["selected_count"] == image_selection_policy.MAX_IMAGES_PER_SKU for batch in results),
        "skus_below_limit": sum(batch["selected_count"] < image_selection_policy.MAX_IMAGES_PER_SKU for batch in results),
        "factory_fill_skus": sum(batch["selected_storefront"] > 0 and batch["selected_factory"] > 0 for batch in results),
        "factory_primary_fallback_skus": sum("primary_from_factory_fallback" in batch["warnings"] for batch in results),
        "no_quality_image_skus": sum(batch["quality_candidates"] == 0 for batch in results),
        "selected_with_deeper_inventory": sum(item["requires_deeper_inventory"] for item in selected),
        "assets_with_warnings": sum(bool(item["warnings"]) for item in items),
        "blocking_assets": sum(bool(item["blocking_issues"]) for item in items),
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }


def build_image_selection_dry_run_report(
    quality_report: Mapping[str, object],
) -> dict[str, object]:
    """Restore the formal domains and delegate selection once to the Core."""
    restored = _restore_report(quality_report)
    candidates = tuple(item.candidate for item in restored)
    audits: defaultdict[tuple[object, ...], deque[dict[str, object]]] = defaultdict(deque)
    for item in restored:
        audits[_item_key_from_candidate(item.candidate)].append(item.quality_audit)
    try:
        batches = image_selection_policy.select_images(candidates)
    except image_selection_policy.ImageSelectionPolicyError:
        raise ImageSelectionDryRunInputError("image_selection_policy_failed") from None
    if len({batch.sku for batch in batches}) != len(batches):
        raise ImageSelectionDryRunInputError("duplicate_sku_batches")
    results = [_serialize_batch(batch, audits) for batch in batches]
    if any(audits.values()) or sum(batch["total_candidates"] for batch in results) != len(restored):
        raise ImageSelectionDryRunInputError("selection_record_accounting_mismatch")
    summary = _summary(results)
    report = {
        "status": "blocked" if any(batch["blocking_issues"] for batch in results) else "ok",
        "policy_version": image_selection_policy.POLICY_VERSION,
        "source_quality_policy_version": image_quality_policy.POLICY_VERSION,
        "summary": summary, "results": results,
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }
    _safe_metadata(report)
    return report


def _input_path(value: Path) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError):
        raise ImageSelectionDryRunInputError("local_quality_report_path_required") from None
    compact = tuple(re.sub(r"[^a-z0-9]", "", part.casefold()) for part in path.parts)
    if (
        path.name.casefold().startswith(".env")
        or path.name.casefold() in _FORBIDDEN_INPUT_NAMES
        or any(part.startswith(("credentials", "serviceaccount", "googleserviceaccount", "clientsecret"))
               or part in {"secrets", "tokenjson"} for part in compact)
    ):
        raise ImageSelectionDryRunInputError("forbidden_input_report_path")
    try:
        return _local_path(path)
    except (ImageAssetTypeDryRunInputError, TypeError, ValueError, OSError):
        raise ImageSelectionDryRunInputError("local_quality_report_path_required") from None


def run_image_selection_dry_run(
    quality_report_path: Path,
    *,
    project_root: Path,
) -> tuple[dict[str, object], Path]:
    """Read one local Quality report and write one fixed Selection audit."""
    source = _input_path(quality_report_path)
    output = _local_path(Path(project_root) / "reports" / REPORT_FILENAME)
    temporary = _local_path(output.with_name(output.name + ".tmp"), require_json=False)
    if source in {output, temporary}:
        raise ImageSelectionDryRunInputError("input_report_collision")
    try:
        quality_report = load_local_json_report(source)
    except (OSError, ValueError, RecursionError):
        raise ImageSelectionDryRunInputError("local_quality_report_read_failed") from None
    report = build_image_selection_dry_run_report(quality_report)
    redactor = Redactor()
    report = sanitize_report_data(report, redactor)
    _safe_metadata(report)
    try:
        SafeJsonReportWriter(output, redactor).write(report)
    except (OSError, ValueError):
        raise ImageSelectionDryRunInputError("image_selection_report_write_failed") from None
    return report, output
