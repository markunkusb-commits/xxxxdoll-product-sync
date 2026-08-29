"""Offline exact-join adapter for unified image eligibility decisions.

This module consumes only the two already-sanitized, versioned local reports.
It restores their domain results, joins hierarchy provenance exactly, and calls
the existing Unified Image Eligibility Core once per WebP asset.  It never
opens media, guesses folder roles, creates clients, or grants upload authority.
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
    unified_image_eligibility_policy,
    webp_output_policy,
)
from .folder_role_dry_run import (
    FolderRoleDryRunInputError,
    _assert_safe_input,
    _local_path,
)
from .image_mapping import ProductSourceRange
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor


REPORT_FILENAME = "unified-image-eligibility-dry-run.json"
REQUEST_COUNTERS = (
    "network_requests_performed",
    "download_requests_performed",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "write_requests_performed",
)
_FOLDER_COUNTERS = (
    "network_requests_performed", "download_requests_performed", "write_requests_performed",
)
_FOLDER_REPORT_FIELDS = frozenset({
    "status", "policy_version", "summary", "results", *_FOLDER_COUNTERS,
})
_FOLDER_RESULT_FIELDS = frozenset({
    "role", "policy_version", "normalized_folder_name", "matched_rule", "depth",
    "parent_safe_folder_name", "sku", "product_source", "gallery_eligible",
    "requires_deeper_inventory", "warnings", "blocking_issues", "safe_folder_name",
    "source_manifest_kind",
})
_FOLDER_SUMMARY_FIELDS = frozenset({
    "total_folders", "depth1_folders", "depth2_folders",
    *(role.value for role in folder_role_policy.FolderRole),
    "gallery_eligible_folders", "requires_deeper_inventory_folders",
    "folders_with_warnings", "blocking_folders", *_FOLDER_COUNTERS,
})
_WEBP_REPORT_FIELDS = frozenset({
    "status", "policy_version", "source_policy_version", "summary", "results",
    *REQUEST_COUNTERS,
})
_WEBP_RESULT_FIELDS = frozenset({
    "sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name",
    "parent_safe_folder_name", "safe_name", "source_asset_class", "source_mime_type",
    "source_asset_eligible", "requires_webp_pipeline", "webp_action",
    "target_mime_type", "target_extension", "wordpress_upload_ready", "reason",
    "warnings", "blocking_issues", "policy_version",
})
_WEBP_SUMMARY_FIELDS = frozenset({
    "total_assets", "source_asset_eligible", "source_asset_ineligible",
    "requires_webp_pipeline", *(action.value for action in webp_output_policy.WebPAction),
    "wordpress_upload_ready", "jpeg_sources", "png_sources", "webp_sources",
    "design_sources", "video_sources", "unsupported_sources", "unknown_sources",
    "other_media_sources", "assets_with_warnings", "blocking_assets", *REQUEST_COUNTERS,
})
_DEPTHS = {"root": 0, "nested": 1, "depth2": 2}
_ISSUE = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_MIME = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.ASCII,
)
_FORBIDDEN_INPUT_NAMES = frozenset({
    "google-drive-folder-manifest-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
    "image-asset-type-dry-run.json",
    REPORT_FILENAME,
})
_INVALID_WEBP_CONTRACT_REASONS = frozenset({
    "invalid_webp_action", "invalid_webp_target", "webp_pipeline_not_required",
    "wordpress_upload_ready_contract_violation", "invalid_webp_pipeline_contract",
})


class UnifiedImageEligibilityDryRunInputError(ValueError):
    """Fixed safe codes only; never input values, paths or report contents."""


@dataclass(frozen=True, slots=True)
class _JoinKey:
    sku: str
    start_row: int
    end_row: int
    source_manifest_kind: str
    depth: int
    safe_folder_name: str
    parent_safe_folder_name: str | None


@dataclass(frozen=True, slots=True)
class _FolderRecord:
    result: folder_role_policy.FolderRoleClassification
    key: _JoinKey


@dataclass(frozen=True, slots=True)
class _WebPRecord:
    result: webp_output_policy.WebPOutputPolicyResult
    context: dict[str, object]
    key: _JoinKey | None
    contract_issues: tuple[str, ...]


def _schema(value: object, fields: frozenset[str], code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise UnifiedImageEligibilityDryRunInputError(code)
    if set(value) != fields or any(type(key) is not str for key in value):
        raise UnifiedImageEligibilityDryRunInputError(code)
    return value


def _safe_metadata(value: object) -> None:
    try:
        _assert_safe_input(value)
    except (FolderRoleDryRunInputError, RecursionError):
        raise UnifiedImageEligibilityDryRunInputError("unsafe_input_report") from None


def _text(value: object, code: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 500:
        raise UnifiedImageEligibilityDryRunInputError(code)
    if any(unicodedata.category(char) == "Cc" and not char.isspace() for char in value):
        raise UnifiedImageEligibilityDryRunInputError(code)
    _safe_metadata(value)
    return value


def _issues(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not _ISSUE.fullmatch(item) for item in value):
        raise UnifiedImageEligibilityDryRunInputError("invalid_report_issues")
    _safe_metadata(value)
    return tuple(value)


def _nonnegative(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise UnifiedImageEligibilityDryRunInputError(code)
    return value


def _rows(value: object, *, optional: bool = False) -> ProductSourceRange | None:
    if optional and value is None:
        return None
    rows = _schema(value, frozenset({"start_row", "end_row"}), "invalid_product_source")
    start = _nonnegative(rows["start_row"], "invalid_product_source")
    end = _nonnegative(rows["end_row"], "invalid_product_source")
    if start == 0 or end < start:
        raise UnifiedImageEligibilityDryRunInputError("invalid_product_source")
    return ProductSourceRange(start, end)


def _validate_summary(value: object, fields: frozenset[str], counters: tuple[str, ...]) -> None:
    summary = _schema(value, fields, "invalid_report_summary")
    for item in summary.values():
        _nonnegative(item, "invalid_report_summary")
    if any(summary[counter] != 0 for counter in counters):
        raise UnifiedImageEligibilityDryRunInputError("input_report_not_offline")


def _restore_folder_record(value: object) -> _FolderRecord:
    item = _schema(value, _FOLDER_RESULT_FIELDS, "invalid_folder_role_record")
    if item["policy_version"] != folder_role_policy.POLICY_VERSION:
        raise UnifiedImageEligibilityDryRunInputError("folder_role_policy_version_mismatch")
    kind, depth = item["source_manifest_kind"], item["depth"]
    if kind not in {"nested", "depth2"} or type(depth) is not int or depth != _DEPTHS[kind]:
        raise UnifiedImageEligibilityDryRunInputError("invalid_folder_role_depth")
    try:
        role = folder_role_policy.FolderRole(item["role"])
    except (TypeError, ValueError):
        raise UnifiedImageEligibilityDryRunInputError("invalid_folder_role") from None
    normalized = _text(item["normalized_folder_name"], "invalid_folder_role_text")
    matched_rule = _text(item["matched_rule"], "invalid_folder_role_rule", optional=True)
    if matched_rule is not None and not _ISSUE.fullmatch(matched_rule):
        raise UnifiedImageEligibilityDryRunInputError("invalid_folder_role_rule")
    parent = _text(
        item["parent_safe_folder_name"], "invalid_parent_safe_folder_name",
        optional=kind == "nested",
    )
    if (kind == "nested" and parent is not None) or (kind == "depth2" and parent is None):
        raise UnifiedImageEligibilityDryRunInputError("invalid_parent_safe_folder_name")
    sku = _text(item["sku"], "invalid_sku")
    name = _text(item["safe_folder_name"], "invalid_safe_folder_name")
    source = _rows(item["product_source"])
    if type(item["gallery_eligible"]) is not bool or type(item["requires_deeper_inventory"]) is not bool:
        raise UnifiedImageEligibilityDryRunInputError("invalid_folder_role_flags")
    warnings, blockers = _issues(item["warnings"]), _issues(item["blocking_issues"])
    result = folder_role_policy.FolderRoleClassification(
        role=role, normalized_folder_name=normalized, matched_rule=matched_rule,
        depth=depth, parent_safe_folder_name=parent, sku=sku, product_source=source,
        gallery_eligible=item["gallery_eligible"],
        requires_deeper_inventory=item["requires_deeper_inventory"],
        warnings=warnings, blocking_issues=blockers,
    )
    key = _JoinKey(sku, source.start_row, source.end_row, kind, depth, name, parent)
    return _FolderRecord(result, key)


def _restore_folder_report(value: object) -> list[_FolderRecord]:
    report = _schema(value, _FOLDER_REPORT_FIELDS, "invalid_folder_role_report")
    _safe_metadata(report)
    if report["status"] != "ok":
        raise UnifiedImageEligibilityDryRunInputError("folder_role_report_status_not_ok")
    if report["policy_version"] != folder_role_policy.POLICY_VERSION:
        raise UnifiedImageEligibilityDryRunInputError("folder_role_policy_version_mismatch")
    _validate_summary(report["summary"], _FOLDER_SUMMARY_FIELDS, _FOLDER_COUNTERS)
    for counter in _FOLDER_COUNTERS:
        if type(report[counter]) is not int or report[counter] != 0:
            raise UnifiedImageEligibilityDryRunInputError("input_report_not_offline")
    if not isinstance(report["results"], list):
        raise UnifiedImageEligibilityDryRunInputError("invalid_folder_role_results")
    return [_restore_folder_record(item) for item in report["results"]]


def _restore_webp_record(value: object) -> _WebPRecord:
    item = _schema(value, _WEBP_RESULT_FIELDS, "invalid_webp_record")
    if item["policy_version"] != webp_output_policy.POLICY_VERSION:
        raise UnifiedImageEligibilityDryRunInputError("webp_policy_version_mismatch")
    kind, depth = item["source_manifest_kind"], item["depth"]
    if kind not in _DEPTHS or type(depth) is not int or depth != _DEPTHS[kind]:
        raise UnifiedImageEligibilityDryRunInputError("invalid_webp_depth")
    sku = _text(item["sku"], "invalid_sku")
    name = _text(item["safe_name"], "invalid_safe_name")
    folder = _text(item["safe_folder_name"], "invalid_safe_folder_name", optional=kind == "root")
    parent = _text(item["parent_safe_folder_name"], "invalid_parent_safe_folder_name", optional=kind != "depth2")
    if (
        (kind == "root" and (folder is not None or parent is not None))
        or (kind == "nested" and (folder is None or parent is not None))
        or (kind == "depth2" and (folder is None or parent is None))
    ):
        raise UnifiedImageEligibilityDryRunInputError("invalid_webp_hierarchy")
    source = _rows(item["product_source"], optional=kind == "root")
    if kind != "root" and source is None:
        raise UnifiedImageEligibilityDryRunInputError("invalid_product_source")
    try:
        asset_class = image_asset_type_policy.AssetClass(item["source_asset_class"])
    except (TypeError, ValueError):
        raise UnifiedImageEligibilityDryRunInputError("invalid_source_asset_class") from None
    mime = item["source_mime_type"]
    if mime is not None and (type(mime) is not str or not _MIME.fullmatch(mime)):
        raise UnifiedImageEligibilityDryRunInputError("invalid_source_mime_type")
    if mime is not None:
        _safe_metadata(mime)
    if type(item["source_asset_eligible"]) is not bool or type(item["requires_webp_pipeline"]) is not bool:
        raise UnifiedImageEligibilityDryRunInputError("invalid_webp_flags")
    warnings, blockers = _issues(item["warnings"]), _issues(item["blocking_issues"])
    reason = item["reason"]
    if reason is not None and (type(reason) is not str or not _ISSUE.fullmatch(reason)):
        raise UnifiedImageEligibilityDryRunInputError("invalid_webp_reason")
    contract: list[str] = []
    try:
        action = webp_output_policy.WebPAction(item["webp_action"])
    except (TypeError, ValueError):
        action = webp_output_policy.WebPAction.NOT_ALLOWED
        contract.append("invalid_webp_action")
    if item["target_mime_type"] != "image/webp" or item["target_extension"] != ".webp":
        contract.append("invalid_webp_target")
    if item["wordpress_upload_ready"] is not False:
        contract.append("wordpress_upload_ready_contract_violation")
    if (
        item["source_asset_eligible"] != item["requires_webp_pipeline"]
        or item["source_asset_eligible"] != (action is not webp_output_policy.WebPAction.NOT_ALLOWED)
    ):
        contract.append("invalid_webp_pipeline_contract")
    result = webp_output_policy.WebPOutputPolicyResult(
        source_asset_class=asset_class, source_mime_type=mime,
        source_asset_eligible=item["source_asset_eligible"],
        requires_webp_pipeline=item["requires_webp_pipeline"], webp_action=action,
        reason=reason, warnings=warnings, blocking_issues=blockers,
    )
    context = {
        "sku": sku,
        "product_source": source.to_dict() if source is not None else None,
        "source_manifest_kind": kind,
        "depth": depth,
        "safe_folder_name": folder,
        "parent_safe_folder_name": parent,
        "safe_name": name,
        "source_asset_class": asset_class.value,
        "source_mime_type": mime,
    }
    key = None if kind == "root" else _JoinKey(
        sku, source.start_row, source.end_row, kind, depth, folder, parent,
    )
    return _WebPRecord(result, context, key, tuple(dict.fromkeys(contract)))


def _restore_webp_report(value: object) -> list[_WebPRecord]:
    report = _schema(value, _WEBP_REPORT_FIELDS, "invalid_webp_report")
    _safe_metadata(report)
    if report["status"] != "ok":
        raise UnifiedImageEligibilityDryRunInputError("webp_report_status_not_ok")
    if report["policy_version"] != webp_output_policy.POLICY_VERSION:
        raise UnifiedImageEligibilityDryRunInputError("webp_policy_version_mismatch")
    if report["source_policy_version"] != image_asset_type_policy.POLICY_VERSION:
        raise UnifiedImageEligibilityDryRunInputError("asset_type_policy_version_mismatch")
    _validate_summary(report["summary"], _WEBP_SUMMARY_FIELDS, REQUEST_COUNTERS)
    for counter in REQUEST_COUNTERS:
        if type(report[counter]) is not int or report[counter] != 0:
            raise UnifiedImageEligibilityDryRunInputError("input_report_not_offline")
    if not isinstance(report["results"], list):
        raise UnifiedImageEligibilityDryRunInputError("invalid_webp_results")
    return [_restore_webp_record(item) for item in report["results"]]


def _merge_codes(*values: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(code for group in values for code in group))


def _joined_context_matches(folder: _FolderRecord, asset: _WebPRecord) -> bool:
    """Independent post-join equality audit over every approved key field."""
    source = folder.result.product_source
    context_source = asset.context["product_source"]
    return (
        asset.key is not None
        and folder.key == asset.key
        and folder.result.sku == asset.context["sku"]
        and source is not None
        and context_source == source.to_dict()
        and folder.result.depth == asset.context["depth"]
        and folder.key.source_manifest_kind == asset.context["source_manifest_kind"]
        and folder.key.safe_folder_name == asset.context["safe_folder_name"]
        and folder.result.parent_safe_folder_name == asset.context["parent_safe_folder_name"]
    )


def _result_record(asset: _WebPRecord, matches: list[_FolderRecord]) -> dict[str, object]:
    if asset.key is None:
        join_status, role = "missing", None
    elif not matches:
        join_status, role = "missing", None
    elif len(matches) > 1:
        join_status, role = "ambiguous", None
    elif _joined_context_matches(matches[0], asset):
        join_status, role = "joined", matches[0].result
    else:
        join_status, role = "joined", None

    # The existing Core remains the only eligibility policy.  None is an
    # explicit fail-closed domain value for root/missing/ambiguous joins.
    core = unified_image_eligibility_policy.evaluate_unified_image_eligibility(role, asset.result)
    data = core.to_dict()
    warnings = tuple(data["warnings"])
    blockers = tuple(data["blocking_issues"])
    join_warnings = tuple(code for match in matches for code in match.result.warnings)
    join_blockers = tuple(code for match in matches for code in match.result.blocking_issues)
    reason = data["eligibility_reason"]
    eligible = data["unified_image_eligible"]
    if asset.key is not None and join_status == "missing":
        reason, eligible = "missing_folder_role_join", False
        join_blockers += ("missing_folder_role_join",)
    elif join_status == "ambiguous":
        reason, eligible = "ambiguous_folder_role_join", False
        join_blockers += ("ambiguous_folder_role_join",)
    elif join_status == "joined" and role is None:
        reason, eligible = "folder_role_join_mismatch", False
        join_blockers += ("folder_role_join_mismatch",)
    if asset.contract_issues:
        reason, eligible = asset.contract_issues[0], False
        join_blockers += asset.contract_issues
    return {
        **asset.context,
        "join_status": join_status,
        "folder_role": data["folder_role"] if join_status == "joined" else None,
        "folder_role_policy_version": (
            data["folder_role_policy_version"] if join_status == "joined" else None
        ),
        "folder_gallery_eligible": data["folder_gallery_eligible"] if join_status == "joined" else False,
        "requires_deeper_inventory": data["requires_deeper_inventory"] if join_status == "joined" else False,
        "source_asset_eligible": data["source_asset_eligible"],
        "requires_webp_pipeline": data["requires_webp_pipeline"],
        "webp_action": data["webp_action"],
        "target_mime_type": data["target_mime_type"],
        "target_extension": data["target_extension"],
        "unified_image_eligible": eligible,
        "eligibility_reason": reason,
        "unified_policy_version": data["policy_version"],
        "webp_policy_version": data["webp_policy_version"],
        "warnings": _merge_codes(warnings, join_warnings),
        "blocking_issues": _merge_codes(blockers, join_blockers),
    }


def _summary(results: list[dict[str, object]]) -> dict[str, int]:
    ineligible = [item for item in results if not item["unified_image_eligible"]]
    return {
        "total_assets": len(results),
        "root_assets": sum(item["depth"] == 0 for item in results),
        "depth1_assets": sum(item["depth"] == 1 for item in results),
        "depth2_assets": sum(item["depth"] == 2 for item in results),
        "folder_role_joined": sum(item["join_status"] == "joined" for item in results),
        "folder_role_missing": sum(item["join_status"] == "missing" for item in results),
        "folder_role_ambiguous": sum(item["join_status"] == "ambiguous" for item in results),
        "unified_image_eligible": len(results) - len(ineligible),
        "unified_image_ineligible": len(ineligible),
        "eligible_storefront_photos": sum(
            item["unified_image_eligible"] and item["folder_role"] == "storefront_photos" for item in results
        ),
        "eligible_factory_photos": sum(
            item["unified_image_eligible"] and item["folder_role"] == "factory_photos" for item in results
        ),
        **{
            f"ineligible_{name}": sum(item["folder_role"] == role and not item["unified_image_eligible"] for item in results)
            for name, role in (
                ("banner", "banner"), ("video_folder", "video"), ("eye_options", "eye_options"),
                ("promo_assets", "promo_assets"), ("other_skin_tone", "other_skin_tone"),
                ("unknown_role", "unknown"),
            )
        },
        "ineligible_missing_role": sum(
            item["join_status"] == "missing" and not item["unified_image_eligible"] for item in results
        ),
        "ineligible_source_asset": sum(not item["source_asset_eligible"] for item in results),
        "ineligible_invalid_webp_contract": sum(
            (item["eligibility_reason"] in _INVALID_WEBP_CONTRACT_REASONS)
            or bool(set(item["blocking_issues"]) & _INVALID_WEBP_CONTRACT_REASONS)
            for item in results
        ),
        "requires_deeper_inventory_assets": sum(item["requires_deeper_inventory"] for item in results),
        "assets_with_warnings": sum(bool(item["warnings"]) for item in results),
        "blocking_assets": sum(bool(item["blocking_issues"]) for item in results),
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }


def build_unified_image_eligibility_dry_run_report(
    folder_role_report: Mapping[str, object],
    webp_report: Mapping[str, object],
) -> dict[str, object]:
    """Restore, exact-join and evaluate all local records deterministically."""
    # Fully validate/restore both inputs before the first Unified Core call.
    folders = _restore_folder_report(folder_role_report)
    assets = _restore_webp_report(webp_report)
    by_key: defaultdict[_JoinKey, list[_FolderRecord]] = defaultdict(list)
    for folder in folders:
        by_key[folder.key].append(folder)
    results = [_result_record(asset, [] if asset.key is None else by_key[asset.key]) for asset in assets]
    summary = _summary(results)
    report = {
        "status": "blocked" if summary["blocking_assets"] else "ok",
        "policy_version": unified_image_eligibility_policy.POLICY_VERSION,
        "folder_role_policy_version": folder_role_policy.POLICY_VERSION,
        "webp_policy_version": webp_output_policy.POLICY_VERSION,
        "summary": summary,
        "results": results,
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }
    _safe_metadata(report)
    return report


def _input_path(value: Path) -> Path:
    path = Path(value)
    compact = tuple(re.sub(r"[^a-z0-9]", "", part.casefold()) for part in path.parts)
    if (
        path.name.casefold().startswith(".env")
        or path.name.casefold() in _FORBIDDEN_INPUT_NAMES
        or any(part.startswith(("credentials", "serviceaccount", "googleserviceaccount", "clientsecret"))
               or part in {"secrets", "tokenjson"} for part in compact)
    ):
        raise UnifiedImageEligibilityDryRunInputError("forbidden_input_report_path")
    try:
        return _local_path(path)
    except (FolderRoleDryRunInputError, TypeError, ValueError, OSError):
        raise UnifiedImageEligibilityDryRunInputError("local_input_report_path_required") from None


def run_unified_image_eligibility_dry_run(
    folder_role_report_path: Path,
    webp_report_path: Path,
    *,
    project_root: Path,
) -> tuple[dict[str, object], Path]:
    """Read exactly two safe local reports and write one fixed JSON audit."""
    folder_path, webp_path = _input_path(folder_role_report_path), _input_path(webp_report_path)
    output = _local_path(Path(project_root) / "reports" / REPORT_FILENAME)
    temporary = _local_path(output.with_name(output.name + ".tmp"), require_json=False)
    if folder_path == webp_path or folder_path in {output, temporary} or webp_path in {output, temporary}:
        raise UnifiedImageEligibilityDryRunInputError("input_report_collision")
    try:
        folder_report = load_local_json_report(folder_path)
        webp_report = load_local_json_report(webp_path)
    except (OSError, ValueError, RecursionError):
        raise UnifiedImageEligibilityDryRunInputError("local_input_report_read_failed") from None
    report = build_unified_image_eligibility_dry_run_report(folder_report, webp_report)
    redactor = Redactor()
    report = sanitize_report_data(report, redactor)
    _safe_metadata(report)
    try:
        SafeJsonReportWriter(output, redactor).write(report)
    except (OSError, ValueError):
        raise UnifiedImageEligibilityDryRunInputError("unified_eligibility_report_write_failed") from None
    return report, output
