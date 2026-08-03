from __future__ import annotations

import base64
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser  # noqa: E402
from sync_worker.config import ConfigError, Settings, load_config  # noqa: E402
from sync_worker.http_client import (  # noqa: E402
    HttpResponse,
    ReadOnlyHttpClient,
    RetryConfig,
)
from sync_worker.inspect_product import ReferenceProductInspector  # noqa: E402
from sync_worker.report import ReferenceProductReportWriter  # noqa: E402
from sync_worker.security import redactor_for_settings  # noqa: E402


SKU = "MD-M001-150-A-SUSAN"
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


DESCRIPTION_HTML = (
    '<section class="reference-only">'
    '<img src="https://cdn.example.invalid/images/01-intro.webp?token=fake">'
    '<p>Private source HTML must not be copied.</p>'
    '<img src="https://cdn.example.invalid/images/spec.webp">'
    '<img data-src="/uploads/03-detail.webp">'
    "</section>"
)


def _product() -> dict[str, object]:
    return {
        "id": 150,
        "name": "Susan",
        "slug": "susan",
        "sku": SKU,
        "type": "simple",
        "status": "draft",
        "regular_price": "199.00",
        "sale_price": "179.00",
        "stock_status": "instock",
        "catalog_visibility": "visible",
        "featured": False,
        "virtual": False,
        "downloadable": False,
        "categories": [
            {"id": 10, "name": "MD DOLLS", "slug": "md-dolls"},
            {"id": 11, "name": "Susan Collection", "slug": "susan"},
        ],
        "brands": [{"id": 20, "name": "XXXXDOLL", "slug": "xxxxdoll"}],
        "tags": [{"id": 30, "name": "Reference", "slug": "reference"}],
        "taxonomies": {
            "pa_collection": [
                {"term_id": 31, "name": "MD Series", "slug": "md-series"}
            ]
        },
        "attributes": [
            {
                "id": 0,
                "name": "Custom Outfit",
                "slug": "custom-outfit",
                "position": 0,
                "visible": True,
                "variation": False,
                "options": ["Default", "Formal"],
            },
            {
                "id": 5,
                "name": "Color",
                "slug": "pa_color",
                "position": 1,
                "visible": True,
                "variation": True,
                "options": ["Warm", "Cool"],
            },
        ],
        "images": [
            {
                "id": 501,
                "src": "https://cdn.example.invalid/uploads/susan-main.webp?token=x",
                "alt": "Susan main",
            },
            {
                "id": 502,
                "src": "https://cdn.example.invalid/uploads/susan-side.webp",
                "alt": "Susan side",
            },
            {
                "id": 503,
                "src": "https://cdn.example.invalid/uploads/spec.webp",
                "alt": "Specifications",
            },
        ],
        "short_description": "<p>Short Susan summary.</p>",
        "description": DESCRIPTION_HTML,
        "meta_data": [
            {"key": "_product_addons_exclude_global", "value": "0"},
            {
                "key": "_product_addons",
                "value": {
                    "option_name": "must-not-be-reported-option",
                    "price": "must-not-be-reported-price",
                    "image_url": "https://example.invalid/private-addon.webp",
                    "html": "<b>must-not-be-reported-html</b>",
                },
            },
            {"key": "original_folder", "value": "source/susan"},
            {"key": "local_image_folder", "value": "images/susan"},
            {"key": "_stripe_token", "value": "sensitive-meta-value"},
            {"key": "paypal_secret", "value": "another-sensitive-value"},
            {"key": "custom_mapping_candidate", "value": "unknown-value"},
        ],
        "customer_note": "must-not-be-reported",
        "permalink": "https://sandbox.wpcomstaging.com/product/susan/?token=unsafe",
    }


def _response(payload: object, status_code: int = 200) -> HttpResponse:
    return HttpResponse(status_code, json.dumps(payload).encode("utf-8"))


class ProductMockTransport:
    """Mock-only transport retaining no credentials or full URLs."""

    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._response = _response(payload, status_code)
        self.calls: list[tuple[str, str, dict[str, list[str]]]] = []
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
        self.calls.append((method, parsed.path, parse_qs(parsed.query)))
        if isinstance(headers, dict) and "Authorization" in headers:
            self.authorization_headers_seen += 1
        return self._response


class FailingProductMockTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(
        self,
        method: str,
        url: str,
        headers: object,
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        self.calls += 1
        authorization = (
            headers.get("Authorization", "") if isinstance(headers, dict) else ""
        )
        raise RuntimeError(
            f"Authorization: {authorization}; Cookie=fake-cookie; {url}"
        )


class InspectProductTests(unittest.TestCase):
    def _inspect(
        self, payload: object
    ) -> tuple[dict[str, object], ProductMockTransport]:
        settings = load_config(SAFE_CONFIG)
        redactor = redactor_for_settings(settings)
        transport = ProductMockTransport(payload)
        client = ReadOnlyHttpClient(
            settings.wp_base_url,
            redactor=redactor,
            transport=transport,
            sleeper=lambda _: None,
        )
        inspector = ReferenceProductInspector(
            settings,
            client,
            redactor=redactor,
            clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
        return inspector.run(SKU), transport

    def test_unique_exact_sku_is_found_with_one_mock_get(self) -> None:
        mismatched = {**_product(), "id": 999, "sku": "OTHER-SKU"}
        report, transport = self._inspect([mismatched, _product()])

        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["product_found"])
        self.assertEqual(report["product_id"], 150)
        self.assertEqual(report["product"]["sku"], SKU)
        self.assertEqual(
            transport.calls,
            [("GET", "/wp-json/wc/v3/products", {"sku": [SKU]})],
        )
        self.assertEqual(transport.authorization_headers_seen, 1)
        self.assertEqual(report["get_requests"], 1)
        self.assertEqual(report["head_requests"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_product_not_found_returns_no_product_data(self) -> None:
        report, _ = self._inspect([])

        self.assertEqual(report["status"], "product_not_found")
        self.assertFalse(report["product_found"])
        self.assertIsNone(report["product_id"])
        self.assertEqual(report["product"], {})

    def test_duplicate_exact_sku_returns_error_without_product_data(self) -> None:
        report, _ = self._inspect([_product(), {**_product(), "id": 151}])

        self.assertEqual(report["status"], "duplicate_sku_error")
        self.assertFalse(report["product_found"])
        self.assertEqual(report["product"], {})

    def test_product_level_custom_attribute_is_preserved_safely(self) -> None:
        report, _ = self._inspect([_product()])
        custom_attribute = report["attributes"][0]

        self.assertEqual(custom_attribute["id"], 0)
        self.assertEqual(custom_attribute["name"], "Custom Outfit")
        self.assertEqual(custom_attribute["options"], ["Default", "Formal"])
        self.assertEqual(
            set(custom_attribute),
            {"id", "name", "slug", "position", "visible", "variation", "options"},
        )

    def test_product_basic_fields_are_strictly_allowlisted(self) -> None:
        report, _ = self._inspect([_product()])

        self.assertEqual(
            set(report["product"]),
            {
                "id",
                "name",
                "slug",
                "sku",
                "type",
                "status",
                "regular_price",
                "sale_price",
                "stock_status",
                "catalog_visibility",
                "featured",
                "virtual",
                "downloadable",
            },
        )
        self.assertNotIn("customer_note", report["product"])
        self.assertNotIn("permalink", report["product"])

    def test_categories_brands_and_product_taxonomies_are_allowlisted(self) -> None:
        report, _ = self._inspect([_product()])

        self.assertEqual(report["categories"][0]["name"], "MD DOLLS")
        self.assertEqual(
            set(report["categories"][0]), {"id", "name", "slug"}
        )
        self.assertEqual(report["brand_terms"][0]["term_id"], 20)
        self.assertEqual(
            set(report["brand_terms"][0]), {"term_id", "name", "slug"}
        )
        self.assertEqual(
            [term["term_id"] for term in report["product_taxonomy_terms"]],
            [30, 31],
        )

    def test_image_manifest_keeps_order_but_not_urls(self) -> None:
        report, _ = self._inspect([_product()])
        manifest = report["image_manifest"]
        serialized = json.dumps(manifest)

        self.assertEqual(
            [item["file_name"] for item in manifest],
            ["susan-main.webp", "susan-side.webp", "spec.webp"],
        )
        self.assertEqual([item["position"] for item in manifest], [0, 1, 2])
        self.assertTrue(manifest[0]["is_primary"])
        self.assertFalse(manifest[1]["is_primary"])
        self.assertNotIn("https://", serialized)
        self.assertNotIn("token=", serialized)

    def test_description_html_is_reduced_to_summary_only(self) -> None:
        report, _ = self._inspect([_product()])
        summary = report["description_summary"]
        serialized = json.dumps(report)

        self.assertEqual(summary["description_characters"], len(DESCRIPTION_HTML))
        self.assertEqual(summary["description_image_count"], 3)
        self.assertEqual(
            summary["description_image_file_names"],
            ["01-intro.webp", "spec.webp", "03-detail.webp"],
        )
        self.assertTrue(summary["contains_spec_webp"])
        self.assertNotIn("<section", serialized)
        self.assertNotIn("Private source HTML", serialized)

    def _contains_spec_for_file(self, file_name: str | None) -> bool:
        product = _product()
        product["description"] = (
            f'<img src="/uploads/{file_name}">' if file_name is not None else "<p>No image</p>"
        )
        report, _ = self._inspect([product])
        return bool(report["description_summary"]["contains_spec_webp"])

    def test_exact_spec_webp_is_recognized(self) -> None:
        self.assertTrue(self._contains_spec_for_file("spec.webp"))

    def test_sku_spec_webp_is_recognized(self) -> None:
        self.assertTrue(self._contains_spec_for_file(f"{SKU}-spec.webp"))

    def test_uppercase_sku_spec_webp_is_recognized(self) -> None:
        self.assertTrue(self._contains_spec_for_file(f"{SKU}-SPEC.WEBP"))

    def test_specification_webp_is_not_recognized(self) -> None:
        self.assertFalse(self._contains_spec_for_file("specification.webp"))

    def test_description_without_spec_image_is_not_recognized(self) -> None:
        self.assertFalse(self._contains_spec_for_file(None))

    def test_meta_whitelist_and_unknown_keys_never_expose_unknown_values(self) -> None:
        report, _ = self._inspect([_product()])
        serialized = json.dumps(report)
        unknown = report["unknown_meta_keys"]

        self.assertEqual(
            report["whitelisted_meta"],
            {
                "_product_addons_exclude_global": False,
                "original_folder": "source/susan",
                "local_image_folder": "images/susan",
            },
        )
        self.assertEqual(
            {item["key"] for item in unknown},
            {"_stripe_token", "paypal_secret", "custom_mapping_candidate"},
        )
        self.assertTrue(all(item["review_required"] for item in unknown))
        for sensitive_value in (
            "sensitive-meta-value",
            "another-sensitive-value",
            "unknown-value",
        ):
            self.assertNotIn(sensitive_value, serialized)

    def test_nonempty_product_addons_outputs_only_safe_structure(self) -> None:
        report, _ = self._inspect([_product()])
        summary = report["product_addons_meta_summary"]
        serialized = json.dumps(report)

        self.assertEqual(
            summary,
            {
                "present": True,
                "value_type": "object",
                "is_empty": False,
                "item_count": 4,
                "review_required": True,
            },
        )
        self.assertNotIn(
            "_product_addons",
            {item["key"] for item in report["unknown_meta_keys"]},
        )
        for forbidden in (
            "must-not-be-reported-option",
            "must-not-be-reported-price",
            "private-addon.webp",
            "must-not-be-reported-html",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            set(summary),
            {"present", "value_type", "is_empty", "item_count", "review_required"},
        )

    def test_empty_product_addons_shapes_do_not_require_review(self) -> None:
        cases = (
            ([], "array", 0),
            ({}, "object", 0),
            ("", "string", None),
            (None, "null", None),
        )

        for value, value_type, item_count in cases:
            with self.subTest(value_type=value_type):
                product = _product()
                product["meta_data"] = [
                    {"key": "_product_addons", "value": value}
                ]
                report, _ = self._inspect([product])
                summary = report["product_addons_meta_summary"]

                self.assertTrue(summary["present"])
                self.assertEqual(summary["value_type"], value_type)
                self.assertTrue(summary["is_empty"])
                self.assertFalse(summary["review_required"])
                if item_count is None:
                    self.assertNotIn("item_count", summary)
                else:
                    self.assertEqual(summary["item_count"], item_count)

    def test_yith_global_inheritance_states(self) -> None:
        enabled_report, _ = self._inspect([_product()])
        disabled_product = copy.deepcopy(_product())
        disabled_product["meta_data"][0]["value"] = "1"
        disabled_report, _ = self._inspect([disabled_product])
        unknown_product = copy.deepcopy(_product())
        unknown_product["categories"] = [
            {"id": 99, "name": "Other", "slug": "other"}
        ]
        unknown_report, _ = self._inspect([unknown_product])

        self.assertFalse(enabled_report["global_addons_excluded"])
        self.assertEqual(
            enabled_report["yith_global_options_inheritance"], "likely_enabled"
        )
        self.assertTrue(disabled_report["global_addons_excluded"])
        self.assertEqual(
            disabled_report["yith_global_options_inheritance"], "likely_disabled"
        )
        self.assertEqual(
            unknown_report["yith_global_options_inheritance"], "unable_to_confirm"
        )

    def test_report_writer_contains_no_credentials_and_forces_zero_writes(self) -> None:
        report, _ = self._inspect([_product()])
        settings = load_config(SAFE_CONFIG)
        redactor = redactor_for_settings(settings)
        wordpress_token = base64.b64encode(
            b"fake-wordpress-user:fake-wordpress-password"
        ).decode("ascii")
        woocommerce_token = base64.b64encode(
            b"fake-woocommerce-key:fake-woocommerce-secret"
        ).decode("ascii")

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / f"reference-product-{SKU}.json"
            ReferenceProductReportWriter(path, redactor).write(report)
            saved_text = path.read_text(encoding="utf-8")
            saved = json.loads(saved_text)

        for forbidden in (
            "fake-wordpress-password",
            "fake-woocommerce-key",
            "fake-woocommerce-secret",
            wordpress_token,
            woocommerce_token,
            "Authorization",
            "Cookie",
            "sensitive-meta-value",
            "sandbox.wpcomstaging.com",
        ):
            self.assertNotIn(forbidden, saved_text)
        self.assertEqual(saved["write_requests_performed"], 0)

    def test_request_exception_is_sanitized_in_inspection_report(self) -> None:
        settings = load_config(SAFE_CONFIG)
        redactor = redactor_for_settings(settings)
        transport = FailingProductMockTransport()
        client = ReadOnlyHttpClient(
            settings.wp_base_url,
            redactor=redactor,
            transport=transport,
            retries=RetryConfig(max_retries=0),
        )

        report = ReferenceProductInspector(
            settings, client, redactor=redactor
        ).run(SKU)
        serialized = json.dumps(report)

        self.assertEqual(report["status"], "request_failed")
        self.assertEqual(transport.calls, 1)
        self.assertNotIn("fake-woocommerce-key", serialized)
        self.assertNotIn("fake-woocommerce-secret", serialized)
        self.assertNotIn("fake-cookie", serialized)
        self.assertNotIn("Authorization: Basic", serialized)
        self.assertNotIn("sandbox.wpcomstaging.com", serialized)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_inspector_safety_failure_stops_before_mock_transport(self) -> None:
        settings = Settings(
            wp_base_url="https://sandbox.wpcomstaging.com",
            sync_environment="production",
            dry_run=True,
            default_product_status="draft",
            allow_delete=False,
        )
        transport = ProductMockTransport([_product()])
        client = ReadOnlyHttpClient(settings.wp_base_url, transport=transport)

        with self.assertRaisesRegex(ConfigError, "SYNC_ENVIRONMENT"):
            ReferenceProductInspector(settings, client).run(SKU)

        self.assertEqual(transport.calls, [])
        self.assertEqual(client.counters.write_requests_performed, 0)

    def test_invalid_sku_is_rejected_before_mock_transport(self) -> None:
        settings = load_config(SAFE_CONFIG)
        transport = ProductMockTransport([_product()])
        client = ReadOnlyHttpClient(settings.wp_base_url, transport=transport)

        with self.assertRaisesRegex(ValueError, "SKU"):
            ReferenceProductInspector(settings, client).run("../unsafe")

        self.assertEqual(transport.calls, [])
        self.assertEqual(client.counters.write_requests_performed, 0)

    def test_cli_parser_accepts_inspect_product_without_running_it(self) -> None:
        arguments = build_parser().parse_args(["inspect-product", "--sku", SKU])

        self.assertEqual(arguments.command, "inspect-product")
        self.assertEqual(arguments.sku, SKU)


if __name__ == "__main__":
    unittest.main()
