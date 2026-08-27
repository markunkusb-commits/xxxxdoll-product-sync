"""Exact-cell, read-only Google Sheets access for mapped media references."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from .config import GoogleSettings
from .google_api import (
    GoogleClients,
    GoogleSheetsReadonlyClientFactory,
    ReadOnlySheetsGateway,
)
from .image_mapping import (
    PHOTO_DOWNLOAD_LINK_MARKER,
    ProductSourceRange,
    SupplierMediaSourceReference,
    create_supplier_media_source_reference,
)
from .sheet_layout import validate_sheet_title


MAX_MEDIA_REFERENCE_COORDINATES = 100
_SINGLE_CELL_A1_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_UNSAFE_MAPPING_WARNINGS = frozenset(
    {
        "ambiguous_media_source",
        "unmatched_media_source",
        "missing_reference",
        "cross_product_media_reference_pair",
        "unsupported_media_marker",
        "invalid_reference",
    }
)

ReferenceReadStatus = Literal[
    "read",
    "media_reference_response_missing",
    "media_reference_cell_missing",
    "empty_media_reference",
    "invalid_media_reference_cell",
    "ambiguous_media_hyperlink",
    "ambiguous_media_smart_chip",
    "dynamic_hyperlink_formula_unsupported",
    "unsupported_hyperlink_formula",
    "media_reference_link_missing",
]
FormulaFunction = Literal["HYPERLINK", "IMAGE", "OTHER", "NONE"]
ReferenceVerification = Literal[
    "verified_unchanged",
    "reference_changed_since_mapping",
    "mapping_reference_redacted",
    "verification_unavailable",
    "not_read",
]


class SecureMediaReferenceInputError(ValueError):
    """Safe mapping/sheet validation error without supplier values."""


class SecureMediaReferenceResponseError(RuntimeError):
    """Safe malformed batch response error without supplier values."""


@dataclass(frozen=True, slots=True)
class ValidatedMappedMediaSource:
    product_source: ProductSourceRange
    product_series: str
    product_identity_values: tuple[str, ...] = field(repr=False)
    marker_coordinate: str
    reference_coordinate: str
    mapping_reference_status: str
    mapping_reference_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class SecureMediaReferenceReadResult:
    mapped_source: ValidatedMappedMediaSource
    read_status: ReferenceReadStatus
    reference_verification: ReferenceVerification
    fresh_reference_fingerprint: str | None
    raw_reference: str | None = field(repr=False)
    cell_level_link_present: bool = False
    formula_present: bool = False
    formula_function: FormulaFunction = "NONE"
    formula_is_hyperlink: bool = False
    smart_chip_present: bool = False
    smart_chip_rich_link_count: int = 0
    smart_chip_unique_uri: bool = False
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()

    def to_supplier_reference(self) -> SupplierMediaSourceReference | None:
        if self.read_status != "read" or self.raw_reference is None:
            return None
        mapped = self.mapped_source
        return create_supplier_media_source_reference(
            source_coordinate=mapped.reference_coordinate,
            marker_coordinate=mapped.marker_coordinate,
            marker_text=PHOTO_DOWNLOAD_LINK_MARKER,
            raw_reference=self.raw_reference,
            product_source_candidate=mapped.product_source,
        )


@dataclass(frozen=True, slots=True)
class SecureMediaReferenceReadBatch:
    results: tuple[SecureMediaReferenceReadResult, ...]
    coordinates_requested: int
    read_requests_performed: int
    write_requests_performed: Literal[0] = 0


def _mapping(value: object, error_code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SecureMediaReferenceInputError(error_code)
    return value


def _array(value: object, error_code: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise SecureMediaReferenceInputError(error_code)
    return value


def validate_single_cell_coordinate(value: object) -> str:
    if not isinstance(value, str):
        raise SecureMediaReferenceInputError("invalid_media_reference_coordinate")
    if _SINGLE_CELL_A1_PATTERN.fullmatch(value) is None:
        raise SecureMediaReferenceInputError("invalid_media_reference_coordinate")
    return value


def _coordinate_sort_key(coordinate: str) -> tuple[int, int]:
    matched = _SINGLE_CELL_A1_PATTERN.fullmatch(coordinate)
    if matched is None:  # pragma: no cover - validated caller invariant
        return (0, 0)
    column = 0
    for character in matched.group(1):
        column = column * 26 + ord(character) - ord("A") + 1
    return int(matched.group(2)), column


def _positive_row(value: object, error_code: str) -> int:
    if type(value) is not int or value <= 0:
        raise SecureMediaReferenceInputError(error_code)
    return value


def _safe_text(value: object, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecureMediaReferenceInputError(error_code)
    if "http://" in value.casefold() or "https://" in value.casefold():
        raise SecureMediaReferenceInputError(error_code)
    return value


def _text_values(identity: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("model", "raw_model"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip() and value not in values:
            values.append(value)
    return tuple(values)


def _blocking_values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _array(value, "invalid_mapping_blocking_issues")
    if not all(isinstance(item, str) for item in items):
        raise SecureMediaReferenceInputError("invalid_mapping_blocking_issues")
    return tuple(items)


def _warning_values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _array(value, "invalid_mapping_warnings")
    if not all(isinstance(item, str) for item in items):
        raise SecureMediaReferenceInputError("invalid_mapping_warnings")
    return tuple(items)


def _validated_fingerprint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SecureMediaReferenceInputError(
            "invalid_mapping_reference_fingerprint"
        )
    return value


def validate_mapping_report(
    report: Mapping[str, object],
) -> tuple[ValidatedMappedMediaSource, ...]:
    """Select only approved exact/source-range media entries from the report."""

    if not isinstance(report, Mapping) or report.get("status") != "ok":
        raise SecureMediaReferenceInputError("mapping_report_not_ok")
    raw_products = _array(report.get("results"), "mapping_results_missing")
    selected_by_coordinate: dict[str, ValidatedMappedMediaSource] = {}
    for raw_product in raw_products:
        product = _mapping(raw_product, "invalid_mapping_product_result")
        if _blocking_values(product.get("blocking_issues")):
            continue
        product_source = _mapping(
            product.get("product_source"), "invalid_mapping_product_source"
        )
        start_row = _positive_row(
            product_source.get("start_row"), "invalid_mapping_product_source"
        )
        end_row = _positive_row(
            product_source.get("end_row"), "invalid_mapping_product_source"
        )
        if end_row < start_row:
            raise SecureMediaReferenceInputError("invalid_mapping_product_source")
        identity = _mapping(
            product.get("product_identity"), "invalid_mapping_product_identity"
        )
        series = _safe_text(
            product.get("series") or identity.get("series"),
            "invalid_mapping_product_identity",
        )
        identity_values = _text_values(identity)
        raw_sources = _array(
            product.get("media_sources"), "invalid_mapping_media_sources"
        )
        for raw_source in raw_sources:
            media_source = _mapping(
                raw_source, "invalid_mapping_media_source"
            )
            if media_source.get("match_status") != "exact_source_range_match":
                continue
            if media_source.get("match_method") != "source_range":
                continue
            if media_source.get("ambiguous") is True:
                continue
            if _blocking_values(media_source.get("blocking_issues")):
                continue
            warnings = _warning_values(media_source.get("warnings"))
            if _UNSAFE_MAPPING_WARNINGS.intersection(warnings):
                continue
            reference_coordinate = validate_single_cell_coordinate(
                media_source.get("reference_coordinate")
            )
            marker_coordinate = validate_single_cell_coordinate(
                media_source.get("marker_coordinate")
            )
            reference_status = _safe_text(
                media_source.get("reference_status"),
                "invalid_mapping_reference_status",
            )
            candidate = ValidatedMappedMediaSource(
                product_source=ProductSourceRange(start_row, end_row),
                product_series=series,
                product_identity_values=identity_values,
                marker_coordinate=marker_coordinate,
                reference_coordinate=reference_coordinate,
                mapping_reference_status=reference_status,
                mapping_reference_fingerprint=_validated_fingerprint(
                    media_source.get("reference_fingerprint")
                ),
            )
            existing = selected_by_coordinate.get(reference_coordinate)
            if existing is not None and existing != candidate:
                raise SecureMediaReferenceInputError(
                    "duplicate_media_reference_coordinate_conflict"
                )
            selected_by_coordinate[reference_coordinate] = candidate
    if len(selected_by_coordinate) > MAX_MEDIA_REFERENCE_COORDINATES:
        raise SecureMediaReferenceInputError("too_many_media_references")
    return tuple(
        sorted(
            selected_by_coordinate.values(),
            key=lambda item: (
                item.product_source.start_row,
                _coordinate_sort_key(item.reference_coordinate),
            ),
        )
    )


def _column_name(zero_based_column: int) -> str:
    value = zero_based_column + 1
    characters: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        characters.append(chr(ord("A") + remainder))
    return "".join(reversed(characters))


def _grid_coordinate(grid: Mapping[str, object]) -> str | None:
    raw_row = grid.get("startRow", 0)
    raw_column = grid.get("startColumn", 0)
    if (
        type(raw_row) is not int
        or raw_row < 0
        or type(raw_column) is not int
        or raw_column < 0
    ):
        return None
    return f"{_column_name(raw_column)}{raw_row + 1}"


def _grid_cell_data(grid: Mapping[str, object]) -> Mapping[str, object]:
    row_data = grid.get("rowData", [])
    if not isinstance(row_data, list) or len(row_data) > 1:
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_grid_response"
        )
    if not row_data:
        return {}
    row = row_data[0]
    if not isinstance(row, Mapping):
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_grid_response"
        )
    values = row.get("values", [])
    if not isinstance(values, list) or len(values) > 1:
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_grid_response"
        )
    if not values:
        return {}
    cell = values[0]
    if not isinstance(cell, Mapping):
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_grid_response"
        )
    return cell


def _response_by_coordinate(
    response: object,
    *,
    requested: frozenset[str],
) -> Mapping[str, Mapping[str, object]]:
    if not isinstance(response, Mapping):
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_batch_response"
        )
    raw_sheets = response.get("sheets")
    if not isinstance(raw_sheets, list):
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_batch_response"
        )
    indexed: dict[str, Mapping[str, object]] = {}
    for raw_sheet in raw_sheets:
        if not isinstance(raw_sheet, Mapping):
            raise SecureMediaReferenceResponseError(
                "invalid_media_reference_batch_response"
            )
        raw_grids = raw_sheet.get("data", [])
        if not isinstance(raw_grids, list):
            raise SecureMediaReferenceResponseError(
                "invalid_media_reference_batch_response"
            )
        for raw_grid in raw_grids:
            if not isinstance(raw_grid, Mapping):
                raise SecureMediaReferenceResponseError(
                    "invalid_media_reference_batch_response"
                )
            coordinate = _grid_coordinate(raw_grid)
            if coordinate is None or coordinate not in requested:
                raise SecureMediaReferenceResponseError(
                    "unexpected_media_reference_batch_range"
                )
            if coordinate in indexed:
                raise SecureMediaReferenceResponseError(
                    "duplicate_media_reference_batch_range"
                )
            indexed[coordinate] = _grid_cell_data(raw_grid)
    return indexed


def _valid_link_uri(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        return None
    if any(ord(character) < 32 for character in normalized):
        return None
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    return normalized if parsed.scheme else None


def _rich_text_links(cell: Mapping[str, object]) -> tuple[str, ...]:
    raw_runs = cell.get("textFormatRuns", [])
    if not isinstance(raw_runs, list):
        return ()
    links: list[str] = []
    for raw_run in raw_runs:
        if not isinstance(raw_run, Mapping):
            continue
        raw_format = raw_run.get("format")
        if not isinstance(raw_format, Mapping):
            continue
        raw_link = raw_format.get("link")
        if not isinstance(raw_link, Mapping):
            continue
        uri = _valid_link_uri(raw_link.get("uri"))
        if uri is not None and uri not in links:
            links.append(uri)
    return tuple(links)


def _cell_level_link(cell: Mapping[str, object]) -> str | None:
    raw_format = cell.get("userEnteredFormat")
    if not isinstance(raw_format, Mapping):
        return None
    text_format = raw_format.get("textFormat")
    if not isinstance(text_format, Mapping):
        return None
    raw_link = text_format.get("link")
    if not isinstance(raw_link, Mapping):
        return None
    return _valid_link_uri(raw_link.get("uri"))


def _formula_diagnostic(cell: Mapping[str, object]) -> tuple[bool, FormulaFunction]:
    raw_value = cell.get("userEnteredValue")
    if not isinstance(raw_value, Mapping):
        return False, "NONE"
    formula = raw_value.get("formulaValue")
    if not isinstance(formula, str) or not formula.strip():
        return False, "NONE"
    function_match = re.match(r"^\s*=\s*([A-Za-z][A-Za-z0-9._]*)", formula)
    if function_match is None:
        return True, "OTHER"
    function_name = function_match.group(1).upper()
    if function_name == "HYPERLINK":
        return True, "HYPERLINK"
    if function_name == "IMAGE":
        return True, "IMAGE"
    return True, "OTHER"


def _parse_formula_string_literal(
    formula: str, start: int
) -> tuple[str, int] | None:
    if start >= len(formula) or formula[start] != '"':
        return None
    characters: list[str] = []
    index = start + 1
    while index < len(formula):
        character = formula[index]
        if character != '"':
            characters.append(character)
            index += 1
            continue
        if index + 1 < len(formula) and formula[index + 1] == '"':
            characters.append('"')
            index += 2
            continue
        return "".join(characters), index + 1
    return None


def _valid_optional_formula_argument(formula: str, start: int) -> bool:
    """Validate one opaque second argument without evaluating or retaining it."""

    index = start
    while index < len(formula) and formula[index].isspace():
        index += 1
    if index >= len(formula) or formula[index] == ")":
        return False
    depth = 0
    in_string = False
    has_content = False
    while index < len(formula):
        character = formula[index]
        if in_string:
            if character == '"':
                if index + 1 < len(formula) and formula[index + 1] == '"':
                    index += 2
                    has_content = True
                    continue
                in_string = False
            index += 1
            has_content = True
            continue
        if character == '"':
            in_string = True
            has_content = True
            index += 1
            continue
        if character == "(":
            depth += 1
            has_content = True
            index += 1
            continue
        if character == ")":
            if depth:
                depth -= 1
                has_content = True
                index += 1
                continue
            return has_content and not formula[index + 1 :].strip()
        if character == "," and depth == 0:
            return False
        if not character.isspace():
            has_content = True
        index += 1
    return False


def _literal_hyperlink_formula(
    formula: str,
) -> tuple[Literal["literal", "dynamic", "unsupported"], str | None]:
    opening = re.match(r"^\s*=\s*HYPERLINK\s*\(", formula, re.IGNORECASE)
    if opening is None:
        return "unsupported", None
    index = opening.end()
    while index < len(formula) and formula[index].isspace():
        index += 1
    if index >= len(formula) or formula[index] in ",)":
        return "unsupported", None
    if formula[index] != '"':
        return "dynamic", None
    parsed_literal = _parse_formula_string_literal(formula, index)
    if parsed_literal is None:
        return "unsupported", None
    raw_uri, index = parsed_literal
    uri = _valid_link_uri(raw_uri)
    if uri is None:
        return "unsupported", None
    while index < len(formula) and formula[index].isspace():
        index += 1
    if index >= len(formula):
        return "unsupported", None
    if formula[index] == ")":
        if formula[index + 1 :].strip():
            return "unsupported", None
        return "literal", uri
    if formula[index] != ",":
        return "unsupported", None
    if not _valid_optional_formula_argument(formula, index + 1):
        return "unsupported", None
    return "literal", uri


def _smart_chip_links(
    cell: Mapping[str, object],
) -> tuple[bool, int, tuple[str, ...]]:
    raw_runs = cell.get("chipRuns", [])
    if not isinstance(raw_runs, list):
        return False, 0, ()
    present = bool(raw_runs)
    link_count = 0
    links: list[str] = []
    for raw_run in raw_runs:
        if not isinstance(raw_run, Mapping):
            continue
        raw_chip = raw_run.get("chip")
        if not isinstance(raw_chip, Mapping):
            continue
        rich_link = raw_chip.get("richLinkProperties")
        if not isinstance(rich_link, Mapping):
            continue
        uri = _valid_link_uri(rich_link.get("uri"))
        if uri is None:
            continue
        link_count += 1
        if uri not in links:
            links.append(uri)
    return present, link_count, tuple(links)


def _cell_value(
    cell: Mapping[str, object],
) -> tuple[
    ReferenceReadStatus,
    str | None,
    tuple[str, ...],
    bool,
    int,
    bool,
    bool,
    bool,
    FormulaFunction,
]:
    def result(
        status: ReferenceReadStatus,
        raw_reference: str | None,
        warnings: tuple[str, ...],
    ) -> tuple[
        ReferenceReadStatus,
        str | None,
        tuple[str, ...],
        bool,
        int,
        bool,
        bool,
        bool,
        FormulaFunction,
    ]:
        return (
            status,
            raw_reference,
            warnings,
            chip_present,
            chip_link_count,
            chip_unique,
            cell_link is not None,
            formula_present,
            formula_function,
        )

    chip_present, chip_link_count, chip_links = _smart_chip_links(cell)
    chip_unique = len(chip_links) == 1
    cell_link = _cell_level_link(cell)
    formula_present, formula_function = _formula_diagnostic(cell)
    if not cell:
        return result(
            "media_reference_cell_missing",
            None,
            ("media_reference_cell_missing",),
        )
    direct_hyperlink = _valid_link_uri(cell.get("hyperlink"))
    if direct_hyperlink is not None:
        return result("read", direct_hyperlink, ())
    rich_links = _rich_text_links(cell)
    if len(rich_links) > 1:
        return result(
            "ambiguous_media_hyperlink",
            None,
            ("ambiguous_media_hyperlink",),
        )
    if len(rich_links) == 1:
        return result("read", rich_links[0], ())
    if cell_link is not None:
        return result("read", cell_link, ())
    if len(chip_links) > 1:
        return result(
            "ambiguous_media_smart_chip",
            None,
            ("ambiguous_media_smart_chip",),
        )
    if len(chip_links) == 1:
        return result("read", chip_links[0], ())
    if formula_function == "HYPERLINK":
        raw_formula = cell.get("userEnteredValue")
        formula = (
            raw_formula.get("formulaValue")
            if isinstance(raw_formula, Mapping)
            else None
        )
        if isinstance(formula, str):
            formula_status, formula_uri = _literal_hyperlink_formula(formula)
            if formula_status == "literal" and formula_uri is not None:
                return result("read", formula_uri, ())
            if formula_status == "dynamic":
                return result(
                    "dynamic_hyperlink_formula_unsupported",
                    None,
                    ("dynamic_hyperlink_formula_unsupported",),
                )
        return result(
            "unsupported_hyperlink_formula",
            None,
            ("unsupported_hyperlink_formula",),
        )
    formatted_value = cell.get("formattedValue")
    if formatted_value is None or formatted_value == "":
        return result(
            "empty_media_reference",
            None,
            ("empty_media_reference",),
        )
    if not isinstance(formatted_value, str):
        return result(
            "invalid_media_reference_cell",
            None,
            ("invalid_media_reference_cell",),
        )
    normalized = formatted_value.strip()
    if not normalized:
        return result(
            "empty_media_reference",
            None,
            ("empty_media_reference",),
        )
    if normalized.startswith("https://") or normalized.startswith("http://"):
        return result("read", normalized, ())
    return result(
        "media_reference_link_missing",
        None,
        ("media_reference_link_missing",),
    )


def _read_result(
    mapped: ValidatedMappedMediaSource,
    response_range: Mapping[str, object] | None,
) -> SecureMediaReferenceReadResult:
    if response_range is None:
        return SecureMediaReferenceReadResult(
            mapped_source=mapped,
            read_status="media_reference_response_missing",
            reference_verification="not_read",
            fresh_reference_fingerprint=None,
            raw_reference=None,
            warnings=("media_reference_response_missing",),
            blocking_issues=("media_reference_response_missing",),
        )
    (
        read_status,
        raw_reference,
        read_warnings,
        smart_chip_present,
        smart_chip_rich_link_count,
        smart_chip_unique_uri,
        cell_level_link_present,
        formula_present,
        formula_function,
    ) = _cell_value(response_range)
    if read_status != "read" or raw_reference is None:
        return SecureMediaReferenceReadResult(
            mapped_source=mapped,
            read_status=read_status,
            reference_verification="not_read",
            fresh_reference_fingerprint=None,
            raw_reference=None,
            cell_level_link_present=cell_level_link_present,
            formula_present=formula_present,
            formula_function=formula_function,
            formula_is_hyperlink=formula_function == "HYPERLINK",
            smart_chip_present=smart_chip_present,
            smart_chip_rich_link_count=smart_chip_rich_link_count,
            smart_chip_unique_uri=smart_chip_unique_uri,
            warnings=read_warnings,
            blocking_issues=read_warnings,
        )
    fresh_source = create_supplier_media_source_reference(
        source_coordinate=mapped.reference_coordinate,
        marker_coordinate=mapped.marker_coordinate,
        marker_text=PHOTO_DOWNLOAD_LINK_MARKER,
        raw_reference=raw_reference,
        product_source_candidate=mapped.product_source,
    )
    fresh_fingerprint = fresh_source.reference_fingerprint
    if mapped.mapping_reference_status == "redacted":
        verification: ReferenceVerification = "mapping_reference_redacted"
        warnings = ("mapping_reference_redacted",)
        blockers: tuple[str, ...] = ()
    elif mapped.mapping_reference_fingerprint is None or fresh_fingerprint is None:
        verification = "verification_unavailable"
        warnings = ("reference_verification_unavailable",)
        blockers = ("reference_verification_unavailable",)
    elif mapped.mapping_reference_fingerprint == fresh_fingerprint:
        verification = "verified_unchanged"
        warnings = ()
        blockers = ()
    else:
        verification = "reference_changed_since_mapping"
        warnings = ("reference_changed_since_mapping",)
        blockers = ("reference_changed_since_mapping",)
    return SecureMediaReferenceReadResult(
        mapped_source=mapped,
        read_status="read",
        reference_verification=verification,
        fresh_reference_fingerprint=fresh_fingerprint,
        raw_reference=raw_reference,
        cell_level_link_present=cell_level_link_present,
        formula_present=formula_present,
        formula_function=formula_function,
        formula_is_hyperlink=formula_function == "HYPERLINK",
        smart_chip_present=smart_chip_present,
        smart_chip_rich_link_count=smart_chip_rich_link_count,
        smart_chip_unique_uri=smart_chip_unique_uri,
        warnings=warnings,
        blocking_issues=blockers,
    )


class SecureMediaReferenceReader:
    """Read approved hyperlink cells with one Sheets metadata GET."""

    def __init__(
        self,
        settings: GoogleSettings,
        client_factory: GoogleSheetsReadonlyClientFactory | None,
        *,
        clients: GoogleClients | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._clients = clients

    def run(
        self,
        mapping_report: Mapping[str, object],
        *,
        sheet_title: str,
    ) -> SecureMediaReferenceReadBatch:
        mapped_sources = validate_mapping_report(mapping_report)
        validated_sheet = validate_sheet_title(sheet_title)
        if not mapped_sources:
            return SecureMediaReferenceReadBatch(
                results=(),
                coordinates_requested=0,
                read_requests_performed=0,
                write_requests_performed=0,
            )
        clients = self._clients
        if clients is None:
            self._settings.validate_sheets_readonly()
            if self._client_factory is None:
                raise TypeError("client_factory is required when clients are not supplied")
            sheets = self._client_factory.create_sheets_readonly(self._settings)
        else:
            sheets = clients.sheets
        gateway = ReadOnlySheetsGateway(sheets)
        coordinates = tuple(
            item.reference_coordinate for item in mapped_sources
        )
        response = gateway.batch_get_sheet_link_cells(
            self._settings.clm_spreadsheet_id,
            validated_sheet,
            coordinates,
        )
        response_by_coordinate = _response_by_coordinate(
            response,
            requested=frozenset(coordinates),
        )
        results = tuple(
            _read_result(item, response_by_coordinate.get(item.reference_coordinate))
            for item in mapped_sources
        )
        return SecureMediaReferenceReadBatch(
            results=results,
            coordinates_requested=len(coordinates),
            read_requests_performed=gateway.counters.read_requests_performed,
            write_requests_performed=0,
        )
