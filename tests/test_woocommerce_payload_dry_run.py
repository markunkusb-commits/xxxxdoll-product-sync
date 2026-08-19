from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.product_model import from_clm_product  # noqa: E402
from sync_worker.product_size_enricher import (  # noqa: E402
    enrich_products_with_sizes,
)
from sync_worker.product_size_enrichment_dry_run import (  # noqa: E402
    restore_product_records,
    restore_size_records,
)
from sync_worker.woocommerce_payload_dry_run import (  # noqa: E402
    AMBIGUOUS_JOIN_ISSUE,
    UNSAFE_PUBLIC_ISSUE,
    build_woocommerce_payload_report,
    restore_presented_product_options,
    run_woocommerce_payload_dry_run,
    scan_public_surfaces,
)
from sync_worker.woocommerce_product_mapper import (  # noqa: E402
    WOO_CORE_PAYLOAD_ALLOWLIST,
    build_woocommerce_product_payload,
)


def price(amount: str | None, currency: str = "USD", context: str = "minimum_retail_price") -> dict[str, object] | None:
    if amount is None:
        return None
    return {
        "raw_value": f"{currency}{amount}",
        "currency": currency,
        "amount": float(amount),
        "context": context,
    }


def product_item(
    model: str | None = "SiQ157cm-Miko",
    *,
    start_row: int = 480,
    base: str | None = "270",
    currency: str = "USD",
    height_model: str | None = None,
    fob: str = "2250",
) -> dict[str, object]:
    specifications = {"height": "157cm", "waist": "60cm"}
    if height_model is not None:
        specifications["height_model"] = height_model
    return {
        "series": "ultra",
        "raw_series_title": "Ultra Series",
        "model": model,
        "specifications": specifications,
        "pricing": {
            "fob_unit_price": price(fob, "RMB", "fob_unit_price"),
            "minimum_retail_price": price(base, currency),
            "normal_options_price": None,
            "body_only_price": None,
            "including_head_price": None,
        },
        "included_features": ["articulated fingers"],
        "upgrade_options": [],
        "notices": [],
        "source": {"start_row": start_row, "end_row": start_row + 10},
        "warnings": [],
    }


def measurement(raw: str = "61cm") -> dict[str, object]:
    return {
        "metric": {"value": 61, "unit": "cm"},
        "imperial": None,
        "raw_value": raw,
    }


def size_item(body_type: str = "SiQ157cm", *, row: int = 2) -> dict[str, object]:
    return {
        "body_type": body_type,
        "raw_body_type": body_type,
        "type": "Full Silicone",
        "raw_type": "Full Silicone",
        "supplier_costs": {
            "fob_price": {
                "amount": 2200,
                "currency": "RMB",
                "raw_value": "RMB2200",
            }
        },
        "measurements": {"upper_chest": measurement()},
        "raw_measurements": [],
        "source": {
            "row": row,
            "coordinates": {"body_type": f"B{row}"},
            "type_merged_range": None,
        },
        "warnings": [],
    }


def component(name: str, coordinate: str) -> dict[str, object]:
    return {
        "option_name": name,
        "category": "product_extra_option",
        "supplier_cost": {
            "amount": "300",
            "currency": "RMB",
            "raw_price": "￥300",
        },
        "source_coordinate": coordinate,
    }


def presented_option(
    name: str,
    target: str,
    display: str,
    *,
    mapping_type: str = "alias",
) -> dict[str, object]:
    components = (
        [component("硅胶头植眉毛", "A3"), component("硅胶头植睫毛", "A4")]
        if mapping_type == "composite"
        else []
    )
    return {
        "product_upgrade_name": name,
        "product_raw_value": f"1. {name}",
        "mapping_type": mapping_type,
        "registry_version": "clm-option-map-v1",
        "supplier_cost": {
            "amount": "600" if mapping_type == "composite" else "500",
            "currency": "RMB",
            "raw_values": ["￥600" if mapping_type == "composite" else "￥500"],
            "source_provenance": {
                "coordinates": ["A3", "A4"] if mapping_type == "composite" else ["A2"]
            },
        },
        "economic_pricing": {
            "target_retail_usd": target,
            "cost_usd": "90.0000" if mapping_type == "composite" else "75.0000",
            "policy_version": "option-retail-v1",
        },
        "presentation": {
            "display_price_usd": display,
            "strategy": "nine_ending",
            "candidate_price": display,
            "uplift_amount": str(float(display) - float(target)),
            "uplift_rate": "0.0578",
            "fallback_used": False,
            "policy_version": "retail-presentation-v1",
            "status": "presented",
        },
        "catalog_mapping": {
            "mapping_type": mapping_type,
            "status": "composite" if mapping_type == "composite" else "alias",
            "registry_version": "clm-option-map-v1",
            "catalog_option_name": None if mapping_type == "composite" else name,
            "catalog_category": None if mapping_type == "composite" else "product_extra_option",
            "components": components,
            "combined_supplier_cost": None,
            "candidate_option_names": [],
            "missing_component_names": [],
            "source_coordinates": ["A3", "A4"] if mapping_type == "composite" else ["A2"],
        },
        "warnings": [],
    }


def four_options() -> list[dict[str, object]]:
    return [
        presented_option("Gel Butt", "112.50", "119.00"),
        presented_option("Hair Implant", "112.50", "119.00"),
        presented_option("Hard Hands and Feet", "90.00", "99.00"),
        presented_option(
            "Eyebrows/Eyelashes Implant",
            "135.00",
            "139.00",
            mapping_type="composite",
        ),
    ]


def presentation_product(
    model: str = "SiQ157cm-Miko",
    *,
    start_row: int = 480,
    options: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "series": "ultra",
        "product_identity": {
            "model": model,
            "raw_model": model,
            "raw_series_title": "Ultra Series",
        },
        "source_trace": {"start_row": start_row, "end_row": start_row + 10},
        "included_features": ["articulated fingers"],
        "retail_pricing": {},
        "presented_upgrade_options": list(options or []),
        "unpresented_upgrade_options": [],
        "warnings": [],
    }


def reports(
    *,
    products: list[dict[str, object]] | None = None,
    sizes: list[dict[str, object]] | None = None,
    presentations: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    return (
        {"status": "ok", "products": products or [product_item()]},
        {"status": "ok", "records": sizes if sizes is not None else [size_item()]},
        {
            "status": "ok",
            "results": presentations if presentations is not None else [presentation_product(options=four_options())],
        },
    )


def build(
    *,
    product_report: dict[str, object] | None = None,
    size_report: dict[str, object] | None = None,
    presentation_report: dict[str, object] | None = None,
) -> dict[str, object]:
    defaults = reports()
    return build_woocommerce_payload_report(
        restore_product_records(product_report or defaults[0]),
        restore_size_records(size_report or defaults[1]),
        restore_presented_product_options(presentation_report or defaults[2]),
        product_input_file="mock-products.json",
        size_input_file="mock-sizes.json",
        presented_option_input_file="mock-presented.json",
    )


def candidate(report: dict[str, object]) -> dict[str, object]:
    return report["candidates"][0]


class WooCommercePayloadDryRunTests(unittest.TestCase):
    def test_01_cli_is_registered(self) -> None:
        args = build_parser().parse_args([
            "build-woocommerce-payloads", "--products", "p.json",
            "--sizes", "s.json", "--presented-options", "o.json",
        ])
        self.assertEqual(args.command, "build-woocommerce-payloads")
        self.assertEqual(args.product_input_path, Path("p.json"))

    def test_02_products_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["build-woocommerce-payloads", "--sizes", "s.json", "--presented-options", "o.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_sizes_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["build-woocommerce-payloads", "--products", "p.json", "--presented-options", "o.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_04_presented_options_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["build-woocommerce-payloads", "--products", "p.json", "--sizes", "s.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_05_product_json_restores_product_record(self) -> None:
        restored = restore_product_records(reports()[0])
        self.assertEqual(restored[0].identity.model, "SiQ157cm-Miko")

    def test_06_size_json_restores_size_record(self) -> None:
        restored = restore_size_records(reports()[1])
        self.assertEqual(restored[0].identity.body_type, "SiQ157cm")

    def test_07_presentation_json_restores_domain_objects(self) -> None:
        restored = restore_presented_product_options(reports()[2])
        self.assertEqual(len(restored[0].options), 4)
        self.assertEqual(restored[0].options[0].presentation.presentation.display_price_usd, 119)

    def test_08_source_rows_drive_deterministic_join(self) -> None:
        p = [product_item("First", start_row=10), product_item("Second", start_row=30)]
        o = [presentation_product("Second", start_row=30, options=[presented_option("Second Option", "90", "99")]), presentation_product("First", start_row=10, options=[presented_option("First Option", "90", "99")])]
        report = build(product_report=reports(products=p)[0], size_report=reports(sizes=[])[1], presentation_report=reports(presentations=o)[2])
        self.assertEqual(report["candidates"][0]["storefront_options"][0]["name"], "First Option")

    def test_09_duplicate_source_join_is_blocked(self) -> None:
        duplicate = [presentation_product(options=four_options()), presentation_product(options=four_options())]
        report = build(presentation_report=reports(presentations=duplicate)[2])
        self.assertIn(AMBIGUOUS_JOIN_ISSUE, candidate(report)["blocking_issues"])

    def test_10_series_only_is_never_used(self) -> None:
        wrong = [presentation_product("Different Model", options=four_options())]
        report = build(presentation_report=reports(presentations=wrong)[2])
        self.assertIn(AMBIGUOUS_JOIN_ISSUE, candidate(report)["blocking_issues"])
        self.assertEqual(candidate(report)["storefront_options"], [])

    def test_11_unverified_array_order_is_not_used(self) -> None:
        p = [product_item("A", start_row=10), product_item("B", start_row=30)]
        o = [presentation_product("B", start_row=30), presentation_product("A", start_row=10)]
        report = build(product_report=reports(products=p)[0], size_report=reports(sizes=[])[1], presentation_report=reports(presentations=o)[2])
        self.assertNotIn(AMBIGUOUS_JOIN_ISSUE, report["candidates"][0]["blocking_issues"])

    def test_12_existing_product_model_converter_is_reused(self) -> None:
        with patch("sync_worker.product_size_enrichment_dry_run.from_clm_product", wraps=from_clm_product) as converter:
            restore_product_records(reports()[0])
        converter.assert_called_once()

    def test_13_existing_size_enricher_is_reused(self) -> None:
        products = restore_product_records(reports()[0])
        sizes = restore_size_records(reports()[1])
        presented = restore_presented_product_options(reports()[2])
        with patch("sync_worker.woocommerce_payload_dry_run.product_size_enricher.enrich_products_with_sizes", wraps=enrich_products_with_sizes) as enricher:
            build_woocommerce_payload_report(products, sizes, presented, product_input_file="p", size_input_file="s", presented_option_input_file="o")
        enricher.assert_called_once()

    def test_14_existing_woo_mapper_is_reused(self) -> None:
        products = restore_product_records(reports()[0])
        sizes = restore_size_records(reports()[1])
        presented = restore_presented_product_options(reports()[2])
        with patch("sync_worker.woocommerce_payload_dry_run.woocommerce_product_mapper.build_woocommerce_product_payload", wraps=build_woocommerce_product_payload) as mapper:
            build_woocommerce_payload_report(products, sizes, presented, product_input_file="p", size_input_file="s", presented_option_input_file="o")
        mapper.assert_called_once()

    def test_15_height_model_is_name_fallback(self) -> None:
        fixture = reports(products=[product_item(None, height_model="SiW160cm-Imani")])[0]
        self.assertEqual(candidate(build(product_report=fixture))["payload"]["name"], "SiW160cm-Imani")

    def test_16_missing_name_is_blocker(self) -> None:
        fixture = reports(products=[product_item(None)])[0]
        self.assertIn("missing_product_name", candidate(build(product_report=fixture))["blocking_issues"])

    def test_17_base_usd_retail_becomes_regular_price(self) -> None:
        self.assertEqual(candidate(build())["payload"]["regular_price"], "270.00")

    def test_18_missing_base_price_is_blocker(self) -> None:
        fixture = reports(products=[product_item(base=None)])[0]
        self.assertIn("missing_base_retail_price", candidate(build(product_report=fixture))["blocking_issues"])

    def test_19_non_usd_base_price_is_blocker(self) -> None:
        fixture = reports(products=[product_item(base="270", currency="RMB")])[0]
        self.assertIn("unsupported_base_price_currency", candidate(build(product_report=fixture))["blocking_issues"])

    def test_20_fob_never_becomes_regular_price(self) -> None:
        fixture = reports(products=[product_item(base="270", fob="9999")])[0]
        self.assertEqual(candidate(build(product_report=fixture))["payload"]["regular_price"], "270.00")

    def test_21_four_storefront_options_are_preserved(self) -> None:
        self.assertEqual(len(candidate(build())["storefront_options"]), 4)

    def test_22_display_119_is_used(self) -> None:
        prices = [item["price_usd"] for item in candidate(build())["storefront_options"]]
        self.assertEqual(prices.count("119.00"), 2)

    def test_23_display_99_is_used(self) -> None:
        self.assertIn("99.00", [item["price_usd"] for item in candidate(build())["storefront_options"]])

    def test_24_display_139_is_used(self) -> None:
        self.assertIn("139.00", [item["price_usd"] for item in candidate(build())["storefront_options"]])

    def test_25_economic_target_is_not_storefront_price(self) -> None:
        self.assertNotIn("112.50", [item["price_usd"] for item in candidate(build())["storefront_options"]])

    def test_26_composite_is_one_customer_option(self) -> None:
        names = [item["name"] for item in candidate(build())["storefront_options"]]
        self.assertEqual(names.count("Eyebrows/Eyelashes Implant"), 1)
        self.assertNotIn("硅胶头植眉毛", names)

    def test_27_matched_size_supplies_attribute(self) -> None:
        attrs = candidate(build())["payload"]["attributes"]
        upper_chest = next(item for item in attrs if item["name"] == "Upper Chest")
        self.assertEqual(upper_chest["options"], ["61cm"])

    def test_28_unmatched_size_uses_safe_product_fallback(self) -> None:
        report = build(size_report=reports(sizes=[])[1])
        attrs = candidate(report)["payload"]["attributes"]
        waist = next(item for item in attrs if item["name"] == "Waist")
        self.assertEqual(waist["options"], ["60cm"])
        self.assertIn("size_enrichment_unmatched", candidate(report)["warnings"])

    def test_29_attributes_are_not_variations(self) -> None:
        self.assertTrue(all(item["variation"] is False for item in candidate(build())["payload"]["attributes"]))

    def test_30_attributes_are_visible(self) -> None:
        self.assertTrue(all(item["visible"] is True for item in candidate(build())["payload"]["attributes"]))

    def test_31_status_is_draft(self) -> None:
        self.assertEqual(candidate(build())["payload"]["status"], "draft")

    def test_32_type_is_simple(self) -> None:
        self.assertEqual(candidate(build())["payload"]["type"], "simple")

    def test_33_sku_is_not_guessed(self) -> None:
        self.assertNotIn("sku", candidate(build())["payload"])

    def test_34_category_is_not_guessed(self) -> None:
        self.assertNotIn("categories", candidate(build())["payload"])

    def test_35_images_are_not_guessed(self) -> None:
        self.assertNotIn("images", candidate(build())["payload"])

    def test_36_description_is_not_guessed(self) -> None:
        self.assertNotIn("description", candidate(build())["payload"])

    def test_37_payload_keys_follow_mapper_allowlist(self) -> None:
        self.assertFalse(set(candidate(build())["payload"]) - WOO_CORE_PAYLOAD_ALLOWLIST)

    def test_38_supplier_cost_leak_is_detected(self) -> None:
        self.assertIn(UNSAFE_PUBLIC_ISSUE, scan_public_surfaces({"payload": {"name": "supplier_cost 500"}}))

    def test_39_fx_leak_is_detected(self) -> None:
        self.assertIn(UNSAFE_PUBLIC_ISSUE, scan_public_surfaces({"payload": {"name": "FX rate 0.1500"}}))

    def test_40_coordinate_leak_is_detected(self) -> None:
        self.assertIn(UNSAFE_PUBLIC_ISSUE, scan_public_surfaces({"payload": {"name": "A17"}}))

    def test_41_url_leak_is_detected(self) -> None:
        self.assertIn(UNSAFE_PUBLIC_ISSUE, scan_public_surfaces({"payload": {"name": "https://supplier.example/a"}}))

    def test_42_credentials_leak_is_detected(self) -> None:
        self.assertIn(UNSAFE_PUBLIC_ISSUE, scan_public_surfaces({"payload": {"name": "consumer secret hidden"}}))

    def test_43_audit_is_not_merged_into_payload(self) -> None:
        built = candidate(build())
        self.assertIn("internal_supplier_costs", built["audit"])
        self.assertNotIn("internal_supplier_costs", built["payload"])

    def test_44_audit_is_not_merged_into_storefront(self) -> None:
        storefront = json.dumps(candidate(build())["storefront_options"])
        self.assertNotIn("supplier_cost", storefront)
        self.assertNotIn("source_coordinate", storefront)

    def test_45_candidate_ready_for_write_is_always_false(self) -> None:
        self.assertIs(candidate(build())["ready_for_write"], False)

    def test_46_ready_for_write_summary_is_zero(self) -> None:
        self.assertEqual(build()["summary"]["ready_for_write_count"], 0)

    def test_47_network_request_counter_is_zero(self) -> None:
        self.assertEqual(build()["network_requests_performed"], 0)

    def test_48_write_request_counter_is_zero(self) -> None:
        self.assertEqual(build()["write_requests_performed"], 0)

    def test_49_summary_counts_candidates_and_options(self) -> None:
        summary = build()["summary"]
        self.assertEqual(summary["total_products"], 1)
        self.assertEqual(summary["candidates_built"], 1)
        self.assertEqual(summary["candidates_with_storefront_options"], 1)

    def test_50_summary_counts_size_states(self) -> None:
        self.assertEqual(build()["summary"]["size_matched"], 1)
        self.assertEqual(build(size_report=reports(sizes=[])[1])["summary"]["size_unmatched"], 1)

    def test_51_validation_is_reported(self) -> None:
        validation = candidate(build())["validation"]
        self.assertIn("valid", validation)
        self.assertIn("errors", validation)
        self.assertIn("warnings", validation)

    def test_52_local_runner_writes_only_the_expected_report(self) -> None:
        product_report, size_report, presentation_report = reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            paths = [root / name for name in ("p.json", "s.json", "o.json")]
            for path, value in zip(paths, (product_report, size_report, presentation_report), strict=True):
                path.write_text(json.dumps(value), encoding="utf-8")
            report, output = run_woocommerce_payload_dry_run(*paths, project_root=root)
            files = sorted(path.name for path in (root / "reports").iterdir())
        self.assertEqual(files, ["woocommerce-payload-dry-run.json"])
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(output.name, "woocommerce-payload-dry-run.json")

    def test_53_runner_does_not_open_network_socket(self) -> None:
        product_report, size_report, presentation_report = reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            paths = [root / name for name in ("p.json", "s.json", "o.json")]
            for path, value in zip(paths, (product_report, size_report, presentation_report), strict=True):
                path.write_text(json.dumps(value), encoding="utf-8")
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                report, _ = run_woocommerce_payload_dry_run(*paths, project_root=root)
        self.assertEqual(report["network_requests_performed"], 0)

    def test_54_cli_calls_local_runner(self) -> None:
        mock_report = {"status": "ok", "summary": {}, "network_requests_performed": 0, "write_requests_performed": 0}
        with patch("sync_worker.cli.run_woocommerce_payload_dry_run", return_value=(mock_report, Path("report.json"))) as runner:
            status = main(["build-woocommerce-payloads", "--products", "p.json", "--sizes", "s.json", "--presented-options", "o.json"])
        self.assertEqual(status, 0)
        runner.assert_called_once()

    def test_55_public_surfaces_do_not_contain_audit_fields(self) -> None:
        built = candidate(build())
        public = json.dumps({key: built[key] for key in ("payload", "storefront_options", "public_content")}, ensure_ascii=False)
        for forbidden in ("supplier_cost", "target_retail_usd", "source_coordinate", "￥", "RMB"):
            self.assertNotIn(forbidden, public)

    def test_56_input_reports_are_not_mutated(self) -> None:
        fixtures = reports()
        before = deepcopy(fixtures)
        build(product_report=fixtures[0], size_report=fixtures[1], presentation_report=fixtures[2])
        self.assertEqual(fixtures, before)


if __name__ == "__main__":
    unittest.main()
