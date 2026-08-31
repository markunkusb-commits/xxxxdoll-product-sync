"""Freeze an approved image selection against historical safe manifests.

This adapter is deliberately local-only.  It restores the formal Selection
and Drive manifest domain objects, delegates identity validation to the
existing secure-selected-media Baseline Core, and writes one non-authorizing
snapshot exactly once.  It never creates a provider client or opens media.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import (
    folder_role_policy,
    google_drive_depth2_folder_manifest as depth2_core,
    google_drive_folder_manifest as root_core,
    google_drive_nested_folder_manifest as nested_core,
    image_selection_policy,
    secure_selected_media_handle as baseline_core,
)
from .image_asset_type_dry_run import (
    ImageAssetTypeDryRunInputError,
    _assert_safe_metadata,
    _local_path,
)
from .image_mapping import ProductSourceRange
from .report import sanitize_report_data
from .sanitization import Redactor


SNAPSHOT_VERSION = "xxxxdoll-selected-media-baseline-snapshot-v1"
REPORT_FILENAME = "selected-media-baseline-snapshot.json"
MAX_INPUT_BYTES = 64 * 1024 * 1024

_SELECTION_COUNTERS = (
    "network_requests_performed",
    "download_requests_performed",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "write_requests_performed",
)
_OUTPUT_COUNTERS = (
    "network_requests_performed",
    "drive_read_requests_performed",
    "download_requests_performed",
    "media_read_requests_performed",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "write_requests_performed",
)
_EXPECTED_INPUT_NAMES = (
    "image-selection-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
)
_SELECTION_REPORT_FIELDS = frozenset({
    "status", "policy_version", "source_quality_policy_version", "summary",
    "results", *_SELECTION_COUNTERS,
})
_SELECTION_BATCH_FIELDS = frozenset({
    "sku", "total_candidates", "quality_candidates", "storefront_candidates",
    "factory_candidates", "selected_count", "selected_storefront",
    "selected_factory", "primary_count", "gallery_count", "warnings",
    "blocking_issues", "items",
})
_SELECTION_ITEM_FIELDS = frozenset({
    "sku", "product_source", "source_manifest_kind", "depth",
    "safe_folder_name", "parent_safe_folder_name", "safe_name", "folder_role",
    "image_width", "image_height", "short_edge", "long_edge", "pixel_count",
    "megapixels", "size_bytes", "orientation", "quality_reason",
    "min_short_edge_px", "min_megapixels", "quality_policy_version",
    "quality_eligible", "selected", "selection_position", "image_role",
    "selection_reason", "selection_policy_version",
    "requires_deeper_inventory", "warnings", "blocking_issues",
})
_HISTORICAL_COMMON_REPORT_FIELDS = frozenset({
    "status", "inputs", "summary", "results", "root_issues", "warnings",
    "blocking_issues", "write_requests_performed",
})
_NESTED_RESULT_FIELDS = frozenset({
    "sku", "product_source", "root_folder_id_fingerprint",
    "nested_folder_id_fingerprint", "safe_folder_name", "depth", "status",
    "item_count", "image_candidate_count", "nested_folder_at_depth_limit_count",
    "shortcut_count", "google_workspace_file_count", "other_file_count",
    "duplicate_name_candidate_count", "duplicate_content_candidate_count",
    "pages_read", "items", "warnings", "blocking_issues",
})
_DEPTH2_RESULT_FIELDS = frozenset({
    "sku", "product_source", "root_folder_id_fingerprint",
    "depth1_folder_id_fingerprint", "depth2_folder_id_fingerprint",
    "depth1_safe_folder_name", "depth2_safe_folder_name", "depth", "status",
    "item_count", "image_candidate_count", "nested_folder_at_depth_limit_count",
    "shortcut_count", "google_workspace_file_count", "other_file_count",
    "duplicate_name_candidate_count", "duplicate_content_candidate_count",
    "pages_read", "items", "warnings", "blocking_issues",
})
_HISTORICAL_ITEM_REQUIRED_FIELDS = frozenset({
    "safe_name", "mime_type", "size_bytes", "modified_time",
    "provider_content_checksum", "file_id_fingerprint", "item_kind",
    "image_candidate", "image_width", "image_height", "warnings",
})
_HISTORICAL_ITEM_OPTIONAL_FIELDS = frozenset({"image_candidate_status"})
_ISSUE = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_SHA256 = re.compile(r"[a-f0-9]{64}", re.ASCII)
_FORBIDDEN_OUTPUT_KEYS = frozenset({
    "provider_file_id", "raw_file_id", "raw_folder_id", "raw_nested_folder_id",
    "raw_depth2_folder_id", "provider_resource_id", "resource_key", "url",
    "download_url", "download_link", "local_path", "absolute_path",
    "authorization", "cookie", "token", "client_secret", "credentials",
    "download_ready", "wordpress_upload_ready",
})


class SelectedMediaBaselineSnapshotError(ValueError):
    """Fixed, provider-data-free validation and orchestration error codes."""


def _mapping(value: object, fields: frozenset[str], code: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise SelectedMediaBaselineSnapshotError(code)
    return value


def _list(value: object, code: str) -> list[object]:
    if not isinstance(value, list):
        raise SelectedMediaBaselineSnapshotError(code)
    return value


def _counter(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise SelectedMediaBaselineSnapshotError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    result = _counter(value, code)
    if result == 0:
        raise SelectedMediaBaselineSnapshotError(code)
    return result


def _optional_nonnegative_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    return _counter(value, code)


def _optional_positive_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, code)


def _text(value: object, code: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 500:
        raise SelectedMediaBaselineSnapshotError(code)
    try:
        _assert_safe_metadata(value)
    except (ImageAssetTypeDryRunInputError, RecursionError):
        raise SelectedMediaBaselineSnapshotError(code) from None
    return value


def _issues(value: object, code: str) -> tuple[str, ...]:
    items = _list(value, code)
    if any(type(item) is not str or _ISSUE.fullmatch(item) is None for item in items):
        raise SelectedMediaBaselineSnapshotError(code)
    return tuple(items)


def _source(value: object) -> ProductSourceRange:
    source = _mapping(
        value, frozenset({"start_row", "end_row"}), "invalid_product_source"
    )
    start = _positive_int(source["start_row"], "invalid_product_source")
    end = _positive_int(source["end_row"], "invalid_product_source")
    if end < start:
        raise SelectedMediaBaselineSnapshotError("invalid_product_source")
    return ProductSourceRange(start, end)


def _sha256(value: object, code: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise SelectedMediaBaselineSnapshotError(code)
    return value


def _restore_selection_item(value: object) -> image_selection_policy.ImageSelectionItem:
    item = _mapping(value, _SELECTION_ITEM_FIELDS, "invalid_selection_item")
    if item["selection_policy_version"] != image_selection_policy.POLICY_VERSION:
        raise SelectedMediaBaselineSnapshotError("selection_policy_version_mismatch")
    kind = item["source_manifest_kind"]
    depth = item["depth"]
    if type(kind) is not str or kind not in {"root", "nested", "depth2"}:
        raise SelectedMediaBaselineSnapshotError("invalid_selection_hierarchy")
    if type(depth) is not int or depth != {"root": 0, "nested": 1, "depth2": 2}[kind]:
        raise SelectedMediaBaselineSnapshotError("invalid_selection_hierarchy")
    folder = _text(item["safe_folder_name"], "invalid_safe_folder_name", optional=kind == "root")
    parent = _text(
        item["parent_safe_folder_name"], "invalid_parent_safe_folder_name",
        optional=kind != "depth2",
    )
    if (
        (kind == "root" and (folder is not None or parent is not None))
        or (kind == "nested" and (folder is None or parent is not None))
        or (kind == "depth2" and (folder is None or parent is None))
    ):
        raise SelectedMediaBaselineSnapshotError("invalid_selection_hierarchy")
    try:
        folder_role = folder_role_policy.FolderRole(item["folder_role"])
        image_role = image_selection_policy.ImageSelectionRole(item["image_role"])
        selection_reason = image_selection_policy.ImageSelectionReason(item["selection_reason"])
    except (TypeError, ValueError):
        raise SelectedMediaBaselineSnapshotError("invalid_selection_item") from None
    if type(item["selected"]) is not bool or type(item["quality_eligible"]) is not bool:
        raise SelectedMediaBaselineSnapshotError("invalid_selection_item")
    if type(item["requires_deeper_inventory"]) is not bool:
        raise SelectedMediaBaselineSnapshotError("invalid_selection_item")
    position = item["selection_position"]
    if position is not None and (type(position) is not int or position < 0):
        raise SelectedMediaBaselineSnapshotError("invalid_selection_item")
    restored = image_selection_policy.ImageSelectionItem(
        sku=_text(item["sku"], "invalid_selection_sku"),
        folder_role=folder_role,
        safe_name=_text(item["safe_name"], "invalid_selection_safe_name"),
        source_manifest_kind=kind,
        depth=depth,
        safe_folder_name=folder,
        parent_safe_folder_name=parent,
        product_source=_source(item["product_source"]),
        requires_deeper_inventory=item["requires_deeper_inventory"],
        quality_eligible=item["quality_eligible"],
        selected=item["selected"],
        selection_position=position,
        image_role=image_role,
        selection_reason=selection_reason,
        warnings=_issues(item["warnings"], "invalid_selection_issues"),
        blocking_issues=_issues(item["blocking_issues"], "invalid_selection_issues"),
    )
    if restored.blocking_issues:
        raise SelectedMediaBaselineSnapshotError("selection_report_contains_blockers")
    if restored.selected:
        try:
            baseline_core._validate_selection_item(restored)
        except baseline_core.SecureSelectedMediaHandleError:
            raise SelectedMediaBaselineSnapshotError("invalid_selected_item") from None
    elif restored.selection_position is not None or restored.image_role is not image_selection_policy.ImageSelectionRole.NOT_SELECTED:
        raise SelectedMediaBaselineSnapshotError("invalid_not_selected_item")
    return restored


def _restore_selection_report(value: object) -> tuple[image_selection_policy.ImageSelectionItem, ...]:
    report = _mapping(value, _SELECTION_REPORT_FIELDS, "invalid_selection_report")
    try:
        _assert_safe_metadata(report)
    except (ImageAssetTypeDryRunInputError, RecursionError):
        raise SelectedMediaBaselineSnapshotError("unsafe_selection_report") from None
    if report["status"] != "ok":
        raise SelectedMediaBaselineSnapshotError("selection_report_status_not_ok")
    if report["policy_version"] != image_selection_policy.POLICY_VERSION:
        raise SelectedMediaBaselineSnapshotError("selection_policy_version_mismatch")
    summary = report["summary"]
    if not isinstance(summary, Mapping) or any(type(key) is not str for key in summary):
        raise SelectedMediaBaselineSnapshotError("invalid_selection_summary")
    for counter in _SELECTION_COUNTERS:
        if _counter(report[counter], "invalid_selection_counter") != 0:
            raise SelectedMediaBaselineSnapshotError("selection_report_not_offline")
        if _counter(summary.get(counter), "invalid_selection_summary") != 0:
            raise SelectedMediaBaselineSnapshotError("selection_report_not_offline")
    if _counter(summary.get("blocking_assets"), "invalid_selection_summary") != 0:
        raise SelectedMediaBaselineSnapshotError("selection_report_contains_blockers")
    batches = _list(report["results"], "invalid_selection_results")
    restored: list[image_selection_policy.ImageSelectionItem] = []
    seen_skus: set[str] = set()
    for raw_batch in batches:
        batch = _mapping(raw_batch, _SELECTION_BATCH_FIELDS, "invalid_selection_batch")
        sku = _text(batch["sku"], "invalid_selection_sku")
        if sku in seen_skus:
            raise SelectedMediaBaselineSnapshotError("duplicate_selection_sku_batch")
        seen_skus.add(sku)
        if _issues(batch["blocking_issues"], "invalid_selection_issues"):
            raise SelectedMediaBaselineSnapshotError("selection_report_contains_blockers")
        _issues(batch["warnings"], "invalid_selection_issues")
        items = [_restore_selection_item(item) for item in _list(batch["items"], "invalid_selection_items")]
        if any(item.sku != sku for item in items):
            raise SelectedMediaBaselineSnapshotError("selection_batch_sku_mismatch")
        selected = [item for item in items if item.selected]
        positions = sorted(item.selection_position for item in selected)
        expected_counts = {
            "total_candidates": len(items),
            "selected_count": len(selected),
            "selected_storefront": sum(item.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS for item in selected),
            "selected_factory": sum(item.folder_role is folder_role_policy.FolderRole.FACTORY_PHOTOS for item in selected),
            "primary_count": sum(item.image_role is image_selection_policy.ImageSelectionRole.PRIMARY for item in selected),
            "gallery_count": sum(item.image_role is image_selection_policy.ImageSelectionRole.GALLERY for item in selected),
        }
        if positions != list(range(len(selected))):
            raise SelectedMediaBaselineSnapshotError("invalid_selection_positions")
        if any(_counter(batch[key], "invalid_selection_batch_count") != expected for key, expected in expected_counts.items()):
            raise SelectedMediaBaselineSnapshotError("selection_batch_count_mismatch")
        for key in ("quality_candidates", "storefront_candidates", "factory_candidates"):
            _counter(batch[key], "invalid_selection_batch_count")
        restored.extend(selected)
    selected_total = _counter(summary.get("selected_total"), "invalid_selection_summary")
    if selected_total != len(restored) or _counter(summary.get("total_skus"), "invalid_selection_summary") != len(batches):
        raise SelectedMediaBaselineSnapshotError("selection_summary_mismatch")
    return tuple(restored)


def _restore_historical_item(value: object) -> root_core.DriveManifestItem:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise SelectedMediaBaselineSnapshotError("invalid_historical_item")
    fields = set(value)
    if not _HISTORICAL_ITEM_REQUIRED_FIELDS <= fields or fields - _HISTORICAL_ITEM_REQUIRED_FIELDS - _HISTORICAL_ITEM_OPTIONAL_FIELDS:
        raise SelectedMediaBaselineSnapshotError("invalid_historical_item")
    name = _text(value["safe_name"], "invalid_historical_safe_name")
    mime = _text(value["mime_type"], "invalid_historical_mime_type")
    if root_core._safe_name(name)[0] != name or root_core._MIME_TYPE_PATTERN.fullmatch(mime) is None:
        raise SelectedMediaBaselineSnapshotError("invalid_historical_item")
    modified = value["modified_time"]
    if modified is not None and (
        type(modified) is not str or root_core._MODIFIED_TIME_PATTERN.fullmatch(modified) is None
    ):
        raise SelectedMediaBaselineSnapshotError("invalid_historical_modified_time")
    kind = value["item_kind"]
    if kind not in {"image_candidate", "nested_folder", "shortcut", "google_workspace_file", "other_file"}:
        raise SelectedMediaBaselineSnapshotError("invalid_historical_item_kind")
    if type(value["image_candidate"]) is not bool:
        raise SelectedMediaBaselineSnapshotError("invalid_historical_image_candidate")
    status = value.get("image_candidate_status")
    if status is not None and type(status) is not str:
        raise SelectedMediaBaselineSnapshotError("invalid_historical_image_candidate")
    return root_core.DriveManifestItem(
        safe_name=name,
        mime_type=mime,
        size_bytes=_optional_nonnegative_int(value["size_bytes"], "invalid_historical_size"),
        modified_time=modified,
        md5_checksum=value["provider_content_checksum"],
        file_id_fingerprint=value["file_id_fingerprint"],
        item_kind=kind,
        image_candidate=value["image_candidate"],
        image_candidate_status=status or ("drive_metadata_image_candidate" if value["image_candidate"] else None),
        image_width=_optional_positive_int(value["image_width"], "invalid_historical_dimensions"),
        image_height=_optional_positive_int(value["image_height"], "invalid_historical_dimensions"),
        image_rotation=None,
        warnings=_issues(value["warnings"], "invalid_historical_issues"),
        provider_file_id=None,
    )


def _validate_result_counts(result: Mapping[str, object], items: Sequence[root_core.DriveManifestItem]) -> None:
    expected = {
        "item_count": len(items),
        "image_candidate_count": sum(item.image_candidate for item in items),
        "nested_folder_at_depth_limit_count": sum(item.item_kind == "nested_folder" for item in items),
        "shortcut_count": sum(item.item_kind == "shortcut" for item in items),
        "google_workspace_file_count": sum(item.item_kind == "google_workspace_file" for item in items),
        "other_file_count": sum(item.item_kind == "other_file" for item in items),
        "duplicate_name_candidate_count": sum("duplicate_name_candidate" in item.warnings for item in items),
        "duplicate_content_candidate_count": sum("duplicate_content_candidate" in item.warnings for item in items),
    }
    if any(_counter(result[key], "invalid_historical_result_count") != expected_value for key, expected_value in expected.items()):
        raise SelectedMediaBaselineSnapshotError("historical_result_count_mismatch")


def _restore_nested_result(value: object) -> nested_core.GoogleDriveNestedFolderManifest:
    result = _mapping(value, _NESTED_RESULT_FIELDS, "invalid_nested_baseline_result")
    items = tuple(_restore_historical_item(item) for item in _list(result["items"], "invalid_historical_items"))
    _validate_result_counts(result, items)
    return nested_core.GoogleDriveNestedFolderManifest(
        sku=_text(result["sku"], "invalid_historical_sku"),
        product_source=_source(result["product_source"]),
        root_folder_id_fingerprint=_sha256(result["root_folder_id_fingerprint"], "invalid_historical_folder_fingerprint"),
        nested_folder_id_fingerprint=_sha256(result["nested_folder_id_fingerprint"], "invalid_historical_folder_fingerprint"),
        safe_folder_name=_text(result["safe_folder_name"], "invalid_historical_folder_name"),
        depth=result["depth"],
        status=result["status"],
        items=items,
        pages_read=_counter(result["pages_read"], "invalid_historical_pages"),
        warnings=_issues(result["warnings"], "invalid_historical_issues"),
        blocking_issues=_issues(result["blocking_issues"], "invalid_historical_issues"),
    )


def _restore_depth2_result(value: object) -> depth2_core.GoogleDriveDepth2FolderManifest:
    result = _mapping(value, _DEPTH2_RESULT_FIELDS, "invalid_depth2_baseline_result")
    items = tuple(_restore_historical_item(item) for item in _list(result["items"], "invalid_historical_items"))
    _validate_result_counts(result, items)
    return depth2_core.GoogleDriveDepth2FolderManifest(
        sku=_text(result["sku"], "invalid_historical_sku"),
        product_source=_source(result["product_source"]),
        root_folder_id_fingerprint=_sha256(result["root_folder_id_fingerprint"], "invalid_historical_folder_fingerprint"),
        depth1_folder_id_fingerprint=_sha256(result["depth1_folder_id_fingerprint"], "invalid_historical_folder_fingerprint"),
        depth2_folder_id_fingerprint=_sha256(result["depth2_folder_id_fingerprint"], "invalid_historical_folder_fingerprint"),
        depth1_safe_folder_name=_text(result["depth1_safe_folder_name"], "invalid_historical_folder_name"),
        depth2_safe_folder_name=_text(result["depth2_safe_folder_name"], "invalid_historical_folder_name"),
        depth=result["depth"],
        status=result["status"],
        items=items,
        pages_read=_counter(result["pages_read"], "invalid_historical_pages"),
        warnings=_issues(result["warnings"], "invalid_historical_issues"),
        blocking_issues=_issues(result["blocking_issues"], "invalid_historical_issues"),
    )


def _restore_historical_report(value: object, kind: str) -> tuple[object, ...]:
    expected = (
        _HISTORICAL_COMMON_REPORT_FIELDS
        if kind == "nested"
        else _HISTORICAL_COMMON_REPORT_FIELDS | {"depth1_issues"}
    )
    report = _mapping(value, frozenset(expected), f"invalid_{kind}_baseline_report")
    try:
        _assert_safe_metadata(report)
    except (ImageAssetTypeDryRunInputError, RecursionError):
        raise SelectedMediaBaselineSnapshotError(f"unsafe_{kind}_baseline_report") from None
    if "manifests" in report:
        raise SelectedMediaBaselineSnapshotError("historical_results_required")
    if report["status"] != "ok":
        raise SelectedMediaBaselineSnapshotError(f"{kind}_baseline_status_not_ok")
    if _issues(report["blocking_issues"], "invalid_historical_issues"):
        raise SelectedMediaBaselineSnapshotError(f"{kind}_baseline_contains_blockers")
    _issues(report["warnings"], "invalid_historical_issues")
    if _counter(report["write_requests_performed"], "invalid_historical_counter") != 0:
        raise SelectedMediaBaselineSnapshotError("historical_report_contains_writes")
    summary = report["summary"]
    if not isinstance(summary, Mapping) or any(type(key) is not str for key in summary):
        raise SelectedMediaBaselineSnapshotError("invalid_historical_summary")
    for key in ("download_requests_performed", "write_requests_performed"):
        if key in summary and _counter(summary[key], "invalid_historical_counter") != 0:
            raise SelectedMediaBaselineSnapshotError("historical_report_contains_writes")
    results = _list(report["results"], f"invalid_{kind}_baseline_results")
    restore = _restore_nested_result if kind == "nested" else _restore_depth2_result
    manifests = tuple(restore(result) for result in results)
    for manifest in manifests:
        if manifest.depth != (1 if kind == "nested" else 2):
            raise SelectedMediaBaselineSnapshotError(f"invalid_{kind}_baseline_depth")
        if manifest.blocking_issues:
            raise SelectedMediaBaselineSnapshotError(f"{kind}_baseline_contains_blockers")
    return manifests


def _join_matches(
    selection: image_selection_policy.ImageSelectionItem,
    manifests: Sequence[object],
) -> list[object]:
    matches = []
    for manifest in manifests:
        if type(manifest) is nested_core.GoogleDriveNestedFolderManifest:
            provenance = (
                manifest.sku, manifest.product_source, "nested", manifest.depth,
                manifest.safe_folder_name, None,
            )
        elif type(manifest) is depth2_core.GoogleDriveDepth2FolderManifest:
            provenance = (
                manifest.sku, manifest.product_source, "depth2", manifest.depth,
                manifest.depth2_safe_folder_name, manifest.depth1_safe_folder_name,
            )
        else:
            continue
        expected = (
            selection.sku, selection.product_source,
            selection.source_manifest_kind, selection.depth,
            selection.safe_folder_name, selection.parent_safe_folder_name,
        )
        if provenance == expected:
            matches.extend(manifest for item in manifest.items if item.safe_name == selection.safe_name)
    return matches


def _validate_selected_source_item(manifest: object, safe_name: str) -> None:
    item = next(item for item in manifest.items if item.safe_name == safe_name)
    if item.item_kind != "image_candidate" or item.image_candidate is not True:
        raise SelectedMediaBaselineSnapshotError("baseline_snapshot_source_not_image_candidate")
    fingerprint = item.file_id_fingerprint
    if fingerprint is None:
        raise SelectedMediaBaselineSnapshotError("baseline_snapshot_fingerprint_missing")
    if type(fingerprint) is not str or root_core._SHA256_PATTERN.fullmatch(fingerprint) is None:
        raise SelectedMediaBaselineSnapshotError("baseline_snapshot_fingerprint_invalid")
    checksum = item.md5_checksum
    if checksum is None:
        raise SelectedMediaBaselineSnapshotError("baseline_snapshot_checksum_missing")
    if type(checksum) is not str or root_core._MD5_PATTERN.fullmatch(checksum) is None:
        raise SelectedMediaBaselineSnapshotError("baseline_snapshot_checksum_invalid")


def _assert_snapshot_safe(report: Mapping[str, object]) -> None:
    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if type(key) is not str or key.casefold() in _FORBIDDEN_OUTPUT_KEYS:
                    raise SelectedMediaBaselineSnapshotError("unsafe_baseline_snapshot_output")
                walk(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                walk(item)
    walk(report)
    try:
        _assert_safe_metadata(report)
        root_core._assert_report_safe(report)
    except (ImageAssetTypeDryRunInputError, root_core.GoogleDriveFolderManifestError, RecursionError):
        raise SelectedMediaBaselineSnapshotError("unsafe_baseline_snapshot_output") from None


def build_selected_media_baseline_snapshot(
    selection_report: Mapping[str, object],
    nested_baseline_report: Mapping[str, object],
    depth2_baseline_report: Mapping[str, object],
    *,
    selection_report_sha256: str,
    nested_baseline_report_sha256: str,
    depth2_baseline_report_sha256: str,
) -> dict[str, object]:
    """Build one safe historical identity snapshot entirely in memory."""

    hashes = (
        _sha256(selection_report_sha256, "invalid_selection_report_sha256"),
        _sha256(nested_baseline_report_sha256, "invalid_nested_baseline_report_sha256"),
        _sha256(depth2_baseline_report_sha256, "invalid_depth2_baseline_report_sha256"),
    )
    selected = _restore_selection_report(selection_report)
    nested = _restore_historical_report(nested_baseline_report, "nested")
    depth2 = _restore_historical_report(depth2_baseline_report, "depth2")
    all_manifests = (*nested, *depth2)
    results: list[dict[str, object]] = []
    for item in selected:
        if item.source_manifest_kind not in {"nested", "depth2"}:
            raise SelectedMediaBaselineSnapshotError("baseline_snapshot_unsupported_manifest_kind")
        matches = _join_matches(item, all_manifests)
        if not matches:
            raise SelectedMediaBaselineSnapshotError("baseline_snapshot_source_missing")
        if len(matches) != 1:
            raise SelectedMediaBaselineSnapshotError("baseline_snapshot_source_ambiguous")
        manifest = matches[0]
        _validate_selected_source_item(manifest, item.safe_name)
        try:
            identity = baseline_core.create_selected_media_baseline_identity(item, manifest)
        except baseline_core.SecureSelectedMediaHandleError:
            raise SelectedMediaBaselineSnapshotError("baseline_snapshot_core_rejected") from None
        results.append({
            "selection_position": item.selection_position,
            "image_role": item.image_role.value,
            "folder_role": item.folder_role.value,
            "selection_reason": item.selection_reason.value,
            "baseline_identity": identity.to_safe_dict(),
        })
    results.sort(key=lambda result: (
        result["baseline_identity"]["sku"], result["selection_position"]
    ))
    nested_count = sum(
        result["baseline_identity"]["source_manifest_kind"] == "nested"
        for result in results
    )
    depth2_count = sum(
        result["baseline_identity"]["source_manifest_kind"] == "depth2"
        for result in results
    )
    summary = {
        "selected_items": len(selected),
        "baseline_created": len(results),
        "baseline_nested": nested_count,
        "baseline_depth2": depth2_count,
        "baseline_missing": 0,
        "baseline_ambiguous": 0,
        "missing_fingerprint": 0,
        "invalid_fingerprint": 0,
        "missing_checksum": 0,
        "invalid_checksum": 0,
        "jpeg_baselines": sum(
            result["baseline_identity"]["source_mime_type"] == "image/jpeg"
            for result in results
        ),
        "blocking_items": 0,
    }
    report = {
        "status": "ok",
        "snapshot_version": SNAPSHOT_VERSION,
        "source_selection_policy_version": image_selection_policy.POLICY_VERSION,
        "source_handle_policy_version": baseline_core.POLICY_VERSION,
        "selection_report_sha256": hashes[0],
        "nested_baseline_report_sha256": hashes[1],
        "depth2_baseline_report_sha256": hashes[2],
        "summary": summary,
        "results": results,
        **dict.fromkeys(_OUTPUT_COUNTERS, 0),
    }
    _assert_snapshot_safe(report)
    return report


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SelectedMediaBaselineSnapshotError("duplicate_json_key")
        result[key] = value
    return result


def _read_local_json(path: Path, expected_name: str) -> tuple[Mapping[str, object], str]:
    try:
        local = _local_path(path)
    except (ImageAssetTypeDryRunInputError, OSError, TypeError, ValueError):
        raise SelectedMediaBaselineSnapshotError("local_baseline_input_required") from None
    if local.name != expected_name:
        raise SelectedMediaBaselineSnapshotError("unexpected_baseline_input_file")
    try:
        size = local.stat().st_size
        if size <= 0 or size > MAX_INPUT_BYTES or not local.is_file():
            raise SelectedMediaBaselineSnapshotError("invalid_baseline_input_size")
        raw = local.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_no_duplicates
        )
    except SelectedMediaBaselineSnapshotError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise SelectedMediaBaselineSnapshotError("local_baseline_input_read_failed") from None
    if not isinstance(payload, Mapping):
        raise SelectedMediaBaselineSnapshotError("baseline_input_root_must_be_object")
    return payload, hashlib.sha256(raw).hexdigest()


def _safe_output(project_root: Path) -> Path:
    try:
        root = _local_path(Path(project_root), require_json=False)
    except (ImageAssetTypeDryRunInputError, OSError, TypeError, ValueError):
        raise SelectedMediaBaselineSnapshotError("local_project_root_required") from None
    output = Path(os.path.abspath(root / "reports" / REPORT_FILENAME))
    try:
        if os.path.commonpath((str(root), str(output))) != str(root):
            raise SelectedMediaBaselineSnapshotError("local_snapshot_output_required")
    except ValueError:
        raise SelectedMediaBaselineSnapshotError("local_snapshot_output_required") from None
    return output


def _write_snapshot_once(output: Path, report: Mapping[str, object]) -> None:
    sanitized = sanitize_report_data(report, Redactor())
    if not isinstance(sanitized, dict):
        raise SelectedMediaBaselineSnapshotError("unsafe_baseline_snapshot_output")
    sanitized["write_requests_performed"] = 0
    _assert_snapshot_safe(sanitized)
    created = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            json.dump(sanitized, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError:
        raise SelectedMediaBaselineSnapshotError(
            "selected_media_baseline_snapshot_already_exists"
        ) from None
    except OSError:
        if created:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        raise SelectedMediaBaselineSnapshotError("baseline_snapshot_write_failed") from None


def run_selected_media_baseline_snapshot(
    selection_report_path: Path,
    nested_baseline_path: Path,
    depth2_baseline_path: Path,
    *,
    project_root: Path,
) -> tuple[dict[str, object], Path]:
    """Read exactly three local historical inputs and create one snapshot."""

    output = _safe_output(project_root)
    if output.exists():
        raise SelectedMediaBaselineSnapshotError(
            "selected_media_baseline_snapshot_already_exists"
        )
    paths = (Path(selection_report_path), Path(nested_baseline_path), Path(depth2_baseline_path))
    try:
        normalized = tuple(_local_path(path) for path in paths)
    except (ImageAssetTypeDryRunInputError, OSError, TypeError, ValueError):
        raise SelectedMediaBaselineSnapshotError("local_baseline_input_required") from None
    if len(set(normalized)) != 3 or output in normalized:
        raise SelectedMediaBaselineSnapshotError("baseline_input_collision")
    loaded = tuple(
        _read_local_json(path, expected)
        for path, expected in zip(paths, _EXPECTED_INPUT_NAMES, strict=True)
    )
    report = build_selected_media_baseline_snapshot(
        loaded[0][0], loaded[1][0], loaded[2][0],
        selection_report_sha256=loaded[0][1],
        nested_baseline_report_sha256=loaded[1][1],
        depth2_baseline_report_sha256=loaded[2][1],
    )
    _write_snapshot_once(output, report)
    return report, output
