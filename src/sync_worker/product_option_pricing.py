"""Pure Product Option pricing enrichment over deterministic Linker results."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal

from . import option_pricing_policy
from .additional_option_parser import AdditionalOptionPricing, OptionCategory
from .option_mapping_registry import OptionMappingResolution
from .option_pricing_policy import (
    OptionRetailPricingResult,
    UnsupportedOptionCurrencyError,
)
from .product_model import (
    ProductIdentity,
    ProductSource,
    RetailPricing,
)
from .product_option_linker import (
    AmbiguousUpgradeOption,
    LinkedUpgradeOption,
    ProductOptionLinkResult,
    UnmatchedUpgradeOption,
)


PricingMappingType = Literal["exact", "alias", "composite"]
UnpricedStatus = Literal[
    "no_supplier_price",
    "unsupported_currency",
    "mapping_not_priceable",
]

_NON_PRICEABLE_MAPPING_STATUSES = frozenset(
    {
        "incomplete_composite",
        "ambiguous",
        "currency_conflict",
        "missing_component_price",
    }
)


@dataclass(frozen=True, slots=True)
class SupplierCostProvenance:
    amount: Decimal | int | float | None
    currency: str | None
    raw_values: tuple[str, ...]
    source_coordinates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PricedMappingComponent:
    option_name: str
    category: OptionCategory
    supplier_cost: AdditionalOptionPricing
    source_coordinate: str


@dataclass(frozen=True, slots=True)
class PricingMappingSnapshot:
    mapping_type: PricingMappingType | None
    mapping_status: str
    registry_version: str | None
    catalog_option_name: str | None
    catalog_category: OptionCategory | None
    components: tuple[PricedMappingComponent, ...]
    candidate_option_names: tuple[str, ...]
    missing_component_names: tuple[str, ...]
    source_coordinates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PricedLinkedOption:
    product_upgrade_name: str
    product_raw_value: str
    mapping: PricingMappingSnapshot
    supplier_cost: SupplierCostProvenance
    pricing: OptionRetailPricingResult
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnpricedLinkedOption:
    product_upgrade_name: str
    product_raw_value: str
    mapping: PricingMappingSnapshot
    supplier_cost: SupplierCostProvenance | None
    status: UnpricedStatus
    pricing: OptionRetailPricingResult | None
    unavailable_reason: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PricedProductOptionResult:
    product_identity: ProductIdentity
    series: str
    included_features: tuple[str, ...]
    priced_upgrade_options: tuple[PricedLinkedOption, ...]
    unpriced_upgrade_options: tuple[UnpricedLinkedOption, ...]
    warnings: tuple[str, ...]
    source: ProductSource
    retail_pricing: RetailPricing

    def to_dict(self) -> dict[str, object]:
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class ProductOptionPricingSummary:
    total_products: int
    products_with_priced_options: int
    products_without_priced_options: int
    total_linked_options: int
    priced_options: int
    unpriced_options: int
    exact_priced: int
    alias_priced: int
    composite_priced: int
    no_supplier_price: int
    unsupported_currency: int
    mapping_not_priceable: int
    total_option_retail_usd: Decimal

    def to_dict(self) -> dict[str, object]:
        return _json_safe(asdict(self))


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _simple_mapping(linked: LinkedUpgradeOption) -> PricingMappingSnapshot:
    mapping_type: PricingMappingType = (
        "alias" if linked.match_method == "approved_alias" else "exact"
    )
    return PricingMappingSnapshot(
        mapping_type=mapping_type,
        mapping_status=("alias" if mapping_type == "alias" else "exact_catalog"),
        registry_version=linked.registry_version,
        catalog_option_name=linked.matched_catalog_option.option_name,
        catalog_category=linked.category,
        components=(),
        candidate_option_names=(linked.matched_catalog_option.option_name,),
        missing_component_names=(),
        source_coordinates=(linked.pricing_source.raw_coordinate,),
    )


def _simple_supplier_cost(
    linked: LinkedUpgradeOption,
) -> SupplierCostProvenance:
    raw_values = (
        (linked.pricing.raw_price,) if linked.pricing.raw_price is not None else ()
    )
    return SupplierCostProvenance(
        amount=linked.pricing.amount,
        currency=linked.pricing.currency,
        raw_values=raw_values,
        source_coordinates=(linked.pricing_source.raw_coordinate,),
    )


def _composite_mapping(
    resolution: OptionMappingResolution,
) -> PricingMappingSnapshot:
    components = tuple(
        PricedMappingComponent(
            option_name=component.option_name,
            category=component.category,
            supplier_cost=component.supplier_cost,
            source_coordinate=component.source.raw_coordinate,
        )
        for component in resolution.components
    )
    component_coordinates = tuple(
        component.source_coordinate for component in components
    )
    return PricingMappingSnapshot(
        mapping_type="composite",
        mapping_status=resolution.status,
        registry_version=resolution.registry_version,
        catalog_option_name=None,
        catalog_category=None,
        components=components,
        candidate_option_names=tuple(
            candidate.identity.option_name
            for candidate in resolution.catalog_candidates
        ),
        missing_component_names=resolution.missing_component_names,
        source_coordinates=(
            component_coordinates
            or tuple(
                candidate.source.raw_coordinate
                for candidate in resolution.catalog_candidates
            )
        ),
    )


def _resolution_mapping(
    resolution: OptionMappingResolution,
) -> PricingMappingSnapshot:
    if resolution.mapping_type == "composite":
        return _composite_mapping(resolution)
    mapping_type: PricingMappingType | None = (
        "alias"
        if resolution.mapping_type == "alias"
        else "exact"
        if resolution.mapping_type == "exact_catalog"
        else None
    )
    return PricingMappingSnapshot(
        mapping_type=mapping_type,
        mapping_status=resolution.status,
        registry_version=resolution.registry_version,
        catalog_option_name=resolution.catalog_option_name,
        catalog_category=resolution.category,
        components=(),
        candidate_option_names=tuple(
            candidate.identity.option_name
            for candidate in resolution.catalog_candidates
        ),
        missing_component_names=resolution.missing_component_names,
        source_coordinates=tuple(
            candidate.source.raw_coordinate
            for candidate in resolution.catalog_candidates
        ),
    )


def _composite_supplier_cost(
    resolution: OptionMappingResolution,
) -> tuple[SupplierCostProvenance, AdditionalOptionPricing] | None:
    combined = resolution.combined_supplier_cost
    if combined is None:
        return None
    raw_values = tuple(
        component.supplier_cost.raw_price
        for component in resolution.components
        if component.supplier_cost.raw_price is not None
    )
    source_coordinates = tuple(
        component.source.raw_coordinate for component in resolution.components
    )
    provenance = SupplierCostProvenance(
        amount=combined.amount,
        currency=combined.currency,
        raw_values=raw_values,
        source_coordinates=source_coordinates,
    )
    policy_input = AdditionalOptionPricing(
        amount=combined.amount,
        currency=combined.currency,
        raw_price=None,
    )
    return provenance, policy_input


def _price_candidate(
    *,
    product_upgrade_name: str,
    product_raw_value: str,
    mapping: PricingMappingSnapshot,
    supplier_cost: SupplierCostProvenance,
    policy_input: AdditionalOptionPricing,
    rmb_to_usd_rate: Decimal,
    warnings: tuple[str, ...],
) -> PricedLinkedOption | UnpricedLinkedOption:
    try:
        pricing = option_pricing_policy.calculate_option_retail_price(
            policy_input,
            rmb_to_usd_rate=rmb_to_usd_rate,
        )
    except UnsupportedOptionCurrencyError as error:
        return UnpricedLinkedOption(
            product_upgrade_name=product_upgrade_name,
            product_raw_value=product_raw_value,
            mapping=mapping,
            supplier_cost=supplier_cost,
            status="unsupported_currency",
            pricing=None,
            unavailable_reason="unsupported supplier cost currency",
            warnings=tuple(dict.fromkeys((*warnings, str(error)))),
        )

    if pricing.status == "no_supplier_price":
        return UnpricedLinkedOption(
            product_upgrade_name=product_upgrade_name,
            product_raw_value=product_raw_value,
            mapping=mapping,
            supplier_cost=supplier_cost,
            status="no_supplier_price",
            pricing=pricing,
            unavailable_reason="supplier cost is missing",
            warnings=warnings,
        )
    return PricedLinkedOption(
        product_upgrade_name=product_upgrade_name,
        product_raw_value=product_raw_value,
        mapping=mapping,
        supplier_cost=supplier_cost,
        pricing=pricing,
        warnings=warnings,
    )


def _price_simple_option(
    linked: LinkedUpgradeOption,
    *,
    rmb_to_usd_rate: Decimal,
) -> PricedLinkedOption | UnpricedLinkedOption:
    return _price_candidate(
        product_upgrade_name=linked.product_option.name,
        product_raw_value=linked.product_raw_option,
        mapping=_simple_mapping(linked),
        supplier_cost=_simple_supplier_cost(linked),
        policy_input=linked.pricing,
        rmb_to_usd_rate=rmb_to_usd_rate,
        warnings=linked.warnings,
    )


def _price_composite_option(
    resolution: OptionMappingResolution,
    *,
    rmb_to_usd_rate: Decimal,
) -> PricedLinkedOption | UnpricedLinkedOption:
    combined = _composite_supplier_cost(resolution)
    mapping = _composite_mapping(resolution)
    if combined is None:
        return UnpricedLinkedOption(
            product_upgrade_name=resolution.product_upgrade_name,
            product_raw_value=resolution.product_raw_value,
            mapping=mapping,
            supplier_cost=None,
            status="mapping_not_priceable",
            pricing=None,
            unavailable_reason=f"mapping status is {resolution.status}",
            warnings=resolution.warnings,
        )
    supplier_cost, policy_input = combined
    return _price_candidate(
        product_upgrade_name=resolution.product_upgrade_name,
        product_raw_value=resolution.product_raw_value,
        mapping=mapping,
        supplier_cost=supplier_cost,
        policy_input=policy_input,
        rmb_to_usd_rate=rmb_to_usd_rate,
        warnings=resolution.warnings,
    )


def _unpriceable_resolution(
    resolution: OptionMappingResolution,
) -> UnpricedLinkedOption:
    combined = _composite_supplier_cost(resolution)
    return UnpricedLinkedOption(
        product_upgrade_name=resolution.product_upgrade_name,
        product_raw_value=resolution.product_raw_value,
        mapping=_resolution_mapping(resolution),
        supplier_cost=combined[0] if combined is not None else None,
        status="mapping_not_priceable",
        pricing=None,
        unavailable_reason=f"mapping status is {resolution.status}",
        warnings=resolution.warnings,
    )


def _ambiguous_unpriced(
    ambiguous: AmbiguousUpgradeOption,
) -> UnpricedLinkedOption:
    mapping_type: PricingMappingType = (
        "composite"
        if ambiguous.match_method == "approved_composite"
        else "alias"
        if ambiguous.match_method == "approved_alias"
        else "exact"
    )
    coordinates = tuple(
        candidate.source.raw_coordinate
        for candidate in ambiguous.catalog_candidates
    )
    return UnpricedLinkedOption(
        product_upgrade_name=ambiguous.product_option.name,
        product_raw_value=ambiguous.product_raw_option,
        mapping=PricingMappingSnapshot(
            mapping_type=mapping_type,
            mapping_status="ambiguous",
            registry_version=None,
            catalog_option_name=None,
            catalog_category=None,
            components=(),
            candidate_option_names=tuple(
                candidate.identity.option_name
                for candidate in ambiguous.catalog_candidates
            ),
            missing_component_names=(),
            source_coordinates=coordinates,
        ),
        supplier_cost=None,
        status="mapping_not_priceable",
        pricing=None,
        unavailable_reason="mapping status is ambiguous",
        warnings=ambiguous.warnings,
    )


def _unmatched_unpriced(
    unmatched: UnmatchedUpgradeOption,
) -> UnpricedLinkedOption:
    return UnpricedLinkedOption(
        product_upgrade_name=unmatched.product_option.name,
        product_raw_value=unmatched.product_raw_option,
        mapping=PricingMappingSnapshot(
            mapping_type=None,
            mapping_status="unmatched",
            registry_version=None,
            catalog_option_name=None,
            catalog_category=None,
            components=(),
            candidate_option_names=(),
            missing_component_names=(),
            source_coordinates=(),
        ),
        supplier_cost=None,
        status="mapping_not_priceable",
        pricing=None,
        unavailable_reason="upgrade option has no deterministic mapping",
        warnings=unmatched.warnings,
    )


def enrich_product_option_pricing(
    results: Sequence[ProductOptionLinkResult],
    *,
    rmb_to_usd_rate: Decimal,
) -> list[PricedProductOptionResult]:
    """Price already-linked options without matching, I/O, or base-price math."""

    if not isinstance(rmb_to_usd_rate, Decimal):
        raise TypeError("rmb_to_usd_rate must be a Decimal")

    enriched: list[PricedProductOptionResult] = []
    for result in results:
        if not isinstance(result, ProductOptionLinkResult):
            raise TypeError(
                "results must contain only ProductOptionLinkResult values"
            )
        priced: list[PricedLinkedOption] = []
        unpriced: list[UnpricedLinkedOption] = []

        for linked in result.linked_upgrade_options:
            candidate = _price_simple_option(
                linked,
                rmb_to_usd_rate=rmb_to_usd_rate,
            )
            (priced if isinstance(candidate, PricedLinkedOption) else unpriced).append(
                candidate
            )

        handled_raw_values: set[str] = set()
        for resolution in result.mapping_resolutions:
            if resolution.status == "composite":
                candidate = _price_composite_option(
                    resolution,
                    rmb_to_usd_rate=rmb_to_usd_rate,
                )
                (priced if isinstance(candidate, PricedLinkedOption) else unpriced).append(
                    candidate
                )
                handled_raw_values.add(resolution.product_raw_value)
            elif resolution.status in _NON_PRICEABLE_MAPPING_STATUSES:
                unpriced.append(_unpriceable_resolution(resolution))
                handled_raw_values.add(resolution.product_raw_value)

        for ambiguous in result.ambiguous_upgrade_options:
            if ambiguous.product_raw_option not in handled_raw_values:
                unpriced.append(_ambiguous_unpriced(ambiguous))
                handled_raw_values.add(ambiguous.product_raw_option)
        for unmatched in result.unmatched_upgrade_options:
            if unmatched.product_raw_option not in handled_raw_values:
                unpriced.append(_unmatched_unpriced(unmatched))
                handled_raw_values.add(unmatched.product_raw_option)

        enriched.append(
            PricedProductOptionResult(
                product_identity=result.product_identity,
                series=result.series,
                included_features=result.included_features,
                priced_upgrade_options=tuple(priced),
                unpriced_upgrade_options=tuple(unpriced),
                warnings=result.warnings,
                source=result.source,
                retail_pricing=result.retail_pricing,
            )
        )
    return enriched


def summarize_product_option_pricing(
    results: Sequence[PricedProductOptionResult],
) -> ProductOptionPricingSummary:
    for result in results:
        if not isinstance(result, PricedProductOptionResult):
            raise TypeError(
                "results must contain only PricedProductOptionResult values"
            )
    priced_options = sum(len(result.priced_upgrade_options) for result in results)
    unpriced_options = sum(
        len(result.unpriced_upgrade_options) for result in results
    )
    total_linked_options = sum(
        option.mapping.mapping_type is not None
        for result in results
        for option in (
            *result.priced_upgrade_options,
            *result.unpriced_upgrade_options,
        )
    )
    total_retail = sum(
        (
            option.pricing.retail.target_retail_usd
            for result in results
            for option in result.priced_upgrade_options
            if option.pricing.retail is not None
        ),
        Decimal("0"),
    )
    return ProductOptionPricingSummary(
        total_products=len(results),
        products_with_priced_options=sum(
            bool(result.priced_upgrade_options) for result in results
        ),
        products_without_priced_options=sum(
            not result.priced_upgrade_options for result in results
        ),
        total_linked_options=total_linked_options,
        priced_options=priced_options,
        unpriced_options=unpriced_options,
        exact_priced=sum(
            option.mapping.mapping_type == "exact"
            for result in results
            for option in result.priced_upgrade_options
        ),
        alias_priced=sum(
            option.mapping.mapping_type == "alias"
            for result in results
            for option in result.priced_upgrade_options
        ),
        composite_priced=sum(
            option.mapping.mapping_type == "composite"
            for result in results
            for option in result.priced_upgrade_options
        ),
        no_supplier_price=sum(
            option.status == "no_supplier_price"
            for result in results
            for option in result.unpriced_upgrade_options
        ),
        unsupported_currency=sum(
            option.status == "unsupported_currency"
            for result in results
            for option in result.unpriced_upgrade_options
        ),
        mapping_not_priceable=sum(
            option.status == "mapping_not_priceable"
            for result in results
            for option in result.unpriced_upgrade_options
        ),
        total_option_retail_usd=total_retail,
    )
