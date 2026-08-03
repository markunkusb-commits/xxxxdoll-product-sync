"""HTTPS-only, read-only HTTP client with endpoint guards and limited retries."""

from __future__ import annotations

import http.client
import json
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from .sanitization import Redactor


class RequestBlocked(RuntimeError):
    """Raised before transport when a request violates a safety rule."""


class HttpRequestError(RuntimeError):
    """Raised with a sanitized summary when a safe request fails."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes

    def json(self) -> object:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpRequestError("Response was not valid UTF-8 JSON") from error


class HttpTransport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class StdlibHttpsTransport:
    """Minimal transport that neither follows redirects nor stores response headers."""

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        if method.upper() not in _ALLOWED_METHODS:
            raise RequestBlocked("Transport permits only GET and HEAD")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RequestBlocked("Transport requires an HTTPS hostname")
        hostname = parsed.hostname.lower()
        if not _is_staging_hostname(hostname) or _is_production_hostname(hostname):
            raise RequestBlocked("Transport requires the configured staging domain")
        if parsed.username or parsed.password or parsed.fragment:
            raise RequestBlocked("Transport URL contains forbidden authentication data")
        _validate_resource_path(parsed.path)
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
        if query_keys & _FORBIDDEN_QUERY_KEYS:
            raise RequestBlocked("Transport URL contains forbidden query data")
        normalized_headers = {name.lower() for name in headers}
        if "cookie" in normalized_headers or "set-cookie" in normalized_headers:
            raise RequestBlocked("Transport forbids Cookie headers")

        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=connect_timeout,
            context=ssl.create_default_context(),
        )
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(read_timeout)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request(method, target, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(max_response_bytes + 1) if method == "GET" else b""
            if len(body) > max_response_bytes:
                raise HttpRequestError("Response exceeded the configured size limit")
            return HttpResponse(status_code=response.status, body=body)
        finally:
            connection.close()


@dataclass(slots=True)
class RequestCounters:
    get_requests: int = 0
    head_requests: int = 0

    @property
    def write_requests_performed(self) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    connect_seconds: float = 5.0
    read_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_retries: int = 2
    backoff_seconds: float = 0.2


_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_FORBIDDEN_RESOURCE_PREFIXES = ("order", "customer", "payment", "coupon")
_FORBIDDEN_QUERY_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "consumer_key",
        "consumer_secret",
        "password",
        "app_password",
        "token",
        "_wpnonce",
    }
)


def _decode_path(path: str) -> str:
    decoded = path
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    return decoded.lower()


def _is_staging_hostname(hostname: str | None) -> bool:
    return bool(hostname) and (
        hostname == "wpcomstaging.com" or hostname.endswith(".wpcomstaging.com")
    )


def _is_production_hostname(hostname: str | None) -> bool:
    return bool(hostname) and (
        hostname == "xxxxdoll.com" or hostname.endswith(".xxxxdoll.com")
    )


def _validate_resource_path(path: str) -> None:
    segments = [segment for segment in _decode_path(path).split("/") if segment]
    for segment in segments:
        if segment.startswith(_FORBIDDEN_RESOURCE_PREFIXES):
            raise RequestBlocked("Sensitive resource endpoint is forbidden")
    if "users" in segments:
        user_index = segments.index("users")
        if segments[user_index + 1 :] != ["me"]:
            raise RequestBlocked("User list endpoints are forbidden")


class ReadOnlyHttpClient:
    """A fail-closed client that can only perform safe staging reads."""

    def __init__(
        self,
        base_url: str,
        *,
        redactor: Redactor | None = None,
        transport: HttpTransport | None = None,
        timeouts: TimeoutConfig | None = None,
        retries: RetryConfig | None = None,
        max_response_bytes: int = 2_000_000,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname.lower() if parsed.hostname else None
        if parsed.scheme.lower() != "https":
            raise RequestBlocked("Base URL must use HTTPS")
        if not _is_staging_hostname(hostname):
            raise RequestBlocked("Base URL must target wpcomstaging.com")
        if _is_production_hostname(hostname):
            raise RequestBlocked("Production hostname is forbidden")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RequestBlocked("Base URL must not contain authentication or query data")

        port_suffix = f":{parsed.port}" if parsed.port else ""
        self._origin = f"https://{hostname}{port_suffix}"
        self._base_path = parsed.path.rstrip("/")
        self.hostname = hostname
        self._redactor = redactor or Redactor()
        self._transport = transport or StdlibHttpsTransport()
        self._timeouts = timeouts or TimeoutConfig()
        self._retries = retries or RetryConfig()
        self._max_response_bytes = max_response_bytes
        self._sleeper = sleeper
        self.counters = RequestCounters()

    def _validate_path(self, path: str) -> None:
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not path.startswith("/")
            or path.startswith("//")
        ):
            raise RequestBlocked("Only absolute paths on the configured host are allowed")
        _validate_resource_path(parsed.path)

    def _build_url(
        self,
        path: str,
        query: Mapping[str, str | int | Sequence[str | int]] | None,
    ) -> str:
        self._validate_path(path)
        encoded_query = ""
        if query:
            normalized_keys = {str(key).lower() for key in query}
            if normalized_keys & _FORBIDDEN_QUERY_KEYS:
                raise RequestBlocked("Authentication data is forbidden in URL queries")
            encoded_query = urlencode(query, doseq=True)
        url = self._origin + self._base_path + path
        return url + ("?" + encoded_query if encoded_query else "")

    @staticmethod
    def _validate_headers(headers: Mapping[str, str]) -> None:
        normalized = {name.lower() for name in headers}
        if "cookie" in normalized or "set-cookie" in normalized:
            raise RequestBlocked("Cookie headers are forbidden")

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int | Sequence[str | int]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        normalized_method = str(method).upper()
        if normalized_method not in _ALLOWED_METHODS:
            raise RequestBlocked("Only GET and HEAD requests are permitted")

        safe_headers = dict(headers or {})
        self._validate_headers(safe_headers)
        url = self._build_url(path, query)
        attempts = self._retries.max_retries + 1

        for attempt in range(attempts):
            if normalized_method == "GET":
                self.counters.get_requests += 1
            else:
                self.counters.head_requests += 1
            try:
                response = self._transport.send(
                    normalized_method,
                    url,
                    safe_headers,
                    self._timeouts.connect_seconds,
                    self._timeouts.read_seconds,
                    self._max_response_bytes,
                )
            except RequestBlocked:
                raise
            except Exception as error:
                if attempt + 1 < attempts:
                    self._sleeper(self._retries.backoff_seconds * (attempt + 1))
                    continue
                raise HttpRequestError(self._redactor.exception(error)) from None

            if response.status_code not in _RETRYABLE_STATUSES or attempt + 1 >= attempts:
                return response
            self._sleeper(self._retries.backoff_seconds * (attempt + 1))

        raise AssertionError("Retry loop exited unexpectedly")

    def get(
        self,
        path: str,
        *,
        query: Mapping[str, str | int | Sequence[str | int]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self.request("GET", path, query=query, headers=headers)

    def head(
        self,
        path: str,
        *,
        query: Mapping[str, str | int | Sequence[str | int]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self.request("HEAD", path, query=query, headers=headers)
