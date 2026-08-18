"""Versioned, human-approved mappings for CLM product upgrade options."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Literal

from .additional_option_parser import (
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
    OptionCategory,
)
from .product_model import UpgradeOptionRecord


REGISTRY_VERSION = "clm-option-map-v1"

APPROVED_ALIAS_MAPPINGS: Mapping[str, str] = MappingProxyType(
    {
        "Gel Butt": "凝胶屁股",
        "Hard Hands and Feet": "硬手硬脚(仅限硅胶)",
        "Hair Implant": "硅胶头植发",
    }
)

APPROVED_COMPOSITE_MAPPINGS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "Eyebrows/Eyelashes Implant": (
            "硅胶头植眉毛",
            "硅胶头植睫毛",
        )
    }
)


MappingType = Literal["exact_catalog", "alias", "composite"]
MappingStatus = Literal[
    "exact_catalog",
    "alias",
    "composite",
    "unmatched",
    "ambiguous",
    "incomplete_composite",
    "missing_component_price",
    "currency_conflict",
]


def canonical_option_key(value: str) -> str:
    """Return a strict equality key; this performs no semantic guessing."""

    if not isinstance(value, str):
        raise TypeError("option name must be text")
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _display_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


@dataclass(frozen=True, slots=True)
class OptionAliasMapping:
    product_upgrade_name: str
    catalog_option_name: str


@dataclass(frozen=True, slots=True)
class CompositeOptionMapping:
    product_upgrade_name: str
    catalog_option_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CombinedSupplierCost:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class OptionMappingComponent:
    option_name: str
    raw_option_name: str
    category: OptionCategory
    supplier_cost: AdditionalOptionPricing
    source: AdditionalOptionSource
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OptionMappingResolution:
    registry_version: str
    status: MappingStatus
    product_upgrade_name: str
    product_raw_value: str
    mapping_type: MappingType | None = None
    catalog_option_name: str | None = None
    catalog_raw_option: str | None = None
    category: OptionCategory | None = None
    supplier_cost: AdditionalOptionPricing | None = None
    source: AdditionalOptionSource | None = None
    components: tuple[OptionMappingComponent, ...] = ()
    combined_supplier_cost: CombinedSupplierCost | None = None
    pricing: None = None
    catalog_candidates: tuple[AdditionalOptionRecord, ...] = ()
    missing_component_names: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

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


def _component(record: AdditionalOptionRecord) -> OptionMappingComponent:
    return OptionMappingComponent(
        option_name=record.identity.option_name,
        raw_option_name=record.identity.raw_name,
        category=record.category,
        supplier_cost=record.pricing,
        source=record.source,
        warnings=record.warnings,
    )


def _decimal_amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


@dataclass(frozen=True, slots=True)
class OptionMappingRegistry:
    version: str
    aliases: tuple[OptionAliasMapping, ...]
    composites: tuple[CompositeOptionMapping, ...]

    @classmethod
    def from_mappings(
        cls,
        *,
        version: str,
        aliases: Mapping[str, str],
        composites: Mapping[str, Sequence[str]],
    ) -> OptionMappingRegistry:
        if not isinstance(version, str) or not version.strip():
            raise ValueError("registry version must be non-empty text")

        alias_entries: list[OptionAliasMapping] = []
        composite_entries: list[CompositeOptionMapping] = []
        seen_sources: set[str] = set()

        for product_name, catalog_name in aliases.items():
            product_display = _display_name(product_name)
            catalog_display = _display_name(catalog_name)
            if not product_display or not catalog_display:
                raise ValueError("alias mapping names must be non-empty text")
            source_key = canonical_option_key(product_display)
            if source_key in seen_sources:
                raise ValueError("mapping sources must be unique after normalization")
            seen_sources.add(source_key)
            alias_entries.append(
                OptionAliasMapping(product_display, catalog_display)
            )

        for product_name, catalog_names in composites.items():
            product_display = _display_name(product_name)
            if isinstance(catalog_names, (str, bytes)) or not isinstance(
                catalog_names, Sequence
            ):
                raise ValueError("composite targets must be a sequence of text")
            target_names = tuple(_display_name(name) for name in catalog_names)
            if not product_display or len(target_names) < 2 or not all(target_names):
                raise ValueError("composite mappings require two or more targets")
            target_keys = tuple(canonical_option_key(name) for name in target_names)
            if len(set(target_keys)) != len(target_keys):
                raise ValueError("composite targets must be unique")
            source_key = canonical_option_key(product_display)
            if source_key in seen_sources:
                raise ValueError("mapping sources must be unique after normalization")
            seen_sources.add(source_key)
            composite_entries.append(
                CompositeOptionMapping(product_display, target_names)
            )

        return cls(
            version=version.strip(),
            aliases=tuple(alias_entries),
            composites=tuple(composite_entries),
        )

    @classmethod
    def approved_v1(cls) -> OptionMappingRegistry:
        return cls.from_mappings(
            version=REGISTRY_VERSION,
            aliases=APPROVED_ALIAS_MAPPINGS,
            composites=APPROVED_COMPOSITE_MAPPINGS,
        )

    def resolve(
        self,
        product_upgrade: UpgradeOptionRecord | str,
        catalog: Sequence[AdditionalOptionRecord],
    ) -> OptionMappingResolution:
        if isinstance(product_upgrade, UpgradeOptionRecord):
            product_name = _display_name(product_upgrade.name)
            product_raw_value = product_upgrade.raw_value
        elif isinstance(product_upgrade, str):
            product_name = _display_name(product_upgrade)
            product_raw_value = product_upgrade
        else:
            raise TypeError("product_upgrade must be text or UpgradeOptionRecord")
        if not product_name:
            raise ValueError("product upgrade name must be non-empty text")

        catalog_index: dict[str, list[AdditionalOptionRecord]] = defaultdict(list)
        for record in catalog:
            if not isinstance(record, AdditionalOptionRecord):
                raise TypeError(
                    "catalog must contain only AdditionalOptionRecord values"
                )
            catalog_index[
                canonical_option_key(record.identity.option_name)
            ].append(record)

        exact_records = catalog_index.get(canonical_option_key(product_name), [])
        if len(exact_records) == 1:
            return self._single_resolution(
                status="exact_catalog",
                mapping_type="exact_catalog",
                product_name=product_name,
                product_raw_value=product_raw_value,
                record=exact_records[0],
            )
        if len(exact_records) > 1:
            return self._ambiguous_resolution(
                mapping_type="exact_catalog",
                product_name=product_name,
                product_raw_value=product_raw_value,
                records=exact_records,
            )

        product_key = canonical_option_key(product_name)
        alias = next(
            (
                entry
                for entry in self.aliases
                if canonical_option_key(entry.product_upgrade_name) == product_key
            ),
            None,
        )
        if alias is not None:
            records = catalog_index.get(
                canonical_option_key(alias.catalog_option_name), []
            )
            if len(records) == 1:
                return self._single_resolution(
                    status="alias",
                    mapping_type="alias",
                    product_name=product_name,
                    product_raw_value=product_raw_value,
                    record=records[0],
                )
            if len(records) > 1:
                return self._ambiguous_resolution(
                    mapping_type="alias",
                    product_name=product_name,
                    product_raw_value=product_raw_value,
                    records=records,
                )
            return OptionMappingResolution(
                registry_version=self.version,
                status="unmatched",
                mapping_type="alias",
                product_upgrade_name=product_name,
                product_raw_value=product_raw_value,
                missing_component_names=(alias.catalog_option_name,),
                warnings=("approved alias target was not found",),
            )

        composite = next(
            (
                entry
                for entry in self.composites
                if canonical_option_key(entry.product_upgrade_name) == product_key
            ),
            None,
        )
        if composite is not None:
            return self._resolve_composite(
                product_name,
                product_raw_value,
                composite,
                catalog_index,
            )

        return OptionMappingResolution(
            registry_version=self.version,
            status="unmatched",
            product_upgrade_name=product_name,
            product_raw_value=product_raw_value,
            warnings=("no deterministic catalog option mapping",),
        )

    def _single_resolution(
        self,
        *,
        status: Literal["exact_catalog", "alias"],
        mapping_type: Literal["exact_catalog", "alias"],
        product_name: str,
        product_raw_value: str,
        record: AdditionalOptionRecord,
    ) -> OptionMappingResolution:
        return OptionMappingResolution(
            registry_version=self.version,
            status=status,
            mapping_type=mapping_type,
            product_upgrade_name=product_name,
            product_raw_value=product_raw_value,
            catalog_option_name=record.identity.option_name,
            catalog_raw_option=record.identity.raw_name,
            category=record.category,
            supplier_cost=record.pricing,
            source=record.source,
            catalog_candidates=(record,),
            warnings=record.warnings,
        )

    def _ambiguous_resolution(
        self,
        *,
        mapping_type: MappingType,
        product_name: str,
        product_raw_value: str,
        records: Sequence[AdditionalOptionRecord],
    ) -> OptionMappingResolution:
        return OptionMappingResolution(
            registry_version=self.version,
            status="ambiguous",
            mapping_type=mapping_type,
            product_upgrade_name=product_name,
            product_raw_value=product_raw_value,
            catalog_candidates=tuple(records),
            warnings=("multiple catalog options matched an approved mapping",),
        )

    def _resolve_composite(
        self,
        product_name: str,
        product_raw_value: str,
        mapping: CompositeOptionMapping,
        catalog_index: Mapping[str, Sequence[AdditionalOptionRecord]],
    ) -> OptionMappingResolution:
        matched_records: list[AdditionalOptionRecord] = []
        ambiguous_records: list[AdditionalOptionRecord] = []
        missing_names: list[str] = []
        for component_name in mapping.catalog_option_names:
            records = list(
                catalog_index.get(canonical_option_key(component_name), ())
            )
            if not records:
                missing_names.append(component_name)
            elif len(records) > 1:
                ambiguous_records.extend(records)
            else:
                matched_records.append(records[0])

        if ambiguous_records:
            return self._ambiguous_resolution(
                mapping_type="composite",
                product_name=product_name,
                product_raw_value=product_raw_value,
                records=ambiguous_records,
            )
        if missing_names:
            return OptionMappingResolution(
                registry_version=self.version,
                status="incomplete_composite",
                mapping_type="composite",
                product_upgrade_name=product_name,
                product_raw_value=product_raw_value,
                components=tuple(_component(record) for record in matched_records),
                catalog_candidates=tuple(matched_records),
                missing_component_names=tuple(missing_names),
                warnings=("approved composite mapping is incomplete",),
            )

        components = tuple(_component(record) for record in matched_records)
        amounts = tuple(
            _decimal_amount(record.pricing.amount) for record in matched_records
        )
        currencies = tuple(
            (record.pricing.currency or "").strip().upper()
            for record in matched_records
        )
        common = {
            "registry_version": self.version,
            "mapping_type": "composite",
            "product_upgrade_name": product_name,
            "product_raw_value": product_raw_value,
            "components": components,
            "catalog_candidates": tuple(matched_records),
        }
        if any(amount is None for amount in amounts) or any(
            not currency for currency in currencies
        ):
            return OptionMappingResolution(
                status="missing_component_price",
                warnings=("composite component supplier price is missing",),
                **common,
            )
        if len(set(currencies)) != 1:
            return OptionMappingResolution(
                status="currency_conflict",
                warnings=("composite component currencies do not match",),
                **common,
            )

        combined = sum(
            (amount for amount in amounts if amount is not None),
            Decimal("0"),
        )
        return OptionMappingResolution(
            status="composite",
            combined_supplier_cost=CombinedSupplierCost(
                amount=combined,
                currency=currencies[0],
            ),
            **common,
        )


APPROVED_OPTION_MAPPING_REGISTRY = OptionMappingRegistry.approved_v1()
