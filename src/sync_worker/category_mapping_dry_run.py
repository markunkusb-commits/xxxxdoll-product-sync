"""Pure-local Category Mapping reality-check adapter and safe report."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from . import category_mapping
from .category_mapping import (
    CATEGORY_REGISTRY_VERSION,
    CategoryMappingResult,
)
from .product_model import ProductRecord
from .product_size_enrichment_dry_run import (
    load_local_json_report,
    restore_product_records,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .sku_policy import select_sku_identity


REPORT_FILENAME = "category-mapping-dry-run.json"
_REPORT_URL_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>]+")


class CategoryMappingDryRunInputError(ValueError):
    """Safe structural error for a local Product report."""


_MISSING_SERIES_RESTORE_SENTINEL = "category-missing-series"


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved = input_path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _product_identity(product: ProductRecord) -> str | None:
    """Reuse the existing stable identity selection without category inference."""

    identity, _ = select_sku_identity(product)
    return identity


def restore_category_product_records(
    report: Mapping[str, object],
) -> list[ProductRecord]:
    """Reuse canonical CLM restoration while preserving explicit missing series."""

    raw_products = report.get("products")
    if not isinstance(raw_products, list):
        raise CategoryMappingDryRunInputError("products must be an array")
    prepared_products: list[object] = []
    missing_series_indexes: set[int] = set()
    for index, raw_product in enumerate(raw_products):
        if not isinstance(raw_product, Mapping):
            prepared_products.append(raw_product)
            continue
        series = raw_product.get("series")
        if series is None or (isinstance(series, str) and not series.strip()):
            prepared = dict(raw_product)
            prepared["series"] = _MISSING_SERIES_RESTORE_SENTINEL
            prepared_products.append(prepared)
            missing_series_indexes.add(index)
        else:
            prepared_products.append(raw_product)

    products = restore_product_records({"products": prepared_products})
    return [
        replace(
            product,
            identity=replace(product.identity, series=None),  # type: ignore[arg-type]
        )
        if index in missing_series_indexes
        else product
        for index, product in enumerate(products)
    ]


def _result_report(
    product: ProductRecord,
    result: CategoryMappingResult,
) -> dict[str, object]:
    return {
        "product_identity": _product_identity(product),
        "series": result.series,
        "category": {
            "status": result.status,
            "category_key": result.category_key,
            "display_name": result.display_name,
            "woo_category_id": result.woo_category_id,
            "registry_version": result.registry_version,
        },
        "source": {
            "start_row": product.source.start_row,
            "end_row": product.source.end_row,
        },
        "warnings": list(result.warnings),
        "blocking_issues": list(result.blocking_issues),
    }


def _report_redactor(report: dict[str, object], redactor: Redactor) -> Redactor:
    serialized = json.dumps(report, ensure_ascii=False)
    discovered = tuple(
        match.group(0)
        for pattern in (REPORT_SECRET_SCAN_PATTERN, _REPORT_URL_PATTERN)
        for match in pattern.finditer(serialized)
    )
    return Redactor.from_values((*redactor.secrets, *discovered))


def build_category_mapping_dry_run_report(
    products: Sequence[ProductRecord],
    *,
    input_file: str,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Use the canonical unbound registry and project an allowlisted report."""

    if isinstance(products, (str, bytes)) or not isinstance(products, Sequence):
        raise CategoryMappingDryRunInputError("products must be a sequence")
    if not isinstance(input_file, str) or not input_file:
        raise CategoryMappingDryRunInputError("input_file must be text")

    registry = category_mapping.CategoryRegistry()
    batch = category_mapping.map_categories(products, registry)
    results = [
        _result_report(product, result)
        for product, result in zip(products, batch.results, strict=True)
    ]
    report: dict[str, object] = {
        "status": "ok",
        "input_file": input_file,
        "registry": {
            "version": CATEGORY_REGISTRY_VERSION,
            "woo_binding_enabled": False,
        },
        "summary": {
            "total_products": batch.summary.total_products,
            "mapped_internal": batch.summary.mapped_internal,
            "mapped_woo": batch.summary.mapped_woo,
            "missing_series": batch.summary.missing_series,
            "unsupported_series": batch.summary.unsupported_series,
            "unbound_woo_category": batch.summary.unbound_woo_category,
            "blocking_products": sum(
                bool(result.blocking_issues) for result in batch.results
            ),
        },
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": results,
    }
    active_redactor = redactor or Redactor()
    sanitized = sanitize_report_data(
        report,
        _report_redactor(report, active_redactor),
    )
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("Category Mapping dry-run report must be an object")
    return sanitized


def run_category_mapping_dry_run(
    product_input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Restore one local CLM report and write one safe category audit."""

    input_path = Path(product_input_path)
    product_report = load_local_json_report(input_path)
    products = restore_category_product_records(product_report)
    active_redactor = redactor or Redactor()
    report = build_category_mapping_dry_run_report(
        products,
        input_file=_safe_input_reference(input_path, project_root),
        redactor=active_redactor,
    )
    output_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output_path, active_redactor).write(report)
    return report, output_path
