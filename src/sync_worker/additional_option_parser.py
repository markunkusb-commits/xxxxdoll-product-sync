"""Pure-local, conservative parser for supplier additional options."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .sheet_layout import column_index_to_label


OptionCategory = Literal[
    "appearance",
    "material",
    "function",
    "accessory",
    "other",
]

_PRICE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?P<raw>(?P<plus>\+)?\s*(?P<currency>RMB|US\$|¥)\s*"
    r"(?P<amount>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|[A-Za-z]+))"
    r"(?![A-Za-z0-9])"
)
_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s\"'<>]+")
_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")

_CATEGORY_NAMES: tuple[tuple[OptionCategory, frozenset[str]], ...] = (
    (
        "appearance",
        frozenset(
            {
                "hair implant",
                "wigs",
                "wig",
                "eyes option",
                "skin tone",
                "color option",
            }
        ),
    ),
    ("material", frozenset({"material option", "silicone option"})),
    ("function", frozenset({"gel butt"})),
    ("accessory", frozenset({"hands option", "feet option"})),
)


class AdditionalOptionParserError(ValueError):
    """Safe structural error for an in-memory option layout fixture."""


@dataclass(frozen=True, slots=True)
class AdditionalOptionIdentity:
    option_name: str
    raw_name: str


@dataclass(frozen=True, slots=True)
class AdditionalOptionPricing:
    amount: int | float | None
    currency: str | None
    raw_price: str | None


@dataclass(frozen=True, slots=True)
class AdditionalOptionSource:
    row: int
    column: str
    raw_coordinate: str


@dataclass(frozen=True, slots=True)
class AdditionalOptionRecord:
    identity: AdditionalOptionIdentity
    pricing: AdditionalOptionPricing
    category: OptionCategory
    source: AdditionalOptionSource
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RawAdditionalOptionEntry:
    raw_value: str
    source: AdditionalOptionSource
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdditionalOptionParseResult:
    options: tuple[AdditionalOptionRecord, ...]
    raw_entries: tuple[RawAdditionalOptionEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _safe_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    without_urls = _URL_PATTERN.sub("[URL_REDACTED]", value)
    without_secrets = REPORT_SECRET_SCAN_PATTERN.sub(
        "[REDACTED_SECRET]", without_urls
    )
    return Redactor().text(without_secrets, limit=2000).strip()


def _source(cell: Mapping[str, object]) -> AdditionalOptionSource:
    raw_coordinate = cell.get("coordinate")
    coordinate = (
        raw_coordinate.upper().strip()
        if isinstance(raw_coordinate, str)
        else ""
    )
    coordinate_match = _COORDINATE_PATTERN.fullmatch(coordinate)

    raw_row = cell.get("row")
    if isinstance(raw_row, int) and not isinstance(raw_row, bool) and raw_row > 0:
        row = raw_row
    elif coordinate_match is not None:
        row = int(coordinate_match.group(2))
    else:
        row = 0

    raw_column = cell.get("column")
    if isinstance(raw_column, str) and raw_column.strip():
        column = raw_column.upper().strip()
    elif coordinate_match is not None:
        column = coordinate_match.group(1)
    else:
        column_index = cell.get("column_index")
        if (
            isinstance(column_index, int)
            and not isinstance(column_index, bool)
            and column_index > 0
        ):
            column = column_index_to_label(column_index)
        else:
            column = ""

    if not coordinate and row > 0 and column:
        coordinate = f"{column}{row}"
    return AdditionalOptionSource(
        row=row,
        column=column,
        raw_coordinate=coordinate,
    )


def _category(option_name: str) -> tuple[OptionCategory, tuple[str, ...]]:
    normalized = _NON_WORD_PATTERN.sub(" ", option_name.casefold()).strip()
    for category, names in _CATEGORY_NAMES:
        if normalized in names:
            return category, ()
    return "other", ("unknown option category",)


def _numeric_amount(value: str) -> int | float | None:
    normalized = value.replace(",", "")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", normalized):
        return None
    return float(normalized) if "." in normalized else int(normalized)


def _raw_entry(
    raw_value: str,
    source: AdditionalOptionSource,
    warning: str,
) -> RawAdditionalOptionEntry:
    return RawAdditionalOptionEntry(
        raw_value=raw_value,
        source=source,
        warnings=(warning,),
    )


class AdditionalOptionParser:
    """Parse option cells without inferring relationships between cells."""

    def parse(self, layout: Mapping[str, object]) -> AdditionalOptionParseResult:
        if not isinstance(layout, Mapping):
            raise AdditionalOptionParserError("Layout must be a mapping")
        raw_cells = layout.get("non_empty_cells")
        if not isinstance(raw_cells, list):
            raise AdditionalOptionParserError(
                "Layout must contain non_empty_cells"
            )

        options: list[AdditionalOptionRecord] = []
        raw_entries: list[RawAdditionalOptionEntry] = []
        for raw_cell in raw_cells:
            if not isinstance(raw_cell, Mapping):
                continue
            raw_value = _safe_text(raw_cell.get("formatted_value"))
            if not raw_value:
                continue
            source = _source(raw_cell)
            price_matches = list(_PRICE_PATTERN.finditer(raw_value))
            if len(price_matches) > 1:
                raw_entries.append(_raw_entry(raw_value, source, "unknown format"))
                continue

            match = price_matches[0] if price_matches else None
            raw_price: str | None = None
            currency: str | None = None
            amount: int | float | None = None
            warnings: list[str] = []
            if match is not None:
                raw_price = match.group("raw").strip()
                currency_token = match.group("currency").upper()
                currency = "USD" if currency_token == "US$" else "RMB"
                amount = _numeric_amount(match.group("amount"))
                if amount is None:
                    warnings.append("unable to parse price")
                option_name = (
                    raw_value[: match.start()] + raw_value[match.end() :]
                ).strip(" \t-–—:;()[]")
            else:
                option_name = raw_value

            if not option_name:
                raw_entries.append(_raw_entry(raw_value, source, "unknown format"))
                continue

            category, category_warnings = _category(option_name)
            warnings.extend(category_warnings)
            options.append(
                AdditionalOptionRecord(
                    identity=AdditionalOptionIdentity(
                        option_name=option_name,
                        raw_name=raw_value,
                    ),
                    pricing=AdditionalOptionPricing(
                        amount=amount,
                        currency=currency,
                        raw_price=raw_price,
                    ),
                    category=category,
                    source=source,
                    warnings=tuple(warnings),
                )
            )
        return AdditionalOptionParseResult(
            options=tuple(options),
            raw_entries=tuple(raw_entries),
        )


def parse_additional_options(
    layout: Mapping[str, object],
) -> AdditionalOptionParseResult:
    return AdditionalOptionParser().parse(layout)
