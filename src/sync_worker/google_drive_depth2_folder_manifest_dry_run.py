"""Fresh Sheets -> Root -> depth-one -> depth-two metadata dry run.

Only mapping and SKU reports are local inputs. All traversal authorization comes
from fresh domain objects, never an earlier serialized Drive manifest.
"""

from __future__ import annotations

from pathlib import Path

from .config import GoogleSettings
from .google_api import GoogleDriveMetadataAndSheetsClientFactory, GoogleDriveMetadataGateway
from .google_drive_depth2_folder_manifest import (
    MAX_DEPTH2_FOLDERS_PER_RUN,
    MAX_TRAVERSAL_DEPTH,
    GoogleDriveDepth2FolderManifest,
    GoogleDriveDepth2FolderManifestError,
    build_depth2_drive_folder_manifests_with_gateway,
    create_secure_google_drive_depth2_folder_handle,
)
from .google_drive_folder_manifest_dry_run import (
    REPORT_FILENAME as ROOT_REPORT_FILENAME,
    GoogleDriveFolderManifestDryRunError,
    _assert_report_safe,
    _item_report,
    _safe_input_reference,
    _unique,
    read_root_drive_manifest_batch,
    validate_drive_manifest_scopes,
)
from .google_drive_nested_folder_manifest_dry_run import (
    REPORT_FILENAME as NESTED_REPORT_FILENAME,
    NestedDriveManifestRead,
    read_nested_drive_manifest_batch,
)
from .media_source_discovery_dry_run import validate_sku_snapshot_compatibility
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .secure_media_reference_read import SecureMediaReferenceReader, validate_mapping_report
from .sheet_layout import validate_sheet_title


REPORT_FILENAME = "google-drive-depth2-folder-manifest-dry-run.json"


class GoogleDriveDepth2FolderManifestDryRunError(GoogleDriveFolderManifestDryRunError):
    """Fixed, provider-data-free orchestration error."""


def _depth2_manifest_report(manifest: GoogleDriveDepth2FolderManifest) -> dict[str, object]:
    if type(manifest.depth) is not int or manifest.depth != MAX_TRAVERSAL_DEPTH:
        raise GoogleDriveDepth2FolderManifestDryRunError("invalid_depth2_manifest_depth")
    items = manifest.items
    return {
        "sku": manifest.sku,
        "product_source": manifest.product_source.to_dict(),
        "root_folder_id_fingerprint": manifest.root_folder_id_fingerprint,
        "depth1_folder_id_fingerprint": manifest.depth1_folder_id_fingerprint,
        "depth2_folder_id_fingerprint": manifest.depth2_folder_id_fingerprint,
        "depth1_safe_folder_name": manifest.depth1_safe_folder_name,
        "depth2_safe_folder_name": manifest.depth2_safe_folder_name,
        "depth": manifest.depth,
        "status": manifest.status,
        "item_count": len(items),
        "image_candidate_count": sum(item.image_candidate for item in items),
        "nested_folder_at_depth_limit_count": sum(item.item_kind == "nested_folder" for item in items),
        "shortcut_count": sum(item.item_kind == "shortcut" for item in items),
        "google_workspace_file_count": sum(item.item_kind == "google_workspace_file" for item in items),
        "other_file_count": sum(item.item_kind == "other_file" for item in items),
        "duplicate_name_candidate_count": sum("duplicate_name_candidate" in item.warnings for item in items),
        "duplicate_content_candidate_count": sum("duplicate_content_candidate" in item.warnings for item in items),
        "pages_read": manifest.pages_read,
        "items": [
            {**_item_report(item), "image_candidate_status": item.image_candidate_status}
            for item in items
        ],
        "warnings": list(manifest.warnings),
        "blocking_issues": list(manifest.blocking_issues),
    }


def build_depth2_drive_folder_manifest_report(
    nested_read: NestedDriveManifestRead,
    *,
    mapping_input_file: str,
    sheet_title: str,
    sku_report_input_file: str,
    sheets_read_requests_performed: int,
    gateway: GoogleDriveMetadataGateway,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Promote actual depth-limit items, run Core once and allowlist its output."""

    if not isinstance(nested_read, NestedDriveManifestRead):
        raise GoogleDriveDepth2FolderManifestDryRunError("fresh_nested_manifest_read_required")
    root_read = nested_read.root_read
    parents = nested_read.core_batch.manifests
    root_issues = list(nested_read.root_issues)
    depth1_issues = []
    candidates = []
    for parent in parents:
        if type(parent.depth) is not int or parent.depth != 1:
            raise GoogleDriveDepth2FolderManifestDryRunError("invalid_depth1_manifest_depth")
        if parent.status not in {"listed", "empty_folder"} or parent.blocking_issues:
            depth1_issues.append({
                "sku": parent.sku, "product_source": parent.product_source.to_dict(),
                "root_folder_id_fingerprint": parent.root_folder_id_fingerprint,
                "depth1_folder_id_fingerprint": parent.nested_folder_id_fingerprint,
                "depth1_safe_folder_name": parent.safe_folder_name,
                "status": parent.status, "warnings": list(parent.warnings),
                "blocking_issues": list(parent.blocking_issues),
            })
            continue
        candidates.extend(
            (parent, item) for item in parent.items
            if item.item_kind == "nested_folder" and "max_traversal_depth_reached" in item.warnings
        )
    # Count invalid/missing-ID targets too: they cannot bypass the Core batch cap.
    if len(candidates) > MAX_DEPTH2_FOLDERS_PER_RUN:
        raise GoogleDriveDepth2FolderManifestError("depth2_folder_batch_limit_exceeded")
    handles = []
    invalid_manifests = []
    for parent, item in candidates:
        try:
            handles.append(create_secure_google_drive_depth2_folder_handle(parent, item))
        except Exception:
            # A missing provider ID is an explicit blocker, never a fingerprint fallback.
            invalid_manifests.append(GoogleDriveDepth2FolderManifest(
                sku=parent.sku, product_source=parent.product_source,
                root_folder_id_fingerprint=parent.root_folder_id_fingerprint,
                depth1_folder_id_fingerprint=parent.nested_folder_id_fingerprint,
                depth2_folder_id_fingerprint=item.file_id_fingerprint,
                depth1_safe_folder_name=parent.safe_folder_name,
                depth2_safe_folder_name=item.safe_name,
                depth=MAX_TRAVERSAL_DEPTH, status="invalid_depth2_folder_handle",
                items=(), pages_read=0, warnings=("invalid_depth2_folder_handle",),
                blocking_issues=("invalid_depth2_folder_handle",),
            ))
    # No returned child is ever promoted or listed again.
    depth2_batch = build_depth2_drive_folder_manifests_with_gateway(handles, gateway)
    manifests = (*depth2_batch.manifests, *invalid_manifests)
    results = [_depth2_manifest_report(manifest) for manifest in manifests]
    results.sort(key=lambda item: (
        item["sku"], item["root_folder_id_fingerprint"], item["depth1_folder_id_fingerprint"],
        item["depth2_folder_id_fingerprint"] or "", item["product_source"]["start_row"],
        item["product_source"]["end_row"], item["depth1_safe_folder_name"], item["depth2_safe_folder_name"],
    ))
    root_issues.sort(key=lambda item: (
        item["product_source"]["start_row"], item["product_source"]["end_row"],
        item["sku"] or "", item["root_folder_id_fingerprint"] or "",
    ))
    depth1_issues.sort(key=lambda item: (
        item["sku"], item["root_folder_id_fingerprint"], item["depth1_folder_id_fingerprint"] or "",
        item["product_source"]["start_row"], item["product_source"]["end_row"],
        item["depth1_safe_folder_name"],
    ))
    counts = depth2_batch.summary.to_dict()
    depth2_pages = counts.pop("pages_read")
    depth2_reads = counts.pop("drive_read_requests_performed")
    counts["total_depth2_folders"] += len(invalid_manifests)
    counts["invalid_depth2_folder_handles"] += len(invalid_manifests)
    root_reads = root_read.drive_read_requests_performed
    nested_summary = nested_read.core_batch.summary
    nested_reads = nested_summary.drive_read_requests_performed
    summary = {
        "root_folders_processed": root_read.core_batch.summary.total_folders,
        "depth1_folders_processed": nested_summary.total_nested_folders,
        "root_sources_blocked": len(root_read.blocked_results),
        "root_folder_issues": len(root_issues),
        "depth1_folder_issues": len(depth1_issues),
        **counts,
        "root_pages_read": root_read.core_batch.summary.pages_read,
        "nested_pages_read": nested_summary.pages_read,
        "depth2_pages_read": depth2_pages,
        "sheets_read_requests_performed": sheets_read_requests_performed,
        "root_drive_read_requests_performed": root_reads,
        "nested_drive_read_requests_performed": nested_reads,
        "depth2_drive_read_requests_performed": depth2_reads,
        "network_requests_performed": sheets_read_requests_performed + root_reads + nested_reads + depth2_reads,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    blocking_issues = _unique(tuple(
        issue for result in (*root_issues, *depth1_issues, *results) for issue in result["blocking_issues"]
    ))
    warnings = _unique(tuple(
        warning for result in (*root_issues, *depth1_issues, *results) for warning in result["warnings"]
    ) + tuple(warning for root in root_read.core_batch.manifests for warning in root.warnings)
        + tuple(warning for parent in parents for warning in parent.warnings))
    report = {
        "status": "partial" if root_issues or depth1_issues or blocking_issues else "ok",
        "inputs": {"mapping": mapping_input_file, "sheet": sheet_title, "sku_report": sku_report_input_file},
        "summary": summary, "results": results,
        "root_issues": root_issues, "depth1_issues": depth1_issues,
        "warnings": list(warnings), "blocking_issues": list(blocking_issues),
        "write_requests_performed": 0,
    }
    forbidden_values = (*nested_read.forbidden_values, *(
        item.provider_file_id for manifest in manifests for item in manifest.items if item.provider_file_id
    ))
    _assert_report_safe(report, forbidden_values=forbidden_values)
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):
        raise GoogleDriveDepth2FolderManifestDryRunError("invalid_depth2_manifest_report")
    _assert_report_safe(sanitized, forbidden_values=forbidden_values)
    return sanitized


def run_depth2_drive_folder_manifest_dry_run(
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path,
    settings: GoogleSettings,
    client_factory: GoogleDriveMetadataAndSheetsClientFactory,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Rebuild all three Drive levels in memory; persist only the final report."""

    mapping_path = Path(mapping_input_path)
    sku_path = Path(sku_report_input_path)
    if any(
        path.name.casefold() in {ROOT_REPORT_FILENAME, NESTED_REPORT_FILENAME}
        for path in (mapping_path, sku_path)
    ):
        raise GoogleDriveDepth2FolderManifestDryRunError("serialized_drive_manifest_not_supported")
    mapping = load_local_json_report(mapping_path)
    sku_report = load_local_json_report(sku_path)
    validate_sku_snapshot_compatibility(mapping, sku_report)
    validated_sheet = validate_sheet_title(sheet_title)
    mapped_sources = validate_mapping_report(mapping)
    validate_drive_manifest_scopes(settings)
    active_redactor = redactor or Redactor()
    if mapped_sources:
        clients = client_factory.create_drive_metadata_clients(settings)
        read_batch = SecureMediaReferenceReader(settings, None, clients=clients).run(
            mapping, sheet_title=validated_sheet,
        )
        read_results = read_batch.results
        sheets_reads = read_batch.read_requests_performed
        gateway = GoogleDriveMetadataGateway(clients.drive)
    else:
        read_results = ()
        sheets_reads = 0
        gateway = GoogleDriveMetadataGateway(object())
    root_read = read_root_drive_manifest_batch(read_results, sku_report, gateway=gateway)
    nested_read = read_nested_drive_manifest_batch(root_read, gateway=gateway)
    report = build_depth2_drive_folder_manifest_report(
        nested_read,
        mapping_input_file=_safe_input_reference(mapping_path, project_root),
        sheet_title=validated_sheet,
        sku_report_input_file=_safe_input_reference(sku_path, project_root),
        sheets_read_requests_performed=sheets_reads, gateway=gateway, redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    _assert_report_safe(report)
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
