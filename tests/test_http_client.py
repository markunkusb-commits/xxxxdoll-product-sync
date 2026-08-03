from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.http_client import (  # noqa: E402
    HttpRequestError,
    HttpResponse,
    ReadOnlyHttpClient,
    RequestBlocked,
    RetryConfig,
    StdlibHttpsTransport,
)
from sync_worker.sanitization import Redactor, sanitize_url  # noqa: E402


class MockTransport:
    def __init__(self, outcomes: list[HttpResponse | Exception] | None = None) -> None:
        self.outcomes = outcomes or [HttpResponse(200, b"{}")]
        self.calls: list[tuple[str, str]] = []

    def send(
        self,
        method: str,
        url: str,
        headers: object,
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        self.calls.append((method, urlsplit(url).path))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ReadOnlyHttpClientTests(unittest.TestCase):
    def test_default_transport_also_rejects_write_methods_before_network(self) -> None:
        with self.assertRaisesRegex(RequestBlocked, "GET and HEAD"):
            StdlibHttpsTransport().send(
                "POST",
                "https://sandbox.wpcomstaging.com/wp-json/",
                {},
                5.0,
                15.0,
                1024,
            )

    def test_default_transport_rejects_sensitive_reads_before_network(self) -> None:
        with self.assertRaisesRegex(RequestBlocked, "Sensitive resource"):
            StdlibHttpsTransport().send(
                "GET",
                "https://sandbox.wpcomstaging.com/wp-json/wc/v3/orders",
                {},
                5.0,
                15.0,
                1024,
            )

    def test_blocks_non_https_base_url_before_transport(self) -> None:
        transport = MockTransport()

        with self.assertRaisesRegex(RequestBlocked, "HTTPS"):
            ReadOnlyHttpClient(
                "http://sandbox.wpcomstaging.com", transport=transport
            )

        self.assertEqual(transport.calls, [])

    def test_blocks_production_hostname_before_transport(self) -> None:
        transport = MockTransport()

        with self.assertRaises(RequestBlocked):
            ReadOnlyHttpClient("https://xxxxdoll.com", transport=transport)

        self.assertEqual(transport.calls, [])

    def test_blocks_every_write_method_without_transport(self) -> None:
        transport = MockTransport()
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com", transport=transport
        )

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(RequestBlocked, "GET and HEAD"):
                    client.request(method, "/wp-json/wc/v3/products")

        self.assertEqual(transport.calls, [])
        self.assertEqual(client.counters.write_requests_performed, 0)

    def test_blocks_sensitive_resource_endpoints(self) -> None:
        transport = MockTransport()
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com", transport=transport
        )
        forbidden_paths = (
            "/wp-json/wc/v3/orders",
            "/wp-json/wc/v3/customers",
            "/wp-json/wc/v3/payment_gateways",
            "/wp-json/wc/v3/coupons",
            "/wp-json/wp/v2/users",
        )

        for path in forbidden_paths:
            with self.subTest(path=path):
                with self.assertRaises(RequestBlocked):
                    client.get(path)

        self.assertEqual(transport.calls, [])

    def test_allows_only_current_user_identity_endpoint(self) -> None:
        transport = MockTransport([HttpResponse(200, b'{"id": 1}')])
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com", transport=transport
        )

        response = client.get("/wp-json/wp/v2/users/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(transport.calls, [("GET", "/wp-json/wp/v2/users/me")])

    def test_blocks_authentication_in_url_and_cookie_headers(self) -> None:
        transport = MockTransport()
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com", transport=transport
        )

        with self.assertRaises(RequestBlocked):
            client.get(
                "/wp-json/wc/v3/products", query={"consumer_key": "fake-key"}
            )
        with self.assertRaises(RequestBlocked):
            client.get(
                "/wp-json/wc/v3/products?consumer_secret=fake-secret"
            )
        with self.assertRaises(RequestBlocked):
            client.get(
                "/wp-json/wc/v3/products", headers={"Cookie": "fake-cookie"}
            )

        self.assertEqual(transport.calls, [])

    def test_get_and_head_counters_count_transport_calls(self) -> None:
        transport = MockTransport([HttpResponse(200, b"{}"), HttpResponse(200, b"")])
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com", transport=transport
        )

        client.get("/wp-json/")
        client.head("/wp-json/")

        self.assertEqual(client.counters.get_requests, 1)
        self.assertEqual(client.counters.head_requests, 1)
        self.assertEqual(client.counters.write_requests_performed, 0)

    def test_retries_safe_get_a_limited_number_of_times(self) -> None:
        transport = MockTransport(
            [
                RuntimeError("temporary failure"),
                HttpResponse(503, b"{}"),
                HttpResponse(200, b"{}"),
            ]
        )
        sleeps: list[float] = []
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com",
            transport=transport,
            retries=RetryConfig(max_retries=2, backoff_seconds=0.01),
            sleeper=sleeps.append,
        )

        response = client.get("/wp-json/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.counters.get_requests, 3)
        self.assertEqual(len(sleeps), 2)

    def test_transport_exception_is_redacted(self) -> None:
        secret = "fake-app-secret"
        transport = MockTransport(
            [
                RuntimeError(
                    "Authorization: Basic unsafe; Cookie=session-value; "
                    f"https://user:{secret}@sandbox.wpcomstaging.com/wp-json/"
                    "?consumer_secret=query-secret"
                )
            ]
        )
        client = ReadOnlyHttpClient(
            "https://sandbox.wpcomstaging.com",
            redactor=Redactor.from_values([secret, "query-secret"]),
            transport=transport,
            retries=RetryConfig(max_retries=0),
        )

        with self.assertRaises(HttpRequestError) as caught:
            client.get("/wp-json/")

        message = str(caught.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn("query-secret", message)
        self.assertNotIn("session-value", message)
        self.assertNotIn("Basic unsafe", message)
        self.assertNotIn("user:", message)

    def test_redactor_repr_does_not_expose_registered_secrets(self) -> None:
        secret = "fake-redactor-secret"

        self.assertNotIn(secret, repr(Redactor.from_values([secret])))

    def test_url_sanitization_removes_credentials_query_and_host_label(self) -> None:
        sanitized = sanitize_url(
            "https://user:fake-password@sandbox.wpcomstaging.com/wp-json/"
            "?consumer_key=fake-key"
        )

        self.assertEqual(sanitized, "https://***.wpcomstaging.com/wp-json/")
        self.assertNotIn("fake-password", sanitized)
        self.assertNotIn("fake-key", sanitized)
        self.assertNotIn("sandbox", sanitized)


if __name__ == "__main__":
    unittest.main()
