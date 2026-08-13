"""Bounded, read-only supplier Drive and Sheets structure inventory."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from .config import GoogleSettings
from .google_api import (
    GoogleClientFactory,
    ReadOnlyGoogleGateway,
    google_redactor_for_settings,
)
from .sanitization import Redactor


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
DRIVE_ITEM_LIMIT = 500
MIN_MAX_DEPTH = 1
MAX_MAX_DEPTH = 6
SHEET_ROW_LIMIT = 10
SHEET_COLUMN_LIMIT = 52
_EMBEDDED_URL_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)[^\s\"'<>),]+"
)


def validate_max_depth(max_depth: int) -> int:
    if (
        not isinstance(max_depth, int)
        or isinstance(max_depth, bool)
        or not MIN_MAX_DEPTH <= max_depth <= MAX_MAX_DEPTH
    ):
        raise ValueError("max_depth must be an integer from 1 to 6")
    return max_depth


def _safe_text(value: object, *, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    without_controls = "".join(
        character for character in value if character >= " " or character == "\t"
    )
    without_urls = _EMBEDDED_URL_PATTERN.sub("[REDACTED_URL]", without_controls)
    return without_urls[:limit]


def _safe_int(value: object, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    return default


def _safe_bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _extension(name: str) -> str:
    return PurePath(name).suffix.lower().removeprefix(".")


def _column_label(index: int) -> str:
    label = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _safe_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return _safe_text(str(value), limit=1000)
    return ""


def _sheet_structure(rows: list[list[str]]) -> dict[str, object]:
    non_empty_indices = sorted(
        {
            column_index
            for row in rows
            for column_index, value in enumerate(row)
            if value != ""
        }
    )
    populated_rows = [
        (index, [value for value in row if value != ""])
        for index, row in enumerate(rows)
        if any(value != "" for value in row)
    ]
    header_index: int | None = None
    for row_index, values in populated_rows:
        if len(values) >= 2:
            header_index = row_index
            break
    if header_index is None and populated_rows:
        header_index = populated_rows[0][0]

    detected_header_rows: list[int] = []
    possible_field_names: list[str] = []
    if header_index is not None:
        first_populated = populated_rows[0][0]
        detected_header_rows = list(range(first_populated + 1, header_index + 2))
        seen: set[str] = set()
        for value in rows[header_index]:
            normalized = value.strip()
            if normalized and normalized not in seen:
                possible_field_names.append(normalized)
                seen.add(normalized)

    return {
        "detected_header_rows": detected_header_rows,
        "non_empty_columns": [_column_label(index) for index in non_empty_indices],
        "possible_field_names": possible_field_names,
    }


class SupplierInventoryRunner:
    """Build a sanitized supplier structure inventory using read-only APIs only."""

    def __init__(
        self,
        settings: GoogleSettings,
        factory: GoogleClientFactory,
        *,
        max_depth: int = 4,
        redactor: Redactor | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._max_depth = validate_max_depth(max_depth)
        self._redactor = redactor or google_redactor_for_settings(settings)
        self._logger = logger or logging.getLogger("sync_worker.supplier_inventory")
        self._warnings: list[str] = []
        self._errors: list[dict[str, str]] = []

    def _log(self, event: str, **fields: object) -> None:
        payload = self._redactor.value({"event": event, **fields})
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _record_error(
        self,
        check: str,
        error: BaseException,
        *,
        additional_secrets: tuple[str, ...] = (),
    ) -> None:
        redactor = Redactor.from_values(
            (*self._redactor.secrets, *additional_secrets)
        )
        summary = _EMBEDDED_URL_PATTERN.sub(
            "[REDACTED_URL]", redactor.exception(error)
        )
        self._errors.append({"check": check, "summary": summary})
        self._log("supplier_inventory_check_failed", check=check, error=summary)

    def _base_report(self) -> dict[str, object]:
        return {
            "status": "error",
            "clm": {"root_name": "", "tree": [], "option_summary": []},
            "md": {"root_name": "", "tree": [], "top_level_summary": []},
            "spreadsheet": {"title": "", "sheet_count": 0, "sheets": []},
            "read_requests_performed": 0,
            "write_requests_performed": 0,
            "warnings": self._warnings,
            "errors": self._errors,
        }

    def _folder_metadata(
        self, gateway: ReadOnlyGoogleGateway, label: str, folder_id: str
    ) -> tuple[str, str]:
        payload = gateway.get_folder(folder_id)
        if not isinstance(payload, Mapping):
            raise TypeError(f"{label} root metadata was not an object")
        name = _safe_text(payload.get("name"))
        mime_type = _safe_text(payload.get("mimeType"))
        if mime_type != FOLDER_MIME_TYPE:
            raise ValueError(f"{label} root is not a folder")
        return name, mime_type

    def _scan_drive(
        self,
        gateway: ReadOnlyGoogleGateway,
        *,
        root_id: str,
        root_name: str,
        root_mime_type: str,
        label: str,
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []

        def walk(
            folder_id: str,
            folder_name: str,
            mime_type: str,
            depth: int,
            path_labels: list[str],
        ) -> None:
            record: dict[str, object] = {
                "name": folder_name,
                "mime_type": mime_type,
                "item_type": "folder",
                "depth": depth,
                "child_count": None,
                "path_labels": path_labels,
                "truncated": False,
            }
            records.append(record)
            if depth >= self._max_depth:
                self._warnings.append(
                    f"{label} max_depth reached at " + " / ".join(path_labels)
                )
                return

            try:
                payload = gateway.list_inventory_children(
                    folder_id, item_limit=DRIVE_ITEM_LIMIT
                )
            except Exception as error:
                self._record_error(
                    f"{label}_folder_list",
                    error,
                    additional_secrets=(folder_id,),
                )
                return
            if not isinstance(payload, Mapping):
                self._record_error(
                    f"{label}_folder_list", TypeError("Drive list was not an object")
                )
                return

            raw_items = payload.get("files")
            items = raw_items if isinstance(raw_items, list) else []
            bounded_items = items[:DRIVE_ITEM_LIMIT]
            truncated = bool(payload.get("nextPageToken")) or len(items) > DRIVE_ITEM_LIMIT
            record["child_count"] = len(bounded_items)
            record["truncated"] = truncated
            if truncated:
                self._warnings.append(
                    f"{label} item limit reached at " + " / ".join(path_labels)
                )

            for raw_item in bounded_items:
                if not isinstance(raw_item, Mapping):
                    continue
                child_id = raw_item.get("id")
                name = _safe_text(raw_item.get("name"))
                child_mime = _safe_text(raw_item.get("mimeType"))
                if child_mime == FOLDER_MIME_TYPE:
                    if not isinstance(child_id, str) or not child_id:
                        self._warnings.append(
                            f"{label} skipped folder without internal identifier"
                        )
                        continue
                    walk(
                        child_id,
                        name,
                        child_mime,
                        depth + 1,
                        [*path_labels, name],
                    )
                    continue
                records.append(
                    {
                        "name": name,
                        "mime_type": child_mime,
                        "item_type": "file",
                        "depth": depth + 1,
                        "child_count": 0,
                        "extension": _extension(name),
                        "path_labels": list(path_labels),
                    }
                )

        walk(root_id, root_name, root_mime_type, 0, [root_name])
        return records

    @staticmethod
    def _is_descendant(record: Mapping[str, object], path: list[str]) -> bool:
        labels = record.get("path_labels")
        if (
            not isinstance(labels, list)
            or len(labels) < len(path)
            or labels[: len(path)] != path
        ):
            return False
        if record.get("item_type") == "file":
            return True
        return labels != path

    def _option_summary(
        self, records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        option_folders = [
            record
            for record in records
            if record.get("item_type") == "folder"
            and str(record.get("name", "")).casefold() == "option"
        ]
        for option in option_folders:
            option_path = option.get("path_labels")
            option_depth = option.get("depth")
            if not isinstance(option_path, list) or not isinstance(option_depth, int):
                continue
            series_folders = [
                record
                for record in records
                if record.get("item_type") == "folder"
                and record.get("depth") == option_depth + 1
                and isinstance(record.get("path_labels"), list)
                and record["path_labels"][:-1] == option_path
            ]
            for series in series_folders:
                series_path = series["path_labels"]
                series_depth = int(series["depth"])
                descendants = [
                    record
                    for record in records
                    if record is not series
                    and self._is_descendant(record, series_path)
                ]
                files = [
                    record
                    for record in descendants
                    if record.get("item_type") == "file"
                ]
                summaries.append(
                    {
                        "series_name": series.get("name", ""),
                        "folder_count": sum(
                            1
                            for record in descendants
                            if record.get("item_type") == "folder"
                        ),
                        "file_count": len(files),
                        "extensions": sorted(
                            {
                                str(record.get("extension"))
                                for record in files
                                if record.get("extension")
                            }
                        ),
                        "first_level_names": [
                            str(record.get("name", ""))
                            for record in descendants
                            if record.get("depth") == series_depth + 1
                        ],
                    }
                )
        return summaries

    def _md_top_level_summary(
        self, records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        for top_folder in records:
            if (
                top_folder.get("item_type") != "folder"
                or top_folder.get("depth") != 1
            ):
                continue
            top_path = top_folder.get("path_labels")
            if not isinstance(top_path, list):
                continue
            descendants = [
                record
                for record in records
                if record is not top_folder
                and self._is_descendant(record, top_path)
            ]
            files = [
                record
                for record in descendants
                if record.get("item_type") == "file"
            ]
            extension_counts = Counter(
                str(record.get("extension"))
                for record in files
                if record.get("extension")
            )
            summaries.append(
                {
                    "name": top_folder.get("name", ""),
                    "subfolder_count": sum(
                        1
                        for record in descendants
                        if record.get("item_type") == "folder"
                    ),
                    "image_count": sum(
                        count
                        for extension, count in extension_counts.items()
                        if extension in IMAGE_EXTENSIONS
                    ),
                    "extensions": dict(sorted(extension_counts.items())),
                    "file_name_samples": [
                        str(record.get("name", "")) for record in files[:10]
                    ],
                }
            )
        return summaries

    def _spreadsheet_inventory(
        self, gateway: ReadOnlyGoogleGateway
    ) -> dict[str, object]:
        payload = gateway.get_inventory_spreadsheet(
            self._settings.clm_spreadsheet_id
        )
        if not isinstance(payload, Mapping):
            raise TypeError("Spreadsheet metadata was not an object")
        properties = payload.get("properties")
        title = (
            _safe_text(properties.get("title"))
            if isinstance(properties, Mapping)
            else ""
        )
        raw_sheets = payload.get("sheets")
        sheets = raw_sheets if isinstance(raw_sheets, list) else []
        sheet_reports: list[dict[str, object]] = []
        for sheet in sheets:
            sheet_properties = (
                sheet.get("properties") if isinstance(sheet, Mapping) else None
            )
            if not isinstance(sheet_properties, Mapping):
                continue
            sheet_title = _safe_text(sheet_properties.get("title"))
            grid = sheet_properties.get("gridProperties")
            grid_properties = grid if isinstance(grid, Mapping) else {}
            row_count = _safe_int(grid_properties.get("rowCount"))
            column_count = _safe_int(grid_properties.get("columnCount"))
            sample_rows: list[list[str]] = []
            sample_success = False
            if row_count > 0 and column_count > 0:
                try:
                    sample = gateway.get_inventory_sheet_sample(
                        self._settings.clm_spreadsheet_id, sheet_title
                    )
                    if isinstance(sample, Mapping):
                        raw_rows = sample.get("values")
                        if isinstance(raw_rows, list):
                            sample_rows = [
                                [_safe_cell(cell) for cell in row[:SHEET_COLUMN_LIMIT]]
                                for row in raw_rows[:SHEET_ROW_LIMIT]
                                if isinstance(row, list)
                            ]
                        sample_success = True
                    else:
                        raise TypeError("Sheet sample was not an object")
                except Exception as error:
                    self._record_error("sheet_sample_get", error)
            else:
                sample_success = True
            structure = _sheet_structure(sample_rows)
            sheet_reports.append(
                {
                    "title": sheet_title,
                    "row_count": row_count,
                    "column_count": column_count,
                    "index": _safe_int(sheet_properties.get("index")),
                    "hidden": _safe_bool(sheet_properties.get("hidden")),
                    "frozen_row_count": _safe_int(
                        grid_properties.get("frozenRowCount")
                    ),
                    "frozen_column_count": _safe_int(
                        grid_properties.get("frozenColumnCount")
                    ),
                    "sample_read_success": sample_success,
                    "sample_rows": sample_rows,
                    **structure,
                }
            )
        return {"title": title, "sheet_count": len(sheet_reports), "sheets": sheet_reports}

    def _finalize(
        self,
        report: dict[str, object],
        gateway: ReadOnlyGoogleGateway | None,
    ) -> dict[str, object]:
        report["read_requests_performed"] = (
            gateway.counters.read_requests_performed if gateway is not None else 0
        )
        report["write_requests_performed"] = 0
        report["warnings"] = self._warnings
        report["errors"] = self._errors
        if self._errors:
            report["status"] = "partial" if gateway is not None else "error"
        else:
            report["status"] = "ok"
        sanitized = self._redactor.value(report)
        if not isinstance(sanitized, dict):
            raise TypeError("Supplier inventory report sanitization failed")
        self._log(
            "supplier_inventory_complete",
            status=report["status"],
            read_requests_performed=report["read_requests_performed"],
            write_requests_performed=0,
        )
        return sanitized

    def run(self) -> dict[str, object]:
        self._settings.validate()
        report = self._base_report()
        self._log("supplier_inventory_started", max_depth=self._max_depth)
        try:
            clients = self._factory.create(self._settings)
        except Exception as error:
            self._record_error("google_client_creation", error)
            return self._finalize(report, None)

        gateway = ReadOnlyGoogleGateway(clients)
        try:
            root_name, root_mime = self._folder_metadata(
                gateway, "clm", self._settings.clm_drive_folder_id
            )
            clm_tree = self._scan_drive(
                gateway,
                root_id=self._settings.clm_drive_folder_id,
                root_name=root_name,
                root_mime_type=root_mime,
                label="clm",
            )
            report["clm"] = {
                "root_name": root_name,
                "tree": clm_tree,
                "option_summary": self._option_summary(clm_tree),
            }
        except Exception as error:
            self._record_error("clm_inventory", error)

        try:
            root_name, root_mime = self._folder_metadata(
                gateway, "md", self._settings.md_drive_folder_id
            )
            md_tree = self._scan_drive(
                gateway,
                root_id=self._settings.md_drive_folder_id,
                root_name=root_name,
                root_mime_type=root_mime,
                label="md",
            )
            report["md"] = {
                "root_name": root_name,
                "tree": md_tree,
                "top_level_summary": self._md_top_level_summary(md_tree),
            }
        except Exception as error:
            self._record_error("md_inventory", error)

        try:
            report["spreadsheet"] = self._spreadsheet_inventory(gateway)
        except Exception as error:
            self._record_error("spreadsheet_inventory", error)

        return self._finalize(report, gateway)
