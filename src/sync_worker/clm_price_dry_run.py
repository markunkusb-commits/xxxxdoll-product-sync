"""Pure-local CLM price-list parsing and allowlisted report generation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .clm_price_parser import CLMProductBlock, parse_clm_price_layout
from .report import SafeJsonReportWriter
from .sanitization import Redactor


SERIES_NAMES = ("classic", "pro", "ulw", "ultra")
REPORT_FILENAME = "clm-parser-dry-run.json"


class CLMPriceListInputError(ValueError):
    """Safe validation error for a local sheet-layout snapshot."""


def load_local_sheet_layout(input_path: Path) -> Mapping[str, object]:
    """Load one local UTF-8 JSON snapshot without consulting configuration."""
    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise CLMPriceListInputError("Input must be a JSON file")
    if not path.exists():
        raise CLMPriceListInputError("Input JSON file does not exist")
    if not path.is_file():
        raise CLMPriceListInputError("Input JSON path must be a file")
    try:
        raw_layout = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CLMPriceListInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(raw_layout, Mapping):
        raise CLMPriceListInputError("Input JSON root must be an object")
    return raw_layout


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    """Retain a project-relative path, never an external absolute path."""
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _product_report(product: CLMProductBlock) -> dict[str, object]:
    """Project the parser model onto the explicitly permitted report fields."""
    return {
        "series": product.series,
        "raw_series_title": product.raw_series_title,
        "model": product.model,
        "specifications": dict(product.specifications),
        "pricing": asdict(product.pricing),
        "included_features": list(product.included_features),
        "upgrade_options": [asdict(item) for item in product.upgrade_options],
        "notices": list(product.notices),
        "source": asdict(product.source),
        "warnings": list(product.warnings),
    }


def build_clm_parser_report(
    layout: Mapping[str, object],
    *,
    input_file: str,
) -> dict[str, object]:
    """Parse a layout and build a deterministic, URL-safe dry-run report."""
    products = parse_clm_price_layout(layout)
    series_summary = {series: 0 for series in SERIES_NAMES}
    product_reports: list[dict[str, object]] = []
    warnings_count = 0
    for product in products:
        series_summary[product.series] += 1
        product_reports.append(_product_report(product))
        warnings_count += len(product.warnings)
    return {
        "status": "ok",
        "input_file": input_file,
        "detected_product_count": len(product_reports),
        "series_summary": series_summary,
        "warnings_count": warnings_count,
        "errors_count": 0,
        "products": product_reports,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
    }


def run_clm_parser_dry_run(
    input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Run the local parser and persist only its sanitized dry-run report."""
    path = Path(input_path)
    layout = load_local_sheet_layout(path)
    report = build_clm_parser_report(
        layout,
        input_file=_safe_input_reference(path, project_root),
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, redactor or Redactor()).write(report)
    return report, report_path
