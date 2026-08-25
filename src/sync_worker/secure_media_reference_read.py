"""Exact-cell, read-only Google Sheets access for mapped media references."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .config import GoogleSettings
from .google_api import GoogleClientFactory, ReadOnlyGoogleGateway
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
]
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


def _returned_coordinate(value: object, sheet_title: str) -> str | None:
    if not isinstance(value, str) or value.count("!") != 1:
        return None
    returned_sheet, coordinate = value.split("!", 1)
    if returned_sheet.startswith("'") and returned_sheet.endswith("'"):
        returned_sheet = returned_sheet[1:-1].replace("''", "'")
    if returned_sheet != sheet_title:
        return None
    try:
        return validate_single_cell_coordinate(coordinate)
    except SecureMediaReferenceInputError:
        return None


def _response_by_coordinate(
    response: object,
    *,
    sheet_title: str,
    requested: frozenset[str],
) -> Mapping[str, Mapping[str, object]]:
    if not isinstance(response, Mapping):
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_batch_response"
        )
    raw_ranges = response.get("valueRanges")
    if not isinstance(raw_ranges, list):
        raise SecureMediaReferenceResponseError(
            "invalid_media_reference_batch_response"
        )
    indexed: dict[str, Mapping[str, object]] = {}
    for raw_range in raw_ranges:
        if not isinstance(raw_range, Mapping):
            raise SecureMediaReferenceResponseError(
                "invalid_media_reference_batch_response"
            )
        coordinate = _returned_coordinate(raw_range.get("range"), sheet_title)
        if coordinate is None or coordinate not in requested:
            raise SecureMediaReferenceResponseError(
                "unexpected_media_reference_batch_range"
            )
        if coordinate in indexed:
            raise SecureMediaReferenceResponseError(
                "duplicate_media_reference_batch_range"
            )
        indexed[coordinate] = raw_range
    return indexed


def _cell_value(
    response_range: Mapping[str, object],
) -> tuple[ReferenceReadStatus, str | None, tuple[str, ...]]:
    if "values" not in response_range:
        return (
            "media_reference_cell_missing",
            None,
            ("media_reference_cell_missing",),
        )
    values = response_range.get("values")
    if not isinstance(values, list) or not values:
        return (
            "media_reference_cell_missing",
            None,
            ("media_reference_cell_missing",),
        )
    if (
        len(values) != 1
        or not isinstance(values[0], list)
        or len(values[0]) != 1
    ):
        return (
            "invalid_media_reference_cell",
            None,
            ("invalid_media_reference_cell",),
        )
    value = values[0][0]
    if not isinstance(value, str):
        return (
            "invalid_media_reference_cell",
            None,
            ("invalid_media_reference_cell",),
        )
    if not value.strip():
        return (
            "empty_media_reference",
            None,
            ("empty_media_reference",),
        )
    return "read", value, ()


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
    read_status, raw_reference, read_warnings = _cell_value(response_range)
    if read_status != "read" or raw_reference is None:
        return SecureMediaReferenceReadResult(
            mapped_source=mapped,
            read_status=read_status,
            reference_verification="not_read",
            fresh_reference_fingerprint=None,
            raw_reference=None,
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
        warnings=warnings,
        blocking_issues=blockers,
    )


class SecureMediaReferenceReader:
    """Read approved cells with one Sheets batchGet and no write surface."""

    def __init__(
        self,
        settings: GoogleSettings,
        client_factory: GoogleClientFactory,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory

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
        self._settings.validate()
        clients = self._client_factory.create(self._settings)
        gateway = ReadOnlyGoogleGateway(clients)
        coordinates = tuple(
            item.reference_coordinate for item in mapped_sources
        )
        response = gateway.batch_get_sheet_cells(
            self._settings.clm_spreadsheet_id,
            validated_sheet,
            coordinates,
        )
        response_by_coordinate = _response_by_coordinate(
            response,
            sheet_title=validated_sheet,
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
