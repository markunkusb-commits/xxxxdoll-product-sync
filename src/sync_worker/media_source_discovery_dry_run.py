"""Secure exact-cell read to Media Source Discovery report adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import GoogleSettings
from .google_api import GoogleSheetsReadonlyClientFactory
from .media_source_discovery import (
    MediaSourceDiscoveryResult,
    discover_media_source,
)
from .product_size_enrichment_dry_run import load_local_json_report
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .secure_media_reference_read import (
    SecureMediaReferenceReadBatch,
    SecureMediaReferenceReadResult,
    SecureMediaReferenceReader,
    ValidatedMappedMediaSource,
)
from .sku_dry_run import is_safe_sku
from .sku_policy import SKU_POLICY_VERSION, SkuAudit, SkuGenerationResult


REPORT_FILENAME = "media-source-discovery-dry-run.json"
_UNSAFE_REPORT_PATTERN = re.compile(
    r"(?i)https?://|user:[^\s@]+@|"
    r"(?:consumer_key|consumer_secret|access_token|token|signature|auth|"
    r"key|password)\s*=|\b(?:authorization|cookie)\b"
)


class MediaSourceDiscoveryDryRunInputError(ValueError):
    """Safe local report error without supplier cell contents."""


@dataclass(frozen=True, slots=True)
class VerifiedSkuEntry:
    product_start_row: int
    product_end_row: int
    series: str
    product_identity: str
    result: SkuGenerationResult


def _mapping(value: object, error_code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MediaSourceDiscoveryDryRunInputError(error_code)
    return value


def _array(value: object, error_code: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise MediaSourceDiscoveryDryRunInputError(error_code)
    return value


def _text(value: object, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaSourceDiscoveryDryRunInputError(error_code)
    return value


def _positive_row(value: object, error_code: str) -> int:
    if type(value) is not int or value <= 0:
        raise MediaSourceDiscoveryDryRunInputError(error_code)
    return value


def _text_tuple(value: object, error_code: str) -> tuple[str, ...]:
    items = _array(value, error_code)
    if not all(isinstance(item, str) for item in items):
        raise MediaSourceDiscoveryDryRunInputError(error_code)
    return tuple(items)


def restore_verified_sku_entries(
    report: Mapping[str, object] | None,
) -> tuple[VerifiedSkuEntry, ...]:
    """Restore only verified SKU results carrying stable Product source rows."""

    if report is None:
        return ()
    if (
        report.get("status") != "ok"
        or report.get("policy_version") != SKU_POLICY_VERSION
    ):
        raise MediaSourceDiscoveryDryRunInputError("sku_report_not_verified")
    source_bindings: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    raw_bindings = report.get("product_source_bindings", [])
    for raw_binding in _array(raw_bindings, "invalid_sku_source_bindings"):
        binding = _mapping(raw_binding, "invalid_sku_source_binding")
        binding_sku = binding.get("sku")
        binding_series = binding.get("series")
        binding_identity = binding.get("product_identity")
        binding_source = binding.get("product_source")
        if (
            isinstance(binding_sku, str)
            and isinstance(binding_series, str)
            and isinstance(binding_identity, str)
            and isinstance(binding_source, Mapping)
        ):
            source_bindings.setdefault(
                (binding_sku, binding_series, binding_identity), []
            ).append(binding_source)
    entries: list[VerifiedSkuEntry] = []
    for raw_result in _array(report.get("results"), "sku_results_missing"):
        item = _mapping(raw_result, "invalid_sku_result")
        if item.get("status") != "ok":
            continue
        if _text_tuple(
            item.get("blocking_issues", []), "invalid_sku_blocking_issues"
        ):
            continue
        sku = item.get("sku")
        if not is_safe_sku(sku):
            continue
        source_value = item.get("product_source")
        if source_value is None:
            matched_bindings = source_bindings.get(
                (str(sku), str(item.get("series")), str(item.get("product_identity"))),
                [],
            )
            if len(matched_bindings) != 1:
                continue
            source_value = matched_bindings[0]
        source = _mapping(source_value, "sku_source_missing")
        start_row = _positive_row(
            source.get("start_row"), "invalid_sku_product_source"
        )
        end_row = _positive_row(
            source.get("end_row"), "invalid_sku_product_source"
        )
        if end_row < start_row:
            raise MediaSourceDiscoveryDryRunInputError(
                "invalid_sku_product_source"
            )
        series = _text(item.get("series"), "invalid_sku_identity")
        identity = _text(
            item.get("product_identity") or item.get("raw_identity"),
            "invalid_sku_identity",
        )
        policy_version = _text(
            item.get("policy_version"), "invalid_sku_policy_version"
        )
        if policy_version != SKU_POLICY_VERSION:
            raise MediaSourceDiscoveryDryRunInputError(
                "invalid_sku_policy_version"
            )
        audit_payload = item.get("audit")
        audit = audit_payload if isinstance(audit_payload, Mapping) else {}
        identity_source = audit.get("identity_source")
        if identity_source not in {"model", "raw_model", "height_model", "none"}:
            identity_source = "none"
        namespace = audit.get("series_namespace")
        if namespace is not None and not isinstance(namespace, str):
            namespace = None
        sku_value = str(sku)
        result = SkuGenerationResult(
            status="ok",
            sku=sku_value,
            series=series,
            raw_identity=identity,
            normalized_identity=(
                item.get("normalized_identity")
                if isinstance(item.get("normalized_identity"), str)
                else None
            ),
            policy_version=policy_version,
            warnings=(),
            blocking_issues=(),
            conflicting_product_identities=(),
            audit=SkuAudit(
                policy_version=policy_version,
                identity_source=identity_source,  # type: ignore[arg-type]
                series_namespace=namespace,
            ),
        )
        entries.append(
            VerifiedSkuEntry(
                product_start_row=start_row,
                product_end_row=end_row,
                series=series,
                product_identity=identity,
                result=result,
            )
        )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.product_start_row,
                entry.product_end_row,
                entry.product_identity,
                entry.result.sku or "",
            ),
        )
    )


def join_verified_sku(
    mapped: ValidatedMappedMediaSource,
    entries: Sequence[VerifiedSkuEntry],
    *,
    sku_report_provided: bool = True,
) -> tuple[SkuGenerationResult | None, tuple[str, ...]]:
    """Join on one exact source range, validating identity when available."""

    range_candidates = tuple(
        entry
        for entry in entries
        if entry.product_start_row == mapped.product_source.start_row
        and entry.product_end_row == mapped.product_source.end_row
    )
    if not range_candidates:
        return (
            (None, ("sku_join_not_found",))
            if sku_report_provided
            else (None, ())
        )
    candidates_by_sku: dict[str, list[VerifiedSkuEntry]] = {}
    for entry in range_candidates:
        sku = entry.result.sku
        if sku is not None:
            candidates_by_sku.setdefault(sku, []).append(entry)
    if len(candidates_by_sku) != 1:
        return None, ("sku_join_ambiguous",)
    candidates = next(iter(candidates_by_sku.values()))
    mapped_identities = frozenset(mapped.product_identity_values)
    if mapped_identities:
        identity_matches = tuple(
            entry
            for entry in candidates
            if entry.product_identity in mapped_identities
        )
        if not identity_matches:
            return None, ("sku_join_identity_conflict",)
        candidates = identity_matches
    return candidates[0].result, ()


def _normalized_snapshot_reference(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return PurePosixPath(value.strip().replace("\\", "/")).as_posix()


def validate_sku_snapshot_compatibility(
    mapping_report: Mapping[str, object],
    sku_report: Mapping[str, object] | None,
) -> None:
    """Prevent a supplied SKU report from crossing Product snapshots."""

    if sku_report is None:
        return
    mapping_inputs = mapping_report.get("inputs")
    mapping_product_input = (
        mapping_inputs.get("products")
        if isinstance(mapping_inputs, Mapping)
        else None
    )
    mapping_snapshot = _normalized_snapshot_reference(mapping_product_input)
    sku_snapshot = _normalized_snapshot_reference(sku_report.get("input_file"))
    if mapping_snapshot is None or sku_snapshot is None:
        raise MediaSourceDiscoveryDryRunInputError(
            "sku_snapshot_provenance_missing"
        )
    if mapping_snapshot != sku_snapshot:
        raise MediaSourceDiscoveryDryRunInputError("sku_snapshot_mismatch")


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def discover_from_secure_read_result(
    read_result: SecureMediaReferenceReadResult,
    sku_result: SkuGenerationResult | None,
) -> MediaSourceDiscoveryResult | None:
    """Classify only the fresh, in-memory supplier reference from Sheets.

    Report projections and ``MediaSourceMappingResult`` values are deliberately
    outside this helper's input type so their redacted reference cannot become a
    provider-classification input.
    """

    if not isinstance(read_result, SecureMediaReferenceReadResult):
        raise TypeError("read_result must be a SecureMediaReferenceReadResult")
    source = read_result.to_supplier_reference()
    if source is None:
        return None
    if source.raw_reference != read_result.raw_reference:
        raise MediaSourceDiscoveryDryRunInputError(
            "fresh_media_reference_handoff_failed"
        )
    return discover_media_source(source, sku_result=sku_result)


def _result_report(
    read_result: SecureMediaReferenceReadResult,
    sku_entries: Sequence[VerifiedSkuEntry],
    *,
    sku_report_provided: bool,
) -> tuple[dict[str, object], MediaSourceDiscoveryResult | None]:
    mapped = read_result.mapped_source
    sku_result, sku_warnings = join_verified_sku(
        mapped,
        sku_entries,
        sku_report_provided=sku_report_provided,
    )
    discovery = discover_from_secure_read_result(
        read_result,
        sku_result,
    )
    warnings = list(read_result.warnings)
    warnings.extend(sku_warnings)
    blockers = list(read_result.blocking_issues)
    if discovery is not None:
        warnings.extend(discovery.warnings)
        blockers.extend(discovery.blocking_issues)
    report = {
        "sku": sku_result.sku if sku_result is not None else None,
        "product_source": mapped.product_source.to_dict(),
        "marker_coordinate": mapped.marker_coordinate,
        "reference_coordinate": mapped.reference_coordinate,
        "reference_verification": read_result.reference_verification,
        "provider": discovery.provider if discovery is not None else "unknown",
        "resource_kind": (
            discovery.resource_kind if discovery is not None else "unknown"
        ),
        "safe_host": discovery.safe_host if discovery is not None else None,
        "safe_path_hint": (
            discovery.safe_path_hint if discovery is not None else None
        ),
        "reference_fingerprint": (
            discovery.reference_fingerprint if discovery is not None else None
        ),
        "resource_id_fingerprint": (
            discovery.resource_id_fingerprint if discovery is not None else None
        ),
        "requires_provider_api": (
            discovery.requires_provider_api if discovery is not None else False
        ),
        "requires_http_probe": (
            discovery.requires_http_probe if discovery is not None else False
        ),
        "download_ready": False,
        "warnings": list(_unique(warnings)),
        "blocking_issues": list(_unique(blockers)),
    }
    return report, discovery


def _assert_report_safe(report: Mapping[str, object]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if (
        REPORT_SECRET_SCAN_PATTERN.search(serialized)
        or _UNSAFE_REPORT_PATTERN.search(serialized)
    ):
        raise MediaSourceDiscoveryDryRunInputError(
            "unsafe_media_reference_leak"
        )


def build_media_source_discovery_report(
    read_batch: SecureMediaReferenceReadBatch,
    *,
    mapping_input_file: str,
    sheet_title: str,
    sku_report_input_file: str | None,
    sku_report: Mapping[str, object] | None = None,
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Call the existing Discovery Core and project one safe report."""

    sku_entries = restore_verified_sku_entries(sku_report)
    sku_report_provided = sku_report is not None
    projected: list[dict[str, object]] = []
    discoveries: list[MediaSourceDiscoveryResult] = []
    for read_result in read_batch.results:
        result, discovery = _result_report(
            read_result,
            sku_entries,
            sku_report_provided=sku_report_provided,
        )
        projected.append(result)
        if discovery is not None:
            discoveries.append(discovery)
    projected.sort(
        key=lambda item: (
            item["product_source"]["start_row"],  # type: ignore[index]
            item["reference_coordinate"],
        )
    )
    summary = {
        "total_mapped_sources": len(read_batch.results),
        "coordinates_requested": read_batch.coordinates_requested,
        "references_read": sum(
            item.read_status == "read" for item in read_batch.results
        ),
        "verified_unchanged": sum(
            item.reference_verification == "verified_unchanged"
            for item in read_batch.results
        ),
        "reference_changed_since_mapping": sum(
            item.reference_verification == "reference_changed_since_mapping"
            for item in read_batch.results
        ),
        "classified_sources": sum(
            item.discovery_status == "classified" for item in discoveries
        ),
        "redacted_or_unclassifiable_sources": (
            len(read_batch.results)
            - sum(item.discovery_status == "classified" for item in discoveries)
        ),
        "google_drive_sources": sum(
            item.provider == "google_drive" for item in discoveries
        ),
        "dropbox_sources": sum(item.provider == "dropbox" for item in discoveries),
        "onedrive_sources": sum(item.provider == "onedrive" for item in discoveries),
        "sharepoint_sources": sum(
            item.provider == "sharepoint" for item in discoveries
        ),
        "direct_web_sources": sum(
            item.provider == "direct_web" for item in discoveries
        ),
        "unknown_sources": (
            len(read_batch.results)
            - sum(item.provider != "unknown" for item in discoveries)
        ),
        "folder_candidates": sum(
            item.resource_kind == "folder" for item in discoveries
        ),
        "file_candidates": sum(
            item.resource_kind == "file" for item in discoveries
        ),
        "direct_image_candidates": sum(
            item.resource_kind == "direct_image_candidate"
            for item in discoveries
        ),
        "archive_candidates": sum(
            item.resource_kind == "archive_candidate" for item in discoveries
        ),
        "blocked_sources": sum(
            bool(item["blocking_issues"]) for item in projected
        ),
        "cell_missing": sum(
            item.read_status
            in {
                "media_reference_response_missing",
                "media_reference_cell_missing",
            }
            for item in read_batch.results
        ),
        "cell_empty": sum(
            item.read_status == "empty_media_reference"
            for item in read_batch.results
        ),
        "sku_joined": sum(item["sku"] is not None for item in projected),
        "sku_join_not_found": sum(
            "sku_join_not_found" in item["warnings"] for item in projected
        ),
        "sku_join_ambiguous": sum(
            "sku_join_ambiguous" in item["warnings"] for item in projected
        ),
    }
    report: dict[str, object] = {
        "status": "ok",
        "inputs": {
            "mapping": mapping_input_file,
            "sheet": sheet_title,
            "sku_report": sku_report_input_file,
        },
        "summary": summary,
        "read_requests_performed": read_batch.read_requests_performed,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": projected,
    }
    _assert_report_safe(report)
    sanitized = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(sanitized, dict):
        raise TypeError("Media Source Discovery report must be an object")
    _assert_report_safe(sanitized)
    return sanitized


def _safe_input_reference(input_path: Path, project_root: Path) -> str:
    resolved = input_path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return input_path.name


def run_media_source_discovery_dry_run(
    mapping_input_path: Path,
    sheet_title: str,
    settings: GoogleSettings,
    client_factory: GoogleSheetsReadonlyClientFactory,
    *,
    project_root: Path,
    sku_report_input_path: Path | None = None,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read local reports, batch-read approved cells, and write safely."""

    mapping_path = Path(mapping_input_path)
    mapping_report = load_local_json_report(mapping_path)
    sku_path = Path(sku_report_input_path) if sku_report_input_path else None
    sku_report = load_local_json_report(sku_path) if sku_path is not None else None
    validate_sku_snapshot_compatibility(mapping_report, sku_report)
    mapping_input_reference = _safe_input_reference(mapping_path, project_root)
    sku_input_reference = (
        _safe_input_reference(sku_path, project_root)
        if sku_path is not None
        else None
    )
    _assert_report_safe(
        {
            "inputs": {
                "mapping": mapping_input_reference,
                "sheet": sheet_title,
                "sku_report": sku_input_reference,
            }
        }
    )
    read_batch = SecureMediaReferenceReader(settings, client_factory).run(
        mapping_report,
        sheet_title=sheet_title,
    )
    active_redactor = redactor or Redactor()
    report = build_media_source_discovery_report(
        read_batch,
        mapping_input_file=mapping_input_reference,
        sheet_title=sheet_title,
        sku_report_input_file=sku_input_reference,
        sku_report=sku_report,
        redactor=active_redactor,
    )
    report_path = project_root / "reports" / REPORT_FILENAME
    _assert_report_safe(report)
    SafeJsonReportWriter(report_path, active_redactor).write(report)
    return report, report_path
