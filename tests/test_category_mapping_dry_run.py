from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import category_mapping  # noqa: E402
from sync_worker.category_mapping import (  # noqa: E402
    CATEGORY_REGISTRY_VERSION,
    CategoryRegistry,
)
from sync_worker.category_mapping_dry_run import (  # noqa: E402
    REPORT_FILENAME,
    build_category_mapping_dry_run_report,
    restore_category_product_records,
    run_category_mapping_dry_run,
)
from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.clm_price_parser import CLMProductBlock  # noqa: E402
from sync_worker.product_model import from_clm_product  # noqa: E402
from sync_worker.product_size_enrichment_dry_run import (  # noqa: E402
    _restore_clm_product,
)


def product_item(
    model: str,
    *,
    series: str = "ultra",
    start_row: int = 480,
    fob: int = 2250,
) -> dict[str, object]:
    return {
        "series": series,
        "raw_series_title": f"{series} Series",
        "model": model,
        "specifications": {"height_model": model},
        "pricing": {
            "fob_unit_price": {
                "raw_value": f"RMB{fob}",
                "currency": "RMB",
                "amount": fob,
                "context": "fob_unit_price",
            },
            "minimum_retail_price": None,
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


def product_report() -> dict[str, object]:
    return {
        "status": "ok",
        "products": [
            product_item("Classic-Model", series="classic", start_row=10),
            product_item("FD160cm-Meru", series="pro", start_row=30),
            product_item("ULW-Model", series="ulw", start_row=50),
            product_item("SiQ157cm-Miko", series="ultra", start_row=70),
        ],
    }


def restored_products():
    return restore_category_product_records(product_report())


def built(products=None, *, input_file: str = "mock-products.json"):
    return build_category_mapping_dry_run_report(
        products if products is not None else restored_products(),
        input_file=input_file,
    )


class CategoryMappingDryRunTests(unittest.TestCase):
    def test_01_cli_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            ["map-categories-dry-run", "--products", "products.json"]
        )
        self.assertEqual(arguments.command, "map-categories-dry-run")
        self.assertEqual(arguments.product_input_path, Path("products.json"))

    def test_02_products_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["map-categories-dry-run"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_runner_reads_local_product_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(product_report()), encoding="utf-8")
            report, _ = run_category_mapping_dry_run(input_path, project_root=root)
        self.assertEqual(report["summary"]["total_products"], 4)

    def test_04_restore_builds_clm_product_blocks(self) -> None:
        captured: list[CLMProductBlock] = []

        def converter(block: CLMProductBlock):
            captured.append(block)
            return from_clm_product(block)

        with patch(
            "sync_worker.product_size_enrichment_dry_run.from_clm_product",
            side_effect=converter,
        ):
            products = restored_products()
        self.assertEqual(len(products), 4)
        self.assertTrue(all(isinstance(block, CLMProductBlock) for block in captured))

    def test_05_restore_calls_from_clm_product(self) -> None:
        with patch(
            "sync_worker.product_size_enrichment_dry_run.from_clm_product",
            wraps=from_clm_product,
        ) as converter:
            restored_products()
        self.assertEqual(converter.call_count, 4)

    def test_06_restore_uses_existing_clm_restorer(self) -> None:
        with patch(
            "sync_worker.product_size_enrichment_dry_run._restore_clm_product",
            wraps=_restore_clm_product,
        ) as restorer:
            restored_products()
        self.assertEqual(restorer.call_count, 4)

    def test_07_existing_category_registry_is_constructed(self) -> None:
        with patch(
            "sync_worker.category_mapping_dry_run.category_mapping.CategoryRegistry",
            side_effect=CategoryRegistry,
        ) as factory:
            built()
        factory.assert_called_once_with()

    def test_08_existing_map_categories_is_called(self) -> None:
        products = restored_products()
        with patch(
            "sync_worker.category_mapping_dry_run.category_mapping.map_categories",
            wraps=category_mapping.map_categories,
        ) as mapper:
            build_category_mapping_dry_run_report(products, input_file="fixture.json")
        mapper.assert_called_once()
        self.assertIs(mapper.call_args.args[0], products)

    def test_09_classic_maps_to_clm_classic(self) -> None:
        self.assertEqual(built()["results"][0]["category"]["category_key"], "clm-classic")

    def test_10_pro_maps_to_clm_pro(self) -> None:
        self.assertEqual(built()["results"][1]["category"]["category_key"], "clm-pro")

    def test_11_ulw_maps_to_clm_ulw(self) -> None:
        self.assertEqual(built()["results"][2]["category"]["category_key"], "clm-ulw")

    def test_12_ultra_maps_to_clm_ultra(self) -> None:
        self.assertEqual(built()["results"][3]["category"]["category_key"], "clm-ultra")

    def test_13_registry_version_is_reported(self) -> None:
        report = built()
        self.assertEqual(report["registry"]["version"], CATEGORY_REGISTRY_VERSION)
        self.assertTrue(
            all(
                result["category"]["registry_version"] == CATEGORY_REGISTRY_VERSION
                for result in report["results"]
            )
        )

    def test_14_woo_ids_are_all_null(self) -> None:
        self.assertTrue(
            all(result["category"]["woo_category_id"] is None for result in built()["results"])
        )

    def test_15_woo_binding_is_disabled(self) -> None:
        self.assertIs(built()["registry"]["woo_binding_enabled"], False)

    def test_16_mapped_internal_count(self) -> None:
        self.assertEqual(built()["summary"]["mapped_internal"], 4)

    def test_17_mapped_woo_is_zero(self) -> None:
        self.assertEqual(built()["summary"]["mapped_woo"], 0)

    def test_18_unbound_woo_count(self) -> None:
        self.assertEqual(built()["summary"]["unbound_woo_category"], 4)

    def test_19_missing_series_is_blocking(self) -> None:
        value = restored_products()[0]
        missing = replace(value, identity=replace(value.identity, series=None))  # type: ignore[arg-type]
        report = built([missing])
        self.assertEqual(report["summary"]["missing_series"], 1)
        self.assertEqual(report["results"][0]["category"]["status"], "missing_series")
        self.assertEqual(report["results"][0]["blocking_issues"], ["missing_series"])

    def test_20_unsupported_series_is_blocking(self) -> None:
        value = restored_products()[0]
        unsupported = replace(value, identity=replace(value.identity, series="fake-series"))
        report = built([unsupported])
        self.assertEqual(report["summary"]["unsupported_series"], 1)
        self.assertEqual(report["results"][0]["category"]["status"], "unsupported_series")
        self.assertEqual(report["results"][0]["blocking_issues"], ["unsupported_series"])

    def test_21_blocking_product_count(self) -> None:
        values = restored_products()
        missing = replace(values[0], identity=replace(values[0].identity, series=None))  # type: ignore[arg-type]
        unsupported = replace(values[1], identity=replace(values[1].identity, series="future"))
        self.assertEqual(built([missing, unsupported])["summary"]["blocking_products"], 2)

    def test_22_unbound_is_not_a_blocking_error(self) -> None:
        report = built()
        self.assertEqual(report["summary"]["blocking_products"], 0)
        self.assertTrue(all(result["blocking_issues"] == [] for result in report["results"]))

    def test_23_unsupported_does_not_fallback_to_other(self) -> None:
        value = restored_products()[0]
        unsupported = replace(value, identity=replace(value.identity, series="future"))
        result = built([unsupported])["results"][0]
        self.assertIsNone(result["category"]["category_key"])
        self.assertNotIn("other", json.dumps(result).casefold())

    def test_24_product_name_does_not_guess_category(self) -> None:
        value = restored_products()[3]
        missing = replace(value, identity=replace(value.identity, series=None))  # type: ignore[arg-type]
        result = built([missing])["results"][0]
        self.assertEqual(result["product_identity"], "SiQ157cm-Miko")
        self.assertEqual(result["category"]["status"], "missing_series")

    def test_25_model_does_not_guess_category(self) -> None:
        value = restored_products()[1]
        unsupported = replace(value, identity=replace(value.identity, series="fake-series"))
        result = built([unsupported])["results"][0]
        self.assertEqual(result["product_identity"], "FD160cm-Meru")
        self.assertIsNone(result["category"]["category_key"])

    def test_26_product_identity_is_preserved(self) -> None:
        identities = [result["product_identity"] for result in built()["results"]]
        self.assertEqual(
            identities,
            ["Classic-Model", "FD160cm-Meru", "ULW-Model", "SiQ157cm-Miko"],
        )

    def test_27_source_trace_is_preserved(self) -> None:
        self.assertEqual(
            [result["source"] for result in built()["results"]],
            [
                {"start_row": 10, "end_row": 20},
                {"start_row": 30, "end_row": 40},
                {"start_row": 50, "end_row": 60},
                {"start_row": 70, "end_row": 80},
            ],
        )

    def test_28_report_is_deterministic(self) -> None:
        products = restored_products()
        self.assertEqual(built(products), built(products))

    def test_29_result_order_is_stable(self) -> None:
        products = list(reversed(restored_products()))
        identities = [result["product_identity"] for result in built(products)["results"]]
        self.assertEqual(
            identities,
            ["SiQ157cm-Miko", "ULW-Model", "FD160cm-Meru", "Classic-Model"],
        )

    def test_30_report_does_not_include_supplier_cost_or_pricing(self) -> None:
        serialized = json.dumps(built(), ensure_ascii=False).casefold()
        for forbidden in ("supplier_cost", "fob", "fx", "option_pricing", "rmb2250"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_31_report_does_not_include_credentials(self) -> None:
        serialized = json.dumps(built(), ensure_ascii=False)
        for forbidden in (
            "consumer_key",
            "consumer_secret",
            "Authorization",
            "Cookie",
            "WP_APP_PASSWORD",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_32_sensitive_identity_is_not_reported(self) -> None:
        value = restored_products()[0]
        unsafe_identity = replace(
            value,
            identity=replace(
                value.identity,
                model="Authorization: Basic secret-value",
                raw_model="Cookie=session-secret",
            ),
            specifications=replace(value.specifications, normalized={}),
        )
        result = built([unsafe_identity])["results"][0]
        self.assertIsNone(result["product_identity"])
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn("session-secret", json.dumps(result))

    def test_33_sensitive_input_filename_is_redacted(self) -> None:
        report = built(input_file="ck_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456.json")
        self.assertNotIn("ck_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", json.dumps(report))

    def test_34_url_input_reference_is_redacted(self) -> None:
        report = built(input_file="https://supplier.example/private/report.json")
        serialized = json.dumps(report)
        self.assertNotIn("supplier.example", serialized)
        self.assertNotIn("https://", serialized)

    def test_35_network_request_count_is_zero(self) -> None:
        self.assertEqual(built()["network_requests_performed"], 0)

    def test_36_external_write_count_is_zero(self) -> None:
        self.assertEqual(built()["write_requests_performed"], 0)

    def test_37_runner_opens_no_network_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(product_report()), encoding="utf-8")
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                report, _ = run_category_mapping_dry_run(input_path, project_root=root)
        self.assertEqual(report["network_requests_performed"], 0)

    def test_38_runner_writes_only_expected_local_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(product_report()), encoding="utf-8")
            report, output_path = run_category_mapping_dry_run(input_path, project_root=root)
            output_files = sorted(path.name for path in (root / "reports").iterdir())
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(output_path.name, REPORT_FILENAME)
        self.assertEqual(output_files, [REPORT_FILENAME])

    def test_39_input_file_reference_is_local_and_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "mock-products.json"
            input_path.write_text(json.dumps(product_report()), encoding="utf-8")
            report, _ = run_category_mapping_dry_run(input_path, project_root=root)
        self.assertEqual(report["input_file"], "mock-products.json")

    def test_40_cli_calls_local_runner(self) -> None:
        mock_report = {
            "status": "ok",
            "summary": {},
            "network_requests_performed": 0,
            "write_requests_performed": 0,
        }
        with patch(
            "sync_worker.cli.run_category_mapping_dry_run",
            return_value=(mock_report, Path(REPORT_FILENAME)),
        ) as runner:
            status = main(
                ["map-categories-dry-run", "--products", "mock-products.json"]
            )
        self.assertEqual(status, 0)
        runner.assert_called_once()

    def test_41_summary_has_all_required_fields(self) -> None:
        self.assertEqual(
            set(built()["summary"]),
            {
                "total_products",
                "mapped_internal",
                "mapped_woo",
                "missing_series",
                "unsupported_series",
                "unbound_woo_category",
                "blocking_products",
            },
        )

    def test_42_top_level_report_structure(self) -> None:
        report = built()
        self.assertEqual(
            set(report),
            {
                "status",
                "input_file",
                "registry",
                "summary",
                "network_requests_performed",
                "write_requests_performed",
                "results",
            },
        )
        self.assertEqual(report["status"], "ok")

    def test_43_local_null_series_reaches_registry_as_missing(self) -> None:
        fixture = product_report()
        fixture["products"][0]["series"] = None
        products = restore_category_product_records(fixture)
        report = built(products)
        self.assertEqual(report["summary"]["missing_series"], 1)
        self.assertEqual(report["results"][0]["category"]["status"], "missing_series")

    def test_44_local_blank_series_reaches_registry_as_missing(self) -> None:
        fixture = product_report()
        fixture["products"][0]["series"] = "  "
        products = restore_category_product_records(fixture)
        self.assertEqual(built(products)["summary"]["missing_series"], 1)


if __name__ == "__main__":
    unittest.main()
