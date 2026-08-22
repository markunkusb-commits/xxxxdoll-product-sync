"""Pure-local WooCommerce payload-candidate dry run.

The adapter restores existing report projections into their canonical domain
objects, delegates all business behaviour to the existing enrichment and mapper
layers, and writes only a local, permanently write-disabled report.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .additional_option_parser import AdditionalOptionPricing
from .option_pricing_policy import (
    OptionPricingMetadata,
    OptionRetailCandidate,
    OptionRetailPricingResult,
    SupplierCostSnapshot,
)
from .product_model import ProductRecord
from .product_option_pricing import (
    PricedLinkedOption,
    PricedMappingComponent,
    PricingMappingSnapshot,
    SupplierCostProvenance,
)
from . import product_size_enricher
from .product_size_enrichment_dry_run import (
    load_local_json_report,
    restore_product_records,
    restore_size_records,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .retail_price_presentation import (
    EconomicRetailPrice,
    PresentationCalculation,
    PresentationMetadata,
    PresentedRetailPrice,
    RetailPricePresentationResult,
)
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from . import sku_policy
from . import woocommerce_product_mapper
from .woocommerce_product_mapper import (
    PresentedUpgradeOption,
    WOO_CORE_PAYLOAD_ALLOWLIST,
    WooCommerceProductPayloadCandidate,
)


REPORT_FILENAME = "woocommerce-payload-dry-run.json"
AMBIGUOUS_JOIN_ISSUE = "ambiguous_product_report_join"
UNSAFE_PUBLIC_ISSUE = "unsafe_public_data_detected"

_URL_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.)[^\s\"'<>]+"
    r"|\b(?:drive|docs)\.google\.com(?:/[^\s\"'<>]*)?"
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)authorization|cookie|credential|consumer[_ -]?key"
    r"|consumer[_ -]?secret|password|private[_ -]?key"
    r"|access[_ -]?token|refresh[_ -]?token"
)
_INTERNAL_PUBLIC_PATTERN = re.compile(
    r"(?i)\bfob\b|fob unit price|supplier[_ ]?cost|combined_supplier_cost"
    r"|economic[_ ]?(?:target|pricing)|target_retail_usd"
    r"|\bfx(?:[_ ]?rate)?\b|\bmargin\b|\bmarkup\b|minimum[_ ]?profit"
    r"|source[_ ]?(?:coordinate|row)|google coordinate"
)
_COORDINATE_PATTERN = re.compile(
    r"(?:^|[\"\s\[:])[A-Z]{1,2}[1-9][0-9]*"
    r"(?::[A-Z]{1,2}[1-9][0-9]*)?(?=$|[\"\s,\]])"
)
_SUPPLIER_PRICE_PATTERN = re.compile(r"(?i)(?:￥|¥|\bRMB\b|\bCNY\b|0\.1500)")


class WooCommercePayloadDryRunInputError(ValueError):
    """Safe structural error for explicitly supplied local JSON reports."""


@dataclass(frozen=True, slots=True)
class PresentedProductOptions:
    series: str
    model: str | None
    raw_model: str | None
    start_row: int
    end_row: int
    options: tuple[PresentedUpgradeOption, ...]
    warnings: tuple[str, ...]


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WooCommercePayloadDryRunInputError(f"{label} must be an object")
    return value


def _items(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise WooCommercePayloadDryRunInputError(f"{label} must be an array")
    return value


def _text(
    value: object,
    *,
    label: str,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if nullable else ""
        raise WooCommercePayloadDryRunInputError(f"{label} must be text{suffix}")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WooCommercePayloadDryRunInputError(
            f"{label} must be a positive integer"
        )
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise WooCommercePayloadDryRunInputError(f"{label} must be boolean")
    return value


def _decimal(value: object, *, label: str, nullable: bool = True) -> Decimal | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise WooCommercePayloadDryRunInputError(f"{label} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise WooCommercePayloadDryRunInputError(
            f"{label} must be numeric"
        ) from error
    if not result.is_finite():
        raise WooCommercePayloadDryRunInputError(f"{label} must be finite")
    return result


def _text_tuple(value: object, *, label: str) -> tuple[str, ...]:
    values = _items(value, label=label)
    if any(not isinstance(item, str) for item in values):
        raise WooCommercePayloadDryRunInputError(
            f"{label} must contain only text"
        )
    return tuple(values)


def _restore_supplier_cost(value: object, *, label: str) -> SupplierCostProvenance:
    payload = _mapping(value, label=label)
    provenance = _mapping(
        payload.get("source_provenance", {}),
        label=f"{label} source provenance",
    )
    amount = _decimal(payload.get("amount"), label=f"{label} amount")
    currency = _text(
        payload.get("currency"), label=f"{label} currency", nullable=True
    )
    return SupplierCostProvenance(
        amount=amount,
        currency=currency,
        raw_values=_text_tuple(
            payload.get("raw_values", []), label=f"{label} raw values"
        ),
        source_coordinates=_text_tuple(
            provenance.get("coordinates", []),
            label=f"{label} coordinates",
        ),
    )


def _restore_component(value: object, *, label: str) -> PricedMappingComponent:
    payload = _mapping(value, label=label)
    supplier = _mapping(payload.get("supplier_cost"), label=f"{label} cost")
    name = _text(payload.get("option_name"), label=f"{label} name")
    category = _text(payload.get("category"), label=f"{label} category")
    coordinate = _text(
        payload.get("source_coordinate"), label=f"{label} source coordinate"
    )
    if name is None or category is None or coordinate is None:  # required guards
        raise AssertionError("Component unexpectedly incomplete")
    if category not in {
        "product_extra_option",
        "appearance",
        "material",
        "function",
        "accessory",
        "other",
    }:
        raise WooCommercePayloadDryRunInputError(
            f"{label} category is unsupported"
        )
    return PricedMappingComponent(
        option_name=name,
        category=category,  # type: ignore[arg-type]
        supplier_cost=AdditionalOptionPricing(
            amount=_decimal(supplier.get("amount"), label=f"{label} cost amount"),
            currency=_text(
                supplier.get("currency"),
                label=f"{label} cost currency",
                nullable=True,
            ),
            raw_price=_text(
                supplier.get("raw_price"),
                label=f"{label} raw price",
                nullable=True,
            ),
        ),
        source_coordinate=coordinate,
    )


def _restore_mapping(value: object, *, label: str) -> PricingMappingSnapshot:
    payload = _mapping(value, label=label)
    mapping_type = _text(
        payload.get("mapping_type"), label=f"{label} type", nullable=True
    )
    if mapping_type not in {None, "exact", "alias", "composite"}:
        raise WooCommercePayloadDryRunInputError(f"{label} type is unsupported")
    category = _text(
        payload.get("catalog_category"),
        label=f"{label} category",
        nullable=True,
    )
    if category not in {
        None,
        "product_extra_option",
        "appearance",
        "material",
        "function",
        "accessory",
        "other",
    }:
        raise WooCommercePayloadDryRunInputError(
            f"{label} category is unsupported"
        )
    components = _items(payload.get("components", []), label=f"{label} components")
    status = _text(payload.get("status"), label=f"{label} status")
    if status is None:  # required guard
        raise AssertionError("Mapping status unexpectedly missing")
    return PricingMappingSnapshot(
        mapping_type=mapping_type,  # type: ignore[arg-type]
        mapping_status=status,
        registry_version=_text(
            payload.get("registry_version"),
            label=f"{label} registry version",
            nullable=True,
        ),
        catalog_option_name=_text(
            payload.get("catalog_option_name"),
            label=f"{label} catalog option",
            nullable=True,
        ),
        catalog_category=category,  # type: ignore[arg-type]
        components=tuple(
            _restore_component(component, label=f"{label} components[{index}]")
            for index, component in enumerate(components)
        ),
        candidate_option_names=_text_tuple(
            payload.get("candidate_option_names", []),
            label=f"{label} candidates",
        ),
        missing_component_names=_text_tuple(
            payload.get("missing_component_names", []),
            label=f"{label} missing components",
        ),
        source_coordinates=_text_tuple(
            payload.get("source_coordinates", []),
            label=f"{label} source coordinates",
        ),
    )


def _restore_presented_option(
    value: object,
    *,
    product_index: int,
    option_index: int,
) -> PresentedUpgradeOption:
    label = f"results[{product_index}] presented options[{option_index}]"
    payload = _mapping(value, label=label)
    economic = _mapping(
        payload.get("economic_pricing"), label=f"{label} economic pricing"
    )
    presentation = _mapping(
        payload.get("presentation"), label=f"{label} presentation"
    )
    supplier = _restore_supplier_cost(
        payload.get("supplier_cost"), label=f"{label} supplier cost"
    )
    target = _decimal(
        economic.get("target_retail_usd"), label=f"{label} economic target"
    )
    display = _decimal(
        presentation.get("display_price_usd"), label=f"{label} display price"
    )
    name = _text(
        payload.get("product_upgrade_name"), label=f"{label} product option name"
    )
    raw_value = _text(
        payload.get("product_raw_value"), label=f"{label} product raw value"
    )
    policy_version = _text(
        economic.get("policy_version"),
        label=f"{label} economic policy version",
        nullable=True,
    ) or "unknown"
    presentation_policy = _text(
        presentation.get("policy_version"),
        label=f"{label} presentation policy version",
        nullable=True,
    ) or "unknown"
    strategy = _text(
        presentation.get("strategy"), label=f"{label} strategy"
    )
    status = _text(presentation.get("status"), label=f"{label} status")
    if name is None or raw_value is None or strategy is None or status is None:
        raise AssertionError("Presented option unexpectedly incomplete")
    priced = PricedLinkedOption(
        product_upgrade_name=name,
        product_raw_value=raw_value,
        mapping=_restore_mapping(
            payload.get("catalog_mapping"), label=f"{label} catalog mapping"
        ),
        supplier_cost=supplier,
        pricing=OptionRetailPricingResult(
            status="priced",
            supplier_cost=SupplierCostSnapshot(
                amount=_decimal(supplier.amount, label=f"{label} supplier amount"),
                currency=supplier.currency,
                raw_value=(supplier.raw_values[0] if supplier.raw_values else None),
            ),
            fx=None,
            calculation=None,
            retail=(
                OptionRetailCandidate(target_retail_usd=target)
                if target is not None
                else None
            ),
            metadata=OptionPricingMetadata(
                policy_version=policy_version,
                warnings=(),
            ),
        ),
        warnings=_text_tuple(payload.get("warnings", []), label=f"{label} warnings"),
    )
    presented = RetailPricePresentationResult(
        economic=EconomicRetailPrice(target_retail_usd=target),
        presentation=PresentedRetailPrice(display_price_usd=display),
        calculation=PresentationCalculation(
            strategy=strategy,  # type: ignore[arg-type]
            candidate_price=_decimal(
                presentation.get("candidate_price"),
                label=f"{label} candidate price",
            ),
            uplift_amount=_decimal(
                presentation.get("uplift_amount"),
                label=f"{label} uplift amount",
            ),
            uplift_rate=_decimal(
                presentation.get("uplift_rate"),
                label=f"{label} uplift rate",
            ),
            fallback_used=_boolean(
                presentation.get("fallback_used"),
                label=f"{label} fallback used",
            ),
        ),
        metadata=PresentationMetadata(policy_version=presentation_policy),
        warnings=(),
        status=status,  # type: ignore[arg-type]
    )
    return PresentedUpgradeOption(priced_option=priced, presentation=presented)


def restore_presented_product_options(
    report: Mapping[str, object],
) -> list[PresentedProductOptions]:
    """Restore presentation results without re-running mapping or pricing."""

    results = _items(report.get("results"), label="presentation results")
    restored: list[PresentedProductOptions] = []
    for product_index, value in enumerate(results):
        label = f"results[{product_index}]"
        payload = _mapping(value, label=label)
        identity = _mapping(payload.get("product_identity"), label=f"{label} identity")
        source = _mapping(payload.get("source_trace"), label=f"{label} source")
        options = _items(
            payload.get("presented_upgrade_options", []),
            label=f"{label} presented options",
        )
        series = _text(payload.get("series"), label=f"{label} series")
        if series is None:  # required guard
            raise AssertionError("Presented product series unexpectedly missing")
        restored.append(
            PresentedProductOptions(
                series=series,
                model=_text(
                    identity.get("model"), label=f"{label} model", nullable=True
                ),
                raw_model=_text(
                    identity.get("raw_model"),
                    label=f"{label} raw model",
                    nullable=True,
                ),
                start_row=_integer(
                    source.get("start_row"), label=f"{label} start row"
                ),
                end_row=_integer(source.get("end_row"), label=f"{label} end row"),
                options=tuple(
                    _restore_presented_option(
                        option,
                        product_index=product_index,
                        option_index=option_index,
                    )
                    for option_index, option in enumerate(options)
                ),
                warnings=_text_tuple(
                    payload.get("warnings", []), label=f"{label} warnings"
                ),
            )
        )
    return restored


def _source_key(product: ProductRecord) -> tuple[int, int]:
    return (product.source.start_row, product.source.end_row)


def _identity_values(product: ProductRecord) -> frozenset[str]:
    values = {
        value.casefold().strip()
        for value in (
            product.identity.model,
            product.identity.raw_model,
            product.specifications.normalized.get("height_model"),
        )
        if isinstance(value, str) and value.strip()
    }
    return frozenset(values)


def _presentation_matches_product(
    product: ProductRecord,
    presentation: PresentedProductOptions,
) -> bool:
    if product.identity.series.casefold().strip() != presentation.series.casefold().strip():
        return False
    product_values = _identity_values(product)
    presentation_values = {
        value.casefold().strip()
        for value in (presentation.model, presentation.raw_model)
        if isinstance(value, str) and value.strip()
    }
    return not product_values or not presentation_values or bool(
        product_values.intersection(presentation_values)
    )


def _has_public_leak(value: object) -> bool:
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    return bool(
        _URL_PATTERN.search(serialized)
        or _CREDENTIAL_PATTERN.search(serialized)
        or REPORT_SECRET_SCAN_PATTERN.search(serialized)
        or _INTERNAL_PUBLIC_PATTERN.search(serialized)
        or _COORDINATE_PATTERN.search(serialized)
        or _SUPPLIER_PRICE_PATTERN.search(serialized)
    )


def scan_public_surfaces(candidate: Mapping[str, object]) -> tuple[str, ...]:
    """Scan only customer-facing candidate surfaces for internal data."""

    issues: list[str] = []
    payload = candidate.get("payload", {})
    if not isinstance(payload, Mapping) or set(payload).difference(
        WOO_CORE_PAYLOAD_ALLOWLIST
    ):
        issues.append("unsafe_payload_key")
    for field in ("payload", "storefront_options", "public_content"):
        if _has_public_leak(candidate.get(field, {})):
            issues.append(UNSAFE_PUBLIC_ISSUE)
    return tuple(dict.fromkeys(issues))


def _scrub_public_value(value: object) -> object:
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            if _has_public_leak(str(key)):
                continue
            cleaned[str(key)] = _scrub_public_value(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_public_value(item) for item in value]
    if isinstance(value, str) and _has_public_leak(value):
        return "[REDACTED]"
    return value


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved = input_path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def _candidate_report(
    candidate: WooCommerceProductPayloadCandidate,
    *,
    extra_blockers: Sequence[str] = (),
) -> tuple[dict[str, object], bool]:
    raw = candidate.to_dict()
    leak_issues = scan_public_surfaces(raw)
    validation_issues = woocommerce_product_mapper.validate_woocommerce_product_payload(
        candidate
    )
    errors = _unique(
        (
            *candidate.blocking_issues,
            *extra_blockers,
            *validation_issues,
            *leak_issues,
        )
    )
    raw["blocking_issues"] = list(errors)
    raw["warnings"] = list(candidate.warnings)
    raw["ready_for_write"] = False
    for field in ("payload", "storefront_options", "public_content"):
        raw[field] = _scrub_public_value(raw.get(field, {}))
    raw["validation"] = {
        "valid": not errors,
        "errors": list(errors),
        "warnings": list(candidate.warnings),
    }
    return raw, bool(leak_issues)


def build_woocommerce_payload_report(
    products: Sequence[ProductRecord],
    sizes: Sequence[object],
    presented_products: Sequence[PresentedProductOptions],
    *,
    product_input_file: str,
    size_input_file: str,
    presented_option_input_file: str,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Delegate to the existing enricher and mapper, then validate the report."""

    size_results = product_size_enricher.enrich_products_with_sizes(products, sizes)
    sku_batch = sku_policy.validate_sku_uniqueness(products)
    if len(sku_batch.results) != len(products):
        raise WooCommercePayloadDryRunInputError(
            "SKU policy result count was inconsistent"
        )
    size_by_source: dict[tuple[int, int], list[object]] = defaultdict(list)
    product_by_source: dict[tuple[int, int], list[ProductRecord]] = defaultdict(list)
    presentation_by_source: dict[
        tuple[int, int], list[PresentedProductOptions]
    ] = defaultdict(list)
    for product, sku_result in zip(products, sku_batch.results, strict=True):
        product_by_source[_source_key(product)].append(product)
    for result in size_results:
        size_by_source[_source_key(result.product)].append(result)
    for presentation in presented_products:
        presentation_by_source[
            (presentation.start_row, presentation.end_row)
        ].append(presentation)

    candidates: list[dict[str, object]] = []
    unsafe_payloads = 0
    for product in products:
        key = _source_key(product)
        join_blockers: list[str] = []
        size_candidates = size_by_source.get(key, [])
        size_result = size_candidates[0] if len(size_candidates) == 1 else None
        if len(product_by_source[key]) != 1 or len(size_candidates) != 1:
            join_blockers.append(AMBIGUOUS_JOIN_ISSUE)

        presentation_candidates = presentation_by_source.get(key, [])
        presented_options: tuple[PresentedUpgradeOption, ...] = ()
        if len(presentation_candidates) == 1 and _presentation_matches_product(
            product, presentation_candidates[0]
        ):
            presented_options = presentation_candidates[0].options
        elif presentation_candidates:
            join_blockers.append(AMBIGUOUS_JOIN_ISSUE)

        mapped = woocommerce_product_mapper.build_woocommerce_product_payload(
            product,
            sku_result=sku_result,
            size_enrichment=size_result,  # type: ignore[arg-type]
            presented_options=presented_options,
        )
        if join_blockers:
            mapped = replace(
                mapped,
                blocking_issues=_unique((*mapped.blocking_issues, *join_blockers)),
                ready_for_write=False,
            )
        candidate_report, unsafe = _candidate_report(mapped)
        candidates.append(candidate_report)
        unsafe_payloads += int(unsafe)

    def blockers(candidate: Mapping[str, object]) -> tuple[str, ...]:
        raw = candidate.get("blocking_issues", [])
        return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()

    def match_status(candidate: Mapping[str, object]) -> str:
        audit = candidate.get("audit")
        return str(audit.get("size_match_status")) if isinstance(audit, Mapping) else "not_provided"

    summary = {
        "total_products": len(products),
        "candidates_built": len(candidates),
        "candidates_with_blocking_issues": sum(bool(blockers(item)) for item in candidates),
        "candidates_without_blocking_issues": sum(not blockers(item) for item in candidates),
        "candidates_with_storefront_options": sum(bool(item.get("storefront_options")) for item in candidates),
        "candidates_without_storefront_options": sum(not item.get("storefront_options") for item in candidates),
        "missing_product_name": sum("missing_product_name" in blockers(item) for item in candidates),
        "missing_base_retail_price": sum("missing_base_retail_price" in blockers(item) for item in candidates),
        "unsupported_base_price_currency": sum("unsupported_base_price_currency" in blockers(item) for item in candidates),
        "products_with_sku": sum(
            isinstance(item.get("payload"), Mapping)
            and isinstance(item["payload"].get("sku"), str)  # type: ignore[union-attr]
            for item in candidates
        ),
        "products_without_sku": sum(
            not (
                isinstance(item.get("payload"), Mapping)
                and isinstance(item["payload"].get("sku"), str)  # type: ignore[union-attr]
            )
            for item in candidates
        ),
        "sku_missing_count": sum(
            "missing_sku" in blockers(item) for item in candidates
        ),
        "size_matched": sum(match_status(item) == "matched" for item in candidates),
        "size_unmatched": sum(match_status(item) == "unmatched" for item in candidates),
        "size_ambiguous": sum(match_status(item) == "ambiguous" for item in candidates),
        "unsafe_payloads": unsafe_payloads,
        "draft_products": sum(
            isinstance(item.get("payload"), Mapping)
            and item["payload"].get("status") == "draft"  # type: ignore[union-attr]
            for item in candidates
        ),
        "simple_products": sum(
            isinstance(item.get("payload"), Mapping)
            and item["payload"].get("type") == "simple"  # type: ignore[union-attr]
            for item in candidates
        ),
        "ready_for_write_count": 0,
    }
    report: dict[str, object] = {
        "status": "ok",
        "inputs": {
            "products": product_input_file,
            "sizes": size_input_file,
            "presented_options": presented_option_input_file,
        },
        "summary": summary,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "candidates": candidates,
    }
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("WooCommerce payload report must remain an object")
    return sanitized


def run_woocommerce_payload_dry_run(
    product_input_path: Path,
    size_input_path: Path,
    presented_option_input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read three local reports and write one local candidate report."""

    product_path = Path(product_input_path)
    size_path = Path(size_input_path)
    presented_path = Path(presented_option_input_path)
    products = restore_product_records(load_local_json_report(product_path))
    sizes = restore_size_records(load_local_json_report(size_path))
    presented = restore_presented_product_options(
        load_local_json_report(presented_path)
    )
    active_redactor = redactor or Redactor()
    report = build_woocommerce_payload_report(
        products,
        sizes,
        presented,
        product_input_file=_safe_input_reference(product_path, project_root),
        size_input_file=_safe_input_reference(size_path, project_root),
        presented_option_input_file=_safe_input_reference(
            presented_path, project_root
        ),
        redactor=active_redactor,
    )
    output_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output_path, active_redactor).write(report)
    return report, output_path
