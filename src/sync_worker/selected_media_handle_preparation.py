"""Selection-driven fresh metadata preparation of secure media handles.

Historical identities are restored from the frozen safe snapshot.  Provider
authority comes only from fresh in-memory Drive domain objects.  No media bytes
are read and no intermediate traversal report is written.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    google_drive_depth2_folder_manifest as depth2_core,
    google_drive_folder_manifest as root_core,
    google_drive_nested_folder_manifest as nested_core,
    image_selection_policy,
    secure_selected_media_handle as handle_core,
    selected_media_baseline_snapshot as snapshot_core,
)
from .config import GoogleSettings
from .google_api import (
    GoogleDriveMetadataAndSheetsClientFactory,
    GoogleDriveMetadataGateway,
)
from .google_drive_folder_manifest_dry_run import (
    RootDriveManifestRead,
    read_root_drive_manifest_batch,
    validate_drive_manifest_scopes,
)
from .media_source_discovery_dry_run import (
    restore_verified_sku_entries,
    validate_sku_snapshot_compatibility,
)
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .secure_media_reference_read import (
    SecureMediaReferenceReader,
    validate_mapping_report,
)
from .sheet_layout import validate_sheet_title


POLICY_VERSION = "xxxxdoll-selected-media-handle-preparation-v1"
REPORT_FILENAME = "selected-media-handle-preparation.json"
_ZERO_COUNTERS = (
    "download_requests_performed", "media_read_requests_performed",
    "conversion_requests_performed", "wordpress_upload_requests_performed",
    "write_requests_performed",
)
_SNAPSHOT_FIELDS = frozenset({
    "status", "snapshot_version", "source_selection_policy_version",
    "source_handle_policy_version", "selection_report_sha256",
    "nested_baseline_report_sha256", "depth2_baseline_report_sha256",
    "summary", "results", "network_requests_performed",
    "drive_read_requests_performed", "download_requests_performed",
    "media_read_requests_performed", "conversion_requests_performed",
    "wordpress_upload_requests_performed", "write_requests_performed",
})
_SNAPSHOT_RESULT_FIELDS = frozenset({
    "selection_position", "image_role", "folder_role", "selection_reason",
    "baseline_identity",
})
_SNAPSHOT_SUMMARY_FIELDS = frozenset({
    "selected_items", "baseline_created", "baseline_nested", "baseline_depth2",
    "baseline_missing", "baseline_ambiguous", "missing_fingerprint",
    "invalid_fingerprint", "missing_checksum", "invalid_checksum",
    "jpeg_baselines", "blocking_items",
})
_SAFE_PROPAGATED_ERROR_CODES = frozenset({
    "invalid_safe_baseline_identity",
    "selected_media_baseline_provenance_mismatch",
    "fresh_selected_media_checksum_missing",
    "fresh_selected_media_fingerprint_missing",
    "selected_media_content_changed",
    "selected_media_file_identity_changed",
    "selected_media_metadata_changed",
    "selected_media_provenance_mismatch",
    "selected_media_source_ambiguous",
    "selected_media_source_missing",
    "selected_media_source_not_image_candidate",
    "source_manifest_blocked",
})


class SelectedMediaHandlePreparationError(ValueError):
    """Fixed safe errors only; never provider identity or exception repr."""


@dataclass(frozen=True, slots=True)
class SelectedMediaHandlePreparationResult:
    status: str
    _report: Mapping[str, object] = field(repr=False)
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...] = field(repr=False)

    def to_safe_report_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(self._report, ensure_ascii=False))

    def __repr__(self) -> str:
        return f"SelectedMediaHandlePreparationResult(status={self.status!r}, handles_count={len(self.handles)})"


def _safe_error(error: BaseException, fallback: str) -> str:
    if len(error.args) == 1 and isinstance(error.args[0], str):
        code = error.args[0]
        if code in _SAFE_PROPAGATED_ERROR_CODES:
            return code
    return fallback


def _restore_snapshot(
    snapshot: Mapping[str, object],
    selections: Sequence[image_selection_policy.ImageSelectionItem],
    selection_sha256: str,
) -> tuple[handle_core.SelectedMediaBaselineIdentity, ...]:
    if set(snapshot) != _SNAPSHOT_FIELDS:
        raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
    if snapshot.get("status") != "ok":
        raise SelectedMediaHandlePreparationError("baseline_snapshot_status_not_ok")
    if snapshot.get("snapshot_version") != snapshot_core.SNAPSHOT_VERSION:
        raise SelectedMediaHandlePreparationError("baseline_snapshot_version_mismatch")
    if snapshot.get("source_selection_policy_version") != image_selection_policy.POLICY_VERSION:
        raise SelectedMediaHandlePreparationError("selection_policy_version_mismatch")
    if snapshot.get("source_handle_policy_version") != handle_core.POLICY_VERSION:
        raise SelectedMediaHandlePreparationError("handle_policy_version_mismatch")
    for field in ("nested_baseline_report_sha256", "depth2_baseline_report_sha256"):
        value = snapshot.get(field)
        if type(value) is not str or root_core._SHA256_PATTERN.fullmatch(value) is None:
            raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
    for counter in (
        "network_requests_performed", "drive_read_requests_performed",
        *_ZERO_COUNTERS,
    ):
        if type(snapshot.get(counter)) is not int or snapshot[counter] != 0:
            raise SelectedMediaHandlePreparationError("baseline_snapshot_not_offline")
    if snapshot.get("selection_report_sha256") != selection_sha256:
        raise SelectedMediaHandlePreparationError("selection_snapshot_hash_mismatch")
    raw_results = snapshot.get("results")
    if not isinstance(raw_results, list):
        raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
    summary = snapshot.get("summary")
    if (
        not isinstance(summary, Mapping)
        or any(type(key) is not str for key in summary)
        or set(summary) != _SNAPSHOT_SUMMARY_FIELDS
        or any(type(summary[field]) is not int or summary[field] < 0 for field in summary)
    ):
        raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
    index: dict[tuple[object, ...], list[Mapping[str, object]]] = {}
    for raw in raw_results:
        if not isinstance(raw, Mapping) or set(raw) != _SNAPSHOT_RESULT_FIELDS:
            raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
        identity = raw.get("baseline_identity")
        if not isinstance(identity, Mapping):
            raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
        source = identity.get("product_source")
        if not isinstance(source, Mapping):
            raise SelectedMediaHandlePreparationError("malformed_baseline_snapshot")
        key = (
            identity.get("sku"), source.get("start_row"), source.get("end_row"),
            identity.get("source_manifest_kind"), identity.get("depth"),
            identity.get("safe_folder_name"), identity.get("parent_safe_folder_name"),
            identity.get("safe_name"), raw.get("selection_position"),
        )
        index.setdefault(key, []).append(raw)
    restored = []
    for selection in selections:
        source = selection.product_source
        key = (
            selection.sku, source.start_row, source.end_row,
            selection.source_manifest_kind, selection.depth,
            selection.safe_folder_name, selection.parent_safe_folder_name,
            selection.safe_name, selection.selection_position,
        )
        matches = index.get(key, [])
        if not matches:
            raise SelectedMediaHandlePreparationError("baseline_snapshot_source_missing")
        if len(matches) != 1:
            raise SelectedMediaHandlePreparationError("baseline_snapshot_source_ambiguous")
        raw = matches[0]
        if (
            raw.get("image_role") != selection.image_role.value
            or raw.get("folder_role") != selection.folder_role.value
            or raw.get("selection_reason") != selection.selection_reason.value
        ):
            raise SelectedMediaHandlePreparationError("baseline_snapshot_selection_mismatch")
        try:
            restored.append(handle_core.restore_selected_media_baseline_identity(
                selection, raw["baseline_identity"],
            ))
        except handle_core.SecureSelectedMediaHandleError as error:
            raise SelectedMediaHandlePreparationError(
                _safe_error(error, "baseline_identity_restore_failed")
            ) from None
    if len(index) != len(selections):
        raise SelectedMediaHandlePreparationError("baseline_snapshot_record_mismatch")
    expected_summary = {
        "selected_items": len(selections),
        "baseline_created": len(restored),
        "baseline_nested": sum(item.source_manifest_kind == "nested" for item in restored),
        "baseline_depth2": sum(item.source_manifest_kind == "depth2" for item in restored),
        "baseline_missing": 0,
        "baseline_ambiguous": 0,
        "missing_fingerprint": 0,
        "invalid_fingerprint": 0,
        "missing_checksum": 0,
        "invalid_checksum": 0,
        "jpeg_baselines": sum(item.source_mime_type == "image/jpeg" for item in restored),
        "blocking_items": 0,
    }
    if dict(summary) != expected_summary:
        raise SelectedMediaHandlePreparationError("baseline_snapshot_summary_mismatch")
    return tuple(restored)


def validate_preparation_inputs(
    selection_report: Mapping[str, object],
    selection_bytes: bytes,
    baseline_snapshot: Mapping[str, object],
    mapping_report: Mapping[str, object],
    sku_report: Mapping[str, object],
    sheet_title: str,
    settings: GoogleSettings,
) -> tuple[
    tuple[image_selection_policy.ImageSelectionItem, ...],
    tuple[handle_core.SelectedMediaBaselineIdentity, ...],
]:
    """Complete every local/schema/scope check before client construction."""
    try:
        selections = snapshot_core._restore_selection_report(selection_report)
        selection_hash = hashlib.sha256(selection_bytes).hexdigest()
        baselines = _restore_snapshot(baseline_snapshot, selections, selection_hash)
        validate_mapping_report(mapping_report)
        validate_sku_snapshot_compatibility(mapping_report, sku_report)
        restore_verified_sku_entries(sku_report)
        validate_sheet_title(sheet_title)
        validate_drive_manifest_scopes(settings)
    except SelectedMediaHandlePreparationError:
        raise
    except Exception as error:
        raise SelectedMediaHandlePreparationError(
            _safe_error(error, "preparation_input_validation_failed")
        ) from None
    return tuple(selections), baselines


def _root_key(value: object) -> tuple[object, ...]:
    return value.sku, value.product_source.start_row, value.product_source.end_row


def _manifest_key(value: object) -> tuple[object, ...]:
    if type(value) is nested_core.GoogleDriveNestedFolderManifest:
        return (*_root_key(value), "nested", 1, value.safe_folder_name, None)
    return (
        *_root_key(value), "depth2", 2,
        value.depth2_safe_folder_name, value.depth1_safe_folder_name,
    )


def _fresh_item(manifest: object | None, safe_name: str) -> root_core.DriveManifestItem | None:
    if manifest is None:
        return None
    matches = [item for item in manifest.items if item.safe_name == safe_name]
    return matches[0] if len(matches) == 1 else None


def prepare_selected_media_handles_from_fresh_root(
    selections: Sequence[image_selection_policy.ImageSelectionItem],
    baselines: Sequence[handle_core.SelectedMediaBaselineIdentity],
    root_read: RootDriveManifestRead,
    gateway: GoogleDriveMetadataGateway,
    *,
    sheets_read_requests_performed: int,
) -> SelectedMediaHandlePreparationResult:
    """Traverse only selected paths and build all handles or expose none."""
    selections = tuple(selections)
    baselines = tuple(baselines)
    if len(selections) != len(baselines):
        raise SelectedMediaHandlePreparationError("baseline_selection_count_mismatch")
    ordered = tuple(sorted(
        zip(selections, baselines, strict=True),
        key=lambda pair: (pair[0].sku, pair[0].selection_position),
    ))
    selections = tuple(pair[0] for pair in ordered)
    baselines = tuple(pair[1] for pair in ordered)
    required_roots = sorted({_root_key(item) for item in selections})
    roots_by_key: dict[tuple[object, ...], list[object]] = {}
    for manifest in root_read.core_batch.manifests:
        roots_by_key.setdefault(_root_key(manifest), []).append(manifest)
    path_errors: dict[tuple[object, ...], str] = {}
    required_depth1 = sorted({
        (*_root_key(item), item.parent_safe_folder_name if item.source_manifest_kind == "depth2" else item.safe_folder_name)
        for item in selections
    })
    nested_handles = []
    root_ok = set()
    depth1_missing = depth1_ambiguous = 0
    for sku, start, end, folder in required_depth1:
        root_key = (sku, start, end)
        roots = roots_by_key.get(root_key, [])
        if len(roots) != 1:
            code = "fresh_root_source_missing" if not roots else "fresh_root_source_ambiguous"
            path_errors[(sku, start, end, folder)] = code
            continue
        root = roots[0]
        root_ok.add(root_key)
        matches = [
            item for item in root.items
            if item.safe_name == folder and item.item_kind == "nested_folder"
        ]
        if len(matches) != 1:
            code = "fresh_selected_folder_missing" if not matches else "fresh_selected_folder_ambiguous"
            path_errors[(sku, start, end, folder)] = code
            depth1_missing += not matches
            depth1_ambiguous += bool(matches)
            continue
        try:
            nested_handles.append(
                nested_core.create_secure_google_drive_nested_folder_handle(root, matches[0])
            )
        except Exception as error:
            path_errors[(sku, start, end, folder)] = _safe_error(error, "fresh_selected_folder_invalid")
    nested_batch = nested_core.build_nested_drive_folder_manifests_with_gateway(
        nested_handles, gateway,
    )
    nested_by_key = {_manifest_key(item): item for item in nested_batch.manifests}
    required_depth2 = sorted({
        (*_root_key(item), item.parent_safe_folder_name, item.safe_folder_name)
        for item in selections if item.source_manifest_kind == "depth2"
    })
    depth2_handles = []
    depth2_missing = depth2_ambiguous = 0
    for sku, start, end, parent, folder in required_depth2:
        parent_manifest = nested_by_key.get((sku, start, end, "nested", 1, parent, None))
        if parent_manifest is None:
            path_errors[(sku, start, end, parent, folder)] = path_errors.get(
                (sku, start, end, parent), "fresh_selected_folder_missing"
            )
            depth2_missing += 1
            continue
        matches = [
            item for item in parent_manifest.items
            if item.safe_name == folder and item.item_kind == "nested_folder"
        ]
        if len(matches) != 1:
            code = "fresh_selected_depth2_folder_missing" if not matches else "fresh_selected_depth2_folder_ambiguous"
            path_errors[(sku, start, end, parent, folder)] = code
            depth2_missing += not matches
            depth2_ambiguous += bool(matches)
            continue
        try:
            depth2_handles.append(
                depth2_core.create_secure_google_drive_depth2_folder_handle(parent_manifest, matches[0])
            )
        except Exception as error:
            path_errors[(sku, start, end, parent, folder)] = _safe_error(error, "fresh_selected_depth2_folder_invalid")
    depth2_batch = depth2_core.build_depth2_drive_folder_manifests_with_gateway(
        depth2_handles, gateway,
    )
    all_fresh = (*nested_batch.manifests, *depth2_batch.manifests)
    fresh_by_key: dict[tuple[object, ...], list[object]] = {}
    for manifest in all_fresh:
        fresh_by_key.setdefault(_manifest_key(manifest), []).append(manifest)
    handles = []
    results = []
    for selection, baseline in zip(selections, baselines, strict=True):
        source = selection.product_source
        key = (
            selection.sku, source.start_row, source.end_row,
            selection.source_manifest_kind, selection.depth,
            selection.safe_folder_name, selection.parent_safe_folder_name,
        )
        path_key = (
            selection.sku, source.start_row, source.end_row,
            selection.parent_safe_folder_name if selection.source_manifest_kind == "depth2" else selection.safe_folder_name,
        )
        deep_path_key = (*path_key, selection.safe_folder_name)
        manifests = fresh_by_key.get(key, [])
        blocker = path_errors.get(deep_path_key) or path_errors.get(path_key)
        if blocker is None and len(manifests) != 1:
            blocker = "selected_media_source_missing" if not manifests else "selected_media_source_ambiguous"
        manifest = manifests[0] if len(manifests) == 1 else None
        fresh = _fresh_item(manifest, selection.safe_name)
        if blocker is None:
            try:
                handle = handle_core.create_secure_selected_media_handle(
                    selection, baseline, manifest,
                )
            except handle_core.SecureSelectedMediaHandleError as error:
                blocker = _safe_error(error, "secure_selected_media_handle_failed")
            else:
                handles.append(handle)
        result = {
            "sku": selection.sku,
            "selection_position": selection.selection_position,
            "image_role": selection.image_role.value,
            "folder_role": selection.folder_role.value,
            "source_manifest_kind": selection.source_manifest_kind,
            "depth": selection.depth,
            "safe_folder_name": selection.safe_folder_name,
            "parent_safe_folder_name": selection.parent_safe_folder_name,
            "safe_name": selection.safe_name,
            "baseline_file_id_fingerprint": baseline.file_id_fingerprint,
            "fresh_file_id_fingerprint": None if fresh is None else fresh.file_id_fingerprint,
            "baseline_md5_checksum": baseline.md5_checksum,
            "fresh_md5_checksum": None if fresh is None else fresh.md5_checksum,
            "baseline_mime_type": baseline.source_mime_type,
            "fresh_mime_type": None if fresh is None else fresh.mime_type,
            "baseline_size_bytes": baseline.size_bytes,
            "fresh_size_bytes": None if fresh is None else fresh.size_bytes,
            "baseline_image_width": baseline.image_width,
            "fresh_image_width": None if fresh is None else fresh.image_width,
            "baseline_image_height": baseline.image_height,
            "fresh_image_height": None if fresh is None else fresh.image_height,
            "handle_status": "prepared" if blocker is None else "blocked",
            "warnings": [],
            "blocking_issues": [] if blocker is None else [blocker],
        }
        results.append(result)
    results.sort(key=lambda item: (item["sku"], item["selection_position"]))
    blocked = sum(item["handle_status"] == "blocked" for item in results)
    authoritative = tuple(handles) if blocked == 0 and len(handles) == len(selections) else ()
    root_reads = root_read.drive_read_requests_performed
    depth1_reads = nested_batch.summary.drive_read_requests_performed
    depth2_reads = depth2_batch.summary.drive_read_requests_performed
    blockers = [code for item in results for code in item["blocking_issues"]]
    summary = {
        "selected_items": len(selections), "baseline_restored": len(baselines),
        "required_root_sources": len(required_roots),
        "required_depth1_folders": len(required_depth1),
        "required_depth2_folders": len(required_depth2),
        "root_sources_ok": len(root_ok),
        "root_sources_blocked": len(required_roots) - len(root_ok),
        "depth1_folders_listed": nested_batch.summary.nested_folders_listed,
        "depth1_folder_missing": depth1_missing,
        "depth1_folder_ambiguous": depth1_ambiguous,
        "depth2_folders_listed": depth2_batch.summary.depth2_folders_listed,
        "depth2_folder_missing": depth2_missing,
        "depth2_folder_ambiguous": depth2_ambiguous,
        "handles_prepared": len(handles), "handles_blocked": blocked,
        "nested_handles": sum(item["source_manifest_kind"] == "nested" and item["handle_status"] == "prepared" for item in results),
        "depth2_handles": sum(item["source_manifest_kind"] == "depth2" and item["handle_status"] == "prepared" for item in results),
        "primary_handles": sum(item["image_role"] == "primary" and item["handle_status"] == "prepared" for item in results),
        "gallery_handles": sum(item["image_role"] == "gallery" and item["handle_status"] == "prepared" for item in results),
        "file_identity_changed": blockers.count("selected_media_file_identity_changed"),
        "content_changed": blockers.count("selected_media_content_changed"),
        "metadata_changed": blockers.count("selected_media_metadata_changed"),
        "source_missing": sum("missing" in code for code in blockers),
        "source_ambiguous": sum("ambiguous" in code for code in blockers),
        "sheets_read_requests_performed": sheets_read_requests_performed,
        "root_drive_read_requests_performed": root_reads,
        "depth1_drive_read_requests_performed": depth1_reads,
        "depth2_drive_read_requests_performed": depth2_reads,
        "network_requests_performed": sheets_read_requests_performed + root_reads + depth1_reads + depth2_reads,
        **dict.fromkeys(_ZERO_COUNTERS, 0),
    }
    report = {
        "status": "ok" if blocked == 0 else "blocked",
        "policy_version": POLICY_VERSION,
        "source_snapshot_version": snapshot_core.SNAPSHOT_VERSION,
        "source_handle_policy_version": handle_core.POLICY_VERSION,
        "summary": summary, "results": results,
        "network_requests_performed": summary["network_requests_performed"],
        **dict.fromkeys(_ZERO_COUNTERS, 0),
    }
    sanitized = sanitize_report_data(report, Redactor())
    root_core._assert_report_safe(sanitized)
    return SelectedMediaHandlePreparationResult(report["status"], sanitized, authoritative)


def prepare_selected_media_handles(
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    settings: GoogleSettings,
    client_factory: GoogleDriveMetadataAndSheetsClientFactory,
) -> SelectedMediaHandlePreparationResult:
    """Prepare authoritative handles without writing any report."""
    try:
        selection_path = snapshot_core._local_path(selection_report_path)
        raw_selection = selection_path.read_bytes()
        selection_report = json.loads(raw_selection.decode("utf-8"))
        baseline_snapshot = load_local_json_report(Path(baseline_snapshot_path))
        mapping_report = load_local_json_report(Path(mapping_path))
        sku_report = load_local_json_report(Path(sku_report_path))
    except Exception:
        raise SelectedMediaHandlePreparationError("preparation_local_input_read_failed") from None
    selections, baselines = validate_preparation_inputs(
        selection_report, raw_selection, baseline_snapshot, mapping_report,
        sku_report, sheet_title, settings,
    )
    clients = client_factory.create_drive_metadata_clients(settings)
    read_batch = SecureMediaReferenceReader(settings, None, clients=clients).run(
        mapping_report, sheet_title=sheet_title,
    )
    required_sources = {
        (item.product_source.start_row, item.product_source.end_row)
        for item in selections
    }
    selected_reads = tuple(
        item for item in read_batch.results
        if (item.mapped_source.product_source.start_row, item.mapped_source.product_source.end_row) in required_sources
    )
    gateway = GoogleDriveMetadataGateway(clients.drive)
    root_read = read_root_drive_manifest_batch(selected_reads, sku_report, gateway=gateway)
    result = prepare_selected_media_handles_from_fresh_root(
        selections, baselines, root_read, gateway,
        sheets_read_requests_performed=read_batch.read_requests_performed,
    )
    return result


def run_selected_media_handle_preparation(
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    settings: GoogleSettings,
    client_factory: GoogleDriveMetadataAndSheetsClientFactory,
    *, project_root: Path,
) -> tuple[SelectedMediaHandlePreparationResult, Path]:
    """Prepare handles and write the standalone Preparation audit."""

    result = prepare_selected_media_handles(
        selection_report_path, baseline_snapshot_path, mapping_path,
        sheet_title, sku_report_path, settings, client_factory,
    )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output, Redactor()).write(result.to_safe_report_dict())
    return result, output
