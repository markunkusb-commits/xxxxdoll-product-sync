"""Stable, pure-local SKU generation policy for CLM products.

This module performs no I/O and does not mutate ProductRecord values.  It
returns auditable candidates; batch collisions and duplicate inputs remain
blocking conditions for explicit human review.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from .product_model import ProductRecord
from .sanitization import REPORT_SECRET_SCAN_PATTERN


SKU_POLICY_VERSION = "clm-sku-v1"
MAX_SKU_LENGTH = 64
SERIES_NAMESPACES = {
    "classic": "CLASSIC",
    "pro": "PRO",
    "ulw": "ULW",
    "ultra": "ULTRA",
}

SkuStatus = Literal[
    "ok",
    "missing_identity",
    "unsupported_series",
    "invalid_identity",
    "collision",
    "duplicate_input",
    "too_long",
]
SkuIdentitySource = Literal["model", "raw_model", "height_model", "none"]

_WHITESPACE_PATTERN = re.compile(r"\s+")
_REPEATED_HYPHEN_PATTERN = re.compile(r"-+")
_UNSAFE_SKU_CHARACTER_PATTERN = re.compile(r"[^A-Z0-9-]+")
_CELL_COORDINATE_PATTERN = re.compile(r"^[A-Z]{1,2}[1-9][0-9]*$")
_MAPPER_UNSAFE_IDENTITY_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)"
    r"|authorization|cookie|consumer[_ -]?key|consumer[_ -]?secret"
    r"|password|private[_ -]?key|access[_ -]?token|refresh[_ -]?token"
)
_UNSAFE_IDENTITY_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)"
    r"|\b(?:fob|rmb|cny|price|margin|markup)\b"
    r"|supplier[_ -]?cost|consumer[_ -]?(?:key|secret)"
    r"|authorization|cookie|password|private[_ -]?key"
    r"|access[_ -]?token|refresh[_ -]?token"
)


@dataclass(frozen=True, slots=True)
class SkuAudit:
    policy_version: str
    identity_source: SkuIdentitySource
    series_namespace: str | None


@dataclass(frozen=True, slots=True)
class SkuGenerationResult:
    status: SkuStatus
    sku: str | None
    series: str
    raw_identity: str | None
    normalized_identity: str | None
    policy_version: str
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    conflicting_product_identities: tuple[str, ...]
    audit: SkuAudit

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkuCollision:
    sku: str
    conflicting_product_identities: tuple[str, ...]
    policy_version: str = SKU_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class DuplicateSkuInput:
    sku: str
    raw_identity: str
    occurrences: int
    policy_version: str = SKU_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class SkuBatchResult:
    results: tuple[SkuGenerationResult, ...]
    collisions: tuple[SkuCollision, ...]
    duplicate_inputs: tuple[DuplicateSkuInput, ...]
    policy_version: str = SKU_POLICY_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_identity_candidate(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def select_sku_identity(product: ProductRecord) -> tuple[str | None, SkuIdentitySource]:
    """Select identity using the existing Woo Mapper priority exactly."""

    if not isinstance(product, ProductRecord):
        raise TypeError("product must be a ProductRecord")
    candidates: tuple[tuple[object, SkuIdentitySource], ...] = (
        (product.identity.model, "model"),
        (product.identity.raw_model, "raw_model"),
        (product.specifications.normalized.get("height_model"), "height_model"),
    )
    for candidate, source in candidates:
        cleaned = _clean_identity_candidate(candidate)
        if cleaned is not None and not (
            _MAPPER_UNSAFE_IDENTITY_PATTERN.search(cleaned)
            or REPORT_SECRET_SCAN_PATTERN.search(cleaned)
        ):
            return cleaned, source
    return None, "none"


def normalize_sku_identity(raw_identity: str) -> str:
    """Apply deterministic, non-hashing SKU-safe normalization."""

    if not isinstance(raw_identity, str):
        raise TypeError("raw_identity must be text")
    normalized = unicodedata.normalize("NFKC", raw_identity).strip().upper()
    normalized = normalized.replace("+", " PLUS ")
    normalized = normalized.replace("#", "")
    normalized = normalized.replace("/", "-").replace("_", "-")
    normalized = _WHITESPACE_PATTERN.sub("-", normalized)
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    normalized = _UNSAFE_SKU_CHARACTER_PATTERN.sub("-", normalized)
    return _REPEATED_HYPHEN_PATTERN.sub("-", normalized).strip("-")


def _unsafe_raw_identity(raw_identity: str) -> bool:
    normalized_for_coordinate = unicodedata.normalize(
        "NFKC", raw_identity
    ).strip().upper()
    return bool(
        _UNSAFE_IDENTITY_PATTERN.search(raw_identity)
        or REPORT_SECRET_SCAN_PATTERN.search(raw_identity)
        or _CELL_COORDINATE_PATTERN.fullmatch(normalized_for_coordinate)
    )


def _result(
    *,
    status: SkuStatus,
    sku: str | None,
    series: str,
    raw_identity: str | None,
    normalized_identity: str | None,
    identity_source: SkuIdentitySource,
    series_namespace: str | None,
    blocking_issues: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    conflicting_product_identities: tuple[str, ...] = (),
) -> SkuGenerationResult:
    return SkuGenerationResult(
        status=status,
        sku=sku,
        series=series,
        raw_identity=raw_identity,
        normalized_identity=normalized_identity,
        policy_version=SKU_POLICY_VERSION,
        warnings=warnings,
        blocking_issues=blocking_issues,
        conflicting_product_identities=conflicting_product_identities,
        audit=SkuAudit(
            policy_version=SKU_POLICY_VERSION,
            identity_source=identity_source,
            series_namespace=series_namespace,
        ),
    )


def generate_sku(product: ProductRecord) -> SkuGenerationResult:
    """Generate one stable SKU candidate without reading mutable source data."""

    if not isinstance(product, ProductRecord):
        raise TypeError("product must be a ProductRecord")
    raw_series = product.identity.series
    series = raw_series.strip().casefold() if isinstance(raw_series, str) else ""
    namespace = SERIES_NAMESPACES.get(series)
    raw_identity, identity_source = select_sku_identity(product)

    if namespace is None:
        return _result(
            status="unsupported_series",
            sku=None,
            series=series,
            raw_identity=raw_identity,
            normalized_identity=None,
            identity_source=identity_source,
            series_namespace=None,
            blocking_issues=("unsupported_series",),
        )
    if raw_identity is None:
        had_identity_text = any(
            _clean_identity_candidate(candidate) is not None
            for candidate in (
                product.identity.model,
                product.identity.raw_model,
                product.specifications.normalized.get("height_model"),
            )
        )
        return _result(
            status="invalid_identity" if had_identity_text else "missing_identity",
            sku=None,
            series=series,
            raw_identity=None,
            normalized_identity=None,
            identity_source="none",
            series_namespace=namespace,
            blocking_issues=(
                "invalid_sku_identity" if had_identity_text else "missing_sku_identity",
            ),
        )

    normalized_identity = normalize_sku_identity(raw_identity)
    if not normalized_identity or _unsafe_raw_identity(raw_identity):
        return _result(
            status="invalid_identity",
            sku=None,
            series=series,
            raw_identity=raw_identity,
            normalized_identity=normalized_identity or None,
            identity_source=identity_source,
            series_namespace=namespace,
            blocking_issues=("invalid_sku_identity",),
        )

    sku = f"CLM-{namespace}-{normalized_identity}"
    if len(sku) > MAX_SKU_LENGTH:
        return _result(
            status="too_long",
            sku=sku,
            series=series,
            raw_identity=raw_identity,
            normalized_identity=normalized_identity,
            identity_source=identity_source,
            series_namespace=namespace,
            blocking_issues=("sku_too_long",),
        )
    return _result(
        status="ok",
        sku=sku,
        series=series,
        raw_identity=raw_identity,
        normalized_identity=normalized_identity,
        identity_source=identity_source,
        series_namespace=namespace,
    )


def _duplicate_signature(result: SkuGenerationResult) -> tuple[str, str]:
    return (
        result.series,
        unicodedata.normalize("NFKC", result.raw_identity or "").casefold(),
    )


def generate_skus(products: Sequence[ProductRecord]) -> SkuBatchResult:
    """Generate in input order and mark duplicate inputs or SKU collisions."""

    if isinstance(products, (str, bytes)) or not isinstance(products, Sequence):
        raise TypeError("products must be a sequence")
    generated = [generate_sku(product) for product in products]
    groups: dict[str, list[int]] = defaultdict(list)
    for index, result in enumerate(generated):
        if result.sku is not None and result.status == "ok":
            groups[result.sku].append(index)

    collisions: list[SkuCollision] = []
    duplicates: list[DuplicateSkuInput] = []
    for sku, indexes in groups.items():
        if len(indexes) < 2:
            continue
        signatures = {_duplicate_signature(generated[index]) for index in indexes}
        identities = tuple(
            dict.fromkeys(
                generated[index].raw_identity or "" for index in indexes
            )
        )
        if len(signatures) == 1:
            raw_identity = generated[indexes[0]].raw_identity or ""
            duplicates.append(
                DuplicateSkuInput(
                    sku=sku,
                    raw_identity=raw_identity,
                    occurrences=len(indexes),
                )
            )
            for index in indexes:
                generated[index] = replace(
                    generated[index],
                    status="duplicate_input",
                    blocking_issues=("duplicate_input",),
                    conflicting_product_identities=identities,
                )
        else:
            collisions.append(
                SkuCollision(
                    sku=sku,
                    conflicting_product_identities=identities,
                )
            )
            for index in indexes:
                generated[index] = replace(
                    generated[index],
                    status="collision",
                    blocking_issues=("sku_collision",),
                    conflicting_product_identities=identities,
                )

    return SkuBatchResult(
        results=tuple(generated),
        collisions=tuple(collisions),
        duplicate_inputs=tuple(duplicates),
    )


def validate_sku_uniqueness(products: Sequence[ProductRecord]) -> SkuBatchResult:
    """Named validation entry point using the same deterministic batch policy."""

    return generate_skus(products)
