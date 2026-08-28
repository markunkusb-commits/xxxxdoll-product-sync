"""Offline safe-manifest adapter for the Image Asset Type Policy.

Only file-kind items enter the policy. No traversal, role join, image selection,
quality filtering, deduplication or content inspection occurs here.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from . import image_asset_type_policy
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN_TEXT, Redactor


REPORT_FILENAME = "image-asset-type-dry-run.json"
_FILE_KINDS = frozenset({"image_candidate", "other_file", "google_workspace_file"})
_SKIPPED_KINDS = {"nested_folder": "skipped_nested_folders", "shortcut": "skipped_shortcuts"}
_POLICY_REPORT_FIELDS = (
    "safe_name", "normalized_mime_type", "safe_extension", "asset_class",
    "classification_source", "storefront_eligible", "policy_version", "status",
    "size_bytes", "image_width", "image_height",
)
_SECRET_PATTERN = re.compile(REPORT_SECRET_SCAN_PATTERN_TEXT, re.IGNORECASE)
_UNSAFE_TEXT_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://|\bdrive\.google\.com\b|\bwww\."
    r"|-----BEGIN [^-]*PRIVATE KEY-----"
    r"|\b(?:resource[_ ]?key|provider[_ ]file[_ ]id|raw[_ ](?:file|folder)[_ ]id)\s*[:=]",
    re.IGNORECASE,
)
_FORBIDDEN_KEYS = frozenset({
    "id", "ids", "credentials", "credential", "serviceaccount",
    "serviceaccountfile", "googleserviceaccountfile", "resourcekey",
    "resourcekeys", "providerresourceid", "webviewlink", "webcontentlink",
    "exportlinks", "downloadlink", "privatekey", "privatekeyid", "clientemail",
    "tokenuri", "accesstoken", "refreshtoken", "authorization", "cookie",
    "setcookie", "password", "secret", "token", "wpapppassword",
    "wcconsumerkey", "wcconsumersecret",
})
_RAW_ID_SUFFIXES = (
    "fileid", "fileids", "folderid", "folderids", "driveid", "driveids",
    "spreadsheetid", "spreadsheetids", "sheetid", "sheetids",
)


class ImageAssetTypeDryRunInputError(ValueError):
    """Fixed validation codes only; no input paths or provider values."""


def _assert_safe_metadata(value: object) -> None:
    """Validate even unused fields; fingerprints are safe but never projected."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ImageAssetTypeDryRunInputError("invalid_manifest_key")
            canonical = unicodedata.normalize("NFKC", key)
            compact = re.sub(r"[^a-z0-9]", "", canonical.casefold())
            if (
                compact in _FORBIDDEN_KEYS
                or compact.endswith(_RAW_ID_SUFFIXES)
                or canonical not in sanitize_report_data({canonical: None}, Redactor())
            ):
                raise ImageAssetTypeDryRunInputError("unsafe_manifest_field")
            _assert_safe_metadata(key)
            _assert_safe_metadata(item)
    elif isinstance(value, list):
        for item in value:
            _assert_safe_metadata(item)
    elif isinstance(value, str):
        canonical = unicodedata.normalize("NFKC", value)
        if (
            _UNSAFE_TEXT_PATTERN.search(canonical)
            or _SECRET_PATTERN.search(canonical)
            or Redactor().text(canonical, limit=len(canonical) + 1) != canonical
            or any(unicodedata.category(char) == "Cc" and not char.isspace() for char in canonical)
        ):
            raise ImageAssetTypeDryRunInputError("unsafe_manifest_text")
    elif value is not None and type(value) not in {bool, int, float}:
        raise ImageAssetTypeDryRunInputError("invalid_manifest_value")


def _object_list(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ImageAssetTypeDryRunInputError(f"invalid_{field}")
    return value


def _manifest_results(report: object, kind: str) -> list[Mapping[str, object]]:
    if not isinstance(report, Mapping) or report.get("status") != "ok":
        raise ImageAssetTypeDryRunInputError(f"{kind}_manifest_status_not_ok")
    _assert_safe_metadata(report)
    return _object_list(report.get("results"), f"{kind}_manifest_results")


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ImageAssetTypeDryRunInputError(f"invalid_{field}")
    return value


def _issues(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ImageAssetTypeDryRunInputError("invalid_manifest_issues")
    return value


def _source_context(folder: Mapping[str, object], kind: str, depth: int) -> dict[str, object]:
    # Root reports currently omit depth and name. Do not recover them from IDs.
    supplied_depth = folder.get("depth", 0 if kind == "root" else None)
    if type(supplied_depth) is not int or supplied_depth != depth:
        raise ImageAssetTypeDryRunInputError(f"invalid_{kind}_manifest_depth")
    name_key = "depth2_safe_folder_name" if kind == "depth2" else "safe_folder_name"
    name = _text(folder.get(name_key), "safe_folder_name", optional=kind == "root")
    parent = None
    if kind == "depth2":
        parent = _text(folder.get("depth1_safe_folder_name"), "parent_safe_folder_name")
    elif kind == "nested":
        parent = _text(folder.get("parent_safe_folder_name"), "parent_safe_folder_name", optional=True)
    source = folder.get("product_source")
    if source is not None:
        if not isinstance(source, Mapping):
            raise ImageAssetTypeDryRunInputError("invalid_product_source")
        start, end = source.get("start_row"), source.get("end_row")
        if type(start) is not int or type(end) is not int or start <= 0 or end < start:
            raise ImageAssetTypeDryRunInputError("invalid_product_source")
        source = {"start_row": start, "end_row": end}
    return {
        "sku": _text(folder.get("sku"), "sku"),
        "product_source": source,
        "source_manifest_kind": kind,
        "depth": depth,
        "safe_folder_name": name,
        "parent_safe_folder_name": parent,
    }


def _classify_file(
    item: Mapping[str, object], context: Mapping[str, object],
    folder_warnings: list[str], folder_blockers: list[str],
) -> dict[str, object]:
    # No MIME/name heuristics here: the versioned Core is the only classifier.
    classification = image_asset_type_policy.classify_image_asset_type(
        item.get("mime_type"), _text(item.get("safe_name"), "safe_name"),
        size_bytes=item.get("size_bytes"), image_width=item.get("image_width"),
        image_height=item.get("image_height"), sku=context["sku"],
    )
    projected = classification.to_dict()
    raw_mime = item.get("mime_type")
    return {
        **context,
        **{key: projected[key] for key in _POLICY_REPORT_FIELDS},
        "mime_type": raw_mime if isinstance(raw_mime, str) else None,
        "warnings": sorted(set((*classification.warnings, *folder_warnings, *_issues(item.get("warnings", []))))),
        "blocking_issues": sorted(set((*classification.blocking_issues, *folder_blockers, *_issues(item.get("blocking_issues", []))))),
    }


def build_image_asset_type_dry_run_report(
    root_manifest: Mapping[str, object],
    nested_manifest: Mapping[str, object],
    depth2_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Project actual file items from three successful safe local reports."""
    batches = (
        ("root", 0, _manifest_results(root_manifest, "root")),
        ("nested", 1, _manifest_results(nested_manifest, "nested")),
        ("depth2", 2, _manifest_results(depth2_manifest, "depth2")),
    )
    results = []
    seen = 0
    skipped = {key: 0 for key in _SKIPPED_KINDS.values()}
    for kind, depth, folders in batches:
        for folder in folders:
            context = _source_context(folder, kind, depth)
            warnings = _issues(folder.get("warnings", []))
            blockers = _issues(folder.get("blocking_issues", []))
            for item in _object_list(folder.get("items"), "manifest_items"):
                seen += 1
                item_kind = item.get("item_kind")
                if not isinstance(item_kind, str):
                    raise ImageAssetTypeDryRunInputError("invalid_manifest_item_kind")
                if item_kind in _SKIPPED_KINDS:
                    skipped[_SKIPPED_KINDS[item_kind]] += 1
                elif item_kind in _FILE_KINDS:
                    results.append(_classify_file(item, context, warnings, blockers))
                else:
                    raise ImageAssetTypeDryRunInputError("unsupported_manifest_item_kind")
    results.sort(key=lambda item: (
        item["sku"], item["depth"], item["safe_folder_name"] or "", item["safe_name"],
        item["normalized_mime_type"] or "", item["parent_safe_folder_name"] or "",
        json.dumps(item, ensure_ascii=False, sort_keys=True),
    ))
    eligible = sum(item["storefront_eligible"] for item in results)
    summary = {
        "total_manifest_items_seen": seen,
        "classified_assets": len(results),
        **skipped,
        **{asset.value: sum(item["asset_class"] == asset.value for item in results) for asset in image_asset_type_policy.AssetClass},
        "storefront_eligible_assets": eligible,
        "storefront_ineligible_assets": len(results) - eligible,
        "mime_classified": sum(item["classification_source"] == "mime" for item in results),
        "extension_fallback": sum(item["classification_source"] == "extension_fallback" for item in results),
        "mime_extension_mismatch": sum("asset_extension_mime_mismatch" in item["warnings"] for item in results),
        "assets_with_warnings": sum(bool(item["warnings"]) for item in results),
        "blocking_assets": sum(bool(item["blocking_issues"]) for item in results),
        "root_assets": sum(item["depth"] == 0 for item in results),
        "depth1_assets": sum(item["depth"] == 1 for item in results),
        "depth2_assets": sum(item["depth"] == 2 for item in results),
        "network_requests_performed": 0,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    report = {
        "status": "partial" if summary["blocking_assets"] else "ok",
        "policy_version": image_asset_type_policy.POLICY_VERSION,
        "summary": summary, "results": results,
        "network_requests_performed": 0,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    _assert_safe_metadata(report)
    return report


def _local_path(path: Path, *, require_json: bool = True) -> Path:
    # No URI/UNC or linked input/output, even through a parent junction. Do not
    # resolve a path into a remote filesystem or open a linked credential file.
    path = Path(path)
    text = str(path).replace("\\", "/")
    if text.startswith("//") or re.match(r"^[a-z][a-z0-9+.-]+:", text, re.IGNORECASE):
        raise ImageAssetTypeDryRunInputError("local_manifest_path_required")
    if require_json and path.suffix.casefold() != ".json":
        raise ImageAssetTypeDryRunInputError("json_manifest_path_required")
    try:
        absolute = Path(os.path.abspath(path))
        if str(absolute).replace("\\", "/").startswith("//"):
            raise ImageAssetTypeDryRunInputError("local_manifest_path_required")
        for component in reversed((absolute, *absolute.parents)):
            try:
                info = component.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ImageAssetTypeDryRunInputError("manifest_link_path_not_allowed")
    except ImageAssetTypeDryRunInputError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise ImageAssetTypeDryRunInputError("local_manifest_path_required") from None
    return absolute


def run_image_asset_type_dry_run(
    root_manifest_path: Path, nested_manifest_path: Path, depth2_manifest_path: Path,
    *, project_root: Path, redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Load exactly three local metadata reports and persist only the audit."""
    paths = tuple(_local_path(path) for path in (root_manifest_path, nested_manifest_path, depth2_manifest_path))
    output = _local_path(Path(project_root) / "reports" / REPORT_FILENAME)
    temporary = _local_path(output.with_name(output.name + ".tmp"), require_json=False)
    if output in paths or temporary in paths:
        raise ImageAssetTypeDryRunInputError("manifest_output_collision")
    try:
        manifests = tuple(load_local_json_report(path) for path in paths)
    except (OSError, ValueError, RecursionError):
        raise ImageAssetTypeDryRunInputError("local_manifest_read_failed") from None
    report = build_image_asset_type_dry_run_report(*manifests)
    active_redactor = redactor or Redactor()
    report = sanitize_report_data(report, active_redactor)
    _assert_safe_metadata(report)
    try:
        SafeJsonReportWriter(output, active_redactor).write(report)
    except OSError:
        raise ImageAssetTypeDryRunInputError("asset_type_report_write_failed") from None
    return report, output
