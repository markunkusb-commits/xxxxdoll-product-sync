"""Product Intermediate Model between supplier parsers and future mappers."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .clm_price_parser import (
    CLMProductBlock,
    ParsedPrice,
    RawCommercialEntry,
    RawSpecification,
    UpgradeOption,
)


@dataclass(frozen=True, slots=True)
class MonetaryValue:
    """Traceable monetary value without assigning a business role."""

    raw_value: str
    currency: str | None
    amount: int | float | None
    context: str


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    series: str
    model: str | None
    raw_series_title: str
    raw_model: str | None


@dataclass(frozen=True, slots=True)
class RawSpecificationRecord:
    field: str
    value: str
    field_coordinate: str
    value_coordinate: str


@dataclass(frozen=True, slots=True)
class ProductSpecifications:
    normalized: dict[str, str]
    raw: tuple[RawSpecificationRecord, ...]


@dataclass(frozen=True, slots=True)
class SupplierCosts:
    """Supplier-side costs; these must never be treated as retail pricing."""

    fob_unit_price: MonetaryValue | None
    body_only_fob: MonetaryValue | None
    including_head_fob: MonetaryValue | None


@dataclass(frozen=True, slots=True)
class RetailPricing:
    minimum_retail_price: MonetaryValue | None


@dataclass(frozen=True, slots=True)
class UpgradeOptionRecord:
    name: str
    raw_value: str
    supplier_cost: MonetaryValue | None


@dataclass(frozen=True, slots=True)
class ProductOptions:
    normal_options_price: MonetaryValue | None
    upgrade_options: tuple[UpgradeOptionRecord, ...]


@dataclass(frozen=True, slots=True)
class ProductMedia:
    photo_download_link: str | None


@dataclass(frozen=True, slots=True)
class ProductSource:
    start_row: int
    end_row: int


@dataclass(frozen=True, slots=True)
class RawCommercialField:
    field: str | None
    value: str
    coordinate: str


@dataclass(frozen=True, slots=True)
class UnknownFields:
    raw_commercial_entries: tuple[RawCommercialField, ...]


@dataclass(frozen=True, slots=True)
class ProductRecord:
    """Supplier-neutral product record for a future WooCommerce mapper."""

    identity: ProductIdentity
    specifications: ProductSpecifications
    supplier_costs: SupplierCosts
    retail_pricing: RetailPricing
    options: ProductOptions
    media: ProductMedia
    source: ProductSource
    included_features: tuple[str, ...]
    notices: tuple[str, ...]
    unknown_fields: UnknownFields
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _monetary_value(value: ParsedPrice | None) -> MonetaryValue | None:
    if value is None:
        return None
    return MonetaryValue(
        raw_value=value.raw_value,
        currency=value.currency,
        amount=value.amount,
        context=value.context,
    )


def _raw_specification(value: RawSpecification) -> RawSpecificationRecord:
    return RawSpecificationRecord(
        field=value.field,
        value=value.value,
        field_coordinate=value.field_coordinate,
        value_coordinate=value.value_coordinate,
    )


def _upgrade_option(value: UpgradeOption) -> UpgradeOptionRecord:
    return UpgradeOptionRecord(
        name=value.name,
        raw_value=value.raw_value,
        supplier_cost=_monetary_value(value.price),
    )


def _raw_commercial_field(value: RawCommercialEntry) -> RawCommercialField:
    return RawCommercialField(
        field=value.field,
        value=value.value,
        coordinate=value.coordinate,
    )


def from_clm_product(product: CLMProductBlock) -> ProductRecord:
    """Convert one CLM parser product without business-price inference."""
    if not isinstance(product, CLMProductBlock):
        raise TypeError("product must be a CLMProductBlock")
    return ProductRecord(
        identity=ProductIdentity(
            series=product.series,
            model=product.model,
            raw_series_title=product.raw_series_title,
            raw_model=product.model_raw,
        ),
        specifications=ProductSpecifications(
            normalized=dict(product.specifications),
            raw=tuple(_raw_specification(item) for item in product.raw_specifications),
        ),
        supplier_costs=SupplierCosts(
            fob_unit_price=_monetary_value(product.pricing.fob_unit_price),
            body_only_fob=_monetary_value(product.pricing.body_only_price),
            including_head_fob=_monetary_value(
                product.pricing.including_head_price
            ),
        ),
        retail_pricing=RetailPricing(
            minimum_retail_price=_monetary_value(
                product.pricing.minimum_retail_price
            )
        ),
        options=ProductOptions(
            normal_options_price=_monetary_value(
                product.pricing.normal_options_price
            ),
            upgrade_options=tuple(
                _upgrade_option(item) for item in product.upgrade_options
            ),
        ),
        media=ProductMedia(photo_download_link=product.photo_download_link),
        source=ProductSource(
            start_row=product.source.start_row,
            end_row=product.source.end_row,
        ),
        included_features=tuple(product.included_features),
        notices=tuple(product.notices),
        unknown_fields=UnknownFields(
            raw_commercial_entries=tuple(
                _raw_commercial_field(item)
                for item in product.raw_commercial_entries
            )
        ),
        warnings=tuple(product.warnings),
    )
