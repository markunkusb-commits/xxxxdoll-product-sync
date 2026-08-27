"""Fresh Sheets -> Root -> depth-one Nested metadata dry run.

The only local inputs are Image Mapping and SKU reports. Root/Nested domain
objects and provider identifiers stay in memory; no Root report is reopened.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import GoogleSettings
from .google_api import GoogleDriveMetadataAndSheetsClientFactory, GoogleDriveMetadataGateway
from .google_drive_folder_manifest_dry_run import (
    REPORT_FILENAME as ROOT_REPORT_FILENAME,
    GoogleDriveFolderManifestDryRunError,
    RootDriveManifestRead,
    _assert_report_safe,
    _item_report,
    _safe_input_reference,
    _unique,
    read_root_drive_manifest_batch,
    validate_drive_manifest_scopes,
)
from .google_drive_nested_folder_manifest import (
    MAX_NESTED_FOLDERS_PER_RUN,
    MAX_TRAVERSAL_DEPTH,
    GoogleDriveNestedFolderManifest,
    GoogleDriveNestedFolderManifestBatchResult,
    GoogleDriveNestedFolderManifestError,
    build_nested_drive_folder_manifests_with_gateway,
    create_secure_google_drive_nested_folder_handle,
)
from .media_source_discovery_dry_run import validate_sku_snapshot_compatibility
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .secure_media_reference_read import SecureMediaReferenceReader, validate_mapping_report
from .sheet_layout import validate_sheet_title


REPORT_FILENAME = "google-drive-nested-folder-manifest-dry-run.json"


class GoogleDriveNestedFolderManifestDryRunError(GoogleDriveFolderManifestDryRunError):
    """A fixed, provider-data-free orchestration error."""


@dataclass(frozen=True, slots=True)
class NestedDriveManifestRead:
    """Fresh depth-one domain results; provider identifiers remain memory-only."""

    root_read: RootDriveManifestRead
    core_batch: GoogleDriveNestedFolderManifestBatchResult
    root_issues: tuple[dict[str, object], ...]
    forbidden_values: tuple[str, ...] = field(repr=False)


def _nested_manifest_report(manifest: GoogleDriveNestedFolderManifest) -> dict[str, object]:
    if type(manifest.depth) is not int or manifest.depth != MAX_TRAVERSAL_DEPTH:
        raise GoogleDriveNestedFolderManifestDryRunError("invalid_nested_manifest_depth")
    items = manifest.items
    return {
        "sku": manifest.sku,
        "product_source": manifest.product_source.to_dict(),
        "root_folder_id_fingerprint": manifest.root_folder_id_fingerprint,
        "nested_folder_id_fingerprint": manifest.nested_folder_id_fingerprint,
        "safe_folder_name": manifest.safe_folder_name,
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
        "items": [_item_report(item) for item in items],
        "warnings": list(manifest.warnings),
        "blocking_issues": list(manifest.blocking_issues),
    }


def _root_issue(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "sku": result["sku"],
        "product_source": result["product_source"],
        "root_folder_id_fingerprint": result["folder_id_fingerprint"],
        "status": result["status"],
        "warnings": result["warnings"],
        "blocking_issues": result["blocking_issues"],
    }


def read_nested_drive_manifest_batch(
    root_read: RootDriveManifestRead,
    *,
    gateway: GoogleDriveMetadataGateway,
) -> NestedDriveManifestRead:
    """Hand actual Root items to Nested Core without serializing a manifest."""

    if not isinstance(root_read, RootDriveManifestRead):
        raise GoogleDriveNestedFolderManifestDryRunError("fresh_root_manifest_read_required")
    roots = root_read.core_batch.manifests
    # Failed/incomplete Root listings cannot authorize deeper traversal.
    root_issues = [_root_issue(item) for item in root_read.blocked_results]
    candidates = []
    for root in roots:
        if root.status not in {"listed", "empty_folder"} or root.blocking_issues:
            root_issues.append(_root_issue(root.to_dict()))
            continue
        candidates.extend((root, item) for item in root.items if item.item_kind == "nested_folder")
    # Include malformed candidates in the same Core batch cap; none may bypass it.
    if len(candidates) > MAX_NESTED_FOLDERS_PER_RUN:
        raise GoogleDriveNestedFolderManifestError("nested_folder_batch_limit_exceeded")

    handles = []
    invalid_manifests = []
    for root, item in candidates:
        try:
            handles.append(create_secure_google_drive_nested_folder_handle(root, item))
        except Exception:
            # Do not guess a missing ID or emit the original exception contents.
            invalid_manifests.append(GoogleDriveNestedFolderManifest(
                sku=root.sku, product_source=root.product_source,
                root_folder_id_fingerprint=root.folder_id_fingerprint,
                nested_folder_id_fingerprint=item.file_id_fingerprint,
                safe_folder_name=item.safe_name, depth=MAX_TRAVERSAL_DEPTH,
                status="invalid_nested_folder_handle", items=(), pages_read=0,
                warnings=("invalid_nested_folder_handle",),
                blocking_issues=("invalid_nested_folder_handle",),
            ))
    # One bounded call, never a loop over the returned depth-two children.
    nested_batch = build_nested_drive_folder_manifests_with_gateway(handles, gateway)
    manifests = (*nested_batch.manifests, *invalid_manifests)
    summary = replace(
        nested_batch.summary,
        total_nested_folders=nested_batch.summary.total_nested_folders + len(invalid_manifests),
        invalid_nested_folder_handles=nested_batch.summary.invalid_nested_folder_handles + len(invalid_manifests),
    )
    forbidden_values = (*root_read.forbidden_values, *(
        item.provider_file_id for manifest in manifests for item in manifest.items
        if item.provider_file_id
    ))
    return NestedDriveManifestRead(
        root_read=root_read,
        core_batch=GoogleDriveNestedFolderManifestBatchResult(manifests, summary),
        root_issues=tuple(root_issues),
        forbidden_values=forbidden_values,
    )


def build_nested_drive_folder_manifest_report(
    root_read: RootDriveManifestRead,
    *,
    mapping_input_file: str,
    sheet_title: str,
    sku_report_input_file: str,
    sheets_read_requests_performed: int,
    gateway: GoogleDriveMetadataGateway,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Project the shared depth-one read without changing the existing report."""

    nested_read = read_nested_drive_manifest_batch(root_read, gateway=gateway)
    nested_batch = nested_read.core_batch
    manifests = nested_batch.manifests
    roots = root_read.core_batch.manifests
    root_issues = list(nested_read.root_issues)
    results = [_nested_manifest_report(item) for item in manifests]
    results.sort(key=lambda item: (
        item["sku"], item["safe_folder_name"].casefold(),
        item["nested_folder_id_fingerprint"] or "", item["root_folder_id_fingerprint"],
        item["product_source"]["start_row"], item["product_source"]["end_row"],
        item["safe_folder_name"],
    ))
    root_issues.sort(key=lambda item: (
        item["product_source"]["start_row"], item["product_source"]["end_row"],
        item["sku"] or "", item["root_folder_id_fingerprint"] or "",
    ))
    counts = nested_batch.summary.to_dict()
    nested_pages = counts.pop("pages_read")
    nested_reads = counts.pop("drive_read_requests_performed")
    root_reads = root_read.drive_read_requests_performed
    summary = {
        "root_folders_processed": root_read.core_batch.summary.total_folders,
        "root_folders_listed": root_read.core_batch.summary.folders_listed,
        "root_sources_blocked": len(root_read.blocked_results),
        "root_folder_issues": len(root_issues),
        **counts,
        "root_pages_read": root_read.core_batch.summary.pages_read,
        "nested_pages_read": nested_pages,
        "sheets_read_requests_performed": sheets_read_requests_performed,
        "root_drive_read_requests_performed": root_reads,
        "nested_drive_read_requests_performed": nested_reads,
        "network_requests_performed": sheets_read_requests_performed + root_reads + nested_reads,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    blocking_issues = _unique(tuple(
        issue for result in (*root_issues, *results) for issue in result["blocking_issues"]
    ))
    warnings = _unique(tuple(
        warning for result in (*root_issues, *results) for warning in result["warnings"]
    ) + tuple(warning for root in roots for warning in root.warnings))
    report = {
        "status": "partial" if root_issues or blocking_issues else "ok",
        "inputs": {"mapping": mapping_input_file, "sheet": sheet_title, "sku_report": sku_report_input_file},
        "summary": summary,
        "results": results,
        "root_issues": root_issues,
        "warnings": list(warnings),
        "blocking_issues": list(blocking_issues),
        "write_requests_performed": 0,
    }
    forbidden_values = nested_read.forbidden_values
    _assert_report_safe(report, forbidden_values=forbidden_values)
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):
        raise GoogleDriveNestedFolderManifestDryRunError("invalid_nested_manifest_report")
    _assert_report_safe(sanitized, forbidden_values=forbidden_values)
    return sanitized


def run_nested_drive_folder_manifest_dry_run(
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path,
    settings: GoogleSettings,
    client_factory: GoogleDriveMetadataAndSheetsClientFactory,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Rebuild the entire read-only chain in one process; write only the final report."""

    mapping_path = Path(mapping_input_path)
    sku_path = Path(sku_report_input_path)
    if any(path.name.casefold() == ROOT_REPORT_FILENAME for path in (mapping_path, sku_path)):
        raise GoogleDriveNestedFolderManifestDryRunError("serialized_root_manifest_not_supported")
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
    report = build_nested_drive_folder_manifest_report(
        root_read,
        mapping_input_file=_safe_input_reference(mapping_path, project_root),
        sheet_title=validated_sheet,
        sku_report_input_file=_safe_input_reference(sku_path, project_root),
        sheets_read_requests_performed=sheets_reads,
        gateway=gateway, redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    _assert_report_safe(report)
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
