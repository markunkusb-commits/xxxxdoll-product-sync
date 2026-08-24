"""Read-only WooCommerce product-category discovery.

This module is intentionally independent from the internal Category Registry.
It discovers current WooCommerce category records for later human review and
never creates bindings or exposes a generic HTTP request API.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from .report import SafeJsonReportWriter, sanitize_report_data
from .sanitization import Redactor, sanitize_url
from .security import basic_auth_headers


API_VERSION = "wc/v3"
API_RESOURCE = "products/categories"
CATEGORY_ENDPOINT = "/wp-json/wc/v3/products/categories"
DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_RETRIES = 2
REPORT_FILENAME = "woo-category-discovery.json"

_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_SENSITIVE_REPORT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:ck|cs)_[A-Za-z0-9]+"
    r"|WC_CONSUMER_KEY|WC_CONSUMER_SECRET"
    r"|Authorization|Cookie|password|token"
)


class WooCategoryDiscoveryError(RuntimeError):
    """Base class for safe discovery failures."""


class WooCategoryConfigurationError(WooCategoryDiscoveryError):
    """Raised before network access for an unsafe target or option."""


class WooCategoryCredentialError(WooCategoryDiscoveryError):
    """Raised before network access when process credentials are missing."""


class WooCategoryTransportError(WooCategoryDiscoveryError):
    """Non-retryable safe transport failure."""


class WooCategoryRetryableError(WooCategoryTransportError):
    """Retryable timeout, reset, or transient HTTP failure."""


class WooCategoryDataError(WooCategoryDiscoveryError):
    """Raised for invalid or duplicate Woo category data."""


class WooCategoryPaginationError(WooCategoryDiscoveryError):
    """Raised for unsafe or inconsistent pagination behavior."""


@dataclass(frozen=True, slots=True)
class WooCategoryCredentials:
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WooCategoryPage:
    items: object
    total: int | None = None
    total_pages: int | None = None


@dataclass(frozen=True, slots=True)
class WooCategorySource:
    api_version: str = API_VERSION
    endpoint: str = CATEGORY_ENDPOINT


@dataclass(frozen=True, slots=True)
class WooCategoryRecord:
    id: int
    name: str
    slug: str
    parent: int | None
    count: int | None
    description: str | None
    display: str | None
    parent_name: str | None
    category_path: str
    source: WooCategorySource
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class WooCategoryTransport(Protocol):
    @property
    def base_url(self) -> str: ...

    @property
    def network_requests_performed(self) -> int: ...

    @property
    def write_requests_performed(self) -> int: ...

    def get_categories(
        self,
        *,
        page: int,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> WooCategoryPage: ...


def normalize_woo_base_url(base_url: str) -> str:
    """Validate an HTTPS origin; HTTP is accepted only for local test fixtures."""

    if not isinstance(base_url, str) or not base_url.strip():
        raise WooCategoryConfigurationError("base_url is required")
    try:
        parsed = urlsplit(base_url.strip())
        port = parsed.port
    except ValueError:
        raise WooCategoryConfigurationError("base_url is invalid") from None
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold() if parsed.hostname else None
    if scheme not in {"http", "https"} or hostname is None:
        raise WooCategoryConfigurationError("base_url must be an HTTP(S) URL")
    if scheme != "https" and hostname not in _LOCALHOST_NAMES:
        raise WooCategoryConfigurationError("base_url must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WooCategoryConfigurationError(
            "base_url must not contain authentication or query data"
        )
    if _SENSITIVE_REPORT_PATTERN.search(parsed.path):
        raise WooCategoryConfigurationError("base_url path contains sensitive data")
    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    port_suffix = f":{port}" if port is not None else ""
    base_path = parsed.path.rstrip("/")
    return f"{scheme}://{host_for_url}{port_suffix}{base_path}"


def load_woo_category_credentials(
    environ: Mapping[str, str] | None = None,
) -> WooCategoryCredentials:
    """Read credentials only from the current process environment."""

    source = os.environ if environ is None else environ
    consumer_key = source.get("WC_CONSUMER_KEY", "").strip()
    consumer_secret = source.get("WC_CONSUMER_SECRET", "").strip()
    if not consumer_key:
        raise WooCategoryCredentialError("WC_CONSUMER_KEY is not configured")
    if not consumer_secret:
        raise WooCategoryCredentialError("WC_CONSUMER_SECRET is not configured")
    return WooCategoryCredentials(consumer_key, consumer_secret)


def redactor_for_woo_category_credentials(
    credentials: WooCategoryCredentials,
) -> Redactor:
    headers = basic_auth_headers(
        credentials.consumer_key,
        credentials.consumer_secret,
    )
    authorization = headers.get("Authorization", "")
    token = authorization.removeprefix("Basic ")
    return Redactor.from_values(
        (
            credentials.consumer_key,
            credentials.consumer_secret,
            authorization,
            token,
        )
    )


def _header_integer(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise WooCategoryTransportError(
            f"Woo pagination header {name} was invalid"
        ) from None
    if parsed < 0:
        raise WooCategoryTransportError(
            f"Woo pagination header {name} was invalid"
        )
    return parsed


class StdlibWooCategoryTransport:
    """Single-purpose GET transport for Woo product categories."""

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
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if not isinstance(credentials, WooCategoryCredentials):
            raise TypeError("credentials must be WooCategoryCredentials")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise WooCategoryConfigurationError("timeouts must be positive")
        if max_response_bytes <= 0:
            raise WooCategoryConfigurationError(
                "max_response_bytes must be positive"
            )
        self._base_url = normalize_woo_base_url(base_url)
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

    def get_categories(
        self,
        *,
        page: int,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> WooCategoryPage:
        if type(page) is not int or page <= 0:
            raise WooCategoryConfigurationError("page must be a positive integer")
        if per_page != DEFAULT_PER_PAGE:
            raise WooCategoryConfigurationError("per_page must be 100")

        parsed = self._parsed_base_url
        connection_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            parsed.hostname,
            parsed.port,
            timeout=self._connect_timeout,
            context=ssl.create_default_context(),
        ) if parsed.scheme == "https" else connection_class(
            parsed.hostname,
            parsed.port,
            timeout=self._connect_timeout,
        )
        target = (
            parsed.path.rstrip("/")
            + CATEGORY_ENDPOINT
            + "?"
            + urlencode({"page": page, "per_page": per_page})
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
                raise WooCategoryTransportError(
                    "Woo category response exceeded the size limit"
                )
            if response.status in _RETRYABLE_HTTP_STATUSES:
                raise WooCategoryRetryableError(
                    f"Woo category GET returned HTTP {response.status}"
                )
            if not 200 <= response.status < 300:
                raise WooCategoryTransportError(
                    f"Woo category GET returned HTTP {response.status}"
                )
            try:
                items = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise WooCategoryTransportError(
                    "Woo category response was not valid UTF-8 JSON"
                ) from None
            return WooCategoryPage(
                items=items,
                total=_header_integer(
                    response.getheader("X-WP-Total"),
                    "X-WP-Total",
                ),
                total_pages=_header_integer(
                    response.getheader("X-WP-TotalPages"),
                    "X-WP-TotalPages",
                ),
            )
        except (WooCategoryTransportError, WooCategoryConfigurationError):
            raise
        except (TimeoutError, ConnectionResetError, OSError) as error:
            raise WooCategoryRetryableError(
                f"Woo category GET failed: {type(error).__name__}"
            ) from None
        finally:
            connection.close()


def _strict_positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise WooCategoryDataError("Woo category id must be a positive integer")
    return value


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise WooCategoryDataError(f"Woo category {field_name} must be text")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_category(value: object) -> WooCategoryRecord:
    if not isinstance(value, Mapping):
        raise WooCategoryDataError("Woo category record must be an object")
    category_id = _strict_positive_id(value.get("id"))
    warnings: list[str] = []

    raw_parent = value.get("parent", 0)
    if type(raw_parent) is int and raw_parent >= 0:
        parent: int | None = raw_parent
    else:
        parent = None
        warnings.append("invalid_parent_value")

    raw_count = value.get("count")
    if type(raw_count) is int and raw_count >= 0:
        count: int | None = raw_count
    else:
        count = None
        warnings.append("invalid_count_value")

    name = _required_text(value.get("name"), "name")
    return WooCategoryRecord(
        id=category_id,
        name=name,
        slug=_required_text(value.get("slug"), "slug"),
        parent=parent,
        count=count,
        description=_optional_text(value.get("description")),
        display=_optional_text(value.get("display")),
        parent_name=None,
        category_path=name,
        source=WooCategorySource(),
        warnings=tuple(warnings),
    )


def _page_signature(items: Sequence[object]) -> tuple[object, ...]:
    return tuple(
        item.get("id") if isinstance(item, Mapping) else None for item in items
    )


def _add_tree_metadata(
    records: Sequence[WooCategoryRecord],
) -> tuple[WooCategoryRecord, ...]:
    by_id = {record.id: record for record in records}

    def category_path(record: WooCategoryRecord) -> tuple[str, tuple[str, ...]]:
        names = [record.name]
        warnings: list[str] = []
        visited = {record.id}
        parent_id = record.parent
        while parent_id not in {None, 0}:
            parent = by_id.get(parent_id)
            if parent is None:
                warnings.append("invalid_parent_reference")
                break
            if parent.id in visited:
                warnings.append("parent_cycle_detected")
                break
            visited.add(parent.id)
            names.append(parent.name)
            parent_id = parent.parent
        return " > ".join(reversed(names)), tuple(warnings)

    enriched: list[WooCategoryRecord] = []
    for record in records:
        path, path_warnings = category_path(record)
        parent = by_id.get(record.parent) if record.parent else None
        warnings = tuple(dict.fromkeys((*record.warnings, *path_warnings)))
        enriched.append(
            replace(
                record,
                parent_name=parent.name if parent is not None else None,
                category_path=path,
                warnings=warnings,
            )
        )
    return tuple(enriched)


def _collect_sensitive_strings(value: object) -> tuple[str, ...]:
    discovered: list[str] = []
    if isinstance(value, str) and _SENSITIVE_REPORT_PATTERN.search(value):
        discovered.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            discovered.extend(_collect_sensitive_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            discovered.extend(_collect_sensitive_strings(item))
    return tuple(dict.fromkeys(discovered))


class WooCategoryDiscovery:
    """Paginate, normalize, and report Woo categories without binding them."""

    def __init__(
        self,
        transport: WooCategoryTransport,
        *,
        redactor: Redactor | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(max_pages) is not int or max_pages <= 0:
            raise WooCategoryConfigurationError("max_pages must be positive")
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise WooCategoryConfigurationError("max_retries must be from 0 to 3")
        self._transport = transport
        self._redactor = redactor or Redactor()
        self._max_pages = max_pages
        self._max_retries = max_retries
        self._sleeper = sleeper

    def _read_page(self, page: int) -> WooCategoryPage:
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                return self._transport.get_categories(
                    page=page,
                    per_page=DEFAULT_PER_PAGE,
                )
            except WooCategoryRetryableError:
                if attempt + 1 >= attempts:
                    raise
                self._sleeper(0.1 * (attempt + 1))
        raise AssertionError("retry loop exited unexpectedly")

    def discover(self) -> tuple[tuple[WooCategoryRecord, ...], int]:
        raw_categories: list[object] = []
        seen_page_signatures: set[tuple[object, ...]] = set()
        pages_read = 0
        page_number = 1

        while True:
            page = self._read_page(page_number)
            if not isinstance(page.items, list):
                raise WooCategoryDataError("Woo category page must be an array")
            signature = _page_signature(page.items)
            if page.items and signature in seen_page_signatures:
                raise WooCategoryPaginationError("Woo category page was repeated")
            if page.items:
                seen_page_signatures.add(signature)
            raw_categories.extend(page.items)
            pages_read += 1

            if page.total_pages is not None:
                if type(page.total_pages) is not int or page.total_pages < 0:
                    raise WooCategoryPaginationError("total_pages was invalid")
                if page.total_pages > self._max_pages:
                    raise WooCategoryPaginationError(
                        "Woo category pagination exceeded the maximum"
                    )
                if page_number >= max(page.total_pages, 1):
                    break
            elif len(page.items) < DEFAULT_PER_PAGE:
                break

            if page_number >= self._max_pages:
                raise WooCategoryPaginationError(
                    "Woo category pagination exceeded the maximum"
                )
            page_number += 1

        normalized: list[WooCategoryRecord] = []
        seen_ids: set[int] = set()
        for raw_category in raw_categories:
            category = _normalize_category(raw_category)
            if category.id in seen_ids:
                raise WooCategoryDataError("Woo category id was duplicated")
            seen_ids.add(category.id)
            normalized.append(category)
        normalized.sort(key=lambda category: category.id)
        return _add_tree_metadata(normalized), pages_read

    def build_report(self) -> dict[str, object]:
        records, pages_read = self.discover()
        report: dict[str, object] = {
            "status": "ok",
            "base_url": sanitize_url(self._transport.base_url),
            "api": {
                "version": API_VERSION,
                "resource": API_RESOURCE,
                "read_only": True,
            },
            "summary": {
                "total_categories": len(records),
                "root_categories": sum(record.parent == 0 for record in records),
                "child_categories": sum(
                    isinstance(record.parent, int) and record.parent > 0
                    for record in records
                ),
                "categories_with_products": sum(
                    isinstance(record.count, int) and record.count > 0
                    for record in records
                ),
                "empty_categories": sum(record.count == 0 for record in records),
                "pages_read": pages_read,
            },
            "network_requests_performed": self._transport.network_requests_performed,
            "write_requests_performed": 0,
            "categories": [record.to_dict() for record in records],
        }
        report_redactor = Redactor.from_values(
            (*self._redactor.secrets, *_collect_sensitive_strings(report))
        )
        sanitized = sanitize_report_data(report, report_redactor)
        if not isinstance(sanitized, dict):  # pragma: no cover
            raise AssertionError("Woo category discovery report must be an object")
        sanitized["write_requests_performed"] = 0
        return sanitized


def run_woo_category_discovery(
    base_url: str,
    credentials: WooCategoryCredentials,
    *,
    project_root: Path,
    transport: WooCategoryTransport | None = None,
) -> tuple[dict[str, object], Path]:
    """Run one read-only discovery and persist its sanitized local report."""

    normalized_base_url = normalize_woo_base_url(base_url)
    redactor = redactor_for_woo_category_credentials(credentials)
    active_transport = transport or StdlibWooCategoryTransport(
        normalized_base_url,
        credentials,
    )
    report = WooCategoryDiscovery(
        active_transport,
        redactor=redactor,
    ).build_report()
    output_path = Path(project_root) / "reports" / REPORT_FILENAME
    SafeJsonReportWriter(output_path, redactor).write(report)
    return report, output_path
