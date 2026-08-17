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
from sync_worker.option_pricing_dry_run import (  # noqa: E402
    OptionPricingDryRunInputError,
    build_option_pricing_report,
    load_local_option_report,
    parse_rmb_to_usd_rate,
)
from sync_worker.option_pricing_policy import (  # noqa: E402
    calculate_option_retail_price,
)


RATE = Decimal("0.1500")


def option(
    name: str,
    amount: str | int | None,
    *,
    category: str = "product_extra_option",
    currency: str | None = "RMB",
    raw_price: str | None = None,
    price_range: str | None = None,
    price_anchor: str | None = None,
    shared_price_source: bool = False,
    warnings: list[str] | None = None,
    coordinate: str = "A2",
) -> dict[str, object]:
    return {
        "category": category,
        "option_name": name,
        "price": {
            "amount": amount,
            "currency": currency,
            "raw_price": raw_price,
            "price_range": price_range,
            "price_anchor": price_anchor,
            "shared_price_source": shared_price_source,
        },
        "source_coordinate": coordinate,
        "warnings": warnings or [],
    }


def payload(*options: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "options": list(options)}


def build_one(
    amount: str | int | None,
    *,
    currency: str | None = "RMB",
    category: str = "product_extra_option",
    raw_price: str | None = None,
    warnings: list[str] | None = None,
    price_range: str | None = None,
    price_anchor: str | None = None,
    shared_price_source: bool = False,
) -> dict[str, object]:
    report = build_option_pricing_report(
        payload(
            option(
                "Fixture Option",
                amount,
                currency=currency,
                category=category,
                raw_price=raw_price,
                warnings=warnings,
                price_range=price_range,
                price_anchor=price_anchor,
                shared_price_source=shared_price_source,
            )
        ),
        input_file="fixture.json",
        rmb_to_usd_rate=RATE,
    )
    return report["options"][0]


class OptionPricingDryRunTests(unittest.TestCase):
    def test_01_cli_command_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            [
                "price-additional-options",
                "--input",
                "fixture.json",
                "--rmb-to-usd",
                "0.1500",
            ]
        )
        self.assertEqual(arguments.command, "price-additional-options")
        self.assertEqual(arguments.input_path, Path("fixture.json"))
        self.assertEqual(arguments.rmb_to_usd_rate, Decimal("0.1500"))

    def test_02_input_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["price-additional-options", "--rmb-to-usd", "0.1500"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_03_fx_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["price-additional-options", "--input", "fixture.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_04_invalid_fx_text_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                [
                    "price-additional-options",
                    "--input",
                    "fixture.json",
                    "--rmb-to-usd",
                    "not-a-rate",
                ]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_05_nonpositive_and_nonfinite_fx_are_rejected(self) -> None:
        for invalid in ("0", "-0.15", "NaN", "Infinity"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OptionPricingDryRunInputError):
                    parse_rmb_to_usd_rate(invalid)

    def test_06_fx_is_decimal_and_preserves_scale(self) -> None:
        rate = parse_rmb_to_usd_rate("0.1500")
        report = build_option_pricing_report(
            payload(option("Fixture", "30")),
            input_file="fixture.json",
            rmb_to_usd_rate=rate,
        )
        self.assertIsInstance(rate, Decimal)
        self.assertEqual(report["fx"]["rate"], "0.1500")
        self.assertEqual(report["fx"]["rmb_to_usd"], "0.1500")

    def test_07_rmb_30(self) -> None:
        self.assertEqual(
            build_one("30")["retail"]["target_retail_usd"],
            "19.50",
        )

    def test_08_rmb_50(self) -> None:
        self.assertEqual(
            build_one("50")["retail"]["target_retail_usd"],
            "22.50",
        )

    def test_09_rmb_100(self) -> None:
        self.assertEqual(
            build_one("100")["retail"]["target_retail_usd"],
            "30.00",
        )

    def test_10_rmb_200(self) -> None:
        self.assertEqual(
            build_one("200")["retail"]["target_retail_usd"],
            "45.00",
        )

    def test_11_rmb_300(self) -> None:
        self.assertEqual(
            build_one("300")["retail"]["target_retail_usd"],
            "67.50",
        )

    def test_12_rmb_500(self) -> None:
        self.assertEqual(
            build_one("500")["retail"]["target_retail_usd"],
            "112.50",
        )

    def test_13_rmb_800(self) -> None:
        self.assertEqual(
            build_one("800")["retail"]["target_retail_usd"],
            "180.00",
        )

    def test_14_rmb_1000(self) -> None:
        self.assertEqual(
            build_one("1000")["retail"]["target_retail_usd"],
            "225.00",
        )

    def test_15_rmb_1200(self) -> None:
        self.assertEqual(
            build_one("1200")["retail"]["target_retail_usd"],
            "270.00",
        )

    def test_16_minimum_profit_branch_is_reported(self) -> None:
        priced = build_one("30")
        calculation = priced["calculation"]
        self.assertEqual(calculation["markup_price_usd"], "6.7500")
        self.assertEqual(
            calculation["minimum_profit_price_usd"],
            "19.5000",
        )
        self.assertEqual(priced["retail"]["target_retail_usd"], "19.50")

    def test_17_markup_branch_is_reported(self) -> None:
        priced = build_one("500")
        calculation = priced["calculation"]
        self.assertEqual(calculation["markup_price_usd"], "112.5000")
        self.assertEqual(
            calculation["minimum_profit_price_usd"],
            "90.0000",
        )
        self.assertEqual(priced["retail"]["target_retail_usd"], "112.50")

    def test_18_product_extra_option_is_priced(self) -> None:
        priced = build_one("100", category="product_extra_option")
        self.assertEqual(priced["category"], "product_extra_option")
        self.assertEqual(priced["metadata"]["pricing_status"], "priced")

    def test_19_accessory_is_priced(self) -> None:
        priced = build_one("100", category="accessory")
        self.assertEqual(priced["category"], "accessory")
        self.assertEqual(priced["metadata"]["pricing_status"], "priced")

    def test_20_supplier_cost_is_preserved(self) -> None:
        priced = build_one(
            "500.00",
            raw_price="￥500.00",
        )
        self.assertEqual(priced["supplier_cost"]["amount"], "500.00")
        self.assertEqual(priced["supplier_cost"]["currency"], "RMB")
        self.assertEqual(priced["supplier_cost"]["raw_price"], "￥500.00")

    def test_21_retail_is_separate_from_supplier_cost(self) -> None:
        priced = build_one("500", raw_price="￥500")
        self.assertEqual(priced["supplier_cost"]["amount"], "500")
        self.assertEqual(priced["retail"]["target_retail_usd"], "112.50")
        self.assertNotIn("target_retail_usd", priced["supplier_cost"])
        self.assertNotIn("raw_price", priced["retail"])

    def test_22_existing_pricing_policy_is_reused(self) -> None:
        with patch(
            "sync_worker.option_pricing_dry_run.calculate_option_retail_price",
            wraps=calculate_option_retail_price,
        ) as policy:
            build_one("100")
        policy.assert_called_once()
        self.assertEqual(policy.call_args.kwargs["rate_source"], "cli_injected")

    def test_23_psychological_rounding_is_not_applied(self) -> None:
        target = build_one("30")["retail"]["target_retail_usd"]
        self.assertEqual(target, "19.50")
        self.assertNotEqual(target, "19.99")

    def test_24_zero_cost_uses_policy_status_and_zero_retail(self) -> None:
        priced = build_one("0")
        self.assertEqual(
            priced["metadata"]["pricing_status"],
            "zero_supplier_cost",
        )
        self.assertEqual(priced["retail"]["target_retail_usd"], "0.00")

    def test_25_missing_price_has_no_retail(self) -> None:
        priced = build_one(None, currency=None, raw_price=None)
        self.assertEqual(
            priced["metadata"]["pricing_status"],
            "no_supplier_price",
        )
        self.assertIsNone(priced["calculation"])
        self.assertIsNone(priced["retail"])

    def test_26_unsupported_currency_is_counted_without_guessing_fx(self) -> None:
        report = build_option_pricing_report(
            payload(option("EUR Fixture", "10", currency="EUR")),
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )
        priced = report["options"][0]
        self.assertEqual(
            priced["metadata"]["pricing_status"],
            "unsupported_currency",
        )
        self.assertIsNone(priced["fx"])
        self.assertIsNone(priced["retail"])
        self.assertEqual(report["summary"]["unsupported_currency_options"], 1)

    def test_27_shared_merged_price_provenance_is_propagated(self) -> None:
        priced = build_one(
            "500",
            price_range="B3:B4",
            price_anchor="B3",
            shared_price_source=True,
        )
        self.assertEqual(priced["supplier_cost"]["price_range"], "B3:B4")
        self.assertEqual(priced["supplier_cost"]["price_anchor"], "B3")
        self.assertTrue(priced["supplier_cost"]["shared_price_source"])
        self.assertEqual(priced["metadata"]["warnings"], [])

    def test_28_summary_counts_all_pricing_statuses_and_warnings(self) -> None:
        report = build_option_pricing_report(
            payload(
                option("Priced", "100"),
                option("Zero", "0"),
                option("Missing", None, currency=None),
                option("Unsupported", "10", currency="EUR"),
            ),
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )
        summary = report["summary"]
        self.assertEqual(summary["total_options"], 4)
        self.assertEqual(summary["priced_options"], 1)
        self.assertEqual(summary["zero_cost_options"], 1)
        self.assertEqual(summary["missing_price_options"], 1)
        self.assertEqual(summary["unsupported_currency_options"], 1)
        self.assertEqual(summary["warnings_count"], 3)

    def test_29_policy_parameters_are_fixed_in_report(self) -> None:
        report = build_option_pricing_report(
            payload(option("Fixture", "100")),
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )
        self.assertEqual(report["policy"]["version"], "option-retail-v1")
        self.assertEqual(report["policy"]["markup_rate"], "0.50")
        self.assertEqual(report["policy"]["minimum_profit_usd"], "15.00")
        self.assertEqual(report["fx"]["rate_source"], "cli_injected")

    def test_30_local_json_loader_reads_mock_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            expected = payload(option("Fixture", "100"))
            path.write_text(json.dumps(expected), encoding="utf-8")
            loaded = load_local_option_report(path)
        self.assertEqual(loaded, expected)

    def test_31_cli_uses_only_local_input_and_writes_safe_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.json"
            input_path.write_text(
                json.dumps(payload(option("Fixture", "500"))),
                encoding="utf-8",
            )
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
                        "price-additional-options",
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
            report_path = root / "reports" / "option-pricing-dry-run.json"
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["fx"]["rmb_to_usd"], "0.1500")
            self.assertEqual(saved["write_requests_performed"], 0)

    def test_32_report_build_performs_no_network_request(self) -> None:
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            report = build_option_pricing_report(
                payload(option("Fixture", "100")),
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )
        self.assertEqual(report["network_requests_performed"], 0)
        create_connection.assert_not_called()
        socket_connect.assert_not_called()

    def test_33_report_build_performs_no_external_or_file_write(self) -> None:
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            report = build_option_pricing_report(
                payload(option("Fixture", "100")),
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )
        self.assertEqual(report["write_requests_performed"], 0)
        open_mock.assert_not_called()

    def test_34_output_strings_are_redacted(self) -> None:
        report = build_option_pricing_report(
            payload(
                option(
                    "Fixture https://supplier.example/private?token=secret",
                    "100",
                    raw_price="Authorization: Bearer hidden-value",
                )
            ),
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("token=secret", serialized)
        self.assertNotIn("hidden-value", serialized)

    def test_35_nested_source_coordinate_is_supported(self) -> None:
        fixture = option("Fixture", "100")
        fixture.pop("source_coordinate")
        fixture["source"] = {"raw_coordinate": "A9"}
        report = build_option_pricing_report(
            payload(fixture),
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )
        self.assertEqual(report["options"][0]["source_coordinate"], "A9")

    def test_36_usd_supplier_cost_uses_policy_identity_fx(self) -> None:
        priced = build_one("10", currency="USD", raw_price="US$10")
        self.assertEqual(priced["fx"]["rate"], "1")
        self.assertEqual(priced["retail"]["target_retail_usd"], "25.00")

    def test_37_missing_options_array_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            OptionPricingDryRunInputError,
            "options array",
        ):
            build_option_pricing_report(
                {"status": "ok"},
                input_file="fixture.json",
                rmb_to_usd_rate=RATE,
            )

    def test_38_two_shared_options_are_both_priced_without_missing_price(
        self,
    ) -> None:
        shared_options = payload(
            option(
                "Option A",
                "300.00",
                raw_price="￥300.00",
                price_range="B3:B4",
                price_anchor="B3",
                shared_price_source=True,
                coordinate="A3",
            ),
            option(
                "Option B",
                "300.00",
                raw_price="￥300.00",
                price_range="B3:B4",
                price_anchor="B3",
                shared_price_source=True,
                coordinate="A4",
            ),
        )

        report = build_option_pricing_report(
            shared_options,
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )

        self.assertEqual(report["summary"]["priced_options"], 2)
        self.assertEqual(report["summary"]["missing_price_options"], 0)
        self.assertEqual(
            [item["calculation"]["cost_usd"] for item in report["options"]],
            ["45.0000", "45.0000"],
        )
        self.assertEqual(
            [item["retail"]["target_retail_usd"] for item in report["options"]],
            ["67.50", "67.50"],
        )
        self.assertEqual(
            {item["supplier_cost"]["price_anchor"] for item in report["options"]},
            {"B3"},
        )

    def test_39_legacy_price_without_provenance_defaults_to_not_shared(
        self,
    ) -> None:
        legacy = option("Legacy", "100")
        legacy_price = legacy["price"]
        legacy_price.pop("price_anchor")
        legacy_price.pop("shared_price_source")

        report = build_option_pricing_report(
            payload(legacy),
            input_file="fixture.json",
            rmb_to_usd_rate=RATE,
        )
        supplier_cost = report["options"][0]["supplier_cost"]

        self.assertIsNone(supplier_cost["price_anchor"])
        self.assertFalse(supplier_cost["shared_price_source"])


if __name__ == "__main__":
    unittest.main()
