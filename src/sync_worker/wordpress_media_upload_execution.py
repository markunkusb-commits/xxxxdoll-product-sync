"""Full, sequential staging WordPress media upload execution.

All authority flows in memory from a fresh selected-media preparation through
the existing download, conversion, gate, and transport cores.  JSON is an
audit projection only; it can never restore any authority object.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Protocol

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_media_download_execution as download_execution
from . import verified_webp_conversion as conversion_core
from . import verified_webp_conversion_execution as conversion_execution
from . import wordpress_media_upload_canary as canary_core
from . import wordpress_media_upload_gate as gate_core
from . import wordpress_media_upload_transport as transport_core
from .config import GoogleSettings, Settings
from .report import SafeWriteAuditJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
    prepare_selected_media_handles,
)


POLICY_VERSION = "xxxxdoll-wordpress-media-upload-execution-v1"
REPORT_FILENAME = "wordpress-media-upload-execution.json"
EXACT_CONFIRMATION_TOKEN = "I_CONFIRM_FULL_STAGING_MEDIA_UPLOAD"
_PREPARATION_FIELDS = download_execution._PREPARATION_SUMMARY_FIELDS
_DOWNLOAD_FIELDS = download_execution._DOWNLOAD_SUMMARY_FIELDS
_CONVERSION_FIELDS = conversion_execution._CONVERSION_SUMMARY_FIELDS
_GATE_FIELDS = canary_core._GATE_FIELDS
_TRANSPORT_FIELDS = canary_core._TRANSPORT_FIELDS
_RESULT_FIELDS = (
    "sku",
    "selection_position",
    "image_role",
    "media_identity",
    "upload_filename",
    "wordpress_slug",
    "wordpress_media_id",
    "upload_status",
    "mime_type",
    "warnings",
    "blocking_issues",
)


class WordPressMediaUploadExecutionError(ValueError):
    """Fixed-code batch error without credentials, URLs, paths, or IDs."""


class ExecutionGoogleClientFactory(
    conversion_execution.ExecutionGoogleClientFactory, Protocol
):
    """Minimal Google factory contract inherited from existing execution."""


class DiskUsageResult(Protocol):
    free: int


ProgressCallback = Callable[[Mapping[str, object]], None]


def validate_staging_media_batch_upload_confirmation(value: object) -> None:
    if type(value) is not str or value != EXACT_CONFIRMATION_TOKEN:
        raise WordPressMediaUploadExecutionError(
            "staging_media_batch_upload_confirmation_required"
        )


def validate_expected_selected_items(value: object) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > gate_core.MAX_ARTIFACTS_PER_BATCH
    ):
        raise WordPressMediaUploadExecutionError(
            "wordpress_media_batch_expected_selected_items_invalid"
        )
    return value


def _validate_staging_target(settings: Settings) -> None:
    try:
        gate_core._target_binding(settings)
    except gate_core.WordPressMediaUploadGateError:
        raise WordPressMediaUploadExecutionError(
            "wordpress_media_execution_staging_safety_failed"
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
    raw = report.get("summary")
    if not isinstance(raw, Mapping):
        raise WordPressMediaUploadExecutionError(code)
    result: dict[str, int | None] = {}
    for field in fields:
        value = raw.get(field)
        if field == "failed_at_index" and value is None:
            result[field] = None
        elif type(value) is not int or value < 0:
            raise WordPressMediaUploadExecutionError(code)
        else:
            result[field] = value
    return result


def _preparation_authority(
    preparation: SelectedMediaHandlePreparationResult,
) -> tuple[dict[str, int], tuple[object, ...]]:
    try:
        summary = download_execution._compact_preparation_summary(preparation)
        handles = download_execution._authoritative_handles(preparation, summary)
    except download_execution.SecureMediaDownloadExecutionError as error:
        raise WordPressMediaUploadExecutionError(error.code) from None
    return summary, handles


def _emit(
    callback: ProgressCallback | None,
    *,
    current_index: int,
    total_items: int,
    sku: str,
    selection_position: int,
    stage: str,
    status: str,
) -> None:
    if callback is None:
        return
    try:
        callback(
            {
                "current_index": current_index,
                "total_items": total_items,
                "sku": sku,
                "selection_position": selection_position,
                "stage": stage,
                "status": status,
            }
        )
    except Exception:
        raise WordPressMediaUploadExecutionError(
            "wordpress_media_execution_progress_callback_failed"
        ) from None


def _relay(callback: ProgressCallback | None, stage: str) -> ProgressCallback | None:
    if callback is None:
        return None

    def relay(event: Mapping[str, object]) -> None:
        if not isinstance(event, Mapping):
            raise WordPressMediaUploadExecutionError(
                "wordpress_media_execution_progress_event_invalid"
            )
        current = event.get("current_index")
        total = event.get("total_items")
        sku = event.get("sku")
        position = event.get("selection_position")
        status = event.get("status")
        if (
            type(current) is not int
            or current <= 0
            or type(total) is not int
            or total <= 0
            or type(sku) is not str
            or type(position) is not int
            or position < 0
            or type(status) is not str
        ):
            raise WordPressMediaUploadExecutionError(
                "wordpress_media_execution_progress_event_invalid"
            )
        _emit(
            callback,
            current_index=current,
            total_items=total,
            sku=sku,
            selection_position=position,
            stage=stage,
            status=status,
        )

    return relay


def _remaining(
    summary: Mapping[str, int | None], created_key: str, cleaned_key: str
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


def _safe_results(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise WordPressMediaUploadExecutionError(
            "wordpress_media_execution_transport_audit_invalid"
        )
    results: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise WordPressMediaUploadExecutionError(
                "wordpress_media_execution_transport_audit_invalid"
            )
        results.append({field: raw.get(field) for field in _RESULT_FIELDS})
    return results


def _safe_report(
    *,
    status: str,
    selected_items: int,
    expected_selected_items: int,
    preparation_summary: Mapping[str, int | None],
    capacity_preflight: Mapping[str, int],
    download_summary: Mapping[str, int | None],
    conversion_summary: Mapping[str, int | None],
    gate_summary: Mapping[str, int | None],
    transport_summary: Mapping[str, int | None],
    results: list[Mapping[str, object]],
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
    uploads = int(
        transport_summary.get("wordpress_upload_requests_performed") or 0
    )
    reconciliations = int(
        transport_summary.get("reconciliation_requests_performed") or 0
    )
    writes = int(transport_summary.get("write_requests_performed") or 0)
    deletes = int(transport_summary.get("delete_requests_performed") or 0)
    rollbacks = int(transport_summary.get("rollback_requests_performed") or 0)
    if writes != uploads or deletes != 0 or rollbacks != 0:
        raise WordPressMediaUploadExecutionError(
            "wordpress_media_execution_write_audit_mismatch"
        )
    preparation_network = int(
        preparation_summary.get("network_requests_performed") or 0
    )
    download_requests = int(
        download_summary.get("download_requests_performed") or 0
    )
    report = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "selected_items": selected_items,
        "expected_selected_items": expected_selected_items,
        "capacity_preflight": dict(capacity_preflight),
        "preparation_summary": dict(preparation_summary),
        "download_summary": dict(download_summary),
        "conversion_summary": dict(conversion_summary),
        "gate_summary": dict(gate_summary),
        "transport_summary": dict(transport_summary),
        "lookup_requests_performed": lookup,
        "upload_requests_performed": uploads,
        "reconciliation_requests_performed": reconciliations,
        "write_requests_performed": writes,
        "network_requests_performed": (
            preparation_network
            + download_requests
            + lookup
            + uploads
            + reconciliations
        ),
        "remote_media_created": int(
            transport_summary.get("remote_media_created") or 0
        ),
        "remote_media_reused": int(
            transport_summary.get("remote_media_reused") or 0
        ),
        "created": int(transport_summary.get("created") or 0),
        "reused": int(transport_summary.get("reused") or 0),
        "created_reconciled": int(
            transport_summary.get("created_reconciled") or 0
        ),
        "references_created": int(
            transport_summary.get("references_created") or 0
        ),
        "failed_at_index": transport_summary.get("failed_at_index"),
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
        "results": [dict(item) for item in results],
    }
    safe = sanitize_report_data(report, Redactor())
    drive_manifest_core._assert_report_safe(safe)
    return json.loads(json.dumps(safe, ensure_ascii=False))


def _empty_capacity() -> dict[str, int]:
    return {
        "expected_total_source_bytes": 0,
        "maximum_webp_output_bytes": 0,
        "safety_reserve_bytes": (
            download_execution.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
        ),
        "required_capacity_bytes": 0,
    }


def _blocked_report(
    code: str,
    *,
    expected_selected_items: int,
    selected_items: int = 0,
    preparation_summary: Mapping[str, int | None] | None = None,
    capacity_preflight: Mapping[str, int] | None = None,
    status: str = "blocked",
) -> dict[str, object]:
    return _safe_report(
        status=status,
        selected_items=selected_items,
        expected_selected_items=expected_selected_items,
        preparation_summary=(
            _zero(_PREPARATION_FIELDS)
            if preparation_summary is None
            else preparation_summary
        ),
        capacity_preflight=(
            _empty_capacity() if capacity_preflight is None else capacity_preflight
        ),
        download_summary=_zero(_DOWNLOAD_FIELDS),
        conversion_summary=_zero(_CONVERSION_FIELDS),
        gate_summary=_zero(_GATE_FIELDS),
        transport_summary=_zero(_TRANSPORT_FIELDS),
        results=[],
        webp_cleanup_completed=True,
        webp_files_remaining=0,
        source_cleanup_completed=True,
        source_files_remaining=0,
        retained_webp_artifacts=0,
        retained_download_artifacts=0,
        blocking_issues=(code,),
    )


def _remember_base_exception(
    current: tuple[BaseException, TracebackType | None] | None,
    error: BaseException,
) -> tuple[BaseException, TracebackType | None]:
    return current if current is not None else (error, error.__traceback__)


def execute_wordpress_media_upload_execution(
    preparation: SelectedMediaHandlePreparationResult,
    expected_selected_items: int,
    metadata_settings: GoogleSettings,
    client_factory: ExecutionGoogleClientFactory,
    wordpress_settings: Settings,
    wordpress_transport: transport_core.WordPressMediaHttpTransport,
    confirmation_token: str,
    *,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    """Execute one complete live batch without restoring report authority."""

    validate_staging_media_batch_upload_confirmation(confirmation_token)
    expected = validate_expected_selected_items(expected_selected_items)
    _validate_staging_target(wordpress_settings)
    if progress_callback is not None and not callable(progress_callback):
        raise WordPressMediaUploadExecutionError(
            "wordpress_media_execution_progress_callback_invalid"
        )
    preparation_summary, handles = _preparation_authority(preparation)
    selected = preparation_summary["selected_items"]
    if selected != expected:
        return _blocked_report(
            "wordpress_media_batch_selected_item_count_changed",
            expected_selected_items=expected,
            selected_items=selected,
            preparation_summary=preparation_summary,
        )

    try:
        batch = conversion_execution.execute_prepared_webp_conversion_batch(
            preparation,
            metadata_settings,
            client_factory,
            workspace_parent=workspace_parent,
            disk_usage_reader=disk_usage_reader,
            download_progress_callback=_relay(progress_callback, "download"),
            conversion_progress_callback=_relay(progress_callback, "conversion"),
        )
    except conversion_execution.VerifiedWebPConversionExecutionError as error:
        code = error.code
        if code == "insufficient_webp_conversion_workspace_capacity":
            code = "insufficient_wordpress_media_upload_workspace_capacity"
        elif code == "webp_conversion_workspace_capacity_unavailable":
            code = "wordpress_media_upload_workspace_capacity_unavailable"
        return _blocked_report(
            code,
            expected_selected_items=expected,
            selected_items=selected,
            preparation_summary=preparation_summary,
            status=error.status,
        )

    download_before: Mapping[str, object] = {}
    conversion_before: Mapping[str, object] = {}
    download_after: Mapping[str, object] = {}
    conversion_after: Mapping[str, object] = {}
    gate_summary = _zero(_GATE_FIELDS)
    transport_summary = _zero(_TRANSPORT_FIELDS)
    transport_results: list[dict[str, object]] = []
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    pending_base: tuple[BaseException, TracebackType | None] | None = None
    stage = "download"
    try:
        download_before = batch.download_batch.download_result.to_safe_report_dict()
        download_summary = _compact(
            download_before,
            _DOWNLOAD_FIELDS,
            "wordpress_media_execution_download_audit_invalid",
        )
        download_artifacts = batch.download_artifacts
        download_ok = (
            batch.download_batch.download_result.status == "ok"
            and len(download_artifacts) == selected
            and download_summary["downloads_verified"] == selected
            and download_summary["downloads_failed"] == 0
            and download_summary["checksum_verified"] == selected
            and download_summary["checksum_mismatch"] == 0
            and download_summary["size_verified"] == selected
            and download_summary["size_mismatch"] == 0
            and download_summary["signature_verified"] == selected
            and download_summary["signature_mismatch"] == 0
            and download_summary["authoritative_artifacts"] == selected
        )
        if not download_ok:
            blockers = ("wordpress_media_execution_download_not_verified",)
        else:
            stage = "conversion"
            if batch.conversion_result is None:
                blockers = ("wordpress_media_execution_conversion_not_verified",)
            else:
                conversion_before = batch.conversion_result.to_safe_report_dict()
                conversion_summary = _compact(
                    conversion_before,
                    _CONVERSION_FIELDS,
                    "wordpress_media_execution_conversion_audit_invalid",
                )
                webp_artifacts = batch.webp_artifacts
                conversion_ok = (
                    batch.conversion_result.status == "ok"
                    and len(webp_artifacts) == selected
                    and conversion_summary["source_artifacts_received"] == selected
                    and conversion_summary["conversion_verified"] == selected
                    and conversion_summary["conversion_failed"] == 0
                    and conversion_summary["decode_verified"] == selected
                    and conversion_summary["dimension_verified"] == selected
                    and conversion_summary["webp_signature_verified"] == selected
                    and conversion_summary["webp_decode_verified"] == selected
                    and conversion_summary["authoritative_webp_artifacts"] == selected
                )
                if not conversion_ok:
                    blockers = (
                        "wordpress_media_execution_conversion_not_verified",
                    )
                else:
                    stage = "gate"
                    gate_result = gate_core.create_wordpress_media_upload_intents(
                        webp_artifacts,
                        wordpress_settings,
                    )
                    gate_report = gate_result.to_safe_dict()
                    gate_summary = _compact(
                        gate_report,
                        _GATE_FIELDS,
                        "wordpress_media_execution_gate_audit_invalid",
                    )
                    intent_keys = tuple(
                        (intent.sku, intent.selection_position)
                        for intent in gate_result.intents
                    )
                    handle_keys = tuple(
                        (handle.sku, handle.selection_position) for handle in handles
                    )
                    gate_ok = (
                        gate_result.status == "ok"
                        and len(gate_result.intents) == selected
                        and gate_summary["artifacts_received"] == selected
                        and gate_summary["gate_passed"] == selected
                        and gate_summary["gate_blocked"] == 0
                        and gate_summary["intents_created"] == selected
                        and intent_keys == handle_keys
                    )
                    if not gate_ok:
                        blockers = tuple(
                            gate_report.get("blocking_issues") or ()
                        ) or ("wordpress_media_execution_gate_not_verified",)
                    else:
                        last = gate_result.intents[-1]
                        _emit(
                            progress_callback,
                            current_index=selected,
                            total_items=selected,
                            sku=last.sku,
                            selection_position=last.selection_position,
                            stage="gate",
                            status="gate_verified",
                        )
                        credentials = (
                            transport_core._create_staging_application_password_credentials(
                                wordpress_settings.wp_username,
                                wordpress_settings.wp_app_password,
                            )
                        )
                        permit = transport_core._create_staging_media_write_permit(
                            wordpress_settings
                        )
                        stage = "wordpress_media"
                        transport_result = transport_core.execute_wordpress_media_uploads(
                            gate_result.intents,
                            wordpress_settings,
                            credentials,
                            permit,
                            wordpress_transport,
                            progress_callback=_relay(
                                progress_callback, "wordpress_media"
                            ),
                        )
                        transport_report = transport_result.to_safe_dict()
                        transport_summary = _compact(
                            transport_report,
                            _TRANSPORT_FIELDS,
                            "wordpress_media_execution_transport_audit_invalid",
                        )
                        transport_results = _safe_results(
                            transport_report.get("results")
                        )
                        if (
                            transport_result.status != "ok"
                            or len(transport_result.references) != selected
                            or transport_summary["references_created"] != selected
                        ):
                            blockers = tuple(
                                transport_report.get("blocking_issues") or ()
                            ) or ("wordpress_media_execution_transport_blocked",)
    except Exception:
        blockers = (f"wordpress_media_execution_{stage}_failed",)
    except BaseException as error:
        pending_base = _remember_base_exception(pending_base, error)
    finally:
        try:
            batch.cleanup()
        except Exception:
            blockers = ("wordpress_media_execution_cleanup_failed",)
        except BaseException as error:
            pending_base = _remember_base_exception(pending_base, error)
        try:
            download_after = batch.download_batch.download_result.to_safe_report_dict()
            if batch.conversion_result is not None:
                conversion_after = batch.conversion_result.to_safe_report_dict()
        except Exception:
            blockers = ("wordpress_media_execution_cleanup_audit_failed",)
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
                "wordpress_media_execution_download_audit_invalid",
            )
            if download_before
            else _zero(_DOWNLOAD_FIELDS)
        )
        download_cleanup = (
            _compact(
                download_after,
                _DOWNLOAD_FIELDS,
                "wordpress_media_execution_download_cleanup_audit_invalid",
            )
            if download_after
            else _zero(_DOWNLOAD_FIELDS)
        )
        conversion_summary = (
            _compact(
                conversion_before,
                _CONVERSION_FIELDS,
                "wordpress_media_execution_conversion_audit_invalid",
            )
            if conversion_before
            else _zero(_CONVERSION_FIELDS)
        )
        conversion_cleanup = (
            _compact(
                conversion_after,
                _CONVERSION_FIELDS,
                "wordpress_media_execution_conversion_cleanup_audit_invalid",
            )
            if conversion_after
            else _zero(_CONVERSION_FIELDS)
        )
    except WordPressMediaUploadExecutionError:
        blockers = ("wordpress_media_execution_audit_invalid",)
        download_summary = _zero(_DOWNLOAD_FIELDS)
        download_cleanup = _zero(_DOWNLOAD_FIELDS)
        conversion_summary = _zero(_CONVERSION_FIELDS)
        conversion_cleanup = _zero(_CONVERSION_FIELDS)

    source_remaining = _remaining(
        download_cleanup, "source_files_created", "source_files_cleaned"
    )
    webp_remaining = _remaining(
        conversion_cleanup, "output_files_created", "output_files_cleaned"
    )
    retained_download = len(batch.download_artifacts)
    retained_webp = len(batch.webp_artifacts)
    source_cleanup = source_remaining == 0 and retained_download == 0
    webp_cleanup = webp_remaining == 0 and retained_webp == 0
    if not webp_cleanup:
        blockers = ("wordpress_media_execution_webp_cleanup_incomplete",)
    if not source_cleanup:
        blockers = ("wordpress_media_execution_source_cleanup_incomplete",)
    if progress_callback is not None and handles:
        last_handle = handles[-1]
        _emit(
            progress_callback,
            current_index=selected,
            total_items=selected,
            sku=last_handle.sku,
            selection_position=last_handle.selection_position,
            stage="cleanup",
            status="cleanup_completed",
        )

    success = (
        not blockers
        and transport_summary["references_created"] == selected
        and len(transport_results) == selected
        and source_cleanup
        and webp_cleanup
    )
    reported_download = dict(download_summary)
    reported_download["source_files_cleaned"] = download_cleanup[
        "source_files_cleaned"
    ]
    reported_conversion = dict(conversion_summary)
    reported_conversion["output_files_cleaned"] = conversion_cleanup[
        "output_files_cleaned"
    ]
    return _safe_report(
        status="ok" if success else "blocked",
        selected_items=selected,
        expected_selected_items=expected,
        preparation_summary=preparation_summary,
        capacity_preflight=batch.preflight.to_safe_dict(),
        download_summary=reported_download,
        conversion_summary=reported_conversion,
        gate_summary=gate_summary,
        transport_summary=transport_summary,
        results=transport_results,
        webp_cleanup_completed=webp_cleanup,
        webp_files_remaining=webp_remaining,
        source_cleanup_completed=source_cleanup,
        source_files_remaining=source_remaining,
        retained_webp_artifacts=retained_webp,
        retained_download_artifacts=retained_download,
        warnings=warnings,
        blocking_issues=() if success else blockers,
    )


def run_wordpress_media_upload_execution(
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    expected_selected_items: int,
    confirmation_token: str,
    metadata_settings: GoogleSettings,
    client_factory: ExecutionGoogleClientFactory,
    wordpress_settings: Settings,
    wordpress_transport: transport_core.WordPressMediaHttpTransport,
    *,
    project_root: Path,
    workspace_parent: Path | None = None,
    disk_usage_reader: Callable[[Path], DiskUsageResult] = shutil.disk_usage,
    progress_callback: ProgressCallback | None = None,
) -> tuple[dict[str, object], Path]:
    """Fresh-prepare the full selection and persist a safe write audit."""

    validate_staging_media_batch_upload_confirmation(confirmation_token)
    expected = validate_expected_selected_items(expected_selected_items)
    _validate_staging_target(wordpress_settings)
    preparation_summary: Mapping[str, int | None] | None = None
    selected_items = 0
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
        preparation_summary, _ = _preparation_authority(preparation)
        selected_items = int(preparation_summary["selected_items"])
        report = execute_wordpress_media_upload_execution(
            preparation,
            expected,
            metadata_settings,
            client_factory,
            wordpress_settings,
            wordpress_transport,
            confirmation_token,
            workspace_parent=workspace_parent,
            disk_usage_reader=disk_usage_reader,
            progress_callback=progress_callback,
        )
    except WordPressMediaUploadExecutionError as error:
        report = _blocked_report(
            str(error),
            expected_selected_items=expected,
            selected_items=selected_items,
            preparation_summary=preparation_summary,
        )
    except Exception:
        report = _blocked_report(
            "wordpress_media_execution_failed",
            expected_selected_items=expected,
            selected_items=selected_items,
            preparation_summary=preparation_summary,
            status="failed",
        )
    output = Path(project_root) / "reports" / REPORT_FILENAME
    SafeWriteAuditJsonReportWriter(output, Redactor()).write(report)
    return report, output
