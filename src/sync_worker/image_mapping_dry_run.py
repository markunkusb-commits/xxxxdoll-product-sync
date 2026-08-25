"""Pure-local adapter for ProductRecord to supplier-media mapping dry runs.

The adapter reads already-created JSON snapshots and reasons only about their
layout provenance.  It never opens a media reference or creates an API client.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .image_mapping import (
    ProductSourceRange,
    SupplierMediaSourceReference,
    create_supplier_media_source_reference,
    is_photo_download_link_marker,
    map_product_media_sources,
)
from .product_model import ProductRecord
from .product_size_enrichment_dry_run import (
    load_local_json_report,
    restore_product_records,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor


REPORT_FILENAME = "image-mapping-dry-run.json"

_COORDINATE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_RANGE_PATTERN = re.compile(
    r"^([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)$"
)
_UNSAFE_REPORT_PATTERNS = (
    re.compile(r"(?i)http://[^\s/:@]+:[^\s/@]+@"),
    re.compile(
        r"(?i)(?:access_token|token|signature|auth|key|password)\s*="
    ),
    re.compile(r"(?i)\b(?:authorization|cookie)\b"),
)


class ImageMappingDryRunInputError(ValueError):
    """Safe structural input error that excludes supplier cell contents."""


class ImageMappingDryRunSafetyError(ValueError):
    """Raised before persistence when a report projection is unsafe."""


@dataclass(frozen=True, slots=True)
class LayoutCell:
    coordinate: str
    row: int
    column_index: int
    formatted_value: str
    merged_range: str | None


@dataclass(frozen=True, slots=True)
class MediaReferencePairingIssue:
    issue: str
    marker_coordinate: str
    marker_row: int
    candidate_reference_coordinates: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "marker_coordinate": self.marker_coordinate,
            "marker_row": self.marker_row,
            "candidate_reference_coordinates": list(
                self.candidate_reference_coordinates
            ),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class MediaReferenceExtraction:
    references: tuple[SupplierMediaSourceReference, ...]
    issues: tuple[MediaReferencePairingIssue, ...]

    @property
    def missing_media_references(self) -> int:
        return sum(item.issue == "missing_media_reference" for item in self.issues)

    @property
    def ambiguous_media_reference_pairs(self) -> int:
        return sum(
            item.issue == "ambiguous_media_reference_pair"
            for item in self.issues
        )


def _column_number(label: str) -> int:
    value = 0
    for character in label:
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _coordinate(value: object) -> tuple[str, int, int] | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    match = _COORDINATE_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    return normalized, int(match.group(2)), _column_number(match.group(1))


def _range_bounds(
    value: object,
    *,
    fallback_row: int,
    fallback_column: int,
) -> tuple[int, int, int, int]:
    if not isinstance(value, str):
        return fallback_row, fallback_row, fallback_column, fallback_column
    match = _RANGE_PATTERN.fullmatch(value.strip().upper())
    if match is None:
        return fallback_row, fallback_row, fallback_column, fallback_column
    start_column = _column_number(match.group(1))
    start_row = int(match.group(2))
    end_column = _column_number(match.group(3))
    end_row = int(match.group(4))
    if start_row > end_row or start_column > end_column:
        return fallback_row, fallback_row, fallback_column, fallback_column
    return start_row, end_row, start_column, end_column


def _layout_cells(layout: Mapping[str, object]) -> tuple[LayoutCell, ...]:
    raw_cells = layout.get("non_empty_cells")
    if not isinstance(raw_cells, list):
        raise ImageMappingDryRunInputError(
            "Layout must contain a non_empty_cells array"
        )
    cells: list[LayoutCell] = []
    seen_coordinates: set[str] = set()
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, Mapping):
            raise ImageMappingDryRunInputError("Layout cell must be an object")
        parsed_coordinate = _coordinate(raw_cell.get("coordinate"))
        row = raw_cell.get("row")
        column_index = raw_cell.get("column_index")
        formatted_value = raw_cell.get("formatted_value")
        merged_range = raw_cell.get("merged_range")
        if (
            parsed_coordinate is None
            or type(row) is not int
            or row <= 0
            or type(column_index) is not int
            or column_index <= 0
            or not isinstance(formatted_value, str)
            or (merged_range is not None and not isinstance(merged_range, str))
        ):
            raise ImageMappingDryRunInputError("Layout cell is malformed")
        coordinate, coordinate_row, coordinate_column = parsed_coordinate
        if row != coordinate_row or column_index != coordinate_column:
            raise ImageMappingDryRunInputError(
                "Layout cell coordinate metadata does not match"
            )
        if coordinate in seen_coordinates:
            raise ImageMappingDryRunInputError(
                "Layout contains a duplicate cell coordinate"
            )
        seen_coordinates.add(coordinate)
        cells.append(
            LayoutCell(
                coordinate=coordinate,
                row=row,
                column_index=column_index,
                formatted_value=formatted_value,
                merged_range=merged_range,
            )
        )
    return tuple(sorted(cells, key=lambda item: (item.row, item.column_index)))


def _owning_product_indexes(
    row: int,
    products: Sequence[ProductRecord],
) -> tuple[int, ...]:
    return tuple(
        index
        for index, product in enumerate(products)
        if product.source.start_row <= row <= product.source.end_row
    )


def _structural_reference_candidates(
    marker: LayoutCell,
    cells: Sequence[LayoutCell],
) -> tuple[LayoutCell, ...]:
    """Find only cells in the marker's explicit right-side layout band.

    A marker can point to a value on its own row or on the first row following
    its merged vertical span.  The value must be strictly to the right of the
    marker/merged band.  No wider row search or value-based nearest match is
    performed.
    """

    start_row, end_row, _start_column, end_column = _range_bounds(
        marker.merged_range,
        fallback_row=marker.row,
        fallback_column=marker.column_index,
    )
    allowed_rows = set(range(start_row, end_row + 1)) | {end_row + 1}
    return tuple(
        item
        for item in cells
        if item.coordinate != marker.coordinate
        and item.row in allowed_rows
        and item.column_index > end_column
        and not is_photo_download_link_marker(item.formatted_value)
    )


def extract_supplier_media_source_references(
    layout: Mapping[str, object],
    products: Sequence[ProductRecord],
) -> MediaReferenceExtraction:
    """Extract unambiguous marker/reference pairs from one local layout."""

    cells = _layout_cells(layout)
    markers = tuple(
        item for item in cells if is_photo_download_link_marker(item.formatted_value)
    )
    references: list[SupplierMediaSourceReference] = []
    issues: list[MediaReferencePairingIssue] = []
    stable_products = tuple(products)
    for marker in markers:
        candidates = _structural_reference_candidates(marker, cells)
        marker_owners = _owning_product_indexes(marker.row, stable_products)
        compatible_candidates = tuple(
            candidate
            for candidate in candidates
            if _owning_product_indexes(candidate.row, stable_products)
            == marker_owners
        )
        if not compatible_candidates:
            warnings = (
                ("cross_product_media_reference_pair",)
                if candidates
                else ()
            )
            issues.append(
                MediaReferencePairingIssue(
                    issue="missing_media_reference",
                    marker_coordinate=marker.coordinate,
                    marker_row=marker.row,
                    candidate_reference_coordinates=tuple(
                        item.coordinate for item in candidates
                    ),
                    warnings=warnings,
                )
            )
            continue
        if len(compatible_candidates) > 1:
            issues.append(
                MediaReferencePairingIssue(
                    issue="ambiguous_media_reference_pair",
                    marker_coordinate=marker.coordinate,
                    marker_row=marker.row,
                    candidate_reference_coordinates=tuple(
                        item.coordinate for item in compatible_candidates
                    ),
                    warnings=("ambiguous_media_reference_pair",),
                )
            )
            continue
        candidate = compatible_candidates[0]
        product_candidate = None
        if len(marker_owners) == 1:
            product = stable_products[marker_owners[0]]
            product_candidate = ProductSourceRange(
                start_row=product.source.start_row,
                end_row=product.source.end_row,
            )
        references.append(
            create_supplier_media_source_reference(
                source_coordinate=candidate.coordinate,
                source_row=candidate.row,
                marker_coordinate=marker.coordinate,
                marker_text=marker.formatted_value,
                raw_reference=candidate.formatted_value,
                product_source_candidate=product_candidate,
            )
        )
    return MediaReferenceExtraction(tuple(references), tuple(issues))


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved_input = input_path.resolve()
    try:
        return resolved_input.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _attach_pairing_issues(
    results: list[dict[str, object]],
    issues: Sequence[MediaReferencePairingIssue],
) -> None:
    for issue in issues:
        owners: list[dict[str, object]] = []
        for result in results:
            source = result.get("product_source")
            if not isinstance(source, Mapping):
                continue
            start_row = source.get("start_row")
            end_row = source.get("end_row")
            if (
                type(start_row) is int
                and type(end_row) is int
                and start_row <= issue.marker_row <= end_row
            ):
                owners.append(result)
        if len(owners) != 1:
            continue
        result = owners[0]
        warnings = result.get("warnings")
        blocking_issues = result.get("blocking_issues")
        if isinstance(warnings, list):
            for warning in (issue.issue, *issue.warnings):
                if warning not in warnings:
                    warnings.append(warning)
        if isinstance(blocking_issues, list) and issue.issue not in blocking_issues:
            blocking_issues.append(issue.issue)
        result["status"] = (
            "ambiguous"
            if issue.issue == "ambiguous_media_reference_pair"
            else "invalid"
        )


def _attach_reference_rows(
    results: Sequence[dict[str, object]],
    media_source_results: Sequence[dict[str, object]],
    references: Sequence[SupplierMediaSourceReference],
) -> None:
    provenance_rows: dict[tuple[object, ...], tuple[int | None, int]] = {}
    for reference in references:
        marker = _coordinate(reference.marker_coordinate)
        provenance_rows[
            (
                reference.marker_coordinate,
                reference.source_coordinate,
                reference.reference_fingerprint,
            )
        ] = (marker[1] if marker is not None else None, reference.source_row)

    def attach(item: dict[str, object]) -> None:
        rows = provenance_rows.get(
            (
                item.get("marker_coordinate"),
                item.get("reference_coordinate"),
                item.get("reference_fingerprint"),
            )
        )
        if rows is not None:
            item["marker_row"], item["reference_row"] = rows

    for item in media_source_results:
        attach(item)
    for result in results:
        raw_sources = result.get("media_sources")
        if not isinstance(raw_sources, list):
            continue
        for raw_source in raw_sources:
            if isinstance(raw_source, dict):
                attach(raw_source)


def _assert_report_safe(report: Mapping[str, object]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if any(pattern.search(serialized) for pattern in _UNSAFE_REPORT_PATTERNS):
        raise ImageMappingDryRunSafetyError("unsafe_media_reference_leak")


def build_image_mapping_dry_run_report(
    products: Sequence[ProductRecord],
    layout: Mapping[str, object],
    *,
    product_input_file: str,
    layout_input_file: str,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Extract layout provenance, call the core mapper, and project safely."""

    extraction = extract_supplier_media_source_references(layout, products)
    mapped = map_product_media_sources(products, extraction.references)
    core_report = mapped.to_report_dict()
    summary = dict(mapped.summary.to_dict())
    summary.update(
        {
            "missing_media_references": extraction.missing_media_references,
            "ambiguous_media_reference_pairs": (
                extraction.ambiguous_media_reference_pairs
            ),
        }
    )
    raw_results = core_report.get("results")
    if not isinstance(raw_results, list):
        raise TypeError("Image Mapping Core report results must be an array")
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)]
    raw_media_source_results = core_report.get("media_source_results", [])
    if not isinstance(raw_media_source_results, list):
        raise TypeError(
            "Image Mapping Core media_source_results must be an array"
        )
    media_source_results = [
        dict(item)
        for item in raw_media_source_results
        if isinstance(item, Mapping)
    ]
    _attach_reference_rows(
        results,
        media_source_results,
        extraction.references,
    )
    _attach_pairing_issues(results, extraction.issues)
    report: dict[str, object] = {
        "status": "ok",
        "inputs": {
            "products": product_input_file,
            "layout": layout_input_file,
        },
        "summary": summary,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": results,
        "media_source_results": media_source_results,
        "pairing_issues": [item.to_dict() for item in extraction.issues],
    }
    _assert_report_safe(report)
    active_redactor = redactor or Redactor()
    sanitized = sanitize_report_data(report, active_redactor)
    if not isinstance(sanitized, dict):
        raise TypeError("Image Mapping dry-run report must be an object")
    _assert_report_safe(sanitized)
    return sanitized


def run_image_mapping_dry_run(
    product_input_path: Path,
    layout_input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read two local snapshots and write one safe Image Mapping report."""

    product_path = Path(product_input_path)
    layout_path = Path(layout_input_path)
    product_report = load_local_json_report(product_path)
    layout_report = load_local_json_report(layout_path)
    products = restore_product_records(product_report)
    active_redactor = redactor or Redactor()
    report = build_image_mapping_dry_run_report(
        products,
        layout_report,
        product_input_file=_safe_input_reference(product_path, project_root),
        layout_input_file=_safe_input_reference(layout_path, project_root),
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    _assert_report_safe(report)
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
