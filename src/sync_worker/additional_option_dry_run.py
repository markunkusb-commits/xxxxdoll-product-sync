"""Local dry-run report generation for Additional Option layouts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .additional_option_parser import (
    AdditionalOptionRecord,
    RawAdditionalOptionEntry,
    parse_additional_options,
)
from .report import SafeJsonReportWriter
from .sanitization import Redactor


CATEGORY_NAMES = ("appearance", "material", "function", "accessory", "other")
REPORT_FILENAME = "additional-option-dry-run.json"


class AdditionalOptionInputError(ValueError):
    """Safe validation error for a local Additional Option layout."""


def load_local_option_layout(input_path: Path) -> Mapping[str, object]:
    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise AdditionalOptionInputError("Input must be a JSON file")
    if not path.exists():
        raise AdditionalOptionInputError("Input JSON file does not exist")
    if not path.is_file():
        raise AdditionalOptionInputError("Input JSON path must be a file")
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AdditionalOptionInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(layout, Mapping):
        raise AdditionalOptionInputError("Input JSON root must be an object")
    return layout


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _option_report(option: AdditionalOptionRecord) -> dict[str, object]:
    return {
        "category": option.category,
        "option_name": option.identity.option_name,
        "price": {
            "amount": option.pricing.amount,
            "currency": option.pricing.currency,
            "raw_price": option.pricing.raw_price,
            "price_range": option.pricing.price_range,
        },
        "source_coordinate": option.source.raw_coordinate,
        "warnings": list(option.warnings),
    }


def _raw_entry_report(entry: RawAdditionalOptionEntry) -> dict[str, object]:
    return {
        "raw_value": entry.raw_value,
        "source_coordinate": entry.source.raw_coordinate,
        "warnings": list(entry.warnings),
    }


def build_additional_option_report(
    layout: Mapping[str, object],
    *,
    input_file: str,
) -> dict[str, object]:
    result = parse_additional_options(layout)
    category_summary = {category: 0 for category in CATEGORY_NAMES}
    option_reports: list[dict[str, object]] = []
    warnings_count = 0
    for option in result.options:
        category_summary[option.category] += 1
        option_reports.append(_option_report(option))
        warnings_count += len(option.warnings)
    raw_entry_reports = [_raw_entry_report(item) for item in result.raw_entries]
    warnings_count += sum(len(item.warnings) for item in result.raw_entries)
    return {
        "status": "ok",
        "input_file": input_file,
        "detected_option_count": len(option_reports),
        "category_summary": category_summary,
        "warnings_count": warnings_count,
        "errors_count": 0,
        "options": option_reports,
        "raw_entries": raw_entry_reports,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
    }


def run_additional_option_dry_run(
    input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    path = Path(input_path)
    layout = load_local_option_layout(path)
    report = build_additional_option_report(
        layout,
        input_file=_safe_input_reference(path, project_root),
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, redactor or Redactor()).write(report)
    return report, report_path
