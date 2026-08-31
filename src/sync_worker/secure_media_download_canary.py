"""Same-process one-item canary for verified Google Drive media download."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_media_download as download_core
from . import secure_selected_media_handle as handle_core
from .config import (
    GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    GoogleSettings,
)
from .google_api import (
    GoogleClients,
    GoogleDriveContentGateway,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
    prepare_selected_media_handles,
)


POLICY_VERSION = "xxxxdoll-secure-media-download-canary-v1"
REPORT_FILENAME = "secure-media-download-canary.json"
_SKU_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_PREPARATION_SUMMARY_FIELDS = (
    "selected_items", "handles_prepared", "handles_blocked",
    "nested_handles", "depth2_handles", "primary_handles", "gallery_handles",
    "sheets_read_requests_performed", "root_drive_read_requests_performed",
    "depth1_drive_read_requests_performed", "depth2_drive_read_requests_performed",
    "network_requests_performed",
)
_DOWNLOAD_SUMMARY_FIELDS = (
    "handles_received", "downloads_attempted", "downloads_verified",
    "downloads_failed", "checksum_verified", "checksum_mismatch",
    "size_verified", "size_mismatch", "signature_verified",
    "signature_mismatch", "source_files_created",
    "download_requests_performed", "bytes_downloaded",
    "media_read_requests_performed", "conversion_requests_performed",
    "wordpress_upload_requests_performed", "external_write_requests_performed",
    "write_requests_performed", "source_files_cleaned",
    "authoritative_artifacts",
)
_ZERO_COUNTERS = {
    "media_read_requests_performed": 0,
    "conversion_requests_performed": 0,
    "wordpress_upload_requests_performed": 0,
    "external_write_requests_performed": 0,
    "write_requests_performed": 0,
}


class SecureMediaDownloadCanaryError(ValueError):
    """Fixed safe orchestration errors only."""


class CanaryGoogleClientFactory(Protocol):
    def create_drive_metadata_clients(self, settings: GoogleSettings) -> GoogleClients: ...

    def create_drive_content_readonly(self, settings: GoogleSettings) -> object: ...


def _zero_summary(fields: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(fields, 0)


def _target(sku: object, position: object) -> tuple[str, int]:
    if type(sku) is not str or _SKU_PATTERN.fullmatch(sku) is None:
        raise SecureMediaDownloadCanaryError("invalid_canary_sku")
    if type(position) is not int or position < 0:
        raise SecureMediaDownloadCanaryError("invalid_canary_position")
    return sku, position


def _compact_preparation_summary(
    preparation: SelectedMediaHandlePreparationResult,
) -> dict[str, int]:
    try:
        raw = preparation.to_safe_report_dict()["summary"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SecureMediaDownloadCanaryError("invalid_preparation_result") from None
    if not isinstance(raw, Mapping):
        raise SecureMediaDownloadCanaryError("invalid_preparation_result")
    compact: dict[str, int] = {}
    for field in _PREPARATION_SUMMARY_FIELDS:
        value = raw.get(field)
        if type(value) is not int or value < 0:
            raise SecureMediaDownloadCanaryError("invalid_preparation_result")
        compact[field] = value
    return compact


def _canary_from_handle(
    handle: handle_core.SecureSelectedMediaHandle,
) -> dict[str, object]:
    return {
        "sku": handle.sku,
        "selection_position": handle.selection_position,
        "image_role": handle.image_role.value,
        "folder_role": handle.folder_role.value,
        "safe_name": handle.safe_name,
        "file_id_fingerprint": handle.file_id_fingerprint,
        "source_mime_type": handle.source_mime_type,
        "expected_size_bytes": handle.size_bytes,
        "actual_size_bytes": None,
        "expected_md5_checksum": handle.md5_checksum,
        "actual_md5_checksum": None,
        "source_verified": False,
    }


def _empty_canary(sku: str, position: int) -> dict[str, object]:
    return {
        "sku": sku, "selection_position": position,
        "image_role": None, "folder_role": None, "safe_name": None,
        "file_id_fingerprint": None, "source_mime_type": None,
        "expected_size_bytes": None, "actual_size_bytes": None,
        "expected_md5_checksum": None, "actual_md5_checksum": None,
        "source_verified": False,
    }


def _project_safe_download_audit(
    canary: dict[str, object],
    download_report: Mapping[str, object],
) -> None:
    """Copy only fixed safe integrity facts from the selected Core audit."""

    results = download_report.get("results")
    if not isinstance(results, list) or len(results) != 1:
        return
    audit = results[0]
    if (
        not isinstance(audit, Mapping)
        or audit.get("sku") != canary["sku"]
        or audit.get("selection_position") != canary["selection_position"]
    ):
        return
    actual_size = audit.get("actual_size_bytes")
    if type(actual_size) is int and actual_size >= 0:
        canary["actual_size_bytes"] = actual_size
    actual_md5 = audit.get("actual_md5_checksum")
    if (
        type(actual_md5) is str
        and drive_manifest_core._MD5_PATTERN.fullmatch(actual_md5) is not None
    ):
        canary["actual_md5_checksum"] = actual_md5.casefold()
    canary["source_verified"] = audit.get("source_verified") is True


def _safe_report(
    *,
    status: str,
    canary: Mapping[str, object],
    preparation_summary: Mapping[str, int],
    download_summary: Mapping[str, int],
    cleanup_completed: bool,
    source_files_remaining: int,
    warnings: tuple[str, ...] = (),
    blocking_issues: tuple[str, ...] = (),
) -> dict[str, object]:
    preparation_network = preparation_summary.get("network_requests_performed", 0)
    download_requests = download_summary.get("download_requests_performed", 0)
    report = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "canary": dict(canary),
        "preparation_summary": dict(preparation_summary),
        "download_summary": dict(download_summary),
        "cleanup_completed": cleanup_completed,
        "source_files_remaining": source_files_remaining,
        "network_requests_performed": preparation_network + download_requests,
        "download_requests_performed": download_requests,
        **_ZERO_COUNTERS,
        "warnings": list(warnings),
        "blocking_issues": list(blocking_issues),
    }
    sanitized = sanitize_report_data(report, Redactor())
    drive_manifest_core._assert_report_safe(sanitized)
    return sanitized


def _blocked_report(
    sku: str,
    position: int,
    code: str,
    *,
    preparation_summary: Mapping[str, int] | None = None,
    canary: Mapping[str, object] | None = None,
    status: str = "blocked",
) -> dict[str, object]:
    return _safe_report(
        status=status,
        canary=_empty_canary(sku, position) if canary is None else canary,
        preparation_summary=(
            _zero_summary(_PREPARATION_SUMMARY_FIELDS)
            if preparation_summary is None else preparation_summary
        ),
        download_summary=_zero_summary(_DOWNLOAD_SUMMARY_FIELDS),
        cleanup_completed=True,
        source_files_remaining=0,
        blocking_issues=(code,),
    )


def execute_secure_media_download_canary(
    preparation: SelectedMediaHandlePreparationResult,
    metadata_settings: GoogleSettings,
    client_factory: CanaryGoogleClientFactory,
    *,
    sku: str,
    position: int,
    workspace_parent: Path | None = None,
) -> dict[str, object]:
    """Select exactly one in-memory handle, download, audit, and clean it."""

    target_sku, target_position = _target(sku, position)
    preparation_summary = _compact_preparation_summary(preparation)
    if (
        metadata_settings.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        or metadata_settings.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE
    ):
        return _blocked_report(
            target_sku, target_position,
            "canary_preparation_scope_mismatch",
            preparation_summary=preparation_summary,
        )
    handles = preparation.handles
    if (
        preparation.status != "ok"
        or not handles
        or preparation_summary["handles_blocked"] != 0
        or preparation_summary["handles_prepared"] != len(handles)
        or preparation_summary["selected_items"] != len(handles)
    ):
        return _blocked_report(
            target_sku, target_position,
            "canary_preparation_not_authoritative",
            preparation_summary=preparation_summary,
        )
    matches = tuple(
        handle for handle in handles
        if handle.sku == target_sku and handle.selection_position == target_position
    )
    if not matches:
        return _blocked_report(
            target_sku, target_position, "canary_handle_not_found",
            preparation_summary=preparation_summary,
        )
    if len(matches) != 1:
        return _blocked_report(
            target_sku, target_position, "canary_handle_ambiguous",
            preparation_summary=preparation_summary,
        )
    handle = matches[0]
    canary = _canary_from_handle(handle)
    content_settings = replace(
        metadata_settings,
        drive_scope=GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
        sheets_scope="",
    )
    try:
        drive = client_factory.create_drive_content_readonly(content_settings)
    except Exception:
        return _blocked_report(
            target_sku, target_position, "canary_content_client_creation_failed",
            preparation_summary=preparation_summary, canary=canary, status="failed",
        )
    gateway = GoogleDriveContentGateway(drive)
    download_result: download_core.SecureMediaDownloadBatchResult | None = None
    before_cleanup: dict[str, object] = {}
    after_cleanup: dict[str, object] = {}
    try:
        download_result = download_core.download_secure_media(
            (handle,), gateway, workspace_parent=workspace_parent,
        )
        before_cleanup = download_result.to_safe_report_dict()
        _project_safe_download_audit(canary, before_cleanup)
        artifacts = download_result.artifacts
        if download_result.status == "ok" and len(artifacts) == 1:
            artifact = artifacts[0].to_safe_dict()
            for field in (
                "actual_size_bytes", "actual_md5_checksum", "source_verified",
            ):
                canary[field] = artifact[field]
    except Exception:
        return _blocked_report(
            target_sku, target_position, "canary_download_execution_failed",
            preparation_summary=preparation_summary, canary=canary, status="failed",
        )
    finally:
        if download_result is not None:
            download_result.cleanup()
            after_cleanup = download_result.to_safe_report_dict()
    before_summary = before_cleanup.get("summary", {})
    after_summary = after_cleanup.get("summary", {})
    if not isinstance(before_summary, Mapping) or not isinstance(after_summary, Mapping):
        return _blocked_report(
            target_sku, target_position, "canary_download_audit_invalid",
            preparation_summary=preparation_summary, canary=canary, status="failed",
        )
    created = after_summary.get("source_files_created")
    cleaned = after_summary.get("source_files_cleaned")
    remaining = (
        created - cleaned
        if type(created) is int and type(cleaned) is int and created >= cleaned
        else 1
    )
    cleanup_completed = remaining == 0 and after_summary.get("authoritative_artifacts") == 0
    download_summary = {
        key: value for key, value in before_summary.items()
        if type(key) is str and type(value) is int
    }
    blockers = tuple(
        issue
        for result in before_cleanup.get("results", [])
        if isinstance(result, Mapping)
        for issue in result.get("blocking_issues", [])
        if type(issue) is str
    )
    if not cleanup_completed:
        blockers = (*blockers, "canary_cleanup_incomplete")
    ok = (
        before_cleanup.get("status") == "ok"
        and download_summary.get("handles_received") == 1
        and download_summary.get("downloads_verified") == 1
        and download_summary.get("authoritative_artifacts") == 1
        and canary["source_verified"] is True
        and cleanup_completed
    )
    return _safe_report(
        status="ok" if ok else "blocked",
        canary=canary,
        preparation_summary=preparation_summary,
        download_summary=download_summary,
        cleanup_completed=cleanup_completed,
        source_files_remaining=remaining,
        blocking_issues=() if ok else blockers or ("canary_download_not_verified",),
    )


def run_secure_media_download_canary(
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    sku: str,
    position: int,
    metadata_settings: GoogleSettings,
    client_factory: CanaryGoogleClientFactory,
    *,
    project_root: Path,
) -> tuple[dict[str, object], Path]:
    """Run full no-write Preparation, one canary download, cleanup, then report."""

    target_sku, target_position = _target(sku, position)
    try:
        preparation = prepare_selected_media_handles(
            selection_report_path, baseline_snapshot_path, mapping_path,
            sheet_title, sku_report_path, metadata_settings, client_factory,
        )
        report = execute_secure_media_download_canary(
            preparation, metadata_settings, client_factory,
            sku=target_sku, position=target_position,
        )
    except Exception:
        report = _blocked_report(
            target_sku, target_position, "canary_preparation_failed", status="failed",
        )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output, Redactor()).write(report)
    return report, output
