from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser  # noqa: E402
from sync_worker.config import ConfigError, Settings, load_config  # noqa: E402
from sync_worker.doctor import DoctorRunner, redactor_for_settings  # noqa: E402
from sync_worker.http_client import HttpResponse, ReadOnlyHttpClient  # noqa: E402
from sync_worker.report import DoctorReportWriter  # noqa: E402
from sync_worker.sanitization import Redactor  # noqa: E402


SAFE_CONFIG = {
    "WP_BASE_URL": "https://sandbox.wpcomstaging.com",
    "WP_USERNAME": "fake-wordpress-user",
    "WP_APP_PASSWORD": "fake-wordpress-password",
    "WC_CONSUMER_KEY": "fake-woocommerce-key",
    "WC_CONSUMER_SECRET": "fake-woocommerce-secret",
    "SYNC_ENVIRONMENT": "staging",
    "DRY_RUN": "true",
    "DEFAULT_PRODUCT_STATUS": "draft",
    "ALLOW_DELETE": "false",
}


def _json_response(payload: object, status_code: int = 200) -> HttpResponse:
    return HttpResponse(status_code, json.dumps(payload).encode("utf-8"))


def _route_payloads(*, include_yith: bool = True) -> dict[str, HttpResponse]:
    namespaces = ["wp/v2", "wc/v3"]
    routes: dict[str, object] = {"/wc/v3": {}}
    if include_yith:
        namespaces.append("yith-wapo/v1")
        routes["/yith-wapo/v1/addons"] = {}

    return {
        "/wp-json/": _json_response(
            {"namespaces": namespaces, "routes": routes}
        ),
        "/wp-json/wp/v2/users/me": _json_response(
            {
                "id": 7,
                "name": "must-not-be-reported",
                "email": "private@example.invalid",
            }
        ),
        "/wp-json/wp/v2/media": _json_response(
            [{"id": 9, "author": 7, "caption": "must-not-be-reported"}]
        ),
        "/wp-json/wc/v3/products": _json_response(
            [
                {
                    "id": index,
                    "sku": "fake-wordpress-password" if index == 1 else f"SKU-{index}",
                    "status": "draft",
                    "type": "simple",
                    "description": "must-not-be-reported",
                    "images": [{"src": "https://example.invalid/private.jpg"}],
                }
                for index in range(1, 7)
            ]
        ),
        "/wp-json/wc/v3/products/categories": _json_response(
            [
                {
                    "id": 11,
                    "name": "Dolls",
                    "parent": 0,
                    "description": "must-not-be-reported",
                }
            ]
        ),
        "/wp-json/wc/v3/products/attributes": _json_response(
            [
                {
                    "id": 12,
                    "name": "Color",
                    "slug": "pa_color",
                    "type": "select",
                }
            ]
        ),
        "/wp-json/wp/v2/taxonomies": _json_response(
            {
                "product_cat": {
                    "name": "Product categories",
                    "slug": "product_cat",
                    "rest_base": "product_cat",
                    "types": ["product"],
                    "capabilities": {"manage_terms": "private-capability"},
                },
                "category": {
                    "name": "Post categories",
                    "slug": "category",
                    "rest_base": "categories",
                    "types": ["post"],
                },
            }
        ),
    }


class RouteMockTransport:
    """Mock transport that retains only method/path and never header values."""

    def __init__(self, routes: dict[str, HttpResponse]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, str]] = []
        self.authorization_headers_seen = 0

    def send(
        self,
        method: str,
        url: str,
        headers: object,
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        self.calls.append((method, parsed.path))
        if isinstance(headers, dict) and "Authorization" in headers:
            self.authorization_headers_seen += 1
        return self._routes.get(parsed.path, _json_response({}, 404))


class DoctorTests(unittest.TestCase):
    def _run_doctor(self, *, include_yith: bool = True) -> tuple[dict[str, object], RouteMockTransport]:
        settings = load_config(SAFE_CONFIG)
        redactor = redactor_for_settings(settings)
        transport = RouteMockTransport(_route_payloads(include_yith=include_yith))
        client = ReadOnlyHttpClient(
            settings.wp_base_url,
            redactor=redactor,
            transport=transport,
            sleeper=lambda _: None,
        )
        runner = DoctorRunner(
            settings,
            client,
            redactor=redactor,
            clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
        return runner.run(), transport

    def test_doctor_uses_only_mocked_get_requests_and_allowlists_fields(self) -> None:
        report, transport = self._run_doctor()

        self.assertEqual(len(transport.calls), 7)
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))
        self.assertEqual(report["get_requests"], 7)
        self.assertEqual(report["head_requests"], 0)
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(len(report["products"]), 5)
        self.assertEqual(
            set(report["products"][0]), {"id", "sku", "status", "type"}
        )
        self.assertEqual(
            set(report["product_categories"][0]), {"id", "name", "parent_id"}
        )
        self.assertEqual(
            set(report["product_attributes"][0]), {"id", "name", "slug"}
        )
        self.assertEqual(len(report["product_taxonomies"]), 1)
        self.assertEqual(report["woocommerce_detection"], "detected")
        self.assertEqual(report["yith_detection"], "detected")
        self.assertEqual(transport.authorization_headers_seen, 5)

    def test_doctor_report_contains_no_credentials_or_private_response_data(self) -> None:
        report, _ = self._run_doctor()
        serialized = json.dumps(report, sort_keys=True)
        wordpress_basic_token = base64.b64encode(
            b"fake-wordpress-user:fake-wordpress-password"
        ).decode("ascii")
        woocommerce_basic_token = base64.b64encode(
            b"fake-woocommerce-key:fake-woocommerce-secret"
        ).decode("ascii")

        for forbidden in (
            "fake-wordpress-user",
            "fake-wordpress-password",
            "fake-woocommerce-key",
            "fake-woocommerce-secret",
            "Authorization",
            "Cookie",
            "private@example.invalid",
            "must-not-be-reported",
            "private-capability",
            wordpress_basic_token,
            woocommerce_basic_token,
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn("sandbox.wpcomstaging.com", serialized)
        self.assertIn("***.wpcomstaging.com", serialized)

    def test_yith_falls_back_to_wordpress_bridge(self) -> None:
        report, _ = self._run_doctor(include_yith=False)

        self.assertEqual(report["yith_detection"], "requires_wordpress_bridge")

    def test_non_staging_settings_stop_before_mock_transport(self) -> None:
        settings = Settings(
            wp_base_url="https://sandbox.wpcomstaging.com",
            sync_environment="production",
            dry_run=True,
            default_product_status="draft",
            allow_delete=False,
        )
        transport = RouteMockTransport(_route_payloads())
        client = ReadOnlyHttpClient(settings.wp_base_url, transport=transport)

        with self.assertRaisesRegex(ConfigError, "SYNC_ENVIRONMENT"):
            DoctorRunner(settings, client).run()

        self.assertEqual(transport.calls, [])
        self.assertEqual(client.counters.write_requests_performed, 0)

    def test_every_control_safety_failure_stops_before_mock_transport(self) -> None:
        unsafe_controls = (
            {"sync_environment": "production"},
            {"dry_run": False},
            {"default_product_status": "publish"},
            {"allow_delete": True},
        )

        for override in unsafe_controls:
            with self.subTest(override=override):
                values = {
                    "wp_base_url": "https://sandbox.wpcomstaging.com",
                    "sync_environment": "staging",
                    "dry_run": True,
                    "default_product_status": "draft",
                    "allow_delete": False,
                    **override,
                }
                settings = Settings(**values)
                transport = RouteMockTransport(_route_payloads())
                client = ReadOnlyHttpClient(
                    "https://sandbox.wpcomstaging.com", transport=transport
                )

                with self.assertRaises(ConfigError):
                    DoctorRunner(settings, client).run()

                self.assertEqual(transport.calls, [])
                self.assertEqual(client.counters.write_requests_performed, 0)

    def test_report_writer_drops_forbidden_fields_and_forces_zero_writes(self) -> None:
        secret = "fake-report-secret"
        redactor = Redactor.from_values([secret])
        unsafe_report = {
            "timestamp": "2026-08-03T00:00:00+00:00",
            "staging_hostname": "***.wpcomstaging.com",
            "authorization": f"Basic {secret}",
            "cookie": secret,
            "full_url": f"https://user:{secret}@sandbox.wpcomstaging.com/?token=x",
            "customer": {"email": secret},
            "products": [{"id": 1, "sku": secret}],
            "errors": [{"summary": f"failed with {secret}"}],
            "write_requests_performed": 99,
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "doctor-report.json"
            DoctorReportWriter(path, redactor).write(unsafe_report)
            saved_text = path.read_text(encoding="utf-8")
            saved = json.loads(saved_text)

        self.assertNotIn(secret, saved_text)
        self.assertNotIn("authorization", saved)
        self.assertNotIn("cookie", saved)
        self.assertNotIn("full_url", saved)
        self.assertNotIn("customer", saved)
        self.assertEqual(saved["write_requests_performed"], 0)

    def test_cli_parser_accepts_doctor_command_without_running_it(self) -> None:
        arguments = build_parser().parse_args(["doctor"])

        self.assertEqual(arguments.command, "doctor")


if __name__ == "__main__":
    unittest.main()
