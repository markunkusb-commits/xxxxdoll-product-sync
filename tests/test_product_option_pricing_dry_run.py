from __future__ import annotations

import builtins
import json
import socket
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.option_pricing_policy import (  # noqa: E402
    calculate_option_retail_price,
)
from sync_worker.product_option_linker import ProductOptionLinkResult  # noqa: E402
from sync_worker.product_option_pricing import (  # noqa: E402
    enrich_product_option_pricing,
)
from sync_worker.product_option_pricing_dry_run import (  # noqa: E402
    ProductOptionPricingDryRunInputError,
    build_product_option_pricing_report,
    load_local_linking_report,
    restore_product_option_link_results,
)


RATE = Decimal("0.1500")
REGISTRY_VERSION = "clm-option-map-v1"


def supplier_cost(
    amount: str | None,
    *,
    currency: str | None = "RMB",
    raw_price: str | None = None,
) -> dict[str, object]:
    return {
        "amount": amount,
        "currency": currency,
        "raw_price": raw_price,
        "price_range": None,
        "price_anchor": None,
        "shared_price_source": False,
    }


def simple_link(
    name: str,
    amount: str | None,
    *,
    mapping_type: str = "alias",
    currency: str | None = "RMB",
    catalog_name: str = "目录选项",
    coordinate: str = "A2",
    raw_value: str | None = None,
    registry_version: str | None = REGISTRY_VERSION,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "product_upgrade_name": name,
        "product_raw_value": raw_value or f"1. {name}",
        "mapping_type": mapping_type,
        "registry_version": registry_version,
        "catalog_option_name": catalog_name,
        "catalog_category": "product_extra_option",
        "supplier_cost": supplier_cost(
            amount,
            currency=currency,
            raw_price=(f"￥{amount}" if amount is not None else None),
        ),
        "catalog_source_coordinate": coordinate,
        "warnings": warnings or [],
    }


def component(
    name: str,
    amount: str | None,
    coordinate: str,
    *,
    currency: str | None = "RMB",
) -> dict[str, object]:
    return {
        "option_name": name,
        "category": "product_extra_option",
        "supplier_cost": supplier_cost(
            amount,
            currency=currency,
            raw_price=(f"￥{amount}" if amount is not None else None),
        ),
        "source_coordinate": coordinate,
    }


def composite_link(
    *,
    name: str = "Eyebrows/Eyelashes Implant",
    combined_amount: str = "600",
) -> dict[str, object]:
    return {
        "product_upgrade_name": name,
        "product_raw_value": f"3. {name}",
        "mapping_type": "composite",
        "registry_version": REGISTRY_VERSION,
        "components": [
            component("硅胶头植眉毛", "300", "A3"),
            component("硅胶头植睫毛", "300", "A4"),
        ],
        "combined_supplier_cost": {
            "amount": combined_amount,
            "currency": "RMB",
        },
        "warnings": [],
    }


def mapping_issue(status: str) -> dict[str, object]:
    components = [component("硅胶头植眉毛", "300", "A3")]
    if status == "currency_conflict":
        components.append(
            component("硅胶头植睫毛", "20", "A4", currency="USD")
        )
    elif status == "missing_component_price":
        components.append(component("硅胶头植睫毛", None, "A4", currency=None))
    return {
        "product_upgrade_name": "Eyebrows/Eyelashes Implant",
        "product_raw_value": "3. Eyebrows/Eyelashes Implant",
        "mapping_type": "composite",
        "status": status,
        "registry_version": REGISTRY_VERSION,
        "components": components,
        "missing_component_names": (
            ["硅胶头植睫毛"] if status == "incomplete_composite" else []
        ),
        "warnings": [f"fixture {status}"],
    }


def product_result(
    *linked: dict[str, object],
    mapping_issues: list[dict[str, object]] | None = None,
    unmatched: list[dict[str, object]] | None = None,
    ambiguous: list[dict[str, object]] | None = None,
    model: str = "ULW-170",
) -> dict[str, object]:
    return {
        "series": "ultra",
        "product_identity": {
            "model": model,
            "raw_model": model,
            "raw_series_title": "Ultra Series",
        },
        "source_rows": {"start_row": 480, "end_row": 490},
        "included_features": ["articulated fingers"],
        "raw_upgrade_options": [item["product_raw_value"] for item in linked],
        "linked_upgrade_options": list(linked),
        "unmatched_upgrade_options": unmatched or [],
        "ambiguous_upgrade_options": ambiguous or [],
        "included_upgrade_conflicts": [],
        "mapping_issues": mapping_issues or [],
        "retail_pricing": {
            "minimum_retail_price": {
                "amount": "270",
                "currency": "USD",
                "raw_value": "US$270",
            }
        },
        "warnings": [],
    }


def payload(*results: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "results": list(results)}


def restore_and_build(
    fixture: dict[str, object],
) -> tuple[list[ProductOptionLinkResult], dict[str, object]]:
    restored = restore_product_option_link_results(fixture)
    report = build_product_option_pricing_report(
        restored,
        input_file="fixture.json",
        rmb_to_usd_rate=RATE,
    )
    return restored, report


def first_priced(report: dict[str, object]) -> dict[str, object]:
    return report["results"][0]["priced_upgrade_options"][0]


class ProductOptionPricingDryRunTests(unittest.TestCase):
    def test_01_cli_command_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            [
                "price-linked-product-options",
                "--input",
                "fixture.json",
                "--rmb-to-usd",
                "0.1500",
            ]
        )
        self.assertEqual(arguments.command, "price-linked-product-options")
        self.assertEqual(arguments.input_path, Path("fixture.json"))
        self.assertEqual(arguments.rmb_to_usd_rate, RATE)

    def test_02_input_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["price-linked-product-options", "--rmb-to-usd", "0.1500"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_03_fx_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["price-linked-product-options", "--input", "fixture.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_04_invalid_fx_is_rejected(self) -> None:
        for invalid in ("invalid", "0", "-0.15", "NaN", "Infinity"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SystemExit) as caught:
                    build_parser().parse_args(
                        [
                            "price-linked-product-options",
                            "--input",
                            "fixture.json",
                            "--rmb-to-usd",
                            invalid,
                        ]
                    )
                self.assertEqual(caught.exception.code, 2)

    def test_05_decimal_fx_scale_is_preserved(self) -> None:
        _, report = restore_and_build(payload(product_result()))
        self.assertEqual(report["fx"]["rmb_to_usd"], "0.1500")
        self.assertEqual(report["fx"]["rate_source"], "cli_injected")

    def test_06_local_mock_report_is_loaded(self) -> None:
        fixture = payload(product_result())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            loaded = load_local_linking_report(path)
        self.assertEqual(loaded, fixture)

    def test_07_product_option_link_result_is_restored(self) -> None:
        restored = restore_product_option_link_results(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        self.assertEqual(len(restored), 1)
        self.assertIsInstance(restored[0], ProductOptionLinkResult)

    def test_08_simple_alias_fields_are_restored(self) -> None:
        restored = restore_product_option_link_results(
            payload(product_result(simple_link("Gel Butt", "500")))
        )[0].linked_upgrade_options[0]
        self.assertEqual(restored.product_option.name, "Gel Butt")
        self.assertEqual(restored.match_method, "approved_alias")
        self.assertEqual(restored.pricing.amount, Decimal("500"))
        self.assertEqual(restored.pricing_source.raw_coordinate, "A2")

    def test_09_composite_resolution_is_restored(self) -> None:
        restored = restore_product_option_link_results(
            payload(product_result(composite_link()))
        )[0]
        self.assertEqual(len(restored.linked_upgrade_options), 0)
        self.assertEqual(len(restored.mapping_resolutions), 1)
        self.assertEqual(
            restored.mapping_resolutions[0].combined_supplier_cost.amount,
            Decimal("600"),
        )

    def test_10_existing_product_pricing_layer_is_called(self) -> None:
        restored = restore_product_option_link_results(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        with patch(
            "sync_worker.product_option_pricing_dry_run."
            "product_option_pricing.enrich_product_option_pricing",
            wraps=enrich_product_option_pricing,
        ) as pricing_layer:
            build_product_option_pricing_report(
                restored,
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )
        pricing_layer.assert_called_once_with(restored, rmb_to_usd_rate=RATE)

    def test_11_existing_option_pricing_policy_is_reused(self) -> None:
        restored = restore_product_option_link_results(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price",
            wraps=calculate_option_retail_price,
        ) as policy:
            build_product_option_pricing_report(
                restored,
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )
        policy.assert_called_once()

    def test_12_alias_rmb_500_prices_to_112_50(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        pricing = first_priced(report)["pricing"]
        self.assertEqual(pricing["cost_usd"], "75.0000")
        self.assertEqual(pricing["target_retail_usd"], "112.50")

    def test_13_alias_rmb_400_prices_to_90_00(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Hard Hands and Feet", "400")))
        )
        pricing = first_priced(report)["pricing"]
        self.assertEqual(pricing["cost_usd"], "60.0000")
        self.assertEqual(pricing["target_retail_usd"], "90.00")

    def test_14_composite_rmb_600_prices_to_135_00(self) -> None:
        _, report = restore_and_build(payload(product_result(composite_link())))
        pricing = first_priced(report)["pricing"]
        self.assertEqual(pricing["cost_usd"], "90.0000")
        self.assertEqual(pricing["target_retail_usd"], "135.00")

    def test_15_composite_outputs_one_customer_option(self) -> None:
        _, report = restore_and_build(payload(product_result(composite_link())))
        priced = report["results"][0]["priced_upgrade_options"]
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0]["product_upgrade_name"], "Eyebrows/Eyelashes Implant")

    def test_16_composite_component_provenance_is_preserved(self) -> None:
        _, report = restore_and_build(payload(product_result(composite_link())))
        option = first_priced(report)
        mapping = option["catalog_mapping"]
        self.assertEqual(
            [item["option_name"] for item in mapping["components"]],
            ["硅胶头植眉毛", "硅胶头植睫毛"],
        )
        self.assertEqual(
            mapping["combined_supplier_cost"]["amount"],
            "600",
        )
        self.assertEqual(
            mapping["combined_supplier_cost"]["source_provenance"]["coordinates"],
            ["A3", "A4"],
        )

    def test_17_exact_priced_summary(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(
                    simple_link(
                        "Exact Fixture", "100", mapping_type="exact", registry_version=None
                    )
                )
            )
        )
        self.assertEqual(report["summary"]["exact_priced"], 1)

    def test_18_alias_priced_summary(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        self.assertEqual(report["summary"]["alias_priced"], 1)

    def test_19_composite_priced_summary(self) -> None:
        _, report = restore_and_build(payload(product_result(composite_link())))
        self.assertEqual(report["summary"]["composite_priced"], 1)

    def test_20_no_supplier_price_is_unpriced(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(
                    simple_link("Gel Butt", None, currency=None)
                )
            )
        )
        option = report["results"][0]["unpriced_upgrade_options"][0]
        self.assertEqual(option["pricing"]["status"], "no_supplier_price")
        self.assertEqual(report["summary"]["no_supplier_price"], 1)

    def test_21_unsupported_currency_is_unpriced(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(simple_link("Gel Butt", "500", currency="EUR"))
            )
        )
        option = report["results"][0]["unpriced_upgrade_options"][0]
        self.assertEqual(option["pricing"]["status"], "unsupported_currency")
        self.assertEqual(report["summary"]["unsupported_currency"], 1)

    def test_22_incomplete_composite_is_not_priceable(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(
                    mapping_issues=[mapping_issue("incomplete_composite")]
                )
            )
        )
        option = report["results"][0]["unpriced_upgrade_options"][0]
        self.assertEqual(option["pricing"]["status"], "mapping_not_priceable")
        self.assertIsNone(option["pricing"]["target_retail_usd"])

    def test_23_ambiguous_mapping_is_not_priceable(self) -> None:
        ambiguous = {
            "product_raw_option": "1. Ambiguous",
            "match_method": "approved_alias",
            "catalog_candidates": [
                {
                    "option_name": "Candidate A",
                    "category": "product_extra_option",
                    "source_coordinate": "A2",
                },
                {
                    "option_name": "Candidate B",
                    "category": "product_extra_option",
                    "source_coordinate": "A3",
                },
            ],
            "warnings": ["multiple catalog options matched"],
        }
        _, report = restore_and_build(
            payload(product_result(ambiguous=[ambiguous]))
        )
        option = report["results"][0]["unpriced_upgrade_options"][0]
        self.assertEqual(option["catalog_mapping"]["status"], "ambiguous")
        self.assertEqual(option["pricing"]["status"], "mapping_not_priceable")

    def test_24_currency_conflict_does_not_fallback(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(mapping_issues=[mapping_issue("currency_conflict")])
            )
        )
        option = report["results"][0]["unpriced_upgrade_options"][0]
        self.assertEqual(option["catalog_mapping"]["status"], "currency_conflict")
        self.assertIsNone(option["pricing"]["cost_usd"])

    def test_25_missing_component_price_does_not_fallback(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(
                    mapping_issues=[mapping_issue("missing_component_price")]
                )
            )
        )
        option = report["results"][0]["unpriced_upgrade_options"][0]
        self.assertEqual(
            option["catalog_mapping"]["status"], "missing_component_price"
        )
        self.assertIsNone(option["pricing"]["target_retail_usd"])

    def test_26_product_base_retail_is_unchanged(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        base = report["results"][0]["retail_pricing"]["minimum_retail_price"]
        self.assertEqual(base["amount"], "270")
        self.assertEqual(base["currency"], "USD")

    def test_27_base_and_option_are_never_added(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("order_total", serialized)
        self.assertNotIn("combined_product_price", serialized)
        self.assertEqual(first_priced(report)["pricing"]["target_retail_usd"], "112.50")

    def test_28_psychological_rounding_is_not_applied(self) -> None:
        _, report = restore_and_build(
            payload(
                product_result(
                    simple_link("Gel Butt", "500"),
                    simple_link("Hard Hands and Feet", "400", coordinate="A5"),
                    composite_link(),
                )
            )
        )
        targets = [
            option["pricing"]["target_retail_usd"]
            for option in report["results"][0]["priced_upgrade_options"]
        ]
        self.assertEqual(targets, ["112.50", "90.00", "135.00"])

    def test_29_registry_version_is_preserved(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        self.assertEqual(first_priced(report)["registry_version"], REGISTRY_VERSION)

    def test_30_fx_rate_is_preserved_per_priced_option(self) -> None:
        _, report = restore_and_build(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        self.assertEqual(first_priced(report)["pricing"]["fx_rate"], "0.1500")

    def test_31_policy_version_and_fixed_parameters_are_reported(self) -> None:
        _, report = restore_and_build(payload(product_result()))
        self.assertEqual(report["policy"]["version"], "option-retail-v1")
        self.assertEqual(report["policy"]["markup_rate"], "0.50")
        self.assertEqual(report["policy"]["minimum_profit_usd"], "15.00")

    def test_32_product_source_trace_is_preserved(self) -> None:
        _, report = restore_and_build(payload(product_result()))
        self.assertEqual(
            report["results"][0]["source_trace"],
            {"start_row": 480, "end_row": 490},
        )

    def test_33_urls_and_credentials_do_not_enter_report(self) -> None:
        linked = simple_link(
            "Gel Butt https://drive.google.com/private?token=fixture-secret",
            "500",
            raw_value="Authorization: Bearer fixture-credential",
            warnings=["Cookie: session=fixture-cookie"],
        )
        _, report = restore_and_build(payload(product_result(linked)))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("drive.google.com", serialized)
        self.assertNotIn("fixture-secret", serialized)
        self.assertNotIn("fixture-credential", serialized)
        self.assertNotIn("fixture-cookie", serialized)

    def test_34_build_performs_no_network_requests(self) -> None:
        restored = restore_product_option_link_results(
            payload(product_result(simple_link("Gel Butt", "500")))
        )
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            report = build_product_option_pricing_report(
                restored,
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )
        create_connection.assert_not_called()
        socket_connect.assert_not_called()
        self.assertEqual(report["network_requests_performed"], 0)

    def test_35_build_performs_no_file_or_external_write(self) -> None:
        restored = restore_product_option_link_results(payload(product_result()))
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            report = build_product_option_pricing_report(
                restored,
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )
        open_mock.assert_not_called()
        self.assertEqual(report["write_requests_performed"], 0)

    def test_36_cli_uses_local_fixture_without_configuration_or_network(self) -> None:
        fixture = payload(product_result(simple_link("Gel Butt", "500")))
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
                    [
                        "price-linked-product-options",
                        "--input",
                        str(input_path),
                        "--rmb-to-usd",
                        "0.1500",
                    ]
                )
            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()
            report_path = root / "reports" / "product-option-pricing-dry-run.json"
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["summary"]["priced_options"], 1)
            self.assertEqual(saved["network_requests_performed"], 0)
            self.assertEqual(saved["write_requests_performed"], 0)


if __name__ == "__main__":
    unittest.main()
