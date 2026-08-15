"""Pure-local Size List dry-run report generation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .report import SafeJsonReportWriter
from .sanitization import Redactor
from .size_list_parser import SizeRecord, parse_size_list


REPORT_FILENAME = "size-list-dry-run.json"


class SizeListInputError(ValueError):
    """Safe validation error for a local Size List layout snapshot."""


def load_local_size_layout(input_path: Path) -> Mapping[str, object]:
    """Read one local UTF-8 layout JSON without loading project configuration."""
    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise SizeListInputError("Input must be a JSON file")
    if not path.exists():
        raise SizeListInputError("Input JSON file does not exist")
    if not path.is_file():
        raise SizeListInputError("Input JSON path must be a file")
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SizeListInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(layout, Mapping):
        raise SizeListInputError("Input JSON root must be an object")
    return layout


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _record_report(record: SizeRecord) -> dict[str, object]:
    """Project only explicitly approved SizeRecord fields."""
    return {
        "body_type": record.identity.body_type,
        "raw_body_type": record.identity.raw_body_type,
        "type": record.classification.type,
        "raw_type": record.classification.raw_type,
        "supplier_costs": asdict(record.supplier_costs),
        "measurements": asdict(record.measurements),
        "raw_measurements": [asdict(item) for item in record.raw_measurements],
        "source": asdict(record.source),
        "warnings": list(record.warnings),
    }


def build_size_list_report(
    layout: Mapping[str, object],
    *,
    input_file: str,
) -> dict[str, object]:
    """Call the existing parser and build its deterministic dry-run summary."""
    records = parse_size_list(layout)
    type_summary: dict[str, int] = {}
    report_records: list[dict[str, object]] = []
    warnings_count = 0
    records_with_warnings = 0
    records_with_missing_type = 0
    records_with_ambiguous_merge = 0
    records_with_fob_price = 0

    for record in records:
        if record.classification.type is None:
            records_with_missing_type += 1
        else:
            type_summary[record.classification.type] = (
                type_summary.get(record.classification.type, 0) + 1
            )
        record_warning_count = len(record.warnings)
        warnings_count += record_warning_count
        if record_warning_count:
            records_with_warnings += 1
        if any(
            "ambiguous merged measurement" in warning
            for warning in record.warnings
        ):
            records_with_ambiguous_merge += 1
        if record.supplier_costs.fob_price is not None:
            records_with_fob_price += 1
        report_records.append(_record_report(record))

    return {
        "status": "ok",
        "input_file": input_file,
        "detected_record_count": len(report_records),
        "type_summary": type_summary,
        "warnings_count": warnings_count,
        "errors_count": 0,
        "records_with_warnings": records_with_warnings,
        "records_with_missing_type": records_with_missing_type,
        "records_with_ambiguous_merge": records_with_ambiguous_merge,
        "records_with_fob_price": records_with_fob_price,
        "records_without_fob_price": (
            len(report_records) - records_with_fob_price
        ),
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "records": report_records,
    }


def run_size_list_dry_run(
    input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Parse one local layout and persist its sanitized dry-run report."""
    path = Path(input_path)
    layout = load_local_size_layout(path)
    report = build_size_list_report(
        layout,
        input_file=_safe_input_reference(path, project_root),
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, redactor or Redactor()).write(report)
    return report, report_path
