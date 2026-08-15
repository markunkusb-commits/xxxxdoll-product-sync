from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.product_model import (  # noqa: E402
    ProductRecord,
    from_clm_product,
)
from sync_worker.product_size_enricher import (  # noqa: E402
    enrich_products_with_sizes,
)
from sync_worker.product_size_enrichment_dry_run import (  # noqa: E402
    ProductSizeEnrichmentInputError,
    build_product_size_enrichment_report,
    load_local_json_report,
    restore_product_records,
    restore_size_records,
    run_product_size_enrichment_dry_run,
)
from sync_worker.size_list_parser import SizeRecord, TwoDimensionalValue  # noqa: E402


TEST_KEY = "ck_" + "a" * 24


def price(amount: int, currency: str, context: str) -> dict[str, object]:
    return {
        "raw_value": f"{currency}{amount}",
        "currency": currency,
        "amount": amount,
        "context": context,
    }


def product_item(
    model: str | None,
    *,
    specifications: dict[str, str] | None = None,
    fob: int | None = None,
    retail: int | None = None,
    start_row: int = 10,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "series": "pro",
        "raw_series_title": "CLM Pro",
        "model": model,
        "specifications": dict(specifications or {}),
        "pricing": {
            "fob_unit_price": (
                price(fob, "RMB", "fob_unit_price") if fob is not None else None
            ),
            "minimum_retail_price": (
                price(retail, "USD", "minimum_retail_price")
                if retail is not None
                else None
            ),
            "normal_options_price": None,
            "body_only_price": None,
            "including_head_price": None,
        },
        "included_features": [],
        "upgrade_options": [],
        "notices": [],
        "source": {"start_row": start_row, "end_row": start_row + 5},
        "warnings": list(warnings or []),
        "photo_download_link": "https://supplier.example/private-image.jpg",
    }


def scalar_measurement(value: int, unit: str = "cm") -> dict[str, object]:
    return {
        "metric": {"value": value, "unit": unit},
        "imperial": None,
        "raw_value": f"{value}{unit}",
    }


def size_item(
    body_type: str,
    *,
    row: int,
    measurements: dict[str, object] | None = None,
    fob: int | None = None,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "body_type": body_type,
        "raw_body_type": body_type,
        "type": "Full Silicone",
        "raw_type": "Full Silicone",
        "supplier_costs": {
            "fob_price": (
                {
                    "amount": fob,
                    "currency": "RMB",
                    "raw_value": f"RMB{fob}",
                }
                if fob is not None
                else None
            )
        },
        "measurements": dict(measurements or {}),
        "raw_measurements": [],
        "source": {
            "row": row,
            "coordinates": {"body_type": f"B{row}"},
            "type_merged_range": None,
        },
        "warnings": list(warnings or []),
    }


def fixture_reports() -> tuple[dict[str, object], dict[str, object]]:
    product_report = {
        "status": "ok",
        "products": [
            product_item(
                "FD140cm",
                specifications={"waist": "58cm"},
                fob=2250,
                retail=850,
                start_row=10,
                warnings=[
                    "https://supplier.example/private?token=hidden "
                    f"{TEST_KEY} Authorization: Bearer hidden Cookie=session"
                ],
            ),
            product_item("SiQ157cm-Miko", start_row=20),
            product_item("NoMatch", start_row=30),
            product_item("DUP", start_row=40),
        ],
    }
    size_report = {
        "status": "ok",
        "records": [
            size_item(
                "FD140cm",
                row=2,
                measurements={"waist": scalar_measurement(60)},
                fob=2200,
            ),
            size_item("SiQ157cm", row=3),
            size_item("DUP", row=4),
            size_item("DUP", row=5),
        ],
    }
    return product_report, size_report


def restored_fixture() -> tuple[list[ProductRecord], list[SizeRecord]]:
    product_report, size_report = fixture_reports()
    return restore_product_records(product_report), restore_size_records(size_report)


def built_report() -> dict[str, object]:
    products, sizes = restored_fixture()
    return build_product_size_enrichment_report(
        products,
        sizes,
        product_input_file="mock-products.json",
        size_input_file="mock-sizes.json",
    )


class ProductSizeEnrichmentDryRunTests(unittest.TestCase):
    def test_01_cli_registers_enrich_product_size(self) -> None:
        arguments = build_parser().parse_args(
            [
                "enrich-product-size",
                "--products",
                "products.json",
                "--sizes",
                "sizes.json",
            ]
        )
        self.assertEqual(arguments.command, "enrich-product-size")
        self.assertEqual(arguments.product_input_path, Path("products.json"))
        self.assertEqual(arguments.size_input_path, Path("sizes.json"))

    def test_02_cli_requires_products(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["enrich-product-size", "--sizes", "sizes.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_03_cli_requires_sizes(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["enrich-product-size", "--products", "products.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_04_product_json_is_read_locally(self) -> None:
        product_report, _ = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock-products.json"
            path.write_text(json.dumps(product_report), encoding="utf-8")
            restored = restore_product_records(load_local_json_report(path))
        self.assertEqual(len(restored), 4)
        self.assertEqual(restored[0].identity.model, "FD140cm")

    def test_05_size_json_is_read_locally(self) -> None:
        _, size_report = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock-sizes.json"
            path.write_text(json.dumps(size_report), encoding="utf-8")
            restored = restore_size_records(load_local_json_report(path))
        self.assertEqual(len(restored), 4)
        self.assertEqual(restored[0].identity.body_type, "FD140cm")

    def test_06_product_restore_reuses_from_clm_product(self) -> None:
        product_report, _ = fixture_reports()
        with patch(
            "sync_worker.product_size_enrichment_dry_run.from_clm_product",
            wraps=from_clm_product,
        ) as converter:
            products = restore_product_records(product_report)
        self.assertEqual(len(products), 4)
        self.assertEqual(converter.call_count, 4)

    def test_07_report_reuses_existing_product_size_enricher(self) -> None:
        products, sizes = restored_fixture()
        with patch(
            "sync_worker.product_size_enrichment_dry_run.enrich_products_with_sizes",
            wraps=enrich_products_with_sizes,
        ) as enricher:
            build_product_size_enrichment_report(
                products,
                sizes,
                product_input_file="products.json",
                size_input_file="sizes.json",
            )
        enricher.assert_called_once_with(products, sizes)

    def test_08_matched_summary(self) -> None:
        self.assertEqual(built_report()["summary"]["matched"], 2)

    def test_09_unmatched_summary(self) -> None:
        self.assertEqual(built_report()["summary"]["unmatched"], 1)

    def test_10_ambiguous_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["ambiguous"], 1)
        self.assertEqual(report["results"][3]["match_status"], "ambiguous")

    def test_11_exact_match_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["exact_matches"], 1)
        self.assertEqual(report["results"][0]["match_method"], "exact")

    def test_12_suffix_match_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["suffix_matches"], 1)
        self.assertEqual(
            report["results"][1]["match_method"],
            "verified_suffix_match",
        )

    def test_13_specification_conflict_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["specification_conflicts"], 1)
        self.assertEqual(
            report["results"][0]["specification_conflicts"],
            [
                {
                    "field": "waist",
                    "product_raw_value": "58cm",
                    "size_raw_value": "60cm",
                    "resolution": "unresolved",
                    "comparison_reason": "metric_value_differs",
                }
            ],
        )

    def test_14_supplier_cost_conflict_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["supplier_cost_conflicts"], 1)
        self.assertEqual(
            report["results"][0]["supplier_cost_conflict"],
            {"present": True, "resolution": "unresolved"},
        )

    def test_15_size_fob_never_enters_retail_pricing(self) -> None:
        result = built_report()["results"][0]
        self.assertEqual(
            result["retail_pricing"]["minimum_retail_price"],
            {"amount": 850, "currency": "USD"},
        )
        self.assertEqual(
            result["supplier_costs"]["size_list_fob"],
            {"amount": 2200, "currency": "RMB"},
        )
        serialized = json.dumps(result).casefold()
        for forbidden in ("regular_price", "sale_price", "customer_price"):
            self.assertNotIn(forbidden, serialized)

    def test_16_urls_credentials_and_photo_links_do_not_enter_report(self) -> None:
        serialized = json.dumps(built_report(), ensure_ascii=False)
        self.assertNotIn("supplier.example", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn(TEST_KEY, serialized)
        self.assertNotIn("Bearer hidden", serialized)
        self.assertNotIn("private-image.jpg", serialized)
        self.assertNotIn("photo_download_link", serialized)

    def test_17_network_request_counter_is_zero(self) -> None:
        self.assertEqual(built_report()["network_requests_performed"], 0)

    def test_18_write_request_counter_is_zero(self) -> None:
        self.assertEqual(built_report()["write_requests_performed"], 0)

    def test_19_result_contains_safe_identity_match_and_source_fields(self) -> None:
        first = built_report()["results"][0]
        self.assertEqual(first["product_series"], "pro")
        self.assertEqual(first["product_identity"]["model"], "FD140cm")
        self.assertEqual(first["matched_body_type"], "FD140cm")
        self.assertEqual(first["candidate_keys"], ["FD140cm"])
        self.assertEqual(first["source_rows"]["product"], {"start_row": 10, "end_row": 15})
        self.assertEqual(first["source_rows"]["size"], 2)

    def test_20_cli_uses_no_config_google_or_network(self) -> None:
        product_report, size_report = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_path = root / "mock-products.json"
            size_path = root / "mock-sizes.json"
            product_path.write_text(json.dumps(product_report), encoding="utf-8")
            size_path.write_text(json.dumps(size_report), encoding="utf-8")
            with (
                patch.object(socket, "socket", side_effect=AssertionError("network")),
                patch(
                    "sync_worker.cli.load_config",
                    side_effect=AssertionError(".env configuration"),
                ) as wp_config,
                patch(
                    "sync_worker.cli.load_google_config",
                    side_effect=AssertionError("Google configuration"),
                ) as google_config,
                patch(
                    "sync_worker.cli.OfficialGoogleClientFactory",
                    side_effect=AssertionError("Google client"),
                ) as google_factory,
                patch("sync_worker.cli.PROJECT_ROOT", root),
            ):
                exit_code = main(
                    [
                        "enrich-product-size",
                        "--products",
                        str(product_path),
                        "--sizes",
                        str(size_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()

    def test_21_run_writes_expected_sanitized_report_filename(self) -> None:
        product_report, size_report = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_path = root / "mock-products.json"
            size_path = root / "mock-sizes.json"
            product_path.write_text(json.dumps(product_report), encoding="utf-8")
            size_path.write_text(json.dumps(size_report), encoding="utf-8")
            report, report_path = run_product_size_enrichment_dry_run(
                product_path,
                size_path,
                project_root=root,
            )
            saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_path.name, "product-size-enrichment-dry-run.json")
        self.assertEqual(saved["summary"], report["summary"])
        self.assertEqual(saved["write_requests_performed"], 0)

    def test_22_two_dimensional_size_measurement_is_restored(self) -> None:
        _, size_report = fixture_reports()
        size_report["records"][0]["measurements"]["sole"] = {
            "metric": {"length": 7, "width": 2.5, "unit": "cm"},
            "imperial": {"length": 2.8, "width": 1, "unit": "in"},
            "raw_value": "7*2.5cm\n(2.8*1in)",
        }
        sole = restore_size_records(size_report)[0].measurements.sole
        self.assertIsInstance(sole.metric, TwoDimensionalValue)
        self.assertEqual((sole.metric.length, sole.metric.width), (7, 2.5))

    def test_23_invalid_local_report_shape_fails_safely(self) -> None:
        with self.assertRaisesRegex(ProductSizeEnrichmentInputError, "products"):
            restore_product_records({"products": "not-an-array"})

    def test_24_conflict_raw_values_are_sanitized_for_audit(self) -> None:
        product_report, size_report = fixture_reports()
        size_report["records"][0]["measurements"]["waist"]["raw_value"] = (
            "60cm https://supplier.example/private " + TEST_KEY
        )
        report = build_product_size_enrichment_report(
            restore_product_records(product_report),
            restore_size_records(size_report),
            product_input_file="mock-products.json",
            size_input_file="mock-sizes.json",
        )
        serialized = json.dumps(
            report["results"][0]["specification_conflicts"],
            ensure_ascii=False,
        )
        self.assertIn("product_raw_value", serialized)
        self.assertIn("size_raw_value", serialized)
        self.assertNotIn("supplier.example", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn(TEST_KEY, serialized)


if __name__ == "__main__":
    unittest.main()
