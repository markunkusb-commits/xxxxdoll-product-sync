"""Same-process full-batch source download execution with mandatory cleanup."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
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
from .google_api import GoogleClients, GoogleDriveContentGateway
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
    prepare_selected_media_handles,
)


POLICY_VERSION = "xxxxdoll-secure-media-download-execution-v1"
REPORT_FILENAME = "secure-media-download-execution.json"
DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES = 512 * 1024 * 1024
MAX_BATCH_SOURCE_BYTES = (
    download_core.MAX_HANDLES_PER_BATCH * download_core.MAX_SOURCE_FILE_BYTES
)
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
    "conversion_requests_performed",
    "wordpress_upload_requests_performed", "external_write_requests_performed",
    "source_files_cleaned", "authoritative_artifacts",
)
_RESULT_FIELDS = (
    "sku", "selection_position", "image_role", "folder_role", "safe_name",
    "file_id_fingerprint", "source_mime_type", "expected_size_bytes",
    "actual_size_bytes", "expected_md5_checksum", "actual_md5_checksum",
    "source_verified", "download_status", "warnings", "blocking_issues",
)
_ZERO_EXTERNAL_COUNTERS = {
    "media_read_requests_performed": 0,
    "conversion_requests_performed": 0,
    "wordpress_upload_requests_performed": 0,
    "external_write_requests_performed": 0,
    "write_requests_performed": 0,
}


class SecureMediaDownloadExecutionError(ValueError):
    """Fixed-code execution failure with optional safe expected-byte context."""

    __slots__ = ("code", "expected_total_source_bytes", "status")

    def __init__(
        self,
        code: str,
        *,
        expected_total_source_bytes: int = 0,
        status: str = "blocked",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.expected_total_source_bytes = expected_total_source_bytes
        self.status = status


class ExecutionGoogleClientFactory(Protocol):
    def create_drive_metadata_clients(self, settings: GoogleSettings) -> GoogleClients: ...

    def create_drive_content_readonly(self, settings: GoogleSettings) -> object: ...


class DiskUsageResult(Protocol):
    free: int


@dataclass(frozen=True, slots=True)
class DownloadWorkspacePreflight:
    expected_total_source_bytes: int
    required_capacity_bytes: int
    workspace_parent: Path = field(repr=False)


@dataclass(slots=True)
class SecureMediaDownloadExecutionBatch:
    """Non-serializable in-process capability for future conversion chaining."""

    preparation_summary: Mapping[str, int]
    preflight: DownloadWorkspacePreflight
    download_result: download_core.SecureMediaDownloadBatchResult = field(repr=False)

    def cleanup(self) -> None:
        self.download_result.cleanup()

    def __repr__(self) -> str:
        return (
            "SecureMediaDownloadExecutionBatch("
            f"selected_items={self.preparation_summary.get('selected_items', 0)}, "
            f"status={self.download_result.status!r})"
        )

    def __reduce__(self):
        raise TypeError("secure_media_download_execution_batch_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("secure_media_download_execution_batch_not_serializable")


def _zero_summary(fields: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(fields, 0)


def _compact_preparation_summary(
    preparation: SelectedMediaHandlePreparationResult,
) -> dict[str, int]:
    try:
        raw = preparation.to_safe_report_dict()["summary"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SecureMediaDownloadExecutionError("invalid_preparation_result") from None
    if not isinstance(raw, Mapping):
        raise SecureMediaDownloadExecutionError("invalid_preparation_result")
    compact: dict[str, int] = {}
    for field_name in _PREPARATION_SUMMARY_FIELDS:
        value = raw.get(field_name)
        if type(value) is not int or value < 0:
            raise SecureMediaDownloadExecutionError("invalid_preparation_result")
        compact[field_name] = value
    return compact


def _authoritative_handles(
    preparation: SelectedMediaHandlePreparationResult,
    summary: Mapping[str, int],
) -> tuple[handle_core.SecureSelectedMediaHandle, ...]:
    handles = preparation.handles
    if (
        preparation.status != "ok"
        or not handles
        or summary["handles_blocked"] != 0
        or summary["handles_prepared"] != len(handles)
        or summary["selected_items"] != len(handles)
    ):
        raise SecureMediaDownloadExecutionError(
            "execution_preparation_not_authoritative"
        )
    if len(handles) > download_core.MAX_HANDLES_PER_BATCH:
        raise SecureMediaDownloadExecutionError("download_batch_handle_limit_exceeded")
    return handles


def preflight_download_workspace(
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...],
    *,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
) -> DownloadWorkspacePreflight:
    """Validate sizes, canonical order, and capacity before content authority."""

    if not handles:
        raise SecureMediaDownloadExecutionError("secure_selected_media_handles_required")
    if len(handles) > download_core.MAX_HANDLES_PER_BATCH:
        raise SecureMediaDownloadExecutionError("download_batch_handle_limit_exceeded")
    total = 0
    keys: list[tuple[str, int]] = []
    for handle in handles:
        if type(handle) is not handle_core.SecureSelectedMediaHandle:
            raise SecureMediaDownloadExecutionError("secure_selected_media_handles_required")
        size = handle.size_bytes
        if size is None:
            raise SecureMediaDownloadExecutionError("download_preflight_size_missing")
        if type(size) is not int or size <= 0:
            raise SecureMediaDownloadExecutionError("download_preflight_size_invalid")
        if size > download_core.MAX_SOURCE_FILE_BYTES:
            raise SecureMediaDownloadExecutionError("download_preflight_file_too_large")
        if not handle_core._valid_handle(handle):
            raise SecureMediaDownloadExecutionError("secure_selected_media_handles_required")
        total += size
        keys.append((handle.sku, handle.selection_position))
    stable_keys = tuple(keys)
    if stable_keys != tuple(sorted(stable_keys)) or len(set(stable_keys)) != len(stable_keys):
        raise SecureMediaDownloadExecutionError("download_handles_not_canonical_order")
    if total > MAX_BATCH_SOURCE_BYTES:
        raise SecureMediaDownloadExecutionError(
            "download_batch_source_bytes_limit_exceeded",
            expected_total_source_bytes=total,
        )
    parent = Path(tempfile.gettempdir()) if workspace_parent is None else Path(workspace_parent)
    try:
        validated_parent = download_core._safe_workspace_parent(parent)
    except Exception:
        raise SecureMediaDownloadExecutionError(
            "download_workspace_parent_invalid",
            expected_total_source_bytes=total,
        ) from None
    if validated_parent is None:
        raise SecureMediaDownloadExecutionError(
            "download_workspace_parent_invalid",
            expected_total_source_bytes=total,
        )
    required = total + DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
    try:
        usage = disk_usage_reader(validated_parent)
        available = usage.free
    except Exception:
        raise SecureMediaDownloadExecutionError(
            "download_workspace_capacity_unavailable",
            expected_total_source_bytes=total,
        ) from None
    if type(available) is not int or available < 0:
        raise SecureMediaDownloadExecutionError(
            "download_workspace_capacity_unavailable",
            expected_total_source_bytes=total,
        )
    if available < required:
        raise SecureMediaDownloadExecutionError(
            "insufficient_download_workspace_capacity",
            expected_total_source_bytes=total,
        )
    return DownloadWorkspacePreflight(total, required, validated_parent)


def execute_prepared_media_download_batch(
    preparation: SelectedMediaHandlePreparationResult,
    metadata_settings: GoogleSettings,
    client_factory: ExecutionGoogleClientFactory,
    *,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
    progress_callback: download_core.DownloadProgressCallback | None = None,
) -> SecureMediaDownloadExecutionBatch:
    """Create a content-only client and return the uncleaned in-process batch."""

    summary = _compact_preparation_summary(preparation)
    handles = _authoritative_handles(preparation, summary)
    if (
        metadata_settings.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        or metadata_settings.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE
    ):
        raise SecureMediaDownloadExecutionError("execution_preparation_scope_mismatch")
    preflight = preflight_download_workspace(
        handles,
        workspace_parent=workspace_parent,
        disk_usage_reader=disk_usage_reader,
    )
    content_settings = replace(
        metadata_settings,
        drive_scope=GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
        sheets_scope="",
    )
    try:
        drive = client_factory.create_drive_content_readonly(content_settings)
    except Exception:
        raise SecureMediaDownloadExecutionError(
            "execution_content_client_creation_failed",
            expected_total_source_bytes=preflight.expected_total_source_bytes,
            status="failed",
        ) from None
    gateway = GoogleDriveContentGateway(drive)
    try:
        result = download_core.download_secure_media(
            handles,
            gateway,
            workspace_parent=preflight.workspace_parent,
            progress_callback=progress_callback,
        )
    except Exception:
        raise SecureMediaDownloadExecutionError(
            "execution_download_core_failed",
            expected_total_source_bytes=preflight.expected_total_source_bytes,
            status="failed",
        ) from None
    return SecureMediaDownloadExecutionBatch(summary, preflight, result)


def _safe_handle_results(
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...],
    code: str,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for handle in handles:
        if type(handle) is not handle_core.SecureSelectedMediaHandle:
            continue
        expected_size = handle.size_bytes
        if type(expected_size) is not int or expected_size <= 0:
            expected_size = None
        results.append({
            "sku": handle.sku,
            "selection_position": handle.selection_position,
            "image_role": handle.image_role.value,
            "folder_role": handle.folder_role.value,
            "safe_name": handle.safe_name,
            "file_id_fingerprint": handle.file_id_fingerprint,
            "source_mime_type": handle.source_mime_type,
            "expected_size_bytes": expected_size,
            "actual_size_bytes": None,
            "expected_md5_checksum": handle.md5_checksum,
            "actual_md5_checksum": None,
            "source_verified": False,
            "download_status": "not_attempted",
            "warnings": list(handle.warnings),
            "blocking_issues": [code],
        })
    return results


def _safe_core_results(raw_results: object) -> list[dict[str, object]]:
    if not isinstance(raw_results, list):
        raise SecureMediaDownloadExecutionError(
            "execution_download_audit_invalid", status="failed"
        )
    results: list[dict[str, object]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise SecureMediaDownloadExecutionError(
                "execution_download_audit_invalid", status="failed"
            )
        results.append({field_name: raw.get(field_name) for field_name in _RESULT_FIELDS})
    return results


def _int_summary(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise SecureMediaDownloadExecutionError(
            "execution_download_audit_invalid", status="failed"
        )
    summary: dict[str, int] = {}
    for field_name in _DOWNLOAD_SUMMARY_FIELDS:
        value = raw.get(field_name)
        if type(value) is not int or value < 0:
            raise SecureMediaDownloadExecutionError(
                "execution_download_audit_invalid", status="failed"
            )
        summary[field_name] = value
    return summary


def _stable_messages(results: list[Mapping[str, object]], field_name: str) -> tuple[str, ...]:
    values: list[str] = []
    for result in results:
        raw_values = result.get(field_name, [])
        if not isinstance(raw_values, list):
            continue
        for value in raw_values:
            if type(value) is str and value not in values:
                values.append(value)
    return tuple(values)


def _safe_report(
    *,
    status: str,
    preparation_summary: Mapping[str, int],
    download_summary: Mapping[str, int],
    expected_total_source_bytes: int,
    actual_total_source_bytes: int,
    verified_artifacts_before_cleanup: int,
    cleanup_completed: bool,
    source_files_remaining: int,
    retained_authoritative_artifacts: int,
    results: list[Mapping[str, object]],
    warnings: tuple[str, ...] = (),
    blocking_issues: tuple[str, ...] = (),
) -> dict[str, object]:
    selected_items = preparation_summary.get("selected_items", 0)
    download_requests = download_summary.get("download_requests_performed", 0)
    report = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "preparation_summary": dict(preparation_summary),
        "download_summary": dict(download_summary),
        "expected_total_source_bytes": expected_total_source_bytes,
        "actual_total_source_bytes": actual_total_source_bytes,
        "selected_items": selected_items,
        "downloads_verified": download_summary.get("downloads_verified", 0),
        "downloads_failed": download_summary.get("downloads_failed", 0),
        "checksum_verified": download_summary.get("checksum_verified", 0),
        "checksum_mismatch": download_summary.get("checksum_mismatch", 0),
        "size_verified": download_summary.get("size_verified", 0),
        "size_mismatch": download_summary.get("size_mismatch", 0),
        "signature_verified": download_summary.get("signature_verified", 0),
        "signature_mismatch": download_summary.get("signature_mismatch", 0),
        "verified_artifacts_before_cleanup": verified_artifacts_before_cleanup,
        "cleanup_completed": cleanup_completed,
        "source_files_remaining": source_files_remaining,
        "retained_authoritative_artifacts": retained_authoritative_artifacts,
        "network_requests_performed": (
            preparation_summary.get("network_requests_performed", 0)
            + download_requests
        ),
        "download_requests_performed": download_requests,
        **_ZERO_EXTERNAL_COUNTERS,
        "warnings": list(warnings),
        "blocking_issues": list(blocking_issues),
        "results": [dict(result) for result in results],
    }
    sanitized = sanitize_report_data(report, Redactor())
    drive_manifest_core._assert_report_safe(sanitized)
    return json.loads(json.dumps(sanitized, ensure_ascii=False))


def _blocked_report(
    code: str,
    *,
    preparation_summary: Mapping[str, int] | None = None,
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...] = (),
    expected_total_source_bytes: int = 0,
    status: str = "blocked",
) -> dict[str, object]:
    return _safe_report(
        status=status,
        preparation_summary=(
            _zero_summary(_PREPARATION_SUMMARY_FIELDS)
            if preparation_summary is None else preparation_summary
        ),
        download_summary=_zero_summary(_DOWNLOAD_SUMMARY_FIELDS),
        expected_total_source_bytes=expected_total_source_bytes,
        actual_total_source_bytes=0,
        verified_artifacts_before_cleanup=0,
        cleanup_completed=True,
        source_files_remaining=0,
        retained_authoritative_artifacts=0,
        results=_safe_handle_results(handles, code),
        blocking_issues=(code,),
    )


def finalize_media_download_execution(
    batch: SecureMediaDownloadExecutionBatch,
) -> dict[str, object]:
    """Capture the safe Core audit and always clean every source before return."""

    before_cleanup: dict[str, object]
    try:
        before_cleanup = batch.download_result.to_safe_report_dict()
    finally:
        batch.cleanup()
    after_cleanup = batch.download_result.to_safe_report_dict()
    before_summary = _int_summary(before_cleanup.get("summary"))
    after_summary = _int_summary(after_cleanup.get("summary"))
    results = _safe_core_results(before_cleanup.get("results"))
    created = after_summary["source_files_created"]
    cleaned = after_summary["source_files_cleaned"]
    remaining = created - cleaned if created >= cleaned else 1
    retained = after_summary["authoritative_artifacts"]
    cleanup_completed = remaining == 0 and retained == 0
    verified_before = before_summary["authoritative_artifacts"]
    actual_total = sum(
        value
        for result in results
        for value in (result.get("actual_size_bytes"),)
        if type(value) is int and value >= 0
    )
    download_summary = dict(before_summary)
    download_summary["source_files_cleaned"] = cleaned
    warnings = _stable_messages(results, "warnings")
    blockers = _stable_messages(results, "blocking_issues")
    if not cleanup_completed:
        blockers = (*blockers, "execution_cleanup_incomplete")
    selected_items = batch.preparation_summary["selected_items"]
    success = (
        before_cleanup.get("status") == "ok"
        and before_summary["handles_received"] == selected_items
        and before_summary["downloads_verified"] == selected_items
        and before_summary["downloads_failed"] == 0
        and verified_before == selected_items
        and cleanup_completed
    )
    if not success and not blockers:
        blockers = ("execution_download_not_verified",)
    return _safe_report(
        status="ok" if success else "blocked",
        preparation_summary=batch.preparation_summary,
        download_summary=download_summary,
        expected_total_source_bytes=batch.preflight.expected_total_source_bytes,
        actual_total_source_bytes=actual_total,
        verified_artifacts_before_cleanup=verified_before,
        cleanup_completed=cleanup_completed,
        source_files_remaining=remaining,
        retained_authoritative_artifacts=retained,
        results=results,
        warnings=warnings,
        blocking_issues=() if success else blockers,
    )


def run_secure_media_download_execution(
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    metadata_settings: GoogleSettings,
    client_factory: ExecutionGoogleClientFactory,
    *,
    project_root: Path,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
    progress_callback: download_core.DownloadProgressCallback | None = None,
) -> tuple[dict[str, object], Path]:
    """Prepare, download the full approved batch, audit, cleanup, and report."""

    preparation: SelectedMediaHandlePreparationResult | None = None
    preparation_summary: Mapping[str, int] | None = None
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...] = ()
    try:
        preparation = prepare_selected_media_handles(
            selection_report_path, baseline_snapshot_path, mapping_path,
            sheet_title, sku_report_path, metadata_settings, client_factory,
        )
        handles = preparation.handles
        preparation_summary = _compact_preparation_summary(preparation)
        batch = execute_prepared_media_download_batch(
            preparation,
            metadata_settings,
            client_factory,
            workspace_parent=workspace_parent,
            disk_usage_reader=disk_usage_reader,
            progress_callback=progress_callback,
        )
        report = finalize_media_download_execution(batch)
    except SecureMediaDownloadExecutionError as error:
        report = _blocked_report(
            error.code,
            preparation_summary=preparation_summary,
            handles=handles,
            expected_total_source_bytes=error.expected_total_source_bytes,
            status=error.status,
        )
    except Exception:
        report = _blocked_report(
            "execution_preparation_failed",
            preparation_summary=preparation_summary,
            handles=handles,
            status="failed",
        )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output, Redactor()).write(report)
    return report, output
