"""Read-only Google Drive and Sheets permission diagnostics."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from .config import GoogleSettings
from .google_api import (
    GoogleClientFactory,
    ReadOnlyGoogleGateway,
    google_redactor_for_settings,
)
from .sanitization import Redactor


_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _safe_string(value: object, *, limit: int = 300) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _sample_counts(payload: object) -> tuple[int, int, int]:
    if not isinstance(payload, Mapping):
        return 0, 0, 0
    values = payload.get("values")
    if not isinstance(values, list):
        return 0, 0, 0
    rows = [row for row in values if isinstance(row, list)]
    row_count = len(rows)
    column_count = max((len(row) for row in rows), default=0)
    non_empty_count = sum(
        1 for row in rows for cell in row if cell is not None and cell != ""
    )
    return row_count, column_count, non_empty_count


class GoogleDoctorRunner:
    """Create clients only after validation and retain metadata/counts only."""

    def __init__(
        self,
        settings: GoogleSettings,
        factory: GoogleClientFactory,
        *,
        redactor: Redactor | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._redactor = redactor or google_redactor_for_settings(settings)
        self._logger = logger or logging.getLogger("sync_worker.google_doctor")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._errors: list[dict[str, str]] = []

    def _log(self, event: str, **fields: object) -> None:
        payload = self._redactor.value({"event": event, **fields})
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _error(self, check: str, error: BaseException) -> None:
        summary = self._redactor.exception(error)
        self._errors.append({"check": check, "summary": summary})
        self._log("google_doctor_check_failed", check=check, error=summary)

    def _base_report(self) -> dict[str, object]:
        return {
            "timestamp": self._clock().astimezone(timezone.utc).isoformat(),
            "service_account_file_exists": self._settings.resolved_service_account_file.is_file(),
            "service_account_authentication": False,
            "drive_api_status": False,
            "sheets_api_status": False,
            "clm_folder_access": {"ok": False},
            "clm_folder_name": "",
            "clm_child_summary": [],
            "md_folder_access": {"ok": False},
            "md_folder_name": "",
            "md_child_summary": [],
            "spreadsheet_access": False,
            "spreadsheet_title": "",
            "sheet_summaries": [],
            "read_requests_performed": 0,
            "write_requests_performed": 0,
            "sanitized_errors": self._errors,
        }

    def _folder_check(
        self,
        gateway: ReadOnlyGoogleGateway,
        label: str,
        folder_id: str,
    ) -> tuple[dict[str, object], str, list[dict[str, object]], bool]:
        metadata_ok = False
        list_ok = False
        access: dict[str, object] = {"ok": False}
        folder_name = ""
        children: list[dict[str, object]] = []
        try:
            metadata = gateway.get_folder(folder_id)
            if isinstance(metadata, Mapping):
                folder_name = _safe_string(metadata.get("name"))
                access = {
                    "ok": True,
                    "mime_type": _safe_string(metadata.get("mimeType")),
                    "trashed": _safe_bool(metadata.get("trashed")),
                }
                metadata_ok = True
        except Exception as error:
            self._error(f"{label}_folder_get", error)

        try:
            child_payload = gateway.list_folder_children(folder_id)
            if isinstance(child_payload, Mapping):
                files = child_payload.get("files")
                if isinstance(files, list):
                    for item in files[:100]:
                        if isinstance(item, Mapping):
                            mime_type = _safe_string(item.get("mimeType"))
                            children.append(
                                {
                                    "name": _safe_string(item.get("name")),
                                    "mime_type": mime_type,
                                    "modified_time": _safe_string(
                                        item.get("modifiedTime")
                                    ),
                                    "is_folder": mime_type == _FOLDER_MIME_TYPE,
                                }
                            )
                    list_ok = True
        except Exception as error:
            self._error(f"{label}_folder_list", error)
        return access, folder_name, children, metadata_ok and list_ok

    def _spreadsheet_check(
        self, gateway: ReadOnlyGoogleGateway
    ) -> tuple[bool, str, list[dict[str, object]], bool]:
        try:
            payload = gateway.get_spreadsheet(self._settings.clm_spreadsheet_id)
        except Exception as error:
            self._error("spreadsheet_get", error)
            return False, "", [], False
        if not isinstance(payload, Mapping):
            return False, "", [], False

        properties = payload.get("properties")
        spreadsheet_title = (
            _safe_string(properties.get("title"))
            if isinstance(properties, Mapping)
            else ""
        )
        raw_sheets = payload.get("sheets")
        if not isinstance(raw_sheets, list):
            return True, spreadsheet_title, [], False

        summaries: list[dict[str, object]] = []
        all_samples_ok = True
        for sheet in raw_sheets:
            sheet_properties = (
                sheet.get("properties") if isinstance(sheet, Mapping) else None
            )
            if not isinstance(sheet_properties, Mapping):
                continue
            title = _safe_string(sheet_properties.get("title"))
            grid = sheet_properties.get("gridProperties")
            grid_properties = grid if isinstance(grid, Mapping) else {}
            summary: dict[str, object] = {
                "name": title,
                "sheet_id": _safe_int(sheet_properties.get("sheetId")),
                "row_count": _safe_int(grid_properties.get("rowCount")),
                "column_count": _safe_int(grid_properties.get("columnCount")),
                "sample_read_success": False,
                "returned_row_count": 0,
                "returned_column_count": 0,
                "non_empty_cell_count": 0,
            }
            try:
                sample = gateway.get_sheet_sample(
                    self._settings.clm_spreadsheet_id, title
                )
                rows, columns, non_empty = _sample_counts(sample)
                summary.update(
                    {
                        "sample_read_success": isinstance(sample, Mapping),
                        "returned_row_count": rows,
                        "returned_column_count": columns,
                        "non_empty_cell_count": non_empty,
                    }
                )
                if not isinstance(sample, Mapping):
                    all_samples_ok = False
            except Exception as error:
                all_samples_ok = False
                self._error("sheet_sample_get", error)
            summaries.append(summary)
        return True, spreadsheet_title, summaries, all_samples_ok

    def _finalize(
        self,
        report: dict[str, object],
        gateway: ReadOnlyGoogleGateway | None,
    ) -> dict[str, object]:
        if gateway is not None:
            report["read_requests_performed"] = (
                gateway.counters.read_requests_performed
            )
        report["write_requests_performed"] = 0
        report["sanitized_errors"] = self._errors
        sanitized = self._redactor.value(report)
        if not isinstance(sanitized, dict):
            raise TypeError("Google doctor report sanitization failed")
        self._log(
            "google_doctor_complete",
            read_requests_performed=report["read_requests_performed"],
            write_requests_performed=0,
        )
        return sanitized

    def run(self) -> dict[str, object]:
        self._settings.validate()
        report = self._base_report()
        self._log("google_doctor_started")
        try:
            clients = self._factory.create(self._settings)
        except Exception as error:
            self._error("service_account_authentication", error)
            return self._finalize(report, None)

        report["service_account_authentication"] = True
        gateway = ReadOnlyGoogleGateway(clients)
        (
            report["clm_folder_access"],
            report["clm_folder_name"],
            report["clm_child_summary"],
            clm_ok,
        ) = self._folder_check(
            gateway, "clm", self._settings.clm_drive_folder_id
        )
        (
            report["md_folder_access"],
            report["md_folder_name"],
            report["md_child_summary"],
            md_ok,
        ) = self._folder_check(
            gateway, "md", self._settings.md_drive_folder_id
        )
        report["drive_api_status"] = clm_ok and md_ok
        (
            report["spreadsheet_access"],
            report["spreadsheet_title"],
            report["sheet_summaries"],
            samples_ok,
        ) = self._spreadsheet_check(gateway)
        report["sheets_api_status"] = bool(report["spreadsheet_access"]) and samples_ok
        return self._finalize(report, gateway)
