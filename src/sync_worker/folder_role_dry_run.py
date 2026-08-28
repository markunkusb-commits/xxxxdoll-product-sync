"""Local safe-manifest adapter for the existing Folder Role Policy.

No provider handles, configuration, clients, traversal or image selection enter
this workflow. Manifest fingerprints and file metadata never enter the policy.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from . import folder_role_policy
from .image_mapping import ProductSourceRange
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN_TEXT, Redactor


REPORT_FILENAME = "folder-role-dry-run.json"
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


class FolderRoleDryRunInputError(ValueError):
    """Fixed error codes, never input names, paths or provider values."""


def _assert_safe_input(value: object) -> None:
    """Reject unsafe input before projection, including unused metadata.

    Fingerprints may exist in safe manifests but are never restored as IDs or
    used to classify. Unknown safe metadata is ignored, not copied to output.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FolderRoleDryRunInputError("invalid_manifest_key")
            canonical = unicodedata.normalize("NFKC", key)
            compact = re.sub(r"[^a-z0-9]", "", canonical.casefold())
            if (
                compact in _FORBIDDEN_KEYS
                or compact.endswith(_RAW_ID_SUFFIXES)
                or canonical not in sanitize_report_data({canonical: None}, Redactor())
            ):
                raise FolderRoleDryRunInputError("unsafe_manifest_field")
            _assert_safe_input(key)
            _assert_safe_input(item)
    elif isinstance(value, list):
        for item in value:
            _assert_safe_input(item)
    elif isinstance(value, str):
        canonical = unicodedata.normalize("NFKC", value)
        if (
            _UNSAFE_TEXT_PATTERN.search(canonical)
            or _SECRET_PATTERN.search(canonical)
            or Redactor().text(canonical, limit=len(canonical) + 1) != canonical
            or any(unicodedata.category(char) == "Cc" and not char.isspace() for char in canonical)
        ):
            raise FolderRoleDryRunInputError("unsafe_manifest_text")
    elif value is not None and type(value) not in {bool, int, float}:
        raise FolderRoleDryRunInputError("invalid_manifest_value")


def _manifest_results(report: object, kind: str) -> list[Mapping[str, object]]:
    if not isinstance(report, Mapping) or report.get("status") != "ok":
        raise FolderRoleDryRunInputError(f"{kind}_manifest_status_not_ok")
    _assert_safe_input(report)
    results = report.get("results")
    if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
        raise FolderRoleDryRunInputError(f"invalid_{kind}_manifest_results")
    return results


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FolderRoleDryRunInputError(f"invalid_{field}")
    return value


def _issues(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FolderRoleDryRunInputError("invalid_manifest_issues")
    return value


def _classify_result(item: Mapping[str, object], kind: str) -> dict[str, object]:
    depth = 1 if kind == "nested" else 2
    if type(item.get("depth")) is not int or item["depth"] != depth:
        raise FolderRoleDryRunInputError(f"invalid_{kind}_manifest_depth")
    count = item.get("nested_folder_at_depth_limit_count")
    if type(count) is not int or count < 0:
        raise FolderRoleDryRunInputError("invalid_depth_limit_count")
    source = item.get("product_source")
    if not isinstance(source, Mapping):
        raise FolderRoleDryRunInputError("invalid_product_source")
    start, end = source.get("start_row"), source.get("end_row")
    if type(start) is not int or type(end) is not int or start <= 0 or end < start:
        raise FolderRoleDryRunInputError("invalid_product_source")
    name = _text(item.get("safe_folder_name" if depth == 1 else "depth2_safe_folder_name"), "safe_folder_name")
    parent = None if depth == 1 else _text(item.get("depth1_safe_folder_name"), "parent_safe_folder_name")
    sku = _text(item.get("sku"), "sku")
    warnings = _issues(item.get("warnings", []))
    blockers = _issues(item.get("blocking_issues", []))
    classification = folder_role_policy.classify_folder_role(
        name, parent_safe_folder_name=parent, depth=depth, sku=sku,
        product_source=ProductSourceRange(start, end),
        has_depth_limit_children=count > 0,
    )
    # Keep Core decisions intact. Upstream issues remain audit-only annotations.
    return {
        **classification.to_dict(),
        "safe_folder_name": name,
        "source_manifest_kind": kind,
        "warnings": sorted(set((*classification.warnings, *warnings))),
        "blocking_issues": sorted(set((*classification.blocking_issues, *blockers))),
    }


def build_folder_role_dry_run_report(
    nested_manifest: Mapping[str, object],
    depth2_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Classify safe depth-one/two results with the single versioned Core."""
    nested = _manifest_results(nested_manifest, "nested")
    depth2 = _manifest_results(depth2_manifest, "depth2")
    results = [
        *(_classify_result(item, "nested") for item in nested),
        *(_classify_result(item, "depth2") for item in depth2),
    ]
    results.sort(key=lambda item: (
        item["sku"], item["depth"], item["normalized_folder_name"], item["safe_folder_name"],
        item["parent_safe_folder_name"] or "", item["product_source"]["start_row"],
        item["product_source"]["end_row"], json.dumps(item, ensure_ascii=False, sort_keys=True),
    ))
    summary = {
        "total_folders": len(results),
        "depth1_folders": len(nested),
        "depth2_folders": len(depth2),
        **{role.value: sum(item["role"] == role.value for item in results) for role in folder_role_policy.FolderRole},
        "gallery_eligible_folders": sum(item["gallery_eligible"] for item in results),
        "requires_deeper_inventory_folders": sum(item["requires_deeper_inventory"] for item in results),
        "folders_with_warnings": sum(bool(item["warnings"]) for item in results),
        "blocking_folders": sum(bool(item["blocking_issues"]) for item in results),
        "network_requests_performed": 0,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    report = {
        "status": "partial" if summary["blocking_folders"] else "ok",
        "policy_version": folder_role_policy.POLICY_VERSION,
        "summary": summary,
        "results": results,
        "network_requests_performed": 0,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    _assert_safe_input(report)
    return report


def _local_path(path: Path, *, require_json: bool = True) -> Path:
    # Check before filesystem access. Do not resolve/follow links: a seemingly
    # local file or parent junction could point to credentials or a UNC share.
    path = Path(path)
    text = str(path).replace("\\", "/")
    if text.startswith("//") or re.match(r"^[a-z][a-z0-9+.-]+:", text, re.IGNORECASE):
        raise FolderRoleDryRunInputError("local_manifest_path_required")
    if require_json and path.suffix.casefold() != ".json":
        raise FolderRoleDryRunInputError("json_manifest_path_required")
    try:
        absolute = Path(os.path.abspath(path))
        if str(absolute).replace("\\", "/").startswith("//"):
            raise FolderRoleDryRunInputError("local_manifest_path_required")
        # Inspect parents first without following reparse points at any level.
        for component in reversed((absolute, *absolute.parents)):
            try:
                info = component.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise FolderRoleDryRunInputError("manifest_link_path_not_allowed")
    except FolderRoleDryRunInputError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise FolderRoleDryRunInputError("local_manifest_path_required") from None
    return absolute


def run_folder_role_dry_run(
    nested_manifest_path: Path,
    depth2_manifest_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read exactly two local safe reports and atomically persist the audit."""
    nested_path = _local_path(nested_manifest_path)
    depth2_path = _local_path(depth2_manifest_path)
    report_path = _local_path(Path(project_root) / "reports" / REPORT_FILENAME)
    temporary_path = _local_path(report_path.with_name(report_path.name + ".tmp"), require_json=False)
    if report_path in {nested_path, depth2_path} or temporary_path in {nested_path, depth2_path}:
        raise FolderRoleDryRunInputError("manifest_output_collision")
    try:
        nested = load_local_json_report(nested_path)
        depth2 = load_local_json_report(depth2_path)
    except (OSError, ValueError, RecursionError):
        raise FolderRoleDryRunInputError("local_manifest_read_failed") from None
    report = build_folder_role_dry_run_report(nested, depth2)
    active_redactor = redactor or Redactor()
    report = sanitize_report_data(report, active_redactor)
    _assert_safe_input(report)
    try:
        SafeJsonReportWriter(report_path, active_redactor).write(report)
    except OSError:
        raise FolderRoleDryRunInputError("folder_role_report_write_failed") from None
    return report, report_path
