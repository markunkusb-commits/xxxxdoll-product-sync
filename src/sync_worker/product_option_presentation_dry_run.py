"""Pure-local presentation dry run for priced Product Option reports."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from . import retail_price_presentation
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


REPORT_FILENAME = "product-option-presentation-dry-run.json"
_USD_CENT = Decimal("0.01")
_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s\"'<>]+")


class ProductOptionPresentationDryRunInputError(ValueError):
    """Safe structural error for a local priced-option report."""


@dataclass(frozen=True, slots=True)
class EconomicOptionPricing:
    target_retail_usd: Decimal | None
    cost_usd: Decimal | None
    policy_version: str | None


@dataclass(frozen=True, slots=True)
class EconomicPricedOptionRecord:
    product_upgrade_name: str
    product_raw_value: str
    mapping_type: str | None
    registry_version: str | None
    supplier_cost: dict[str, object] | None
    economic_pricing: EconomicOptionPricing
    catalog_mapping: dict[str, object] | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EconomicProductOptionRecord:
    series: str
    product_identity: dict[str, object]
    source_trace: dict[str, object]
    included_features: tuple[str, ...]
    retail_pricing: dict[str, object]
    priced_upgrade_options: tuple[EconomicPricedOptionRecord, ...]
    warnings: tuple[str, ...]


def load_local_product_option_pricing_report(
    input_path: Path,
) -> Mapping[str, object]:
    """Load only one explicitly supplied local JSON report."""

    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise ProductOptionPresentationDryRunInputError(
            "Input must be a JSON file"
        )
    if not path.exists():
        raise ProductOptionPresentationDryRunInputError(
            "Input JSON file does not exist"
        )
    if not path.is_file():
        raise ProductOptionPresentationDryRunInputError(
            "Input JSON path must be a file"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProductOptionPresentationDryRunInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(payload, Mapping):
        raise ProductOptionPresentationDryRunInputError(
            "Input JSON root must be an object"
        )
    return payload


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must be an object"
        )
    return value


def _optional_mapping(
    value: object,
    *,
    label: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    return copy.deepcopy(dict(_mapping(value, label=label)))


def _items(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must be an array"
        )
    return value


def _text(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not nullable and not value.strip()):
        suffix = " or null" if nullable else ""
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must be text{suffix}"
        )
    return value


def _decimal(value: object, *, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(
        value, (str, int, float, Decimal)
    ):
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must be numeric or null"
        )
    try:
        decimal_value = (
            value if isinstance(value, Decimal) else Decimal(str(value))
        )
    except (InvalidOperation, ValueError) as error:
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must be numeric or null"
        ) from error
    if not decimal_value.is_finite():
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must be finite"
        )
    return decimal_value


def _warnings(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = _items(value, label=label)
    if any(not isinstance(item, str) for item in values):
        raise ProductOptionPresentationDryRunInputError(
            f"{label} must contain only text"
        )
    return tuple(values)


def _restore_priced_option(
    value: object,
    *,
    product_index: int,
    option_index: int,
) -> EconomicPricedOptionRecord:
    label = f"results[{product_index}] priced options[{option_index}]"
    option = _mapping(value, label=label)
    pricing = _mapping(option.get("pricing"), label=f"{label} pricing")
    name = _text(
        option.get("product_upgrade_name"),
        label=f"{label} product upgrade name",
    )
    raw_value = _text(
        option.get("product_raw_value"),
        label=f"{label} product raw value",
    )
    if name is None or raw_value is None:  # pragma: no cover - required guards
        raise AssertionError("Option identity unexpectedly incomplete")
    return EconomicPricedOptionRecord(
        product_upgrade_name=name,
        product_raw_value=raw_value,
        mapping_type=_text(
            option.get("mapping_type"),
            label=f"{label} mapping type",
            nullable=True,
        ),
        registry_version=_text(
            option.get("registry_version"),
            label=f"{label} registry version",
            nullable=True,
        ),
        supplier_cost=_optional_mapping(
            option.get("supplier_cost"),
            label=f"{label} supplier cost",
        ),
        economic_pricing=EconomicOptionPricing(
            target_retail_usd=_decimal(
                pricing.get("target_retail_usd"),
                label=f"{label} economic target",
            ),
            cost_usd=_decimal(
                pricing.get("cost_usd"),
                label=f"{label} USD cost",
            ),
            policy_version=_text(
                pricing.get("policy_version"),
                label=f"{label} pricing policy version",
                nullable=True,
            ),
        ),
        catalog_mapping=_optional_mapping(
            option.get("catalog_mapping"),
            label=f"{label} catalog mapping",
        ),
        warnings=_warnings(option.get("warnings"), label=f"{label} warnings"),
    )


def _restore_product(
    value: object,
    *,
    index: int,
) -> EconomicProductOptionRecord:
    label = f"results[{index}]"
    product = _mapping(value, label=label)
    series = _text(product.get("series"), label=f"{label} series")
    identity = copy.deepcopy(
        dict(_mapping(product.get("product_identity"), label=f"{label} identity"))
    )
    source = copy.deepcopy(
        dict(_mapping(product.get("source_trace"), label=f"{label} source trace"))
    )
    retail = copy.deepcopy(
        dict(
            _mapping(
                product.get("retail_pricing", {}),
                label=f"{label} retail pricing",
            )
        )
    )
    features = _items(
        product.get("included_features", []),
        label=f"{label} included features",
    )
    if any(not isinstance(item, str) for item in features):
        raise ProductOptionPresentationDryRunInputError(
            f"{label} included features must contain only text"
        )
    priced = _items(
        product.get("priced_upgrade_options"),
        label=f"{label} priced options",
    )
    if series is None:  # pragma: no cover - required guard
        raise AssertionError("Product series unexpectedly missing")
    return EconomicProductOptionRecord(
        series=series,
        product_identity=identity,
        source_trace=source,
        included_features=tuple(features),
        retail_pricing=retail,
        priced_upgrade_options=tuple(
            _restore_priced_option(
                option,
                product_index=index,
                option_index=option_index,
            )
            for option_index, option in enumerate(priced)
        ),
        warnings=_warnings(product.get("warnings"), label=f"{label} warnings"),
    )


def restore_economic_product_option_records(
    report: Mapping[str, object],
) -> list[EconomicProductOptionRecord]:
    """Restore economic option records without re-running pricing or mapping."""

    if not isinstance(report, Mapping):
        raise ProductOptionPresentationDryRunInputError(
            "Input pricing report must be an object"
        )
    return [
        _restore_product(value, index=index)
        for index, value in enumerate(
            _items(report.get("results"), label="results")
        )
    ]


def _presentation_report(
    option: EconomicPricedOptionRecord,
) -> tuple[dict[str, object], str, Decimal | None, Decimal | None]:
    try:
        result = retail_price_presentation.present_retail_price(
            option.economic_pricing.target_retail_usd
        )
    except retail_price_presentation.RetailPricePresentationValidationError as error:
        raise ProductOptionPresentationDryRunInputError(
            "Economic target failed presentation-policy validation"
        ) from error

    calculation = result.calculation
    display = result.presentation.display_price_usd
    target = result.economic.target_retail_usd
    report = {
        "product_upgrade_name": option.product_upgrade_name,
        "product_raw_value": option.product_raw_value,
        "mapping_type": option.mapping_type,
        "registry_version": option.registry_version,
        "supplier_cost": copy.deepcopy(option.supplier_cost),
        "economic_pricing": {
            "target_retail_usd": target,
            "cost_usd": option.economic_pricing.cost_usd,
            "policy_version": option.economic_pricing.policy_version,
        },
        "presentation": {
            "display_price_usd": display,
            "strategy": calculation.strategy,
            "candidate_price": calculation.candidate_price,
            "uplift_amount": calculation.uplift_amount,
            "uplift_rate": calculation.uplift_rate,
            "fallback_used": calculation.fallback_used,
            "policy_version": result.metadata.policy_version,
            "status": result.status,
        },
        "catalog_mapping": copy.deepcopy(option.catalog_mapping),
        "warnings": list(dict.fromkeys((*option.warnings, *result.warnings))),
    }
    return report, calculation.strategy, target, display


def _format_usd_total(value: Decimal) -> str:
    return format(value.quantize(_USD_CENT, rounding=ROUND_HALF_UP), "f")


def _format_rate(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


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


def build_product_option_presentation_report(
    products: Sequence[EconomicProductOptionRecord],
    *,
    input_file: str,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Apply the existing presentation policy to restored economic targets."""

    option_reports_by_product: list[dict[str, object]] = []
    total_options = 0
    presented_options = 0
    no_target_price = 0
    strategy_counts = {
        "x_99": 0,
        "nine_ending": 0,
        "x_99_fallback": 0,
        "already_presented": 0,
    }
    economic_total = Decimal("0")
    display_total = Decimal("0")
    max_uplift = Decimal("0")

    for product_index, product in enumerate(products):
        if not isinstance(product, EconomicProductOptionRecord):
            raise ProductOptionPresentationDryRunInputError(
                f"products[{product_index}] must be an economic product record"
            )
        presented: list[dict[str, object]] = []
        unpresented: list[dict[str, object]] = []
        for option in product.priced_upgrade_options:
            option_report, strategy, target, display = _presentation_report(option)
            total_options += 1
            if target is not None:
                economic_total += target
            if display is None:
                unpresented.append(option_report)
                no_target_price += 1
            else:
                presented.append(option_report)
                presented_options += 1
                display_total += display
                uplift = option_report["presentation"]["uplift_rate"]
                if isinstance(uplift, Decimal):
                    max_uplift = max(max_uplift, uplift)
            if strategy in strategy_counts:
                strategy_counts[strategy] += 1

        option_reports_by_product.append(
            {
                "series": product.series,
                "product_identity": copy.deepcopy(product.product_identity),
                "source_trace": copy.deepcopy(product.source_trace),
                "included_features": list(product.included_features),
                "retail_pricing": copy.deepcopy(product.retail_pricing),
                "presented_upgrade_options": presented,
                "unpresented_upgrade_options": unpresented,
                "warnings": list(product.warnings),
            }
        )

    report: dict[str, object] = {
        "status": "ok",
        "input_file": input_file,
        "policy": {
            "version": retail_price_presentation.POLICY_VERSION,
            "max_presentation_uplift_rate": format(
                retail_price_presentation.MAX_PRESENTATION_UPLIFT_RATE,
                "f",
            ),
        },
        "summary": {
            "total_products": len(products),
            "total_priced_options": total_options,
            "presented_options": presented_options,
            "unpresented_options": total_options - presented_options,
            "x99_presentations": strategy_counts["x_99"],
            "nine_ending_presentations": strategy_counts["nine_ending"],
            "fallback_presentations": strategy_counts["x_99_fallback"],
            "unchanged_presentations": strategy_counts["already_presented"],
            "no_target_price": no_target_price,
            "total_economic_target_usd": _format_usd_total(economic_total),
            "total_display_price_usd": _format_usd_total(display_total),
            "max_uplift_rate_observed": _format_rate(max_uplift),
        },
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": option_reports_by_product,
    }
    active_redactor = _report_redactor(report, redactor or Redactor())
    sanitized = sanitize_report_data(report, active_redactor)
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("Sanitized presentation report must remain an object")
    return sanitized


def run_product_option_presentation_dry_run(
    input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read one local pricing report and write one local presentation report."""

    path = Path(input_path)
    payload = load_local_product_option_pricing_report(path)
    products = restore_economic_product_option_records(payload)
    active_redactor = redactor or Redactor()
    report = build_product_option_presentation_report(
        products,
        input_file=_safe_input_reference(path, project_root),
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
