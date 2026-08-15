"""Pure-local Size List Parser V1 for sheet-layout snapshots."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .sheet_layout import column_index_to_label, column_label_to_index


_CORE_HEADERS = frozenset({"type", "body_type", "fob_price"})
_MEASUREMENT_FIELDS = (
    "upper_chest",
    "lower_chest",
    "waist",
    "hip",
    "shoulder",
    "leg_length",
    "thigh",
    "arm_length",
    "sole",
    "net_weight",
    "oral",
    "vagina",
    "anus",
)
_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_RANGE_PATTERN = re.compile(
    r"^([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)$"
)
_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s\"'<>]+")
_NUMBER_PATTERN = r"[0-9]+(?:\.[0-9]+)?"
_UNIT_COMPONENT_PATTERN = re.compile(
    rf"(?i)^(?P<value>{_NUMBER_PATTERN})\s*"
    r"(?P<unit>cm|in|kg|lb|lbs)$"
)
_TWO_DIMENSIONAL_COMPONENT_PATTERN = re.compile(
    rf"(?i)^(?P<length>{_NUMBER_PATTERN})\s*[*x×]\s*"
    rf"(?P<width>{_NUMBER_PATTERN})\s*(?P<unit>cm|in)$"
)
_UNITLESS_COMPONENT_PATTERN = re.compile(rf"^{_NUMBER_PATTERN}$")
_DIMENSION_SEPARATOR_PATTERN = re.compile(r"[*x×]", re.IGNORECASE)
_FOB_PATTERN = re.compile(
    r"(?i)^\s*(?P<currency>￥|¥|RMB)\s*"
    r"(?P<amount>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*$"
)


def _header_form(value: str) -> str:
    normalized = value.casefold().replace("/", " ")
    normalized = re.sub(r"[()（）\[\]]", " ", normalized)
    normalized = re.sub(r"[.：:]", " ", normalized)
    return " ".join(normalized.split())


def _header_candidates(value: str) -> tuple[str, ...]:
    """Return strict canonical-first representations of one header cell."""
    normalized_line_endings = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized_line_endings.split("\n")]
    non_empty_lines = [line for line in lines if line]
    if not non_empty_lines:
        return ()
    candidates = [_header_form(non_empty_lines[0])]
    combined = _header_form(" ".join(non_empty_lines))
    if combined not in candidates:
        candidates.append(combined)
    return tuple(candidates)


def _aliases(*values: str) -> frozenset[str]:
    return frozenset(_header_form(value) for value in values)


_HEADER_ALIASES: dict[str, frozenset[str]] = {
    "type": _aliases("Type", "类型", "Type / 类型", "Type\n类型"),
    "body_type": _aliases(
        "Body type", "身型", "Body type / 身型", "Body type\n身型"
    ),
    "fob_price": _aliases(
        "FOB Price", "出厂价格", "FOB Price / 出厂价格", "FOB Price\n出厂价格"
    ),
    "upper_chest": _aliases(
        "Upper Chest", "上胸围", "Upper Chest / 上胸围", "Upper Chest\n上胸围"
    ),
    "lower_chest": _aliases(
        "Lower Chest", "下胸围", "Lower Chest / 下胸围", "Lower Chest\n下胸围"
    ),
    "waist": _aliases("Waist", "腰围", "Waist / 腰围", "Waist\n腰围"),
    "hip": _aliases("Hip", "臀围", "Hip / 臀围", "Hip\n臀围"),
    "shoulder": _aliases(
        "Shoulder", "肩宽", "Shoulder / 肩宽", "Shoulder\n肩宽"
    ),
    "leg_length": _aliases(
        "Leg Length", "小腿长度", "Leg Length / 小腿长度", "Leg Length\n小腿长度"
    ),
    "thigh": _aliases("Thigh", "大腿长度", "Thigh / 大腿长度", "Thigh\n大腿长度"),
    "arm_length": _aliases(
        "Arm Length", "手臂长", "Arm Length / 手臂长", "Arm Length\n手臂长"
    ),
    "sole": _aliases("Sole", "脚板长度", "Sole / 脚板长度", "Sole\n脚板长度"),
    "net_weight": _aliases(
        "N.W.", "N.W", "N W", "净重", "N.W. / 净重", "N.W.\n净重"
    ),
    "oral": _aliases("Oral", "口腔深度", "Oral / 口腔深度", "Oral\n口腔深度"),
    "vagina": _aliases(
        "Vagina", "阴部深度", "Vagina / 阴部深度", "Vagina\n阴部深度"
    ),
    "anus": _aliases("Anus", "肛门深度", "Anus / 肛门深度", "Anus\n肛门深度"),
}


class SizeListParserError(ValueError):
    """Safe structural error with no supplier cell content."""


@dataclass(frozen=True, slots=True)
class UnitValue:
    value: int | float
    unit: str


@dataclass(frozen=True, slots=True)
class TwoDimensionalValue:
    length: int | float
    width: int | float
    unit: str


@dataclass(frozen=True, slots=True)
class NormalizedMeasurement:
    metric: UnitValue | TwoDimensionalValue | None
    imperial: UnitValue | TwoDimensionalValue | None
    raw_value: str


@dataclass(frozen=True, slots=True)
class SizeIdentity:
    body_type: str
    raw_body_type: str
    normalized_body_type: str
    comparison_key: str


@dataclass(frozen=True, slots=True)
class SizeClassification:
    type: str | None
    raw_type: str | None


@dataclass(frozen=True, slots=True)
class SupplierFOBCost:
    amount: int | float | None
    currency: str | None
    raw_value: str


@dataclass(frozen=True, slots=True)
class SizeSupplierCosts:
    fob_price: SupplierFOBCost | None


@dataclass(frozen=True, slots=True)
class SizeMeasurements:
    upper_chest: NormalizedMeasurement | None = None
    lower_chest: NormalizedMeasurement | None = None
    waist: NormalizedMeasurement | None = None
    hip: NormalizedMeasurement | None = None
    shoulder: NormalizedMeasurement | None = None
    leg_length: NormalizedMeasurement | None = None
    thigh: NormalizedMeasurement | None = None
    arm_length: NormalizedMeasurement | None = None
    sole: NormalizedMeasurement | None = None
    net_weight: NormalizedMeasurement | None = None
    oral: NormalizedMeasurement | None = None
    vagina: NormalizedMeasurement | None = None
    anus: NormalizedMeasurement | None = None


@dataclass(frozen=True, slots=True)
class RawMeasurement:
    fields: tuple[str, ...]
    raw_header: str
    raw_value: str
    coordinate: str
    merged_range: str | None


@dataclass(frozen=True, slots=True)
class SizeSource:
    row: int
    coordinates: dict[str, str]
    type_merged_range: str | None


@dataclass(frozen=True, slots=True)
class SizeRecord:
    identity: SizeIdentity
    classification: SizeClassification
    supplier_costs: SizeSupplierCosts
    measurements: SizeMeasurements
    raw_measurements: tuple[RawMeasurement, ...]
    source: SizeSource
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Cell:
    row: int
    column_index: int
    column: str
    coordinate: str
    value: str
    merged_range: str | None


@dataclass(frozen=True, slots=True)
class _Merge:
    range: str
    start_row: int
    end_row: int
    start_column_index: int
    end_column_index: int
    anchor: str

    def contains(self, row: int, column_index: int) -> bool:
        return (
            self.start_row <= row <= self.end_row
            and self.start_column_index <= column_index <= self.end_column_index
        )


@dataclass(frozen=True, slots=True)
class _HeaderSchema:
    row: int
    columns: dict[str, int]
    raw_headers: dict[int, str]
    unknown_columns: dict[int, str]


def _safe_raw_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    without_urls = _URL_PATTERN.sub("[URL_REDACTED]", value)
    without_secrets = REPORT_SECRET_SCAN_PATTERN.sub(
        "[REDACTED_SECRET]", without_urls
    )
    return Redactor().text(without_secrets, limit=4000)


def _cell_position(raw_cell: Mapping[str, object]) -> tuple[int, int, str, str]:
    raw_coordinate = raw_cell.get("coordinate")
    coordinate = (
        raw_coordinate.upper().strip()
        if isinstance(raw_coordinate, str)
        else ""
    )
    match = _COORDINATE_PATTERN.fullmatch(coordinate)
    raw_row = raw_cell.get("row")
    row = (
        raw_row
        if isinstance(raw_row, int)
        and not isinstance(raw_row, bool)
        and raw_row > 0
        else int(match.group(2)) if match is not None else 0
    )
    raw_column_index = raw_cell.get("column_index")
    column_index = (
        raw_column_index
        if isinstance(raw_column_index, int)
        and not isinstance(raw_column_index, bool)
        and raw_column_index > 0
        else column_label_to_index(match.group(1)) if match is not None else 0
    )
    raw_column = raw_cell.get("column")
    column = (
        raw_column.upper().strip()
        if isinstance(raw_column, str) and raw_column.strip()
        else column_index_to_label(column_index) if column_index > 0 else ""
    )
    if not coordinate and row > 0 and column:
        coordinate = f"{column}{row}"
    if row <= 0 or column_index <= 0 or not coordinate:
        raise SizeListParserError("Layout cell position was invalid")
    return row, column_index, column, coordinate


def _parse_cells(layout: Mapping[str, object]) -> list[_Cell]:
    raw_cells = layout.get("non_empty_cells")
    if not isinstance(raw_cells, list):
        raise SizeListParserError("Layout must contain non_empty_cells")
    cells: list[_Cell] = []
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, Mapping):
            continue
        value = _safe_raw_text(raw_cell.get("formatted_value"))
        if not value or not value.strip():
            continue
        row, column_index, column, coordinate = _cell_position(raw_cell)
        raw_merged_range = raw_cell.get("merged_range")
        merged_range = (
            raw_merged_range.upper().strip()
            if isinstance(raw_merged_range, str)
            else None
        )
        cells.append(
            _Cell(
                row=row,
                column_index=column_index,
                column=column,
                coordinate=coordinate,
                value=value,
                merged_range=merged_range,
            )
        )
    return sorted(cells, key=lambda cell: (cell.row, cell.column_index))


def _merge_from_range(raw_range: str) -> _Merge | None:
    match = _RANGE_PATTERN.fullmatch(raw_range.upper().strip())
    if match is None:
        return None
    start_column = column_label_to_index(match.group(1))
    start_row = int(match.group(2))
    end_column = column_label_to_index(match.group(3))
    end_row = int(match.group(4))
    if end_row < start_row or end_column < start_column:
        return None
    return _Merge(
        range=raw_range.upper().strip(),
        start_row=start_row,
        end_row=end_row,
        start_column_index=start_column,
        end_column_index=end_column,
        anchor=f"{column_index_to_label(start_column)}{start_row}",
    )


def _parse_merges(
    layout: Mapping[str, object], cells: list[_Cell]
) -> list[_Merge]:
    merges: dict[str, _Merge] = {}
    raw_merges = layout.get("merged_ranges")
    if isinstance(raw_merges, list):
        for raw_merge in raw_merges:
            if not isinstance(raw_merge, Mapping):
                continue
            raw_range = raw_merge.get("range")
            if isinstance(raw_range, str):
                parsed = _merge_from_range(raw_range)
                if parsed is not None:
                    merges[parsed.range] = parsed
    for cell in cells:
        if cell.merged_range is None:
            continue
        parsed = _merge_from_range(cell.merged_range)
        if parsed is not None:
            merges[parsed.range] = parsed
    return sorted(
        merges.values(),
        key=lambda merge: (
            merge.start_row,
            merge.start_column_index,
            merge.end_row,
            merge.end_column_index,
        ),
    )


def _header_field(value: str) -> str | None:
    for candidate in _header_candidates(value):
        for field_name, aliases in _HEADER_ALIASES.items():
            if candidate in aliases:
                return field_name
    return None


def _find_header(rows: Mapping[int, list[_Cell]]) -> _HeaderSchema:
    for row_number in sorted(rows):
        columns: dict[str, int] = {}
        raw_headers: dict[int, str] = {}
        unknown_columns: dict[int, str] = {}
        duplicate = False
        for cell in rows[row_number]:
            raw_headers[cell.column_index] = cell.value
            field_name = _header_field(cell.value)
            if field_name is None:
                unknown_columns[cell.column_index] = cell.value
            elif field_name in columns:
                duplicate = True
            else:
                columns[field_name] = cell.column_index
        if _CORE_HEADERS.issubset(columns):
            if duplicate:
                raise SizeListParserError("Size List header contained duplicates")
            return _HeaderSchema(
                row=row_number,
                columns=columns,
                raw_headers=raw_headers,
                unknown_columns=unknown_columns,
            )
    raise SizeListParserError("Required Size List headers were not found")


def _normalized_number(value: str) -> int | float:
    number = Decimal(value)
    return int(number) if number == number.to_integral() else float(number)


def _parse_fob(raw_value: str) -> SupplierFOBCost:
    match = _FOB_PATTERN.fullmatch(raw_value)
    if match is None:
        currency = "RMB" if re.search(r"(?i)￥|¥|RMB", raw_value) else None
        return SupplierFOBCost(
            amount=None,
            currency=currency,
            raw_value=raw_value,
        )
    try:
        amount = _normalized_number(match.group("amount").replace(",", ""))
    except InvalidOperation:
        amount = None
    return SupplierFOBCost(amount=amount, currency="RMB", raw_value=raw_value)


def _parse_measurement(
    field_name: str, raw_value: str
) -> tuple[NormalizedMeasurement | None, str | None]:
    if raw_value.strip() == "/":
        return None, None

    normalized_line_endings = raw_value.replace("\r\n", "\n").replace("\r", "\n")
    components: list[str] = []
    for line in normalized_line_endings.split("\n"):
        candidate = line.strip()
        if not candidate:
            continue
        paired = re.fullmatch(r"([^()]+?)\s*\(([^()]*)\)", candidate)
        if paired is not None:
            components.extend(part.strip() for part in paired.groups() if part.strip())
            continue
        if (
            len(candidate) >= 2
            and candidate[0] in "(（"
            and candidate[-1] in ")）"
        ):
            candidate = candidate[1:-1].strip()
        if candidate:
            components.append(candidate)

    if not components:
        return None, f"malformed measurement: {field_name}"
    if len(components) == 1 and _UNITLESS_COMPONENT_PATTERN.fullmatch(components[0]):
        return None, f"unitless measurement preserved: {field_name}"

    metric: UnitValue | TwoDimensionalValue | None = None
    imperial: UnitValue | TwoDimensionalValue | None = None
    expected_metric = "kg" if field_name == "net_weight" else "cm"
    expected_imperial = "lb" if field_name == "net_weight" else "in"
    invalid_components: list[str] = []
    saw_two_dimensional = any(
        _DIMENSION_SEPARATOR_PATTERN.search(component) for component in components
    )

    for component in components:
        dimensional_match = (
            _TWO_DIMENSIONAL_COMPONENT_PATTERN.fullmatch(component)
            if field_name == "sole"
            else None
        )
        scalar_match = _UNIT_COMPONENT_PATTERN.fullmatch(component)
        if dimensional_match is not None:
            unit = dimensional_match.group("unit").casefold()
            parsed_value: UnitValue | TwoDimensionalValue = TwoDimensionalValue(
                length=_normalized_number(dimensional_match.group("length")),
                width=_normalized_number(dimensional_match.group("width")),
                unit=unit,
            )
        elif scalar_match is not None:
            unit = scalar_match.group("unit").casefold()
            parsed_value = UnitValue(
                value=_normalized_number(scalar_match.group("value")),
                unit="lb" if unit == "lbs" else unit,
            )
        else:
            invalid_components.append(component)
            continue

        if unit == "lbs":
            unit = "lb"
        if unit == expected_metric and metric is None:
            metric = parsed_value
        elif unit == expected_imperial and imperial is None:
            imperial = parsed_value
        else:
            invalid_components.append(component)

    if metric is None and imperial is None:
        if field_name == "sole" and saw_two_dimensional:
            return None, f"malformed two-dimensional measurement: {field_name}"
        return None, f"malformed measurement: {field_name}"

    warning: str | None = None
    if invalid_components:
        if field_name == "sole" and saw_two_dimensional:
            warning = f"malformed two-dimensional measurement: {field_name}"
        elif metric is not None and imperial is None:
            warning = f"malformed imperial component: {field_name}"
        elif imperial is not None and metric is None:
            warning = f"malformed metric component: {field_name}"
        else:
            warning = f"malformed measurement: {field_name}"

    return (
        NormalizedMeasurement(
            metric=metric,
            imperial=imperial,
            raw_value=raw_value,
        ),
        warning,
    )


def parse_measurement_value(
    field_name: str,
    raw_value: str,
) -> tuple[NormalizedMeasurement | None, str | None]:
    """Parse one known measurement using the Size Parser's strict rules."""
    if field_name not in _MEASUREMENT_FIELDS:
        raise SizeListParserError("Unsupported Size List measurement field")
    if not isinstance(raw_value, str):
        raise SizeListParserError("Measurement value must be text")
    return _parse_measurement(field_name, raw_value)


def _normalize_identity(raw_body_type: str) -> tuple[str, str]:
    normalized = " ".join(raw_body_type.split())
    return normalized, normalized.casefold()


class SizeListParser:
    """Parse rows from an in-memory Size List sheet-layout snapshot."""

    def parse(self, layout: Mapping[str, object]) -> list[SizeRecord]:
        if not isinstance(layout, Mapping):
            raise SizeListParserError("Layout must be a mapping")
        cells = _parse_cells(layout)
        rows: dict[int, list[_Cell]] = defaultdict(list)
        positions: dict[tuple[int, int], _Cell] = {}
        by_coordinate: dict[str, _Cell] = {}
        for cell in cells:
            rows[cell.row].append(cell)
            positions.setdefault((cell.row, cell.column_index), cell)
            by_coordinate.setdefault(cell.coordinate, cell)
        header = _find_header(rows)
        merges = _parse_merges(layout, cells)
        type_column = header.columns["type"]
        body_column = header.columns["body_type"]
        fob_column = header.columns["fob_price"]

        records: list[SizeRecord] = []
        for row_number in sorted(row for row in rows if row > header.row):
            body_cell = positions.get((row_number, body_column))
            if body_cell is None or not body_cell.value.strip():
                continue
            warnings: list[str] = []
            coordinates: dict[str, str] = {"body_type": body_cell.coordinate}

            type_cell = positions.get((row_number, type_column))
            type_merge: _Merge | None = None
            if type_cell is not None:
                type_merge = next(
                    (
                        merge
                        for merge in merges
                        if merge.contains(row_number, type_column)
                        and merge.start_column_index == type_column
                        and merge.end_column_index == type_column
                    ),
                    None,
                )
            else:
                type_merge = next(
                    (
                        merge
                        for merge in merges
                        if merge.contains(row_number, type_column)
                        and merge.start_column_index == type_column
                        and merge.end_column_index == type_column
                    ),
                    None,
                )
                if type_merge is not None:
                    type_cell = by_coordinate.get(type_merge.anchor)

            raw_type = type_cell.value if type_cell is not None else None
            normalized_type = (
                " ".join(raw_type.split()) if raw_type is not None else None
            )
            if type_cell is not None:
                coordinates["type"] = type_cell.coordinate
            else:
                warnings.append("missing explicit type classification")

            fob_cell = positions.get((row_number, fob_column))
            fob_price = _parse_fob(fob_cell.value) if fob_cell is not None else None
            if fob_cell is not None:
                coordinates["fob_price"] = fob_cell.coordinate
                if fob_price is not None and fob_price.amount is None:
                    warnings.append("unable to parse FOB price")

            normalized_measurements: dict[
                str, NormalizedMeasurement | None
            ] = {field_name: None for field_name in _MEASUREMENT_FIELDS}
            raw_measurements: list[RawMeasurement] = []
            ambiguous_fields: set[str] = set()

            upper_column = header.columns.get("upper_chest")
            lower_column = header.columns.get("lower_chest")
            if upper_column is not None and lower_column is not None:
                ambiguous_merge = next(
                    (
                        merge
                        for merge in merges
                        if merge.start_row == row_number
                        and merge.end_row == row_number
                        and merge.start_column_index
                        == min(upper_column, lower_column)
                        and merge.end_column_index
                        == max(upper_column, lower_column)
                    ),
                    None,
                )
                if ambiguous_merge is not None:
                    anchor_cell = by_coordinate.get(ambiguous_merge.anchor)
                    if anchor_cell is not None:
                        fields = ("upper_chest", "lower_chest")
                        raw_measurements.append(
                            RawMeasurement(
                                fields=fields,
                                raw_header="Upper Chest / Lower Chest",
                                raw_value=anchor_cell.value,
                                coordinate=anchor_cell.coordinate,
                                merged_range=ambiguous_merge.range,
                            )
                        )
                        coordinates["upper_chest_lower_chest"] = (
                            anchor_cell.coordinate
                        )
                        ambiguous_fields.update(fields)
                        warnings.append("ambiguous merged measurement D:E")

            for field_name in _MEASUREMENT_FIELDS:
                if field_name in ambiguous_fields:
                    continue
                column_index = header.columns.get(field_name)
                if column_index is None:
                    continue
                measurement_cell = positions.get((row_number, column_index))
                if measurement_cell is None:
                    continue
                raw_measurements.append(
                    RawMeasurement(
                        fields=(field_name,),
                        raw_header=header.raw_headers[column_index],
                        raw_value=measurement_cell.value,
                        coordinate=measurement_cell.coordinate,
                        merged_range=measurement_cell.merged_range,
                    )
                )
                coordinates[field_name] = measurement_cell.coordinate
                normalized, warning = parse_measurement_value(
                    field_name, measurement_cell.value
                )
                normalized_measurements[field_name] = normalized
                if warning is not None:
                    warnings.append(warning)

            for column_index, raw_header in sorted(header.unknown_columns.items()):
                unknown_cell = positions.get((row_number, column_index))
                if unknown_cell is None:
                    continue
                unknown_key = re.sub(
                    r"[^a-z0-9]+",
                    "_",
                    _header_form(raw_header),
                ).strip("_") or f"column_{column_index}"
                raw_measurements.append(
                    RawMeasurement(
                        fields=(f"unknown:{unknown_key}",),
                        raw_header=raw_header,
                        raw_value=unknown_cell.value,
                        coordinate=unknown_cell.coordinate,
                        merged_range=unknown_cell.merged_range,
                    )
                )
                coordinates[f"unknown:{unknown_key}"] = unknown_cell.coordinate
                warnings.append(f"unknown measurement header: {unknown_key}")

            normalized_body_type, comparison_key = _normalize_identity(
                body_cell.value
            )
            records.append(
                SizeRecord(
                    identity=SizeIdentity(
                        body_type=normalized_body_type,
                        raw_body_type=body_cell.value,
                        normalized_body_type=normalized_body_type,
                        comparison_key=comparison_key,
                    ),
                    classification=SizeClassification(
                        type=normalized_type,
                        raw_type=raw_type,
                    ),
                    supplier_costs=SizeSupplierCosts(fob_price=fob_price),
                    measurements=SizeMeasurements(**normalized_measurements),
                    raw_measurements=tuple(raw_measurements),
                    source=SizeSource(
                        row=row_number,
                        coordinates=coordinates,
                        type_merged_range=(
                            type_merge.range if type_merge is not None else None
                        ),
                    ),
                    warnings=tuple(warnings),
                )
            )
        return records


def parse_size_list(layout: Mapping[str, object]) -> list[SizeRecord]:
    return SizeListParser().parse(layout)
