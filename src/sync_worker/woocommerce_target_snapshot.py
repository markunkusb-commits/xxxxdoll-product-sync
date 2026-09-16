"""Read-only exact-SKU WooCommerce target snapshot.

This module is deliberately narrower than a generic Woo client: it can only
GET the products resource with an exact SKU filter.  It never exposes a write
method and never carries a full URL or credentials into its safe report.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor
from .security import basic_auth_headers
from .single_product_staging_package import POLICY_VERSION as PACKAGE_POLICY_VERSION
from . import single_product_staging_package_dry_run as package_io
from .sku_dry_run import is_safe_sku
from .sku_policy import MAX_SKU_LENGTH
from .woo_category_binding import STAGING_EXPECTED_HOST
from .woocommerce_category_discovery import (
    WooCategoryCredentials,
    normalize_woo_base_url,
)


POLICY_VERSION = "xxxxdoll-woo-target-snapshot-v1"
REPORT_FILENAME = "woo-target-snapshot.json"
API_VERSION = "wc/v3"
API_RESOURCE = "products"
PRODUCT_ENDPOINT = "/wp-json/wc/v3/products"
DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_RETRIES = 2
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 2_000_000

_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_SAFE_OPTIONAL_TEXT = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PACKAGE_ZERO_COUNTERS = (
    "woocommerce_write_requests_performed",
    "wordpress_requests_performed",
    "external_write_requests_performed",
    "write_requests_performed",
)
_SOURCE_VALIDATION_FIELDS = (
    "sku_verified",
    "payload_verified",
    "category_verified",
    "selection_verified",
    "media_verified",
)
_OUTPUT_ZERO_COUNTERS = (
    "woocommerce_write_requests_performed",
    "wordpress_requests_performed",
    "external_write_requests_performed",
    "write_requests_performed",
)


class WooTargetSnapshotError(RuntimeError):
    """Base class for fixed-code snapshot failures."""


class WooTargetSnapshotInputError(WooTargetSnapshotError):
    """Unsafe path, JSON, or POC-02 Package contract."""


class WooTargetSnapshotConfigurationError(WooTargetSnapshotError):
    """Unsafe target host or transport configuration."""


class WooTargetSnapshotTransportError(WooTargetSnapshotError):
    """Non-retryable safe GET transport failure."""


class WooTargetSnapshotRetryableError(WooTargetSnapshotTransportError):
    """Retryable GET-only timeout or transient response."""


class WooTargetSnapshotDataError(WooTargetSnapshotError):
    """Woo response did not meet the exact-SKU contract."""


@dataclass(frozen=True, slots=True)
class WooProductTargetPage:
    items: object
    total: int | None
    total_pages: int | None


class WooProductTargetTransport(Protocol):
    @property
    def base_url(self) -> str: ...

    @property
    def network_requests_performed(self) -> int: ...

    @property
    def write_requests_performed(self) -> int: ...

    def get_products_by_sku(
        self,
        sku: str,
        *,
        page: int,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> WooProductTargetPage: ...


def validate_staging_target_base_url(base_url: str) -> str:
    """Reuse Woo URL validation, then require the one approved staging host."""

    try:
        normalized = normalize_woo_base_url(base_url)
        parsed = urlsplit(normalized)
    except Exception:
        raise WooTargetSnapshotConfigurationError(
            "woo_target_snapshot_target_invalid"
        ) from None
    if parsed.scheme != "https" or parsed.hostname != STAGING_EXPECTED_HOST:
        raise WooTargetSnapshotConfigurationError(
            "woo_target_snapshot_target_not_approved_staging"
        )
    return normalized


def _header_integer(value: str | None, name: str) -> int:
    if value is None:
        raise WooTargetSnapshotTransportError(
            f"woo_target_snapshot_{name}_header_missing"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise WooTargetSnapshotTransportError(
            f"woo_target_snapshot_{name}_header_invalid"
        ) from None
    if parsed < 0:
        raise WooTargetSnapshotTransportError(
            f"woo_target_snapshot_{name}_header_invalid"
        )
    return parsed


class StdlibWooProductTargetTransport:
    """Single-purpose Woo products GET transport; no write verbs are exposed."""

    __slots__ = (
        "_base_url",
        "_connect_timeout",
        "_headers",
        "_max_response_bytes",
        "_network_requests",
        "_parsed_base_url",
        "_read_timeout",
    )

    def __init__(
        self,
        base_url: str,
        credentials: WooCategoryCredentials,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(credentials, WooCategoryCredentials):
            raise TypeError("credentials must be WooCategoryCredentials")
        if connect_timeout <= 0 or read_timeout <= 0 or max_response_bytes <= 0:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_transport_options_invalid"
            )
        self._base_url = validate_staging_target_base_url(base_url)
        self._parsed_base_url = urlsplit(self._base_url)
        self._headers = {
            **basic_auth_headers(
                credentials.consumer_key,
                credentials.consumer_secret,
            ),
            "Accept": "application/json",
        }
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._max_response_bytes = max_response_bytes
        self._network_requests = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def network_requests_performed(self) -> int:
        return self._network_requests

    @property
    def write_requests_performed(self) -> int:
        return 0

    def get_products_by_sku(
        self,
        sku: str,
        *,
        page: int,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> WooProductTargetPage:
        if not is_safe_sku(sku) or len(sku) > MAX_SKU_LENGTH:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_sku_invalid"
            )
        if type(page) is not int or page <= 0 or per_page != DEFAULT_PER_PAGE:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_pagination_invalid"
            )
        parsed = self._parsed_base_url
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=self._connect_timeout,
            context=ssl.create_default_context(),
        )
        target = (
            parsed.path.rstrip("/")
            + PRODUCT_ENDPOINT
            + "?"
            + urlencode({"sku": sku, "page": page, "per_page": per_page})
        )
        self._network_requests += 1
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self._read_timeout)
            connection.request("GET", target, headers=dict(self._headers))
            response = connection.getresponse()
            body = response.read(self._max_response_bytes + 1)
            if len(body) > self._max_response_bytes:
                raise WooTargetSnapshotTransportError(
                    "woo_target_snapshot_response_too_large"
                )
            if response.status in _RETRYABLE_HTTP_STATUSES:
                raise WooTargetSnapshotRetryableError(
                    "woo_target_snapshot_transient_get_failure"
                )
            if not 200 <= response.status < 300:
                raise WooTargetSnapshotTransportError(
                    "woo_target_snapshot_get_failed"
                )
            try:
                items = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise WooTargetSnapshotTransportError(
                    "woo_target_snapshot_response_json_invalid"
                ) from None
            return WooProductTargetPage(
                items=items,
                total=_header_integer(response.getheader("X-WP-Total"), "total"),
                total_pages=_header_integer(
                    response.getheader("X-WP-TotalPages"), "total_pages"
                ),
            )
        except WooTargetSnapshotError:
            raise
        except (TimeoutError, ConnectionResetError, OSError):
            raise WooTargetSnapshotRetryableError(
                "woo_target_snapshot_transport_retryable"
            ) from None
        finally:
            connection.close()


def _safe_local_json_file(path: Path) -> Path:
    try:
        return package_io._local_path(Path(path), require_file=True)
    except package_io.SingleProductStagingPackageInputError as error:
        code = (
            "woo_target_snapshot_linked_path_not_allowed"
            if str(error) == "single_product_linked_path_not_allowed"
            else "woo_target_snapshot_local_json_required"
        )
        raise WooTargetSnapshotInputError(code) from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_local_json_required"
        ) from None


def _json_object_no_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WooTargetSnapshotInputError(
                "woo_target_snapshot_duplicate_json_key"
            )
        result[key] = value
    return result


def read_package_report(
    path: Path,
) -> tuple[Mapping[str, object], dict[str, str], Path]:
    """Read one bounded local Package and fingerprint its exact raw bytes."""

    local = _safe_local_json_file(path)
    try:
        size = local.stat().st_size
        if size <= 0 or size > MAX_PACKAGE_BYTES:
            raise WooTargetSnapshotInputError(
                "woo_target_snapshot_package_size_invalid"
            )
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_no_duplicates
        )
    except WooTargetSnapshotInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_package_json_invalid"
        ) from None
    if not isinstance(value, Mapping):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_package_root_invalid"
        )
    source = {
        "basename": local.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return value, source, local


def validate_package_report(package: Mapping[str, object]) -> str:
    """Validate the complete POC-02 authority contract and return its sole SKU."""

    if (
        package.get("status") != "ok"
        or package.get("policy_version") != PACKAGE_POLICY_VERSION
        or package.get("write_authorized") is not False
        or package.get("blocking_issues") != []
        or any(
            type(package.get(counter)) is not int or package.get(counter) != 0
            for counter in _PACKAGE_ZERO_COUNTERS
        )
    ):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_package_not_eligible"
        )
    sku = package.get("target_sku")
    if not is_safe_sku(sku) or not isinstance(sku, str) or len(sku) > MAX_SKU_LENGTH:
        raise WooTargetSnapshotInputError("woo_target_snapshot_package_sku_invalid")
    payload = package.get("future_woo_payload")
    if (
        not isinstance(payload, Mapping)
        or payload.get("sku") != sku
        or payload.get("status") != "draft"
        or payload.get("type") != "simple"
    ):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_package_payload_invalid"
        )
    source_validation = package.get("source_validation")
    if not isinstance(source_validation, Mapping) or any(
        source_validation.get(field) is not True
        for field in _SOURCE_VALIDATION_FIELDS
    ):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_package_source_validation_invalid"
        )
    return sku


def _safe_optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_OPTIONAL_TEXT.fullmatch(value) else None


def _project_exact_product(value: object, target_sku: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WooTargetSnapshotDataError(
            "woo_target_snapshot_product_structure_invalid"
        )
    product_id, sku = value.get("id"), value.get("sku")
    if type(product_id) is not int or product_id <= 0:
        raise WooTargetSnapshotDataError("woo_target_snapshot_product_id_invalid")
    if not isinstance(sku, str) or sku != target_sku:
        raise WooTargetSnapshotDataError("woo_target_snapshot_filter_mismatch")
    return {
        "id": product_id,
        "sku": sku,
        "type": _safe_optional_text(value.get("type")),
        "status": _safe_optional_text(value.get("status")),
    }


class WooTargetSnapshotter:
    """Fully enumerate the filtered GET result before classifying the target."""

    def __init__(
        self,
        transport: WooProductTargetTransport,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(max_pages) is not int or max_pages <= 0:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_max_pages_invalid"
            )
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_max_retries_invalid"
            )
        self._transport = transport
        self._max_pages = max_pages
        self._max_retries = max_retries
        self._sleeper = sleeper

    def _read_page(self, sku: str, page: int) -> WooProductTargetPage:
        for attempt in range(self._max_retries + 1):
            try:
                return self._transport.get_products_by_sku(
                    sku,
                    page=page,
                    per_page=DEFAULT_PER_PAGE,
                )
            except WooTargetSnapshotRetryableError:
                if attempt >= self._max_retries:
                    raise
                self._sleeper(0.1 * (attempt + 1))
        raise AssertionError("GET retry loop exited unexpectedly")

    def collect_exact_products(self, sku: str) -> list[dict[str, object]]:
        first = self._read_page(sku, 1)
        if not isinstance(first.items, list):
            raise WooTargetSnapshotDataError("woo_target_snapshot_root_not_array")
        if (
            type(first.total) is not int
            or first.total < 0
            or type(first.total_pages) is not int
            or first.total_pages < 0
            or first.total_pages > self._max_pages
        ):
            raise WooTargetSnapshotDataError(
                "woo_target_snapshot_pagination_contract_invalid"
            )
        if first.total_pages == 0 and (first.total != 0 or first.items):
            raise WooTargetSnapshotDataError(
                "woo_target_snapshot_pagination_contract_invalid"
            )
        pages = [first]
        for page_number in range(2, first.total_pages + 1):
            current = self._read_page(sku, page_number)
            if (
                current.total != first.total
                or current.total_pages != first.total_pages
                or not isinstance(current.items, list)
            ):
                raise WooTargetSnapshotDataError(
                    "woo_target_snapshot_pagination_contract_invalid"
                )
            pages.append(current)
        raw_items = [item for page in pages for item in page.items]
        if len(raw_items) != first.total:
            raise WooTargetSnapshotDataError(
                "woo_target_snapshot_pagination_contract_invalid"
            )
        return [_project_exact_product(item, sku) for item in raw_items]

    def build_report(
        self,
        sku: str,
        source_package: Mapping[str, str],
        *,
        redactor: Redactor | None = None,
    ) -> dict[str, object]:
        if self._transport.write_requests_performed != 0:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_transport_not_read_only"
            )
        products = self.collect_exact_products(sku)
        if self._transport.write_requests_performed != 0:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_transport_not_read_only"
            )
        match_count = len(products)
        blockers: list[str] = []
        existing_target: dict[str, object] | None = None
        if match_count == 1:
            blockers.append("woo_target_sku_already_exists")
            existing_target = products[0]
        elif match_count > 1:
            blockers.append("woo_target_sku_ambiguous")
        report: dict[str, object] = {
            "status": "ok" if match_count == 0 else "blocked",
            "policy_version": POLICY_VERSION,
            "target": {
                "environment": "staging",
                "source_host": STAGING_EXPECTED_HOST,
                "api_version": API_VERSION,
                "resource": API_RESOURCE,
                "read_only": True,
            },
            "sku": sku,
            "match_count": match_count,
            "create_eligible": match_count == 0,
            "existing_target": existing_target,
            "source_package": dict(source_package),
            "blocking_issues": blockers,
            "write_authorized": False,
            "network_requests_performed": self._transport.network_requests_performed,
            "woocommerce_requests_performed": self._transport.network_requests_performed,
            **dict.fromkeys(_OUTPUT_ZERO_COUNTERS, 0),
        }
        safe = sanitize_report_data(report, redactor or Redactor())
        if not isinstance(safe, dict):  # pragma: no cover - structural guard
            raise AssertionError("Woo target snapshot report must remain an object")
        safe["write_authorized"] = False
        for counter in _OUTPUT_ZERO_COUNTERS:
            safe[counter] = 0
        return json.loads(json.dumps(safe, ensure_ascii=False, sort_keys=True))


def run_woo_target_snapshot(
    package_report_path: Path,
    base_url: str,
    credentials: WooCategoryCredentials | None,
    *,
    project_root: Path,
    transport: WooProductTargetTransport | None = None,
    redactor: Redactor | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, object], Path]:
    """Validate one Package, perform only exact-SKU GETs, then write safely."""

    package, source_package, package_path = read_package_report(package_report_path)
    sku = validate_package_report(package)
    normalized_base_url = validate_staging_target_base_url(base_url)
    if transport is None:
        if credentials is None:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_credentials_required"
            )
        active_transport: WooProductTargetTransport = StdlibWooProductTargetTransport(
            normalized_base_url,
            credentials,
        )
    else:
        active_transport = transport
        try:
            transport_base_url = validate_staging_target_base_url(
                active_transport.base_url
            )
        except (AttributeError, TypeError):
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_transport_target_invalid"
            ) from None
        if transport_base_url != normalized_base_url:
            raise WooTargetSnapshotConfigurationError(
                "woo_target_snapshot_transport_target_mismatch"
            )
    if active_transport.write_requests_performed != 0:
        raise WooTargetSnapshotConfigurationError(
            "woo_target_snapshot_transport_not_read_only"
        )
    output = Path(os.path.abspath(Path(project_root) / "reports" / REPORT_FILENAME))
    if package_path == output or package_io._has_link_or_reparse(output):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_output_path_invalid"
        )
    report = WooTargetSnapshotter(
        active_transport,
        sleeper=sleeper,
    ).build_report(sku, source_package, redactor=redactor)
    try:
        SafeJsonReportWriter(output, redactor or Redactor()).write(report)
    except (OSError, TypeError, ValueError):
        raise WooTargetSnapshotInputError(
            "woo_target_snapshot_report_write_failed"
        ) from None
    return report, output
