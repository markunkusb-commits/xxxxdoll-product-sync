"""One-product, staging-only Woo CREATE with pending-before-POST safety."""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import re
import ssl
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_apply_plan as apply_plan
from . import woocommerce_target_snapshot as target_snapshot
from .config import load_woo_category_credential_source
from .report import SafeJsonReportWriter, SafeWriteAuditJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .security import basic_auth_headers
from .woo_category_binding import STAGING_EXPECTED_HOST
from .woocommerce_category_discovery import (
    WooCategoryCredentials,
    load_woo_category_credentials,
    normalize_woo_base_url,
    redactor_for_woo_category_credentials,
)


PENDING_POLICY_VERSION = "xxxxdoll-woo-product-apply-pending-v1"
RECEIPT_POLICY_VERSION = "xxxxdoll-woo-product-apply-receipt-v1"
LOCK_POLICY_VERSION = "xxxxdoll-woo-product-apply-lock-v1"
PENDING_FILENAME = "woo-apply-pending.json"
RECEIPT_FILENAME = "woo-apply-receipt.json"
LOCK_FILENAME = "woo-apply.lock.json"
PRODUCT_ENDPOINT = "/wp-json/wc/v3/products"
APPROVED_BASE_URL = f"https://{STAGING_EXPECTED_HOST}"
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0

EXIT_APPLIED = 0
EXIT_BLOCKED_PRE_WRITE = 1
EXIT_PRE_WRITE_ERROR = 2
EXIT_RECOVERY_REQUIRED = 3

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PLAN_ZERO_COUNTERS = apply_plan._ZERO_COUNTERS
_PLAN_FIELDS = frozenset(
    {
        "status",
        "policy_version",
        "plan_hash",
        "target",
        "operation",
        "preconditions",
        "source_package",
        "source_target_snapshot",
        "blocking_issues",
        "write_authorized",
        *_PLAN_ZERO_COUNTERS,
    }
)
_TARGET = {
    "environment": "staging",
    "source_host": STAGING_EXPECTED_HOST,
    "api_version": target_snapshot.API_VERSION,
    "resource": target_snapshot.API_RESOURCE,
}


class WooProductApplyError(ValueError):
    """Base class for fixed-code apply failures."""


class WooProductApplyPreWriteError(WooProductApplyError):
    """Safe-to-rerun failure that happened before a POST attempt marker."""


class WooProductCreateTransportError(WooProductApplyError):
    """Safe summary for any one-shot CREATE transport failure."""


class WooProductCreateTransport(Protocol):
    @property
    def network_requests_performed(self) -> int: ...

    @property
    def write_requests_performed(self) -> int: ...

    def create_product(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...


CredentialLoader = Callable[[], tuple[WooCategoryCredentials, Redactor]]
GetTransportFactory = Callable[
    [str, WooCategoryCredentials], target_snapshot.WooProductTargetTransport
]
CreateTransportFactory = Callable[
    [str, WooCategoryCredentials], WooProductCreateTransport
]


def validate_apply_base_url(base_url: str) -> str:
    """Require the exact approved HTTPS origin; no port or base path is allowed."""

    try:
        normalized = normalize_woo_base_url(base_url)
    except Exception:
        raise WooProductApplyPreWriteError("woo_apply_target_invalid") from None
    if normalized != APPROVED_BASE_URL:
        raise WooProductApplyPreWriteError("woo_apply_target_not_exact_staging_root")
    return normalized


def _default_credential_loader() -> tuple[WooCategoryCredentials, Redactor]:
    source = load_woo_category_credential_source()
    credentials = load_woo_category_credentials(source)
    return credentials, redactor_for_woo_category_credentials(credentials)


class StdlibWooProductCreateTransport:
    """One-shot exact-host product CREATE transport with no retry surface."""

    __slots__ = (
        "_connect_timeout",
        "_create_called",
        "_headers",
        "_hostname",
        "_network_requests",
        "_read_timeout",
        "_ssl_context",
        "_write_requests",
    )

    def __init__(
        self,
        base_url: str,
        credentials: WooCategoryCredentials,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        validate_apply_base_url(base_url)
        if not isinstance(credentials, WooCategoryCredentials):
            raise TypeError("credentials must be WooCategoryCredentials")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise WooProductApplyPreWriteError("woo_apply_transport_options_invalid")
        self._hostname = STAGING_EXPECTED_HOST
        self._headers = {
            **basic_auth_headers(
                credentials.consumer_key,
                credentials.consumer_secret,
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._ssl_context = ssl.create_default_context()
        self._network_requests = 0
        self._write_requests = 0
        self._create_called = False

    @property
    def network_requests_performed(self) -> int:
        return self._network_requests

    @property
    def write_requests_performed(self) -> int:
        return self._write_requests

    def create_product(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        if self._create_called:
            raise WooProductCreateTransportError("woo_create_already_attempted")
        self._create_called = True
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise WooProductCreateTransportError("woo_create_payload_invalid") from None
        if not body or len(body) > MAX_REQUEST_BYTES:
            raise WooProductCreateTransportError("woo_create_payload_size_invalid")
        headers = {**self._headers, "Content-Length": str(len(body))}
        connection = http.client.HTTPSConnection(
            self._hostname,
            timeout=self._connect_timeout,
            context=self._ssl_context,
        )
        self._network_requests += 1
        self._write_requests += 1
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self._read_timeout)
            connection.request("POST", PRODUCT_ENDPOINT, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise WooProductCreateTransportError("woo_create_response_too_large")
            if not 200 <= response.status < 300:
                raise WooProductCreateTransportError("woo_create_http_error")
            try:
                value = json.loads(response_body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                raise WooProductCreateTransportError(
                    "woo_create_response_json_invalid"
                ) from None
            if not isinstance(value, Mapping):
                raise WooProductCreateTransportError(
                    "woo_create_response_contract_invalid"
                )
            return dict(value)
        except WooProductCreateTransportError:
            raise
        except Exception as error:
            raise WooProductCreateTransportError(
                f"woo_create_transport_{type(error).__name__}"
            ) from None
        finally:
            connection.close()


def _read_plan(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    try:
        local = target_snapshot._safe_local_json_file(path)
        size = local.stat().st_size
        if size <= 0 or size > MAX_PLAN_BYTES:
            raise WooProductApplyPreWriteError("woo_apply_plan_size_invalid")
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooProductApplyPreWriteError:
        raise
    except target_snapshot.WooTargetSnapshotInputError as error:
        code = (
            "woo_apply_plan_duplicate_json_key"
            if str(error) == "woo_target_snapshot_duplicate_json_key"
            else "woo_apply_local_plan_required"
        )
        raise WooProductApplyPreWriteError(code) from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooProductApplyPreWriteError("woo_apply_plan_json_invalid") from None
    if not isinstance(value, Mapping):
        raise WooProductApplyPreWriteError("woo_apply_plan_root_invalid")
    return (
        value,
        {"basename": local.name, "sha256": hashlib.sha256(raw).hexdigest()},
        local,
    )


def validate_frozen_plan(
    value: Mapping[str, object],
    confirmed_plan_hash: str,
) -> tuple[str, dict[str, object], str]:
    """Recompute the semantic hash and validate manual authorization exactly."""

    stored_hash = value.get("plan_hash")
    if (
        set(value) != _PLAN_FIELDS
        or value.get("status") != "ok"
        or value.get("policy_version") != apply_plan.POLICY_VERSION
        or value.get("blocking_issues") != []
        or value.get("write_authorized") is not False
        or type(stored_hash) is not str
        or _HASH_PATTERN.fullmatch(stored_hash) is None
        or any(
            type(value.get(counter)) is not int or value.get(counter) != 0
            for counter in _PLAN_ZERO_COUNTERS
        )
    ):
        raise WooProductApplyPreWriteError("woo_apply_plan_contract_invalid")
    target = value.get("target")
    if not isinstance(target, Mapping) or dict(target) != _TARGET:
        raise WooProductApplyPreWriteError("woo_apply_plan_target_invalid")
    operation = value.get("operation")
    if not isinstance(operation, Mapping) or set(operation) != {
        "action",
        "sku",
        "payload",
    }:
        raise WooProductApplyPreWriteError("woo_apply_plan_operation_invalid")
    if operation.get("action") != "create":
        raise WooProductApplyPreWriteError("woo_apply_plan_action_invalid")
    sku = operation.get("sku")
    payload = operation.get("payload")
    if not isinstance(sku, str) or not isinstance(payload, Mapping):
        raise WooProductApplyPreWriteError("woo_apply_plan_operation_invalid")
    try:
        frozen_payload = apply_plan._frozen_payload(
            {"future_woo_payload": payload}, sku
        )
    except apply_plan.WooApplyPlanError:
        raise WooProductApplyPreWriteError("woo_apply_plan_payload_invalid") from None
    if frozen_payload != dict(payload):
        raise WooProductApplyPreWriteError("woo_apply_plan_payload_invalid")
    if value.get("preconditions") != {
        "match_count": 0,
        "create_eligible": True,
    }:
        raise WooProductApplyPreWriteError("woo_apply_plan_preconditions_invalid")
    if apply_plan._safe_fingerprint(value.get("source_package")) is None or (
        apply_plan._safe_fingerprint(value.get("source_target_snapshot")) is None
    ):
        raise WooProductApplyPreWriteError("woo_apply_plan_sources_invalid")
    try:
        recomputed = apply_plan.compute_plan_hash(
            apply_plan.semantic_plan_body(value)
        )
    except apply_plan.WooApplyPlanError:
        raise WooProductApplyPreWriteError("woo_apply_plan_hash_invalid") from None
    if stored_hash != recomputed:
        raise WooProductApplyPreWriteError("woo_apply_plan_hash_invalid")
    if confirmed_plan_hash != stored_hash:
        raise WooProductApplyPreWriteError("woo_apply_manual_confirmation_mismatch")
    return sku, frozen_payload, stored_hash


def canonical_payload_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _runtime_paths(project_root: Path) -> tuple[Path, Path, Path]:
    paths: list[Path] = []
    for filename in (PENDING_FILENAME, RECEIPT_FILENAME, LOCK_FILENAME):
        try:
            paths.append(
                package_io._local_path(
                    Path(project_root) / "reports" / filename,
                    require_file=False,
                )
            )
        except package_io.SingleProductStagingPackageInputError:
            raise WooProductApplyPreWriteError(
                "woo_apply_runtime_path_invalid"
            ) from None
    return paths[0], paths[1], paths[2]


def _result(
    status: str,
    exit_code: int,
    code: str,
    *,
    get_transport: target_snapshot.WooProductTargetTransport | None = None,
    create_transport: WooProductCreateTransport | None = None,
) -> dict[str, object]:
    get_requests = (
        get_transport.network_requests_performed if get_transport is not None else 0
    )
    create_requests = (
        create_transport.network_requests_performed
        if create_transport is not None
        else 0
    )
    write_requests = (
        create_transport.write_requests_performed
        if create_transport is not None
        else 0
    )
    return {
        "status": status,
        "exit_code": exit_code,
        "result_code": code,
        "network_requests_performed": get_requests + create_requests,
        "woocommerce_requests_performed": get_requests + create_requests,
        "woocommerce_write_requests_performed": write_requests,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": write_requests,
        "write_requests_performed": write_requests,
    }


class _CapturingGetTransport:
    """Record raw pages while delegating all pagination and exact checks to 03A."""

    def __init__(self, transport: target_snapshot.WooProductTargetTransport) -> None:
        self._transport = transport
        self.raw_items: list[object] = []

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    @property
    def network_requests_performed(self) -> int:
        return self._transport.network_requests_performed

    @property
    def write_requests_performed(self) -> int:
        return self._transport.write_requests_performed

    def get_products_by_sku(
        self,
        sku: str,
        *,
        page: int,
        per_page: int = target_snapshot.DEFAULT_PER_PAGE,
    ) -> target_snapshot.WooProductTargetPage:
        result = self._transport.get_products_by_sku(
            sku,
            page=page,
            per_page=per_page,
        )
        if isinstance(result.items, list):
            self.raw_items.extend(result.items)
        return result


def _collect_exact(
    transport: target_snapshot.WooProductTargetTransport,
    sku: str,
) -> tuple[list[dict[str, object]], list[object]]:
    capturing = _CapturingGetTransport(transport)
    projected = target_snapshot.WooTargetSnapshotter(capturing).collect_exact_products(
        sku
    )
    return projected, capturing.raw_items


def _payload_projection_matches(
    payload: Mapping[str, object],
    remote: object,
) -> bool:
    def ordered_positive_ids(items: object) -> tuple[int, ...] | None:
        if not isinstance(items, list):
            return None
        result: list[int] = []
        for item in items:
            if not isinstance(item, Mapping):
                return None
            item_id = item.get("id")
            if type(item_id) is not int or item_id <= 0:
                return None
            result.append(item_id)
        return tuple(result)

    def exact_typed_value(left: object, right: object) -> bool:
        return type(left) is type(right) and left == right

    if not isinstance(remote, Mapping):
        return False
    product_id = remote.get("id")
    if type(product_id) is not int or product_id <= 0:
        return False
    for field in ("name", "sku", "type", "status", "regular_price"):
        if not exact_typed_value(remote.get(field), payload.get(field)):
            return False
    for field in ("description", "short_description"):
        if field in payload and not exact_typed_value(remote.get(field), payload[field]):
            return False
    expected_categories = payload.get("categories")
    remote_categories = remote.get("categories")
    expected_category_ids = ordered_positive_ids(expected_categories)
    remote_category_ids = ordered_positive_ids(remote_categories)
    if (
        expected_category_ids is None
        or remote_category_ids is None
        or remote_category_ids != expected_category_ids
    ):
        return False
    expected_attributes = payload.get("attributes")
    remote_attributes = remote.get("attributes")
    if not isinstance(expected_attributes, list) or not isinstance(remote_attributes, list):
        return False
    if len(expected_attributes) != len(remote_attributes):
        return False
    attribute_fields = ("name", "position", "visible", "variation", "options")
    for expected, actual in zip(expected_attributes, remote_attributes, strict=True):
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            return False
        if any(
            not exact_typed_value(actual.get(field), expected.get(field))
            for field in attribute_fields
        ):
            return False
    expected_images = payload.get("images")
    remote_images = remote.get("images")
    expected_image_ids = ordered_positive_ids(expected_images)
    remote_image_ids = ordered_positive_ids(remote_images)
    if (
        expected_image_ids is None
        or remote_image_ids is None
        or remote_image_ids != expected_image_ids
    ):
        return False
    return True


def _journal_base(
    plan_hash: str,
    plan_source: Mapping[str, str],
    sku: str,
    payload_hash: str,
) -> dict[str, object]:
    return {
        "plan_hash": plan_hash,
        "source_plan": dict(plan_source),
        "target": dict(_TARGET),
        "operation": {
            "action": "create",
            "sku": sku,
            "payload_sha256": payload_hash,
        },
        "manual_confirmation_verified": True,
    }


def _write_pending(path: Path, value: Mapping[str, object], redactor: Redactor) -> None:
    SafeJsonReportWriter(path, redactor).write(value)


def _write_receipt(path: Path, value: Mapping[str, object], redactor: Redactor) -> None:
    SafeWriteAuditJsonReportWriter(path, redactor).write(value)


def _acquire_lock(path: Path, value: Mapping[str, object]) -> None:
    safe = sanitize_report_data(value, Redactor())
    if not isinstance(safe, dict):
        raise WooProductApplyPreWriteError("woo_apply_lock_invalid")
    data = (json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, flags, 0o600)
        os.write(descriptor, data)
        os.fsync(descriptor)
    except FileExistsError:
        raise WooProductApplyPreWriteError("woo_apply_lock_exists") from None
    except OSError:
        raise WooProductApplyPreWriteError("woo_apply_lock_create_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _remove_owned(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _release_lock_or_recovery(
    lock_path: Path,
    result: dict[str, object],
) -> dict[str, object]:
    if _remove_owned(lock_path):
        return result
    result["status"] = "recovery_required"
    result["exit_code"] = EXIT_RECOVERY_REQUIRED
    result["result_code"] = "woo_apply_lock_cleanup_failed"
    return result


def run_woo_product_apply(
    plan_report_path: Path,
    confirmed_plan_hash: str,
    base_url: str,
    *,
    project_root: Path,
    credential_loader: CredentialLoader = _default_credential_loader,
    get_transport_factory: GetTransportFactory = target_snapshot.StdlibWooProductTargetTransport,
    create_transport_factory: CreateTransportFactory = StdlibWooProductCreateTransport,
) -> dict[str, object]:
    """Perform at most one CREATE attempt after all local authorization gates."""

    frozen_plan, plan_source, _ = _read_plan(plan_report_path)
    sku, payload, plan_hash = validate_frozen_plan(
        frozen_plan,
        confirmed_plan_hash,
    )
    normalized_base_url = validate_apply_base_url(base_url)
    pending_path, receipt_path, lock_path = _runtime_paths(project_root)
    if pending_path.exists() or lock_path.exists():
        return _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_existing_runtime_state",
        )
    if receipt_path.exists():
        return _result(
            "blocked_pre_write",
            EXIT_BLOCKED_PRE_WRITE,
            "woo_apply_receipt_already_exists",
        )

    try:
        credentials, redactor = credential_loader()
    except Exception:
        raise WooProductApplyPreWriteError("woo_apply_credentials_invalid") from None
    lock_value = {
        "status": "locked",
        "policy_version": LOCK_POLICY_VERSION,
        "plan_hash": plan_hash,
        "source_plan": plan_source,
        "sku": sku,
    }
    try:
        _acquire_lock(lock_path, lock_value)
    except WooProductApplyPreWriteError as error:
        if str(error) == "woo_apply_lock_exists":
            return _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_existing_lock",
            )
        raise
    if pending_path.exists() or receipt_path.exists():
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_runtime_state_race",
        )
        return _release_lock_or_recovery(lock_path, result)

    try:
        get_transport = get_transport_factory(normalized_base_url, credentials)
        create_transport = create_transport_factory(normalized_base_url, credentials)
    except Exception:
        cleanup = _remove_owned(lock_path)
        if not cleanup:
            return _result(
                "recovery_required",
                EXIT_RECOVERY_REQUIRED,
                "woo_apply_lock_cleanup_failed",
            )
        raise WooProductApplyPreWriteError("woo_apply_transport_create_failed") from None

    try:
        preflight, _ = _collect_exact(get_transport, sku)
    except Exception:
        result = _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_preflight_get_failed",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        if not _remove_owned(lock_path):
            result["status"] = "recovery_required"
            result["exit_code"] = EXIT_RECOVERY_REQUIRED
            result["result_code"] = "woo_apply_lock_cleanup_failed"
        return result
    if preflight:
        code = (
            "woo_apply_target_sku_already_exists"
            if len(preflight) == 1
            else "woo_apply_target_sku_ambiguous"
        )
        result = _result(
            "blocked_pre_write",
            EXIT_BLOCKED_PRE_WRITE,
            code,
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)

    payload_hash = canonical_payload_hash(payload)
    journal = _journal_base(plan_hash, plan_source, sku, payload_hash)
    prepared = {
        **journal,
        "status": "pending",
        "policy_version": PENDING_POLICY_VERSION,
        "post_state": "prepared",
        "post_attempts_started": 0,
    }
    try:
        _write_pending(pending_path, prepared, redactor)
    except Exception:
        result = _result(
            "pre_write_error",
            EXIT_PRE_WRITE_ERROR,
            "woo_apply_pending_prepare_failed",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        if pending_path.exists() or not _remove_owned(lock_path):
            result["status"] = "recovery_required"
            result["exit_code"] = EXIT_RECOVERY_REQUIRED
            result["result_code"] = "woo_apply_pending_state_uncertain"
        return result

    attempting = {
        **prepared,
        "post_state": "attempting",
        "post_attempts_started": 1,
    }
    try:
        _write_pending(pending_path, attempting, redactor)
    except Exception:
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_attempt_marker_failed",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)

    post_transport_completed_without_error = True
    try:
        create_transport.create_product(copy.deepcopy(payload))
    except Exception:
        # A response failure cannot prove that Woo did not commit the CREATE.
        # Never retry here; the fresh exact-SKU GET below is authoritative.
        post_transport_completed_without_error = False

    try:
        readback, raw_readback = _collect_exact(get_transport, sku)
    except Exception:
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_readback_failed",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)
    if create_transport.write_requests_performed != 1:
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_post_count_invalid",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)
    if (
        len(readback) != 1
        or len(raw_readback) != 1
        or not _payload_projection_matches(payload, raw_readback[0])
    ):
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_readback_mismatch",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)

    product_id = readback[0]["id"]
    receipt = {
        **journal,
        "status": "applied",
        "policy_version": RECEIPT_POLICY_VERSION,
        "product": {
            "id": product_id,
            "sku": sku,
            "name": payload["name"],
            "type": payload["type"],
            "status": payload["status"],
        },
        "readback_verified": True,
        "post_transport_completed_without_error": (
            post_transport_completed_without_error
        ),
        "network_requests_performed": (
            get_transport.network_requests_performed
            + create_transport.network_requests_performed
        ),
        "woocommerce_requests_performed": (
            get_transport.network_requests_performed
            + create_transport.network_requests_performed
        ),
        "woocommerce_write_requests_performed": 1,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 1,
        "write_requests_performed": 1,
    }
    try:
        _write_receipt(receipt_path, receipt, redactor)
    except Exception:
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_receipt_write_failed",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)
    try:
        pending_path.unlink()
    except OSError:
        result = _result(
            "recovery_required",
            EXIT_RECOVERY_REQUIRED,
            "woo_apply_pending_cleanup_failed",
            get_transport=get_transport,
            create_transport=create_transport,
        )
        return _release_lock_or_recovery(lock_path, result)
    result = _result(
        "applied",
        EXIT_APPLIED,
        "woo_apply_applied",
        get_transport=get_transport,
        create_transport=create_transport,
    )
    result["receipt"] = receipt
    return _release_lock_or_recovery(lock_path, result)
