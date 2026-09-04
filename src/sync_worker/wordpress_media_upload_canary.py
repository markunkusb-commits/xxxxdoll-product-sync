"""One-item, explicitly confirmed staging WordPress media upload canary.

Every authority object is created and consumed in one process.  Reports are
audit projections only and can never be used to restore download, conversion,
gate, write-permit, credential, or upload authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Protocol

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_media_download as download_core
from . import verified_webp_conversion as conversion_core
from . import wordpress_media_upload_gate as gate_core
from . import wordpress_media_upload_transport as transport_core
from .config import (
    GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    GoogleSettings,
    Settings,
)
from .google_api import GoogleClients, GoogleDriveContentGateway
from .report import SafeWriteAuditJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
    prepare_selected_media_handles,
)


POLICY_VERSION = "xxxxdoll-wordpress-media-upload-canary-v1"
REPORT_FILENAME = "wordpress-media-upload-canary.json"
EXACT_CONFIRMATION_TOKEN = "I_CONFIRM_ONE_STAGING_MEDIA_UPLOAD"
_SKU_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_UPLOAD_STATUSES = frozenset({"created", "reused", "created_reconciled"})
_SAFE_PROGRESS_STATUSES = frozenset(
    {
        "download_started",
        "download_verified",
        "conversion_started",
        "conversion_verified",
        "gate_verified",
        "lookup_started",
        "lookup_reused",
        "upload_started",
        "upload_created",
        "upload_reconciled",
        "upload_blocked",
        "cleanup_completed",
    }
)
_CORE_BLOCKED_PROGRESS_STATUSES = frozenset(
    {"download_blocked", "conversion_blocked"}
)
_PREPARATION_FIELDS = (
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
_DOWNLOAD_FIELDS = (
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
_CONVERSION_FIELDS = (
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
_GATE_FIELDS = (
    "artifacts_received",
    "gate_passed",
    "gate_blocked",
    "intents_created",
)
_TRANSPORT_FIELDS = (
    "intents_received",
    "lookup_requests_performed",
    "reconciliation_requests_performed",
    "network_requests_performed",
    "wordpress_upload_requests_performed",
    "external_write_requests_performed",
    "write_requests_performed",
    "remote_media_created",
    "remote_media_reused",
    "created",
    "reused",
    "created_reconciled",
    "failed_at_index",
    "references_created",
    "delete_requests_performed",
    "rollback_requests_performed",
)


class WordPressMediaUploadCanaryError(ValueError):
    """Fixed-code Canary error with no credential or request context."""


class CanaryGoogleClientFactory(Protocol):
    def create_drive_metadata_clients(self, settings: GoogleSettings) -> GoogleClients: ...

    def create_drive_content_readonly(self, settings: GoogleSettings) -> object: ...


ProgressCallback = Callable[[Mapping[str, object]], None]


def validate_staging_media_upload_confirmation(value: object) -> None:
    if type(value) is not str or value != EXACT_CONFIRMATION_TOKEN:
        raise WordPressMediaUploadCanaryError(
            "staging_media_upload_confirmation_required"
        )


def _target(sku: object, position: object) -> tuple[str, int]:
    if type(sku) is not str or _SKU_PATTERN.fullmatch(sku) is None:
        raise WordPressMediaUploadCanaryError("invalid_wordpress_media_canary_sku")
    if type(position) is not int or position < 0:
        raise WordPressMediaUploadCanaryError(
            "invalid_wordpress_media_canary_position"
        )
    return sku, position


def _validate_staging_target(settings: Settings) -> str:
    try:
        return gate_core._target_binding(settings)
    except gate_core.WordPressMediaUploadGateError:
        raise WordPressMediaUploadCanaryError(
            "wordpress_media_canary_staging_safety_failed"
        ) from None


def _zero(fields: tuple[str, ...]) -> dict[str, int | None]:
    result: dict[str, int | None] = dict.fromkeys(fields, 0)
    if "failed_at_index" in result:
        result["failed_at_index"] = None
    return result


def _compact(
    report: Mapping[str, object],
    fields: tuple[str, ...],
    code: str,
) -> dict[str, int | None]:
    value = report.get("summary")
    if not isinstance(value, Mapping):
        raise WordPressMediaUploadCanaryError(code)
    result: dict[str, int | None] = {}
    for field in fields:
        item = value.get(field)
        if field == "failed_at_index" and item is None:
            result[field] = None
        elif type(item) is not int or item < 0:
            raise WordPressMediaUploadCanaryError(code)
        else:
            result[field] = item
    return result


def _preparation_summary(
    preparation: SelectedMediaHandlePreparationResult,
) -> dict[str, int | None]:
    try:
        report = preparation.to_safe_report_dict()
    except Exception:
        raise WordPressMediaUploadCanaryError(
            "invalid_wordpress_media_canary_preparation_result"
        ) from None
    if not isinstance(report, Mapping):
        raise WordPressMediaUploadCanaryError(
            "invalid_wordpress_media_canary_preparation_result"
        )
    return _compact(
        report,
        _PREPARATION_FIELDS,
        "invalid_wordpress_media_canary_preparation_result",
    )


def _empty_canary(sku: str, position: int) -> dict[str, object]:
    return {
        "sku": sku,
        "selection_position": position,
        "image_role": None,
        "source_mime_type": None,
        "source_size_bytes": None,
        "source_width": None,
        "source_height": None,
        "output_mime_type": None,
        "output_extension": None,
        "output_size_bytes": None,
        "output_sha256": None,
        "output_width": None,
        "output_height": None,
        "media_identity": None,
        "upload_filename": None,
        "wordpress_slug": None,
        "upload_status": "blocked",
        "wordpress_media_id": None,
    }


def _safe_report(
    *,
    status: str,
    canary: Mapping[str, object],
    preparation_summary: Mapping[str, int | None],
    download_summary: Mapping[str, int | None],
    conversion_summary: Mapping[str, int | None],
    gate_summary: Mapping[str, int | None],
    transport_summary: Mapping[str, int | None],
    webp_cleanup_completed: bool,
    webp_files_remaining: int,
    source_cleanup_completed: bool,
    source_files_remaining: int,
    retained_webp_artifacts: int,
    retained_download_artifacts: int,
    warnings: tuple[str, ...] = (),
    blocking_issues: tuple[str, ...] = (),
) -> dict[str, object]:
    lookup = int(transport_summary.get("lookup_requests_performed") or 0)
    uploads = int(transport_summary.get("wordpress_upload_requests_performed") or 0)
    writes = int(transport_summary.get("write_requests_performed") or 0)
    reconciliations = int(
        transport_summary.get("reconciliation_requests_performed") or 0
    )
    if writes != uploads:
        raise WordPressMediaUploadCanaryError(
            "wordpress_media_canary_write_audit_mismatch"
        )
    preparation_network = int(
        preparation_summary.get("network_requests_performed") or 0
    )
    downloads = int(download_summary.get("download_requests_performed") or 0)
    report = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "canary": dict(canary),
        "preparation_summary": dict(preparation_summary),
        "download_summary": dict(download_summary),
        "conversion_summary": dict(conversion_summary),
        "gate_summary": dict(gate_summary),
        "transport_summary": dict(transport_summary),
        "lookup_requests_performed": lookup,
        "upload_requests_performed": uploads,
        "write_requests_performed": writes,
        "reconciliation_requests_performed": reconciliations,
        "network_requests_performed": (
            preparation_network + downloads + lookup + uploads + reconciliations
        ),
        "remote_media_created": int(
            transport_summary.get("remote_media_created") or 0
        ),
        "remote_media_reused": int(
            transport_summary.get("remote_media_reused") or 0
        ),
        "webp_cleanup_completed": webp_cleanup_completed,
        "webp_files_remaining": webp_files_remaining,
        "source_cleanup_completed": source_cleanup_completed,
        "source_files_remaining": source_files_remaining,
        "retained_webp_artifacts": retained_webp_artifacts,
        "retained_download_artifacts": retained_download_artifacts,
        "delete_requests_performed": 0,
        "rollback_requests_performed": 0,
        "woocommerce_requests_performed": 0,
        "woocommerce_write_requests_performed": 0,
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
    status: str = "blocked",
    preparation_summary: Mapping[str, int | None] | None = None,
) -> dict[str, object]:
    return _safe_report(
        status=status,
        canary=_empty_canary(sku, position),
        preparation_summary=(
            _zero(_PREPARATION_FIELDS)
            if preparation_summary is None
            else preparation_summary
        ),
        download_summary=_zero(_DOWNLOAD_FIELDS),
        conversion_summary=_zero(_CONVERSION_FIELDS),
        gate_summary=_zero(_GATE_FIELDS),
        transport_summary=_zero(_TRANSPORT_FIELDS),
        webp_cleanup_completed=True,
        webp_files_remaining=0,
        source_cleanup_completed=True,
        source_files_remaining=0,
        retained_webp_artifacts=0,
        retained_download_artifacts=0,
        blocking_issues=(code,),
    )


def _emit(
    callback: ProgressCallback | None,
    *,
    sku: str,
    position: int,
    stage: str,
    status: str,
) -> None:
    if callback is None:
        return
    if status not in _SAFE_PROGRESS_STATUSES:
        raise WordPressMediaUploadCanaryError(
            "wordpress_media_canary_progress_status_invalid"
        )
    event = {
        "current_index": 1,
        "total_items": 1,
        "sku": sku,
        "selection_position": position,
        "stage": stage,
        "status": status,
    }
    try:
        callback(event)
    except Exception:
        raise WordPressMediaUploadCanaryError(
            "wordpress_media_canary_progress_callback_failed"
        ) from None


def _relay_progress(
    callback: ProgressCallback | None,
    *,
    sku: str,
    position: int,
    stage: str,
) -> ProgressCallback | None:
    if callback is None:
        return None

    def relay(event: Mapping[str, object]) -> None:
        if not isinstance(event, Mapping) or type(event.get("status")) is not str:
            raise WordPressMediaUploadCanaryError(
                "wordpress_media_canary_progress_event_invalid"
            )
        status = str(event["status"])
        if status in _CORE_BLOCKED_PROGRESS_STATUSES:
            status = "upload_blocked"
        _emit(
            callback,
            sku=sku,
            position=position,
            stage=stage,
            status=status,
        )

    return relay


def _remaining(
    summary: Mapping[str, int | None], created: str, cleaned: str
) -> int:
    created_value = summary.get(created)
    cleaned_value = summary.get(cleaned)
    if (
        type(created_value) is not int
        or type(cleaned_value) is not int
        or created_value < 0
        or cleaned_value < 0
        or cleaned_value > created_value
    ):
        return 1
    return created_value - cleaned_value


def _remember_base_exception(
    current: tuple[BaseException, TracebackType | None] | None,
    error: BaseException,
) -> tuple[BaseException, TracebackType | None]:
    return current if current is not None else (error, error.__traceback__)


def execute_wordpress_media_upload_canary(
    preparation: SelectedMediaHandlePreparationResult,
    metadata_settings: GoogleSettings,
    client_factory: CanaryGoogleClientFactory,
    wordpress_settings: Settings,
    wordpress_transport: transport_core.WordPressMediaHttpTransport,
    confirmation_token: str,
    *,
    sku: str,
    position: int,
    workspace_parent: Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    """Run one exact upload while retaining no local media authority."""

    validate_staging_media_upload_confirmation(confirmation_token)
    target_sku, target_position = _target(sku, position)
    _validate_staging_target(wordpress_settings)
    if progress_callback is not None and not callable(progress_callback):
        raise WordPressMediaUploadCanaryError(
            "wordpress_media_canary_progress_callback_invalid"
        )
    preparation_summary = _preparation_summary(preparation)
    if (
        metadata_settings.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        or metadata_settings.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE
    ):
        return _blocked_report(
            target_sku,
            target_position,
            "wordpress_media_canary_preparation_scope_mismatch",
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
            "wordpress_media_canary_preparation_not_authoritative",
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
            "wordpress_media_canary_handle_not_found",
            preparation_summary=preparation_summary,
        )
    if len(matches) != 1:
        return _blocked_report(
            target_sku,
            target_position,
            "wordpress_media_canary_handle_ambiguous",
            preparation_summary=preparation_summary,
        )

    handle = matches[0]
    canary = _empty_canary(target_sku, target_position)
    canary["image_role"] = handle.image_role.value
    canary["source_mime_type"] = handle.source_mime_type
    canary["source_size_bytes"] = handle.size_bytes
    canary["source_width"] = handle.image_width
    canary["source_height"] = handle.image_height
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
            "wordpress_media_canary_content_client_creation_failed",
            status="failed",
            preparation_summary=preparation_summary,
        )

    gateway = GoogleDriveContentGateway(drive)
    download_result: download_core.SecureMediaDownloadBatchResult | None = None
    conversion_result: conversion_core.VerifiedWebPConversionBatchResult | None = None
    download_before: Mapping[str, object] = {}
    download_after: Mapping[str, object] = {}
    conversion_before: Mapping[str, object] = {}
    conversion_after: Mapping[str, object] = {}
    gate_summary = _zero(_GATE_FIELDS)
    transport_summary = _zero(_TRANSPORT_FIELDS)
    warnings = tuple(handle.warnings)
    blockers: tuple[str, ...] = ()
    pending_base: tuple[BaseException, TracebackType | None] | None = None
    stage = "download"
    try:
        download_result = download_core.download_secure_media(
            (handle,),
            gateway,
            workspace_parent=workspace_parent,
            progress_callback=_relay_progress(
                progress_callback,
                sku=target_sku,
                position=target_position,
                stage="download",
            ),
        )
        download_before = download_result.to_safe_report_dict()
        download_summary = _compact(
            download_before,
            _DOWNLOAD_FIELDS,
            "invalid_wordpress_media_canary_download_result",
        )
        if (
            download_result.status != "ok"
            or len(download_result.artifacts) != 1
            or download_summary["downloads_verified"] != 1
            or download_summary["checksum_verified"] != 1
            or download_summary["size_verified"] != 1
            or download_summary["signature_verified"] != 1
            or download_summary["authoritative_artifacts"] != 1
        ):
            blockers = ("wordpress_media_canary_download_not_verified",)
        else:
            source = download_result.artifacts[0]
            canary.update(
                {
                    "source_mime_type": source.source_mime_type,
                    "source_size_bytes": source.actual_size_bytes,
                    "source_width": source.expected_image_width,
                    "source_height": source.expected_image_height,
                }
            )
            stage = "conversion"
            conversion_result = conversion_core.convert_verified_media_to_webp(
                source,
                workspace_parent=workspace_parent,
                progress_callback=_relay_progress(
                    progress_callback,
                    sku=target_sku,
                    position=target_position,
                    stage="conversion",
                ),
            )
            conversion_before = conversion_result.to_safe_report_dict()
            conversion_summary = _compact(
                conversion_before,
                _CONVERSION_FIELDS,
                "invalid_wordpress_media_canary_conversion_result",
            )
            if (
                conversion_result.status != "ok"
                or len(conversion_result.artifacts) != 1
                or conversion_summary["conversion_verified"] != 1
                or conversion_summary["authoritative_webp_artifacts"] != 1
                or conversion_summary["dimension_verified"] != 1
                or conversion_summary["webp_signature_verified"] != 1
                or conversion_summary["webp_decode_verified"] != 1
            ):
                blockers = ("wordpress_media_canary_conversion_not_verified",)
            else:
                webp = conversion_result.artifacts[0]
                conversion_core._local_webp_path_for_upload(webp)
                canary.update(
                    {
                        "output_mime_type": webp.output_mime_type,
                        "output_extension": webp.output_extension,
                        "output_size_bytes": webp.output_size_bytes,
                        "output_sha256": webp.output_sha256,
                        "output_width": webp.image_width,
                        "output_height": webp.image_height,
                    }
                )
                warnings = tuple(dict.fromkeys((*warnings, *webp.warnings)))
                if (
                    webp.output_mime_type != "image/webp"
                    or webp.output_extension != ".webp"
                    or webp.webp_verified is not True
                    or webp.image_width != canary["source_width"]
                    or webp.image_height != canary["source_height"]
                ):
                    blockers = ("wordpress_media_canary_webp_not_verified",)
                else:
                    stage = "gate"
                    gate = gate_core.create_wordpress_media_upload_intents(
                        webp, wordpress_settings
                    )
                    gate_report = gate.to_safe_dict()
                    gate_summary = _compact(
                        gate_report,
                        _GATE_FIELDS,
                        "invalid_wordpress_media_canary_gate_result",
                    )
                    if (
                        gate.status != "ok"
                        or len(gate.intents) != 1
                        or gate_summary["gate_passed"] != 1
                        or gate_summary["intents_created"] != 1
                    ):
                        blockers = tuple(gate_report.get("blocking_issues") or ())
                        if not blockers:
                            blockers = ("wordpress_media_canary_gate_not_verified",)
                    else:
                        intent = gate.intents[0]
                        canary["media_identity"] = intent.media_identity
                        canary["upload_filename"] = intent.upload_filename
                        canary["wordpress_slug"] = transport_core._media_slug(intent)
                        _emit(
                            progress_callback,
                            sku=target_sku,
                            position=target_position,
                            stage="gate",
                            status="gate_verified",
                        )
                        stage = "transport"
                        credentials = (
                            transport_core._create_staging_application_password_credentials(
                                wordpress_settings.wp_username,
                                wordpress_settings.wp_app_password,
                            )
                        )
                        permit = transport_core._create_staging_media_write_permit(
                            wordpress_settings
                        )
                        transport_result = transport_core.execute_wordpress_media_uploads(
                            intent,
                            wordpress_settings,
                            credentials,
                            permit,
                            wordpress_transport,
                            progress_callback=_relay_progress(
                                progress_callback,
                                sku=target_sku,
                                position=target_position,
                                stage="wordpress_media",
                            ),
                        )
                        transport_report = transport_result.to_safe_dict()
                        transport_summary = _compact(
                            transport_report,
                            _TRANSPORT_FIELDS,
                            "invalid_wordpress_media_canary_transport_result",
                        )
                        if (
                            transport_result.status != "ok"
                            or len(transport_result.references) != 1
                        ):
                            blockers = tuple(
                                transport_report.get("blocking_issues") or ()
                            )
                            if not blockers:
                                blockers = (
                                    "wordpress_media_canary_transport_blocked",
                                )
                        else:
                            reference = transport_result.references[0]
                            if reference.upload_status not in _UPLOAD_STATUSES:
                                blockers = (
                                    "wordpress_media_canary_reference_invalid",
                                )
                            else:
                                canary.update(
                                    {
                                        "wordpress_slug": reference.wordpress_slug,
                                        "upload_status": reference.upload_status,
                                        "wordpress_media_id": (
                                            reference.wordpress_media_id
                                        ),
                                    }
                                )
    except Exception:
        blockers = (f"wordpress_media_canary_{stage}_failed",)
    except BaseException as error:
        pending_base = _remember_base_exception(pending_base, error)
    finally:
        try:
            if conversion_result is not None:
                conversion_result.cleanup()
                conversion_after = conversion_result.to_safe_report_dict()
        except Exception:
            blockers = ("wordpress_media_canary_webp_cleanup_failed",)
        except BaseException as error:
            pending_base = _remember_base_exception(pending_base, error)
        finally:
            try:
                if download_result is not None:
                    download_result.cleanup()
                    download_after = download_result.to_safe_report_dict()
            except Exception:
                blockers = ("wordpress_media_canary_source_cleanup_failed",)
            except BaseException as error:
                pending_base = _remember_base_exception(pending_base, error)

    if pending_base is not None:
        error, traceback = pending_base
        raise error.with_traceback(traceback)

    try:
        download_summary = (
            _compact(
                download_before,
                _DOWNLOAD_FIELDS,
                "invalid_wordpress_media_canary_download_result",
            )
            if download_before
            else _zero(_DOWNLOAD_FIELDS)
        )
        conversion_summary = (
            _compact(
                conversion_before,
                _CONVERSION_FIELDS,
                "invalid_wordpress_media_canary_conversion_result",
            )
            if conversion_before
            else _zero(_CONVERSION_FIELDS)
        )
        download_cleanup = (
            _compact(
                download_after,
                _DOWNLOAD_FIELDS,
                "invalid_wordpress_media_canary_download_cleanup_result",
            )
            if download_after
            else _zero(_DOWNLOAD_FIELDS)
        )
        conversion_cleanup = (
            _compact(
                conversion_after,
                _CONVERSION_FIELDS,
                "invalid_wordpress_media_canary_conversion_cleanup_result",
            )
            if conversion_after
            else _zero(_CONVERSION_FIELDS)
        )
    except WordPressMediaUploadCanaryError:
        blockers = ("wordpress_media_canary_audit_invalid",)
        download_summary = _zero(_DOWNLOAD_FIELDS)
        conversion_summary = _zero(_CONVERSION_FIELDS)
        download_cleanup = _zero(_DOWNLOAD_FIELDS)
        conversion_cleanup = _zero(_CONVERSION_FIELDS)

    webp_remaining = _remaining(
        conversion_cleanup, "output_files_created", "output_files_cleaned"
    )
    source_remaining = _remaining(
        download_cleanup, "source_files_created", "source_files_cleaned"
    )
    retained_webp = (
        len(conversion_result.artifacts) if conversion_result is not None else 0
    )
    retained_download = (
        len(download_result.artifacts) if download_result is not None else 0
    )
    webp_cleanup = webp_remaining == 0 and retained_webp == 0
    source_cleanup = source_remaining == 0 and retained_download == 0
    if not webp_cleanup:
        blockers = ("wordpress_media_canary_webp_cleanup_incomplete",)
    if not source_cleanup:
        blockers = ("wordpress_media_canary_source_cleanup_incomplete",)
    try:
        _emit(
            progress_callback,
            sku=target_sku,
            position=target_position,
            stage="cleanup",
            status="cleanup_completed",
        )
    except WordPressMediaUploadCanaryError:
        blockers = ("wordpress_media_canary_progress_callback_failed",)

    return _safe_report(
        status="ok" if not blockers else "blocked",
        canary=canary,
        preparation_summary=preparation_summary,
        download_summary=download_summary,
        conversion_summary=conversion_summary,
        gate_summary=gate_summary,
        transport_summary=transport_summary,
        webp_cleanup_completed=webp_cleanup,
        webp_files_remaining=webp_remaining,
        source_cleanup_completed=source_cleanup,
        source_files_remaining=source_remaining,
        retained_webp_artifacts=retained_webp,
        retained_download_artifacts=retained_download,
        warnings=warnings,
        blocking_issues=blockers,
    )


def run_wordpress_media_upload_canary(
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    sku: str,
    position: int,
    confirmation_token: str,
    metadata_settings: GoogleSettings,
    client_factory: CanaryGoogleClientFactory,
    wordpress_settings: Settings,
    wordpress_transport: transport_core.WordPressMediaHttpTransport,
    *,
    project_root: Path,
    progress_callback: ProgressCallback | None = None,
) -> tuple[dict[str, object], Path]:
    """Fresh-prepare all handles, upload one exact media, then write audit."""

    validate_staging_media_upload_confirmation(confirmation_token)
    target_sku, target_position = _target(sku, position)
    _validate_staging_target(wordpress_settings)
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
        report = execute_wordpress_media_upload_canary(
            preparation,
            metadata_settings,
            client_factory,
            wordpress_settings,
            wordpress_transport,
            confirmation_token,
            sku=target_sku,
            position=target_position,
            progress_callback=progress_callback,
        )
    except Exception:
        report = _blocked_report(
            target_sku,
            target_position,
            "wordpress_media_canary_preparation_failed",
            status="failed",
        )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeWriteAuditJsonReportWriter(output, Redactor()).write(report)
    return report, output
