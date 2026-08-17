"""Pure-local, conservative parser for supplier additional options."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .sheet_layout import column_index_to_label, column_label_to_index


OptionCategory = Literal[
    "product_extra_option",
    "appearance",
    "material",
    "function",
    "accessory",
    "other",
]

_PRICE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?P<raw>(?P<plus>\+)?\s*(?P<currency>RMB|US\$|¥|￥)\s*"
    r"(?P<amount>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|[A-Za-z]+))"
    r"(?![A-Za-z0-9])"
)
_CURRENCY_ONLY_PATTERN = re.compile(r"(?i)^\s*(RMB|US\$|¥|￥)\s*$")
_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s\"'<>]+")
_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")
_HEADER_NAMES = frozenset(
    {
        "产品额外选项",
        "配件额外选项",
        "product additional options",
        "accessory additional options",
    }
)

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
    amount: Decimal | int | float | None
    currency: str | None
    raw_price: str | None
    price_range: str | None = None
    price_anchor: str | None = None
    shared_price_source: bool = False


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


def _numeric_amount(value: str) -> Decimal | None:
    normalized = value.replace(",", "")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", normalized):
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


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


def _currency(currency_token: str) -> str:
    return "USD" if currency_token.upper() == "US$" else "RMB"


def _separate_price(
    raw_price: str,
) -> tuple[Decimal | None, str | None, str, tuple[str, ...]]:
    matches = list(_PRICE_PATTERN.finditer(raw_price))
    if len(matches) == 1:
        match = matches[0]
        amount = _numeric_amount(match.group("amount"))
        warnings = () if amount is not None else ("unable to parse price",)
        return (
            amount,
            _currency(match.group("currency")),
            match.group("raw").strip(),
            warnings,
        )
    currency_only = _CURRENCY_ONLY_PATTERN.fullmatch(raw_price)
    if currency_only is not None:
        return (
            None,
            _currency(currency_only.group(1)),
            raw_price.strip(),
            ("unable to parse price",),
        )
    return None, None, raw_price, ("unable to parse price",)


def _parse_candidate(
    raw_name: str,
    source: AdditionalOptionSource,
    *,
    separate_price: str | None = None,
    price_range: str | None = None,
    price_anchor: str | None = None,
    shared_price_source: bool = False,
    category_override: OptionCategory | None = None,
    extra_warnings: tuple[str, ...] = (),
) -> AdditionalOptionRecord | RawAdditionalOptionEntry:
    if separate_price is not None:
        if _PRICE_PATTERN.search(raw_name) or _CURRENCY_ONLY_PATTERN.fullmatch(
            raw_name
        ):
            return _raw_entry(
                f"{raw_name} | {separate_price}", source, "unknown format"
            )
        amount, currency, raw_price, price_warnings = _separate_price(
            separate_price
        )
        option_name = raw_name
        warnings = [*price_warnings, *extra_warnings]
    else:
        price_matches = list(_PRICE_PATTERN.finditer(raw_name))
        if len(price_matches) > 1:
            return _raw_entry(raw_name, source, "unknown format")
        match = price_matches[0] if price_matches else None
        raw_price = None
        currency = None
        amount = None
        warnings = list(extra_warnings)
        if match is not None:
            raw_price = match.group("raw").strip()
            currency = _currency(match.group("currency"))
            amount = _numeric_amount(match.group("amount"))
            if amount is None:
                warnings.append("unable to parse price")
            option_name = (
                raw_name[: match.start()] + raw_name[match.end() :]
            ).strip(" \t-–—:;()[]")
        else:
            option_name = raw_name

    if not option_name or _CURRENCY_ONLY_PATTERN.fullmatch(option_name):
        return _raw_entry(raw_name, source, "unknown format")
    if category_override is None:
        category, category_warnings = _category(option_name)
        warnings.extend(category_warnings)
    else:
        category = category_override
    return AdditionalOptionRecord(
        identity=AdditionalOptionIdentity(
            option_name=option_name,
            raw_name=raw_name,
        ),
        pricing=AdditionalOptionPricing(
            amount=amount,
            currency=currency,
            raw_price=raw_price,
            price_range=price_range,
            price_anchor=price_anchor,
            shared_price_source=shared_price_source,
        ),
        category=category,
        source=source,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class _MergedPriceRange:
    raw_range: str
    start_row: int
    end_row: int
    start_column: str
    end_column: str
    anchor: str
    anchor_is_valid: bool
    range_is_valid: bool

    def is_shared_vertical_price_range(self, price_column: str) -> bool:
        return (
            self.start_row < self.end_row
            and self.start_column == price_column
            and self.end_column == price_column
            and self.anchor_is_valid
            and self.range_is_valid
        )


def _merged_price_positions(
    layout: Mapping[str, object],
) -> dict[tuple[int, str], _MergedPriceRange]:
    positions: dict[tuple[int, str], _MergedPriceRange] = {}
    raw_merges = layout.get("merged_ranges")
    if not isinstance(raw_merges, list):
        return positions
    for raw_merge in raw_merges:
        if not isinstance(raw_merge, Mapping):
            continue
        raw_range = raw_merge.get("range")
        start_row = raw_merge.get("start_row")
        end_row = raw_merge.get("end_row")
        start_column = raw_merge.get("start_column")
        end_column = raw_merge.get("end_column")
        if (
            not isinstance(raw_range, str)
            or not isinstance(start_row, int)
            or not isinstance(end_row, int)
            or not isinstance(start_column, str)
            or not isinstance(end_column, str)
        ):
            continue
        try:
            normalized_start_column = start_column.upper().strip()
            normalized_end_column = end_column.upper().strip()
            start_index = column_label_to_index(normalized_start_column)
            end_index = column_label_to_index(normalized_end_column)
        except ValueError:
            continue
        expected_anchor = f"{normalized_start_column}{start_row}"
        expected_range = (
            f"{normalized_start_column}{start_row}:"
            f"{normalized_end_column}{end_row}"
        )
        raw_anchor = raw_merge.get("anchor")
        if isinstance(raw_anchor, str) and raw_anchor.strip():
            anchor = raw_anchor.upper().strip()
            anchor_is_valid = anchor == expected_anchor
        else:
            anchor = expected_anchor
            anchor_is_valid = True
        merged_range = _MergedPriceRange(
            raw_range=raw_range,
            start_row=start_row,
            end_row=end_row,
            start_column=normalized_start_column,
            end_column=normalized_end_column,
            anchor=anchor,
            anchor_is_valid=anchor_is_valid,
            range_is_valid=raw_range.upper().strip() == expected_range,
        )
        for row in range(start_row, end_row + 1):
            for column in ("B", "E"):
                column_index = column_label_to_index(column)
                if start_index <= column_index <= end_index:
                    positions[(row, column)] = merged_range
    return positions


def _shared_merged_price_item(
    merged_range: _MergedPriceRange | None,
    *,
    name_column: str,
    price_column: str,
    by_position: Mapping[
        tuple[int, str],
        tuple[Mapping[str, object], str, AdditionalOptionSource],
    ],
) -> tuple[Mapping[str, object], str, AdditionalOptionSource] | None:
    if (
        merged_range is None
        or not merged_range.is_shared_vertical_price_range(price_column)
    ):
        return None
    anchor_item = by_position.get(
        (merged_range.start_row, price_column)
    )
    if anchor_item is None:
        return None
    for row in range(merged_range.start_row, merged_range.end_row + 1):
        name_item = by_position.get((row, name_column))
        if name_item is None or name_item[1].casefold() in _HEADER_NAMES:
            return None
        if (
            row != merged_range.start_row
            and by_position.get((row, price_column)) is not None
        ):
            return None
    return anchor_item


class AdditionalOptionParser:
    """Parse explicit A:B and D:E row pairs plus standalone option cells."""

    def parse(self, layout: Mapping[str, object]) -> AdditionalOptionParseResult:
        if not isinstance(layout, Mapping):
            raise AdditionalOptionParserError("Layout must be a mapping")
        raw_cells = layout.get("non_empty_cells")
        if not isinstance(raw_cells, list):
            raise AdditionalOptionParserError(
                "Layout must contain non_empty_cells"
            )

        cells: list[
            tuple[Mapping[str, object], str, AdditionalOptionSource]
        ] = []
        for raw_cell in raw_cells:
            if not isinstance(raw_cell, Mapping):
                continue
            raw_value = _safe_text(raw_cell.get("formatted_value"))
            if not raw_value:
                continue
            source = _source(raw_cell)
            cells.append((raw_cell, raw_value, source))

        cells.sort(key=lambda item: (item[2].row, item[2].column))
        by_position = {
            (source.row, source.column): item
            for item in cells
            for source in (item[2],)
        }
        merged_positions = _merged_price_positions(layout)
        used_coordinates: set[str] = set()
        used_price_ranges: set[str] = set()
        options: list[AdditionalOptionRecord] = []
        raw_entries: list[RawAdditionalOptionEntry] = []

        for name_column, price_column, category_override in (
            ("A", "B", "product_extra_option"),
            ("D", "E", "accessory"),
        ):
            for raw_cell, raw_name, source in cells:
                if source.column != name_column:
                    continue
                used_coordinates.add(source.raw_coordinate)
                price_item = by_position.get((source.row, price_column))
                merged_range = merged_positions.get(
                    (source.row, price_column)
                )
                price_range = (
                    merged_range.raw_range
                    if merged_range is not None
                    else None
                )
                price_anchor = (
                    merged_range.anchor
                    if merged_range is not None
                    else None
                )
                shared_item = _shared_merged_price_item(
                    merged_range,
                    name_column=name_column,
                    price_column=price_column,
                    by_position=by_position,
                )
                shared_price_source = shared_item is not None
                if shared_item is not None:
                    _, price_value, price_source = shared_item
                    used_coordinates.add(price_source.raw_coordinate)
                elif price_item is not None:
                    raw_price_cell, price_value, price_source = price_item
                    cell_range = raw_price_cell.get("merged_range")
                    if isinstance(cell_range, str):
                        price_range = cell_range
                        if price_anchor is None:
                            price_anchor = price_source.raw_coordinate
                    if price_range is not None and price_range in used_price_ranges:
                        price_value = None
                    else:
                        used_coordinates.add(price_source.raw_coordinate)
                        if price_range is not None:
                            used_price_ranges.add(price_range)
                else:
                    price_value = None

                extra_warnings: tuple[str, ...] = ()
                if price_range is not None and not shared_price_source:
                    if price_value is None:
                        extra_warnings = ("merged price range not reused",)
                    else:
                        extra_warnings = ("merged price range requires review",)
                if raw_name.casefold() in _HEADER_NAMES:
                    continue
                parsed = _parse_candidate(
                    raw_name,
                    source,
                    separate_price=price_value,
                    price_range=price_range,
                    price_anchor=price_anchor,
                    shared_price_source=shared_price_source,
                    category_override=category_override,
                    extra_warnings=extra_warnings,
                )
                if isinstance(parsed, AdditionalOptionRecord):
                    options.append(parsed)
                else:
                    raw_entries.append(parsed)

        for _, raw_value, source in cells:
            if source.raw_coordinate in used_coordinates:
                continue
            if raw_value.casefold() in _HEADER_NAMES:
                continue
            parsed = _parse_candidate(raw_value, source)
            if isinstance(parsed, AdditionalOptionRecord):
                options.append(parsed)
            else:
                raw_entries.append(parsed)
        return AdditionalOptionParseResult(
            options=tuple(options),
            raw_entries=tuple(raw_entries),
        )


def parse_additional_options(
    layout: Mapping[str, object],
) -> AdditionalOptionParseResult:
    return AdditionalOptionParser().parse(layout)
