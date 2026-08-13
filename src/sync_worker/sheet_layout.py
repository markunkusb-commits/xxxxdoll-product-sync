"""Safe, bounded inspection of complex Google Sheet grid layouts."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .config import GoogleSettings
from .google_api import (
    GoogleClientFactory,
    ReadOnlyGoogleGateway,
    google_redactor_for_settings,
)
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


MAX_SHEET_TITLE_LENGTH = 150
MAX_RANGE_ROWS = 100
MAX_RANGE_COLUMNS = 52
MAX_RANGE_CELLS = 5200
_A1_RANGE_PATTERN = re.compile(
    r"^([A-Za-z]+)([1-9][0-9]*):([A-Za-z]+)([1-9][0-9]*)$"
)
_URL_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)[^\s\"'<>]+"
    r"|\b(?:drive|docs)\.google\.com(?:/[^\s\"'<>]*)?"
)
_SAFE_REPORT_NAME_PATTERN = re.compile(r"[^\w-]+", re.UNICODE)


class SheetLayoutValidationError(ValueError):
    """Raised before client creation for an invalid sheet layout request."""


class SheetLayoutResponseError(RuntimeError):
    """Raised for a malformed or non-matching Sheets response."""


@dataclass(frozen=True, slots=True)
class A1Range:
    a1: str
    start_row: int
    end_row: int
    start_column: str
    end_column: str
    start_column_index: int
    end_column_index: int

    @property
    def row_count(self) -> int:
        return self.end_row - self.start_row + 1

    @property
    def column_count(self) -> int:
        return self.end_column_index - self.start_column_index + 1

    @property
    def cell_count(self) -> int:
        return self.row_count * self.column_count


def column_label_to_index(label: str) -> int:
    """Convert an A1 column label to a one-based integer index."""
    normalized = label.upper()
    if not normalized or not normalized.isalpha() or not normalized.isascii():
        raise SheetLayoutValidationError("Invalid A1 column label")
    value = 0
    for character in normalized:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def column_index_to_label(index: int) -> str:
    """Convert a positive one-based column index to an A1 label."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise SheetLayoutValidationError("Column index must be positive")
    result = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def validate_sheet_title(value: str) -> str:
    if not isinstance(value, str):
        raise SheetLayoutValidationError("Sheet title must be text")
    title = value.strip()
    if not 1 <= len(title) <= MAX_SHEET_TITLE_LENGTH:
        raise SheetLayoutValidationError(
            "Sheet title must contain 1 to 150 characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise SheetLayoutValidationError("Sheet title contains control characters")
    return title


def parse_a1_range(value: str) -> A1Range:
    if not isinstance(value, str):
        raise SheetLayoutValidationError("Range must be A1-style text")
    normalized = value.strip().upper()
    match = _A1_RANGE_PATTERN.fullmatch(normalized)
    if match is None:
        raise SheetLayoutValidationError(
            "Range must be one bounded A1-style area such as A1:AZ50"
        )
    start_column, start_row_text, end_column, end_row_text = match.groups()
    start_row = int(start_row_text)
    end_row = int(end_row_text)
    start_column_index = column_label_to_index(start_column)
    end_column_index = column_label_to_index(end_column)
    if end_row < start_row or end_column_index < start_column_index:
        raise SheetLayoutValidationError("Range end must not precede range start")

    parsed = A1Range(
        a1=f"{start_column}{start_row}:{end_column}{end_row}",
        start_row=start_row,
        end_row=end_row,
        start_column=start_column,
        end_column=end_column,
        start_column_index=start_column_index,
        end_column_index=end_column_index,
    )
    if parsed.cell_count > MAX_RANGE_CELLS:
        raise SheetLayoutValidationError("Range may contain at most 5200 cells")
    if parsed.row_count > MAX_RANGE_ROWS:
        raise SheetLayoutValidationError("Range may contain at most 100 rows")
    if parsed.column_count > MAX_RANGE_COLUMNS or end_column_index > 52:
        raise SheetLayoutValidationError("Range may not extend beyond column AZ")
    return parsed


def safe_sheet_report_filename(sheet_title: str) -> str:
    title = validate_sheet_title(sheet_title)
    safe_name = _SAFE_REPORT_NAME_PATTERN.sub("-", title).strip("-_")
    safe_name = re.sub(r"-+", "-", safe_name)[:100].rstrip("-_")
    if not safe_name:
        safe_name = "sheet"
    return f"sheet-layout-{safe_name}.json"


def _safe_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _sanitize_content(value: object, redactor: Redactor) -> str:
    if not isinstance(value, str):
        return ""
    without_urls = _URL_PATTERN.sub("[URL_REDACTED]", value)
    without_explicit_secrets = REPORT_SECRET_SCAN_PATTERN.sub(
        "[REDACTED_SECRET]", without_urls
    )
    return redactor.text(without_explicit_secrets, limit=2000)


def _grid_range_to_report(raw_range: object) -> dict[str, object] | None:
    if not isinstance(raw_range, Mapping):
        return None
    start_row = _safe_int(raw_range.get("startRowIndex"))
    end_row = _safe_int(raw_range.get("endRowIndex"))
    start_column = _safe_int(raw_range.get("startColumnIndex"))
    end_column = _safe_int(raw_range.get("endColumnIndex"))
    if None in (start_row, end_row, start_column, end_column):
        return None
    assert start_row is not None
    assert end_row is not None
    assert start_column is not None
    assert end_column is not None
    if end_row <= start_row or end_column <= start_column:
        return None

    start_row_human = start_row + 1
    end_row_human = end_row
    start_column_label = column_index_to_label(start_column + 1)
    end_column_label = column_index_to_label(end_column)
    anchor = f"{start_column_label}{start_row_human}"
    return {
        "range": (
            f"{start_column_label}{start_row_human}:"
            f"{end_column_label}{end_row_human}"
        ),
        "start_row": start_row_human,
        "end_row": end_row_human,
        "start_column": start_column_label,
        "end_column": end_column_label,
        "anchor": anchor,
        "_start_row_index": start_row,
        "_end_row_index": end_row,
        "_start_column_index": start_column,
        "_end_column_index": end_column,
    }


def _merge_intersects_request(merge: Mapping[str, object], requested: A1Range) -> bool:
    request_start_row = requested.start_row - 1
    request_end_row = requested.end_row
    request_start_column = requested.start_column_index - 1
    request_end_column = requested.end_column_index
    return (
        int(merge["_start_row_index"]) < request_end_row
        and int(merge["_end_row_index"]) > request_start_row
        and int(merge["_start_column_index"]) < request_end_column
        and int(merge["_end_column_index"]) > request_start_column
    )


def _cell_merge(
    row_index: int,
    column_index: int,
    merges: list[dict[str, object]],
) -> dict[str, object] | None:
    for merge in merges:
        if (
            int(merge["_start_row_index"]) <= row_index
            < int(merge["_end_row_index"])
            and int(merge["_start_column_index"]) <= column_index
            < int(merge["_end_column_index"])
        ):
            return merge
    return None


def _public_merge(merge: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in merge.items() if not str(key).startswith("_")
    }


class SheetLayoutInspector:
    """Inspect one validated grid range without interpreting product semantics."""

    def __init__(
        self,
        settings: GoogleSettings,
        factory: GoogleClientFactory,
        *,
        sheet_title: str,
        a1_range: str,
        redactor: Redactor | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._sheet_title = validate_sheet_title(sheet_title)
        self._requested = parse_a1_range(a1_range)
        self._redactor = redactor or google_redactor_for_settings(settings)
        self._logger = logger or logging.getLogger("sync_worker.sheet_layout")
        self._warnings: list[str] = []
        self._errors: list[dict[str, str]] = []

    def _log(self, event: str, **fields: object) -> None:
        payload = self._redactor.value({"event": event, **fields})
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _record_error(self, check: str, error: BaseException) -> None:
        summary = _URL_PATTERN.sub(
            "[URL_REDACTED]", self._redactor.exception(error)
        )
        self._errors.append({"check": check, "summary": summary})
        self._log("sheet_layout_check_failed", check=check, error=summary)

    def _base_report(self) -> dict[str, object]:
        return {
            "status": "error",
            "spreadsheet_title": "",
            "sheet_title": _sanitize_content(self._sheet_title, self._redactor),
            "requested_range": self._requested.a1,
            "returned_range": "",
            "row_start": self._requested.start_row,
            "row_end": self._requested.end_row,
            "column_start": self._requested.start_column,
            "column_end": self._requested.end_column,
            "non_empty_cell_count": 0,
            "non_empty_cells": [],
            "merged_range_count": 0,
            "merged_ranges": [],
            "row_summary": [],
            "read_requests_performed": 0,
            "write_requests_performed": 0,
            "warnings": self._warnings,
            "errors": self._errors,
        }

    def _select_sheet(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        raw_sheets = payload.get("sheets")
        if not isinstance(raw_sheets, list):
            raise SheetLayoutResponseError("Sheets response did not contain sheets")
        for sheet in raw_sheets:
            if not isinstance(sheet, Mapping):
                continue
            properties = sheet.get("properties")
            if (
                isinstance(properties, Mapping)
                and properties.get("title") == self._sheet_title
            ):
                return sheet
        raise SheetLayoutResponseError("Requested sheet was not returned")

    def _parse_merges(
        self, sheet: Mapping[str, object]
    ) -> list[dict[str, object]]:
        raw_merges = sheet.get("merges")
        if raw_merges is None:
            return []
        if not isinstance(raw_merges, list):
            self._warnings.append("Malformed merge metadata was ignored")
            return []
        parsed: list[dict[str, object]] = []
        for raw_merge in raw_merges:
            merge = _grid_range_to_report(raw_merge)
            if merge is not None and _merge_intersects_request(
                merge, self._requested
            ):
                parsed.append(merge)
        return parsed

    def _parse_cells(
        self,
        sheet: Mapping[str, object],
        merges: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        raw_data = sheet.get("data")
        if raw_data is None:
            self._warnings.append("GridData was empty or omitted")
            return []
        if not isinstance(raw_data, list):
            raise SheetLayoutResponseError("GridData was malformed")

        cells: list[dict[str, object]] = []
        seen: set[tuple[int, int]] = set()
        for grid in raw_data:
            if not isinstance(grid, Mapping):
                continue
            start_row = _safe_int(grid.get("startRow"))
            start_column = _safe_int(grid.get("startColumn"))
            if start_row is None:
                start_row = self._requested.start_row - 1
            if start_column is None:
                start_column = self._requested.start_column_index - 1
            raw_rows = grid.get("rowData")
            if raw_rows is None:
                continue
            if not isinstance(raw_rows, list):
                raise SheetLayoutResponseError("GridData rowData was malformed")
            for row_offset, raw_row in enumerate(raw_rows):
                if not isinstance(raw_row, Mapping):
                    continue
                row_index = start_row + row_offset
                if not self._requested.start_row - 1 <= row_index < self._requested.end_row:
                    continue
                raw_values = raw_row.get("values")
                if raw_values is None:
                    continue
                if not isinstance(raw_values, list):
                    raise SheetLayoutResponseError("RowData values were malformed")
                for cell_offset, raw_cell in enumerate(raw_values):
                    column_index = start_column + cell_offset
                    if not (
                        self._requested.start_column_index - 1
                        <= column_index
                        < self._requested.end_column_index
                    ):
                        continue
                    if not isinstance(raw_cell, Mapping):
                        continue
                    formatted_value = raw_cell.get("formattedValue")
                    if not isinstance(formatted_value, str) or formatted_value == "":
                        continue
                    location = (row_index, column_index)
                    if location in seen:
                        continue
                    seen.add(location)
                    row_number = row_index + 1
                    column_number = column_index + 1
                    column_label = column_index_to_label(column_number)
                    coordinate = f"{column_label}{row_number}"
                    merge = _cell_merge(row_index, column_index, merges)
                    cells.append(
                        {
                            "coordinate": coordinate,
                            "row": row_number,
                            "column": column_label,
                            "column_index": column_number,
                            "formatted_value": _sanitize_content(
                                formatted_value, self._redactor
                            ),
                            "is_merged": merge is not None,
                            "is_merge_anchor": (
                                merge is not None and merge["anchor"] == coordinate
                            ),
                            "merged_range": (
                                merge["range"] if merge is not None else None
                            ),
                        }
                    )
        return sorted(cells, key=lambda cell: (int(cell["row"]), int(cell["column_index"])))

    @staticmethod
    def _row_summary(cells: list[dict[str, object]]) -> list[dict[str, object]]:
        rows: dict[int, list[dict[str, object]]] = {}
        for cell in cells:
            row = int(cell["row"])
            rows.setdefault(row, []).append(
                {
                    "coordinate": cell["coordinate"],
                    "value": cell["formatted_value"],
                }
            )
        return [
            {"row": row, "non_empty_cells": rows[row]} for row in sorted(rows)
        ]

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
        report["status"] = "error" if self._errors else "ok"
        sanitized = self._redactor.value(report)
        if not isinstance(sanitized, dict):
            raise TypeError("Sheet layout report sanitization failed")
        self._log(
            "sheet_layout_complete",
            status=report["status"],
            read_requests_performed=report["read_requests_performed"],
            write_requests_performed=0,
        )
        return sanitized

    def run(self) -> dict[str, object]:
        self._settings.validate()
        report = self._base_report()
        self._log("sheet_layout_started")
        try:
            clients = self._factory.create(self._settings)
        except Exception as error:
            self._record_error("google_client_creation", error)
            return self._finalize(report, None)

        gateway = ReadOnlyGoogleGateway(clients)
        try:
            payload = gateway.inspect_sheet_layout(
                self._settings.clm_spreadsheet_id,
                self._sheet_title,
                self._requested.a1,
            )
            if not isinstance(payload, Mapping):
                raise SheetLayoutResponseError("Sheets response was not an object")
            properties = payload.get("properties")
            if isinstance(properties, Mapping):
                report["spreadsheet_title"] = _sanitize_content(
                    properties.get("title"), self._redactor
                )
            sheet = self._select_sheet(payload)
            merges = self._parse_merges(sheet)
            cells = self._parse_cells(sheet, merges)
            report.update(
                {
                    "returned_range": self._requested.a1,
                    "non_empty_cell_count": len(cells),
                    "non_empty_cells": cells,
                    "merged_range_count": len(merges),
                    "merged_ranges": [_public_merge(merge) for merge in merges],
                    "row_summary": self._row_summary(cells),
                }
            )
        except Exception as error:
            self._record_error("sheet_layout_read", error)
        return self._finalize(report, gateway)
