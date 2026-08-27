"""Secure Sheets-to-Drive folder manifest reality-check adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    GoogleSettings,
)
from .google_api import (
    GoogleDriveMetadataAndSheetsClientFactory,
    GoogleDriveMetadataGateway,
)
from .google_drive_folder_manifest import (
    DriveMetadataScopeUnavailable,
    GoogleDriveFolderManifestError,
    GoogleDriveFolderManifest,
    GoogleDriveFolderManifestBatchResult,
    create_secure_google_drive_folder_handle,
    build_drive_folder_manifests_with_gateway,
)
from .media_source_discovery import MediaSourceDiscoveryResult
from .media_source_discovery_dry_run import (
    discover_from_secure_read_result,
    join_verified_sku,
    restore_verified_sku_entries,
    validate_sku_snapshot_compatibility,
)
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .secure_media_reference_read import (
    SecureMediaReferenceReadResult,
    SecureMediaReferenceReader,
    validate_mapping_report,
)
from .sheet_layout import validate_sheet_title


REPORT_FILENAME = "google-drive-folder-manifest-dry-run.json"
_UNSAFE_REPORT_PATTERN = re.compile(
    r"(?i)https?://|drive\.google\.com|webContentLink|webViewLink|"
    r"thumbnailLink|alt\s*=\s*media|get_media|"
    r'"resource_key"\s*:|"provider_resource_id"\s*:|raw_folder_id|'
    r"raw_file_id|(?:access_token|refresh_token|"
    r"client_secret|consumer_secret|signature|authorization|cookie|password)"
    r"\s*[:=]"
)
_SAFE_HANDLE_ERROR_CODES = frozenset(
    {
        "invalid_google_drive_folder_id",
        "verified_sku_required",
        "invalid_google_drive_folder_source",
    }
)


class GoogleDriveFolderManifestDryRunError(ValueError):
    """Safe orchestration/report error without provider identifiers."""


@dataclass(frozen=True, slots=True)
class RootDriveManifestRead:
    """Fresh Root domain results; never use a serialized report for traversal."""

    core_batch: GoogleDriveFolderManifestBatchResult
    blocked_results: tuple[dict[str, object], ...]
    sku_joined: int
    sku_join_not_found: int
    sku_join_ambiguous: int
    drive_read_requests_performed: int
    forbidden_values: tuple[str, ...] = field(repr=False)


def validate_drive_manifest_scopes(settings: GoogleSettings) -> None:
    """Fail before credentials/client construction unless both scopes are exact."""

    if (
        settings.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        or settings.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE
    ):
        raise DriveMetadataScopeUnavailable("drive_metadata_scope_unavailable")


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved = input_path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _assert_report_safe(
    report: Mapping[str, object],
    *,
    forbidden_values: Sequence[str] = (),
) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if REPORT_SECRET_SCAN_PATTERN.search(serialized) or _UNSAFE_REPORT_PATTERN.search(
        serialized
    ):
        raise GoogleDriveFolderManifestDryRunError("unsafe_drive_manifest_leak")
    for value in forbidden_values:
        if value and value in serialized:
            raise GoogleDriveFolderManifestDryRunError(
                "unsafe_drive_manifest_leak"
            )


def _item_report(item: object) -> dict[str, object]:
    return {
        "safe_name": item.safe_name,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "modified_time": item.modified_time,
        "provider_content_checksum": item.md5_checksum,
        "file_id_fingerprint": item.file_id_fingerprint,
        "item_kind": item.item_kind,
        "image_candidate": item.image_candidate,
        "image_width": item.image_width,
        "image_height": item.image_height,
        "warnings": list(item.warnings),
    }


def _manifest_report(
    manifest: GoogleDriveFolderManifest,
) -> dict[str, object]:
    items = tuple(manifest.items)
    warnings = list(manifest.warnings)
    if manifest.status == "empty_folder":
        warnings.append("empty_media_folder")
    return {
        "sku": manifest.sku,
        "product_source": manifest.product_source.to_dict(),
        "folder_id_fingerprint": manifest.folder_id_fingerprint,
        "status": manifest.status,
        "item_count": len(items),
        "image_candidate_count": sum(item.image_candidate for item in items),
        "nested_folder_count": sum(
            item.item_kind == "nested_folder" for item in items
        ),
        "shortcut_count": sum(item.item_kind == "shortcut" for item in items),
        "google_workspace_file_count": sum(
            item.item_kind == "google_workspace_file" for item in items
        ),
        "other_file_count": sum(
            item.item_kind == "other_file" for item in items
        ),
        "duplicate_name_candidate_count": sum(
            "duplicate_name_candidate" in item.warnings for item in items
        ),
        "duplicate_content_candidate_count": sum(
            "duplicate_content_candidate" in item.warnings for item in items
        ),
        "pages_read": manifest.pages_read,
        "items": [_item_report(item) for item in items],
        "warnings": list(_unique(warnings)),
        "blocking_issues": list(manifest.blocking_issues),
    }


def _blocked_result(
    read_result: SecureMediaReferenceReadResult,
    *,
    sku: str | None,
    discovery: MediaSourceDiscoveryResult | None,
    warnings: Sequence[str],
    blockers: Sequence[str],
) -> dict[str, object]:
    is_wrong_kind = discovery is not None and (
        discovery.provider != "google_drive"
        or discovery.resource_kind != "folder"
    )
    stable_blockers = list(blockers)
    if is_wrong_kind:
        stable_blockers.append("not_google_drive_folder")
    stable_blockers = list(_unique(stable_blockers))
    status = (
        "not_google_drive_folder"
        if is_wrong_kind
        else stable_blockers[0] if stable_blockers else "blocked"
    )
    return {
        "sku": sku,
        "product_source": read_result.mapped_source.product_source.to_dict(),
        "folder_id_fingerprint": (
            discovery.resource_id_fingerprint
            if discovery is not None
            and discovery.provider == "google_drive"
            and discovery.resource_kind == "folder"
            else None
        ),
        "status": status,
        "item_count": 0,
        "image_candidate_count": 0,
        "nested_folder_count": 0,
        "shortcut_count": 0,
        "google_workspace_file_count": 0,
        "other_file_count": 0,
        "duplicate_name_candidate_count": 0,
        "duplicate_content_candidate_count": 0,
        "pages_read": 0,
        "items": [],
        "warnings": list(_unique((*warnings, *stable_blockers))),
        "blocking_issues": stable_blockers,
    }


def _safe_handle_error_code(error: BaseException) -> str:
    if (
        isinstance(error, GoogleDriveFolderManifestError)
        and len(error.args) == 1
        and error.args[0] in _SAFE_HANDLE_ERROR_CODES
    ):
        return error.args[0]
    return "folder_handle_creation_failed"


def _empty_core_batch(gateway: GoogleDriveMetadataGateway) -> GoogleDriveFolderManifestBatchResult:
    return build_drive_folder_manifests_with_gateway((), gateway)


def read_root_drive_manifest_batch(
    read_results: Sequence[SecureMediaReferenceReadResult],
    sku_report: Mapping[str, object],
    *,
    gateway: GoogleDriveMetadataGateway,
) -> RootDriveManifestRead:
    """Join, discover, create handles, and retain the fresh Root Core objects."""

    sku_entries = restore_verified_sku_entries(sku_report)
    handles = []
    blocked_results: list[dict[str, object]] = []
    forbidden_values: list[str] = []
    sku_joined = 0
    sku_join_not_found = 0
    sku_join_ambiguous = 0

    for read_result in read_results:
        mapped = read_result.mapped_source
        if isinstance(read_result.raw_reference, str) and read_result.raw_reference:
            forbidden_values.append(read_result.raw_reference)
        sku_result, sku_warnings = join_verified_sku(
            mapped, sku_entries, sku_report_provided=True
        )
        sku_joined += sku_result is not None
        sku_join_not_found += "sku_join_not_found" in sku_warnings
        sku_join_ambiguous += "sku_join_ambiguous" in sku_warnings
        discovery = discover_from_secure_read_result(
            read_result,
            sku_result,
        )
        warnings = [*read_result.warnings, *sku_warnings]
        blockers = [*read_result.blocking_issues]
        if discovery is not None:
            warnings.extend(discovery.warnings)
            blockers.extend(discovery.blocking_issues)
            if discovery.provider_resource_id:
                forbidden_values.append(discovery.provider_resource_id)
            if discovery.resource_key:
                forbidden_values.append(discovery.resource_key)
        if sku_result is None:
            blockers.extend(sku_warnings or ("sku_not_verified",))
        if (
            discovery is not None
            and discovery.discovery_status == "classified"
            and discovery.provider == "google_drive"
            and discovery.resource_kind == "folder"
            and not blockers
            and sku_result is not None
        ):
            try:
                handle = create_secure_google_drive_folder_handle(
                    discovery, mapped.product_source
                )
            except Exception as error:
                error_code = _safe_handle_error_code(error)
                warnings.append(error_code)
                blockers.append(error_code)
            else:
                forbidden_values.append(handle.raw_folder_id)
                handles.append(handle)
                continue
        blocked_results.append(
            _blocked_result(
                read_result,
                sku=sku_result.sku if sku_result is not None else None,
                discovery=discovery,
                warnings=warnings,
                blockers=blockers,
            )
        )

    reads_before = gateway.counters.read_requests_performed
    core_batch = (
        build_drive_folder_manifests_with_gateway(handles, gateway)
        if handles
        else _empty_core_batch(gateway)
    )
    forbidden_values.extend(
        item.provider_file_id
        for manifest in core_batch.manifests
        for item in manifest.items
        if item.provider_file_id
    )
    return RootDriveManifestRead(
        core_batch=core_batch,
        blocked_results=tuple(blocked_results),
        sku_joined=sku_joined,
        sku_join_not_found=sku_join_not_found,
        sku_join_ambiguous=sku_join_ambiguous,
        drive_read_requests_performed=gateway.counters.read_requests_performed - reads_before,
        forbidden_values=tuple(forbidden_values),
    )


def build_drive_folder_manifest_report(
    read_results: Sequence[SecureMediaReferenceReadResult],
    sku_report: Mapping[str, object],
    *,
    mapping_input_file: str,
    sheet_title: str,
    sku_report_input_file: str,
    sheets_read_requests_performed: int,
    gateway: GoogleDriveMetadataGateway,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Project the shared in-memory Root read without changing the Root report."""

    root_read = read_root_drive_manifest_batch(read_results, sku_report, gateway=gateway)
    core_batch = root_read.core_batch
    results = [
        *(_manifest_report(item) for item in core_batch.manifests),
        *root_read.blocked_results,
    ]
    results.sort(
        key=lambda item: (
            item["product_source"]["start_row"],  # type: ignore[index]
            item["product_source"]["end_row"],  # type: ignore[index]
            item.get("sku") or "",
        )
    )
    core_summary = core_batch.summary.to_dict()
    drive_reads = root_read.drive_read_requests_performed
    summary = {
        **core_summary,
        "sku_joined": root_read.sku_joined,
        "sku_join_not_found": root_read.sku_join_not_found,
        "sku_join_ambiguous": root_read.sku_join_ambiguous,
        "sheets_read_requests_performed": sheets_read_requests_performed,
        "drive_read_requests_performed": drive_reads,
        "network_requests_performed": sheets_read_requests_performed + drive_reads,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    report: dict[str, object] = {
        "status": "ok",
        "inputs": {
            "mapping": mapping_input_file,
            "sheet": sheet_title,
            "sku_report": sku_report_input_file,
        },
        "summary": summary,
        "results": results,
    }
    _assert_report_safe(report, forbidden_values=root_read.forbidden_values)
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):
        raise TypeError("Drive folder manifest report must be an object")
    _assert_report_safe(sanitized, forbidden_values=root_read.forbidden_values)
    return sanitized


def run_drive_folder_manifest_dry_run(
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path,
    settings: GoogleSettings,
    client_factory: GoogleDriveMetadataAndSheetsClientFactory,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read exact cells, list first-level metadata, and write one safe report."""

    mapping_path = Path(mapping_input_path)
    sku_path = Path(sku_report_input_path)
    mapping_report = load_local_json_report(mapping_path)
    sku_report = load_local_json_report(sku_path)
    validate_sku_snapshot_compatibility(mapping_report, sku_report)
    validated_sheet = validate_sheet_title(sheet_title)
    mapped_sources = validate_mapping_report(mapping_report)
    validate_drive_manifest_scopes(settings)
    mapping_reference = _safe_input_reference(mapping_path, project_root)
    sku_reference = _safe_input_reference(sku_path, project_root)
    active_redactor = redactor or Redactor()

    if not mapped_sources:
        gateway = GoogleDriveMetadataGateway(object())
        report = build_drive_folder_manifest_report(
            (),
            sku_report,
            mapping_input_file=mapping_reference,
            sheet_title=validated_sheet,
            sku_report_input_file=sku_reference,
            sheets_read_requests_performed=0,
            gateway=gateway,
            redactor=active_redactor,
        )
    else:
        clients = client_factory.create_drive_metadata_clients(settings)
        read_batch = SecureMediaReferenceReader(
            settings, None, clients=clients
        ).run(mapping_report, sheet_title=validated_sheet)
        gateway = GoogleDriveMetadataGateway(clients.drive)
        report = build_drive_folder_manifest_report(
            read_batch.results,
            sku_report,
            mapping_input_file=mapping_reference,
            sheet_title=validated_sheet,
            sku_report_input_file=sku_reference,
            sheets_read_requests_performed=read_batch.read_requests_performed,
            gateway=gateway,
            redactor=active_redactor,
        )
    report_path = project_root / "reports" / REPORT_FILENAME
    _assert_report_safe(report)
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
