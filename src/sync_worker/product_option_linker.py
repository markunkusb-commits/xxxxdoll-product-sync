"""Pure-local, deterministic ProductRecord + AdditionalOptionRecord linking."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from .additional_option_parser import (
    AdditionalOptionIdentity,
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
    OptionCategory,
)
from .option_mapping_registry import (
    OptionMappingRegistry,
    OptionMappingResolution,
)
from .product_model import (
    ProductIdentity,
    ProductRecord,
    ProductSource,
    RetailPricing,
    UpgradeOptionRecord,
)


OptionMatchMethod = Literal["exact", "approved_alias", "approved_composite"]


def _display_name(value: str) -> str:
    return " ".join(value.split())


def _comparison_key(value: str) -> str:
    return _display_name(value).casefold()


@dataclass(frozen=True, slots=True)
class OptionAliasRegistry:
    """Explicit, auditable aliases; the default registry is empty."""

    entries: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def from_mapping(
        cls,
        aliases: Mapping[str, Sequence[str]],
    ) -> OptionAliasRegistry:
        normalized_entries: list[tuple[str, tuple[str, ...]]] = []
        seen_sources: set[str] = set()
        for source, raw_targets in aliases.items():
            if not isinstance(source, str) or not source.strip():
                raise ValueError("Alias source must be non-empty text")
            if isinstance(raw_targets, (str, bytes)) or not isinstance(
                raw_targets, Sequence
            ):
                raise ValueError("Alias targets must be a sequence of text")
            source_key = _comparison_key(source)
            if source_key in seen_sources:
                raise ValueError("Alias sources must be unique after normalization")
            seen_sources.add(source_key)
            targets: list[str] = []
            seen_targets: set[str] = set()
            for target in raw_targets:
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("Alias target must be non-empty text")
                target_key = _comparison_key(target)
                if target_key not in seen_targets:
                    seen_targets.add(target_key)
                    targets.append(target_key)
            normalized_entries.append((source_key, tuple(targets)))
        return cls(entries=tuple(normalized_entries))

    def targets_for(self, value: str) -> tuple[str, ...]:
        key = _comparison_key(value)
        for source, targets in self.entries:
            if source == key:
                return targets
        return ()


@dataclass(frozen=True, slots=True)
class LinkedUpgradeOption:
    product_raw_option: str
    product_option: UpgradeOptionRecord
    matched_catalog_option: AdditionalOptionIdentity
    category: OptionCategory
    pricing: AdditionalOptionPricing
    pricing_source: AdditionalOptionSource
    match_method: OptionMatchMethod
    warnings: tuple[str, ...]
    registry_version: str | None = None


@dataclass(frozen=True, slots=True)
class UnmatchedUpgradeOption:
    product_raw_option: str
    product_option: UpgradeOptionRecord
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AmbiguousUpgradeOption:
    product_raw_option: str
    product_option: UpgradeOptionRecord
    catalog_candidates: tuple[AdditionalOptionRecord, ...]
    match_method: OptionMatchMethod
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncludedUpgradeConflict:
    product_raw_option: str
    product_option: UpgradeOptionRecord
    included_features: tuple[str, ...]
    warning: str = "option appears as both included and upgrade"


@dataclass(frozen=True, slots=True)
class ProductOptionLinkResult:
    product_identity: ProductIdentity
    series: str
    included_features: tuple[str, ...]
    linked_upgrade_options: tuple[LinkedUpgradeOption, ...]
    unmatched_upgrade_options: tuple[UnmatchedUpgradeOption, ...]
    ambiguous_upgrade_options: tuple[AmbiguousUpgradeOption, ...]
    included_upgrade_conflicts: tuple[IncludedUpgradeConflict, ...]
    warnings: tuple[str, ...]
    source: ProductSource
    retail_pricing: RetailPricing
    mapping_resolutions: tuple[OptionMappingResolution, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OptionLinkingSummary:
    total_products: int
    products_with_upgrade_options: int
    linked_options: int
    unmatched_options: int
    ambiguous_options: int
    included_features_count: int
    conflicts: int
    products_without_options: int


@dataclass(frozen=True, slots=True)
class _CatalogCandidate:
    position: int
    record: AdditionalOptionRecord


def _unique_catalog_candidates(
    keys: Sequence[str],
    catalog_index: Mapping[str, Sequence[_CatalogCandidate]],
) -> list[_CatalogCandidate]:
    candidates: list[_CatalogCandidate] = []
    seen_positions: set[int] = set()
    for key in keys:
        for candidate in catalog_index.get(key, ()):
            if candidate.position in seen_positions:
                continue
            seen_positions.add(candidate.position)
            candidates.append(candidate)
    return candidates


def _semantic_keys(name: str, aliases: OptionAliasRegistry) -> frozenset[str]:
    key = _comparison_key(name)
    return frozenset((key, *aliases.targets_for(name)))


def _included_conflicts(
    upgrade: UpgradeOptionRecord,
    included_features: Sequence[str],
    aliases: OptionAliasRegistry,
) -> tuple[str, ...]:
    upgrade_keys = _semantic_keys(upgrade.name, aliases)
    return tuple(
        included_feature
        for included_feature in included_features
        if upgrade_keys.intersection(
            _semantic_keys(included_feature, aliases)
        )
    )


def _linked_option(
    upgrade: UpgradeOptionRecord,
    candidate: _CatalogCandidate,
    *,
    method: OptionMatchMethod,
    registry_version: str | None = None,
) -> LinkedUpgradeOption:
    record = candidate.record
    return LinkedUpgradeOption(
        product_raw_option=upgrade.raw_value,
        product_option=upgrade,
        matched_catalog_option=record.identity,
        category=record.category,
        pricing=record.pricing,
        pricing_source=record.source,
        match_method=method,
        warnings=record.warnings,
        registry_version=registry_version,
    )


def link_products_to_options(
    products: Sequence[ProductRecord],
    catalog: Sequence[AdditionalOptionRecord],
    *,
    alias_registry: OptionAliasRegistry | None = None,
    mapping_registry: OptionMappingRegistry | None = None,
) -> list[ProductOptionLinkResult]:
    """Link only explicit product upgrades; perform no I/O or price math."""
    aliases = alias_registry or OptionAliasRegistry()
    if not isinstance(aliases, OptionAliasRegistry):
        raise TypeError("alias_registry must be an OptionAliasRegistry")
    if mapping_registry is not None and not isinstance(
        mapping_registry, OptionMappingRegistry
    ):
        raise TypeError("mapping_registry must be an OptionMappingRegistry")

    catalog_index: dict[str, list[_CatalogCandidate]] = defaultdict(list)
    for position, option in enumerate(catalog):
        if not isinstance(option, AdditionalOptionRecord):
            raise TypeError("catalog must contain only AdditionalOptionRecord values")
        catalog_index[_comparison_key(option.identity.option_name)].append(
            _CatalogCandidate(position=position, record=option)
        )

    results: list[ProductOptionLinkResult] = []
    for product in products:
        if not isinstance(product, ProductRecord):
            raise TypeError("products must contain only ProductRecord values")
        linked: list[LinkedUpgradeOption] = []
        unmatched: list[UnmatchedUpgradeOption] = []
        ambiguous: list[AmbiguousUpgradeOption] = []
        conflicts: list[IncludedUpgradeConflict] = []
        mapping_resolutions: list[OptionMappingResolution] = []
        result_warnings: list[str] = list(product.warnings)

        for upgrade in product.options.upgrade_options:
            included_matches = _included_conflicts(
                upgrade,
                product.included_features,
                aliases,
            )
            if included_matches:
                conflict = IncludedUpgradeConflict(
                    product_raw_option=upgrade.raw_value,
                    product_option=upgrade,
                    included_features=included_matches,
                )
                conflicts.append(conflict)
                result_warnings.append(conflict.warning)
                continue

            exact_key = _comparison_key(upgrade.name)
            exact_candidates = _unique_catalog_candidates(
                [exact_key], catalog_index
            )
            if len(exact_candidates) == 1:
                linked.append(
                    _linked_option(upgrade, exact_candidates[0], method="exact")
                )
                continue
            if len(exact_candidates) > 1:
                candidate_records = tuple(
                    candidate.record for candidate in exact_candidates
                )
                candidate_warnings = tuple(
                    dict.fromkeys(
                        warning
                        for candidate in candidate_records
                        for warning in candidate.warnings
                    )
                )
                ambiguous.append(
                    AmbiguousUpgradeOption(
                        product_raw_option=upgrade.raw_value,
                        product_option=upgrade,
                        catalog_candidates=candidate_records,
                        match_method="exact",
                        warnings=(
                            *candidate_warnings,
                            "multiple catalog options matched",
                        ),
                    )
                )
                continue

            if mapping_registry is not None:
                resolution = mapping_registry.resolve(upgrade, catalog)
                mapping_resolutions.append(resolution)
                if resolution.status in {"exact_catalog", "alias"}:
                    matched_record = resolution.catalog_candidates[0]
                    linked.append(
                        _linked_option(
                            upgrade,
                            _CatalogCandidate(position=-1, record=matched_record),
                            method=(
                                "exact"
                                if resolution.status == "exact_catalog"
                                else "approved_alias"
                            ),
                            registry_version=resolution.registry_version,
                        )
                    )
                    continue
                if resolution.status == "ambiguous":
                    candidate_warnings = tuple(
                        dict.fromkeys(
                            warning
                            for candidate in resolution.catalog_candidates
                            for warning in candidate.warnings
                        )
                    )
                    ambiguous.append(
                        AmbiguousUpgradeOption(
                            product_raw_option=upgrade.raw_value,
                            product_option=upgrade,
                            catalog_candidates=resolution.catalog_candidates,
                            match_method=(
                                "approved_composite"
                                if resolution.mapping_type == "composite"
                                else "approved_alias"
                            ),
                            warnings=tuple(
                                dict.fromkeys(
                                    (*candidate_warnings, *resolution.warnings)
                                )
                            ),
                        )
                    )
                    continue
                if resolution.status in {
                    "composite",
                    "missing_component_price",
                    "currency_conflict",
                }:
                    continue
                unmatched.append(
                    UnmatchedUpgradeOption(
                        product_raw_option=upgrade.raw_value,
                        product_option=upgrade,
                        warnings=resolution.warnings,
                    )
                )
                continue

            alias_targets = aliases.targets_for(upgrade.name)
            alias_candidates = _unique_catalog_candidates(
                alias_targets, catalog_index
            )
            if len(alias_candidates) == 1:
                linked.append(
                    _linked_option(
                        upgrade,
                        alias_candidates[0],
                        method="approved_alias",
                    )
                )
                continue
            if len(alias_candidates) > 1:
                candidate_records = tuple(
                    candidate.record for candidate in alias_candidates
                )
                candidate_warnings = tuple(
                    dict.fromkeys(
                        warning
                        for candidate in candidate_records
                        for warning in candidate.warnings
                    )
                )
                ambiguous.append(
                    AmbiguousUpgradeOption(
                        product_raw_option=upgrade.raw_value,
                        product_option=upgrade,
                        catalog_candidates=candidate_records,
                        match_method="approved_alias",
                        warnings=(
                            *candidate_warnings,
                            "multiple catalog options matched",
                        ),
                    )
                )
                continue

            unmatched.append(
                UnmatchedUpgradeOption(
                    product_raw_option=upgrade.raw_value,
                    product_option=upgrade,
                    warnings=("no deterministic catalog option match",),
                )
            )

        results.append(
            ProductOptionLinkResult(
                product_identity=product.identity,
                series=product.identity.series,
                included_features=tuple(product.included_features),
                linked_upgrade_options=tuple(linked),
                unmatched_upgrade_options=tuple(unmatched),
                ambiguous_upgrade_options=tuple(ambiguous),
                included_upgrade_conflicts=tuple(conflicts),
                warnings=tuple(dict.fromkeys(result_warnings)),
                source=product.source,
                retail_pricing=product.retail_pricing,
                mapping_resolutions=tuple(mapping_resolutions),
            )
        )
    return results


def summarize_option_linking(
    results: Sequence[ProductOptionLinkResult],
) -> OptionLinkingSummary:
    """Build deterministic counters for a future local reality check."""
    for result in results:
        if not isinstance(result, ProductOptionLinkResult):
            raise TypeError("results must contain only ProductOptionLinkResult values")
    return OptionLinkingSummary(
        total_products=len(results),
        products_with_upgrade_options=sum(
            bool(
                result.linked_upgrade_options
                or result.unmatched_upgrade_options
                or result.ambiguous_upgrade_options
                or result.included_upgrade_conflicts
                or result.mapping_resolutions
            )
            for result in results
        ),
        linked_options=sum(
            len(result.linked_upgrade_options)
            + sum(
                resolution.status == "composite"
                for resolution in result.mapping_resolutions
            )
            for result in results
        ),
        unmatched_options=sum(
            len(result.unmatched_upgrade_options) for result in results
        ),
        ambiguous_options=sum(
            len(result.ambiguous_upgrade_options) for result in results
        ),
        included_features_count=sum(
            len(result.included_features) for result in results
        ),
        conflicts=sum(
            len(result.included_upgrade_conflicts) for result in results
        ),
        products_without_options=sum(
            not (
                result.linked_upgrade_options
                or result.unmatched_upgrade_options
                or result.ambiguous_upgrade_options
                or result.included_upgrade_conflicts
                or result.mapping_resolutions
            )
            for result in results
        ),
    )
