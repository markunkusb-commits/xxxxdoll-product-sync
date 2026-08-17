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

from sync_worker.additional_option_dry_run import (  # noqa: E402
    build_additional_option_report,
)
from sync_worker.additional_option_parser import parse_additional_options  # noqa: E402
from sync_worker.cli import build_parser, main  # noqa: E402


def cell(
    coordinate: str,
    value: str,
    *,
    merged_range: str | None = None,
) -> dict[str, object]:
    column = "".join(character for character in coordinate if character.isalpha())
    row = int("".join(character for character in coordinate if character.isdigit()))
    return {
        "coordinate": coordinate,
        "row": row,
        "column": column,
        "formatted_value": value,
        "is_merged": merged_range is not None,
        "is_merge_anchor": merged_range is not None,
        "merged_range": merged_range,
    }


def fixture_layout() -> dict[str, object]:
    return {
        "status": "ok",
        "non_empty_cells": [
            cell("A2", "Gel Butt"),
            cell("B2", "+¥300"),
            cell("A3", "Skin Tone"),
            cell("B3", "¥500.00"),
            cell("A4", "Hair Implant"),
            cell("B4", "RMB"),
            cell("D2", "Custom Necklace"),
            cell("E2", "RMB500"),
        ],
        "merged_ranges": [],
    }


class AdditionalOptionDryRunTests(unittest.TestCase):
    def test_cli_command_and_required_input_are_registered(self) -> None:
        arguments = build_parser().parse_args(
            ["parse-additional-option", "--input", "fixture.json"]
        )
        self.assertEqual(arguments.command, "parse-additional-option")
        self.assertEqual(arguments.input_path, Path("fixture.json"))

        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["parse-additional-option"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_b_product_option_pair_is_parsed(self) -> None:
        result = parse_additional_options(fixture_layout())

        gel = next(item for item in result.options if item.identity.option_name == "Gel Butt")
        self.assertEqual(gel.source.raw_coordinate, "A2")
        self.assertEqual(gel.category, "product_extra_option")
        self.assertEqual(gel.pricing.amount, 300)

    def test_d_e_accessory_pair_forces_accessory_category(self) -> None:
        result = parse_additional_options(fixture_layout())

        necklace = next(
            item
            for item in result.options
            if item.identity.option_name == "Custom Necklace"
        )
        self.assertEqual(necklace.source.raw_coordinate, "D2")
        self.assertEqual(necklace.category, "accessory")
        self.assertEqual(necklace.pricing.amount, 500)

    def test_rmb_yuan_decimal_and_plus_yuan_prices_are_supported(self) -> None:
        result = parse_additional_options(fixture_layout())
        by_name = {item.identity.option_name: item for item in result.options}

        self.assertEqual(by_name["Gel Butt"].pricing.raw_price, "+¥300")
        self.assertEqual(by_name["Skin Tone"].pricing.amount, 500.0)
        self.assertEqual(by_name["Skin Tone"].pricing.currency, "RMB")
        self.assertIsNone(by_name["Hair Implant"].pricing.amount)
        self.assertEqual(by_name["Hair Implant"].pricing.currency, "RMB")
        self.assertIn(
            "unable to parse price", by_name["Hair Implant"].warnings
        )

    def test_shared_merged_price_keeps_one_provenance_for_both_options(self) -> None:
        merged_layout = {
            "non_empty_cells": [
                cell("A2", "Gel Butt"),
                cell("B2", "¥500.00", merged_range="B2:B3"),
                cell("A3", "Hair Implant"),
            ],
            "merged_ranges": [
                {
                    "range": "B2:B3",
                    "start_row": 2,
                    "end_row": 3,
                    "start_column": "B",
                    "end_column": "B",
                    "anchor": "B2",
                }
            ],
        }

        result = parse_additional_options(merged_layout)
        amounts = [item.pricing.amount for item in result.options]

        self.assertEqual(amounts.count(500.0), 2)
        self.assertEqual(
            [item.pricing.price_range for item in result.options],
            ["B2:B3", "B2:B3"],
        )
        self.assertEqual(
            [item.pricing.price_anchor for item in result.options],
            ["B2", "B2"],
        )
        self.assertTrue(
            all(item.pricing.shared_price_source for item in result.options)
        )
        self.assertTrue(all(not item.warnings for item in result.options))

        report = build_additional_option_report(
            merged_layout,
            input_file="fixture.json",
        )
        prices = [item["price"] for item in report["options"]]
        self.assertTrue(all(item["shared_price_source"] for item in prices))
        self.assertEqual({item["price_anchor"] for item in prices}, {"B2"})

    def test_a_b_unknown_name_uses_explicit_primary_category(self) -> None:
        report = build_additional_option_report(
            {
                "non_empty_cells": [
                    cell("A2", "Custom Shoulder Setup"),
                    cell("B2", "RMB500"),
                ]
            },
            input_file="fixture.json",
        )

        self.assertEqual(report["detected_option_count"], 1)
        self.assertEqual(report["category_summary"]["product_extra_option"], 1)
        self.assertEqual(report["options"][0]["option_name"], "Custom Shoulder Setup")
        self.assertEqual(report["warnings_count"], 0)

    def test_report_shape_and_zero_request_counters(self) -> None:
        report = build_additional_option_report(
            fixture_layout(), input_file="fixture.json"
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["detected_option_count"], 4)
        self.assertEqual(report["errors_count"], 0)
        self.assertEqual(report["network_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(
            set(report["options"][0]),
            {"category", "option_name", "price", "source_coordinate", "warnings"},
        )

    def test_cli_uses_only_local_fixture_and_no_config_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.json"
            input_path.write_text(
                json.dumps(fixture_layout(), ensure_ascii=False),
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
                    ["parse-additional-option", "--input", str(input_path)]
                )

            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()
            report_path = root / "reports" / "additional-option-dry-run.json"
            self.assertTrue(report_path.is_file())
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["write_requests_performed"], 0)


if __name__ == "__main__":
    unittest.main()
