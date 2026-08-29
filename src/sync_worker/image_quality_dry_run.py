"""Offline exact metadata join for the Image Quality Policy Core.

Only successful, sanitized Unified Eligibility and Image Asset Type reports
enter this adapter.  Upstream-ineligible rows are counted and skipped.  Every
eligible candidate requires one exact metadata match before the existing
quality Core is called.  No media, provider, configuration or network I/O is
performed; the sole write is the fixed local JSON audit report.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import (
    folder_role_policy,
    image_asset_type_policy,
    image_quality_policy,
    unified_image_eligibility_policy,
    webp_output_policy,
)
from .image_asset_type_dry_run import (
    ImageAssetTypeDryRunInputError,
    _assert_safe_metadata,
    _local_path,
)
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor


REPORT_FILENAME = "image-quality-dry-run.json"
REQUEST_COUNTERS = (
    "network_requests_performed",
    "download_requests_performed",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "write_requests_performed",
)
_ASSET_COUNTERS = (
    "network_requests_performed", "download_requests_performed", "write_requests_performed",
)
_UNIFIED_REPORT_FIELDS = frozenset({
    "status", "policy_version", "folder_role_policy_version", "webp_policy_version",
    "summary", "results", *REQUEST_COUNTERS,
})
_UNIFIED_RESULT_FIELDS = frozenset({
    "sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name",
    "parent_safe_folder_name", "safe_name", "source_asset_class", "source_mime_type",
    "join_status", "folder_role", "folder_role_policy_version", "folder_gallery_eligible",
    "requires_deeper_inventory", "source_asset_eligible", "requires_webp_pipeline",
    "webp_action", "target_mime_type", "target_extension", "unified_image_eligible",
    "eligibility_reason", "unified_policy_version", "webp_policy_version", "warnings",
    "blocking_issues",
})
_UNIFIED_SUMMARY_FIELDS = frozenset({
    "total_assets", "root_assets", "depth1_assets", "depth2_assets",
    "folder_role_joined", "folder_role_missing", "folder_role_ambiguous",
    "unified_image_eligible", "unified_image_ineligible", "eligible_storefront_photos",
    "eligible_factory_photos", "ineligible_banner", "ineligible_video_folder",
    "ineligible_eye_options", "ineligible_promo_assets", "ineligible_other_skin_tone",
    "ineligible_unknown_role", "ineligible_missing_role", "ineligible_source_asset",
    "ineligible_invalid_webp_contract", "requires_deeper_inventory_assets",
    "assets_with_warnings", "blocking_assets", *REQUEST_COUNTERS,
})
_ASSET_REPORT_FIELDS = frozenset({
    "status", "policy_version", "summary", "results", *_ASSET_COUNTERS,
})
_ASSET_RESULT_FIELDS = frozenset({
    "sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name",
    "parent_safe_folder_name", "safe_name", "normalized_mime_type", "safe_extension",
    "asset_class", "classification_source", "storefront_eligible", "policy_version",
    "status", "size_bytes", "image_width", "image_height", "mime_type", "warnings",
    "blocking_issues",
})
_ASSET_SUMMARY_FIELDS = frozenset({
    "total_manifest_items_seen", "classified_assets", "skipped_nested_folders",
    "skipped_shortcuts", *(asset.value for asset in image_asset_type_policy.AssetClass),
    "storefront_eligible_assets", "storefront_ineligible_assets", "mime_classified",
    "extension_fallback", "mime_extension_mismatch", "assets_with_warnings",
    "blocking_assets", "root_assets", "depth1_assets", "depth2_assets", *_ASSET_COUNTERS,
})
_DEPTHS = {"root": 0, "nested": 1, "depth2": 2}
_ISSUE = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_MIME = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.ASCII,
)
_EXTENSION = re.compile(r"\.[a-z0-9]{1,12}", re.ASCII)
_CLASSIFICATION_STATUSES = {
    "mime": {"metadata_web_image", "metadata_classified"},
    "extension_fallback": {"extension_fallback_candidate"},
    "unknown": {"unknown"},
}
_FORBIDDEN_INPUT_NAMES = frozenset({
    "google-drive-folder-manifest-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
    "folder-role-dry-run.json",
    "webp-output-policy-dry-run.json",
    REPORT_FILENAME,
})


class ImageQualityDryRunInputError(ValueError):
    """Fixed safe error codes only; never paths or untrusted report values."""


@dataclass(frozen=True, slots=True)
class _JoinKey:
    sku: str
    start_row: int | None
    end_row: int | None
    source_manifest_kind: str
    depth: int
    safe_folder_name: str | None
    parent_safe_folder_name: str | None
    safe_name: str


@dataclass(frozen=True, slots=True)
class _UnifiedRecord:
    result: unified_image_eligibility_policy.UnifiedImageEligibilityResult
    context: dict[str, object]
    key: _JoinKey


@dataclass(frozen=True, slots=True)
class _AssetRecord:
    key: _JoinKey
    image_width: object
    image_height: object
    size_bytes: object
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]


def _schema(value: object, fields: frozenset[str], code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value) or set(value) != fields:
        raise ImageQualityDryRunInputError(code)
    return value


def _safe_metadata(value: object) -> None:
    try:
        _assert_safe_metadata(value)
    except (ImageAssetTypeDryRunInputError, RecursionError):
        raise ImageQualityDryRunInputError("unsafe_input_report") from None


def _text(value: object, code: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 500:
        raise ImageQualityDryRunInputError(code)
    if any(unicodedata.category(char) == "Cc" and not char.isspace() for char in value):
        raise ImageQualityDryRunInputError(code)
    _safe_metadata(value)
    return value


def _issues(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not _ISSUE.fullmatch(item) for item in value):
        raise ImageQualityDryRunInputError("invalid_report_issues")
    _safe_metadata(value)
    return tuple(value)


def _counter(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise ImageQualityDryRunInputError(code)
    return value


def _validate_summary(value: object, fields: frozenset[str], counters: tuple[str, ...]) -> None:
    summary = _schema(value, fields, "invalid_report_summary")
    for item in summary.values():
        _counter(item, "invalid_report_summary")
    if any(summary[counter] != 0 for counter in counters):
        raise ImageQualityDryRunInputError("input_report_not_offline")


def _rows(value: object, *, optional: bool) -> tuple[int | None, int | None, dict[str, int] | None]:
    if optional and value is None:
        return None, None, None
    rows = _schema(value, frozenset({"start_row", "end_row"}), "invalid_product_source")
    start = _counter(rows["start_row"], "invalid_product_source")
    end = _counter(rows["end_row"], "invalid_product_source")
    if start == 0 or end < start:
        raise ImageQualityDryRunInputError("invalid_product_source")
    return start, end, {"start_row": start, "end_row": end}


def _context(value: Mapping[str, object]) -> tuple[dict[str, object], _JoinKey]:
    kind, depth = value["source_manifest_kind"], value["depth"]
    if type(kind) is not str or kind not in _DEPTHS or type(depth) is not int or depth != _DEPTHS[kind]:
        raise ImageQualityDryRunInputError("invalid_asset_hierarchy")
    sku = _text(value["sku"], "invalid_sku")
    name = _text(value["safe_name"], "invalid_safe_name")
    folder = _text(value["safe_folder_name"], "invalid_safe_folder_name", optional=kind == "root")
    parent = _text(value["parent_safe_folder_name"], "invalid_parent_safe_folder_name", optional=kind != "depth2")
    if (
        (kind == "root" and (folder is not None or parent is not None))
        or (kind == "nested" and (folder is None or parent is not None))
        or (kind == "depth2" and (folder is None or parent is None))
    ):
        raise ImageQualityDryRunInputError("invalid_asset_hierarchy")
    start, end, source = _rows(value["product_source"], optional=kind == "root")
    if kind != "root" and source is None:
        raise ImageQualityDryRunInputError("invalid_product_source")
    context = {
        "sku": sku, "product_source": source, "source_manifest_kind": kind,
        "depth": depth, "safe_folder_name": folder,
        "parent_safe_folder_name": parent, "safe_name": name,
    }
    return context, _JoinKey(sku, start, end, kind, depth, folder, parent, name)


def _restore_unified_record(value: object) -> _UnifiedRecord:
    item = _schema(value, _UNIFIED_RESULT_FIELDS, "invalid_unified_record")
    context, key = _context(item)
    if item["unified_policy_version"] != unified_image_eligibility_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("unified_policy_version_mismatch")
    role_value = item["folder_role"]
    try:
        role = None if role_value is None else folder_role_policy.FolderRole(role_value)
    except (TypeError, ValueError):
        raise ImageQualityDryRunInputError("invalid_unified_folder_role") from None
    folder_version = item["folder_role_policy_version"]
    if (
        (role is None and folder_version is not None)
        or (role is not None and folder_version != folder_role_policy.POLICY_VERSION)
    ):
        raise ImageQualityDryRunInputError("folder_role_policy_version_mismatch")
    if item["webp_policy_version"] != webp_output_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("webp_policy_version_mismatch")
    try:
        image_asset_type_policy.AssetClass(item["source_asset_class"])
    except (TypeError, ValueError):
        raise ImageQualityDryRunInputError("invalid_unified_source_asset") from None
    source_mime = item["source_mime_type"]
    if source_mime is not None and (type(source_mime) is not str or not _MIME.fullmatch(source_mime)):
        raise ImageQualityDryRunInputError("invalid_unified_source_asset")
    try:
        action = None if item["webp_action"] is None else webp_output_policy.WebPAction(item["webp_action"])
        reason = unified_image_eligibility_policy.EligibilityReason(item["eligibility_reason"])
    except (TypeError, ValueError):
        raise ImageQualityDryRunInputError("invalid_unified_policy_result") from None
    flags = (
        item["folder_gallery_eligible"], item["requires_deeper_inventory"],
        item["source_asset_eligible"], item["requires_webp_pipeline"],
        item["unified_image_eligible"],
    )
    if any(type(flag) is not bool for flag in flags):
        raise ImageQualityDryRunInputError("invalid_unified_policy_result")
    if type(item["join_status"]) is not str or item["join_status"] not in {"joined", "missing", "ambiguous"}:
        raise ImageQualityDryRunInputError("invalid_unified_join_status")
    if item["target_mime_type"] != "image/webp" or item["target_extension"] != ".webp":
        raise ImageQualityDryRunInputError("invalid_unified_target_contract")
    warnings, blockers = _issues(item["warnings"]), _issues(item["blocking_issues"])
    result = unified_image_eligibility_policy.UnifiedImageEligibilityResult(
        folder_role=role, folder_role_policy_version=folder_version,
        webp_policy_version=item["webp_policy_version"],
        folder_gallery_eligible=item["folder_gallery_eligible"],
        source_asset_eligible=item["source_asset_eligible"],
        requires_webp_pipeline=item["requires_webp_pipeline"], webp_action=action,
        unified_image_eligible=item["unified_image_eligible"], eligibility_reason=reason,
        requires_deeper_inventory=item["requires_deeper_inventory"],
        warnings=warnings, blocking_issues=blockers,
    )
    if result.unified_image_eligible and item["join_status"] != "joined":
        raise ImageQualityDryRunInputError("invalid_unified_candidate_contract")
    context.update({
        "folder_role": role.value if role is not None else None,
        "unified_image_eligible": result.unified_image_eligible,
        "requires_deeper_inventory": result.requires_deeper_inventory,
    })
    return _UnifiedRecord(result, context, key)


def _restore_unified_report(value: object) -> list[_UnifiedRecord]:
    report = _schema(value, _UNIFIED_REPORT_FIELDS, "invalid_unified_report")
    _safe_metadata(report)
    if report["status"] != "ok":
        raise ImageQualityDryRunInputError("unified_report_status_not_ok")
    if report["policy_version"] != unified_image_eligibility_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("unified_policy_version_mismatch")
    if report["folder_role_policy_version"] != folder_role_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("folder_role_policy_version_mismatch")
    if report["webp_policy_version"] != webp_output_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("webp_policy_version_mismatch")
    _validate_summary(report["summary"], _UNIFIED_SUMMARY_FIELDS, REQUEST_COUNTERS)
    for counter in REQUEST_COUNTERS:
        if type(report[counter]) is not int or report[counter] != 0:
            raise ImageQualityDryRunInputError("input_report_not_offline")
    if not isinstance(report["results"], list):
        raise ImageQualityDryRunInputError("invalid_unified_results")
    return [_restore_unified_record(item) for item in report["results"]]


def _metadata_value(value: object) -> object:
    # Quality Core owns missing/type/range rules.  The adapter accepts only
    # bounded JSON scalar shapes and never coerces or guesses a value.
    if value is None or type(value) in {bool, int, float}:
        return value
    if type(value) is str and len(value) <= 100:
        _safe_metadata(value)
        return value
    raise ImageQualityDryRunInputError("invalid_asset_metadata_shape")


def _restore_asset_record(value: object) -> _AssetRecord:
    item = _schema(value, _ASSET_RESULT_FIELDS, "invalid_asset_record")
    context, key = _context(item)
    if item["policy_version"] != image_asset_type_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("asset_policy_version_mismatch")
    try:
        image_asset_type_policy.AssetClass(item["asset_class"])
    except (TypeError, ValueError):
        raise ImageQualityDryRunInputError("invalid_asset_class") from None
    source, status = item["classification_source"], item["status"]
    if (
        type(source) is not str or source not in _CLASSIFICATION_STATUSES
        or type(status) is not str or status not in _CLASSIFICATION_STATUSES[source]
    ):
        raise ImageQualityDryRunInputError("invalid_asset_classification")
    if type(item["storefront_eligible"]) is not bool:
        raise ImageQualityDryRunInputError("invalid_asset_classification")
    normalized_mime = item["normalized_mime_type"]
    if normalized_mime is not None and (
        type(normalized_mime) is not str or not _MIME.fullmatch(normalized_mime)
    ):
        raise ImageQualityDryRunInputError("invalid_asset_mime")
    raw_mime = item["mime_type"]
    if raw_mime is not None:
        if type(raw_mime) is not str or len(raw_mime) > 500:
            raise ImageQualityDryRunInputError("invalid_asset_mime")
        _safe_metadata(raw_mime)
    extension = item["safe_extension"]
    if extension is not None and (type(extension) is not str or not _EXTENSION.fullmatch(extension)):
        raise ImageQualityDryRunInputError("invalid_asset_extension")
    warnings, blockers = _issues(item["warnings"]), _issues(item["blocking_issues"])
    return _AssetRecord(
        key=key, image_width=_metadata_value(item["image_width"]),
        image_height=_metadata_value(item["image_height"]),
        size_bytes=_metadata_value(item["size_bytes"]),
        warnings=warnings, blocking_issues=blockers,
    )


def _restore_asset_report(value: object) -> list[_AssetRecord]:
    report = _schema(value, _ASSET_REPORT_FIELDS, "invalid_asset_report")
    _safe_metadata(report)
    if report["status"] != "ok":
        raise ImageQualityDryRunInputError("asset_report_status_not_ok")
    if report["policy_version"] != image_asset_type_policy.POLICY_VERSION:
        raise ImageQualityDryRunInputError("asset_policy_version_mismatch")
    _validate_summary(report["summary"], _ASSET_SUMMARY_FIELDS, _ASSET_COUNTERS)
    for counter in _ASSET_COUNTERS:
        if type(report[counter]) is not int or report[counter] != 0:
            raise ImageQualityDryRunInputError("input_report_not_offline")
    if not isinstance(report["results"], list):
        raise ImageQualityDryRunInputError("invalid_asset_results")
    return [_restore_asset_record(item) for item in report["results"]]


def _merge_codes(*groups: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(code for group in groups for code in group))


def _join_failure(record: _UnifiedRecord, matches: list[_AssetRecord], reason: str) -> dict[str, object]:
    return {
        **record.context,
        "image_width": None, "image_height": None, "short_edge": None,
        "long_edge": None, "pixel_count": None, "megapixels": None,
        "size_bytes": None, "orientation": None,
        "quality_eligible": False, "quality_reason": reason,
        "min_short_edge_px": image_quality_policy.MIN_SHORT_EDGE_PX,
        "min_megapixels": image_quality_policy.MIN_MEGAPIXELS,
        "quality_policy_version": image_quality_policy.POLICY_VERSION,
        "warnings": _merge_codes(
            record.result.warnings, *(match.warnings for match in matches),
        ),
        "blocking_issues": _merge_codes(
            record.result.blocking_issues,
            *(match.blocking_issues for match in matches),
            (reason,),
        ),
        "join_status": "missing" if not matches else "ambiguous",
    }


def _evaluate_joined(record: _UnifiedRecord, asset: _AssetRecord) -> dict[str, object]:
    quality = image_quality_policy.evaluate_image_quality(
        record.result, image_width=asset.image_width,
        image_height=asset.image_height, size_bytes=asset.size_bytes,
    )
    data = quality.to_dict()
    return {
        **record.context,
        **{key: data[key] for key in (
            "image_width", "image_height", "short_edge", "long_edge", "pixel_count",
            "megapixels", "size_bytes", "orientation", "quality_eligible",
            "quality_reason", "min_short_edge_px", "min_megapixels",
        )},
        "quality_policy_version": data["policy_version"],
        "warnings": _merge_codes(tuple(data["warnings"]), asset.warnings),
        "blocking_issues": _merge_codes(tuple(data["blocking_issues"]), asset.blocking_issues),
        "join_status": "joined",
    }


def _summary(total: int, skipped: int, results: list[dict[str, object]]) -> dict[str, int]:
    reason = lambda value: sum(item["quality_reason"] == value for item in results)
    passed = [item for item in results if item["quality_eligible"]]
    return {
        "total_unified_assets": total,
        "upstream_eligible_candidates": len(results),
        "skipped_upstream_ineligible": skipped,
        "quality_metadata_joined": sum(item["join_status"] == "joined" for item in results),
        "quality_metadata_missing": sum(item["join_status"] == "missing" for item in results),
        "quality_metadata_ambiguous": sum(item["join_status"] == "ambiguous" for item in results),
        "quality_evaluated": sum(item["join_status"] == "joined" for item in results),
        "quality_pass": len(passed), "quality_fail": len(results) - len(passed),
        "fail_short_edge": reason("short_edge_below_minimum"),
        "fail_megapixels": reason("megapixels_below_minimum"),
        "fail_metadata_missing": reason("quality_metadata_missing"),
        "fail_metadata_invalid": reason("quality_metadata_invalid"),
        "fail_upstream": reason("upstream_image_ineligible"),
        "fail_invalid_policy_input": reason("invalid_policy_input"),
        "portrait": sum(item["orientation"] == "portrait" for item in results),
        "landscape": sum(item["orientation"] == "landscape" for item in results),
        "square": sum(item["orientation"] == "square" for item in results),
        "storefront_quality_pass": sum(
            item["quality_eligible"] and item["folder_role"] == "storefront_photos" for item in results
        ),
        "factory_quality_pass": sum(
            item["quality_eligible"] and item["folder_role"] == "factory_photos" for item in results
        ),
        "requires_deeper_inventory_quality_pass": sum(
            item["quality_eligible"] and item["requires_deeper_inventory"] for item in results
        ),
        "assets_with_warnings": sum(bool(item["warnings"]) for item in results),
        "blocking_assets": sum(bool(item["blocking_issues"]) for item in results),
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }


def build_image_quality_dry_run_report(
    unified_report: Mapping[str, object],
    asset_report: Mapping[str, object],
) -> dict[str, object]:
    """Validate both inputs, exact-join eligible candidates, then call Core."""
    # Complete validation precedes the first quality decision.
    unified_records = _restore_unified_report(unified_report)
    assets = _restore_asset_report(asset_report)
    eligible = [record for record in unified_records if record.result.unified_image_eligible]
    by_key: defaultdict[_JoinKey, list[_AssetRecord]] = defaultdict(list)
    for asset in assets:
        by_key[asset.key].append(asset)
    results = []
    for record in eligible:  # Preserve stable Unified report order; no ranking/deduplication.
        matches = by_key[record.key]
        if not matches:
            results.append(_join_failure(record, matches, "quality_metadata_join_missing"))
        elif len(matches) > 1:
            results.append(_join_failure(record, matches, "quality_metadata_join_ambiguous"))
        else:
            results.append(_evaluate_joined(record, matches[0]))
    summary = _summary(len(unified_records), len(unified_records) - len(eligible), results)
    report = {
        "status": "blocked" if summary["blocking_assets"] else "ok",
        "policy_version": image_quality_policy.POLICY_VERSION,
        "source_unified_policy_version": unified_image_eligibility_policy.POLICY_VERSION,
        "source_asset_policy_version": image_asset_type_policy.POLICY_VERSION,
        "summary": summary, "results": results,
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }
    _safe_metadata(report)
    return report


def _input_path(value: Path) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError):
        raise ImageQualityDryRunInputError("local_input_report_path_required") from None
    compact = tuple(re.sub(r"[^a-z0-9]", "", part.casefold()) for part in path.parts)
    if (
        path.name.casefold().startswith(".env")
        or path.name.casefold() in _FORBIDDEN_INPUT_NAMES
        or any(part.startswith(("credentials", "serviceaccount", "googleserviceaccount", "clientsecret"))
               or part in {"secrets", "tokenjson"} for part in compact)
    ):
        raise ImageQualityDryRunInputError("forbidden_input_report_path")
    try:
        return _local_path(path)
    except (ImageAssetTypeDryRunInputError, TypeError, ValueError, OSError):
        raise ImageQualityDryRunInputError("local_input_report_path_required") from None


def run_image_quality_dry_run(
    unified_report_path: Path,
    asset_report_path: Path,
    *,
    project_root: Path,
) -> tuple[dict[str, object], Path]:
    """Read exactly two local JSON reports and write one fixed audit report."""
    unified_path, asset_path = _input_path(unified_report_path), _input_path(asset_report_path)
    output = _local_path(Path(project_root) / "reports" / REPORT_FILENAME)
    temporary = _local_path(output.with_name(output.name + ".tmp"), require_json=False)
    if unified_path == asset_path or unified_path in {output, temporary} or asset_path in {output, temporary}:
        raise ImageQualityDryRunInputError("input_report_collision")
    try:
        unified_report = load_local_json_report(unified_path)
        asset_report = load_local_json_report(asset_path)
    except (OSError, ValueError, RecursionError):
        raise ImageQualityDryRunInputError("local_input_report_read_failed") from None
    report = build_image_quality_dry_run_report(unified_report, asset_report)
    redactor = Redactor()
    report = sanitize_report_data(report, redactor)
    _safe_metadata(report)
    try:
        SafeJsonReportWriter(output, redactor).write(report)
    except (OSError, ValueError):
        raise ImageQualityDryRunInputError("image_quality_report_write_failed") from None
    return report, output
