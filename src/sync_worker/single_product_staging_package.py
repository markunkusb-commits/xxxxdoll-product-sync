"""Pure-local, write-disabled single-product staging package builder."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence

from . import image_selection_policy
from . import woo_category_binding
from . import woocommerce_product_mapper as woo_mapper
from . import wordpress_media_upload_execution as media_execution
from . import wordpress_media_upload_transport as media_transport
from .report import sanitize_report_data
from .sanitization import Redactor
from .sku_dry_run import is_safe_sku
from .sku_policy import MAX_SKU_LENGTH, SKU_POLICY_VERSION


POLICY_VERSION = "xxxxdoll-single-product-staging-package-v1"
REPORT_FILENAME = "single-product-staging-package.json"
SOURCE_ROLES = (
    "woocommerce_payload",
    "sku",
    "image_selection",
    "wordpress_media",
    "woo_category_discovery",
)
_PACKAGE_COUNTERS = (
    "network_requests_performed",
    "woocommerce_requests_performed",
    "woocommerce_write_requests_performed",
    "wordpress_requests_performed",
    "external_write_requests_performed",
    "write_requests_performed",
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TARGET_SKU = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")


class SingleProductStagingPackageError(ValueError):
    """Fixed-code structural error for local, untrusted report data."""


def _object(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SingleProductStagingPackageError(code)
    return value


def _array(value: object, code: str) -> list[object]:
    if not isinstance(value, list):
        raise SingleProductStagingPackageError(code)
    return value


def _codes(value: object, code: str) -> tuple[str, ...]:
    raw = _array(value, code)
    if any(type(item) is not str or _SAFE_CODE.fullmatch(item) is None for item in raw):
        raise SingleProductStagingPackageError(code)
    return tuple(dict.fromkeys(raw))


def _append(blockers: list[str], code: str) -> None:
    if code not in blockers:
        blockers.append(code)


def _is_zero_int(value: object) -> bool:
    return type(value) is int and value == 0


def _validate_source_reports(reports: Sequence[Mapping[str, object]]) -> None:
    if len(reports) != len(SOURCE_ROLES):
        raise SingleProductStagingPackageError("single_product_source_count_invalid")
    for report in reports:
        if report.get("status") != "ok":
            raise SingleProductStagingPackageError(
                "single_product_source_report_not_ok"
            )


def _source_fingerprint_projection(
    fingerprints: Mapping[str, object],
) -> dict[str, dict[str, str]]:
    if set(fingerprints) != set(SOURCE_ROLES):
        raise SingleProductStagingPackageError(
            "single_product_source_fingerprints_invalid"
        )
    projected: dict[str, dict[str, str]] = {}
    for role in SOURCE_ROLES:
        entry = _object(
            fingerprints.get(role), "single_product_source_fingerprints_invalid"
        )
        basename, digest = entry.get("basename"), entry.get("sha256")
        if (
            type(basename) is not str
            or not basename
            or "/" in basename
            or "\\" in basename
            or not basename.casefold().endswith(".json")
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
        ):
            raise SingleProductStagingPackageError(
                "single_product_source_fingerprints_invalid"
            )
        projected[role] = {"basename": basename, "sha256": digest}
    return projected


def _find_exact(
    values: object,
    predicate,
    *,
    structure_code: str,
) -> list[Mapping[str, object]]:
    records = _array(values, structure_code)
    matches: list[Mapping[str, object]] = []
    for value in records:
        record = _object(value, structure_code)
        if predicate(record):
            matches.append(record)
    return matches


def _validate_sku(
    report: Mapping[str, object], target_sku: str, blockers: list[str]
) -> bool:
    matches = _find_exact(
        report.get("results"),
        lambda item: item.get("sku") == target_sku,
        structure_code="single_product_sku_report_invalid",
    )
    if not matches:
        _append(blockers, "single_product_sku_not_found")
        return False
    if len(matches) != 1:
        _append(blockers, "single_product_sku_ambiguous")
        return False
    item = matches[0]
    issues = _codes(
        item.get("blocking_issues"), "single_product_sku_report_invalid"
    )
    valid = (
        report.get("policy_version") == SKU_POLICY_VERSION
        and item.get("policy_version") == SKU_POLICY_VERSION
        and item.get("status") == "ok"
        and is_safe_sku(item.get("sku"))
        and not issues
    )
    if not valid:
        _append(blockers, "single_product_sku_not_eligible")
    return valid


def _payload_is_safe(payload: Mapping[str, object]) -> bool:
    if set(payload).difference(woo_mapper.WOO_CORE_PAYLOAD_ALLOWLIST):
        return False
    regular_price = payload.get("regular_price")
    if (
        type(regular_price) is not str
        or woo_mapper._USD_PRICE_PATTERN.fullmatch(regular_price) is None
    ):
        return False
    candidate = {"payload": payload, "storefront_options": {}, "public_content": {}}
    from .woocommerce_payload_dry_run import scan_public_surfaces

    return not scan_public_surfaces(candidate)


def _validate_payload(
    report: Mapping[str, object], target_sku: str, blockers: list[str]
) -> tuple[bool, Mapping[str, object] | None, Mapping[str, object] | None, int | None]:
    matches = _find_exact(
        report.get("candidates"),
        lambda item: isinstance(item.get("payload"), Mapping)
        and item["payload"].get("sku") == target_sku,  # type: ignore[union-attr]
        structure_code="single_product_payload_report_invalid",
    )
    if not matches:
        _append(blockers, "single_product_payload_not_found")
        return False, None, None, None
    if len(matches) != 1:
        _append(blockers, "single_product_payload_ambiguous")
        return False, None, None, None
    candidate = matches[0]
    payload = _object(
        candidate.get("payload"), "single_product_payload_report_invalid"
    )
    candidate_issues = _codes(
        candidate.get("blocking_issues"),
        "single_product_payload_report_invalid",
    )
    warnings = _codes(
        candidate.get("warnings"), "single_product_payload_report_invalid"
    )
    canonical_issues = woo_mapper.validate_woocommerce_product_payload(candidate)
    categories = payload.get("categories")
    category_id: int | None = None
    categories_valid = False
    if isinstance(categories, list) and len(categories) == 1:
        category = categories[0]
        if (
            isinstance(category, Mapping)
            and set(category) == {"id"}
            and type(category.get("id")) is int
            and category["id"] > 0
        ):
            category_id = category["id"]
            categories_valid = True
    name = payload.get("name")
    valid = (
        not candidate_issues
        and not canonical_issues
        and candidate.get("ready_for_write") is False
        and payload.get("sku") == target_sku
        and payload.get("type") == "simple"
        and payload.get("status") == "draft"
        and type(name) is str
        and bool(name.strip())
        and categories_valid
        and _payload_is_safe(payload)
    )
    if not valid:
        _append(blockers, "single_product_payload_not_eligible")
    return valid, candidate, payload, category_id


def _validate_category(
    report: Mapping[str, object],
    candidate: Mapping[str, object] | None,
    category_id: int | None,
    blockers: list[str],
) -> bool:
    if not _is_zero_int(report.get("write_requests_performed")):
        _append(blockers, "single_product_category_not_eligible")
        return False
    source_host = report.get("source_host")
    discovery_hostname = (
        woo_category_binding._normalize_hostname(source_host)
        if isinstance(source_host, str)
        else ""
    )
    if (
        discovery_hostname != source_host
        or discovery_hostname != woo_category_binding.STAGING_EXPECTED_HOST
    ):
        _append(
            blockers, "single_product_category_discovery_host_mismatch"
        )
        return False
    if category_id is None:
        return False
    matches = _find_exact(
        report.get("categories"),
        lambda item: item.get("id") == category_id,
        structure_code="single_product_category_report_invalid",
    )
    if not matches:
        _append(blockers, "single_product_category_not_found")
        return False
    if len(matches) != 1:
        _append(blockers, "single_product_category_ambiguous")
        return False
    name = matches[0].get("name")
    if type(name) is not str or not name.strip():
        _append(blockers, "single_product_category_not_eligible")
        return False
    profile = woo_category_binding.staging_category_binding_profile()
    root_audit = candidate.get("audit") if candidate is not None else None
    category_audit = (
        root_audit.get("category") if isinstance(root_audit, Mapping) else None
    )
    approved = (
        profile.binding_for(category_audit.get("internal_category_key"))
        if isinstance(category_audit, Mapping)
        and isinstance(category_audit.get("internal_category_key"), str)
        else None
    )
    binding_valid = (
        profile.profile_version
        == woo_category_binding.STAGING_BINDING_PROFILE_VERSION
        and profile.environment == woo_category_binding.STAGING_ENVIRONMENT
        and profile.expected_host == woo_category_binding.STAGING_EXPECTED_HOST
        and approved is not None
        and approved.woo_category_id == category_id
        and approved.expected_name == name
        and isinstance(category_audit, Mapping)
        and category_audit.get("binding_profile_version")
        == profile.profile_version
        and category_audit.get("environment") == profile.environment
        and category_audit.get("target_host") == profile.expected_host
        and category_audit.get("woo_category_id") == approved.woo_category_id
        and category_audit.get("verified_name") == approved.expected_name
        and category_audit.get("binding_status") == "bound_verified"
        and category_audit.get("host_verified") is True
        and category_audit.get("discovery_verified") is True
    )
    if not binding_valid:
        _append(blockers, "single_product_category_binding_changed")
        return False
    return True


def _validate_selection(
    report: Mapping[str, object], target_sku: str, blockers: list[str]
) -> tuple[bool, list[Mapping[str, object]]]:
    matches = _find_exact(
        report.get("results"),
        lambda item: item.get("sku") == target_sku,
        structure_code="single_product_selection_report_invalid",
    )
    if not matches:
        _append(blockers, "single_product_selection_not_found")
        return False, []
    if len(matches) != 1:
        _append(blockers, "single_product_selection_ambiguous")
        return False, []
    batch = matches[0]
    batch_issues = _codes(
        batch.get("blocking_issues"), "single_product_selection_report_invalid"
    )
    items = _array(batch.get("items"), "single_product_selection_report_invalid")
    selected = [
        _object(item, "single_product_selection_report_invalid")
        for item in items
        if isinstance(item, Mapping) and item.get("selected") is True
    ]
    valid = True
    if report.get("policy_version") != image_selection_policy.POLICY_VERSION:
        valid = False
    if batch_issues:
        valid = False
    if not selected or len(selected) > image_selection_policy.MAX_IMAGES_PER_SKU:
        valid = False
    if batch.get("selected_count") != len(selected):
        valid = False
    positions: list[int] = []
    roles: list[str] = []
    for item in selected:
        position, role = item.get("selection_position"), item.get("image_role")
        item_issues = _codes(
            item.get("blocking_issues"),
            "single_product_selection_report_invalid",
        )
        if (
            item.get("sku") != target_sku
            or type(position) is not int
            or position < 0
            or role not in {"primary", "gallery"}
            or item_issues
        ):
            valid = False
        if type(position) is int:
            positions.append(position)
        if type(role) is str:
            roles.append(role)
    if len(set(positions)) != len(selected):
        _append(blockers, "single_product_selection_position_ambiguous")
        valid = False
    if roles.count("primary") != 1:
        _append(blockers, "single_product_primary_media_invalid")
        valid = False
    if (
        batch.get("primary_count") != roles.count("primary")
        or batch.get("gallery_count") != roles.count("gallery")
    ):
        valid = False
    if not valid:
        _append(blockers, "single_product_selection_not_eligible")
    return valid, selected


def _validate_media_root(report: Mapping[str, object], blockers: list[str]) -> bool:
    required_zero = (
        "woocommerce_requests_performed",
        "woocommerce_write_requests_performed",
        "delete_requests_performed",
        "rollback_requests_performed",
    )
    valid = (
        report.get("policy_version") == media_execution.POLICY_VERSION
        and all(_is_zero_int(report.get(field)) for field in required_zero)
        and report.get("webp_cleanup_completed") is True
        and report.get("source_cleanup_completed") is True
        and _is_zero_int(report.get("webp_files_remaining"))
        and _is_zero_int(report.get("source_files_remaining"))
    )
    if not valid:
        _append(blockers, "single_product_media_report_not_eligible")
    return valid


def _validate_media(
    report: Mapping[str, object],
    target_sku: str,
    selected: Sequence[Mapping[str, object]],
    blockers: list[str],
) -> tuple[bool, list[dict[str, object]]]:
    root_valid = _validate_media_root(report, blockers)
    all_results = _array(
        report.get("results"), "single_product_media_report_invalid"
    )
    sku_results = [
        _object(item, "single_product_media_report_invalid")
        for item in all_results
        if isinstance(item, Mapping) and item.get("sku") == target_sku
    ]
    if len(sku_results) != len(selected):
        _append(blockers, "single_product_media_count_mismatch")
    plan: list[dict[str, object]] = []
    valid = root_valid and len(sku_results) == len(selected)
    for selection in selected:
        position, role = selection.get("selection_position"), selection.get("image_role")
        same_position = [
            item for item in sku_results if item.get("selection_position") == position
        ]
        if not same_position:
            _append(blockers, "single_product_media_join_missing")
            valid = False
            continue
        if len(same_position) != 1:
            _append(blockers, "single_product_media_join_ambiguous")
            valid = False
            continue
        media = same_position[0]
        if media.get("image_role") != role:
            _append(blockers, "single_product_media_role_mismatch")
            valid = False
            continue
        media_issues = _codes(
            media.get("blocking_issues"), "single_product_media_report_invalid"
        )
        media_id = media.get("wordpress_media_id")
        if (
            type(media_id) is not int
            or media_id <= 0
            or media.get("upload_status") not in media_transport._SAFE_UPLOAD_STATUSES
            or media_issues
        ):
            _append(blockers, "single_product_media_not_eligible")
            valid = False
            continue
        plan.append(
            {
                "selection_position": position,
                "image_role": role,
                "wordpress_media_id": media_id,
            }
        )
    media_ids = [item["wordpress_media_id"] for item in plan]
    if len(set(media_ids)) != len(media_ids):
        _append(blockers, "single_product_media_reference_duplicate")
        valid = False
    if sum(item["image_role"] == "primary" for item in plan) != 1:
        _append(blockers, "single_product_primary_media_invalid")
        valid = False
    if len(plan) != len(selected):
        valid = False
    if not valid:
        plan = []
    else:
        plan.sort(
            key=lambda item: (
                item["image_role"] != "primary",
                item["selection_position"],
            )
        )
    return valid, plan


def _safe_candidate_warnings(candidate: Mapping[str, object] | None) -> tuple[str, ...]:
    if candidate is None:
        return ()
    return _codes(
        candidate.get("warnings"), "single_product_payload_report_invalid"
    )


def build_single_product_staging_package(
    woo_payload_report: Mapping[str, object],
    sku_report: Mapping[str, object],
    selection_report: Mapping[str, object],
    media_report: Mapping[str, object],
    category_discovery_report: Mapping[str, object],
    *,
    target_sku: str,
    source_fingerprints: Mapping[str, object],
    redactor: Redactor | None = None,
) -> dict[str, object]:
    """Exact-join five safe local reports into a write-disabled audit package."""

    reports = (
        _object(woo_payload_report, "single_product_payload_report_invalid"),
        _object(sku_report, "single_product_sku_report_invalid"),
        _object(selection_report, "single_product_selection_report_invalid"),
        _object(media_report, "single_product_media_report_invalid"),
        _object(
            category_discovery_report, "single_product_category_report_invalid"
        ),
    )
    _validate_source_reports(reports)
    if (
        type(target_sku) is not str
        or len(target_sku) > MAX_SKU_LENGTH
        or _SAFE_TARGET_SKU.fullmatch(target_sku) is None
    ):
        raise SingleProductStagingPackageError("single_product_target_sku_invalid")
    fingerprints = _source_fingerprint_projection(source_fingerprints)
    blockers: list[str] = []
    sku_verified = _validate_sku(reports[1], target_sku, blockers)
    payload_verified, candidate, payload, category_id = _validate_payload(
        reports[0], target_sku, blockers
    )
    category_verified = _validate_category(
        reports[4], candidate, category_id, blockers
    )
    selection_verified, selected = _validate_selection(
        reports[2], target_sku, blockers
    )
    media_verified, image_plan = _validate_media(
        reports[3], target_sku, selected, blockers
    )
    warnings = _safe_candidate_warnings(candidate)
    all_verified = all(
        (
            sku_verified,
            payload_verified,
            category_verified,
            selection_verified,
            media_verified,
        )
    )
    resolved_warnings = (
        ["images_not_mapped"]
        if all_verified and "images_not_mapped" in warnings
        else []
    )
    unresolved_warnings = [
        warning for warning in warnings if warning not in resolved_warnings
    ]
    product: dict[str, object] = {}
    future_payload: dict[str, object] = {}
    if payload_verified and payload is not None:
        product = {
            "name": payload["name"],
            "sku": payload["sku"],
            "type": payload["type"],
            "status": payload["status"],
            "regular_price": payload["regular_price"],
            "category_id": category_id,
        }
    if all_verified and payload is not None:
        future_payload = copy.deepcopy(dict(payload))
        future_payload["images"] = [
            {"id": item["wordpress_media_id"]} for item in image_plan
        ]
    report: dict[str, object] = {
        "status": "ok" if all_verified and not blockers else "blocked",
        "policy_version": POLICY_VERSION,
        "target_sku": target_sku,
        "product": product,
        "image_plan": image_plan,
        "future_woo_payload": future_payload,
        "source_validation": {
            "sku_verified": sku_verified,
            "payload_verified": payload_verified,
            "category_verified": category_verified,
            "selection_verified": selection_verified,
            "media_verified": media_verified,
        },
        "source_fingerprints": fingerprints,
        "resolved_warnings": resolved_warnings,
        "unresolved_warnings": unresolved_warnings,
        "blocking_issues": blockers,
        "write_authorized": False,
        **dict.fromkeys(_PACKAGE_COUNTERS, 0),
    }
    safe = sanitize_report_data(report, redactor or Redactor())
    if not isinstance(safe, dict):  # pragma: no cover - structural guard
        raise AssertionError("single product package must remain an object")
    safe["write_authorized"] = False
    for counter in _PACKAGE_COUNTERS:
        safe[counter] = 0
    return json.loads(json.dumps(safe, ensure_ascii=False, sort_keys=True))
