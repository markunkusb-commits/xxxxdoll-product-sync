"""Same-process one-item canary from fresh media handles to verified WebP.

The orchestration preserves the in-memory authority chain.  It never restores
download or conversion authority from a report or path.  WebP outputs are
cleaned before downloaded source bytes on every ordinary, exceptional, and
BaseException path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_media_download as download_core
from . import secure_selected_media_handle as handle_core
from . import verified_webp_conversion as conversion_core
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


POLICY_VERSION = "xxxxdoll-verified-webp-conversion-canary-v1"
REPORT_FILENAME = "verified-webp-conversion-canary.json"
_SKU_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_PREPARATION_SUMMARY_FIELDS = (
    "selected_items",
    "handles_prepared",
    "handles_blocked",
    "nested_handles",
    "depth2_handles",
    "primary_handles",
    "gallery_handles",
    "sheets_read_requests_performed",
    "root_drive_read_requests_performed",
    "depth1_drive_read_requests_performed",
    "depth2_drive_read_requests_performed",
    "network_requests_performed",
)
_DOWNLOAD_SUMMARY_FIELDS = (
    "handles_received",
    "downloads_attempted",
    "downloads_verified",
    "downloads_failed",
    "checksum_verified",
    "checksum_mismatch",
    "size_verified",
    "size_mismatch",
    "signature_verified",
    "signature_mismatch",
    "source_files_created",
    "download_requests_performed",
    "bytes_downloaded",
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "external_write_requests_performed",
    "source_files_cleaned",
    "authoritative_artifacts",
)
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


class VerifiedWebPConversionCanaryError(ValueError):
    """Fixed safe orchestration errors only."""


class CanaryGoogleClientFactory(Protocol):
    def create_drive_metadata_clients(self, settings: GoogleSettings) -> GoogleClients: ...

    def create_drive_content_readonly(self, settings: GoogleSettings) -> object: ...


def _zero_summary(fields: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(fields, 0)


def _target(sku: object, position: object) -> tuple[str, int]:
    if type(sku) is not str or _SKU_PATTERN.fullmatch(sku) is None:
        raise VerifiedWebPConversionCanaryError("invalid_webp_canary_sku")
    if type(position) is not int or position < 0:
        raise VerifiedWebPConversionCanaryError("invalid_webp_canary_position")
    return sku, position


def _compact_summary(
    report: Mapping[str, object],
    fields: tuple[str, ...],
    error_code: str,
) -> dict[str, int]:
    raw = report.get("summary")
    if not isinstance(raw, Mapping):
        raise VerifiedWebPConversionCanaryError(error_code)
    compact: dict[str, int] = {}
    for field in fields:
        value = raw.get(field)
        if type(value) is not int or value < 0:
            raise VerifiedWebPConversionCanaryError(error_code)
        compact[field] = value
    return compact


def _preparation_summary(
    preparation: SelectedMediaHandlePreparationResult,
) -> dict[str, int]:
    try:
        report = preparation.to_safe_report_dict()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise VerifiedWebPConversionCanaryError(
            "invalid_webp_canary_preparation_result"
        ) from None
    if not isinstance(report, Mapping):
        raise VerifiedWebPConversionCanaryError(
            "invalid_webp_canary_preparation_result"
        )
    return _compact_summary(
        report,
        _PREPARATION_SUMMARY_FIELDS,
        "invalid_webp_canary_preparation_result",
    )


def _empty_canary(sku: str, position: int) -> dict[str, object]:
    return {
        "sku": sku,
        "selection_position": position,
        "image_role": None,
        "folder_role": None,
        "safe_name": None,
        "source_mime_type": None,
        "source_size_bytes": None,
        "source_md5_checksum": None,
        "source_width": None,
        "source_height": None,
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
    }


def _canary_from_handle(
    handle: handle_core.SecureSelectedMediaHandle,
) -> dict[str, object]:
    return {
        **_empty_canary(handle.sku, handle.selection_position),
        "image_role": handle.image_role.value,
        "folder_role": handle.folder_role.value,
        "safe_name": handle.safe_name,
        "source_mime_type": handle.source_mime_type,
        "source_size_bytes": handle.size_bytes,
        "source_md5_checksum": handle.md5_checksum,
        "source_width": handle.image_width,
        "source_height": handle.image_height,
    }


def _project_download_artifact(
    canary: dict[str, object],
    artifact: download_core.VerifiedDownloadedMediaArtifact,
) -> None:
    canary["source_mime_type"] = artifact.source_mime_type
    canary["source_size_bytes"] = artifact.actual_size_bytes
    canary["source_md5_checksum"] = artifact.actual_md5_checksum
    canary["source_width"] = artifact.expected_image_width
    canary["source_height"] = artifact.expected_image_height


def _project_webp_artifact(
    canary: dict[str, object],
    artifact: conversion_core.VerifiedWebPArtifact,
) -> None:
    canary.update(
        {
            "conversion_action": artifact.conversion_action,
            "encoder_profile_version": artifact.encoder_profile_version,
            "output_mime_type": artifact.output_mime_type,
            "output_extension": artifact.output_extension,
            "output_size_bytes": artifact.output_size_bytes,
            "output_sha256": artifact.output_sha256,
            "output_width": artifact.image_width,
            "output_height": artifact.image_height,
            "compression_ratio": round(
                artifact.output_size_bytes / artifact.source_size_bytes, 8
            ),
            "webp_verified": artifact.webp_verified,
        }
    )


def _safe_report(
    *,
    status: str,
    canary: Mapping[str, object],
    preparation_summary: Mapping[str, int],
    download_summary: Mapping[str, int],
    conversion_summary: Mapping[str, int],
    source_cleanup_completed: bool,
    source_files_remaining: int,
    webp_cleanup_completed: bool,
    webp_files_remaining: int,
    retained_download_artifacts: int,
    retained_webp_artifacts: int,
    warnings: tuple[str, ...] = (),
    blocking_issues: tuple[str, ...] = (),
) -> dict[str, object]:
    download_requests = download_summary.get("download_requests_performed", 0)
    preparation_network = preparation_summary.get("network_requests_performed", 0)
    local_conversions = conversion_summary.get("conversion_requests_performed", 0)
    report = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "canary": dict(canary),
        "preparation_summary": dict(preparation_summary),
        "download_summary": dict(download_summary),
        "conversion_summary": dict(conversion_summary),
        "source_cleanup_completed": source_cleanup_completed,
        "source_files_remaining": source_files_remaining,
        "webp_cleanup_completed": webp_cleanup_completed,
        "webp_files_remaining": webp_files_remaining,
        "retained_download_artifacts": retained_download_artifacts,
        "retained_webp_artifacts": retained_webp_artifacts,
        "network_requests_performed": preparation_network + download_requests,
        "download_requests_performed": download_requests,
        "conversion_requests_performed": local_conversions,
        **_ZERO_ACTIVITY,
        "warnings": list(warnings),
        "blocking_issues": list(blocking_issues),
    }
    sanitized = sanitize_report_data(report, Redactor())
    drive_manifest_core._assert_report_safe(sanitized)
    return json.loads(json.dumps(sanitized, ensure_ascii=False))


def _blocked_report(
    sku: str,
    position: int,
    code: str,
    *,
    preparation_summary: Mapping[str, int] | None = None,
    canary: Mapping[str, object] | None = None,
    download_summary: Mapping[str, int] | None = None,
    conversion_summary: Mapping[str, int] | None = None,
    status: str = "blocked",
) -> dict[str, object]:
    return _safe_report(
        status=status,
        canary=_empty_canary(sku, position) if canary is None else canary,
        preparation_summary=(
            _zero_summary(_PREPARATION_SUMMARY_FIELDS)
            if preparation_summary is None
            else preparation_summary
        ),
        download_summary=(
            _zero_summary(_DOWNLOAD_SUMMARY_FIELDS)
            if download_summary is None
            else download_summary
        ),
        conversion_summary=(
            _zero_summary(_CONVERSION_SUMMARY_FIELDS)
            if conversion_summary is None
            else conversion_summary
        ),
        source_cleanup_completed=True,
        source_files_remaining=0,
        webp_cleanup_completed=True,
        webp_files_remaining=0,
        retained_download_artifacts=0,
        retained_webp_artifacts=0,
        blocking_issues=(code,),
    )


def _remaining_files(
    summary: Mapping[str, int],
    created_key: str,
    cleaned_key: str,
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


def execute_verified_webp_conversion_canary(
    preparation: SelectedMediaHandlePreparationResult,
    metadata_settings: GoogleSettings,
    client_factory: CanaryGoogleClientFactory,
    *,
    sku: str,
    position: int,
    workspace_parent: Path | None = None,
) -> dict[str, object]:
    """Download and convert one exact handle while retaining no authority."""

    target_sku, target_position = _target(sku, position)
    preparation_summary = _preparation_summary(preparation)
    if (
        metadata_settings.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        or metadata_settings.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE
    ):
        return _blocked_report(
            target_sku,
            target_position,
            "webp_canary_preparation_scope_mismatch",
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
            target_sku,
            target_position,
            "webp_canary_preparation_not_authoritative",
            preparation_summary=preparation_summary,
        )
    matches = tuple(
        handle
        for handle in handles
        if handle.sku == target_sku
        and handle.selection_position == target_position
    )
    if not matches:
        return _blocked_report(
            target_sku,
            target_position,
            "webp_canary_handle_not_found",
            preparation_summary=preparation_summary,
        )
    if len(matches) != 1:
        return _blocked_report(
            target_sku,
            target_position,
            "webp_canary_handle_ambiguous",
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
            target_sku,
            target_position,
            "webp_canary_content_client_creation_failed",
            preparation_summary=preparation_summary,
            canary=canary,
            status="failed",
        )

    gateway = GoogleDriveContentGateway(drive)
    download_result: download_core.SecureMediaDownloadBatchResult | None = None
    conversion_result: conversion_core.VerifiedWebPConversionBatchResult | None = None
    download_before: Mapping[str, object] = {}
    download_after: Mapping[str, object] = {}
    conversion_before: Mapping[str, object] = {}
    conversion_after: Mapping[str, object] = {}
    failure_code: str | None = None
    report_warnings = tuple(handle.warnings)
    try:
        download_result = download_core.download_secure_media(
            (handle,),
            gateway,
            workspace_parent=workspace_parent,
        )
        download_before = download_result.to_safe_report_dict()
        download_summary = _compact_summary(
            download_before,
            _DOWNLOAD_SUMMARY_FIELDS,
            "invalid_webp_canary_download_result",
        )
        download_artifacts = download_result.artifacts
        if (
            download_result.status != "ok"
            or len(download_artifacts) != 1
            or download_summary["handles_received"] != 1
            or download_summary["downloads_verified"] != 1
            or download_summary["checksum_verified"] != 1
            or download_summary["size_verified"] != 1
            or download_summary["signature_verified"] != 1
            or download_summary["authoritative_artifacts"] != 1
        ):
            failure_code = "webp_canary_download_not_verified"
        else:
            source_artifact = download_artifacts[0]
            _project_download_artifact(canary, source_artifact)
            conversion_result = conversion_core.convert_verified_media_to_webp(
                source_artifact,
                workspace_parent=workspace_parent,
            )
            conversion_before = conversion_result.to_safe_report_dict()
            conversion_summary = _compact_summary(
                conversion_before,
                _CONVERSION_SUMMARY_FIELDS,
                "invalid_webp_canary_conversion_result",
            )
            webp_artifacts = conversion_result.artifacts
            if (
                conversion_result.status != "ok"
                or len(webp_artifacts) != 1
                or conversion_summary["source_artifacts_received"] != 1
                or conversion_summary["conversion_attempted"] != 1
                or conversion_summary["conversion_verified"] != 1
                or conversion_summary["authoritative_webp_artifacts"] != 1
                or conversion_summary["dimension_verified"] != 1
                or conversion_summary["webp_signature_verified"] != 1
                or conversion_summary["webp_decode_verified"] != 1
            ):
                failure_code = "webp_canary_conversion_not_verified"
            else:
                webp_artifact = webp_artifacts[0]
                conversion_core._local_webp_path_for_upload(webp_artifact)
                _project_webp_artifact(canary, webp_artifact)
                report_warnings = tuple(
                    dict.fromkeys((*report_warnings, *webp_artifact.warnings))
                )
                if canary["source_mime_type"] in {"image/jpeg", "image/png"}:
                    expected_action = "convert_to_webp"
                    expected_profile = conversion_core.ENCODER_PROFILE_VERSION
                    expected_local_conversions = 1
                elif canary["source_mime_type"] == "image/webp":
                    expected_action = "validate_existing_webp"
                    expected_profile = conversion_core.EXISTING_WEBP_PROFILE_VERSION
                    expected_local_conversions = 0
                else:
                    expected_action = None
                    expected_profile = None
                    expected_local_conversions = -1
                if (
                    canary["output_mime_type"] != "image/webp"
                    or canary["output_extension"] != ".webp"
                    or canary["webp_verified"] is not True
                    or canary["conversion_action"] != expected_action
                    or canary["encoder_profile_version"] != expected_profile
                    or conversion_summary["conversion_requests_performed"]
                    != expected_local_conversions
                    or type(canary["output_size_bytes"]) is not int
                    or canary["output_size_bytes"] <= 0
                    or not conversion_core._valid_sha256(canary["output_sha256"])
                    or canary["source_width"] != canary["output_width"]
                    or canary["source_height"] != canary["output_height"]
                ):
                    failure_code = "webp_canary_output_not_verified"
    except Exception:
        failure_code = "webp_canary_execution_failed"
    finally:
        try:
            if conversion_result is not None:
                conversion_result.cleanup()
                conversion_after = conversion_result.to_safe_report_dict()
        finally:
            if download_result is not None:
                download_result.cleanup()
                download_after = download_result.to_safe_report_dict()

    try:
        download_summary = (
            _compact_summary(
                download_before,
                _DOWNLOAD_SUMMARY_FIELDS,
                "invalid_webp_canary_download_result",
            )
            if download_before
            else _zero_summary(_DOWNLOAD_SUMMARY_FIELDS)
        )
        conversion_summary = (
            _compact_summary(
                conversion_before,
                _CONVERSION_SUMMARY_FIELDS,
                "invalid_webp_canary_conversion_result",
            )
            if conversion_before
            else _zero_summary(_CONVERSION_SUMMARY_FIELDS)
        )
        download_cleanup_summary = (
            _compact_summary(
                download_after,
                _DOWNLOAD_SUMMARY_FIELDS,
                "invalid_webp_canary_download_cleanup_result",
            )
            if download_after
            else _zero_summary(_DOWNLOAD_SUMMARY_FIELDS)
        )
        conversion_cleanup_summary = (
            _compact_summary(
                conversion_after,
                _CONVERSION_SUMMARY_FIELDS,
                "invalid_webp_canary_conversion_cleanup_result",
            )
            if conversion_after
            else _zero_summary(_CONVERSION_SUMMARY_FIELDS)
        )
    except VerifiedWebPConversionCanaryError:
        failure_code = "webp_canary_audit_invalid"
        download_summary = _zero_summary(_DOWNLOAD_SUMMARY_FIELDS)
        conversion_summary = _zero_summary(_CONVERSION_SUMMARY_FIELDS)
        download_cleanup_summary = _zero_summary(_DOWNLOAD_SUMMARY_FIELDS)
        conversion_cleanup_summary = _zero_summary(_CONVERSION_SUMMARY_FIELDS)

    source_remaining = _remaining_files(
        download_cleanup_summary,
        "source_files_created",
        "source_files_cleaned",
    )
    webp_remaining = _remaining_files(
        conversion_cleanup_summary,
        "output_files_created",
        "output_files_cleaned",
    )
    retained_download = len(download_result.artifacts) if download_result is not None else 0
    retained_webp = len(conversion_result.artifacts) if conversion_result is not None else 0
    source_cleanup = source_remaining == 0 and retained_download == 0
    webp_cleanup = webp_remaining == 0 and retained_webp == 0
    if not webp_cleanup:
        failure_code = "webp_canary_webp_cleanup_incomplete"
    if not source_cleanup:
        failure_code = "webp_canary_source_cleanup_incomplete"

    return _safe_report(
        status="ok" if failure_code is None else "blocked",
        canary=canary,
        preparation_summary=preparation_summary,
        download_summary=download_summary,
        conversion_summary=conversion_summary,
        source_cleanup_completed=source_cleanup,
        source_files_remaining=source_remaining,
        webp_cleanup_completed=webp_cleanup,
        webp_files_remaining=webp_remaining,
        retained_download_artifacts=retained_download,
        retained_webp_artifacts=retained_webp,
        warnings=report_warnings,
        blocking_issues=() if failure_code is None else (failure_code,),
    )


def run_verified_webp_conversion_canary(
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
    """Fresh-prepare all handles, run one canary, clean, then write audit."""

    target_sku, target_position = _target(sku, position)
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
        report = execute_verified_webp_conversion_canary(
            preparation,
            metadata_settings,
            client_factory,
            sku=target_sku,
            position=target_position,
        )
    except Exception:
        report = _blocked_report(
            target_sku,
            target_position,
            "webp_canary_preparation_failed",
            status="failed",
        )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output, Redactor()).write(report)
    return report, output
