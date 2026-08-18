"""Pure-local ProductRecord + AdditionalOptionRecord linking dry run."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import get_args

from .additional_option_parser import (
    AdditionalOptionIdentity,
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
    OptionCategory,
)
from .product_model import MonetaryValue, ProductRecord, from_clm_product
from .option_mapping_registry import (
    REGISTRY_VERSION,
    OptionMappingRegistry,
    OptionMappingResolution,
)
from .product_option_linker import (
    OptionAliasRegistry,
    ProductOptionLinkResult,
    link_products_to_options,
    summarize_option_linking,
)
from .product_size_enrichment_dry_run import (
    ProductSizeEnrichmentInputError,
    _restore_clm_product,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


REPORT_FILENAME = "product-option-linking-dry-run.json"
_DISABLED_REGISTRY_VERSION = "disabled"
_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_REPORT_URL_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>]+")
_OPTION_CATEGORIES = frozenset(get_args(OptionCategory))


class ProductOptionLinkingInputError(ValueError):
    """Safe structural error for local Product + Option reports."""


def select_option_mapping_registry(
    registry_version: str | None,
) -> OptionMappingRegistry:
    """Select only an explicitly approved registry; never guess or fall back."""

    if registry_version is None:
        return OptionMappingRegistry(
            version=_DISABLED_REGISTRY_VERSION,
            aliases=(),
            composites=(),
        )
    if registry_version == REGISTRY_VERSION:
        return OptionMappingRegistry.approved_v1()
    raise ProductOptionLinkingInputError(
        f"Unsupported option mapping registry: {registry_version}"
    )


def load_local_json_report(input_path: Path) -> Mapping[str, object]:
    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise ProductOptionLinkingInputError("Input must be a JSON file")
    if not path.exists():
        raise ProductOptionLinkingInputError("Input JSON file does not exist")
    if not path.is_file():
        raise ProductOptionLinkingInputError("Input JSON path must be a file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProductOptionLinkingInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(payload, Mapping):
        raise ProductOptionLinkingInputError("Input JSON root must be an object")
    return payload


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProductOptionLinkingInputError(f"{label} must be an object")
    return value


def _items(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProductOptionLinkingInputError(f"{label} must be an array")
    return value


def _text(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not nullable and not value.strip()):
        suffix = " or null" if nullable else ""
        raise ProductOptionLinkingInputError(f"{label} must be text{suffix}")
    return value


def _boolean(value: object, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProductOptionLinkingInputError(f"{label} must be boolean")
    return value


def _decimal(value: object, *, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(
        value, (str, int, float, Decimal)
    ):
        raise ProductOptionLinkingInputError(f"{label} must be numeric or null")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProductOptionLinkingInputError(
            f"{label} must be numeric or null"
        ) from error
    if not amount.is_finite():
        raise ProductOptionLinkingInputError(f"{label} must be finite")
    return amount


def _warnings(value: object, *, label: str) -> tuple[str, ...]:
    items = [] if value is None else _items(value, label=label)
    if any(not isinstance(item, str) for item in items):
        raise ProductOptionLinkingInputError(f"{label} must contain only text")
    return tuple(items)


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def restore_product_records(
    report: Mapping[str, object],
) -> list[ProductRecord]:
    """Restore CLM blocks, then use the canonical ProductRecord converter."""

    products = _items(report.get("products"), label="products")
    try:
        return [from_clm_product(_restore_clm_product(item)) for item in products]
    except ProductSizeEnrichmentInputError as error:
        raise ProductOptionLinkingInputError(str(error)) from error


def _restore_source(option: Mapping[str, object]) -> AdditionalOptionSource:
    raw_coordinate = option.get("source_coordinate")
    if raw_coordinate is None:
        source = option.get("source")
        if source is not None:
            raw_coordinate = _mapping(source, label="option source").get(
                "raw_coordinate"
            )
    coordinate = _text(
        raw_coordinate,
        label="option source coordinate",
    )
    if coordinate is None:  # pragma: no cover - type guard
        raise AssertionError("Option coordinate unexpectedly missing")
    coordinate = coordinate.upper().strip()
    matched = _COORDINATE_PATTERN.fullmatch(coordinate)
    if matched is None:
        raise ProductOptionLinkingInputError(
            "option source coordinate must be an A1 coordinate"
        )
    return AdditionalOptionSource(
        row=int(matched.group(2)),
        column=matched.group(1),
        raw_coordinate=coordinate,
    )


def _restore_option(value: object) -> AdditionalOptionRecord:
    option = _mapping(value, label="option")
    option_name = _text(option.get("option_name"), label="option name")
    category = _text(option.get("category"), label="option category")
    if option_name is None or category is None:  # pragma: no cover
        raise AssertionError("Required option text unexpectedly missing")
    if category not in _OPTION_CATEGORIES:
        raise ProductOptionLinkingInputError("option category is unsupported")
    raw_price = option.get("price")
    price = {} if raw_price is None else _mapping(raw_price, label="option price")
    raw_name = _text(
        option.get("raw_name"),
        label="option raw name",
        nullable=True,
    )
    return AdditionalOptionRecord(
        identity=AdditionalOptionIdentity(
            option_name=option_name,
            raw_name=raw_name or option_name,
        ),
        pricing=AdditionalOptionPricing(
            amount=_decimal(price.get("amount"), label="option price amount"),
            currency=_text(
                price.get("currency"),
                label="option price currency",
                nullable=True,
            ),
            raw_price=_text(
                price.get("raw_price"),
                label="option raw price",
                nullable=True,
            ),
            price_range=_text(
                price.get("price_range"),
                label="option price range",
                nullable=True,
            ),
            price_anchor=_text(
                price.get("price_anchor"),
                label="option price anchor",
                nullable=True,
            ),
            shared_price_source=_boolean(
                price.get("shared_price_source"),
                label="option shared price source",
            ),
        ),
        category=category,
        source=_restore_source(option),
        warnings=_warnings(option.get("warnings"), label="option warnings"),
    )


def restore_option_records(
    report: Mapping[str, object],
) -> list[AdditionalOptionRecord]:
    options = _items(report.get("options"), label="options")
    return [_restore_option(item) for item in options]


def _money(value: MonetaryValue | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "amount": value.amount,
        "currency": value.currency,
        "raw_value": value.raw_value,
    }


def _supplier_pricing(value: AdditionalOptionPricing) -> dict[str, object]:
    return {
        "amount": value.amount,
        "currency": value.currency,
        "raw_price": value.raw_price,
        "price_range": value.price_range,
        "price_anchor": value.price_anchor,
        "shared_price_source": value.shared_price_source,
    }


def _composite_mapping_report(
    resolution: OptionMappingResolution,
) -> dict[str, object]:
    combined = resolution.combined_supplier_cost
    return {
        "product_upgrade_name": resolution.product_upgrade_name,
        "product_raw_value": resolution.product_raw_value,
        "mapping_type": "composite",
        "registry_version": resolution.registry_version,
        "components": [
            {
                "option_name": component.option_name,
                "category": component.category,
                "supplier_cost": _supplier_pricing(component.supplier_cost),
                "source_coordinate": component.source.raw_coordinate,
            }
            for component in resolution.components
        ],
        "combined_supplier_cost": (
            {
                "amount": combined.amount,
                "currency": combined.currency,
            }
            if combined is not None
            else None
        ),
        "warnings": list(resolution.warnings),
    }


def _mapping_issue_report(
    resolution: OptionMappingResolution,
) -> dict[str, object]:
    return {
        "product_upgrade_name": resolution.product_upgrade_name,
        "product_raw_value": resolution.product_raw_value,
        "mapping_type": resolution.mapping_type,
        "status": resolution.status,
        "registry_version": resolution.registry_version,
        "components": [
            {
                "option_name": component.option_name,
                "category": component.category,
                "supplier_cost": _supplier_pricing(component.supplier_cost),
                "source_coordinate": component.source.raw_coordinate,
            }
            for component in resolution.components
        ],
        "missing_component_names": list(resolution.missing_component_names),
        "warnings": list(resolution.warnings),
    }


def _result_report(
    product: ProductRecord,
    result: ProductOptionLinkResult,
    *,
    mapping_registry_enabled: bool,
) -> dict[str, object]:
    linked_options = [
        {
            "product_upgrade_name": linked.product_option.name,
            "product_raw_value": linked.product_raw_option,
            "mapping_type": (
                "alias" if linked.match_method == "approved_alias" else "exact"
            ),
            "registry_version": linked.registry_version,
            "catalog_option_name": linked.matched_catalog_option.option_name,
            "catalog_category": linked.category,
            "supplier_cost": _supplier_pricing(linked.pricing),
            "catalog_source_coordinate": (
                linked.pricing_source.raw_coordinate
            ),
            "warnings": list(linked.warnings),
            # Backward-compatible report keys for the exact-only baseline.
            "product_raw_option": linked.product_raw_option,
            "matched_catalog_option": linked.matched_catalog_option.option_name,
            "category": linked.category,
            "supplier_pricing": _supplier_pricing(linked.pricing),
            "match_method": linked.match_method,
        }
        for linked in result.linked_upgrade_options
    ]
    if mapping_registry_enabled:
        linked_options.extend(
            _composite_mapping_report(resolution)
            for resolution in result.mapping_resolutions
            if resolution.status == "composite"
        )
    return {
        "series": result.series,
        "product_identity": {
            "model": result.product_identity.model,
            "raw_model": result.product_identity.raw_model,
            "raw_series_title": result.product_identity.raw_series_title,
        },
        "source_rows": {
            "start_row": result.source.start_row,
            "end_row": result.source.end_row,
        },
        "included_features": list(result.included_features),
        "raw_upgrade_options": [
            upgrade.raw_value for upgrade in product.options.upgrade_options
        ],
        "linked_upgrade_options": linked_options,
        "unmatched_upgrade_options": [
            {
                "product_raw_option": unmatched.product_raw_option,
                "warnings": list(unmatched.warnings),
            }
            for unmatched in result.unmatched_upgrade_options
        ],
        "ambiguous_upgrade_options": [
            {
                "product_raw_option": ambiguous.product_raw_option,
                "match_method": ambiguous.match_method,
                "catalog_candidates": [
                    {
                        "option_name": candidate.identity.option_name,
                        "category": candidate.category,
                        "source_coordinate": candidate.source.raw_coordinate,
                    }
                    for candidate in ambiguous.catalog_candidates
                ],
                "warnings": list(ambiguous.warnings),
            }
            for ambiguous in result.ambiguous_upgrade_options
        ],
        "included_upgrade_conflicts": [
            {
                "product_raw_option": conflict.product_raw_option,
                "included_features": list(conflict.included_features),
                "warning": conflict.warning,
            }
            for conflict in result.included_upgrade_conflicts
        ],
        "mapping_issues": (
            [
                _mapping_issue_report(resolution)
                for resolution in result.mapping_resolutions
                if resolution.status
                in {
                    "incomplete_composite",
                    "currency_conflict",
                    "missing_component_price",
                }
            ]
            if mapping_registry_enabled
            else []
        ),
        "retail_pricing": {
            "minimum_retail_price": _money(
                result.retail_pricing.minimum_retail_price
            )
        },
        "warnings": list(result.warnings),
    }


def build_product_option_linking_report(
    products: Sequence[ProductRecord],
    options: Sequence[AdditionalOptionRecord],
    *,
    product_input_file: str,
    option_input_file: str,
    mapping_registry_version: str | None = None,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Call the canonical linker with an explicit versioned registry choice."""

    alias_registry = OptionAliasRegistry()
    mapping_registry = select_option_mapping_registry(mapping_registry_version)
    mapping_registry_enabled = mapping_registry_version is not None
    results = link_products_to_options(
        products,
        options,
        alias_registry=alias_registry,
        mapping_registry=mapping_registry,
    )
    linked_summary = summarize_option_linking(results)
    total_upgrade_options = sum(
        len(product.options.upgrade_options) for product in products
    )
    products_with_upgrades = sum(
        bool(product.options.upgrade_options) for product in products
    )
    resolutions = [
        resolution
        for result in results
        for resolution in result.mapping_resolutions
    ]
    report: dict[str, object] = {
        "status": "ok",
        "product_input_file": product_input_file,
        "option_input_file": option_input_file,
        "mapping_registry": {
            "enabled": mapping_registry_enabled,
            "version": mapping_registry_version,
        },
        "summary": {
            "total_products": len(products),
            "products_with_upgrade_options": products_with_upgrades,
            "products_without_upgrade_options": (
                len(products) - products_with_upgrades
            ),
            "total_upgrade_options": total_upgrade_options,
            "linked_options": linked_summary.linked_options,
            "exact_matches": sum(
                linked.match_method == "exact"
                for result in results
                for linked in result.linked_upgrade_options
            ),
            "alias_matches": sum(
                linked.match_method == "approved_alias"
                for result in results
                for linked in result.linked_upgrade_options
            ),
            "composite_matches": sum(
                resolution.status == "composite"
                for resolution in resolutions
            ),
            "unmatched_options": linked_summary.unmatched_options,
            "ambiguous_options": linked_summary.ambiguous_options,
            "incomplete_composites": sum(
                resolution.status == "incomplete_composite"
                for resolution in resolutions
            ),
            "currency_conflicts": sum(
                resolution.status == "currency_conflict"
                for resolution in resolutions
            ),
            "missing_component_prices": sum(
                resolution.status == "missing_component_price"
                for resolution in resolutions
            ),
            "included_features_count": (
                linked_summary.included_features_count
            ),
            "included_upgrade_conflicts": linked_summary.conflicts,
        },
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": [
            _result_report(
                product,
                result,
                mapping_registry_enabled=mapping_registry_enabled,
            )
            for product, result in zip(products, results, strict=True)
        ],
    }

    base_redactor = redactor or Redactor()
    serialized_report = json.dumps(report, ensure_ascii=False, default=str)
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
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("Sanitized linking report must remain an object")
    return sanitized


def run_product_option_linking_dry_run(
    product_input_path: Path,
    option_input_path: Path,
    *,
    project_root: Path,
    mapping_registry_version: str | None = None,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    select_option_mapping_registry(mapping_registry_version)
    product_path = Path(product_input_path)
    option_path = Path(option_input_path)
    product_report = load_local_json_report(product_path)
    option_report = load_local_json_report(option_path)
    products = restore_product_records(product_report)
    options = restore_option_records(option_report)
    active_redactor = redactor or Redactor()
    report = build_product_option_linking_report(
        products,
        options,
        product_input_file=_safe_input_reference(product_path, project_root),
        option_input_file=_safe_input_reference(option_path, project_root),
        mapping_registry_version=mapping_registry_version,
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
