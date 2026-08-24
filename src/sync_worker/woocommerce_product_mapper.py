"""Pure-local WooCommerce product payload candidate builder.

This module creates an in-memory, write-disabled candidate.  It contains no
WooCommerce client, credentials, HTTP transport, or write gateway.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

from .category_mapping import CategoryMappingResult
from .product_model import MonetaryValue, ProductRecord
from .product_option_pricing import PricedLinkedOption
from .product_size_enricher import ProductSizeMatchResult
from .retail_price_presentation import RetailPricePresentationResult
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .size_list_parser import NormalizedMeasurement
from .sku_policy import (
    MAX_SKU_LENGTH,
    SKU_POLICY_VERSION,
    SkuGenerationResult,
)
from .woo_category_binding import (
    WooCategoryBindingResult,
    WooCategoryBindingVerification,
)


WOO_API_VERSION = "wc/v3"
WOO_RESOURCE = "products"
WOO_METHOD = "POST"
WOO_CORE_PAYLOAD_ALLOWLIST = frozenset(
    {
        "name",
        "sku",
        "type",
        "status",
        "regular_price",
        "description",
        "short_description",
        "attributes",
        "categories",
    }
)
PUBLIC_SPECIFICATION_ALLOWLIST = (
    "height",
    "upper_chest",
    "lower_chest",
    "waist",
    "hip",
    "shoulder",
    "leg_length",
    "thigh",
    "arm_length",
    "sole",
    "net_weight",
    "oral",
    "vagina",
    "anus",
)

_PUBLIC_ATTRIBUTE_NAMES = {
    "height": "Height",
    "upper_chest": "Upper Chest",
    "lower_chest": "Lower Chest",
    "waist": "Waist",
    "hip": "Hip",
    "shoulder": "Shoulder",
    "leg_length": "Leg Length",
    "thigh": "Thigh",
    "arm_length": "Arm Length",
    "sole": "Sole",
    "net_weight": "Net Weight",
    "oral": "Oral",
    "vagina": "Vagina",
    "anus": "Anus",
}
_PUBLIC_ATTRIBUTE_NAME_SET = frozenset(_PUBLIC_ATTRIBUTE_NAMES.values())
_SHIPPING_CANDIDATE_FIELDS = ("carton_size", "gross_weight")
_USD_CENT = Decimal("0.01")
_USD_PRICE_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$")
_SKU_PATTERN = re.compile(
    r"^CLM-(?:CLASSIC|PRO|ULW|ULTRA)-[A-Z0-9]+(?:-[A-Z0-9]+)*$"
)
_FORBIDDEN_SKU_TOKENS = frozenset(
    {
        "FOB",
        "RMB",
        "USD",
        "SUPPLIER",
        "COST",
        "PRICE",
        "SOURCE",
        "ROW",
        "TIMESTAMP",
        "UUID",
    }
)
_URL_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)[^\s\"'<>]+"
    r"|\b(?:drive|docs)\.google\.com(?:/[^\s\"'<>]*)?"
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)authorization|cookie|consumer[_ -]?key|consumer[_ -]?secret"
    r"|password|private[_ -]?key|access[_ -]?token|refresh[_ -]?token"
)
_INTERNAL_PAYLOAD_PATTERN = re.compile(
    r"(?i)\bfob\b|supplier[_ ]?cost|supplier internal|wholesale"
    r"|\bfx(?:[_ ]?rate)?\b|source[_ ]?rows?|google coordinate"
    r"|economic[_ ]?(?:target|margin)|\bmargin\b"
)
_ATTRIBUTE_KEYS = frozenset(
    {"name", "position", "visible", "variation", "options"}
)
_STOREFRONT_OPTION_KEYS = frozenset({"name", "price_usd", "option_type"})
_CATEGORY_PAYLOAD_KEYS = frozenset({"id"})


class WooCommerceProductMapperError(ValueError):
    """Safe mapper validation error."""


@dataclass(frozen=True, slots=True)
class PresentedUpgradeOption:
    """Explicit binding between an economic option and its display result."""

    priced_option: PricedLinkedOption
    presentation: RetailPricePresentationResult


@dataclass(frozen=True, slots=True)
class WooCommerceProductPayloadCandidate:
    api: dict[str, object]
    payload: dict[str, object]
    storefront_options: tuple[dict[str, object], ...]
    public_content: dict[str, object]
    audit: dict[str, object]
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    ready_for_write: Literal[False] = False

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe detached representation."""

        result = _json_safe(asdict(self))
        if not isinstance(result, dict):  # pragma: no cover - structural guard
            raise AssertionError("Candidate serialization must remain an object")
        result["ready_for_write"] = False
        return result


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _safe_text(value: object, *, limit: int = 1000) -> str:
    redacted = Redactor().text(value, limit=limit)
    return _URL_PATTERN.sub("[REDACTED_URL]", redacted).strip()


def _contains_unsafe_text(value: object) -> bool:
    serialized = str(value)
    return bool(
        _URL_PATTERN.search(serialized)
        or _CREDENTIAL_PATTERN.search(serialized)
        or REPORT_SECRET_SCAN_PATTERN.search(serialized)
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _category_projection(
    product: ProductRecord,
    category_mapping_result: CategoryMappingResult | None,
    woo_category_binding_result: WooCategoryBindingResult | None,
    category_binding_verification: WooCategoryBindingVerification | None,
) -> tuple[
    list[dict[str, int]] | None,
    dict[str, object],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Project one pre-verified binding without selecting profiles or IDs."""

    if category_mapping_result is None and woo_category_binding_result is None and category_binding_verification is None:
        return (
            None,
            {
                "internal_registry_version": None,
                "internal_category_key": None,
                "binding_profile_version": None,
                "environment": None,
                "target_host": None,
                "woo_category_id": None,
                "verified_name": None,
                "binding_status": "not_selected",
                "host_verified": False,
                "discovery_verified": False,
            },
            ("category_binding_not_selected",),
            (),
        )

    if category_mapping_result is not None and not isinstance(
        category_mapping_result, CategoryMappingResult
    ):
        raise TypeError("category_mapping_result must be CategoryMappingResult or None")
    if woo_category_binding_result is not None and not isinstance(
        woo_category_binding_result, WooCategoryBindingResult
    ):
        raise TypeError(
            "woo_category_binding_result must be WooCategoryBindingResult or None"
        )
    if category_binding_verification is not None and not isinstance(
        category_binding_verification, WooCategoryBindingVerification
    ):
        raise TypeError(
            "category_binding_verification must be WooCategoryBindingVerification or None"
        )

    warnings: list[str] = []
    blockers: list[str] = []
    mapping = category_mapping_result
    binding = woo_category_binding_result
    verification = category_binding_verification

    if mapping is None:
        blockers.append("category_mapping_result_missing")
    else:
        blockers.extend(mapping.blocking_issues)
    if verification is None:
        blockers.append("category_binding_verification_missing")
    else:
        blockers.extend(verification.blocking_issues)

    mapping_key = mapping.category_key if mapping is not None else None
    product_series = (
        product.identity.series.strip().casefold()
        if isinstance(product.identity.series, str)
        else ""
    )
    binding_status = (
        binding.status
        if binding is not None
        else (
            verification.blocking_issues[0]
            if verification is not None and verification.blocking_issues
            else "verification_missing"
        )
    )
    host_verified = bool(
        verification is not None
        and verification.status == "verified"
        and verification.hostname == verification.expected_host
        and not verification.blocking_issues
    )
    discovery_verified = bool(
        host_verified
        and binding is not None
        and binding.status == "bound_verified"
        and binding.expected_name
        and binding.expected_name == binding.discovered_name
    )

    if mapping is not None and verification is not None:
        if mapping.registry_version != verification.registry_version:
            blockers.append("category_binding_verification_mismatch")
    if mapping is not None and (
        mapping.series != product_series
        or mapping.status == "mapped_woo"
        or mapping.woo_category_id is not None
    ):
        blockers.append("category_binding_verification_mismatch")
    if binding is not None and binding.internal_category_key != mapping_key:
        blockers.append("category_binding_verification_mismatch")
    if (
        binding is not None
        and verification is not None
        and binding not in verification.results
    ):
        blockers.append("category_binding_verification_mismatch")

    categories: list[dict[str, int]] | None = None
    if binding is None:
        if verification is not None and not verification.blocking_issues and mapping_key:
            blockers.append("category_binding_verification_missing")
    elif binding.status == "unbound_category":
        warnings.append("category_unbound")
    elif binding.status in {"binding_target_missing", "binding_target_changed"}:
        blockers.extend(binding.blocking_issues or (binding.status,))
    elif binding.status == "bound_verified":
        category_id = binding.woo_category_id
        if (
            type(category_id) is int
            and category_id > 0
            and discovery_verified
            and not blockers
        ):
            categories = [{"id": category_id}]
        else:
            blockers.append("category_binding_verification_mismatch")

    audit = {
        "internal_registry_version": (
            mapping.registry_version if mapping is not None else None
        ),
        "internal_category_key": mapping_key,
        "binding_profile_version": (
            verification.profile_version if verification is not None else None
        ),
        "environment": (
            verification.environment if verification is not None else None
        ),
        "target_host": (
            verification.hostname if verification is not None else None
        ),
        "woo_category_id": (
            binding.woo_category_id if binding is not None else None
        ),
        "verified_name": (
            binding.discovered_name if discovery_verified and binding is not None else None
        ),
        "binding_status": binding_status,
        "host_verified": host_verified,
        "discovery_verified": discovery_verified,
    }
    return categories, audit, _unique(warnings), _unique(blockers)


def _money_audit(value: MonetaryValue | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "amount": value.amount,
        "currency": value.currency,
        "raw_value": _safe_text(value.raw_value),
        "context": value.context,
    }


def _product_name(product: ProductRecord) -> str | None:
    candidates = (
        product.identity.model,
        product.identity.raw_model,
        product.specifications.normalized.get("height_model"),
    )
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        normalized = " ".join(candidate.split())
        if normalized and not _contains_unsafe_text(normalized):
            return normalized
    return None


def _base_regular_price(
    product: ProductRecord,
) -> tuple[str | None, str | None]:
    price = product.retail_pricing.minimum_retail_price
    if price is None or price.amount is None:
        return None, "missing_base_retail_price"
    currency = (price.currency or "").strip().upper()
    if currency != "USD":
        return None, "unsupported_base_price_currency"
    if isinstance(price.amount, bool):
        return None, "invalid_regular_price"
    try:
        amount = Decimal(str(price.amount))
    except (InvalidOperation, TypeError, ValueError):
        return None, "invalid_regular_price"
    if not amount.is_finite() or amount < 0:
        return None, "invalid_regular_price"
    return format(amount.quantize(_USD_CENT, rounding=ROUND_HALF_UP), "f"), None


def _valid_sku(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) <= MAX_SKU_LENGTH
        and _SKU_PATTERN.fullmatch(value) is not None
        and not _FORBIDDEN_SKU_TOKENS.intersection(value.split("-"))
    )


def _sku_payload(
    sku_result: SkuGenerationResult | None,
) -> tuple[str | None, dict[str, object], tuple[str, ...]]:
    if sku_result is None:
        return (
            None,
            {
                "value": None,
                "policy_version": None,
                "raw_identity": None,
                "normalized_identity": None,
                "status": "missing",
            },
            ("missing_sku",),
        )
    if not isinstance(sku_result, SkuGenerationResult):
        raise TypeError("sku_result must be SkuGenerationResult or None")

    issues = list(sku_result.blocking_issues)
    value = sku_result.sku
    if sku_result.policy_version != SKU_POLICY_VERSION:
        issues.append("invalid_sku_policy_version")
        value = None
    elif value is None:
        if sku_result.status == "missing_identity":
            issues.append("missing_sku")
        elif sku_result.status == "too_long":
            issues.append("sku_too_long")
        else:
            issues.append("invalid_sku")
    elif len(value) > MAX_SKU_LENGTH:
        issues.append("sku_too_long")
        value = None
    elif not _valid_sku(value):
        issues.append("invalid_sku")
        value = None

    return (
        value,
        {
            "value": value,
            "policy_version": sku_result.policy_version,
            "raw_identity": _safe_text(sku_result.raw_identity or ""),
            "normalized_identity": _safe_text(
                sku_result.normalized_identity or ""
            ),
            "status": sku_result.status,
        },
        _unique(issues),
    )


def _measurement_text(value: NormalizedMeasurement | None) -> str | None:
    if value is None:
        return None
    raw = " ".join(value.raw_value.split())
    if not raw or _contains_unsafe_text(raw):
        return None
    return raw


def _public_specifications(
    product: ProductRecord,
    size_enrichment: ProductSizeMatchResult | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    attributes: list[dict[str, object]] = []
    provenance: list[dict[str, object]] = []
    conflict_fields = (
        {conflict.field for conflict in size_enrichment.conflicts}
        if size_enrichment is not None
        else set()
    )
    matched_size = (
        size_enrichment is not None
        and size_enrichment.match.status == "matched"
        and size_enrichment.size_specifications is not None
    )

    for field_name in PUBLIC_SPECIFICATION_ALLOWLIST:
        value: str | None = None
        source = "product"
        if (
            matched_size
            and field_name != "height"
            and field_name not in conflict_fields
        ):
            measurement = getattr(
                size_enrichment.size_specifications,
                field_name,
                None,
            )
            value = _measurement_text(measurement)
            if value is not None:
                source = "verified_size_match"
        if value is None:
            raw_product_value = product.specifications.normalized.get(field_name)
            if isinstance(raw_product_value, str):
                normalized = " ".join(raw_product_value.split())
                if normalized and not _contains_unsafe_text(normalized):
                    value = normalized
                    source = "product"
        if value is None:
            continue
        attributes.append(
            {
                "name": _PUBLIC_ATTRIBUTE_NAMES[field_name],
                "position": len(attributes),
                "visible": True,
                "variation": False,
                "options": [value],
            }
        )
        provenance.append({"field": field_name, "source": source})
    return attributes, provenance


def _option_supplier_cost(option: PricedLinkedOption) -> dict[str, object]:
    return {
        "amount": option.supplier_cost.amount,
        "currency": option.supplier_cost.currency,
        "raw_values": [_safe_text(value) for value in option.supplier_cost.raw_values],
        "source_coordinates": list(option.supplier_cost.source_coordinates),
    }


def _option_components(option: PricedLinkedOption) -> list[dict[str, object]]:
    return [
        {
            "name": _safe_text(component.option_name),
            "supplier_cost": {
                "amount": component.supplier_cost.amount,
                "currency": component.supplier_cost.currency,
                "raw_price": _safe_text(component.supplier_cost.raw_price or ""),
            },
            "source_coordinate": component.source_coordinate,
        }
        for component in option.mapping.components
    ]


def _storefront_option(
    value: PresentedUpgradeOption,
) -> tuple[dict[str, object] | None, dict[str, object], str | None]:
    option = value.priced_option
    presentation = value.presentation
    economic_target = (
        option.pricing.retail.target_retail_usd
        if option.pricing.retail is not None
        else None
    )
    presented_target = presentation.economic.target_retail_usd
    display = presentation.presentation.display_price_usd
    option_name = " ".join(option.product_upgrade_name.split())
    issue: str | None = None
    if (
        not option_name
        or _contains_unsafe_text(option_name)
        or economic_target is None
        or presented_target != economic_target
        or display is None
        or display < economic_target
    ):
        issue = "invalid_storefront_option_price"
        storefront = None
    else:
        storefront = {
            "name": option_name,
            "price_usd": format(display.quantize(_USD_CENT), "f"),
            "option_type": "paid_upgrade",
        }
    audit = {
        "option_name": _safe_text(option.product_upgrade_name),
        "raw_value": _safe_text(option.product_raw_value),
        "mapping_type": option.mapping.mapping_type,
        "mapping_status": option.mapping.mapping_status,
        "registry_version": option.mapping.registry_version,
        "pricing_policy_version": option.pricing.metadata.policy_version,
        "presentation_policy_version": presentation.metadata.policy_version,
        "supplier_cost": _option_supplier_cost(option),
        "economic_target_usd": economic_target,
        "economic_cost_usd": (
            option.pricing.calculation.cost_usd
            if option.pricing.calculation is not None
            else None
        ),
        "display_price_usd": display,
        "components": _option_components(option),
        "warnings": [
            _safe_text(warning)
            for warning in (*option.warnings, *presentation.warnings)
        ],
    }
    return storefront, audit, issue


def _shipping_candidates(product: ProductRecord) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for field_name in _SHIPPING_CANDIDATE_FIELDS:
        value = product.specifications.normalized.get(field_name)
        if isinstance(value, str) and value.strip():
            candidates[field_name] = _safe_text(value)
    return candidates


def _size_cost_audit(
    size_enrichment: ProductSizeMatchResult | None,
) -> dict[str, object] | None:
    if size_enrichment is None:
        return None
    costs = size_enrichment.supplier_costs
    return {
        "price_list_fob": _money_audit(costs.price_list_fob),
        "price_list_body_only_fob": _money_audit(
            costs.price_list_body_only_fob
        ),
        "price_list_including_head_fob": _money_audit(
            costs.price_list_including_head_fob
        ),
        "size_list_fob": (
            {
                "amount": costs.size_list_fob.amount,
                "currency": costs.size_list_fob.currency,
                "raw_value": _safe_text(costs.size_list_fob.raw_value),
            }
            if costs.size_list_fob is not None
            else None
        ),
    }


def _candidate_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, WooCommerceProductPayloadCandidate):
        return value.to_dict()
    if isinstance(value, Mapping):
        return value
    raise WooCommerceProductMapperError(
        "candidate must be a WooCommerce payload candidate or mapping"
    )


def _payload_has_internal_data(payload: Mapping[str, object]) -> bool:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    return bool(
        _INTERNAL_PAYLOAD_PATTERN.search(serialized)
        or _CREDENTIAL_PATTERN.search(serialized)
        or REPORT_SECRET_SCAN_PATTERN.search(serialized)
        or _URL_PATTERN.search(serialized)
    )


def validate_woocommerce_product_payload(
    candidate: WooCommerceProductPayloadCandidate | Mapping[str, object],
) -> tuple[str, ...]:
    """Validate the write-disabled candidate against the V1 safety contract."""

    root = _candidate_mapping(candidate)
    payload_value = root.get("payload")
    if not isinstance(payload_value, Mapping):
        return ("unsafe_field_detected_in_payload",)
    payload = payload_value
    issues: list[str] = []

    if set(payload).difference(WOO_CORE_PAYLOAD_ALLOWLIST):
        issues.append("unsafe_field_detected_in_payload")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append("missing_product_name")
    if payload.get("type") != "simple":
        issues.append("invalid_product_type")
    if payload.get("status") != "draft":
        issues.append("invalid_product_status")

    regular_price = payload.get("regular_price")
    if regular_price is not None and (
        not isinstance(regular_price, str)
        or _USD_PRICE_PATTERN.fullmatch(regular_price) is None
    ):
        issues.append("invalid_regular_price")
    if _payload_has_internal_data(payload):
        issues.append("unsafe_field_detected_in_payload")

    sku = payload.get("sku")
    if sku is None:
        issues.append("missing_sku")
    elif isinstance(sku, str) and len(sku) > MAX_SKU_LENGTH:
        issues.append("sku_too_long")
    elif not _valid_sku(sku):
        issues.append("invalid_sku")

    attributes = payload.get("attributes", [])
    if not isinstance(attributes, list):
        issues.append("unsafe_field_detected_in_payload")
    else:
        for attribute in attributes:
            if not isinstance(attribute, Mapping):
                issues.append("unsafe_field_detected_in_payload")
                continue
            if set(attribute) != _ATTRIBUTE_KEYS:
                issues.append("unsafe_field_detected_in_payload")
            if attribute.get("name") not in _PUBLIC_ATTRIBUTE_NAME_SET:
                issues.append("unsafe_field_detected_in_payload")
            if attribute.get("visible") is not True:
                issues.append("unsafe_field_detected_in_payload")
            if attribute.get("variation") is not False:
                issues.append("attribute_variation_must_be_false")
            options = attribute.get("options")
            if (
                not isinstance(options, list)
                or len(options) != 1
                or not isinstance(options[0], str)
                or not options[0].strip()
            ):
                issues.append("unsafe_field_detected_in_payload")

    categories = payload.get("categories")
    if categories is not None:
        category_audit = audit_category = None
        root_audit = root.get("audit")
        if isinstance(root_audit, Mapping):
            audit_category = root_audit.get("category")
        if isinstance(audit_category, Mapping):
            category_audit = audit_category
        if not isinstance(categories, list) or len(categories) != 1:
            issues.append("invalid_category_binding_payload")
        else:
            category = categories[0]
            if (
                not isinstance(category, Mapping)
                or set(category) != _CATEGORY_PAYLOAD_KEYS
                or type(category.get("id")) is not int
                or category.get("id", 0) <= 0
            ):
                issues.append("invalid_category_binding_payload")
            elif (
                category_audit is None
                or category_audit.get("binding_status") != "bound_verified"
                or category_audit.get("host_verified") is not True
                or category_audit.get("discovery_verified") is not True
                or category_audit.get("woo_category_id") != category.get("id")
                or not isinstance(category_audit.get("verified_name"), str)
                or not category_audit.get("verified_name")
            ):
                issues.append("invalid_category_binding_payload")

    storefront_value = root.get("storefront_options", [])
    storefront = storefront_value if isinstance(storefront_value, list) else []
    if not isinstance(storefront_value, list):
        issues.append("invalid_storefront_option_price")
    for option in storefront:
        if not isinstance(option, Mapping) or set(option) != _STOREFRONT_OPTION_KEYS:
            issues.append("invalid_storefront_option_price")
            continue
        if option.get("option_type") != "paid_upgrade":
            issues.append("invalid_storefront_option_price")
        price = option.get("price_usd")
        if not isinstance(price, str) or _USD_PRICE_PATTERN.fullmatch(price) is None:
            issues.append("invalid_storefront_option_price")

    audit = root.get("audit")
    if isinstance(audit, Mapping):
        pricing = audit.get("pricing", [])
        if isinstance(pricing, list):
            storefront_by_name = {
                option.get("name"): option
                for option in storefront
                if isinstance(option, Mapping)
            }
            for audit_option in pricing:
                if not isinstance(audit_option, Mapping):
                    continue
                option_name = audit_option.get("option_name")
                storefront_option = storefront_by_name.get(option_name)
                target = audit_option.get("economic_target_usd")
                display = audit_option.get("display_price_usd")
                if storefront_option is not None and target is not None:
                    try:
                        storefront_price = Decimal(
                            str(storefront_option.get("price_usd"))
                        )
                        economic = Decimal(str(target))
                    except (InvalidOperation, ValueError):
                        issues.append("invalid_storefront_option_price")
                    else:
                        if storefront_price < economic:
                            issues.append("invalid_storefront_option_price")
                if audit_option.get("mapping_type") == "composite":
                    components = audit_option.get("components", [])
                    component_names = {
                        component.get("name")
                        for component in components
                        if isinstance(component, Mapping)
                    }
                    if any(name in storefront_by_name for name in component_names):
                        issues.append("composite_option_was_split")
                    if display is not None and option_name not in storefront_by_name:
                        issues.append("composite_option_was_split")
    return _unique(issues)


def build_woocommerce_product_payload_candidate(
    product: ProductRecord,
    *,
    sku_result: SkuGenerationResult | None = None,
    size_enrichment: ProductSizeMatchResult | None = None,
    presented_options: Sequence[PresentedUpgradeOption] = (),
    category_mapping_result: CategoryMappingResult | None = None,
    woo_category_binding_result: WooCategoryBindingResult | None = None,
    category_binding_verification: WooCategoryBindingVerification | None = None,
) -> WooCommerceProductPayloadCandidate:
    """Build one deterministic candidate without external access or writes."""

    if not isinstance(product, ProductRecord):
        raise TypeError("product must be a ProductRecord")
    if size_enrichment is not None and not isinstance(
        size_enrichment, ProductSizeMatchResult
    ):
        raise TypeError("size_enrichment must be ProductSizeMatchResult or None")
    if isinstance(presented_options, (str, bytes)) or not isinstance(
        presented_options, Sequence
    ):
        raise TypeError("presented_options must be a sequence")

    warnings = ["images_not_mapped", "customer_description_not_generated"]
    blockers: list[str] = []
    name = _product_name(product)
    payload: dict[str, object] = {"type": "simple", "status": "draft"}
    sku, sku_audit, sku_issues = _sku_payload(sku_result)
    blockers.extend(sku_issues)
    if sku is not None:
        payload["sku"] = sku
    if name is None:
        blockers.append("missing_product_name")
    else:
        payload["name"] = name

    regular_price, price_issue = _base_regular_price(product)
    if price_issue is not None:
        blockers.append(price_issue)
    elif regular_price is not None:
        payload["regular_price"] = regular_price

    attributes, attribute_provenance = _public_specifications(
        product,
        size_enrichment,
    )
    if attributes:
        payload["attributes"] = attributes

    categories, category_audit, category_warnings, category_blockers = (
        _category_projection(
            product,
            category_mapping_result,
            woo_category_binding_result,
            category_binding_verification,
        )
    )
    warnings = [*category_warnings, *warnings]
    blockers.extend(category_blockers)
    if categories is not None:
        payload["categories"] = categories

    if size_enrichment is not None and size_enrichment.match.status != "matched":
        warnings.append("size_enrichment_unmatched")

    storefront_options: list[dict[str, object]] = []
    option_audit: list[dict[str, object]] = []
    for index, value in enumerate(presented_options):
        if not isinstance(value, PresentedUpgradeOption):
            raise TypeError(
                f"presented_options[{index}] must be PresentedUpgradeOption"
            )
        storefront, audit_entry, issue = _storefront_option(value)
        option_audit.append(audit_entry)
        if storefront is not None:
            storefront_options.append(storefront)
        if issue is not None:
            blockers.append(issue)

    registry_versions = _unique(
        tuple(
            version
            for entry in option_audit
            if isinstance((version := entry.get("registry_version")), str)
            and version
        )
    )
    pricing_policy_versions = _unique(
        tuple(
            version
            for entry in option_audit
            if isinstance((version := entry.get("pricing_policy_version")), str)
            and version
        )
    )
    presentation_policy_versions = _unique(
        tuple(
            version
            for entry in option_audit
            if isinstance(
                (version := entry.get("presentation_policy_version")), str
            )
            and version
        )
    )

    audit = {
        "sku": sku_audit,
        "series": product.identity.series,
        "raw_identity": {
            "model": _safe_text(product.identity.model or ""),
            "raw_model": _safe_text(product.identity.raw_model or ""),
            "raw_series_title": _safe_text(product.identity.raw_series_title),
        },
        "source_rows": {
            "start_row": product.source.start_row,
            "end_row": product.source.end_row,
        },
        "product_parser_warnings": [
            _safe_text(warning) for warning in product.warnings
        ],
        "size_match_status": (
            size_enrichment.match.status
            if size_enrichment is not None
            else "not_provided"
        ),
        "registry_versions": list(registry_versions),
        "pricing_policy_versions": list(pricing_policy_versions),
        "presentation_policy_versions": list(presentation_policy_versions),
        "public_attribute_provenance": attribute_provenance,
        "shipping_candidates": _shipping_candidates(product),
        "internal_supplier_costs": {
            "product": {
                "price_list_fob": _money_audit(
                    product.supplier_costs.fob_unit_price
                ),
                "body_only_fob": _money_audit(
                    product.supplier_costs.body_only_fob
                ),
                "including_head_fob": _money_audit(
                    product.supplier_costs.including_head_fob
                ),
            },
            "size_enrichment": _size_cost_audit(size_enrichment),
            "options": [
                {
                    "option_name": entry["option_name"],
                    "supplier_cost": entry["supplier_cost"],
                }
                for entry in option_audit
            ],
        },
        "pricing": option_audit,
        "category": category_audit,
    }
    public_content = {
        "included_features": [
            _safe_text(feature)
            for feature in product.included_features
            if feature.strip() and not _contains_unsafe_text(feature)
        ]
    }
    candidate_mapping: dict[str, object] = {
        "api": {
            "version": WOO_API_VERSION,
            "resource": WOO_RESOURCE,
            "method": WOO_METHOD,
        },
        "payload": payload,
        "storefront_options": storefront_options,
        "public_content": public_content,
        "audit": audit,
        "warnings": list(_unique(warnings)),
        "blocking_issues": [],
        "ready_for_write": False,
    }
    blockers.extend(validate_woocommerce_product_payload(candidate_mapping))
    return WooCommerceProductPayloadCandidate(
        api=dict(candidate_mapping["api"]),
        payload=payload,
        storefront_options=tuple(storefront_options),
        public_content=public_content,
        audit=audit,
        warnings=_unique(warnings),
        blocking_issues=_unique(blockers),
        ready_for_write=False,
    )


def build_woocommerce_product_payload(
    product: ProductRecord,
    *,
    sku_result: SkuGenerationResult | None = None,
    size_enrichment: ProductSizeMatchResult | None = None,
    presented_options: Sequence[PresentedUpgradeOption] = (),
    category_mapping_result: CategoryMappingResult | None = None,
    woo_category_binding_result: WooCategoryBindingResult | None = None,
    category_binding_verification: WooCategoryBindingVerification | None = None,
) -> WooCommerceProductPayloadCandidate:
    """Backward-friendly public name for the V1 candidate builder."""

    return build_woocommerce_product_payload_candidate(
        product,
        sku_result=sku_result,
        size_enrichment=size_enrichment,
        presented_options=presented_options,
        category_mapping_result=category_mapping_result,
        woo_category_binding_result=woo_category_binding_result,
        category_binding_verification=category_binding_verification,
    )
