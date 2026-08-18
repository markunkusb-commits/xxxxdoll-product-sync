"""Pure USD retail-price presentation policy.

The economic target is immutable input to this layer.  This module only derives
an auditable display price and performs no cost, FX, profit, product-base, or
external-I/O work.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Literal


POLICY_VERSION = "retail-presentation-v1"
MAX_PRESENTATION_UPLIFT_RATE = Decimal("0.10")

_USD_CENT = Decimal("0.01")
_RATE_PRECISION = Decimal("0.0001")
_ONE_DOLLAR = Decimal("1.00")
_NINETY_NINE_CENTS = Decimal("0.99")
_LOW_PRICE_THRESHOLD = Decimal("50.00")


PresentationStatus = Literal["presented", "no_target_price"]
PresentationStrategy = Literal[
    "no_target_price",
    "zero_preserved",
    "already_presented",
    "x_99",
    "nine_ending",
    "x_99_fallback",
]


class RetailPricePresentationValidationError(ValueError):
    """Raised when an economic target cannot be safely presented."""


@dataclass(frozen=True, slots=True)
class EconomicRetailPrice:
    target_retail_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class PresentedRetailPrice:
    display_price_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class PresentationCalculation:
    strategy: PresentationStrategy
    candidate_price: Decimal | None
    uplift_amount: Decimal | None
    uplift_rate: Decimal | None
    fallback_used: bool


@dataclass(frozen=True, slots=True)
class PresentationMetadata:
    policy_version: str


@dataclass(frozen=True, slots=True)
class RetailPricePresentationResult:
    economic: EconomicRetailPrice
    presentation: PresentedRetailPrice
    calculation: PresentationCalculation
    metadata: PresentationMetadata
    warnings: tuple[str, ...]
    status: PresentationStatus

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe structure without binary-float conversion."""

        return _json_safe(asdict(self))


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _validate_target(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise RetailPricePresentationValidationError(
            "target_retail_usd must be Decimal or None"
        )
    if not value.is_finite():
        raise RetailPricePresentationValidationError(
            "target_retail_usd must be finite"
        )
    if value < 0:
        raise RetailPricePresentationValidationError(
            "target_retail_usd cannot be negative"
        )
    return value


def _is_x_99(value: Decimal) -> bool:
    quantized = value.quantize(_USD_CENT, rounding=ROUND_HALF_UP)
    return value == quantized and quantized % _ONE_DOLLAR == _NINETY_NINE_CENTS


def _is_nine_ending_whole_dollar(value: Decimal) -> bool:
    quantized = value.quantize(_USD_CENT, rounding=ROUND_HALF_UP)
    return (
        value == quantized
        and quantized == quantized.to_integral_value()
        and int(quantized) % 10 == 9
    )


def _is_presented(value: Decimal) -> bool:
    return _is_x_99(value) or _is_nine_ending_whole_dollar(value)


def _next_x_99(target: Decimal) -> Decimal:
    whole_dollars = target.to_integral_value(rounding=ROUND_FLOOR)
    candidate = whole_dollars + _NINETY_NINE_CENTS
    if candidate < target:
        candidate += _ONE_DOLLAR
    return candidate.quantize(_USD_CENT)


def _next_nine_ending(target: Decimal) -> Decimal:
    ceiling = target.to_integral_value(rounding=ROUND_CEILING)
    increment = (9 - int(ceiling) % 10) % 10
    return (ceiling + Decimal(increment)).quantize(_USD_CENT)


def _uplift_rate(amount: Decimal, target: Decimal) -> Decimal:
    if target == 0:
        return Decimal("0.0000")
    return (amount / target).quantize(_RATE_PRECISION, rounding=ROUND_HALF_UP)


def _result(
    target: Decimal,
    display: Decimal,
    *,
    strategy: PresentationStrategy,
    candidate: Decimal,
    fallback_used: bool,
) -> RetailPricePresentationResult:
    if display < target:  # pragma: no cover - invariant guard
        raise AssertionError("Presentation price cannot be below economic target")
    uplift_amount = display - target
    return RetailPricePresentationResult(
        economic=EconomicRetailPrice(target_retail_usd=target),
        presentation=PresentedRetailPrice(display_price_usd=display),
        calculation=PresentationCalculation(
            strategy=strategy,
            candidate_price=candidate,
            uplift_amount=uplift_amount,
            uplift_rate=_uplift_rate(uplift_amount, target),
            fallback_used=fallback_used,
        ),
        metadata=PresentationMetadata(policy_version=POLICY_VERSION),
        warnings=(),
        status="presented",
    )


def present_retail_price(
    target_retail_usd: Decimal | None,
) -> RetailPricePresentationResult:
    """Derive one USD display price without changing its economic target."""

    target = _validate_target(target_retail_usd)
    if target is None:
        return RetailPricePresentationResult(
            economic=EconomicRetailPrice(target_retail_usd=None),
            presentation=PresentedRetailPrice(display_price_usd=None),
            calculation=PresentationCalculation(
                strategy="no_target_price",
                candidate_price=None,
                uplift_amount=None,
                uplift_rate=None,
                fallback_used=False,
            ),
            metadata=PresentationMetadata(policy_version=POLICY_VERSION),
            warnings=(),
            status="no_target_price",
        )

    if target == 0:
        zero = Decimal("0.00")
        return _result(
            target,
            zero,
            strategy="zero_preserved",
            candidate=zero,
            fallback_used=False,
        )

    if _is_presented(target):
        display = target.quantize(_USD_CENT)
        return _result(
            target,
            display,
            strategy="already_presented",
            candidate=display,
            fallback_used=False,
        )

    if target < _LOW_PRICE_THRESHOLD:
        candidate = _next_x_99(target)
        return _result(
            target,
            candidate,
            strategy="x_99",
            candidate=candidate,
            fallback_used=False,
        )

    nine_ending_candidate = _next_nine_ending(target)
    exact_candidate_uplift_rate = (
        nine_ending_candidate - target
    ) / target
    if exact_candidate_uplift_rate > MAX_PRESENTATION_UPLIFT_RATE:
        display = _next_x_99(target)
        return _result(
            target,
            display,
            strategy="x_99_fallback",
            candidate=nine_ending_candidate,
            fallback_used=True,
        )

    return _result(
        target,
        nine_ending_candidate,
        strategy="nine_ending",
        candidate=nine_ending_candidate,
        fallback_used=False,
    )


def present_retail_prices(
    target_retail_prices: Sequence[Decimal | None],
) -> list[RetailPricePresentationResult]:
    """Present a sequence in stable input order without mutating it."""

    if isinstance(target_retail_prices, (str, bytes)) or not isinstance(
        target_retail_prices, Sequence
    ):
        raise RetailPricePresentationValidationError(
            "target_retail_prices must be a sequence"
        )
    return [present_retail_price(value) for value in target_retail_prices]
