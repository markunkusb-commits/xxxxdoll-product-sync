"""Offline adapter from safe asset-type reports to WebP processing plans.

Only the JSON audit is written. Asset typing and WebP decisions remain in their
existing cores; folder names are provenance, never gallery-selection rules.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from . import image_asset_type_policy, webp_output_policy
from .image_asset_type_dry_run import (
    ImageAssetTypeDryRunInputError,
    _assert_safe_metadata,
    _local_path,
)
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor


REPORT_FILENAME = "webp-output-policy-dry-run.json"
REQUEST_COUNTERS = (
    "network_requests_performed", "download_requests_performed",
    "conversion_requests_performed", "wordpress_upload_requests_performed",
    "write_requests_performed",
)
_UPSTREAM_COUNTERS = frozenset({
    "network_requests_performed", "download_requests_performed", "write_requests_performed",
})
_REPORT_FIELDS = frozenset({"status", "policy_version", "summary", "results"}) | _UPSTREAM_COUNTERS
_ASSET_FIELDS = frozenset({
    "sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name",
    "parent_safe_folder_name", "safe_name", "mime_type", "normalized_mime_type",
    "safe_extension", "asset_class", "classification_source", "storefront_eligible",
    "policy_version", "status", "size_bytes", "image_width", "image_height",
    "warnings", "blocking_issues",
})
_OPTIONAL_ASSET_FIELDS = frozenset({"product_source", "mime_type", "size_bytes", "image_width", "image_height"})
_SUMMARY_FIELDS = frozenset({
    "total_manifest_items_seen", "classified_assets", "skipped_nested_folders",
    "skipped_shortcuts", "storefront_eligible_assets", "storefront_ineligible_assets",
    "mime_classified", "extension_fallback", "mime_extension_mismatch",
    "assets_with_warnings", "blocking_assets", "root_assets", "depth1_assets", "depth2_assets",
}) | _UPSTREAM_COUNTERS | frozenset(asset.value for asset in image_asset_type_policy.AssetClass)
_PLAN_FIELDS = frozenset({
    "policy_version", "source_asset_class", "source_mime_type", "source_asset_eligible",
    "requires_webp_pipeline", "webp_action", "target_mime_type", "target_extension",
    "wordpress_upload_ready", "reason", "warnings", "blocking_issues",
})
_DEPTHS = {"root": 0, "nested": 1, "depth2": 2}
_OTHER_REPORT_NAMES = frozenset({
    "google-drive-folder-manifest-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
    "folder-role-dry-run.json",
})
_SOURCE_STATUSES = {
    "mime": {"metadata_web_image", "metadata_classified"},
    "extension_fallback": {"extension_fallback_candidate"},
    "unknown": {"unknown"},
}
# Syntax validation only, not MIME classification or spelling normalization.
_MIME_SYNTAX = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.ASCII)
_ISSUE_SYNTAX = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_SENSITIVE_TEXT = re.compile(
    r"(?<![a-z0-9])(?:token|secret|credentials?|password|private[_ -]?key|client[_ -]?email)(?![a-z0-9])",
    re.IGNORECASE,
)


class WebPOutputPolicyDryRunInputError(ValueError):
    """Fixed safe codes only; never echo paths or untrusted report values."""


def _schema(value: object, allowed: frozenset[str], required: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebPOutputPolicyDryRunInputError("invalid_asset_report_object")
    if any(type(key) is not str or key not in allowed for key in value):
        raise WebPOutputPolicyDryRunInputError("unsafe_asset_report_field")
    if not required.issubset(value):
        raise WebPOutputPolicyDryRunInputError("missing_asset_report_field")
    return value


def _safe_metadata(value: object) -> None:
    try:
        _assert_safe_metadata(value)
    except (ImageAssetTypeDryRunInputError, RecursionError):
        raise WebPOutputPolicyDryRunInputError("unsafe_asset_report_metadata") from None


def _text(value: object, *, optional: bool = False, basename: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 500:
        raise WebPOutputPolicyDryRunInputError("invalid_asset_report_text")
    canonical = unicodedata.normalize("NFKC", value)
    _safe_metadata(canonical)
    if (
        _SENSITIVE_TEXT.search(canonical)
        or any(unicodedata.category(char).startswith("C") for char in canonical)
        or (basename and (any(char in canonical for char in "/\\:") or canonical in {".", ".."}))
    ):
        raise WebPOutputPolicyDryRunInputError("unsafe_asset_report_text")
    return value


def _issues(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise WebPOutputPolicyDryRunInputError("invalid_asset_report_issues")
    for item in value:
        _text(item, basename=True)
    return tuple(value)


def _number(value: object, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if type(value) is not int or value < 0:
        raise WebPOutputPolicyDryRunInputError("invalid_asset_report_number")
    return value


def _restore_asset(value: object) -> tuple[image_asset_type_policy.ImageAssetTypeResult, dict[str, object]]:
    item = _schema(value, _ASSET_FIELDS, _ASSET_FIELDS - _OPTIONAL_ASSET_FIELDS)
    if item["policy_version"] != image_asset_type_policy.POLICY_VERSION:
        raise WebPOutputPolicyDryRunInputError("asset_type_policy_version_mismatch")
    try:
        asset_class = image_asset_type_policy.AssetClass(item["asset_class"])
    except (TypeError, ValueError):
        raise WebPOutputPolicyDryRunInputError("invalid_asset_class") from None
    source = item["classification_source"]
    status = item["status"]
    if type(source) is not str or source not in _SOURCE_STATUSES or type(status) is not str or status not in _SOURCE_STATUSES[source]:
        raise WebPOutputPolicyDryRunInputError("invalid_asset_classification_source")
    if type(item["storefront_eligible"]) is not bool:
        raise WebPOutputPolicyDryRunInputError("invalid_storefront_eligible")
    mime = item["normalized_mime_type"]
    if mime is not None and (type(mime) is not str or not _MIME_SYNTAX.fullmatch(mime)):
        raise WebPOutputPolicyDryRunInputError("invalid_normalized_mime_type")
    if mime is not None:
        _text(mime)
    extension = item["safe_extension"]
    if extension is not None and (type(extension) is not str or not re.fullmatch(r"\.[a-z0-9]{1,12}", extension)):
        raise WebPOutputPolicyDryRunInputError("invalid_safe_extension")
    if item.get("mime_type") is not None:
        _text(item["mime_type"])

    kind = item["source_manifest_kind"]
    depth = item["depth"]
    if type(kind) is not str or kind not in _DEPTHS or type(depth) is not int or depth != _DEPTHS[kind]:
        raise WebPOutputPolicyDryRunInputError("invalid_asset_source_depth")
    context = {
        "sku": _text(item["sku"], basename=True),
        "source_manifest_kind": kind, "depth": depth,
        "safe_folder_name": _text(item["safe_folder_name"], optional=kind == "root", basename=True),
        "parent_safe_folder_name": _text(item["parent_safe_folder_name"], optional=kind != "depth2", basename=True),
        "safe_name": _text(item["safe_name"], basename=True),
    }
    product_source = item.get("product_source")
    if product_source is not None:
        row_fields = frozenset({"start_row", "end_row"})
        rows = _schema(product_source, row_fields, row_fields)
        start, end = _number(rows["start_row"]), _number(rows["end_row"])
        if start == 0 or end < start:
            raise WebPOutputPolicyDryRunInputError("invalid_product_source")
        product_source = {"start_row": start, "end_row": end}
    context["product_source"] = product_source
    asset = image_asset_type_policy.ImageAssetTypeResult(
        asset_class=asset_class, normalized_mime_type=mime, safe_extension=extension,
        storefront_eligible=item["storefront_eligible"], classification_source=source,
        status=status, safe_name=context["safe_name"], sku=context["sku"],
        size_bytes=_number(item.get("size_bytes"), optional=True),
        image_width=_number(item.get("image_width"), optional=True),
        image_height=_number(item.get("image_height"), optional=True),
        warnings=_issues(item["warnings"]), blocking_issues=_issues(item["blocking_issues"]),
    )
    return asset, context


def _checked_plan(asset: image_asset_type_policy.ImageAssetTypeResult) -> dict[str, object]:
    # The only business decision comes from Core. These independent envelope
    # checks prevent a regressed/injected Core from granting upload authority.
    plan = webp_output_policy.evaluate_webp_output_policy(asset)
    if type(plan) is not webp_output_policy.WebPOutputPolicyResult:
        raise WebPOutputPolicyDryRunInputError("invalid_webp_core_result")
    data = dict(_schema(plan.to_dict(), _PLAN_FIELDS, _PLAN_FIELDS))
    if (
        data["policy_version"] != webp_output_policy.POLICY_VERSION
        or data["source_asset_class"] != asset.asset_class.value
        or data["source_mime_type"] != asset.normalized_mime_type
        or type(data["webp_action"]) is not str
        or data["webp_action"] not in {action.value for action in webp_output_policy.WebPAction}
        or any(type(data[key]) is not bool for key in ("source_asset_eligible", "requires_webp_pipeline", "wordpress_upload_ready"))
    ):
        raise WebPOutputPolicyDryRunInputError("invalid_webp_core_result")
    warnings, blockers = _issues(data["warnings"]), _issues(data["blocking_issues"])
    if data["reason"] is not None and (type(data["reason"]) is not str or not _ISSUE_SYNTAX.fullmatch(data["reason"])):
        raise WebPOutputPolicyDryRunInputError("invalid_webp_core_reason")
    violations = []
    if data["wordpress_upload_ready"]:
        violations.append("wordpress_upload_ready_contract_violation")
    if data["target_mime_type"] != "image/webp" or data["target_extension"] != ".webp":
        violations.append("invalid_webp_target_contract")
    if (
        data["source_asset_eligible"] != data["requires_webp_pipeline"]
        or data["source_asset_eligible"] != (data["webp_action"] != webp_output_policy.WebPAction.NOT_ALLOWED.value)
        or (blockers and data["source_asset_eligible"])
        or not set(asset.blocking_issues).issubset(blockers)
    ):
        violations.append("invalid_webp_pipeline_contract")
    if violations:
        # Do not silently repair a violation into an apparently successful plan.
        # Persist a blocked, non-uploadable record with explicit safe blockers.
        data.update(
            source_asset_eligible=False, requires_webp_pipeline=False,
            webp_action=webp_output_policy.WebPAction.NOT_ALLOWED.value,
            wordpress_upload_ready=False, target_mime_type="image/webp", target_extension=".webp",
            reason="webp_output_contract_blocked",
        )
    data["warnings"] = list(warnings)
    data["blocking_issues"] = list(dict.fromkeys((*blockers, *violations)))
    _safe_metadata(data)
    return data


def build_webp_output_policy_dry_run_report(asset_report: Mapping[str, object]) -> dict[str, object]:
    """Plan only from a versioned, successful Image Asset Type report.

    Preserve upstream order and duplicates. Input summary totals never drive
    output counts. Validate the whole input before the first Core invocation.
    """
    source = _schema(asset_report, _REPORT_FIELDS, frozenset({"status", "policy_version", "summary", "results"}))
    if source["status"] != "ok":
        raise WebPOutputPolicyDryRunInputError("asset_report_status_not_ok")
    if source["policy_version"] != image_asset_type_policy.POLICY_VERSION:
        raise WebPOutputPolicyDryRunInputError("asset_type_policy_version_mismatch")
    _safe_metadata(source)
    upstream_summary = _schema(source["summary"], _SUMMARY_FIELDS, frozenset())
    for key, value in upstream_summary.items():
        _number(value)
    for counters in (source, upstream_summary):
        if any(key in counters and (type(counters[key]) is not int or counters[key] != 0) for key in _UPSTREAM_COUNTERS):
            raise WebPOutputPolicyDryRunInputError("asset_report_not_offline")
    if not isinstance(source["results"], list):
        raise WebPOutputPolicyDryRunInputError("invalid_asset_report_results")
    assets = [_restore_asset(item) for item in source["results"]]
    results = [{**context, **_checked_plan(asset)} for asset, context in assets]
    eligible = sum(item["source_asset_eligible"] for item in results)
    summary = {
        "total_assets": len(results), "source_asset_eligible": eligible,
        "source_asset_ineligible": len(results) - eligible,
        "requires_webp_pipeline": sum(item["requires_webp_pipeline"] for item in results),
        **{action.value: sum(item["webp_action"] == action.value for item in results) for action in webp_output_policy.WebPAction},
        "wordpress_upload_ready": sum(item["wordpress_upload_ready"] for item in results),
        **{f"{name}_sources": sum(item["source_mime_type"] == mime for item in results)
           for name, mime in (("jpeg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp"))},
        **{f"{name}_sources": sum(item["source_asset_class"] == asset_class.value for item in results)
           for name, asset_class in (("design", image_asset_type_policy.AssetClass.DESIGN_SOURCE),
                                    ("video", image_asset_type_policy.AssetClass.VIDEO),
                                    ("unsupported", image_asset_type_policy.AssetClass.UNSUPPORTED),
                                    ("unknown", image_asset_type_policy.AssetClass.UNKNOWN),
                                    ("other_media", image_asset_type_policy.AssetClass.OTHER_MEDIA))},
        "assets_with_warnings": sum(bool(item["warnings"]) for item in results),
        "blocking_assets": sum(bool(item["blocking_issues"]) for item in results),
        **dict.fromkeys(REQUEST_COUNTERS, 0),
    }
    report = {
        "status": "blocked" if summary["blocking_assets"] else "ok",
        "policy_version": webp_output_policy.POLICY_VERSION,
        "source_policy_version": image_asset_type_policy.POLICY_VERSION,
        "summary": summary, "results": results, **dict.fromkeys(REQUEST_COUNTERS, 0),
    }
    _safe_metadata(report)
    return report


def _report_path(value: Path, *, input_file: bool = False, require_json: bool = True) -> Path:
    try:
        path = Path(value)
        if input_file:
            # Reject known credential/other-workflow paths before any file open.
            compact_parts = [re.sub(r"[^a-z0-9]", "", part.casefold()) for part in path.parts]
            if (
                path.name.casefold().startswith(".env")
                or any(part.startswith(("credentials", "serviceaccount", "googleserviceaccount", "clientsecret"))
                       or part in {"secrets", "tokenjson"} for part in compact_parts)
                or path.name.casefold() in _OTHER_REPORT_NAMES
            ):
                raise WebPOutputPolicyDryRunInputError("forbidden_asset_report_path")
        return _local_path(path, require_json=require_json)
    except WebPOutputPolicyDryRunInputError:
        raise
    except (ImageAssetTypeDryRunInputError, TypeError, ValueError, OSError):
        raise WebPOutputPolicyDryRunInputError("local_asset_report_path_required") from None


def run_webp_output_policy_dry_run(
    asset_report_path: Path, *, project_root: Path,
) -> tuple[dict[str, object], Path]:
    """Read one local metadata JSON; write only the fixed, safe audit report."""
    input_path = _report_path(asset_report_path, input_file=True)
    output = _report_path(Path(project_root) / "reports" / REPORT_FILENAME)
    temporary = _report_path(output.with_name(output.name + ".tmp"), require_json=False)
    if input_path in {output, temporary}:
        raise WebPOutputPolicyDryRunInputError("asset_report_output_collision")
    try:
        source = load_local_json_report(input_path)
    except (OSError, ValueError, RecursionError):
        raise WebPOutputPolicyDryRunInputError("local_asset_report_read_failed") from None
    try:
        report = build_webp_output_policy_dry_run_report(source)
    except (webp_output_policy.WebPOutputPolicyError, RecursionError):
        raise WebPOutputPolicyDryRunInputError("invalid_asset_policy_input") from None
    redactor = Redactor()
    report = sanitize_report_data(report, redactor)
    _safe_metadata(report)
    try:
        SafeJsonReportWriter(output, redactor).write(report)
    except (OSError, ValueError):
        raise WebPOutputPolicyDryRunInputError("webp_plan_report_write_failed") from None
    return report, output
