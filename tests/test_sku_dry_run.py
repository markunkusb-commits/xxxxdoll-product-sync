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
from sync_worker.product_model import from_clm_product  # noqa: E402
from sync_worker.product_size_enrichment_dry_run import (  # noqa: E402
    restore_product_records,
)
from sync_worker.sku_dry_run import (  # noqa: E402
    build_sku_dry_run_report,
    is_safe_sku,
    run_sku_dry_run,
)
from sync_worker.sku_policy import (  # noqa: E402
    SKU_POLICY_VERSION,
    generate_sku,
    validate_sku_uniqueness,
)


def product_item(
    model: str | None,
    *,
    series: str = "ultra",
    start_row: int = 480,
    height_model: str | None = None,
    fob: int = 2250,
    retail: int = 270,
) -> dict[str, object]:
    specifications = {"height": "157cm"}
    if height_model is not None:
        specifications["height_model"] = height_model
    return {
        "series": series,
        "raw_series_title": f"{series} Series",
        "model": model,
        "specifications": specifications,
        "pricing": {
            "fob_unit_price": {
                "raw_value": f"RMB{fob}",
                "currency": "RMB",
                "amount": fob,
                "context": "fob_unit_price",
            },
            "minimum_retail_price": {
                "raw_value": f"US${retail}",
                "currency": "USD",
                "amount": retail,
                "context": "minimum_retail_price",
            },
            "normal_options_price": None,
            "body_only_price": None,
            "including_head_price": None,
        },
        "included_features": [],
        "upgrade_options": [],
        "notices": [],
        "source": {"start_row": start_row, "end_row": start_row + 10},
        "warnings": [],
    }


def six_product_report() -> dict[str, object]:
    return {
        "status": "ok",
        "products": [
            product_item("FD160cm-Meru", series="pro", start_row=480),
            product_item("SiQ157cm-Miko", start_row=491),
            product_item("SiW160cm-Imani", start_row=502),
            product_item("SiR161-Vica", start_row=513),
            product_item("SiT163-Harriet", start_row=524),
            product_item("FD177-Zara", series="pro", start_row=535),
        ],
    }


def restored(report: dict[str, object] | None = None):
    return restore_product_records(report or six_product_report())


def built(report: dict[str, object] | None = None) -> dict[str, object]:
    return build_sku_dry_run_report(
        restored(report),
        input_file="mock-products.json",
    )


class SkuDryRunTests(unittest.TestCase):
    def test_01_cli_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            ["generate-sku-dry-run", "--products", "products.json"]
        )
        self.assertEqual(arguments.command, "generate-sku-dry-run")
        self.assertEqual(arguments.product_input_path, Path("products.json"))

    def test_02_products_input_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["generate-sku-dry-run"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_product_json_restores_product_records(self) -> None:
        products = restored()
        self.assertEqual(len(products), 6)
        self.assertEqual(products[0].identity.model, "FD160cm-Meru")

    def test_04_product_model_converter_is_called(self) -> None:
        with patch(
            "sync_worker.product_size_enrichment_dry_run.from_clm_product",
            wraps=from_clm_product,
        ) as converter:
            products = restored()
        self.assertEqual(len(products), 6)
        self.assertEqual(converter.call_count, 6)

    def test_05_generate_sku_is_called_directly_and_by_batch_validation(self) -> None:
        products = restored()
        with patch(
            "sync_worker.sku_dry_run.sku_policy.generate_sku",
            wraps=generate_sku,
        ) as generator:
            build_sku_dry_run_report(products, input_file="fixture.json")
        self.assertEqual(generator.call_count, 12)

    def test_06_policy_version_is_output(self) -> None:
        report = built()
        self.assertEqual(report["policy_version"], SKU_POLICY_VERSION)
        self.assertTrue(
            all(
                result["policy_version"] == SKU_POLICY_VERSION
                for result in report["results"]
            )
        )

    def test_07_fd160_meru_sku(self) -> None:
        self.assertEqual(built()["results"][0]["sku"], "CLM-PRO-FD160CM-MERU")

    def test_08_siq157_miko_sku(self) -> None:
        self.assertEqual(built()["results"][1]["sku"], "CLM-ULTRA-SIQ157CM-MIKO")

    def test_09_siw160_imani_sku(self) -> None:
        self.assertEqual(built()["results"][2]["sku"], "CLM-ULTRA-SIW160CM-IMANI")

    def test_10_sir161_vica_sku(self) -> None:
        self.assertEqual(built()["results"][3]["sku"], "CLM-ULTRA-SIR161-VICA")

    def test_11_sit163_harriet_sku(self) -> None:
        self.assertEqual(built()["results"][4]["sku"], "CLM-ULTRA-SIT163-HARRIET")

    def test_12_fd177_zara_sku(self) -> None:
        self.assertEqual(built()["results"][5]["sku"], "CLM-PRO-FD177-ZARA")

    def test_13_missing_identity_summary(self) -> None:
        report = built({"products": [product_item(None)]})
        self.assertEqual(report["summary"]["missing_identity"], 1)
        self.assertEqual(report["results"][0]["status"], "missing_identity")

    def test_14_unsupported_series_summary(self) -> None:
        report = built({"products": [product_item("MODEL", series="future")]})
        self.assertEqual(report["summary"]["unsupported_series"], 1)

    def test_15_collision_validation_is_called(self) -> None:
        products = restored()
        with patch(
            "sync_worker.sku_dry_run.sku_policy.validate_sku_uniqueness",
            wraps=validate_sku_uniqueness,
        ) as validator:
            build_sku_dry_run_report(products, input_file="fixture.json")
        validator.assert_called_once_with(products)

    def test_16_collision_is_not_automatically_repaired(self) -> None:
        report = built(
            {
                "products": [
                    product_item("A/B", start_row=10),
                    product_item("A_B", start_row=30),
                ]
            }
        )
        self.assertEqual(report["summary"]["collision_count"], 1)
        self.assertEqual(
            [result["sku"] for result in report["results"]],
            ["CLM-ULTRA-A-B", "CLM-ULTRA-A-B"],
        )
        self.assertTrue(
            all("sku_collision" in result["blocking_issues"] for result in report["results"])
        )

    def test_17_duplicate_input_is_not_collision(self) -> None:
        report = built(
            {
                "products": [
                    product_item("Same", start_row=10),
                    product_item("Same", start_row=30),
                ]
            }
        )
        self.assertEqual(report["summary"]["duplicate_input_count"], 1)
        self.assertEqual(report["summary"]["collision_count"], 0)
        self.assertTrue(
            all(result["status"] == "duplicate_input" for result in report["results"])
        )

    def test_18_source_row_is_absent_from_result(self) -> None:
        result = built()["results"][0]
        serialized = json.dumps(result)
        self.assertNotIn("start_row", serialized)
        self.assertNotIn("end_row", serialized)
        self.assertNotIn("480", result["sku"])

    def test_19_price_is_absent_from_sku(self) -> None:
        report = built({"products": [product_item("MODEL", fob=8765, retail=4321)]})
        sku = report["results"][0]["sku"]
        self.assertNotIn("8765", sku)
        self.assertNotIn("4321", sku)
        self.assertNotIn("PRICE", sku)

    def test_20_fob_is_absent_from_sku(self) -> None:
        sku = built()["results"][0]["sku"]
        self.assertNotIn("FOB", sku)
        self.assertNotIn("RMB", sku)

    def test_21_two_builds_are_deterministic(self) -> None:
        self.assertEqual(built(), built())

    def test_22_result_order_is_stable(self) -> None:
        models = [result["raw_identity"] for result in built()["results"]]
        self.assertEqual(
            models,
            [
                "FD160cm-Meru",
                "SiQ157cm-Miko",
                "SiW160cm-Imani",
                "SiR161-Vica",
                "SiT163-Harriet",
                "FD177-Zara",
            ],
        )

    def test_23_too_long_is_counted(self) -> None:
        report = built({"products": [product_item("A" * 100)]})
        self.assertEqual(report["summary"]["sku_too_long"], 1)
        self.assertIn("sku_too_long", report["results"][0]["blocking_issues"])

    def test_24_invalid_identity_is_counted(self) -> None:
        report = built({"products": [product_item("FOB RMB 2250")]})
        self.assertEqual(report["summary"]["invalid_identity"], 1)
        self.assertIsNone(report["results"][0]["sku"])

    def test_25_raw_identity_is_retained_for_audit(self) -> None:
        result = built()["results"][1]
        self.assertEqual(result["raw_identity"], "SiQ157cm-Miko")
        self.assertEqual(result["product_identity"], "SiQ157cm-Miko")
        self.assertEqual(result["audit"]["identity_source"], "model")

    def test_26_network_request_count_is_zero(self) -> None:
        self.assertEqual(built()["network_requests_performed"], 0)

    def test_27_external_write_count_is_zero(self) -> None:
        self.assertEqual(built()["write_requests_performed"], 0)

    def test_28_generated_summary_counts_six(self) -> None:
        summary = built()["summary"]
        self.assertEqual(summary["total_products"], 6)
        self.assertEqual(summary["generated_skus"], 6)

    def test_29_top_level_status_is_ok(self) -> None:
        self.assertEqual(built()["status"], "ok")

    def test_30_sku_alphabet_is_restricted(self) -> None:
        for result in built()["results"]:
            self.assertRegex(result["sku"], r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
            self.assertTrue(is_safe_sku(result["sku"]))

    def test_31_safety_scan_rejects_internal_tokens(self) -> None:
        for token in ("FOB", "RMB", "USD", "SUPPLIER", "COST", "PRICE", "ROW", "SOURCE", "TIMESTAMP", "UUID"):
            self.assertFalse(is_safe_sku(f"CLM-ULTRA-{token}"))

    def test_32_safety_scan_rejects_bad_alphabet(self) -> None:
        for value in ("CLM_ULTRA_MODEL", "CLM ULTRA MODEL", "CLM-ULTRA-模型", "CLM-ULTRA-"):
            self.assertFalse(is_safe_sku(value))

    def test_33_height_model_survives_product_restore(self) -> None:
        report = built(
            {
                "products": [
                    product_item(None, height_model="SiW160cm-Imani")
                ]
            }
        )
        self.assertEqual(report["results"][0]["sku"], "CLM-ULTRA-SIW160CM-IMANI")

    def test_34_runner_reads_only_explicit_local_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(six_product_report()), encoding="utf-8")
            report, output_path = run_sku_dry_run(input_path, project_root=root)
            output_files = sorted(path.name for path in (root / "reports").iterdir())
        self.assertEqual(report["summary"]["generated_skus"], 6)
        self.assertEqual(output_path.name, "sku-dry-run.json")
        self.assertEqual(output_files, ["sku-dry-run.json"])

    def test_35_runner_opens_no_network_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(six_product_report()), encoding="utf-8")
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                report, _ = run_sku_dry_run(input_path, project_root=root)
        self.assertEqual(report["network_requests_performed"], 0)

    def test_36_cli_calls_local_runner(self) -> None:
        mock_report = {
            "status": "ok",
            "summary": {},
            "network_requests_performed": 0,
            "write_requests_performed": 0,
        }
        with patch(
            "sync_worker.cli.run_sku_dry_run",
            return_value=(mock_report, Path("sku-dry-run.json")),
        ) as runner:
            status = main(
                ["generate-sku-dry-run", "--products", "mock-products.json"]
            )
        self.assertEqual(status, 0)
        runner.assert_called_once()

    def test_37_input_file_reference_is_local_and_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(six_product_report()), encoding="utf-8")
            report, _ = run_sku_dry_run(input_path, project_root=root)
        self.assertEqual(report["input_file"], "mock-products.json")

    def test_38_collision_records_conflicting_identities(self) -> None:
        report = built(
            {
                "products": [
                    product_item("A/B", start_row=10),
                    product_item("A_B", start_row=30),
                ]
            }
        )
        self.assertEqual(
            report["results"][0]["conflicting_product_identities"],
            ["A/B", "A_B"],
        )

    def test_39_report_does_not_include_source_or_prices(self) -> None:
        for result in built()["results"]:
            keys = set(result)
            self.assertFalse(
                keys.intersection(
                    {"source", "start_row", "end_row", "price", "fob"}
                )
            )


if __name__ == "__main__":
    unittest.main()
