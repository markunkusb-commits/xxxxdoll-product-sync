"""Explicit, versioned category mapping for CLM product records.

The registry is deliberately local and side-effect free.  It produces an
internal category candidate from an explicit supplier series and only emits a
WooCommerce category identifier when an approved binding is supplied by the
caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal

from .product_model import ProductRecord


CATEGORY_REGISTRY_VERSION = "clm-category-map-v1"

CategoryMappingStatus = Literal[
    "mapped_internal",
    "mapped_woo",
    "unsupported_series",
    "missing_series",
]


class CategoryRegistryError(ValueError):
    """Base error for invalid explicit registry configuration."""


class InvalidWooCategoryIdError(CategoryRegistryError):
    """Raised when a Woo category ID is not a positive integer."""


class UnknownCategoryKeyError(CategoryRegistryError):
    """Raised when a binding references no approved internal category key."""


class CategoryBindingConflictError(CategoryRegistryError):
    """Raised when one internal key is bound to different Woo category IDs."""


@dataclass(frozen=True, slots=True)
class InternalCategoryDefinition:
    series: str
    category_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class WooCategoryBinding:
    """An explicit, externally approved binding to an existing Woo category."""

    category_key: str
    woo_category_id: int


@dataclass(frozen=True, slots=True)
class CategoryMappingResult:
    status: CategoryMappingStatus
    series: str | None
    category_key: str | None
    display_name: str | None
    woo_category_id: int | None
    registry_version: str
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CategoryBatchSummary:
    total_products: int
    mapped_internal: int
    mapped_woo: int
    missing_series: int
    unsupported_series: int
    unbound_woo_category: int


@dataclass(frozen=True, slots=True)
class CategoryBatchResult:
    results: tuple[CategoryMappingResult, ...]
    summary: CategoryBatchSummary
    registry_version: str = CATEGORY_REGISTRY_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


INTERNAL_CATEGORY_DEFINITIONS = (
    InternalCategoryDefinition("classic", "clm-classic", "CLM Classic"),
    InternalCategoryDefinition("pro", "clm-pro", "CLM Pro"),
    InternalCategoryDefinition("ulw", "clm-ulw", "CLM ULW"),
    InternalCategoryDefinition("ultra", "clm-ultra", "CLM Ultra"),
)

_DEFINITION_BY_SERIES = MappingProxyType(
    {definition.series: definition for definition in INTERNAL_CATEGORY_DEFINITIONS}
)
_APPROVED_CATEGORY_KEYS = frozenset(
    definition.category_key for definition in INTERNAL_CATEGORY_DEFINITIONS
)


def _validate_woo_category_id(value: object) -> int:
    # bool is intentionally rejected even though it subclasses int.
    if type(value) is not int or value <= 0:
        raise InvalidWooCategoryIdError(
            "woo_category_id must be a positive integer"
        )
    return value


def _canonical_series(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized or None


class CategoryRegistry:
    """Validated internal definitions plus optional approved Woo bindings."""

    __slots__ = ("_woo_category_ids", "_woo_bindings")

    def __init__(
        self,
        woo_bindings: Sequence[WooCategoryBinding] = (),
    ) -> None:
        if isinstance(woo_bindings, (str, bytes)) or not isinstance(
            woo_bindings, Sequence
        ):
            raise TypeError("woo_bindings must be a sequence")

        ids_by_key: dict[str, int] = {}
        stable_bindings: list[WooCategoryBinding] = []
        for binding in woo_bindings:
            if not isinstance(binding, WooCategoryBinding):
                raise TypeError("each woo binding must be a WooCategoryBinding")
            if binding.category_key not in _APPROVED_CATEGORY_KEYS:
                raise UnknownCategoryKeyError(
                    "woo binding references an unknown internal category key"
                )
            woo_category_id = _validate_woo_category_id(binding.woo_category_id)
            existing = ids_by_key.get(binding.category_key)
            if existing is not None and existing != woo_category_id:
                raise CategoryBindingConflictError(
                    "one category_key cannot bind to different woo_category_id values"
                )
            if existing is None:
                ids_by_key[binding.category_key] = woo_category_id
                stable_bindings.append(binding)

        self._woo_category_ids = MappingProxyType(ids_by_key)
        self._woo_bindings = tuple(stable_bindings)

    @property
    def registry_version(self) -> str:
        return CATEGORY_REGISTRY_VERSION

    @property
    def internal_definitions(self) -> tuple[InternalCategoryDefinition, ...]:
        return INTERNAL_CATEGORY_DEFINITIONS

    @property
    def woo_bindings(self) -> tuple[WooCategoryBinding, ...]:
        return self._woo_bindings

    def map_product(self, product: ProductRecord) -> CategoryMappingResult:
        """Map one product using only its explicit series classification."""

        if not isinstance(product, ProductRecord):
            raise TypeError("product must be a ProductRecord")
        series = _canonical_series(product.identity.series)
        if series is None:
            return CategoryMappingResult(
                status="missing_series",
                series=None,
                category_key=None,
                display_name=None,
                woo_category_id=None,
                registry_version=CATEGORY_REGISTRY_VERSION,
                warnings=(),
                blocking_issues=("missing_series",),
            )

        definition = _DEFINITION_BY_SERIES.get(series)
        if definition is None:
            return CategoryMappingResult(
                status="unsupported_series",
                series=series,
                category_key=None,
                display_name=None,
                woo_category_id=None,
                registry_version=CATEGORY_REGISTRY_VERSION,
                warnings=(),
                blocking_issues=("unsupported_series",),
            )

        woo_category_id = self._woo_category_ids.get(definition.category_key)
        if woo_category_id is None:
            return CategoryMappingResult(
                status="mapped_internal",
                series=series,
                category_key=definition.category_key,
                display_name=definition.display_name,
                woo_category_id=None,
                registry_version=CATEGORY_REGISTRY_VERSION,
                warnings=("category_waiting_for_woo_binding",),
                blocking_issues=(),
            )
        return CategoryMappingResult(
            status="mapped_woo",
            series=series,
            category_key=definition.category_key,
            display_name=definition.display_name,
            woo_category_id=woo_category_id,
            registry_version=CATEGORY_REGISTRY_VERSION,
            warnings=(),
            blocking_issues=(),
        )

    def map_products(
        self, products: Sequence[ProductRecord]
    ) -> CategoryBatchResult:
        """Map products in input order and return an auditable batch summary."""

        if isinstance(products, (str, bytes)) or not isinstance(products, Sequence):
            raise TypeError("products must be a sequence")
        results = tuple(self.map_product(product) for product in products)
        summary = CategoryBatchSummary(
            total_products=len(results),
            mapped_internal=sum(
                result.status == "mapped_internal" for result in results
            ),
            mapped_woo=sum(result.status == "mapped_woo" for result in results),
            missing_series=sum(
                result.status == "missing_series" for result in results
            ),
            unsupported_series=sum(
                result.status == "unsupported_series" for result in results
            ),
            unbound_woo_category=sum(
                result.status == "mapped_internal"
                and result.woo_category_id is None
                for result in results
            ),
        )
        return CategoryBatchResult(results=results, summary=summary)


def map_category(
    product: ProductRecord,
    registry: CategoryRegistry | None = None,
) -> CategoryMappingResult:
    """Convenience single-product API using the default unbound registry."""

    return (registry or CategoryRegistry()).map_product(product)


def map_categories(
    products: Sequence[ProductRecord],
    registry: CategoryRegistry | None = None,
) -> CategoryBatchResult:
    """Convenience batch API using the default unbound registry."""

    return (registry or CategoryRegistry()).map_products(products)
