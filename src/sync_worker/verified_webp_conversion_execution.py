"""Full same-process download and verified WebP conversion execution.

Authority is never restored from reports.  Fresh preparation capabilities flow
directly through the existing full Download Core and full Conversion Core.
The public CLI-oriented runner audits the transient artifacts, cleans WebP
outputs first and downloaded sources second, and only then writes a safe JSON
report.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_media_download as download_core
from . import secure_media_download_execution as download_execution
from . import secure_selected_media_handle as handle_core
from . import verified_webp_conversion as conversion_core
from .config import GoogleSettings
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
    prepare_selected_media_handles,
)


POLICY_VERSION = "xxxxdoll-verified-webp-conversion-execution-v1"
REPORT_FILENAME = "verified-webp-conversion-execution.json"
_PREPARATION_SUMMARY_FIELDS = download_execution._PREPARATION_SUMMARY_FIELDS
_DOWNLOAD_SUMMARY_FIELDS = download_execution._DOWNLOAD_SUMMARY_FIELDS
_CONVERSION_SUMMARY_FIELDS = (
    "source_artifacts_received",
    "conversion_attempted",
    "conversion_verified",
    "conversion_failed",
    "converted_from_jpeg",
    "converted_from_png",
    "validated_existing_webp",
    "decode_verified",
    "decode_failed",
    "dimension_verified",
    "dimension_mismatch",
    "webp_signature_verified",
    "webp_signature_mismatch",
    "webp_decode_verified",
    "webp_decode_failed",
    "output_files_created",
    "output_files_cleaned",
    "source_total_bytes",
    "output_total_bytes",
    "authoritative_webp_artifacts",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "external_write_requests_performed",
)
_ZERO_ACTIVITY = {
    "wordpress_upload_requests_performed": 0,
    "external_write_requests_performed": 0,
    "write_requests_performed": 0,
}


class VerifiedWebPConversionExecutionError(ValueError):
    """Fixed-code batch execution failure without authority-bearing context."""

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


class ExecutionGoogleClientFactory(download_execution.ExecutionGoogleClientFactory, Protocol):
    """The same minimal factory contract used by Download Execution."""


class DiskUsageResult(Protocol):
    free: int


@dataclass(frozen=True, slots=True)
class CombinedWorkspacePreflight:
    expected_total_source_bytes: int
    maximum_webp_output_bytes: int
    required_capacity_bytes: int
    workspace_parent: Path = field(repr=False)

    def to_safe_dict(self) -> dict[str, int]:
        return {
            "expected_total_source_bytes": self.expected_total_source_bytes,
            "maximum_webp_output_bytes": self.maximum_webp_output_bytes,
            "safety_reserve_bytes": (
                download_execution.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
            ),
            "required_capacity_bytes": self.required_capacity_bytes,
        }


@dataclass(slots=True)
class VerifiedWebPConversionExecutionBatch:
    """Non-serializable transient authority for a future same-process upload."""

    preparation_summary: Mapping[str, int]
    preflight: CombinedWorkspacePreflight
    download_batch: download_execution.SecureMediaDownloadExecutionBatch = field(
        repr=False
    )
    conversion_result: conversion_core.VerifiedWebPConversionBatchResult | None = field(
        default=None, repr=False
    )

    @property
    def download_artifacts(
        self,
    ) -> tuple[download_core.VerifiedDownloadedMediaArtifact, ...]:
        return self.download_batch.download_result.artifacts

    @property
    def webp_artifacts(self) -> tuple[conversion_core.VerifiedWebPArtifact, ...]:
        if self.conversion_result is None:
            return ()
        return self.conversion_result.artifacts

    def cleanup(self) -> None:
        try:
            if self.conversion_result is not None:
                self.conversion_result.cleanup()
        finally:
            self.download_batch.cleanup()

    def __repr__(self) -> str:
        return (
            "VerifiedWebPConversionExecutionBatch("
            f"selected_items={self.preparation_summary.get('selected_items', 0)}, "
            f"webp_artifacts={len(self.webp_artifacts)})"
        )

    def __reduce__(self):
        raise TypeError("verified_webp_conversion_execution_batch_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("verified_webp_conversion_execution_batch_not_serializable")


def _zero_summary(fields: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(fields, 0)


def _compact_summary(
    report: Mapping[str, object],
    fields: tuple[str, ...],
    error_code: str,
) -> dict[str, int]:
    raw = report.get("summary")
    if not isinstance(raw, Mapping):
        raise VerifiedWebPConversionExecutionError(error_code, status="failed")
    summary: dict[str, int] = {}
    for field_name in fields:
        value = raw.get(field_name)
        if type(value) is not int or value < 0:
            raise VerifiedWebPConversionExecutionError(error_code, status="failed")
        summary[field_name] = value
    return summary


def preflight_webp_conversion_workspace(
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...],
    *,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
) -> CombinedWorkspacePreflight:
    """Reserve sources, a bounded output per item, and the existing safety margin."""

    try:
        source_preflight = download_execution.preflight_download_workspace(
            handles,
            workspace_parent=workspace_parent,
            disk_usage_reader=disk_usage_reader,
        )
    except download_execution.SecureMediaDownloadExecutionError as error:
        if error.code == "insufficient_download_workspace_capacity":
            code = "insufficient_webp_conversion_workspace_capacity"
        else:
            code = error.code
        raise VerifiedWebPConversionExecutionError(
            code,
            expected_total_source_bytes=error.expected_total_source_bytes,
        ) from None

    output_bound = len(handles) * conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES
    required = source_preflight.required_capacity_bytes + output_bound
    try:
        available = disk_usage_reader(source_preflight.workspace_parent).free
    except Exception:
        raise VerifiedWebPConversionExecutionError(
            "webp_conversion_workspace_capacity_unavailable",
            expected_total_source_bytes=source_preflight.expected_total_source_bytes,
        ) from None
    if type(available) is not int or available < 0:
        raise VerifiedWebPConversionExecutionError(
            "webp_conversion_workspace_capacity_unavailable",
            expected_total_source_bytes=source_preflight.expected_total_source_bytes,
        )
    if available < required:
        raise VerifiedWebPConversionExecutionError(
            "insufficient_webp_conversion_workspace_capacity",
            expected_total_source_bytes=source_preflight.expected_total_source_bytes,
        )
    return CombinedWorkspacePreflight(
        expected_total_source_bytes=source_preflight.expected_total_source_bytes,
        maximum_webp_output_bytes=output_bound,
        required_capacity_bytes=required,
        workspace_parent=source_preflight.workspace_parent,
    )


def execute_prepared_webp_conversion_batch(
    preparation: SelectedMediaHandlePreparationResult,
    metadata_settings: GoogleSettings,
    client_factory: ExecutionGoogleClientFactory,
    *,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
    download_progress_callback: download_core.DownloadProgressCallback | None = None,
    conversion_progress_callback: conversion_core.ConversionProgressCallback | None = None,
) -> VerifiedWebPConversionExecutionBatch:
    """Run exactly one full Download Core call and one full Conversion Core call."""

    if download_progress_callback is not None and not callable(download_progress_callback):
        raise VerifiedWebPConversionExecutionError(
            "invalid_webp_execution_download_progress_callback"
        )
    if conversion_progress_callback is not None and not callable(
        conversion_progress_callback
    ):
        raise VerifiedWebPConversionExecutionError(
            "invalid_webp_execution_conversion_progress_callback"
        )

    try:
        preparation_summary = download_execution._compact_preparation_summary(preparation)
        handles = download_execution._authoritative_handles(
            preparation, preparation_summary
        )
    except download_execution.SecureMediaDownloadExecutionError as error:
        raise VerifiedWebPConversionExecutionError(error.code) from None

    preflight = preflight_webp_conversion_workspace(
        handles,
        workspace_parent=workspace_parent,
        disk_usage_reader=disk_usage_reader,
    )

    def staged_download_progress(event: Mapping[str, object]) -> None:
        if download_progress_callback is None:
            return
        download_progress_callback({
            "current_index": event.get("current_index"),
            "total_items": event.get("total_items"),
            "sku": event.get("sku"),
            "selection_position": event.get("selection_position"),
            "stage": "download",
            "status": event.get("status"),
        })

    try:
        download_batch = download_execution.execute_prepared_media_download_batch(
            preparation,
            metadata_settings,
            client_factory,
            workspace_parent=preflight.workspace_parent,
            disk_usage_reader=disk_usage_reader,
            progress_callback=(
                staged_download_progress
                if download_progress_callback is not None
                else None
            ),
        )
    except download_execution.SecureMediaDownloadExecutionError as error:
        raise VerifiedWebPConversionExecutionError(
            error.code,
            expected_total_source_bytes=preflight.expected_total_source_bytes,
            status=error.status,
        ) from None

    conversion_result: conversion_core.VerifiedWebPConversionBatchResult | None = None
    try:
        download_report = download_batch.download_result.to_safe_report_dict()
        download_summary = _compact_summary(
            download_report,
            _DOWNLOAD_SUMMARY_FIELDS,
            "webp_execution_download_audit_invalid",
        )
        artifacts = download_batch.download_result.artifacts
        selected_items = preparation_summary["selected_items"]
        if (
            download_batch.download_result.status == "ok"
            and len(artifacts) == selected_items
            and download_summary["downloads_verified"] == selected_items
            and download_summary["downloads_failed"] == 0
            and download_summary["authoritative_artifacts"] == selected_items
        ):
            conversion_result = conversion_core.convert_verified_media_to_webp(
                artifacts,
                workspace_parent=preflight.workspace_parent,
                progress_callback=conversion_progress_callback,
            )
        return VerifiedWebPConversionExecutionBatch(
            preparation_summary=preparation_summary,
            preflight=preflight,
            download_batch=download_batch,
            conversion_result=conversion_result,
        )
    except BaseException:
        try:
            if conversion_result is not None:
                conversion_result.cleanup()
        finally:
            download_batch.cleanup()
        raise


def _remaining_files(
    summary: Mapping[str, int], created_key: str, cleaned_key: str
) -> int:
    created = summary.get(created_key)
    cleaned = summary.get(cleaned_key)
    if (
        type(created) is not int
        or type(cleaned) is not int
        or created < 0
        or cleaned < 0
        or cleaned > created
    ):
        return 1
    return created - cleaned


def _stable_messages(
    results: list[Mapping[str, object]], field_name: str
) -> tuple[str, ...]:
    messages: list[str] = []
    for result in results:
        raw = result.get(field_name)
        if not isinstance(raw, list):
            continue
        for value in raw:
            if type(value) is str and value not in messages:
                messages.append(value)
    return tuple(messages)


def _safe_results(
    download_report: Mapping[str, object],
    conversion_report: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    raw_download = download_report.get("results")
    if not isinstance(raw_download, list):
        raise VerifiedWebPConversionExecutionError(
            "webp_execution_download_audit_invalid", status="failed"
        )
    raw_conversion = (
        conversion_report.get("results", [])
        if conversion_report is not None
        else []
    )
    if not isinstance(raw_conversion, list):
        raise VerifiedWebPConversionExecutionError(
            "webp_execution_conversion_audit_invalid", status="failed"
        )
    conversion_by_key: dict[tuple[object, object], Mapping[str, object]] = {}
    for raw in raw_conversion:
        if not isinstance(raw, Mapping):
            raise VerifiedWebPConversionExecutionError(
                "webp_execution_conversion_audit_invalid", status="failed"
            )
        conversion_by_key[(raw.get("sku"), raw.get("selection_position"))] = raw

    results: list[dict[str, object]] = []
    for raw in raw_download:
        if not isinstance(raw, Mapping):
            raise VerifiedWebPConversionExecutionError(
                "webp_execution_download_audit_invalid", status="failed"
            )
        converted = conversion_by_key.get(
            (raw.get("sku"), raw.get("selection_position")), {}
        )
        source_size = raw.get("actual_size_bytes")
        output_size = converted.get("output_size_bytes")
        ratio = (
            round(output_size / source_size, 8)
            if type(source_size) is int
            and source_size > 0
            and type(output_size) is int
            and output_size >= 0
            else None
        )
        warnings = list(dict.fromkeys(
            value
            for values in (raw.get("warnings", []), converted.get("warnings", []))
            if isinstance(values, list)
            for value in values
            if type(value) is str
        ))
        blockers = list(dict.fromkeys(
            value
            for values in (
                raw.get("blocking_issues", []),
                converted.get("blocking_issues", []),
            )
            if isinstance(values, list)
            for value in values
            if type(value) is str
        ))
        results.append({
            "sku": raw.get("sku"),
            "selection_position": raw.get("selection_position"),
            "image_role": raw.get("image_role"),
            "folder_role": raw.get("folder_role"),
            "safe_name": raw.get("safe_name"),
            "source_mime_type": raw.get("source_mime_type"),
            "source_size_bytes": source_size,
            "source_md5_checksum": raw.get("actual_md5_checksum"),
            "source_width": raw.get("expected_image_width"),
            "source_height": raw.get("expected_image_height"),
            "conversion_action": converted.get("conversion_action"),
            "encoder_profile_version": converted.get("encoder_profile_version"),
            "output_mime_type": converted.get("output_mime_type"),
            "output_extension": converted.get("output_extension"),
            "output_size_bytes": output_size,
            "output_sha256": converted.get("output_sha256"),
            "output_width": converted.get("image_width"),
            "output_height": converted.get("image_height"),
            "compression_ratio": ratio,
            "webp_verified": converted.get("webp_verified", False),
            "conversion_status": converted.get("conversion_status", "not_attempted"),
            "warnings": warnings,
            "blocking_issues": blockers,
        })
    return results


def _safe_report(
    *,
    status: str,
    preparation_summary: Mapping[str, int],
    download_summary: Mapping[str, int],
    conversion_summary: Mapping[str, int],
    capacity_preflight: Mapping[str, int],
    results: list[Mapping[str, object]],
    source_cleanup_completed: bool,
    source_files_remaining: int,
    webp_cleanup_completed: bool,
    webp_files_remaining: int,
    retained_download_artifacts: int,
    retained_webp_artifacts: int,
    verified_webp_artifacts_before_cleanup: int,
    warnings: tuple[str, ...] = (),
    blocking_issues: tuple[str, ...] = (),
) -> dict[str, object]:
    source_total = conversion_summary.get("source_total_bytes", 0)
    if source_total == 0:
        source_total = sum(
            value
            for item in results
            for value in (item.get("source_size_bytes"),)
            if type(value) is int and value >= 0
        )
    output_total = conversion_summary.get("output_total_bytes", 0)
    compression_ratio = (
        round(output_total / source_total, 8) if source_total > 0 else None
    )
    download_requests = download_summary.get("download_requests_performed", 0)
    local_conversions = conversion_summary.get("conversion_requests_performed", 0)
    report = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "selected_items": preparation_summary.get("selected_items", 0),
        "capacity_preflight": dict(capacity_preflight),
        "preparation_summary": dict(preparation_summary),
        "download_summary": dict(download_summary),
        "conversion_summary": dict(conversion_summary),
        "source_total_bytes": source_total,
        "output_total_bytes": output_total,
        "compression_ratio": compression_ratio,
        "downloads_verified": download_summary.get("downloads_verified", 0),
        "downloads_failed": download_summary.get("downloads_failed", 0),
        "checksum_verified": download_summary.get("checksum_verified", 0),
        "checksum_mismatch": download_summary.get("checksum_mismatch", 0),
        "source_size_verified": download_summary.get("size_verified", 0),
        "source_size_mismatch": download_summary.get("size_mismatch", 0),
        "source_signature_verified": download_summary.get("signature_verified", 0),
        "source_signature_mismatch": download_summary.get("signature_mismatch", 0),
        "conversion_attempted": conversion_summary.get("conversion_attempted", 0),
        "conversion_verified": conversion_summary.get("conversion_verified", 0),
        "conversion_failed": conversion_summary.get("conversion_failed", 0),
        "converted_from_jpeg": conversion_summary.get("converted_from_jpeg", 0),
        "converted_from_png": conversion_summary.get("converted_from_png", 0),
        "validated_existing_webp": conversion_summary.get(
            "validated_existing_webp", 0
        ),
        "decode_verified": conversion_summary.get("decode_verified", 0),
        "decode_failed": conversion_summary.get("decode_failed", 0),
        "dimension_verified": conversion_summary.get("dimension_verified", 0),
        "dimension_mismatch": conversion_summary.get("dimension_mismatch", 0),
        "webp_signature_verified": conversion_summary.get(
            "webp_signature_verified", 0
        ),
        "webp_signature_mismatch": conversion_summary.get(
            "webp_signature_mismatch", 0
        ),
        "webp_decode_verified": conversion_summary.get("webp_decode_verified", 0),
        "webp_decode_failed": conversion_summary.get("webp_decode_failed", 0),
        "verified_webp_artifacts_before_cleanup": (
            verified_webp_artifacts_before_cleanup
        ),
        "source_cleanup_completed": source_cleanup_completed,
        "source_files_remaining": source_files_remaining,
        "webp_cleanup_completed": webp_cleanup_completed,
        "webp_files_remaining": webp_files_remaining,
        "retained_download_artifacts": retained_download_artifacts,
        "retained_webp_artifacts": retained_webp_artifacts,
        "network_requests_performed": (
            preparation_summary.get("network_requests_performed", 0)
            + download_requests
        ),
        "download_requests_performed": download_requests,
        "conversion_requests_performed": local_conversions,
        **_ZERO_ACTIVITY,
        "warnings": list(warnings),
        "blocking_issues": list(blocking_issues),
        "results": [dict(item) for item in results],
    }
    safe = sanitize_report_data(report, Redactor())
    drive_manifest_core._assert_report_safe(safe)
    return json.loads(json.dumps(safe, ensure_ascii=False))


def _safe_handle_results(
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...], code: str
) -> list[dict[str, object]]:
    return [{
        "sku": handle.sku,
        "selection_position": handle.selection_position,
        "image_role": handle.image_role.value,
        "folder_role": handle.folder_role.value,
        "safe_name": handle.safe_name,
        "source_mime_type": handle.source_mime_type,
        "source_size_bytes": None,
        "source_md5_checksum": None,
        "source_width": handle.image_width,
        "source_height": handle.image_height,
        "conversion_action": None,
        "encoder_profile_version": None,
        "output_mime_type": None,
        "output_extension": None,
        "output_size_bytes": None,
        "output_sha256": None,
        "output_width": None,
        "output_height": None,
        "compression_ratio": None,
        "webp_verified": False,
        "conversion_status": "not_attempted",
        "warnings": list(handle.warnings),
        "blocking_issues": [code],
    } for handle in handles if type(handle) is handle_core.SecureSelectedMediaHandle]


def _blocked_report(
    code: str,
    *,
    preparation_summary: Mapping[str, int] | None = None,
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...] = (),
    expected_total_source_bytes: int = 0,
    status: str = "blocked",
) -> dict[str, object]:
    capacity = {
        "expected_total_source_bytes": expected_total_source_bytes,
        "maximum_webp_output_bytes": 0,
        "safety_reserve_bytes": (
            download_execution.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
        ),
        "required_capacity_bytes": 0,
    }
    return _safe_report(
        status=status,
        preparation_summary=(
            _zero_summary(_PREPARATION_SUMMARY_FIELDS)
            if preparation_summary is None
            else preparation_summary
        ),
        download_summary=_zero_summary(_DOWNLOAD_SUMMARY_FIELDS),
        conversion_summary=_zero_summary(_CONVERSION_SUMMARY_FIELDS),
        capacity_preflight=capacity,
        results=_safe_handle_results(handles, code),
        source_cleanup_completed=True,
        source_files_remaining=0,
        webp_cleanup_completed=True,
        webp_files_remaining=0,
        retained_download_artifacts=0,
        retained_webp_artifacts=0,
        verified_webp_artifacts_before_cleanup=0,
        blocking_issues=(code,),
    )


def finalize_webp_conversion_execution(
    batch: VerifiedWebPConversionExecutionBatch,
) -> dict[str, object]:
    """Audit transient authorities, then clean WebP before source on every path."""

    download_before: Mapping[str, object]
    conversion_before: Mapping[str, object] | None = None
    try:
        download_before = batch.download_batch.download_result.to_safe_report_dict()
        if batch.conversion_result is not None:
            conversion_before = batch.conversion_result.to_safe_report_dict()
        results = _safe_results(download_before, conversion_before)
    finally:
        batch.cleanup()

    download_after = batch.download_batch.download_result.to_safe_report_dict()
    conversion_after = (
        batch.conversion_result.to_safe_report_dict()
        if batch.conversion_result is not None
        else None
    )
    download_summary = _compact_summary(
        download_before,
        _DOWNLOAD_SUMMARY_FIELDS,
        "webp_execution_download_audit_invalid",
    )
    download_cleanup_summary = _compact_summary(
        download_after,
        _DOWNLOAD_SUMMARY_FIELDS,
        "webp_execution_download_cleanup_audit_invalid",
    )
    conversion_summary = (
        _compact_summary(
            conversion_before,
            _CONVERSION_SUMMARY_FIELDS,
            "webp_execution_conversion_audit_invalid",
        )
        if conversion_before is not None
        else _zero_summary(_CONVERSION_SUMMARY_FIELDS)
    )
    conversion_cleanup_summary = (
        _compact_summary(
            conversion_after,
            _CONVERSION_SUMMARY_FIELDS,
            "webp_execution_conversion_cleanup_audit_invalid",
        )
        if conversion_after is not None
        else _zero_summary(_CONVERSION_SUMMARY_FIELDS)
    )

    source_remaining = _remaining_files(
        download_cleanup_summary, "source_files_created", "source_files_cleaned"
    )
    webp_remaining = _remaining_files(
        conversion_cleanup_summary, "output_files_created", "output_files_cleaned"
    )
    retained_download = len(batch.download_artifacts)
    retained_webp = len(batch.webp_artifacts)
    source_cleanup = source_remaining == 0 and retained_download == 0
    webp_cleanup = webp_remaining == 0 and retained_webp == 0
    verified_before = conversion_summary["authoritative_webp_artifacts"]
    selected = batch.preparation_summary["selected_items"]
    success = (
        download_before.get("status") == "ok"
        and conversion_before is not None
        and conversion_before.get("status") == "ok"
        and download_summary["downloads_verified"] == selected
        and download_summary["downloads_failed"] == 0
        and download_summary["authoritative_artifacts"] == selected
        and conversion_summary["source_artifacts_received"] == selected
        and conversion_summary["conversion_verified"] == selected
        and conversion_summary["conversion_failed"] == 0
        and verified_before == selected
        and source_cleanup
        and webp_cleanup
    )
    warnings = _stable_messages(results, "warnings")
    blockers = _stable_messages(results, "blocking_issues")
    if not webp_cleanup:
        blockers = (*blockers, "webp_execution_webp_cleanup_incomplete")
    if not source_cleanup:
        blockers = (*blockers, "webp_execution_source_cleanup_incomplete")
    if not success and not blockers:
        blockers = (
            "webp_execution_download_not_verified"
            if conversion_before is None
            else "webp_execution_conversion_not_verified",
        )
    reported_download_summary = dict(download_summary)
    reported_download_summary["source_files_cleaned"] = download_cleanup_summary[
        "source_files_cleaned"
    ]
    reported_conversion_summary = dict(conversion_summary)
    reported_conversion_summary["output_files_cleaned"] = conversion_cleanup_summary[
        "output_files_cleaned"
    ]
    return _safe_report(
        status="ok" if success else "blocked",
        preparation_summary=batch.preparation_summary,
        download_summary=reported_download_summary,
        conversion_summary=reported_conversion_summary,
        capacity_preflight=batch.preflight.to_safe_dict(),
        results=results,
        source_cleanup_completed=source_cleanup,
        source_files_remaining=source_remaining,
        webp_cleanup_completed=webp_cleanup,
        webp_files_remaining=webp_remaining,
        retained_download_artifacts=retained_download,
        retained_webp_artifacts=retained_webp,
        verified_webp_artifacts_before_cleanup=verified_before,
        warnings=warnings,
        blocking_issues=() if success else tuple(dict.fromkeys(blockers)),
    )


def run_verified_webp_conversion_execution(
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
    download_progress_callback: download_core.DownloadProgressCallback | None = None,
    conversion_progress_callback: conversion_core.ConversionProgressCallback | None = None,
) -> tuple[dict[str, object], Path]:
    """Fresh-prepare, download, convert, audit, cleanup, and write one report."""

    preparation_summary: Mapping[str, int] | None = None
    handles: tuple[handle_core.SecureSelectedMediaHandle, ...] = ()
    try:
        preparation = prepare_selected_media_handles(
            selection_report_path,
            baseline_snapshot_path,
            mapping_path,
            sheet_title,
            sku_report_path,
            metadata_settings,
            client_factory,
        )
        handles = preparation.handles
        preparation_summary = download_execution._compact_preparation_summary(preparation)
        batch = execute_prepared_webp_conversion_batch(
            preparation,
            metadata_settings,
            client_factory,
            workspace_parent=workspace_parent,
            disk_usage_reader=disk_usage_reader,
            download_progress_callback=download_progress_callback,
            conversion_progress_callback=conversion_progress_callback,
        )
        report = finalize_webp_conversion_execution(batch)
    except VerifiedWebPConversionExecutionError as error:
        report = _blocked_report(
            error.code,
            preparation_summary=preparation_summary,
            handles=handles,
            expected_total_source_bytes=error.expected_total_source_bytes,
            status=error.status,
        )
    except Exception:
        report = _blocked_report(
            "webp_execution_failed",
            preparation_summary=preparation_summary,
            handles=handles,
            status="failed",
        )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output, Redactor()).write(report)
    return report, output
