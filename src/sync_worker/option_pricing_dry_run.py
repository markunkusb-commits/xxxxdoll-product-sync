"""Pure-local pricing dry run for parsed Additional Option reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .additional_option_parser import AdditionalOptionPricing
from .option_pricing_policy import (
    MARKUP_RATE,
    MINIMUM_PROFIT_USD,
    POLICY_VERSION,
    OptionPricingPolicyError,
    UnsupportedOptionCurrencyError,
    calculate_option_retail_price,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor


REPORT_FILENAME = "option-pricing-dry-run.json"
RATE_SOURCE = "cli_injected"


class OptionPricingDryRunInputError(ValueError):
    """Safe validation error for a local option-pricing dry run."""


def parse_rmb_to_usd_rate(value: str) -> Decimal:
    """Parse one finite, positive CLI rate without using binary float."""

    try:
        rate = Decimal(value.strip())
    except (AttributeError, InvalidOperation, ValueError) as error:
        raise OptionPricingDryRunInputError(
            "RMB-to-USD rate must be a positive decimal"
        ) from error
    if not rate.is_finite() or rate <= 0:
        raise OptionPricingDryRunInputError(
            "RMB-to-USD rate must be a positive decimal"
        )
    return rate


def load_local_option_report(input_path: Path) -> Mapping[str, object]:
    path = Path(input_path)
    if path.suffix.casefold() != ".json":
        raise OptionPricingDryRunInputError("Input must be a JSON file")
    if not path.exists():
        raise OptionPricingDryRunInputError("Input JSON file does not exist")
    if not path.is_file():
        raise OptionPricingDryRunInputError("Input JSON path must be a file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OptionPricingDryRunInputError(
            "Input must be a readable UTF-8 JSON file"
        ) from error
    if not isinstance(payload, Mapping):
        raise OptionPricingDryRunInputError("Input JSON root must be an object")
    return payload


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OptionPricingDryRunInputError(f"{label} must be an object")
    return value


def _text(value: object, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        suffix = " or null" if nullable else ""
        raise OptionPricingDryRunInputError(f"{label} must be text{suffix}")
    return value


def _amount(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(
        value, (str, int, float, Decimal)
    ):
        raise OptionPricingDryRunInputError(
            "option price amount must be numeric or null"
        )
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise OptionPricingDryRunInputError(
            "option price amount must be numeric or null"
        ) from error
    if not amount.is_finite():
        raise OptionPricingDryRunInputError(
            "option price amount must be finite"
        )
    return amount


def _warnings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise OptionPricingDryRunInputError(
            "option warnings must be an array of text"
        )
    return tuple(value)


def _boolean(value: object, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise OptionPricingDryRunInputError(f"{label} must be boolean")
    return value


def _combined_warnings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    combined: list[str] = []
    for group in groups:
        for warning in group:
            if warning not in combined:
                combined.append(warning)
    return tuple(combined)


def _restore_price(option: Mapping[str, object]) -> AdditionalOptionPricing:
    raw_price = option.get("price")
    if raw_price is None:
        price: Mapping[str, object] = {}
    else:
        price = _mapping(raw_price, label="option price")
    return AdditionalOptionPricing(
        amount=_amount(price.get("amount")),
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
    )


def _source_coordinate(option: Mapping[str, object]) -> str | None:
    direct = option.get("source_coordinate")
    if direct is not None:
        return _text(direct, label="option source coordinate")
    source = option.get("source")
    if source is None:
        return None
    source_mapping = _mapping(source, label="option source")
    return _text(
        source_mapping.get("raw_coordinate"),
        label="option source coordinate",
        nullable=True,
    )


def _unsupported_option_report(
    *,
    category: str,
    option_name: str,
    pricing: AdditionalOptionPricing,
    source_coordinate: str | None,
    warnings: tuple[str, ...],
) -> dict[str, object]:
    merged_warnings = _combined_warnings(
        warnings,
        ("unsupported supplier cost currency",),
    )
    return {
        "category": category,
        "option_name": option_name,
        "source_coordinate": source_coordinate,
        "supplier_cost": {
            "amount": pricing.amount,
            "currency": pricing.currency,
            "raw_price": pricing.raw_price,
            "price_range": pricing.price_range,
            "price_anchor": pricing.price_anchor,
            "shared_price_source": pricing.shared_price_source,
        },
        "fx": None,
        "calculation": None,
        "retail": None,
        "metadata": {
            "policy_version": POLICY_VERSION,
            "pricing_status": "unsupported_currency",
            "warnings": list(merged_warnings),
        },
    }


def _priced_option_report(
    option: Mapping[str, object],
    *,
    rmb_to_usd_rate: Decimal,
) -> dict[str, object]:
    category = _text(option.get("category"), label="option category")
    option_name = _text(option.get("option_name"), label="option name")
    if category is None or option_name is None:  # pragma: no cover - type guard
        raise AssertionError("Required option text unexpectedly missing")
    pricing = _restore_price(option)
    source_coordinate = _source_coordinate(option)
    input_warnings = _warnings(option.get("warnings"))

    try:
        result = calculate_option_retail_price(
            pricing,
            rmb_to_usd_rate=rmb_to_usd_rate,
            rate_source=RATE_SOURCE,
        )
    except UnsupportedOptionCurrencyError:
        return _unsupported_option_report(
            category=category,
            option_name=option_name,
            pricing=pricing,
            source_coordinate=source_coordinate,
            warnings=input_warnings,
        )
    except OptionPricingPolicyError as error:
        raise OptionPricingDryRunInputError(
            "Option supplier cost failed pricing-policy validation"
        ) from error

    policy_warnings = result.metadata.warnings
    warnings = _combined_warnings(input_warnings, policy_warnings)
    fx = None
    if result.fx is not None:
        fx = {
            "source_currency": result.fx.source_currency,
            "target_currency": result.fx.target_currency,
            "rate": result.fx.rate,
            "rate_source": result.fx.rate_source,
        }
    calculation = None
    if result.calculation is not None:
        calculation = {
            "cost_usd": result.calculation.cost_usd,
            "markup_price_usd": result.calculation.markup_price_usd,
            "minimum_profit_price_usd": (
                result.calculation.minimum_profit_price_usd
            ),
        }
    retail = None
    if result.retail is not None:
        retail = {
            "target_retail_usd": result.retail.target_retail_usd,
        }
    return {
        "category": category,
        "option_name": option_name,
        "source_coordinate": source_coordinate,
        "supplier_cost": {
            "amount": result.supplier_cost.amount,
            "currency": result.supplier_cost.currency,
            "raw_price": result.supplier_cost.raw_value,
            "price_range": pricing.price_range,
            "price_anchor": pricing.price_anchor,
            "shared_price_source": pricing.shared_price_source,
        },
        "fx": fx,
        "calculation": calculation,
        "retail": retail,
        "metadata": {
            "policy_version": result.metadata.policy_version,
            "pricing_status": result.status,
            "warnings": list(warnings),
        },
    }


def build_option_pricing_report(
    payload: Mapping[str, object],
    *,
    input_file: str,
    rmb_to_usd_rate: Decimal,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise OptionPricingDryRunInputError("Input report must be an object")
    if not isinstance(rmb_to_usd_rate, Decimal):
        raise OptionPricingDryRunInputError("FX rate must be Decimal")
    validated_rate = parse_rmb_to_usd_rate(format(rmb_to_usd_rate, "f"))
    raw_options = payload.get("options")
    if not isinstance(raw_options, list):
        raise OptionPricingDryRunInputError(
            "Input report must contain an options array"
        )

    option_reports: list[dict[str, object]] = []
    summary = {
        "total_options": 0,
        "priced_options": 0,
        "zero_cost_options": 0,
        "missing_price_options": 0,
        "unsupported_currency_options": 0,
        "warnings_count": 0,
    }
    for index, raw_option in enumerate(raw_options):
        option = _mapping(raw_option, label=f"options[{index}]")
        option_report = _priced_option_report(
            option,
            rmb_to_usd_rate=validated_rate,
        )
        option_reports.append(option_report)
        status = option_report["metadata"]["pricing_status"]
        summary["total_options"] += 1
        if status == "priced":
            summary["priced_options"] += 1
        elif status == "zero_supplier_cost":
            summary["zero_cost_options"] += 1
        elif status == "no_supplier_price":
            summary["missing_price_options"] += 1
        elif status == "unsupported_currency":
            summary["unsupported_currency_options"] += 1
        summary["warnings_count"] += len(
            option_report["metadata"]["warnings"]
        )

    report = {
        "status": "ok",
        "input_file": input_file,
        "fx": {
            "rate": format(validated_rate, "f"),
            "rmb_to_usd": format(validated_rate, "f"),
            "source_currency": "RMB",
            "target_currency": "USD",
            "rate_source": RATE_SOURCE,
        },
        "policy": {
            "version": POLICY_VERSION,
            "markup_rate": format(MARKUP_RATE, "f"),
            "minimum_profit_usd": format(MINIMUM_PROFIT_USD, "f"),
        },
        "summary": summary,
        "options": option_reports,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
    }
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("Sanitized pricing report must remain an object")
    return sanitized


def run_option_pricing_dry_run(
    input_path: Path,
    *,
    rmb_to_usd_rate: Decimal,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    path = Path(input_path)
    payload = load_local_option_report(path)
    active_redactor = redactor or Redactor()
    report = build_option_pricing_report(
        payload,
        input_file=_safe_input_reference(path, project_root),
        rmb_to_usd_rate=rmb_to_usd_rate,
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
