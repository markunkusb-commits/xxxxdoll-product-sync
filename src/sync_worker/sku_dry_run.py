"""Pure-local SKU policy reality-check report adapter."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import sku_policy
from .product_model import ProductRecord
from .product_size_enrichment_dry_run import (
    load_local_json_report,
    restore_product_records,
)
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .sku_policy import SKU_POLICY_VERSION, SkuGenerationResult


REPORT_FILENAME = "sku-dry-run.json"
_SKU_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_FORBIDDEN_SKU_TOKENS = frozenset(
    {
        "FOB",
        "RMB",
        "USD",
        "SUPPLIER",
        "COST",
        "PRICE",
        "ROW",
        "SOURCE",
        "TIMESTAMP",
        "UUID",
    }
)


class SkuDryRunInputError(ValueError):
    """Safe structural error for an explicitly supplied local Product report."""


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved = input_path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def is_safe_sku(value: object) -> bool:
    """Validate the public SKU alphabet and internal-data token boundary."""

    if not isinstance(value, str) or _SKU_PATTERN.fullmatch(value) is None:
        return False
    return not _FORBIDDEN_SKU_TOKENS.intersection(value.split("-"))


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _result_report(
    result: SkuGenerationResult,
    product: ProductRecord,
) -> dict[str, object]:
    blocking_issues = result.blocking_issues
    status = result.status
    if result.sku is not None and not is_safe_sku(result.sku):
        blocking_issues = _unique((*blocking_issues, "unsafe_sku_output"))
        status = "invalid_identity"
    return {
        "series": result.series,
        "product_identity": result.raw_identity,
        "raw_identity": result.raw_identity,
        "normalized_identity": result.normalized_identity,
        "sku": result.sku,
        "product_source": {
            "start_row": product.source.start_row,
            "end_row": product.source.end_row,
        },
        "status": status,
        "policy_version": result.policy_version,
        "warnings": list(result.warnings),
        "blocking_issues": list(blocking_issues),
        "conflicting_product_identities": list(
            result.conflicting_product_identities
        ),
        "audit": {
            "policy_version": result.audit.policy_version,
            "identity_source": result.audit.identity_source,
            "series_namespace": result.audit.series_namespace,
        },
    }


def _validate_policy_consistency(
    individual: Sequence[SkuGenerationResult],
    batch: Sequence[SkuGenerationResult],
) -> None:
    if len(individual) != len(batch):
        raise SkuDryRunInputError("SKU policy result count was inconsistent")
    for single, validated in zip(individual, batch, strict=True):
        if (
            single.sku != validated.sku
            or single.series != validated.series
            or single.raw_identity != validated.raw_identity
            or single.normalized_identity != validated.normalized_identity
            or single.policy_version != validated.policy_version
        ):
            raise SkuDryRunInputError("SKU policy results were not deterministic")


def build_sku_dry_run_report(
    products: Sequence[ProductRecord],
    *,
    input_file: str,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Call the existing SKU policy and project an allowlisted local report."""

    if isinstance(products, (str, bytes)) or not isinstance(products, Sequence):
        raise SkuDryRunInputError("products must be a sequence")
    individual = [sku_policy.generate_sku(product) for product in products]
    batch = sku_policy.validate_sku_uniqueness(products)
    _validate_policy_consistency(individual, batch.results)
    results = [
        _result_report(result, product)
        for result, product in zip(batch.results, products, strict=True)
    ]

    def status_count(status: str) -> int:
        return sum(result["status"] == status for result in results)

    report: dict[str, object] = {
        "status": "ok",
        "policy_version": SKU_POLICY_VERSION,
        "input_file": input_file,
        "summary": {
            "total_products": len(products),
            "generated_skus": status_count("ok"),
            "missing_identity": status_count("missing_identity"),
            "unsupported_series": status_count("unsupported_series"),
            "invalid_identity": status_count("invalid_identity"),
            "collision_count": len(batch.collisions),
            "duplicate_input_count": len(batch.duplicate_inputs),
            "sku_too_long": status_count("too_long"),
        },
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": results,
    }
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):  # pragma: no cover - structural guard
        raise AssertionError("SKU dry-run report must remain an object")
    return sanitized


def run_sku_dry_run(
    product_input_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Restore a local CLM report and write one local SKU candidate report."""

    input_path = Path(product_input_path)
    product_report = load_local_json_report(input_path)
    products = restore_product_records(product_report)
    active_redactor = redactor or Redactor()
    report = build_sku_dry_run_report(
        products,
        input_file=_safe_input_reference(input_path, project_root),
        redactor=active_redactor,
    )
    output_path = project_root / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output_path, active_redactor).write(report)
    return report, output_path
