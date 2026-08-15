"""Deterministic, pure-local ProductRecord + SizeRecord enrichment."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from typing import Literal

from .product_model import (
    MonetaryValue,
    ProductRecord,
    ProductSpecifications,
    RetailPricing,
)
from .size_list_parser import (
    NormalizedMeasurement,
    SizeMeasurements,
    SizeRecord,
    SupplierFOBCost,
    TwoDimensionalValue,
    UnitValue,
    parse_measurement_value,
)


MatchStatus = Literal["matched", "unmatched", "ambiguous"]
MatchMethod = Literal["exact", "verified_suffix_match"]
MatchConfidence = Literal["exact", "deterministic", "none"]
MeasurementComparisonStatus = Literal[
    "equivalent",
    "different",
    "missing_product",
    "missing_size",
    "incomparable",
]

_VERIFIED_BODY_TOKEN = re.compile(
    r"(?i)^(?:[a-z]{1,6}[0-9]{2,3}(?:cm)?|[0-9]{2,3}cm)"
    r"(?:\s+(?:xs|s|m|l|xl|plus\+?|torso))*$"
)
_PROTECTED_BODY_SUFFIXES = frozenset(
    {"xs", "s", "m", "l", "xl", "plus", "plus+", "torso"}
)


@dataclass(frozen=True, slots=True)
class MatchMetadata:
    status: MatchStatus
    method: MatchMethod | None
    product_raw_identity: str | None
    matched_body_type: str | None
    candidate_keys: tuple[str, ...]
    confidence: MatchConfidence
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpecificationConflict:
    field: str
    product_raw_value: str
    size_raw_value: str
    comparison_reason: str
    resolution: Literal["unresolved"] = "unresolved"


@dataclass(frozen=True, slots=True)
class MeasurementComparison:
    status: MeasurementComparisonStatus
    reason: str


@dataclass(frozen=True, slots=True)
class EnrichedSupplierCosts:
    price_list_fob: MonetaryValue | None
    price_list_body_only_fob: MonetaryValue | None
    price_list_including_head_fob: MonetaryValue | None
    size_list_fob: SupplierFOBCost | None


@dataclass(frozen=True, slots=True)
class SupplierCostConflict:
    price_list_fob: MonetaryValue
    size_list_fob: SupplierFOBCost
    resolution: Literal["unresolved"] = "unresolved"


@dataclass(frozen=True, slots=True)
class ProductSizeMatchResult:
    product: ProductRecord
    size: SizeRecord | None
    product_specifications: ProductSpecifications
    size_specifications: SizeMeasurements | None
    supplier_costs: EnrichedSupplierCosts
    retail_pricing: RetailPricing
    match: MatchMetadata
    conflicts: tuple[SpecificationConflict, ...]
    supplier_cost_conflict: SupplierCostConflict | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EnrichmentSummary:
    total_products: int
    matched: int
    unmatched: int
    ambiguous: int
    exact_matches: int
    suffix_matches: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class _IdentityCandidate:
    raw_value: str
    display_key: str
    comparison_key: str


@dataclass(frozen=True, slots=True)
class _SizeCandidate:
    position: int
    record: SizeRecord


def _display_key(value: str) -> str:
    return " ".join(value.split())


def _comparison_key(value: str) -> str:
    return _display_key(value).casefold()


def _append_identity_candidate(
    candidates: list[_IdentityCandidate],
    seen: set[str],
    *,
    match_value: str | None,
    raw_value: str | None = None,
) -> None:
    if not isinstance(match_value, str):
        return
    display = _display_key(match_value)
    if not display:
        return
    comparison = display.casefold()
    if comparison in seen:
        return
    seen.add(comparison)
    candidates.append(
        _IdentityCandidate(
            raw_value=(
                raw_value
                if isinstance(raw_value, str) and raw_value.strip()
                else match_value
            ),
            display_key=display,
            comparison_key=comparison,
        )
    )


def _is_height_model_label(value: str) -> bool:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return words == ["height", "model"]


def _product_identity_candidates(product: ProductRecord) -> list[_IdentityCandidate]:
    candidates: list[_IdentityCandidate] = []
    seen: set[str] = set()
    _append_identity_candidate(
        candidates,
        seen,
        match_value=product.identity.model,
        raw_value=product.identity.raw_model,
    )

    height_model = product.specifications.normalized.get("height_model")
    _append_identity_candidate(
        candidates,
        seen,
        match_value=height_model,
    )
    for raw_specification in product.specifications.raw:
        if _is_height_model_label(raw_specification.field):
            _append_identity_candidate(
                candidates,
                seen,
                match_value=raw_specification.value,
                raw_value=raw_specification.value,
            )
    return candidates


def _verified_suffix_candidate(candidate: _IdentityCandidate) -> _IdentityCandidate | None:
    if candidate.display_key.count("-") != 1:
        return None
    body_token, suffix = (
        part.strip() for part in candidate.display_key.split("-", maxsplit=1)
    )
    if not body_token or not suffix or not any(character.isalpha() for character in suffix):
        return None
    if suffix.casefold() in _PROTECTED_BODY_SUFFIXES:
        return None
    if _VERIFIED_BODY_TOKEN.fullmatch(body_token) is None:
        return None
    return _IdentityCandidate(
        raw_value=candidate.raw_value,
        display_key=body_token,
        comparison_key=body_token.casefold(),
    )


def _candidate_keys(
    exact_candidates: Sequence[_IdentityCandidate],
    suffix_candidates: Sequence[_IdentityCandidate],
) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for candidate in (*exact_candidates, *suffix_candidates):
        if candidate.comparison_key in seen:
            continue
        seen.add(candidate.comparison_key)
        output.append(candidate.display_key)
    return tuple(output)


def _unique_size_candidates(
    identity_candidates: Sequence[_IdentityCandidate],
    size_index: dict[str, list[_SizeCandidate]],
) -> list[tuple[_IdentityCandidate, _SizeCandidate]]:
    matches: list[tuple[_IdentityCandidate, _SizeCandidate]] = []
    seen_positions: set[int] = set()
    for identity_candidate in identity_candidates:
        for size_candidate in size_index.get(identity_candidate.comparison_key, []):
            if size_candidate.position in seen_positions:
                continue
            seen_positions.add(size_candidate.position)
            matches.append((identity_candidate, size_candidate))
    return matches


def _match_warning_values(
    product: ProductRecord,
    size_candidates: Sequence[_SizeCandidate],
    diagnostic: str | None,
) -> tuple[str, ...]:
    warnings: list[str] = list(product.warnings)
    for size_candidate in size_candidates:
        warnings.extend(size_candidate.record.warnings)
    if diagnostic is not None:
        warnings.append(diagnostic)
    return tuple(dict.fromkeys(warnings))


def _match_product(
    product: ProductRecord,
    size_index: dict[str, list[_SizeCandidate]],
) -> tuple[MatchMetadata, SizeRecord | None]:
    exact_candidates = _product_identity_candidates(product)
    suffix_candidates: list[_IdentityCandidate] = []
    suffix_seen: set[str] = set()
    for candidate in exact_candidates:
        suffix_candidate = _verified_suffix_candidate(candidate)
        if (
            suffix_candidate is not None
            and suffix_candidate.comparison_key not in suffix_seen
        ):
            suffix_seen.add(suffix_candidate.comparison_key)
            suffix_candidates.append(suffix_candidate)

    keys = _candidate_keys(exact_candidates, suffix_candidates)
    primary_raw_identity = exact_candidates[0].raw_value if exact_candidates else None
    exact_matches = _unique_size_candidates(exact_candidates, size_index)
    if len(exact_matches) == 1:
        identity_candidate, size_candidate = exact_matches[0]
        return (
            MatchMetadata(
                status="matched",
                method="exact",
                product_raw_identity=identity_candidate.raw_value,
                matched_body_type=size_candidate.record.identity.body_type,
                candidate_keys=keys,
                confidence="exact",
                warnings=_match_warning_values(product, [size_candidate], None),
            ),
            size_candidate.record,
        )
    if len(exact_matches) > 1:
        matched_sizes = [item[1] for item in exact_matches]
        return (
            MatchMetadata(
                status="ambiguous",
                method="exact",
                product_raw_identity=primary_raw_identity,
                matched_body_type=None,
                candidate_keys=keys,
                confidence="none",
                warnings=_match_warning_values(
                    product,
                    matched_sizes,
                    "multiple deterministic size matches",
                ),
            ),
            None,
        )

    suffix_matches = _unique_size_candidates(suffix_candidates, size_index)
    if len(suffix_matches) == 1:
        identity_candidate, size_candidate = suffix_matches[0]
        return (
            MatchMetadata(
                status="matched",
                method="verified_suffix_match",
                product_raw_identity=identity_candidate.raw_value,
                matched_body_type=size_candidate.record.identity.body_type,
                candidate_keys=keys,
                confidence="deterministic",
                warnings=_match_warning_values(product, [size_candidate], None),
            ),
            size_candidate.record,
        )
    if len(suffix_matches) > 1:
        matched_sizes = [item[1] for item in suffix_matches]
        return (
            MatchMetadata(
                status="ambiguous",
                method="verified_suffix_match",
                product_raw_identity=primary_raw_identity,
                matched_body_type=None,
                candidate_keys=keys,
                confidence="none",
                warnings=_match_warning_values(
                    product,
                    matched_sizes,
                    "multiple deterministic size matches",
                ),
            ),
            None,
        )

    return (
        MatchMetadata(
            status="unmatched",
            method=None,
            product_raw_identity=primary_raw_identity,
            matched_body_type=None,
            candidate_keys=keys,
            confidence="none",
            warnings=_match_warning_values(
                product,
                [],
                "no deterministic size match",
            ),
        ),
        None,
    )


def _compare_measurement_component(
    product_value: UnitValue | TwoDimensionalValue,
    size_value: UnitValue | TwoDimensionalValue,
    *,
    component_name: Literal["metric", "imperial"],
) -> MeasurementComparison:
    if product_value.unit.casefold() != size_value.unit.casefold():
        return MeasurementComparison(
            status="incomparable",
            reason=f"{component_name}_unit_differs",
        )
    if isinstance(product_value, TwoDimensionalValue) != isinstance(
        size_value, TwoDimensionalValue
    ):
        return MeasurementComparison(
            status="different",
            reason=f"{component_name}_shape_differs",
        )
    if isinstance(product_value, TwoDimensionalValue) and isinstance(
        size_value, TwoDimensionalValue
    ):
        if product_value.length != size_value.length:
            return MeasurementComparison(
                status="different",
                reason=f"{component_name}_length_differs",
            )
        if product_value.width != size_value.width:
            return MeasurementComparison(
                status="different",
                reason=f"{component_name}_width_differs",
            )
        return MeasurementComparison(
            status="equivalent",
            reason=f"{component_name}_dimensions_equal",
        )
    if not isinstance(product_value, UnitValue) or not isinstance(
        size_value, UnitValue
    ):
        return MeasurementComparison(
            status="incomparable",
            reason=f"{component_name}_representation_unknown",
        )
    if product_value.value != size_value.value:
        return MeasurementComparison(
            status="different",
            reason=f"{component_name}_value_differs",
        )
    return MeasurementComparison(
        status="equivalent",
        reason=f"{component_name}_values_equal",
    )


def compare_measurement_equivalence(
    field_name: str,
    product_raw_value: str | None,
    size_value: NormalizedMeasurement | None,
) -> MeasurementComparison:
    """Compare supplied values exactly, without conversion or tolerance."""
    if product_raw_value is None or not product_raw_value.strip():
        return MeasurementComparison(
            status="missing_product",
            reason="product_measurement_missing",
        )
    if size_value is None:
        return MeasurementComparison(
            status="missing_size",
            reason="size_measurement_missing",
        )
    product_value, _ = parse_measurement_value(field_name, product_raw_value)
    if product_value is None:
        return MeasurementComparison(
            status="incomparable",
            reason="product_measurement_unparseable",
        )
    if product_value.metric is not None and size_value.metric is not None:
        return _compare_measurement_component(
            product_value.metric,
            size_value.metric,
            component_name="metric",
        )
    if product_value.imperial is not None and size_value.imperial is not None:
        return _compare_measurement_component(
            product_value.imperial,
            size_value.imperial,
            component_name="imperial",
        )
    return MeasurementComparison(
        status="incomparable",
        reason="no_common_comparable_unit",
    )


def _specification_conflicts(
    product: ProductRecord,
    size: SizeRecord | None,
) -> tuple[SpecificationConflict, ...]:
    if size is None:
        return ()
    measurement_fields = {field.name for field in fields(size.measurements)}
    conflicts: list[SpecificationConflict] = []
    for field_name in sorted(measurement_fields):
        product_value = product.specifications.normalized.get(field_name)
        if product_value is not None and not isinstance(product_value, str):
            continue
        size_value = getattr(size.measurements, field_name)
        if product_value is None and size_value is None:
            continue
        comparison = compare_measurement_equivalence(
            field_name,
            product_value,
            size_value,
        )
        if comparison.status == "different":
            assert product_value is not None and size_value is not None
            conflicts.append(
                SpecificationConflict(
                    field=field_name,
                    product_raw_value=product_value,
                    size_raw_value=size_value.raw_value,
                    comparison_reason=comparison.reason,
                )
            )
    return tuple(conflicts)


def _supplier_costs(
    product: ProductRecord,
    size: SizeRecord | None,
) -> EnrichedSupplierCosts:
    return EnrichedSupplierCosts(
        price_list_fob=product.supplier_costs.fob_unit_price,
        price_list_body_only_fob=product.supplier_costs.body_only_fob,
        price_list_including_head_fob=product.supplier_costs.including_head_fob,
        size_list_fob=(
            size.supplier_costs.fob_price if size is not None else None
        ),
    )


def _supplier_cost_conflict(
    costs: EnrichedSupplierCosts,
) -> SupplierCostConflict | None:
    price_list_fob = costs.price_list_fob
    size_list_fob = costs.size_list_fob
    if price_list_fob is None or size_list_fob is None:
        return None
    if (
        price_list_fob.amount == size_list_fob.amount
        and (price_list_fob.currency or "").casefold()
        == (size_list_fob.currency or "").casefold()
    ):
        return None
    return SupplierCostConflict(
        price_list_fob=price_list_fob,
        size_list_fob=size_list_fob,
    )


def enrich_products_with_sizes(
    products: Sequence[ProductRecord],
    sizes: Sequence[SizeRecord],
) -> list[ProductSizeMatchResult]:
    """Enrich products without mutating inputs or performing any I/O."""
    size_index: dict[str, list[_SizeCandidate]] = defaultdict(list)
    for position, size in enumerate(sizes):
        if not isinstance(size, SizeRecord):
            raise TypeError("sizes must contain only SizeRecord values")
        size_index[_comparison_key(size.identity.body_type)].append(
            _SizeCandidate(position=position, record=size)
        )

    results: list[ProductSizeMatchResult] = []
    for product in products:
        if not isinstance(product, ProductRecord):
            raise TypeError("products must contain only ProductRecord values")
        match, size = _match_product(product, size_index)
        costs = _supplier_costs(product, size)
        results.append(
            ProductSizeMatchResult(
                product=product,
                size=size,
                product_specifications=product.specifications,
                size_specifications=(
                    size.measurements if size is not None else None
                ),
                supplier_costs=costs,
                retail_pricing=product.retail_pricing,
                match=match,
                conflicts=_specification_conflicts(product, size),
                supplier_cost_conflict=_supplier_cost_conflict(costs),
            )
        )
    return results


def summarize_enrichment(
    results: Sequence[ProductSizeMatchResult],
) -> EnrichmentSummary:
    """Return deterministic counters for a future local dry-run report."""
    for result in results:
        if not isinstance(result, ProductSizeMatchResult):
            raise TypeError("results must contain only ProductSizeMatchResult values")
    return EnrichmentSummary(
        total_products=len(results),
        matched=sum(result.match.status == "matched" for result in results),
        unmatched=sum(result.match.status == "unmatched" for result in results),
        ambiguous=sum(result.match.status == "ambiguous" for result in results),
        exact_matches=sum(
            result.match.status == "matched" and result.match.method == "exact"
            for result in results
        ),
        suffix_matches=sum(
            result.match.status == "matched"
            and result.match.method == "verified_suffix_match"
            for result in results
        ),
        conflicts=sum(
            len(result.conflicts) + (result.supplier_cost_conflict is not None)
            for result in results
        ),
    )
