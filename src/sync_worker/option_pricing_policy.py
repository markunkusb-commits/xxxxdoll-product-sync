"""Deterministic retail-price policy for supplier additional-option costs.

This module is deliberately pure: it accepts an already parsed supplier cost and
an explicitly injected exchange rate.  It does not load configuration, contact
an FX provider, or combine an option price with a base product price.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

from .additional_option_parser import AdditionalOptionPricing


POLICY_VERSION = "option-retail-v1"
MARKUP_RATE = Decimal("0.50")
MARKUP_MULTIPLIER = Decimal("1.50")
MINIMUM_PROFIT_USD = Decimal("15.00")

_USD_CENT = Decimal("0.01")
_INTERMEDIATE_PRECISION = Decimal("0.0001")
_RMB_CURRENCIES = frozenset({"RMB", "CNY"})
_SUPPORTED_CURRENCIES = _RMB_CURRENCIES | {"USD"}


class OptionPricingPolicyError(ValueError):
    """Base class for safe, deterministic option-pricing failures."""


class UnsupportedOptionCurrencyError(OptionPricingPolicyError):
    """Raised when no explicit conversion rule exists for a currency."""


class MissingExchangeRateError(OptionPricingPolicyError):
    """Raised when an RMB/CNY supplier cost has no injected conversion rate."""


class InvalidExchangeRateError(OptionPricingPolicyError):
    """Raised when an injected conversion rate is unsafe or unusable."""


class InvalidSupplierCostError(OptionPricingPolicyError):
    """Raised when a supplier cost is negative or not a finite number."""


PricingStatus = Literal[
    "priced",
    "no_supplier_price",
    "zero_supplier_cost",
]


@dataclass(frozen=True, slots=True)
class SupplierCostSnapshot:
    """Immutable supplier-side cost; never a customer retail price."""

    amount: Decimal | None
    currency: str | None
    raw_value: str | None


@dataclass(frozen=True, slots=True)
class FXConversion:
    source_currency: str
    target_currency: str
    rate: Decimal
    rate_source: str
    rate_timestamp: str | None


@dataclass(frozen=True, slots=True)
class OptionPriceCalculation:
    cost_usd: Decimal
    markup_rate: Decimal
    markup_price_usd: Decimal
    minimum_profit_usd: Decimal
    minimum_profit_price_usd: Decimal


@dataclass(frozen=True, slots=True)
class OptionRetailCandidate:
    target_retail_usd: Decimal


@dataclass(frozen=True, slots=True)
class OptionPricingMetadata:
    policy_version: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OptionRetailPricingResult:
    status: PricingStatus
    supplier_cost: SupplierCostSnapshot
    fx: FXConversion | None
    calculation: OptionPriceCalculation | None
    retail: OptionRetailCandidate | None
    metadata: OptionPricingMetadata

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation without converting through float."""

        return _json_safe(asdict(self))


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _supplier_amount(value: int | float | Decimal) -> Decimal:
    if isinstance(value, bool):
        raise InvalidSupplierCostError("supplier cost must be a finite number")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidSupplierCostError(
            "supplier cost must be a finite number"
        ) from exc
    if not amount.is_finite():
        raise InvalidSupplierCostError("supplier cost must be a finite number")
    if amount < 0:
        raise InvalidSupplierCostError("supplier cost cannot be negative")
    return amount


def _currency(value: str | None) -> str:
    currency = (value or "").strip().upper()
    if currency not in _SUPPORTED_CURRENCIES:
        raise UnsupportedOptionCurrencyError(
            f"unsupported supplier cost currency: {currency or '[missing]'}"
        )
    return currency


def _rmb_rate(value: Decimal | None) -> Decimal:
    if value is None:
        raise MissingExchangeRateError(
            "an injected RMB-to-USD Decimal rate is required"
        )
    if not isinstance(value, Decimal):
        raise InvalidExchangeRateError(
            "RMB-to-USD rate must be injected as Decimal"
        )
    if not value.is_finite() or value <= 0:
        raise InvalidExchangeRateError(
            "RMB-to-USD rate must be finite and greater than zero"
        )
    return value


def _intermediate(value: Decimal) -> Decimal:
    return value.quantize(_INTERMEDIATE_PRECISION, rounding=ROUND_HALF_UP)


def calculate_option_retail_price(
    supplier_cost: AdditionalOptionPricing | None,
    *,
    rmb_to_usd_rate: Decimal | None = None,
    rate_source: str = "injected",
    rate_timestamp: str | None = None,
) -> OptionRetailPricingResult:
    """Calculate a USD retail candidate from one supplier option cost.

    The result never includes a base product price and never mutates or replaces
    the supplier cost.  RMB/CNY conversion requires an explicitly supplied
    :class:`Decimal` rate; USD costs use an auditable identity conversion.
    """

    if supplier_cost is None or supplier_cost.amount is None:
        snapshot = SupplierCostSnapshot(
            amount=None,
            currency=(supplier_cost.currency if supplier_cost else None),
            raw_value=(supplier_cost.raw_price if supplier_cost else None),
        )
        return OptionRetailPricingResult(
            status="no_supplier_price",
            supplier_cost=snapshot,
            fx=None,
            calculation=None,
            retail=None,
            metadata=OptionPricingMetadata(
                policy_version=POLICY_VERSION,
                warnings=(
                    "supplier price is missing; no retail candidate calculated",
                ),
            ),
        )

    amount = _supplier_amount(supplier_cost.amount)
    currency = _currency(supplier_cost.currency)
    snapshot = SupplierCostSnapshot(
        amount=amount,
        currency=currency,
        raw_value=supplier_cost.raw_price,
    )

    if currency in _RMB_CURRENCIES:
        rate = _rmb_rate(rmb_to_usd_rate)
        fx = FXConversion(
            source_currency=currency,
            target_currency="USD",
            rate=rate,
            rate_source=rate_source,
            rate_timestamp=rate_timestamp,
        )
    else:
        rate = Decimal("1")
        fx = FXConversion(
            source_currency="USD",
            target_currency="USD",
            rate=rate,
            rate_source="not_required",
            rate_timestamp=None,
        )

    exact_cost_usd = amount * rate
    exact_markup_price = exact_cost_usd * MARKUP_MULTIPLIER
    exact_minimum_profit_price = exact_cost_usd + MINIMUM_PROFIT_USD
    calculation = OptionPriceCalculation(
        cost_usd=_intermediate(exact_cost_usd),
        markup_rate=MARKUP_RATE,
        markup_price_usd=_intermediate(exact_markup_price),
        minimum_profit_usd=MINIMUM_PROFIT_USD,
        minimum_profit_price_usd=_intermediate(exact_minimum_profit_price),
    )

    if amount == 0:
        return OptionRetailPricingResult(
            status="zero_supplier_cost",
            supplier_cost=snapshot,
            fx=fx,
            calculation=calculation,
            retail=OptionRetailCandidate(target_retail_usd=Decimal("0.00")),
            metadata=OptionPricingMetadata(
                policy_version=POLICY_VERSION,
                warnings=(
                    "zero supplier cost preserved as a free option",
                ),
            ),
        )

    exact_target = max(exact_markup_price, exact_minimum_profit_price)
    target_retail = exact_target.quantize(_USD_CENT, rounding=ROUND_HALF_UP)
    return OptionRetailPricingResult(
        status="priced",
        supplier_cost=snapshot,
        fx=fx,
        calculation=calculation,
        retail=OptionRetailCandidate(target_retail_usd=target_retail),
        metadata=OptionPricingMetadata(
            policy_version=POLICY_VERSION,
            warnings=(),
        ),
    )
