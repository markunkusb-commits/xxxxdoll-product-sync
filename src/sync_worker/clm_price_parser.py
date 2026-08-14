"""Pure-local CLM RMB Price List block parser (V1)."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .sheet_layout import column_index_to_label, column_label_to_index


SUPPORTED_SERIES = frozenset({"classic", "pro", "ulw", "ultra"})
_URL_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)[^\s\"'<>]+"
    r"|\b(?:drive|docs)\.google\.com(?:/[^\s\"'<>]*)?"
)
_PRICE_PATTERN = re.compile(
    r"(?i)(?P<sign>\+)?\s*(?P<currency>RMB|US\$|¥)\s*"
    r"(?P<amount>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
)
_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")
_SPEC_MAX_COLUMN_INDEX = 32  # AF; used only as structural evidence.
_COMMERCIAL_MIN_COLUMN_INDEX = 33  # AG supports confirmed "More collocation".


def _label_key(value: str) -> str:
    normalized = value.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


_SPECIFICATION_ALIASES = {
    "model": "model",
    "cup": "cup",
    "height": "height",
    "height model": "height_model",
    "height no head": "height_no_head",
    "upper chest": "upper_chest",
    "lower chest": "lower_chest",
    "waist": "waist",
    "hip": "hip",
    "thigh": "thigh",
    "thigh circumference": "thigh_circumference",
    "leg length": "leg_length",
    "sole": "sole",
    "sole length": "sole_length",
    "shoulder": "shoulder",
    "neck circumference": "neck_circumference",
    "arm length": "arm_length",
    "upper arm circumference": "upper_arm_circumference",
    "width": "width",
    "thickness": "thickness",
    "n w": "net_weight",
    "nw": "net_weight",
    "g w": "gross_weight",
    "gw": "gross_weight",
    "carton size": "carton_size",
    "vagina anus": "vagina_anus",
    "oral vagina anus": "oral_vagina_anus",
}

_PRICE_LABELS = (
    ("fob unit price", "fob_unit_price"),
    ("minimum retail price", "minimum_retail_price"),
    ("normal options price", "normal_options_price"),
    ("normal option price", "normal_options_price"),
    ("price including head", "including_head_price"),
    ("including head", "including_head_price"),
    ("only body", "body_only_price"),
    ("body only", "body_only_price"),
)


class CLMPriceParserError(ValueError):
    """Safe structural input error with no raw supplier content."""


@dataclass(frozen=True, slots=True)
class ParsedPrice:
    raw_value: str
    currency: str | None
    amount: int | float | None
    context: str


@dataclass(frozen=True, slots=True)
class RawSpecification:
    field: str
    value: str
    field_coordinate: str
    value_coordinate: str


@dataclass(frozen=True, slots=True)
class UpgradeOption:
    name: str
    raw_value: str
    price: ParsedPrice | None = None


@dataclass(frozen=True, slots=True)
class RawCommercialEntry:
    field: str | None
    value: str
    coordinate: str


@dataclass(frozen=True, slots=True)
class Pricing:
    fob_unit_price: ParsedPrice | None = None
    minimum_retail_price: ParsedPrice | None = None
    normal_options_price: ParsedPrice | None = None
    body_only_price: ParsedPrice | None = None
    including_head_price: ParsedPrice | None = None


@dataclass(frozen=True, slots=True)
class BlockSource:
    start_row: int
    end_row: int


@dataclass(frozen=True, slots=True)
class CLMProductBlock:
    series: str
    raw_series_title: str
    model: str | None
    model_raw: str | None
    cup: str | None
    specifications: dict[str, str]
    raw_specifications: list[RawSpecification]
    included_features: list[str]
    upgrade_options: list[UpgradeOption]
    notices: list[str]
    pricing: Pricing
    photo_download_link: str | None
    raw_commercial_entries: list[RawCommercialEntry]
    source: BlockSource
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Cell:
    coordinate: str
    row: int
    column_index: int
    value: str
    raw_value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PendingPrice:
    key: str
    label_cell: _Cell
    parsed_price: ParsedPrice | None
    price_cell: _Cell | None


def recognize_series_title(value: str) -> str | None:
    normalized = _label_key(value)
    if not normalized.startswith("clm "):
        return None
    series = normalized.removeprefix("clm ")
    return series if series in SUPPORTED_SERIES else None


def parse_price(raw_value: str, *, context: str) -> ParsedPrice:
    sanitized = _sanitize_value(raw_value)
    match = _PRICE_PATTERN.search(raw_value)
    if match is None:
        return ParsedPrice(
            raw_value=sanitized,
            currency=None,
            amount=None,
            context=context,
        )
    currency_token = match.group("currency").upper()
    currency = {"RMB": "RMB", "US$": "USD", "¥": "CNY"}[currency_token]
    amount_text = match.group("amount").replace(",", "")
    numeric_amount: int | float
    if "." in amount_text:
        numeric_amount = float(amount_text)
    else:
        numeric_amount = int(amount_text)
    return ParsedPrice(
        raw_value=sanitized,
        currency=currency,
        amount=numeric_amount,
        context=context,
    )


def _sanitize_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    without_urls = _URL_PATTERN.sub("[URL_REDACTED]", value)
    without_explicit_secrets = REPORT_SECRET_SCAN_PATTERN.sub(
        "[REDACTED_SECRET]", without_urls
    )
    return Redactor().text(without_explicit_secrets, limit=2000)


def _coordinate_parts(coordinate: str) -> tuple[int, int] | None:
    match = _COORDINATE_PATTERN.fullmatch(coordinate.upper())
    if match is None:
        return None
    return int(match.group(2)), column_label_to_index(match.group(1))


def _parse_cells(layout: Mapping[str, object]) -> list[_Cell]:
    raw_cells = layout.get("non_empty_cells")
    if not isinstance(raw_cells, list):
        raise CLMPriceParserError("Layout must contain non_empty_cells")
    cells: list[_Cell] = []
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, Mapping):
            continue
        raw_value = raw_cell.get("formatted_value")
        if not isinstance(raw_value, str) or raw_value == "":
            continue
        coordinate = raw_cell.get("coordinate")
        coordinate_text = coordinate.upper() if isinstance(coordinate, str) else ""
        parts = _coordinate_parts(coordinate_text)
        row = raw_cell.get("row")
        column_index = raw_cell.get("column_index")
        if not isinstance(row, int) or isinstance(row, bool) or row < 1:
            row = parts[0] if parts else None
        if (
            not isinstance(column_index, int)
            or isinstance(column_index, bool)
            or column_index < 1
        ):
            column_index = parts[1] if parts else None
        if row is None or column_index is None:
            raise CLMPriceParserError("Layout cell position was invalid")
        if not coordinate_text:
            coordinate_text = f"{column_index_to_label(column_index)}{row}"
        cells.append(
            _Cell(
                coordinate=coordinate_text,
                row=row,
                column_index=column_index,
                value=_sanitize_value(raw_value),
                raw_value=raw_value,
            )
        )
    return sorted(cells, key=lambda cell: (cell.row, cell.column_index))


def _snake_name(value: str) -> str:
    result = _NON_WORD_PATTERN.sub("_", value.casefold()).strip("_")
    return result or "unknown_specification"


def _specification_key(value: str) -> str | None:
    return _SPECIFICATION_ALIASES.get(_label_key(value))


def _commercial_price_key(value: str) -> str | None:
    normalized = _label_key(value)
    for label, key in _PRICE_LABELS:
        if label in normalized:
            return key
    return None


def _price_context_key(value: str) -> str | None:
    normalized = _label_key(value)
    if "price including head" in normalized or "including head" in normalized:
        return "including_head_price"
    if "only body" in normalized or "body only" in normalized:
        return "body_only_price"
    return None


def _is_includes_header(value: str) -> bool:
    normalized = _label_key(value)
    return "price includes the following" in normalized or normalized in {
        "included features",
        "features included",
    }


def _is_upgrade_header(value: str) -> bool:
    return "upgrade option" in _label_key(value)


def _is_photo_label(value: str) -> bool:
    return "photo download link" in _label_key(value)


def _is_notice(value: str) -> bool:
    normalized = _label_key(value)
    return (
        normalized.startswith("notice")
        or normalized.startswith("note")
        or "wig in photo" in normalized
        or "can only" in normalized
        or "only be" in normalized
    )


def _contains_url(value: str) -> bool:
    return bool(_URL_PATTERN.search(value)) or "[URL_REDACTED]" in value


def _looks_like_specification_pair(field_cell: _Cell, value_cell: _Cell) -> bool:
    if field_cell.column_index > _SPEC_MAX_COLUMN_INDEX:
        return False
    if value_cell.column_index <= field_cell.column_index:
        return False
    if value_cell.column_index - field_cell.column_index > 10:
        return False
    if any(
        predicate(field_cell.value)
        for predicate in (
            _is_includes_header,
            _is_upgrade_header,
            _is_photo_label,
            _is_notice,
        )
    ):
        return False
    if _commercial_price_key(field_cell.value) is not None:
        return False
    field_text = field_cell.value.strip()
    return bool(field_text) and not _contains_url(field_cell.raw_value)


def _add_specification(
    field_cell: _Cell,
    value_cell: _Cell,
    specifications: dict[str, str],
    raw_specifications: list[RawSpecification],
    warnings: list[str],
) -> tuple[str, str]:
    known_key = _specification_key(field_cell.value)
    key = known_key or _snake_name(field_cell.value)
    if known_key is None:
        warnings.append(f"Unknown specification preserved: {field_cell.value}")
    if key in specifications:
        suffix = 2
        while f"{key}__{suffix}" in specifications:
            suffix += 1
        warnings.append(f"Duplicate specification preserved: {field_cell.value}")
        key = f"{key}__{suffix}"
    specifications[key] = value_cell.value
    raw_specifications.append(
        RawSpecification(
            field=field_cell.value,
            value=value_cell.value,
            field_coordinate=field_cell.coordinate,
            value_coordinate=value_cell.coordinate,
        )
    )
    if known_key == "height_model":
        warnings.append("Height(Model) preserved without splitting height and model")
    return key, value_cell.value


def _parse_specifications(
    rows: Mapping[int, list[_Cell]],
    *,
    start_row: int,
    content_end_row: int,
    series_coordinate: str,
) -> tuple[
    dict[str, str],
    list[RawSpecification],
    str | None,
    str | None,
    str | None,
    list[str],
    set[str],
]:
    specifications: dict[str, str] = {}
    raw_specifications: list[RawSpecification] = []
    warnings: list[str] = []
    consumed: set[str] = {series_coordinate}
    model: str | None = None
    model_raw: str | None = None
    cup: str | None = None
    for row_number in range(start_row, content_end_row + 1):
        row_cells = [
            cell
            for cell in rows.get(row_number, [])
            if cell.coordinate not in consumed
            and cell.column_index <= _SPEC_MAX_COLUMN_INDEX
            and not _is_photo_label(cell.value)
        ]
        index = 0
        while index + 1 < len(row_cells):
            field_cell = row_cells[index]
            value_cell = row_cells[index + 1]
            known_key = _specification_key(field_cell.value)
            value_is_field = _specification_key(value_cell.value) is not None
            if not value_is_field and (
                known_key is not None
                or _looks_like_specification_pair(field_cell, value_cell)
            ):
                key, parsed_value = _add_specification(
                    field_cell,
                    value_cell,
                    specifications,
                    raw_specifications,
                    warnings,
                )
                consumed.update((field_cell.coordinate, value_cell.coordinate))
                if key == "model":
                    model = parsed_value.strip() or None
                    model_raw = parsed_value
                elif key == "cup":
                    cup = parsed_value.strip() or None
                index += 2
                continue
            index += 1
    return (
        specifications,
        raw_specifications,
        model,
        model_raw,
        cup,
        warnings,
        consumed,
    )


def _price_from_cells(
    label_cell: _Cell, row_cells: list[_Cell], context: str
) -> tuple[ParsedPrice | None, _Cell | None]:
    if _PRICE_PATTERN.search(label_cell.raw_value):
        return parse_price(label_cell.raw_value, context=context), label_cell
    right_candidates = [
        candidate
        for candidate in row_cells
        if candidate.column_index > label_cell.column_index
    ]
    for candidate in right_candidates:
        if _PRICE_PATTERN.search(candidate.raw_value):
            return parse_price(candidate.raw_value, context=context), candidate
    return None, right_candidates[0] if right_candidates else None


def _pending_coordinates(pending: _PendingPrice) -> set[str]:
    coordinates = {pending.label_cell.coordinate}
    if pending.price_cell is not None:
        coordinates.add(pending.price_cell.coordinate)
    return coordinates


def _raw_pending_entry(
    pending: _PendingPrice,
    fallback_cell: _Cell | None = None,
) -> RawCommercialEntry:
    value_cell = pending.price_cell or fallback_cell
    if value_cell is None:
        return RawCommercialEntry(
            field=None,
            value=pending.label_cell.value,
            coordinate=pending.label_cell.coordinate,
        )
    return RawCommercialEntry(
        field=pending.label_cell.value,
        value=value_cell.value,
        coordinate=value_cell.coordinate,
    )


def _normalize_prices(
    rows: Mapping[int, list[_Cell]],
    *,
    start_row: int,
    content_end_row: int,
    consumed_spec_coordinates: set[str],
) -> tuple[
    dict[str, ParsedPrice | None],
    set[str],
    list[RawCommercialEntry],
    list[str],
]:
    """Normalize known prices with one-row value/context look-ahead."""
    price_values: dict[str, ParsedPrice | None] = {
        "fob_unit_price": None,
        "minimum_retail_price": None,
        "normal_options_price": None,
        "body_only_price": None,
        "including_head_price": None,
    }
    consumed: set[str] = set()
    raw_entries: list[RawCommercialEntry] = []
    warnings: list[str] = []
    pending: _PendingPrice | None = None

    def preserve_pending(
        item: _PendingPrice,
        *,
        fallback_cell: _Cell | None = None,
        duplicate: bool = False,
    ) -> None:
        raw_entries.append(_raw_pending_entry(item, fallback_cell))
        consumed.update(_pending_coordinates(item))
        if fallback_cell is not None:
            consumed.add(fallback_cell.coordinate)
        warning = (
            "Duplicate price preserved as raw commercial entry"
            if duplicate
            else "Ambiguous price preserved as raw commercial entry"
        )
        warnings.append(warning)

    def store_pending(item: _PendingPrice, target_key: str) -> None:
        if item.parsed_price is None:
            preserve_pending(item)
            return
        if price_values[target_key] is not None:
            preserve_pending(item, duplicate=True)
            return
        price_values[target_key] = ParsedPrice(
            raw_value=item.parsed_price.raw_value,
            currency=item.parsed_price.currency,
            amount=item.parsed_price.amount,
            context=target_key,
        )
        consumed.update(_pending_coordinates(item))

    for row_number in range(start_row, content_end_row + 1):
        row_cells = [
            cell
            for cell in rows.get(row_number, [])
            if cell.coordinate not in consumed_spec_coordinates
            and cell.coordinate not in consumed
        ]

        if pending is not None:
            context_cell = next(
                (
                    cell
                    for cell in row_cells
                    if _price_context_key(cell.value) is not None
                ),
                None,
            )
            if pending.parsed_price is not None:
                if pending.key == "fob_unit_price" and context_cell is not None:
                    target_key = _price_context_key(context_cell.value)
                    assert target_key is not None
                    store_pending(pending, target_key)
                    consumed.add(context_cell.coordinate)
                else:
                    store_pending(pending, pending.key)
                pending = None
                row_cells = [
                    cell
                    for cell in row_cells
                    if cell.coordinate not in consumed
                ]
            elif pending.price_cell is not None:
                preserve_pending(pending)
                pending = None
                row_cells = [
                    cell
                    for cell in row_cells
                    if cell.coordinate not in consumed
                ]
            else:
                value_cell = next(
                    (
                        cell
                        for cell in row_cells
                        if _PRICE_PATTERN.search(cell.raw_value)
                        and _commercial_price_key(cell.value) is None
                    ),
                    None,
                )
                if value_cell is not None:
                    parsed = parse_price(value_cell.raw_value, context=pending.key)
                    pending = _PendingPrice(
                        key=pending.key,
                        label_cell=pending.label_cell,
                        parsed_price=parsed,
                        price_cell=value_cell,
                    )
                    if pending.key == "fob_unit_price" and context_cell is not None:
                        target_key = _price_context_key(context_cell.value)
                        assert target_key is not None
                        store_pending(pending, target_key)
                        consumed.add(context_cell.coordinate)
                        pending = None
                    elif pending.key != "fob_unit_price":
                        store_pending(pending, pending.key)
                        pending = None
                    row_cells = [
                        cell
                        for cell in row_cells
                        if cell.coordinate not in consumed
                    ]
                else:
                    fallback_cell = next(
                        (
                            cell
                            for cell in row_cells
                            if _commercial_price_key(cell.value) is None
                        ),
                        None,
                    )
                    preserve_pending(pending, fallback_cell=fallback_cell)
                    pending = None
                    row_cells = [
                        cell
                        for cell in row_cells
                        if cell.coordinate not in consumed
                    ]

        for label_cell in row_cells:
            if label_cell.coordinate in consumed:
                continue
            price_key = _commercial_price_key(label_cell.value)
            if price_key is None:
                continue
            if pending is not None:
                store_pending(pending, pending.key)
            parsed_price, price_cell = _price_from_cells(
                label_cell, row_cells, price_key
            )
            candidate = _PendingPrice(
                key=price_key,
                label_cell=label_cell,
                parsed_price=parsed_price,
                price_cell=price_cell,
            )
            if parsed_price is None or price_key == "fob_unit_price":
                pending = candidate
            else:
                store_pending(candidate, price_key)
                pending = None

    if pending is not None:
        store_pending(pending, pending.key)
    return price_values, consumed, raw_entries, warnings


def _upgrade_from_row(cells: list[_Cell]) -> tuple[UpgradeOption | None, set[str]]:
    if not cells:
        return None, set()
    price_cell = next(
        (cell for cell in cells if _PRICE_PATTERN.search(cell.raw_value)), None
    )
    consumed = {cell.coordinate for cell in cells}
    if price_cell is not None:
        price = parse_price(price_cell.raw_value, context="upgrade_option")
        name_parts = [
            _PRICE_PATTERN.sub("", cell.value).strip(" ()+-")
            for cell in cells
            if cell is not price_cell or _PRICE_PATTERN.sub("", cell.value).strip(" ()+-")
        ]
        name = " ".join(part for part in name_parts if part).strip()
        if not name:
            name = "Unlabeled upgrade"
        raw_value = " | ".join(cell.value for cell in cells)
        return UpgradeOption(name=name, raw_value=raw_value, price=price), consumed
    raw_value = " | ".join(cell.value for cell in cells)
    return UpgradeOption(name=raw_value, raw_value=raw_value), consumed


def _parse_commercial(
    rows: Mapping[int, list[_Cell]],
    *,
    start_row: int,
    content_end_row: int,
    consumed_spec_coordinates: set[str],
) -> tuple[
    list[str],
    list[UpgradeOption],
    list[str],
    Pricing,
    list[RawCommercialEntry],
    list[str],
]:
    included_features: list[str] = []
    upgrade_options: list[UpgradeOption] = []
    notices: list[str] = []
    (
        price_values,
        consumed_price_coordinates,
        raw_entries,
        warnings,
    ) = _normalize_prices(
        rows,
        start_row=start_row,
        content_end_row=content_end_row,
        consumed_spec_coordinates=consumed_spec_coordinates,
    )
    mode: str | None = None
    consumed = set(consumed_spec_coordinates) | consumed_price_coordinates
    for row_number in range(start_row, content_end_row + 1):
        unconsumed_row_cells = [
            cell
            for cell in rows.get(row_number, [])
            if cell.coordinate not in consumed
        ]
        semantic_trigger = any(
            _is_includes_header(cell.value)
            or _is_upgrade_header(cell.value)
            or _is_photo_label(cell.value)
            or _is_notice(cell.value)
            or _commercial_price_key(cell.value) is not None
            for cell in unconsumed_row_cells
        )
        row_cells = (
            unconsumed_row_cells
            if mode is not None or semantic_trigger
            else [
                cell
                for cell in unconsumed_row_cells
                if cell.column_index >= _COMMERCIAL_MIN_COLUMN_INDEX
            ]
        )
        if not row_cells:
            continue

        for cell in row_cells:
            if cell.coordinate in consumed:
                continue
            if _is_includes_header(cell.value):
                mode = "included"
                consumed.add(cell.coordinate)
                continue
            if _is_upgrade_header(cell.value):
                mode = "upgrade"
                consumed.add(cell.coordinate)
                continue
            if _is_photo_label(cell.value):
                mode = None
                consumed.add(cell.coordinate)
                continue
            if _is_notice(cell.value):
                notices.append(cell.value)
                consumed.add(cell.coordinate)
                continue
        remaining = [cell for cell in row_cells if cell.coordinate not in consumed]
        if not remaining:
            continue
        if mode == "included":
            for cell in remaining:
                included_features.append(cell.value)
                consumed.add(cell.coordinate)
            continue
        if mode == "upgrade":
            option, option_consumed = _upgrade_from_row(remaining)
            if option is not None:
                upgrade_options.append(option)
                consumed.update(option_consumed)
            continue
        for cell in remaining:
            raw_entries.append(
                RawCommercialEntry(
                    field=None,
                    value=cell.value,
                    coordinate=cell.coordinate,
                )
            )
            consumed.add(cell.coordinate)

    return (
        included_features,
        upgrade_options,
        notices,
        Pricing(**price_values),
        raw_entries,
        warnings,
    )


def _photo_link(
    cells: list[_Cell], start_row: int, end_row: int
) -> tuple[str | None, int | None]:
    block_cells = [cell for cell in cells if start_row <= cell.row <= end_row]
    label = next((cell for cell in block_cells if _is_photo_label(cell.value)), None)
    if label is None:
        return None, None
    candidates = [
        cell
        for cell in block_cells
        if cell.row >= label.row and cell.coordinate != label.coordinate
    ]
    if any(_contains_url(cell.raw_value) for cell in candidates):
        return "[URL_REDACTED]", label.row
    return None, label.row


class CLMPriceListParser:
    """Parse a sheet-layout report into traceable CLM product blocks."""

    def parse(self, layout: Mapping[str, object]) -> list[CLMProductBlock]:
        if not isinstance(layout, Mapping):
            raise CLMPriceParserError("Layout must be a mapping")
        cells = _parse_cells(layout)
        rows: dict[int, list[_Cell]] = defaultdict(list)
        for cell in cells:
            rows[cell.row].append(cell)

        starts: list[tuple[int, _Cell, str]] = []
        for cell in cells:
            series = recognize_series_title(cell.raw_value)
            if series is not None:
                starts.append((cell.row, cell, series))
        starts.sort(key=lambda item: (item[0], item[1].column_index))
        deduplicated: list[tuple[int, _Cell, str]] = []
        seen_rows: set[int] = set()
        for start in starts:
            if start[0] not in seen_rows:
                deduplicated.append(start)
                seen_rows.add(start[0])
        if not deduplicated:
            return []

        maximum_row = max(cell.row for cell in cells)
        products: list[CLMProductBlock] = []
        for index, (start_row, series_cell, series) in enumerate(deduplicated):
            hard_end_row = (
                deduplicated[index + 1][0] - 1
                if index + 1 < len(deduplicated)
                else maximum_row
            )
            photo_download_link, photo_label_row = _photo_link(
                cells, start_row, hard_end_row
            )
            content_end_row = (
                photo_label_row - 1
                if photo_label_row is not None and photo_label_row > start_row
                else hard_end_row
            )
            (
                specifications,
                raw_specifications,
                model,
                model_raw,
                cup,
                spec_warnings,
                consumed_spec_coordinates,
            ) = _parse_specifications(
                rows,
                start_row=start_row,
                content_end_row=content_end_row,
                series_coordinate=series_cell.coordinate,
            )
            (
                included_features,
                upgrade_options,
                notices,
                pricing,
                raw_commercial_entries,
                commercial_warnings,
            ) = _parse_commercial(
                rows,
                start_row=start_row,
                content_end_row=content_end_row,
                consumed_spec_coordinates=consumed_spec_coordinates,
            )
            warnings = [*spec_warnings, *commercial_warnings]
            if photo_label_row is not None and photo_download_link is None:
                warnings.append("Photo download link label found without a safe URL")
            products.append(
                CLMProductBlock(
                    series=series,
                    raw_series_title=series_cell.value,
                    model=model,
                    model_raw=model_raw,
                    cup=cup,
                    specifications=specifications,
                    raw_specifications=raw_specifications,
                    included_features=included_features,
                    upgrade_options=upgrade_options,
                    notices=notices,
                    pricing=pricing,
                    photo_download_link=photo_download_link,
                    raw_commercial_entries=raw_commercial_entries,
                    source=BlockSource(start_row=start_row, end_row=hard_end_row),
                    warnings=warnings,
                )
            )
        return products


def parse_clm_price_layout(
    layout: Mapping[str, object],
) -> list[CLMProductBlock]:
    return CLMPriceListParser().parse(layout)
