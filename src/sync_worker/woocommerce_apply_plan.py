"""Pure-local frozen Woo CREATE plan and deterministic PLAN_HASH V1."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_product_mapper as woo_mapper
from . import woocommerce_target_snapshot as target_snapshot
from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .woo_category_binding import STAGING_EXPECTED_HOST


POLICY_VERSION = "xxxxdoll-woo-frozen-apply-plan-v1"
REPORT_FILENAME = "woo-apply-plan.json"
ACTION = "create"
_SEMANTIC_FIELDS = (
    "policy_version",
    "target",
    "operation",
    "preconditions",
    "source_package",
    "source_target_snapshot",
)
_ZERO_COUNTERS = (
    "network_requests_performed",
    "woocommerce_requests_performed",
    "woocommerce_write_requests_performed",
    "wordpress_requests_performed",
    "external_write_requests_performed",
    "write_requests_performed",
)
_SNAPSHOT_ZERO_COUNTERS = (
    "woocommerce_write_requests_performed",
    "wordpress_requests_performed",
    "external_write_requests_performed",
    "write_requests_performed",
)
_PAYLOAD_ALLOWLIST = woo_mapper.WOO_CORE_PAYLOAD_ALLOWLIST | {"images"}


class WooApplyPlanError(ValueError):
    """Base class for fixed-code, local-only Freeze failures."""


class WooApplyPlanInputError(WooApplyPlanError):
    """Unsafe local path, JSON, fingerprint, or input contract."""


class WooApplyPlanPayloadError(WooApplyPlanError):
    """The final Package payload is not safe to freeze unchanged."""


def _read_target_snapshot(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    """Read and fingerprint one Snapshot with the POC-03A hardened policy."""

    try:
        local = target_snapshot._safe_local_json_file(path)
        size = local.stat().st_size
        if size <= 0 or size > target_snapshot.MAX_PACKAGE_BYTES:
            raise WooApplyPlanInputError("woo_apply_plan_snapshot_size_invalid")
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooApplyPlanInputError:
        raise
    except target_snapshot.WooTargetSnapshotInputError as error:
        code = (
            "woo_apply_plan_snapshot_duplicate_json_key"
            if str(error) == "woo_target_snapshot_duplicate_json_key"
            else "woo_apply_plan_local_snapshot_required"
        )
        raise WooApplyPlanInputError(code) from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooApplyPlanInputError(
            "woo_apply_plan_snapshot_json_invalid"
        ) from None
    if not isinstance(value, Mapping):
        raise WooApplyPlanInputError("woo_apply_plan_snapshot_root_invalid")
    return (
        value,
        {"basename": local.name, "sha256": hashlib.sha256(raw).hexdigest()},
        local,
    )


def _read_package(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    """Reuse the complete POC-03A Package reader without weakening it."""

    try:
        return target_snapshot.read_package_report(path)
    except target_snapshot.WooTargetSnapshotInputError:
        raise WooApplyPlanInputError("woo_apply_plan_local_package_required") from None


def _safe_fingerprint(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != {"basename", "sha256"}:
        return None
    basename, digest = value.get("basename"), value.get("sha256")
    if (
        type(basename) is not str
        or not basename
        or Path(basename).name != basename
        or not basename.casefold().endswith(".json")
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return None
    return {"basename": basename, "sha256": digest}


def _snapshot_target(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or (
        value.get("environment") != "staging"
        or value.get("source_host") != STAGING_EXPECTED_HOST
        or value.get("api_version") != target_snapshot.API_VERSION
        or value.get("resource") != target_snapshot.API_RESOURCE
        or value.get("read_only") is not True
    ):
        raise WooApplyPlanInputError("woo_apply_plan_snapshot_target_invalid")
    return {
        "environment": "staging",
        "source_host": STAGING_EXPECTED_HOST,
        "api_version": target_snapshot.API_VERSION,
        "resource": target_snapshot.API_RESOURCE,
    }


def _validate_snapshot_common(
    snapshot: Mapping[str, object],
    *,
    sku: str,
    package_source: Mapping[str, str],
) -> tuple[dict[str, str], int, bool]:
    if (
        snapshot.get("policy_version") != target_snapshot.POLICY_VERSION
        or snapshot.get("write_authorized") is not False
        or snapshot.get("sku") != sku
        or any(
            type(snapshot.get(counter)) is not int
            or snapshot.get(counter) != 0
            for counter in _SNAPSHOT_ZERO_COUNTERS
        )
    ):
        raise WooApplyPlanInputError("woo_apply_plan_snapshot_contract_invalid")
    target = _snapshot_target(snapshot.get("target"))
    source_package = _safe_fingerprint(snapshot.get("source_package"))
    if source_package != dict(package_source):
        raise WooApplyPlanInputError(
            "woo_apply_plan_package_snapshot_mismatch"
        )
    network_count = snapshot.get("network_requests_performed")
    woo_count = snapshot.get("woocommerce_requests_performed")
    if (
        type(network_count) is not int
        or network_count <= 0
        or type(woo_count) is not int
        or woo_count != network_count
    ):
        raise WooApplyPlanInputError(
            "woo_apply_plan_snapshot_request_count_invalid"
        )
    match_count = snapshot.get("match_count")
    create_eligible = snapshot.get("create_eligible")
    if type(match_count) is not int or match_count < 0 or type(create_eligible) is not bool:
        raise WooApplyPlanInputError("woo_apply_plan_snapshot_contract_invalid")
    return target, match_count, create_eligible


def _snapshot_disposition(
    snapshot: Mapping[str, object],
    *,
    match_count: int,
    create_eligible: bool,
) -> str | None:
    status = snapshot.get("status")
    blockers = snapshot.get("blocking_issues")
    existing = snapshot.get("existing_target")
    if (
        status == "ok"
        and blockers == []
        and match_count == 0
        and create_eligible is True
        and existing is None
    ):
        return None
    if (
        status == "blocked"
        and create_eligible is False
        and match_count == 1
        and isinstance(existing, Mapping)
        and blockers == ["woo_target_sku_already_exists"]
    ):
        return "woo_apply_plan_target_sku_already_exists"
    if (
        status == "blocked"
        and create_eligible is False
        and match_count > 1
        and existing is None
        and blockers == ["woo_target_sku_ambiguous"]
    ):
        return "woo_apply_plan_target_sku_ambiguous"
    raise WooApplyPlanInputError("woo_apply_plan_snapshot_contract_invalid")


def _frozen_payload(package: Mapping[str, object], sku: str) -> dict[str, object]:
    payload = package.get("future_woo_payload")
    if not isinstance(payload, Mapping):
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_invalid")
    if set(payload).difference(_PAYLOAD_ALLOWLIST):
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_surface_unsafe")
    name = payload.get("name")
    regular_price = payload.get("regular_price")
    if (
        not isinstance(name, str)
        or not name.strip()
        or payload.get("sku") != sku
        or payload.get("status") != "draft"
        or payload.get("type") != "simple"
        or not isinstance(regular_price, str)
        or woo_mapper._USD_PRICE_PATTERN.fullmatch(regular_price) is None
        or woo_mapper._payload_has_internal_data(payload)
    ):
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_invalid")

    categories = payload.get("categories")
    if not isinstance(categories, list) or len(categories) != 1:
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_categories_invalid")
    category = categories[0]
    if (
        not isinstance(category, Mapping)
        or set(category) != woo_mapper._CATEGORY_PAYLOAD_KEYS
        or type(category.get("id")) is not int
        or category["id"] <= 0
    ):
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_categories_invalid")

    attributes = payload.get("attributes")
    if not isinstance(attributes, list):
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_attributes_invalid")
    for expected_position, attribute in enumerate(attributes):
        if (
            not isinstance(attribute, Mapping)
            or set(attribute) != woo_mapper._ATTRIBUTE_KEYS
            or attribute.get("name") not in woo_mapper._PUBLIC_ATTRIBUTE_NAME_SET
            or type(attribute.get("position")) is not int
            or attribute.get("position") != expected_position
            or attribute.get("visible") is not True
            or attribute.get("variation") is not False
        ):
            raise WooApplyPlanPayloadError(
                "woo_apply_plan_payload_attributes_invalid"
            )
        options = attribute.get("options")
        if (
            not isinstance(options, list)
            or len(options) != 1
            or not isinstance(options[0], str)
            or not options[0].strip()
        ):
            raise WooApplyPlanPayloadError(
                "woo_apply_plan_payload_attributes_invalid"
            )

    for field in ("description", "short_description"):
        if field in payload and not isinstance(payload[field], str):
            raise WooApplyPlanPayloadError("woo_apply_plan_payload_invalid")

    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_images_invalid")
    image_ids: list[int] = []
    for image in images:
        if (
            not isinstance(image, Mapping)
            or set(image) != {"id"}
            or type(image.get("id")) is not int
            or image["id"] <= 0
        ):
            raise WooApplyPlanPayloadError("woo_apply_plan_payload_images_invalid")
        image_ids.append(image["id"])
    if len(image_ids) != len(set(image_ids)):
        raise WooApplyPlanPayloadError(
            "woo_apply_plan_payload_images_invalid"
        )
    frozen = copy.deepcopy(dict(payload))
    sanitized = sanitize_report_data(frozen, Redactor())
    if sanitized != frozen:
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_surface_unsafe")
    try:
        return json.loads(json.dumps(frozen, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError, RecursionError):
        raise WooApplyPlanPayloadError("woo_apply_plan_payload_invalid") from None


def semantic_plan_body(plan: Mapping[str, object]) -> dict[str, object]:
    """Return the one fixed semantic body used to calculate PLAN_HASH."""

    if any(field not in plan for field in _SEMANTIC_FIELDS):
        raise WooApplyPlanInputError("woo_apply_plan_semantic_body_invalid")
    return {field: copy.deepcopy(plan[field]) for field in _SEMANTIC_FIELDS}


def canonical_plan_bytes(semantic_body: Mapping[str, object]) -> bytes:
    """Canonical UTF-8 JSON: sorted keys, no insignificant whitespace."""

    try:
        return json.dumps(
            semantic_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise WooApplyPlanInputError("woo_apply_plan_canonicalization_failed") from None


def compute_plan_hash(semantic_body: Mapping[str, object]) -> str:
    """Compute the only supported V1 PLAN_HASH."""

    return hashlib.sha256(canonical_plan_bytes(semantic_body)).hexdigest()


def build_woo_apply_plan(
    package: Mapping[str, object],
    snapshot: Mapping[str, object],
    *,
    source_package: Mapping[str, str],
    source_target_snapshot: Mapping[str, str],
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Freeze one safe CREATE plan, or a non-actionable blocked report."""

    try:
        sku = target_snapshot.validate_package_report(package)
    except target_snapshot.WooTargetSnapshotInputError:
        raise WooApplyPlanInputError("woo_apply_plan_package_contract_invalid") from None
    package_fingerprint = _safe_fingerprint(source_package)
    snapshot_fingerprint = _safe_fingerprint(source_target_snapshot)
    if package_fingerprint is None or snapshot_fingerprint is None:
        raise WooApplyPlanInputError("woo_apply_plan_source_fingerprint_invalid")
    target, match_count, create_eligible = _validate_snapshot_common(
        snapshot,
        sku=sku,
        package_source=package_fingerprint,
    )
    blocker = _snapshot_disposition(
        snapshot,
        match_count=match_count,
        create_eligible=create_eligible,
    )
    report: dict[str, object] = {
        "status": "blocked" if blocker else "ok",
        "policy_version": POLICY_VERSION,
        "plan_hash": None,
        "target": target,
        "operation": None,
        "preconditions": {
            "match_count": match_count,
            "create_eligible": create_eligible,
        },
        "source_package": package_fingerprint,
        "source_target_snapshot": snapshot_fingerprint,
        "blocking_issues": [blocker] if blocker else [],
        "write_authorized": False,
        **dict.fromkeys(_ZERO_COUNTERS, 0),
    }
    if blocker is None:
        report["operation"] = {
            "action": ACTION,
            "sku": sku,
            "payload": _frozen_payload(package, sku),
        }
        semantic = semantic_plan_body(report)
        sanitized_semantic = sanitize_report_data(semantic, redactor or Redactor())
        if sanitized_semantic != semantic:
            raise WooApplyPlanPayloadError("woo_apply_plan_semantic_body_unsafe")
        report["plan_hash"] = compute_plan_hash(semantic)
    safe = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(safe, dict):  # pragma: no cover - structural guard
        raise AssertionError("Woo apply plan must remain an object")
    if safe.get("operation") != report.get("operation"):
        raise WooApplyPlanPayloadError("woo_apply_plan_saved_payload_mismatch")
    safe["write_authorized"] = False
    for counter in _ZERO_COUNTERS:
        safe[counter] = 0
    return json.loads(json.dumps(safe, ensure_ascii=False, sort_keys=True))


def _safe_output_path(project_root: Path) -> Path:
    try:
        return package_io._local_path(
            Path(project_root) / "reports" / REPORT_FILENAME,
            require_file=False,
        )
    except package_io.SingleProductStagingPackageInputError:
        raise WooApplyPlanInputError("woo_apply_plan_output_path_invalid") from None


def run_woo_apply_plan(
    package_report_path: Path,
    target_snapshot_path: Path,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read two local authorities and atomically persist only the frozen plan."""

    package, package_source, package_path = _read_package(package_report_path)
    snapshot, snapshot_source, snapshot_path = _read_target_snapshot(
        target_snapshot_path
    )
    output = _safe_output_path(project_root)
    if (
        package_path == snapshot_path
        or package_path == output
        or snapshot_path == output
    ):
        raise WooApplyPlanInputError("woo_apply_plan_path_collision")
    report = build_woo_apply_plan(
        package,
        snapshot,
        source_package=package_source,
        source_target_snapshot=snapshot_source,
        redactor=redactor,
    )
    try:
        SafeJsonReportWriter(output, redactor or Redactor()).write(report)
    except (OSError, TypeError, ValueError):
        raise WooApplyPlanInputError("woo_apply_plan_write_failed") from None
    return report, output
