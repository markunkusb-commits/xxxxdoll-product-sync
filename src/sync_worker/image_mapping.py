"""Deterministic, pure-local ProductRecord to supplier-media source mapping.

This module maps provenance only.  It never opens a media reference, downloads
content, creates an API client, or emits a WooCommerce ``images`` payload.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal
from urllib.parse import urlsplit

from .product_model import ProductRecord
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


ReferenceStatus = Literal["available", "redacted", "missing", "invalid"]
MediaMatchStatus = Literal[
    "exact_source_range_match",
    "unmatched_media_source",
    "ambiguous_media_source",
    "unsupported_media_marker",
    "invalid_reference",
]
ProductMediaStatus = Literal["mapped", "no_media_source", "ambiguous", "invalid"]
MediaSourceKind = Literal[
    "unknown",
    "direct_image",
    "folder",
    "archive",
    "cloud_share",
]

PHOTO_DOWNLOAD_LINK_MARKER = "photo download link"
REDACTED_REFERENCE = "[URL_REDACTED]"

_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_URL_TEXT_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s\"'<>]+")
_SENSITIVE_BASENAME_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:token|secret|signature|credential|auth|key)"
    r"(?:[^a-z0-9]|$)"
)
_REDACTED_REFERENCE_VALUES = frozenset(
    {"[url_redacted]", "[redacted_url]"}
)
_SUPPORTED_MEDIA_SOURCE_KINDS = frozenset(
    {"unknown", "direct_image", "folder", "archive", "cloud_share"}
)


class ImageMappingError(ValueError):
    """Safe structural error without supplier reference contents."""


@dataclass(frozen=True, slots=True)
class ProductSourceRange:
    start_row: int
    end_row: int

    def contains(self, row: int) -> bool:
        return self.start_row <= row <= self.end_row

    def to_dict(self) -> dict[str, int]:
        return {"start_row": self.start_row, "end_row": self.end_row}


@dataclass(frozen=True, slots=True)
class ProductIdentitySnapshot:
    series: str
    model: str | None
    raw_model: str | None
    raw_series_title: str

    def to_dict(self) -> dict[str, object]:
        return {
            "series": _safe_text(self.series),
            "model": _safe_text(self.model),
            "raw_model": _safe_text(self.raw_model),
            "raw_series_title": _safe_text(self.raw_series_title),
        }


@dataclass(frozen=True, slots=True)
class SupplierMediaSourceReference:
    """In-memory supplier reference; raw_reference is never report-serialized."""

    source_coordinate: str
    source_row: int
    marker_coordinate: str | None
    marker_text: str | None
    raw_reference: str | None = field(repr=False)
    safe_reference: str | None
    reference_status: ReferenceStatus
    reference_fingerprint: str | None
    product_source_candidate: ProductSourceRange | None
    media_source_kind: MediaSourceKind = "unknown"
    warnings: tuple[str, ...] = ()

    @property
    def download_ready(self) -> Literal[False]:
        return False

    def to_report_dict(self) -> dict[str, object]:
        """Return a safe projection that deliberately excludes raw_reference."""

        normalized = create_supplier_media_source_reference(
            source_coordinate=self.source_coordinate,
            source_row=self.source_row,
            marker_coordinate=self.marker_coordinate,
            marker_text=self.marker_text,
            raw_reference=self.raw_reference,
            product_source_candidate=self.product_source_candidate,
            media_source_kind=self.media_source_kind,
        )
        return {
            "source_coordinate": normalized.source_coordinate,
            "source_row": normalized.source_row,
            "marker_coordinate": normalized.marker_coordinate,
            "marker_text": _safe_text(normalized.marker_text),
            "safe_reference": normalized.safe_reference,
            "reference_status": normalized.reference_status,
            "reference_fingerprint": normalized.reference_fingerprint,
            "product_source_candidate": (
                normalized.product_source_candidate.to_dict()
                if normalized.product_source_candidate is not None
                else None
            ),
            "media_source_kind": normalized.media_source_kind,
            "download_ready": False,
            "warnings": list(normalized.warnings),
        }


@dataclass(frozen=True, slots=True)
class MediaSourceMappingResult:
    match_status: MediaMatchStatus
    match_method: str | None
    marker_coordinate: str | None
    marker_text: str | None
    reference_coordinate: str
    reference_status: ReferenceStatus
    safe_reference: str | None
    reference_fingerprint: str | None
    media_source_kind: MediaSourceKind
    candidate_product_identities: tuple[str, ...]
    download_ready: Literal[False] = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "match_status": self.match_status,
            "match_method": self.match_method,
            "marker_coordinate": self.marker_coordinate,
            "marker_text": _safe_text(self.marker_text),
            "reference_coordinate": self.reference_coordinate,
            "reference_status": self.reference_status,
            "safe_reference": self.safe_reference,
            "reference_fingerprint": self.reference_fingerprint,
            "media_source_kind": self.media_source_kind,
            "candidate_product_identities": list(
                self.candidate_product_identities
            ),
            "download_ready": False,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ProductMediaMappingResult:
    status: ProductMediaStatus
    product_identity: ProductIdentitySnapshot
    series: str
    product_source: ProductSourceRange
    media_sources: tuple[MediaSourceMappingResult, ...]
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "product_identity": self.product_identity.to_dict(),
            "series": self.series,
            "product_source": self.product_source.to_dict(),
            "media_sources": [item.to_dict() for item in self.media_sources],
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


@dataclass(frozen=True, slots=True)
class ImageMappingSummary:
    total_products: int
    products_with_media_source: int
    products_without_media_source: int
    total_media_sources: int
    mapped_media_sources: int
    unmatched_media_sources: int
    ambiguous_media_sources: int
    redacted_media_sources: int
    duplicate_media_references: int
    shared_media_references: int

    def to_dict(self) -> dict[str, int]:
        return {
            "total_products": self.total_products,
            "products_with_media_source": self.products_with_media_source,
            "products_without_media_source": self.products_without_media_source,
            "total_media_sources": self.total_media_sources,
            "mapped_media_sources": self.mapped_media_sources,
            "unmatched_media_sources": self.unmatched_media_sources,
            "ambiguous_media_sources": self.ambiguous_media_sources,
            "redacted_media_sources": self.redacted_media_sources,
            "duplicate_media_references": self.duplicate_media_references,
            "shared_media_references": self.shared_media_references,
        }


@dataclass(frozen=True, slots=True)
class ImageMappingBatchResult:
    product_results: tuple[ProductMediaMappingResult, ...]
    media_source_results: tuple[MediaSourceMappingResult, ...]
    summary: ImageMappingSummary
    network_requests_performed: Literal[0] = 0
    write_requests_performed: Literal[0] = 0

    def to_report_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "summary": self.summary.to_dict(),
            "network_requests_performed": 0,
            "write_requests_performed": 0,
            "results": [item.to_dict() for item in self.product_results],
            "media_source_results": [
                item.to_dict() for item in self.media_source_results
            ],
        }


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    without_urls = _URL_TEXT_PATTERN.sub("[REDACTED_URL]", value)
    return Redactor().text(
        REPORT_SECRET_SCAN_PATTERN.sub("[REDACTED]", without_urls),
        limit=300,
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _coordinate(value: object) -> tuple[str, int] | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    matched = _COORDINATE_PATTERN.fullmatch(normalized)
    if matched is None:
        return None
    return normalized, int(matched.group(2))


def _normalize_marker(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None


def _reference_fingerprint(raw_reference: str, source_coordinate: str) -> str:
    normalized = raw_reference.strip()
    if normalized.casefold() in _REDACTED_REFERENCE_VALUES:
        normalized = f"redacted-provenance:{source_coordinate}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_url_reference(raw_reference: str) -> str | None:
    if any(ord(character) < 32 for character in raw_reference):
        return None
    try:
        parsed = urlsplit(raw_reference.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold() if parsed.hostname else None
    if scheme not in {"http", "https"} or hostname is None:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host = f"{host}:{port}"
    basename = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if basename and (
        _SENSITIVE_BASENAME_PATTERN.search(basename)
        or REPORT_SECRET_SCAN_PATTERN.search(basename)
    ):
        basename = "[REDACTED_BASENAME]"
    path = f"/{basename}" if basename else "/"
    return f"{scheme}://{host}{path}"


def create_supplier_media_source_reference(
    *,
    source_coordinate: str,
    marker_coordinate: str | None,
    marker_text: str | None,
    raw_reference: str | None,
    source_row: int | None = None,
    product_source_candidate: ProductSourceRange | None = None,
    media_source_kind: str = "unknown",
) -> SupplierMediaSourceReference:
    """Create a normalized in-memory reference without opening the resource."""

    warnings: list[str] = []
    parsed_source = _coordinate(source_coordinate)
    normalized_source_coordinate = (
        parsed_source[0]
        if parsed_source is not None
        else _safe_text(str(source_coordinate)) or ""
    )
    coordinate_row = parsed_source[1] if parsed_source is not None else None
    if parsed_source is None:
        warnings.append("invalid_source_coordinate")
    effective_source_row = coordinate_row if source_row is None else source_row
    if type(effective_source_row) is not int or effective_source_row <= 0:
        effective_source_row = coordinate_row or 0
        warnings.append("invalid_source_row")
    elif coordinate_row is not None and effective_source_row != coordinate_row:
        warnings.append("source_row_coordinate_mismatch")

    parsed_marker = _coordinate(marker_coordinate)
    normalized_marker_coordinate = (
        parsed_marker[0] if parsed_marker is not None else None
    )
    if parsed_marker is None:
        warnings.append("invalid_marker_coordinate")
    normalized_marker = _normalize_marker(marker_text)
    if normalized_marker != PHOTO_DOWNLOAD_LINK_MARKER:
        warnings.append("unsupported_media_marker")

    normalized_kind = (
        media_source_kind
        if media_source_kind in _SUPPORTED_MEDIA_SOURCE_KINDS
        else "unknown"
    )
    if normalized_kind != media_source_kind:
        warnings.append("unsupported_media_source_kind")

    fingerprint: str | None = None
    safe_reference: str | None = None
    if raw_reference is None or (
        isinstance(raw_reference, str) and not raw_reference.strip()
    ):
        reference_status: ReferenceStatus = "missing"
        warnings.append("missing_reference")
    elif not isinstance(raw_reference, str):
        reference_status = "invalid"
        warnings.append("invalid_reference")
    else:
        fingerprint = _reference_fingerprint(
            raw_reference,
            normalized_source_coordinate,
        )
        if raw_reference.strip().casefold() in _REDACTED_REFERENCE_VALUES:
            reference_status = "redacted"
            safe_reference = REDACTED_REFERENCE
            warnings.append("redacted_reference")
        else:
            safe_reference = _safe_url_reference(raw_reference)
            if safe_reference is None:
                reference_status = "invalid"
                warnings.append("invalid_reference")
            else:
                reference_status = "available"

    if (
        parsed_source is None
        or "source_row_coordinate_mismatch" in warnings
        or "invalid_source_row" in warnings
    ):
        reference_status = "invalid"
        safe_reference = None
        warnings.append("invalid_reference")

    return SupplierMediaSourceReference(
        source_coordinate=normalized_source_coordinate,
        source_row=effective_source_row,
        marker_coordinate=normalized_marker_coordinate,
        marker_text=_safe_text(marker_text),
        raw_reference=raw_reference if isinstance(raw_reference, str) else None,
        safe_reference=safe_reference,
        reference_status=reference_status,
        reference_fingerprint=fingerprint,
        product_source_candidate=product_source_candidate,
        media_source_kind=normalized_kind,  # type: ignore[arg-type]
        warnings=_unique(warnings),
    )


def _normalize_source(
    source: SupplierMediaSourceReference,
) -> SupplierMediaSourceReference:
    if not isinstance(source, SupplierMediaSourceReference):
        raise TypeError(
            "media_sources must contain SupplierMediaSourceReference values"
        )
    return create_supplier_media_source_reference(
        source_coordinate=source.source_coordinate,
        source_row=source.source_row,
        marker_coordinate=source.marker_coordinate,
        marker_text=source.marker_text,
        raw_reference=source.raw_reference,
        product_source_candidate=source.product_source_candidate,
        media_source_kind=source.media_source_kind,
    )


def _product_identity(product: ProductRecord) -> ProductIdentitySnapshot:
    return ProductIdentitySnapshot(
        series=product.identity.series,
        model=product.identity.model,
        raw_model=product.identity.raw_model,
        raw_series_title=product.identity.raw_series_title,
    )


def _identity_key(product: ProductRecord) -> str:
    identity = product.identity
    model = _safe_text(identity.model or identity.raw_model or "<missing>")
    series = _safe_text(identity.series)
    return (
        f"{series}:{model}:"
        f"{product.source.start_row}-{product.source.end_row}"
    )


def _product_sort_key(product: ProductRecord) -> tuple[object, ...]:
    return (
        product.source.start_row,
        product.source.end_row,
        product.identity.series.casefold(),
        (product.identity.model or "").casefold(),
        (product.identity.raw_model or "").casefold(),
    )


def _source_sort_key(
    source: SupplierMediaSourceReference,
) -> tuple[object, ...]:
    return (
        source.source_row,
        source.source_coordinate,
        source.marker_coordinate or "",
        source.reference_fingerprint or "",
        source.reference_status,
    )


def _source_mapping(
    source: SupplierMediaSourceReference,
    products: Sequence[ProductRecord],
) -> tuple[MediaSourceMappingResult, tuple[int, ...]]:
    marker = _normalize_marker(source.marker_text)
    marker_parts = _coordinate(source.marker_coordinate)
    reference_parts = _coordinate(source.source_coordinate)
    potential_candidates: tuple[int, ...] = ()
    if marker_parts is not None and reference_parts is not None:
        potential_candidates = tuple(
            index
            for index, product in enumerate(products)
            if product.source.start_row <= marker_parts[1] <= product.source.end_row
            and product.source.start_row
            <= reference_parts[1]
            <= product.source.end_row
        )
    candidate_identities = tuple(
        _identity_key(products[index]) for index in potential_candidates
    )
    warnings = list(source.warnings)

    if marker != PHOTO_DOWNLOAD_LINK_MARKER:
        match_status: MediaMatchStatus = "unsupported_media_marker"
        match_method = None
    elif marker_parts is None or reference_parts is None:
        match_status = "invalid_reference"
        match_method = None
        warnings.append("invalid_reference")
    elif source.reference_status in {"missing", "invalid"}:
        match_status = "invalid_reference"
        match_method = None
    elif len(potential_candidates) == 0:
        match_status = "unmatched_media_source"
        match_method = None
        warnings.append("unmatched_media_source")
    elif len(potential_candidates) > 1:
        match_status = "ambiguous_media_source"
        match_method = None
        warnings.append("ambiguous_media_source")
    else:
        match_status = "exact_source_range_match"
        match_method = "source_range"

    return (
        MediaSourceMappingResult(
            match_status=match_status,
            match_method=match_method,
            marker_coordinate=source.marker_coordinate,
            marker_text=source.marker_text,
            reference_coordinate=source.source_coordinate,
            reference_status=source.reference_status,
            safe_reference=source.safe_reference,
            reference_fingerprint=source.reference_fingerprint,
            media_source_kind=source.media_source_kind,
            candidate_product_identities=candidate_identities,
            download_ready=False,
            warnings=_unique(warnings),
        ),
        potential_candidates,
    )


def summarize_image_mapping(
    product_results: Sequence[ProductMediaMappingResult],
    media_source_results: Sequence[MediaSourceMappingResult],
) -> ImageMappingSummary:
    """Build the required deterministic batch counters."""

    shared_fingerprints = {
        item.reference_fingerprint
        for item in media_source_results
        if item.reference_fingerprint is not None
        and "shared_media_reference" in item.warnings
    }
    duplicate_groups: dict[tuple[object, ...], int] = defaultdict(int)
    for item in media_source_results:
        if "duplicate_media_reference" not in item.warnings:
            continue
        duplicate_groups[
            (
                item.candidate_product_identities,
                item.reference_fingerprint,
                item.marker_coordinate,
                item.reference_coordinate,
            )
        ] += 1
    return ImageMappingSummary(
        total_products=len(product_results),
        products_with_media_source=sum(
            bool(item.media_sources) for item in product_results
        ),
        products_without_media_source=sum(
            not item.media_sources for item in product_results
        ),
        total_media_sources=len(media_source_results),
        mapped_media_sources=sum(
            item.match_status == "exact_source_range_match"
            for item in media_source_results
        ),
        unmatched_media_sources=sum(
            item.match_status == "unmatched_media_source"
            for item in media_source_results
        ),
        ambiguous_media_sources=sum(
            item.match_status == "ambiguous_media_source"
            for item in media_source_results
        ),
        redacted_media_sources=sum(
            item.reference_status == "redacted"
            for item in media_source_results
        ),
        duplicate_media_references=sum(
            max(count - 1, 0) for count in duplicate_groups.values()
        ),
        shared_media_references=len(shared_fingerprints),
    )


def map_product_media_sources(
    products: Sequence[ProductRecord],
    media_sources: Sequence[SupplierMediaSourceReference],
) -> ImageMappingBatchResult:
    """Map sources by explicit row ranges only; never by names or URLs."""

    if isinstance(products, (str, bytes)) or not isinstance(products, Sequence):
        raise TypeError("products must be a sequence")
    if isinstance(media_sources, (str, bytes)) or not isinstance(
        media_sources, Sequence
    ):
        raise TypeError("media_sources must be a sequence")
    if any(not isinstance(product, ProductRecord) for product in products):
        raise TypeError("products must contain ProductRecord values")

    stable_products = tuple(sorted(products, key=_product_sort_key))
    for product in stable_products:
        if (
            type(product.source.start_row) is not int
            or type(product.source.end_row) is not int
            or product.source.start_row <= 0
            or product.source.end_row < product.source.start_row
        ):
            raise ImageMappingError("product source range is invalid")
    stable_sources = tuple(
        sorted((_normalize_source(item) for item in media_sources), key=_source_sort_key)
    )

    source_results: list[MediaSourceMappingResult] = []
    candidate_indexes: list[tuple[int, ...]] = []
    exact_product_indexes: list[int | None] = []
    for source in stable_sources:
        result, candidates = _source_mapping(source, stable_products)
        source_results.append(result)
        candidate_indexes.append(candidates)
        exact_product_indexes.append(
            candidates[0]
            if result.match_status == "exact_source_range_match"
            and len(candidates) == 1
            else None
        )

    duplicate_groups: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for source_index, product_index in enumerate(exact_product_indexes):
        if product_index is None:
            continue
        source = source_results[source_index]
        duplicate_groups[
            (
                product_index,
                source.reference_fingerprint,
                source.marker_coordinate,
                source.reference_coordinate,
            )
        ].append(source_index)
    skipped_duplicate_indexes: set[int] = set()
    for indexes in duplicate_groups.values():
        if len(indexes) < 2:
            continue
        for source_index in indexes:
            source_results[source_index] = replace(
                source_results[source_index],
                warnings=_unique(
                    (
                        *source_results[source_index].warnings,
                        "duplicate_media_reference",
                    )
                ),
            )
        skipped_duplicate_indexes.update(indexes[1:])

    shared_groups: dict[str, list[int]] = defaultdict(list)
    for source_index, product_index in enumerate(exact_product_indexes):
        fingerprint = source_results[source_index].reference_fingerprint
        if product_index is not None and fingerprint is not None:
            shared_groups[fingerprint].append(source_index)
    for indexes in shared_groups.values():
        if len({exact_product_indexes[index] for index in indexes}) < 2:
            continue
        for source_index in indexes:
            source_results[source_index] = replace(
                source_results[source_index],
                warnings=_unique(
                    (
                        *source_results[source_index].warnings,
                        "shared_media_reference",
                    )
                ),
            )

    product_results: list[ProductMediaMappingResult] = []
    for product_index, product in enumerate(stable_products):
        mapped_sources = tuple(
            source_results[source_index]
            for source_index, exact_product_index in enumerate(
                exact_product_indexes
            )
            if exact_product_index == product_index
            and source_index not in skipped_duplicate_indexes
        )
        ambiguous_sources = tuple(
            source_results[source_index]
            for source_index, candidates in enumerate(candidate_indexes)
            if product_index in candidates
            and source_results[source_index].match_status
            == "ambiguous_media_source"
        )
        invalid_sources = tuple(
            source_results[source_index]
            for source_index, candidates in enumerate(candidate_indexes)
            if product_index in candidates
            and source_results[source_index].match_status
            in {"unsupported_media_marker", "invalid_reference"}
        )
        if ambiguous_sources:
            status: ProductMediaStatus = "ambiguous"
            blockers = ("ambiguous_media_source",)
        elif invalid_sources:
            status = "invalid"
            blockers = _unique(
                tuple(item.match_status for item in invalid_sources)
            )
        elif mapped_sources:
            status = "mapped"
            blockers = ()
        else:
            status = "no_media_source"
            blockers = ()
        warnings = _unique(
            tuple(
                warning
                for item in (*mapped_sources, *ambiguous_sources, *invalid_sources)
                for warning in item.warnings
            )
        )
        if status == "no_media_source":
            warnings = _unique((*warnings, "images_not_mapped"))
        product_results.append(
            ProductMediaMappingResult(
                status=status,
                product_identity=_product_identity(product),
                series=product.identity.series,
                product_source=ProductSourceRange(
                    start_row=product.source.start_row,
                    end_row=product.source.end_row,
                ),
                media_sources=mapped_sources,
                warnings=warnings,
                blocking_issues=blockers,
            )
        )

    stable_source_results = tuple(source_results)
    stable_product_results = tuple(product_results)
    summary = summarize_image_mapping(
        stable_product_results,
        stable_source_results,
    )
    return ImageMappingBatchResult(
        product_results=stable_product_results,
        media_source_results=stable_source_results,
        summary=summary,
        network_requests_performed=0,
        write_requests_performed=0,
    )
