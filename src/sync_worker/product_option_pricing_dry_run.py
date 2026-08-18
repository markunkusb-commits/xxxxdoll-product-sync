"""Pure-local pricing dry run for Product Option Linking reports."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import get_args

from . import product_option_pricing
from .additional_option_parser import (
    AdditionalOptionIdentity,
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
    OptionCategory,
)
from .option_mapping_registry import (
    CombinedSupplierCost,
    MappingStatus,
    MappingType,
    OptionMappingComponent,
    OptionMappingResolution,
)
from .option_pricing_dry_run import (
    OptionPricingDryRunInputError,
    parse_rmb_to_usd_rate,
)
from .option_pricing_policy import MARKUP_RATE, MINIMUM_PROFIT_USD, POLICY_VERSION
from .product_model import (
    MonetaryValue,
    ProductIdentity,
    ProductSource,
    RetailPricing,
    UpgradeOptionRecord,
)
from .product_option_linker import (
    AmbiguousUpgradeOption,
    IncludedUpgradeConflict,
    LinkedUpgradeOption,
    OptionMatchMethod,
    ProductOptionLinkResult,
    UnmatchedUpgradeOption,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


REPORT_FILENAME = "product-option-pricing-dry-run.json"
RATE_SOURCE = "cli_injected"
_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s\"'<>]+")
_OPTION_CATEGORIES = frozenset(get_args(OptionCategory))
_MAPPING_STATUSES = frozenset(get_args(MappingStatus))
_MAPPING_TYPES = frozenset(get_args(MappingType))
_MATCH_METHODS = frozenset(get_args(OptionMatchMethod))


class ProductOptionPricingDryRunInputError(ValueError):
    """Safe structural error for a local mapped-option pricing report."""


def load_local_linking_report(input_path: Path) -> Mapping[str, object]:
    """Load only one explicitly supplied local JSON report."""

    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise ProductOptionPricingDryRunInputError("Input must be a JSON file")
    if not path.exists():
        raise ProductOptionPricingDryRunInputError("Input JSON file does not exist")
    if not path.is_file():
        raise ProductOptionPricingDryRunInputError(
            "Input JSON path must be a file"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProductOptionPricingDryRunInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(payload, Mapping):
        raise ProductOptionPricingDryRunInputError(
            "Input JSON root must be an object"
        )
    return payload


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProductOptionPricingDryRunInputError(f"{label} must be an object")
    return value


def _items(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProductOptionPricingDryRunInputError(f"{label} must be an array")
    return value


def _text(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not nullable and not value.strip()):
        suffix = " or null" if nullable else ""
        raise ProductOptionPricingDryRunInputError(f"{label} must be text{suffix}")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProductOptionPricingDryRunInputError(
            f"{label} must be a positive integer"
        )
    return value


def _decimal(
    value: object,
    *,
    label: str,
    nullable: bool = True,
) -> Decimal | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(
        value, (str, int, float, Decimal)
    ):
        suffix = " or null" if nullable else ""
        raise ProductOptionPricingDryRunInputError(
            f"{label} must be numeric{suffix}"
        )
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProductOptionPricingDryRunInputError(
            f"{label} must be numeric"
        ) from error
    if not amount.is_finite():
        raise ProductOptionPricingDryRunInputError(f"{label} must be finite")
    return amount


def _boolean(value: object, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProductOptionPricingDryRunInputError(f"{label} must be boolean")
    return value


def _warnings(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = _items(value, label=label)
    if any(not isinstance(item, str) for item in values):
        raise ProductOptionPricingDryRunInputError(
            f"{label} must contain only text"
        )
    return tuple(values)


def _text_items(value: object, *, label: str) -> tuple[str, ...]:
    values = _items(value, label=label)
    if any(not isinstance(item, str) for item in values):
        raise ProductOptionPricingDryRunInputError(
            f"{label} must contain only text"
        )
    return tuple(values)


def _category(value: object, *, label: str) -> OptionCategory:
    category = _text(value, label=label)
    if category not in _OPTION_CATEGORIES:
        raise ProductOptionPricingDryRunInputError(f"{label} is unsupported")
    return category  # type: ignore[return-value]


def _coordinate_source(value: object, *, label: str) -> AdditionalOptionSource:
    coordinate = _text(value, label=label)
    if coordinate is None:  # pragma: no cover - required text guard
        raise AssertionError("Coordinate unexpectedly missing")
    coordinate = coordinate.strip().upper()
    matched = _COORDINATE_PATTERN.fullmatch(coordinate)
    if matched is None:
        raise ProductOptionPricingDryRunInputError(
            f"{label} must be an A1 coordinate"
        )
    return AdditionalOptionSource(
        row=int(matched.group(2)),
        column=matched.group(1),
        raw_coordinate=coordinate,
    )


def _restore_supplier_cost(
    value: object,
    *,
    label: str,
) -> AdditionalOptionPricing:
    cost = _mapping(value, label=label)
    return AdditionalOptionPricing(
        amount=_decimal(cost.get("amount"), label=f"{label} amount"),
        currency=_text(
            cost.get("currency"),
            label=f"{label} currency",
            nullable=True,
        ),
        raw_price=_text(
            cost.get("raw_price"),
            label=f"{label} raw price",
            nullable=True,
        ),
        price_range=_text(
            cost.get("price_range"),
            label=f"{label} price range",
            nullable=True,
        ),
        price_anchor=_text(
            cost.get("price_anchor"),
            label=f"{label} price anchor",
            nullable=True,
        ),
        shared_price_source=_boolean(
            cost.get("shared_price_source"),
            label=f"{label} shared source",
        ),
    )


def _upgrade(name: str, raw_value: str) -> UpgradeOptionRecord:
    return UpgradeOptionRecord(name=name, raw_value=raw_value, supplier_cost=None)


def _restore_linked_option(value: object, *, index: int) -> LinkedUpgradeOption:
    linked = _mapping(value, label=f"linked_upgrade_options[{index}]")
    mapping_type = _text(
        linked.get("mapping_type"),
        label=f"linked_upgrade_options[{index}] mapping type",
    )
    if mapping_type not in {"exact", "alias"}:
        raise ProductOptionPricingDryRunInputError(
            "Simple linked option mapping type must be exact or alias"
        )
    name = _text(
        linked.get("product_upgrade_name"),
        label=f"linked_upgrade_options[{index}] product upgrade name",
    )
    raw_value = _text(
        linked.get("product_raw_value", linked.get("product_raw_option")),
        label=f"linked_upgrade_options[{index}] product raw value",
    )
    catalog_name = _text(
        linked.get("catalog_option_name", linked.get("matched_catalog_option")),
        label=f"linked_upgrade_options[{index}] catalog option name",
    )
    category = _category(
        linked.get("catalog_category", linked.get("category")),
        label=f"linked_upgrade_options[{index}] category",
    )
    raw_cost = linked.get("supplier_cost", linked.get("supplier_pricing"))
    cost = _restore_supplier_cost(
        raw_cost,
        label=f"linked_upgrade_options[{index}] supplier cost",
    )
    source = _coordinate_source(
        linked.get("catalog_source_coordinate"),
        label=f"linked_upgrade_options[{index}] source coordinate",
    )
    registry_version = _text(
        linked.get("registry_version"),
        label=f"linked_upgrade_options[{index}] registry version",
        nullable=True,
    )
    if name is None or raw_value is None or catalog_name is None:  # pragma: no cover
        raise AssertionError("Required linked-option text unexpectedly missing")
    return LinkedUpgradeOption(
        product_raw_option=raw_value,
        product_option=_upgrade(name, raw_value),
        matched_catalog_option=AdditionalOptionIdentity(
            option_name=catalog_name,
            raw_name=catalog_name,
        ),
        category=category,
        pricing=cost,
        pricing_source=source,
        match_method="approved_alias" if mapping_type == "alias" else "exact",
        warnings=_warnings(
            linked.get("warnings"),
            label=f"linked_upgrade_options[{index}] warnings",
        ),
        registry_version=registry_version,
    )


def _restore_component(value: object, *, label: str) -> OptionMappingComponent:
    component = _mapping(value, label=label)
    name = _text(component.get("option_name"), label=f"{label} option name")
    raw_name = _text(
        component.get("raw_option_name"),
        label=f"{label} raw option name",
        nullable=True,
    )
    source = _coordinate_source(
        component.get("source_coordinate"),
        label=f"{label} source coordinate",
    )
    if name is None:  # pragma: no cover - required text guard
        raise AssertionError("Component name unexpectedly missing")
    return OptionMappingComponent(
        option_name=name,
        raw_option_name=raw_name or name,
        category=_category(component.get("category"), label=f"{label} category"),
        supplier_cost=_restore_supplier_cost(
            component.get("supplier_cost"),
            label=f"{label} supplier cost",
        ),
        source=source,
        warnings=_warnings(component.get("warnings"), label=f"{label} warnings"),
    )


def _restore_resolution(
    value: object,
    *,
    label: str,
    default_status: str | None = None,
) -> OptionMappingResolution:
    entry = _mapping(value, label=label)
    raw_status = entry.get("status", default_status)
    status = _text(raw_status, label=f"{label} status")
    if status not in _MAPPING_STATUSES:
        raise ProductOptionPricingDryRunInputError(f"{label} status is unsupported")
    raw_mapping_type = entry.get("mapping_type")
    mapping_type = _text(
        raw_mapping_type,
        label=f"{label} mapping type",
        nullable=True,
    )
    normalized_mapping_type = (
        "exact_catalog" if mapping_type == "exact" else mapping_type
    )
    if (
        normalized_mapping_type is not None
        and normalized_mapping_type not in _MAPPING_TYPES
    ):
        raise ProductOptionPricingDryRunInputError(
            f"{label} mapping type is unsupported"
        )
    name = _text(
        entry.get("product_upgrade_name"),
        label=f"{label} product upgrade name",
    )
    raw_value = _text(
        entry.get("product_raw_value"),
        label=f"{label} product raw value",
    )
    registry_version = _text(
        entry.get("registry_version"),
        label=f"{label} registry version",
        nullable=True,
    )
    components = tuple(
        _restore_component(component, label=f"{label} components[{index}]")
        for index, component in enumerate(
            _items(entry.get("components", []), label=f"{label} components")
        )
    )
    combined_value = entry.get("combined_supplier_cost")
    combined = None
    if combined_value is not None:
        combined_data = _mapping(
            combined_value,
            label=f"{label} combined supplier cost",
        )
        amount = _decimal(
            combined_data.get("amount"),
            label=f"{label} combined supplier cost amount",
            nullable=False,
        )
        currency = _text(
            combined_data.get("currency"),
            label=f"{label} combined supplier cost currency",
        )
        if amount is None or currency is None:  # pragma: no cover - guards
            raise AssertionError("Combined supplier cost unexpectedly incomplete")
        combined = CombinedSupplierCost(amount=amount, currency=currency)
    missing = entry.get("missing_component_names", [])
    if name is None or raw_value is None:  # pragma: no cover - required guards
        raise AssertionError("Resolution identity unexpectedly incomplete")
    return OptionMappingResolution(
        registry_version=registry_version or "unversioned",
        status=status,  # type: ignore[arg-type]
        product_upgrade_name=name,
        product_raw_value=raw_value,
        mapping_type=normalized_mapping_type,  # type: ignore[arg-type]
        catalog_option_name=_text(
            entry.get("catalog_option_name"),
            label=f"{label} catalog option name",
            nullable=True,
        ),
        category=(
            _category(entry.get("category"), label=f"{label} category")
            if entry.get("category") is not None
            else None
        ),
        components=components,
        combined_supplier_cost=combined,
        catalog_candidates=tuple(
            AdditionalOptionRecord(
                identity=AdditionalOptionIdentity(
                    option_name=component.option_name,
                    raw_name=component.raw_option_name,
                ),
                pricing=component.supplier_cost,
                category=component.category,
                source=component.source,
                warnings=component.warnings,
            )
            for component in components
        ),
        missing_component_names=_text_items(
            missing,
            label=f"{label} missing component names",
        ),
        warnings=_warnings(entry.get("warnings"), label=f"{label} warnings"),
    )


def _restore_ambiguous(value: object, *, index: int) -> AmbiguousUpgradeOption:
    label = f"ambiguous_upgrade_options[{index}]"
    entry = _mapping(value, label=label)
    raw_value = _text(entry.get("product_raw_option"), label=f"{label} raw value")
    name = _text(
        entry.get("product_upgrade_name"),
        label=f"{label} product upgrade name",
        nullable=True,
    )
    match_method = _text(entry.get("match_method"), label=f"{label} match method")
    if match_method not in _MATCH_METHODS:
        raise ProductOptionPricingDryRunInputError(
            f"{label} match method is unsupported"
        )
    candidates: list[AdditionalOptionRecord] = []
    for candidate_index, raw_candidate in enumerate(
        _items(entry.get("catalog_candidates"), label=f"{label} candidates")
    ):
        candidate_label = f"{label} candidates[{candidate_index}]"
        candidate = _mapping(raw_candidate, label=candidate_label)
        option_name = _text(
            candidate.get("option_name"), label=f"{candidate_label} option name"
        )
        if option_name is None:  # pragma: no cover
            raise AssertionError("Candidate name unexpectedly missing")
        candidates.append(
            AdditionalOptionRecord(
                identity=AdditionalOptionIdentity(option_name, option_name),
                pricing=AdditionalOptionPricing(None, None, None),
                category=_category(
                    candidate.get("category"), label=f"{candidate_label} category"
                ),
                source=_coordinate_source(
                    candidate.get("source_coordinate"),
                    label=f"{candidate_label} source coordinate",
                ),
                warnings=(),
            )
        )
    if raw_value is None:  # pragma: no cover
        raise AssertionError("Ambiguous raw value unexpectedly missing")
    return AmbiguousUpgradeOption(
        product_raw_option=raw_value,
        product_option=_upgrade(name or raw_value, raw_value),
        catalog_candidates=tuple(candidates),
        match_method=match_method,  # type: ignore[arg-type]
        warnings=_warnings(entry.get("warnings"), label=f"{label} warnings"),
    )


def _restore_unmatched(value: object, *, index: int) -> UnmatchedUpgradeOption:
    label = f"unmatched_upgrade_options[{index}]"
    entry = _mapping(value, label=label)
    raw_value = _text(entry.get("product_raw_option"), label=f"{label} raw value")
    name = _text(
        entry.get("product_upgrade_name"),
        label=f"{label} product upgrade name",
        nullable=True,
    )
    if raw_value is None:  # pragma: no cover
        raise AssertionError("Unmatched raw value unexpectedly missing")
    return UnmatchedUpgradeOption(
        product_raw_option=raw_value,
        product_option=_upgrade(name or raw_value, raw_value),
        warnings=_warnings(entry.get("warnings"), label=f"{label} warnings"),
    )


def _restore_conflict(value: object, *, index: int) -> IncludedUpgradeConflict:
    label = f"included_upgrade_conflicts[{index}]"
    entry = _mapping(value, label=label)
    raw_value = _text(entry.get("product_raw_option"), label=f"{label} raw value")
    name = _text(
        entry.get("product_upgrade_name"),
        label=f"{label} product upgrade name",
        nullable=True,
    )
    warning = _text(entry.get("warning"), label=f"{label} warning")
    if raw_value is None or warning is None:  # pragma: no cover
        raise AssertionError("Conflict text unexpectedly missing")
    return IncludedUpgradeConflict(
        product_raw_option=raw_value,
        product_option=_upgrade(name or raw_value, raw_value),
        included_features=_text_items(
            entry.get("included_features", []),
            label=f"{label} included features",
        ),
        warning=warning,
    )


def _restore_money(value: object) -> MonetaryValue | None:
    if value is None:
        return None
    money = _mapping(value, label="minimum retail price")
    raw_value = _text(
        money.get("raw_value"),
        label="minimum retail price raw value",
        nullable=True,
    )
    return MonetaryValue(
        raw_value=raw_value or "",
        currency=_text(
            money.get("currency"),
            label="minimum retail price currency",
            nullable=True,
        ),
        amount=_decimal(money.get("amount"), label="minimum retail price amount"),
        context="minimum_retail_price",
    )


def _restore_product_result(value: object, *, index: int) -> ProductOptionLinkResult:
    label = f"results[{index}]"
    result = _mapping(value, label=label)
    series = _text(result.get("series"), label=f"{label} series")
    identity = _mapping(result.get("product_identity"), label=f"{label} identity")
    source = _mapping(result.get("source_rows"), label=f"{label} source rows")
    retail = _mapping(
        result.get("retail_pricing", {}),
        label=f"{label} retail pricing",
    )
    if series is None:  # pragma: no cover
        raise AssertionError("Series unexpectedly missing")
    linked_values = _items(
        result.get("linked_upgrade_options"),
        label=f"{label} linked options",
    )
    linked: list[LinkedUpgradeOption] = []
    resolutions: list[OptionMappingResolution] = []
    for linked_index, raw_linked in enumerate(linked_values):
        linked_mapping = _mapping(
            raw_linked,
            label=f"{label} linked options[{linked_index}]",
        )
        if linked_mapping.get("mapping_type") == "composite":
            resolutions.append(
                _restore_resolution(
                    linked_mapping,
                    label=f"{label} linked options[{linked_index}]",
                    default_status="composite",
                )
            )
        else:
            linked.append(_restore_linked_option(linked_mapping, index=linked_index))
    resolutions.extend(
        _restore_resolution(
            issue,
            label=f"{label} mapping issues[{issue_index}]",
        )
        for issue_index, issue in enumerate(
            _items(result.get("mapping_issues", []), label=f"{label} mapping issues")
        )
    )
    return ProductOptionLinkResult(
        product_identity=ProductIdentity(
            series=series,
            model=_text(
                identity.get("model"), label=f"{label} model", nullable=True
            ),
            raw_series_title=(
                _text(
                    identity.get("raw_series_title"),
                    label=f"{label} raw series title",
                )
                or series
            ),
            raw_model=_text(
                identity.get("raw_model"),
                label=f"{label} raw model",
                nullable=True,
            ),
        ),
        series=series,
        included_features=_text_items(
            result.get("included_features", []),
            label=f"{label} included features",
        ),
        linked_upgrade_options=tuple(linked),
        unmatched_upgrade_options=tuple(
            _restore_unmatched(item, index=item_index)
            for item_index, item in enumerate(
                _items(
                    result.get("unmatched_upgrade_options", []),
                    label=f"{label} unmatched options",
                )
            )
        ),
        ambiguous_upgrade_options=tuple(
            _restore_ambiguous(item, index=item_index)
            for item_index, item in enumerate(
                _items(
                    result.get("ambiguous_upgrade_options", []),
                    label=f"{label} ambiguous options",
                )
            )
        ),
        included_upgrade_conflicts=tuple(
            _restore_conflict(item, index=item_index)
            for item_index, item in enumerate(
                _items(
                    result.get("included_upgrade_conflicts", []),
                    label=f"{label} included conflicts",
                )
            )
        ),
        warnings=_warnings(result.get("warnings"), label=f"{label} warnings"),
        source=ProductSource(
            start_row=_integer(source.get("start_row"), label=f"{label} start row"),
            end_row=_integer(source.get("end_row"), label=f"{label} end row"),
        ),
        retail_pricing=RetailPricing(
            minimum_retail_price=_restore_money(retail.get("minimum_retail_price"))
        ),
        mapping_resolutions=tuple(resolutions),
    )


def restore_product_option_link_results(
    report: Mapping[str, object],
) -> list[ProductOptionLinkResult]:
    """Restore the canonical linker models without re-running matching."""

    if not isinstance(report, Mapping):
        raise ProductOptionPricingDryRunInputError(
            "Input linking report must be an object"
        )
    return [
        _restore_product_result(value, index=index)
        for index, value in enumerate(
            _items(report.get("results"), label="results")
        )
    ]


def _money_report(value: MonetaryValue | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "amount": value.amount,
        "currency": value.currency,
        "raw_value": value.raw_value,
    }


def _supplier_cost_report(
    value: product_option_pricing.SupplierCostProvenance | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "amount": value.amount,
        "currency": value.currency,
        "raw_values": list(value.raw_values),
        "source_provenance": {
            "coordinates": list(value.source_coordinates),
        },
    }


def _component_report(
    value: product_option_pricing.PricedMappingComponent,
) -> dict[str, object]:
    return {
        "option_name": value.option_name,
        "category": value.category,
        "supplier_cost": {
            "amount": value.supplier_cost.amount,
            "currency": value.supplier_cost.currency,
            "raw_price": value.supplier_cost.raw_price,
        },
        "source_coordinate": value.source_coordinate,
    }


def _mapping_report(
    value: product_option_pricing.PricingMappingSnapshot,
    *,
    supplier_cost: product_option_pricing.SupplierCostProvenance | None,
) -> dict[str, object]:
    return {
        "mapping_type": value.mapping_type,
        "status": value.mapping_status,
        "registry_version": value.registry_version,
        "catalog_option_name": value.catalog_option_name,
        "catalog_category": value.catalog_category,
        "components": [_component_report(component) for component in value.components],
        "combined_supplier_cost": (
            _supplier_cost_report(supplier_cost)
            if value.mapping_type == "composite"
            else None
        ),
        "candidate_option_names": list(value.candidate_option_names),
        "missing_component_names": list(value.missing_component_names),
        "source_coordinates": list(value.source_coordinates),
    }


def _pricing_report(
    value: product_option_pricing.PricedLinkedOption,
) -> dict[str, object]:
    pricing = value.pricing
    calculation = pricing.calculation
    retail = pricing.retail
    return {
        "status": pricing.status,
        "fx_rate": pricing.fx.rate if pricing.fx is not None else None,
        "cost_usd": calculation.cost_usd if calculation is not None else None,
        "markup_price_usd": (
            calculation.markup_price_usd if calculation is not None else None
        ),
        "minimum_profit_price_usd": (
            calculation.minimum_profit_price_usd
            if calculation is not None
            else None
        ),
        "target_retail_usd": (
            retail.target_retail_usd if retail is not None else None
        ),
        "policy_version": pricing.metadata.policy_version,
    }


def _priced_option_report(
    value: product_option_pricing.PricedLinkedOption,
) -> dict[str, object]:
    return {
        "product_upgrade_name": value.product_upgrade_name,
        "product_raw_value": value.product_raw_value,
        "mapping_type": value.mapping.mapping_type,
        "registry_version": value.mapping.registry_version,
        "catalog_mapping": _mapping_report(
            value.mapping,
            supplier_cost=value.supplier_cost,
        ),
        "supplier_cost": _supplier_cost_report(value.supplier_cost),
        "pricing": _pricing_report(value),
        "warnings": list(
            dict.fromkeys((*value.warnings, *value.pricing.metadata.warnings))
        ),
    }


def _unpriced_option_report(
    value: product_option_pricing.UnpricedLinkedOption,
) -> dict[str, object]:
    policy_warnings = value.pricing.metadata.warnings if value.pricing else ()
    return {
        "product_upgrade_name": value.product_upgrade_name,
        "product_raw_value": value.product_raw_value,
        "mapping_type": value.mapping.mapping_type,
        "registry_version": value.mapping.registry_version,
        "catalog_mapping": _mapping_report(
            value.mapping,
            supplier_cost=value.supplier_cost,
        ),
        "supplier_cost": _supplier_cost_report(value.supplier_cost),
        "pricing": {
            "status": value.status,
            "fx_rate": None,
            "cost_usd": None,
            "markup_price_usd": None,
            "minimum_profit_price_usd": None,
            "target_retail_usd": None,
            "policy_version": (
                value.pricing.metadata.policy_version
                if value.pricing is not None
                else POLICY_VERSION
            ),
            "unavailable_reason": value.unavailable_reason,
        },
        "warnings": list(dict.fromkeys((*value.warnings, *policy_warnings))),
    }


def _product_report(
    value: product_option_pricing.PricedProductOptionResult,
) -> dict[str, object]:
    return {
        "series": value.series,
        "product_identity": {
            "model": value.product_identity.model,
            "raw_model": value.product_identity.raw_model,
            "raw_series_title": value.product_identity.raw_series_title,
        },
        "source_trace": {
            "start_row": value.source.start_row,
            "end_row": value.source.end_row,
        },
        "included_features": list(value.included_features),
        "retail_pricing": {
            "minimum_retail_price": _money_report(
                value.retail_pricing.minimum_retail_price
            )
        },
        "priced_upgrade_options": [
            _priced_option_report(option) for option in value.priced_upgrade_options
        ],
        "unpriced_upgrade_options": [
            _unpriced_option_report(option)
            for option in value.unpriced_upgrade_options
        ],
        "warnings": list(value.warnings),
    }


def _validated_rate(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ProductOptionPricingDryRunInputError("FX rate must be Decimal")
    try:
        return parse_rmb_to_usd_rate(format(value, "f"))
    except OptionPricingDryRunInputError as error:
        raise ProductOptionPricingDryRunInputError(str(error)) from error


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _report_redactor(report: Mapping[str, object], base: Redactor) -> Redactor:
    serialized = json.dumps(report, ensure_ascii=False, default=str)
    discovered = tuple(
        matched
        for pattern in (REPORT_SECRET_SCAN_PATTERN, _URL_PATTERN)
        for match in pattern.finditer(serialized)
        if (matched := match.group(0)).casefold()
        not in {"authorization", "cookie"}
    )
    return Redactor.from_values((*base.secrets, *discovered))


def build_product_option_pricing_report(
    link_results: Sequence[ProductOptionLinkResult],
    *,
    input_file: str,
    rmb_to_usd_rate: Decimal,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Call the existing pricing layer and serialize only its safe output."""

    validated_rate = _validated_rate(rmb_to_usd_rate)
    priced_results = product_option_pricing.enrich_product_option_pricing(
        link_results,
        rmb_to_usd_rate=validated_rate,
    )
    summary = product_option_pricing.summarize_product_option_pricing(
        priced_results
    )
    report: dict[str, object] = {
        "status": "ok",
        "input_file": input_file,
        "fx": {
            "rmb_to_usd": format(validated_rate, "f"),
            "rate_source": RATE_SOURCE,
        },
        "policy": {
            "version": POLICY_VERSION,
            "markup_rate": format(MARKUP_RATE, "f"),
            "minimum_profit_usd": format(MINIMUM_PROFIT_USD, "f"),
        },
        "summary": summary.to_dict(),
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": [_product_report(result) for result in priced_results],
    }
    active_redactor = _report_redactor(report, redactor or Redactor())
    sanitized = sanitize_report_data(report, active_redactor)
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("Sanitized pricing report must remain an object")
    return sanitized


def run_product_option_pricing_dry_run(
    input_path: Path,
    *,
    rmb_to_usd_rate: Decimal,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read one local linking report and write one sanitized local report."""

    path = Path(input_path)
    payload = load_local_linking_report(path)
    results = restore_product_option_link_results(payload)
    active_redactor = redactor or Redactor()
    report = build_product_option_pricing_report(
        results,
        input_file=_safe_input_reference(path, project_root),
        rmb_to_usd_rate=rmb_to_usd_rate,
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
