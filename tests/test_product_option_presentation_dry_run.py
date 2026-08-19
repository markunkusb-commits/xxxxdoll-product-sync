from __future__ import annotations

import builtins
import copy
import json
import socket
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.product_option_presentation_dry_run import (  # noqa: E402
    EconomicProductOptionRecord,
    build_product_option_presentation_report,
    load_local_product_option_pricing_report,
    restore_economic_product_option_records,
)
from sync_worker.retail_price_presentation import (  # noqa: E402
    PresentationCalculation,
    PresentedRetailPrice,
    present_retail_price,
)


OPTION_POLICY_VERSION = "option-retail-v1"
PRESENTATION_POLICY_VERSION = "retail-presentation-v1"
REGISTRY_VERSION = "clm-option-map-v1"


def supplier_cost(
    amount: str = "500",
    *,
    coordinates: list[str] | None = None,
) -> dict[str, object]:
    return {
        "amount": amount,
        "currency": "RMB",
        "raw_values": [f"￥{amount}"],
        "source_provenance": {
            "coordinates": coordinates or ["A2"],
        },
    }


def catalog_mapping(
    *,
    mapping_type: str = "alias",
    components: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "mapping_type": mapping_type,
        "status": "composite" if mapping_type == "composite" else mapping_type,
        "registry_version": REGISTRY_VERSION,
        "catalog_option_name": None if mapping_type == "composite" else "目录选项",
        "catalog_category": (
            None if mapping_type == "composite" else "product_extra_option"
        ),
        "components": components or [],
        "combined_supplier_cost": (
            supplier_cost("600", coordinates=["A3", "A4"])
            if mapping_type == "composite"
            else None
        ),
        "candidate_option_names": [],
        "missing_component_names": [],
        "source_coordinates": ["A2"],
    }


def priced_option(
    name: str,
    target: str | None,
    *,
    cost_usd: str | None = "75.0000",
    supplier_amount: str = "500",
    mapping_type: str = "alias",
    mapping: dict[str, object] | None = None,
    raw_value: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "product_upgrade_name": name,
        "product_raw_value": raw_value or f"1. {name}",
        "mapping_type": mapping_type,
        "registry_version": REGISTRY_VERSION,
        "catalog_mapping": mapping or catalog_mapping(mapping_type=mapping_type),
        "supplier_cost": supplier_cost(supplier_amount),
        "pricing": {
            "status": "priced" if target is not None else "no_supplier_price",
            "fx_rate": "0.1500" if target is not None else None,
            "cost_usd": cost_usd,
            "markup_price_usd": None,
            "minimum_profit_price_usd": None,
            "target_retail_usd": target,
            "policy_version": OPTION_POLICY_VERSION,
        },
        "warnings": warnings or [],
    }


def composite_option(target: str = "135.00") -> dict[str, object]:
    components = [
        {
            "option_name": "硅胶头植眉毛",
            "category": "product_extra_option",
            "supplier_cost": {
                "amount": "300",
                "currency": "RMB",
                "raw_price": "￥300",
            },
            "source_coordinate": "A3",
        },
        {
            "option_name": "硅胶头植睫毛",
            "category": "product_extra_option",
            "supplier_cost": {
                "amount": "300",
                "currency": "RMB",
                "raw_price": "￥300",
            },
            "source_coordinate": "A4",
        },
    ]
    return priced_option(
        "Eyebrows/Eyelashes Implant",
        target,
        cost_usd="90.0000",
        supplier_amount="600",
        mapping_type="composite",
        mapping=catalog_mapping(mapping_type="composite", components=components),
        raw_value="4. Eyebrows/Eyelashes Implant",
    )


def product(
    *options: dict[str, object],
    model: str = "ULW-170",
) -> dict[str, object]:
    return {
        "series": "ultra",
        "product_identity": {
            "model": model,
            "raw_model": model,
            "raw_series_title": "Ultra Series",
        },
        "source_trace": {"start_row": 480, "end_row": 490},
        "included_features": ["articulated fingers"],
        "retail_pricing": {
            "minimum_retail_price": {
                "amount": "270",
                "currency": "USD",
                "raw_value": "US$270",
            }
        },
        "priced_upgrade_options": list(options),
        "unpriced_upgrade_options": [],
        "warnings": [],
    }


def payload(*products: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "results": list(products)}


def build_fixture(
    fixture: dict[str, object],
) -> tuple[list[EconomicProductOptionRecord], dict[str, object]]:
    restored = restore_economic_product_option_records(fixture)
    report = build_product_option_presentation_report(
        restored,
        input_file="fixture.json",
    )
    return restored, report


def first_presented(report: dict[str, object]) -> dict[str, object]:
    return report["results"][0]["presented_upgrade_options"][0]


class ProductOptionPresentationDryRunTests(unittest.TestCase):
    def test_01_cli_command_is_registered_without_fx(self) -> None:
        arguments = build_parser().parse_args(
            ["present-product-option-prices", "--input", "fixture.json"]
        )
        self.assertEqual(arguments.command, "present-product-option-prices")
        self.assertEqual(arguments.input_path, Path("fixture.json"))
        self.assertFalse(hasattr(arguments, "rmb_to_usd_rate"))

    def test_02_input_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["present-product-option-prices"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_local_json_report_is_loaded(self) -> None:
        fixture = payload(product(priced_option("Gel Butt", "112.50")))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            loaded = load_local_product_option_pricing_report(path)
        self.assertEqual(loaded, fixture)

    def test_04_economic_pricing_records_are_restored(self) -> None:
        restored = restore_economic_product_option_records(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        self.assertEqual(len(restored), 1)
        self.assertIsInstance(restored[0], EconomicProductOptionRecord)
        option = restored[0].priced_upgrade_options[0]
        self.assertEqual(option.economic_pricing.target_retail_usd, Decimal("112.50"))
        self.assertEqual(option.economic_pricing.cost_usd, Decimal("75.0000"))

    def test_05_existing_presentation_policy_is_called(self) -> None:
        restored = restore_economic_product_option_records(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        with patch(
            "sync_worker.product_option_presentation_dry_run."
            "retail_price_presentation.present_retail_price",
            wraps=present_retail_price,
        ) as policy:
            build_product_option_presentation_report(
                restored,
                input_file="fixture.json",
            )
        policy.assert_called_once_with(Decimal("112.50"))

    def test_06_dry_run_does_not_reimplement_presentation_rules(self) -> None:
        restored = restore_economic_product_option_records(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        baseline = present_retail_price(Decimal("112.50"))
        policy_result = replace(
            baseline,
            presentation=PresentedRetailPrice(display_price_usd=Decimal("118.00")),
            calculation=PresentationCalculation(
                strategy="nine_ending",
                candidate_price=Decimal("118.00"),
                uplift_amount=Decimal("5.50"),
                uplift_rate=Decimal("0.0489"),
                fallback_used=False,
            ),
        )
        with patch(
            "sync_worker.product_option_presentation_dry_run."
            "retail_price_presentation.present_retail_price",
            return_value=policy_result,
        ):
            report = build_product_option_presentation_report(
                restored,
                input_file="fixture.json",
            )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "118.00",
        )

    def test_07_112_50_to_119(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "119.00",
        )

    def test_08_90_to_99(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Hard Hands and Feet", "90.00")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "99.00",
        )

    def test_09_135_to_139(self) -> None:
        _, report = build_fixture(payload(product(composite_option())))
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "139.00",
        )

    def test_10_19_50_to_19_99(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Low Option", "19.50")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "19.99",
        )

    def test_11_47_25_to_47_99(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Low Option", "47.25")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "47.99",
        )

    def test_12_56_25_to_59(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Nine Option", "56.25")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "59.00",
        )

    def test_13_67_50_to_69(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Nine Option", "67.50")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["display_price_usd"],
            "69.00",
        )

    def test_14_economic_target_is_preserved(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        option = first_presented(report)
        self.assertEqual(option["economic_pricing"]["target_retail_usd"], "112.50")

    def test_15_display_price_is_stored_as_a_separate_layer(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        option = first_presented(report)
        self.assertEqual(option["economic_pricing"]["target_retail_usd"], "112.50")
        self.assertEqual(option["presentation"]["display_price_usd"], "119.00")
        self.assertNotEqual(
            option["economic_pricing"]["target_retail_usd"],
            option["presentation"]["display_price_usd"],
        )

    def test_16_display_is_never_below_economic_target(self) -> None:
        options = [
            priced_option("A", "19.50"),
            priced_option("B", "47.25"),
            priced_option("C", "56.25"),
            priced_option("D", "90.00"),
            priced_option("E", "112.50"),
            composite_option(),
        ]
        _, report = build_fixture(payload(product(*options)))
        for option in report["results"][0]["presented_upgrade_options"]:
            with self.subTest(name=option["product_upgrade_name"]):
                self.assertGreaterEqual(
                    Decimal(option["presentation"]["display_price_usd"]),
                    Decimal(option["economic_pricing"]["target_retail_usd"]),
                )

    def test_17_uplift_amount_is_reported(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["uplift_amount"],
            "6.50",
        )

    def test_18_uplift_rate_is_reported(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        self.assertEqual(
            first_presented(report)["presentation"]["uplift_rate"],
            "0.0578",
        )

    def test_19_maximum_observed_uplift_does_not_exceed_policy(self) -> None:
        _, report = build_fixture(
            payload(
                product(
                    priced_option("A", "90.00"),
                    priced_option("B", "112.50"),
                    composite_option(),
                )
            )
        )
        observed = Decimal(report["summary"]["max_uplift_rate_observed"])
        policy_max = Decimal(report["policy"]["max_presentation_uplift_rate"])
        self.assertLessEqual(observed, policy_max)

    def test_20_fallback_metadata_is_preserved(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Fallback", "50.00")))
        )
        presentation = first_presented(report)["presentation"]
        self.assertEqual(presentation["display_price_usd"], "50.99")
        self.assertEqual(presentation["strategy"], "x_99_fallback")
        self.assertEqual(presentation["candidate_price"], "59.00")
        self.assertTrue(presentation["fallback_used"])

    def test_21_x99_summary(self) -> None:
        _, report = build_fixture(
            payload(
                product(
                    priced_option("A", "19.50"),
                    priced_option("B", "47.25"),
                )
            )
        )
        self.assertEqual(report["summary"]["x99_presentations"], 2)

    def test_22_nine_ending_summary(self) -> None:
        _, report = build_fixture(
            payload(
                product(
                    priced_option("A", "90.00"),
                    priced_option("B", "112.50"),
                    composite_option(),
                )
            )
        )
        self.assertEqual(report["summary"]["nine_ending_presentations"], 3)

    def test_23_unchanged_summary(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Already", "119.00")))
        )
        self.assertEqual(report["summary"]["unchanged_presentations"], 1)

    def test_24_fallback_summary(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Fallback", "50.00")))
        )
        self.assertEqual(report["summary"]["fallback_presentations"], 1)

    def test_25_composite_remains_one_customer_option(self) -> None:
        _, report = build_fixture(payload(product(composite_option())))
        presented = report["results"][0]["presented_upgrade_options"]
        self.assertEqual(len(presented), 1)
        self.assertEqual(
            presented[0]["product_upgrade_name"],
            "Eyebrows/Eyelashes Implant",
        )

    def test_26_composite_components_are_preserved(self) -> None:
        _, report = build_fixture(payload(product(composite_option())))
        components = first_presented(report)["catalog_mapping"]["components"]
        self.assertEqual(
            [component["option_name"] for component in components],
            ["硅胶头植眉毛", "硅胶头植睫毛"],
        )
        combined = first_presented(report)["catalog_mapping"][
            "combined_supplier_cost"
        ]
        self.assertEqual(combined["amount"], "600")

    def test_27_missing_target_is_unpresented_without_price(self) -> None:
        _, report = build_fixture(
            payload(
                product(
                    priced_option(
                        "Missing",
                        None,
                        cost_usd=None,
                    )
                )
            )
        )
        unpresented = report["results"][0]["unpresented_upgrade_options"]
        self.assertEqual(len(unpresented), 1)
        self.assertEqual(unpresented[0]["presentation"]["status"], "no_target_price")
        self.assertIsNone(unpresented[0]["presentation"]["display_price_usd"])
        self.assertEqual(report["summary"]["no_target_price"], 1)

    def test_28_product_base_retail_is_not_changed(self) -> None:
        fixture = payload(product(priced_option("Gel Butt", "112.50")))
        original = copy.deepcopy(fixture)
        _, report = build_fixture(fixture)
        self.assertEqual(fixture, original)
        base = report["results"][0]["retail_pricing"]["minimum_retail_price"]
        self.assertEqual(base["amount"], "270")
        self.assertEqual(base["currency"], "USD")

    def test_29_no_order_or_cart_total_is_calculated(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("order_total", serialized)
        self.assertNotIn("cart_total", serialized)
        self.assertNotIn("base_plus_option", serialized)

    def test_30_supplier_cost_is_preserved(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        cost = first_presented(report)["supplier_cost"]
        self.assertEqual(cost["amount"], "500")
        self.assertEqual(cost["currency"], "RMB")
        self.assertEqual(cost["source_provenance"]["coordinates"], ["A2"])

    def test_31_mapping_registry_version_is_preserved(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        option = first_presented(report)
        self.assertEqual(option["registry_version"], REGISTRY_VERSION)
        self.assertEqual(
            option["catalog_mapping"]["registry_version"],
            REGISTRY_VERSION,
        )

    def test_32_economic_pricing_policy_version_is_preserved(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        self.assertEqual(
            first_presented(report)["economic_pricing"]["policy_version"],
            OPTION_POLICY_VERSION,
        )

    def test_33_presentation_policy_version_is_reported(self) -> None:
        _, report = build_fixture(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        self.assertEqual(report["policy"]["version"], PRESENTATION_POLICY_VERSION)
        self.assertEqual(
            first_presented(report)["presentation"]["policy_version"],
            PRESENTATION_POLICY_VERSION,
        )

    def test_34_urls_and_credentials_do_not_enter_report(self) -> None:
        option = priced_option(
            "Gel Butt https://drive.google.com/private?token=fixture-secret",
            "112.50",
            raw_value="Authorization: Bearer fixture-credential",
            warnings=["Cookie: session=fixture-cookie"],
        )
        _, report = build_fixture(payload(product(option)))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("drive.google.com", serialized)
        self.assertNotIn("fixture-secret", serialized)
        self.assertNotIn("fixture-credential", serialized)
        self.assertNotIn("fixture-cookie", serialized)

    def test_35_build_performs_no_network_requests(self) -> None:
        restored = restore_economic_product_option_records(
            payload(product(priced_option("Gel Butt", "112.50")))
        )
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            report = build_product_option_presentation_report(
                restored,
                input_file="fixture.json",
            )
        create_connection.assert_not_called()
        socket_connect.assert_not_called()
        self.assertEqual(report["network_requests_performed"], 0)

    def test_36_build_performs_no_file_or_external_write(self) -> None:
        restored = restore_economic_product_option_records(payload(product()))
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            report = build_product_option_presentation_report(
                restored,
                input_file="fixture.json",
            )
        open_mock.assert_not_called()
        self.assertEqual(report["write_requests_performed"], 0)

    def test_37_cli_uses_local_fixture_without_config_or_network(self) -> None:
        fixture = payload(product(priced_option("Gel Butt", "112.50")))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.json"
            input_path.write_text(json.dumps(fixture), encoding="utf-8")
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
                    ["present-product-option-prices", "--input", str(input_path)]
                )
            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()
            saved_path = (
                root / "reports" / "product-option-presentation-dry-run.json"
            )
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["summary"]["presented_options"], 1)
            self.assertEqual(saved["network_requests_performed"], 0)
            self.assertEqual(saved["write_requests_performed"], 0)

    def test_38_single_ultra_display_total_is_dynamic_476(self) -> None:
        options = (
            priced_option("Gel Butt", "112.50"),
            priced_option("Hair Implant", "112.50"),
            priced_option(
                "Hard Hands and Feet",
                "90.00",
                cost_usd="60.0000",
                supplier_amount="400",
            ),
            composite_option(),
        )
        _, report = build_fixture(payload(product(*options)))
        self.assertEqual(report["summary"]["total_priced_options"], 4)
        self.assertEqual(report["summary"]["total_display_price_usd"], "476.00")

    def test_39_two_ultra_display_total_is_dynamic_952(self) -> None:
        def ultra_options() -> tuple[dict[str, object], ...]:
            return (
                priced_option("Gel Butt", "112.50"),
                priced_option("Hair Implant", "112.50"),
                priced_option(
                    "Hard Hands and Feet",
                    "90.00",
                    cost_usd="60.0000",
                    supplier_amount="400",
                ),
                composite_option(),
            )

        _, report = build_fixture(
            payload(
                product(*ultra_options(), model="ULW-170-A"),
                product(*ultra_options(), model="ULW-170-B"),
            )
        )
        summary = report["summary"]
        self.assertEqual(summary["total_products"], 2)
        self.assertEqual(summary["total_priced_options"], 8)
        self.assertEqual(summary["presented_options"], 8)
        self.assertEqual(summary["total_display_price_usd"], "952.00")


if __name__ == "__main__":
    unittest.main()
