"""Pure-local Product + Size enrichment dry-run adapter and report."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields
from pathlib import Path

from .clm_price_parser import (
    BlockSource,
    CLMProductBlock,
    ParsedPrice,
    Pricing,
    UpgradeOption,
)
from .product_model import MonetaryValue, ProductRecord, from_clm_product
from .product_size_enricher import (
    ProductSizeMatchResult,
    enrich_products_with_sizes,
    summarize_enrichment,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .size_list_parser import (
    NormalizedMeasurement,
    RawMeasurement,
    SizeClassification,
    SizeIdentity,
    SizeMeasurements,
    SizeRecord,
    SizeSource,
    SizeSupplierCosts,
    SupplierFOBCost,
    TwoDimensionalValue,
    UnitValue,
)


REPORT_FILENAME = "product-size-enrichment-dry-run.json"
_REPORT_URL_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>]+")


class ProductSizeEnrichmentInputError(ValueError):
    """Safe structural error that never includes supplier values."""


def load_local_json_report(input_path: Path) -> Mapping[str, object]:
    """Load one local JSON object without consulting configuration."""
    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise ProductSizeEnrichmentInputError("Input must be a JSON file")
    if not path.exists():
        raise ProductSizeEnrichmentInputError("Input JSON file does not exist")
    if not path.is_file():
        raise ProductSizeEnrichmentInputError("Input JSON path must be a file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProductSizeEnrichmentInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(payload, Mapping):
        raise ProductSizeEnrichmentInputError("Input JSON root must be an object")
    return payload


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProductSizeEnrichmentInputError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ProductSizeEnrichmentInputError(f"{label} must be an array")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductSizeEnrichmentInputError(f"{label} must be text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProductSizeEnrichmentInputError(f"{label} must be text or null")
    return value


def _number(value: object, label: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductSizeEnrichmentInputError(f"{label} must be numeric or null")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductSizeEnrichmentInputError(f"{label} must be an integer")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    items = _sequence(value, label)
    if not all(isinstance(item, str) for item in items):
        raise ProductSizeEnrichmentInputError(f"{label} must contain only text")
    return tuple(items)


def _restore_price(value: object, *, context: str) -> ParsedPrice | None:
    if value is None:
        return None
    payload = _mapping(value, f"pricing.{context}")
    return ParsedPrice(
        raw_value=_optional_text(payload.get("raw_value"), "price raw_value") or "",
        currency=_optional_text(payload.get("currency"), "price currency"),
        amount=_number(payload.get("amount"), "price amount"),
        context=(
            _optional_text(payload.get("context"), "price context") or context
        ),
    )


def _restore_pricing(value: object) -> Pricing:
    payload = _mapping(value, "product pricing")
    return Pricing(
        fob_unit_price=_restore_price(
            payload.get("fob_unit_price"), context="fob_unit_price"
        ),
        minimum_retail_price=_restore_price(
            payload.get("minimum_retail_price"),
            context="minimum_retail_price",
        ),
        normal_options_price=_restore_price(
            payload.get("normal_options_price"),
            context="normal_options_price",
        ),
        body_only_price=_restore_price(
            payload.get("body_only_price"), context="body_only_price"
        ),
        including_head_price=_restore_price(
            payload.get("including_head_price"),
            context="including_head_price",
        ),
    )


def _restore_upgrade_options(value: object) -> list[UpgradeOption]:
    options: list[UpgradeOption] = []
    for raw_option in _sequence(value, "product upgrade_options"):
        payload = _mapping(raw_option, "upgrade option")
        options.append(
            UpgradeOption(
                name=_required_text(payload.get("name"), "upgrade option name"),
                raw_value=_required_text(
                    payload.get("raw_value"), "upgrade option raw_value"
                ),
                price=_restore_price(
                    payload.get("price"), context="upgrade_option"
                ),
            )
        )
    return options


def _restore_specifications(value: object) -> dict[str, str]:
    payload = _mapping(value, "product specifications")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in payload.items()):
        raise ProductSizeEnrichmentInputError(
            "product specifications must contain only text"
        )
    return dict(payload)


def _restore_clm_product(value: object) -> CLMProductBlock:
    payload = _mapping(value, "product")
    specifications = _restore_specifications(payload.get("specifications", {}))
    source = _mapping(payload.get("source"), "product source")
    model = _optional_text(payload.get("model"), "product model")
    return CLMProductBlock(
        series=_required_text(payload.get("series"), "product series"),
        raw_series_title=_required_text(
            payload.get("raw_series_title"), "product raw_series_title"
        ),
        model=model,
        model_raw=model,
        cup=specifications.get("cup"),
        specifications=specifications,
        raw_specifications=[],
        included_features=list(
            _text_tuple(
                payload.get("included_features", []),
                "product included_features",
            )
        ),
        upgrade_options=_restore_upgrade_options(
            payload.get("upgrade_options", [])
        ),
        notices=list(_text_tuple(payload.get("notices", []), "product notices")),
        pricing=_restore_pricing(payload.get("pricing", {})),
        photo_download_link=None,
        raw_commercial_entries=[],
        source=BlockSource(
            start_row=_integer(source.get("start_row"), "product source start_row"),
            end_row=_integer(source.get("end_row"), "product source end_row"),
        ),
        warnings=list(_text_tuple(payload.get("warnings", []), "product warnings")),
    )


def restore_product_records(report: Mapping[str, object]) -> list[ProductRecord]:
    """Restore CLM blocks and use the canonical intermediate-model converter."""
    products = _sequence(report.get("products"), "products")
    return [from_clm_product(_restore_clm_product(item)) for item in products]


def _restore_unit_value(
    value: object,
    *,
    label: str,
) -> UnitValue | TwoDimensionalValue | None:
    if value is None:
        return None
    payload = _mapping(value, label)
    unit = _required_text(payload.get("unit"), f"{label} unit")
    if "length" in payload or "width" in payload:
        length = _number(payload.get("length"), f"{label} length")
        width = _number(payload.get("width"), f"{label} width")
        if length is None or width is None:
            raise ProductSizeEnrichmentInputError(
                f"{label} dimensions must be numeric"
            )
        return TwoDimensionalValue(length=length, width=width, unit=unit)
    numeric_value = _number(payload.get("value"), f"{label} value")
    if numeric_value is None:
        raise ProductSizeEnrichmentInputError(f"{label} value must be numeric")
    return UnitValue(value=numeric_value, unit=unit)


def _restore_measurement(value: object, *, label: str) -> NormalizedMeasurement | None:
    if value is None:
        return None
    payload = _mapping(value, label)
    return NormalizedMeasurement(
        metric=_restore_unit_value(payload.get("metric"), label=f"{label} metric"),
        imperial=_restore_unit_value(
            payload.get("imperial"), label=f"{label} imperial"
        ),
        raw_value=_required_text(payload.get("raw_value"), f"{label} raw_value"),
    )


def _restore_measurements(value: object) -> SizeMeasurements:
    payload = _mapping(value, "size measurements")
    restored = {
        field.name: _restore_measurement(
            payload.get(field.name), label=f"size measurement {field.name}"
        )
        for field in fields(SizeMeasurements)
    }
    return SizeMeasurements(**restored)


def _restore_fob(value: object) -> SupplierFOBCost | None:
    if value is None:
        return None
    payload = _mapping(value, "size FOB")
    return SupplierFOBCost(
        amount=_number(payload.get("amount"), "size FOB amount"),
        currency=_optional_text(payload.get("currency"), "size FOB currency"),
        raw_value=_optional_text(payload.get("raw_value"), "size FOB raw_value") or "",
    )


def _restore_raw_measurements(value: object) -> tuple[RawMeasurement, ...]:
    restored: list[RawMeasurement] = []
    for raw_measurement in _sequence(value, "size raw_measurements"):
        payload = _mapping(raw_measurement, "raw measurement")
        restored.append(
            RawMeasurement(
                fields=_text_tuple(payload.get("fields", []), "raw measurement fields"),
                raw_header=_required_text(
                    payload.get("raw_header"), "raw measurement header"
                ),
                raw_value=_required_text(
                    payload.get("raw_value"), "raw measurement value"
                ),
                coordinate=_required_text(
                    payload.get("coordinate"), "raw measurement coordinate"
                ),
                merged_range=_optional_text(
                    payload.get("merged_range"), "raw measurement merged_range"
                ),
            )
        )
    return tuple(restored)


def _restore_size_record(value: object) -> SizeRecord:
    payload = _mapping(value, "size record")
    body_type = _required_text(payload.get("body_type"), "size body_type")
    raw_body_type = _optional_text(
        payload.get("raw_body_type"), "size raw_body_type"
    ) or body_type
    normalized_body_type = " ".join(body_type.split())
    source = _mapping(payload.get("source"), "size source")
    coordinates = _mapping(source.get("coordinates", {}), "size source coordinates")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in coordinates.items()):
        raise ProductSizeEnrichmentInputError(
            "size source coordinates must contain only text"
        )
    supplier_costs = _mapping(payload.get("supplier_costs", {}), "size supplier_costs")
    return SizeRecord(
        identity=SizeIdentity(
            body_type=normalized_body_type,
            raw_body_type=raw_body_type,
            normalized_body_type=normalized_body_type,
            comparison_key=normalized_body_type.casefold(),
        ),
        classification=SizeClassification(
            type=_optional_text(payload.get("type"), "size type"),
            raw_type=_optional_text(payload.get("raw_type"), "size raw_type"),
        ),
        supplier_costs=SizeSupplierCosts(
            fob_price=_restore_fob(supplier_costs.get("fob_price"))
        ),
        measurements=_restore_measurements(payload.get("measurements", {})),
        raw_measurements=_restore_raw_measurements(
            payload.get("raw_measurements", [])
        ),
        source=SizeSource(
            row=_integer(source.get("row"), "size source row"),
            coordinates=dict(coordinates),
            type_merged_range=_optional_text(
                source.get("type_merged_range"), "size source type_merged_range"
            ),
        ),
        warnings=_text_tuple(payload.get("warnings", []), "size warnings"),
    )


def restore_size_records(report: Mapping[str, object]) -> list[SizeRecord]:
    """Restore only the existing SizeRecord representation from a dry run."""
    records = _sequence(report.get("records"), "records")
    return [_restore_size_record(item) for item in records]


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _money_summary(value: MonetaryValue | SupplierFOBCost | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"amount": value.amount, "currency": value.currency}


def _result_report(result: ProductSizeMatchResult) -> dict[str, object]:
    supplier_conflict = result.supplier_cost_conflict
    return {
        "product_series": result.product.identity.series,
        "product_identity": {
            "model": result.product.identity.model,
            "raw_model": result.product.identity.raw_model,
            "raw_match_identity": result.match.product_raw_identity,
        },
        "matched_body_type": result.match.matched_body_type,
        "match_status": result.match.status,
        "match_method": result.match.method,
        "candidate_keys": list(result.match.candidate_keys),
        "source_rows": {
            "product": {
                "start_row": result.product.source.start_row,
                "end_row": result.product.source.end_row,
            },
            "size": result.size.source.row if result.size is not None else None,
        },
        "warnings": list(result.match.warnings),
        "specification_conflicts": [
            {
                "field": conflict.field,
                "product_raw_value": conflict.product_raw_value,
                "size_raw_value": conflict.size_raw_value,
                "resolution": conflict.resolution,
                "comparison_reason": conflict.comparison_reason,
            }
            for conflict in result.conflicts
        ],
        "supplier_cost_conflict": {
            "present": supplier_conflict is not None,
            "resolution": (
                supplier_conflict.resolution
                if supplier_conflict is not None
                else None
            ),
        },
        "retail_pricing": {
            "minimum_retail_price": _money_summary(
                result.retail_pricing.minimum_retail_price
            )
        },
        "supplier_costs": {
            "price_list_fob": _money_summary(
                result.supplier_costs.price_list_fob
            ),
            "size_list_fob": _money_summary(
                result.supplier_costs.size_list_fob
            ),
        },
    }


def build_product_size_enrichment_report(
    products: Sequence[ProductRecord],
    sizes: Sequence[SizeRecord],
    *,
    product_input_file: str,
    size_input_file: str,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Call the canonical joiner and project an allowlisted safe report."""
    results = enrich_products_with_sizes(products, sizes)
    summary = summarize_enrichment(results)
    report: dict[str, object] = {
        "status": "ok",
        "product_input_file": product_input_file,
        "size_input_file": size_input_file,
        "summary": {
            "total_products": summary.total_products,
            "matched": summary.matched,
            "unmatched": summary.unmatched,
            "ambiguous": summary.ambiguous,
            "exact_matches": summary.exact_matches,
            "suffix_matches": summary.suffix_matches,
            "specification_conflicts": sum(
                len(result.conflicts) for result in results
            ),
            "supplier_cost_conflicts": sum(
                result.supplier_cost_conflict is not None for result in results
            ),
        },
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": [_result_report(result) for result in results],
    }
    base_redactor = redactor or Redactor()
    serialized_report = json.dumps(report, ensure_ascii=False)
    discovered_sensitive_values = tuple(
        matched_value
        for pattern in (REPORT_SECRET_SCAN_PATTERN, _REPORT_URL_PATTERN)
        for match in pattern.finditer(serialized_report)
        if (matched_value := match.group(0)).casefold()
        not in {"authorization", "cookie"}
    )
    report_redactor = Redactor.from_values(
        (*base_redactor.secrets, *discovered_sensitive_values)
    )
    sanitized = sanitize_report_data(report, report_redactor)
    if not isinstance(sanitized, dict):
        raise TypeError("Enrichment report must be an object")
    return sanitized


def run_product_size_enrichment_dry_run(
    product_input_path: Path,
    size_input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read two local reports, enrich them, and persist one safe report."""
    product_path = Path(product_input_path)
    size_path = Path(size_input_path)
    product_report = load_local_json_report(product_path)
    size_report = load_local_json_report(size_path)
    products = restore_product_records(product_report)
    sizes = restore_size_records(size_report)
    active_redactor = redactor or Redactor()
    report = build_product_size_enrichment_report(
        products,
        sizes,
        product_input_file=_safe_input_reference(product_path, project_root),
        size_input_file=_safe_input_reference(size_path, project_root),
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
