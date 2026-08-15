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
from sync_worker.size_list_dry_run import (  # noqa: E402
    build_size_list_report,
    load_local_size_layout,
)
from sync_worker.size_list_parser import parse_size_list  # noqa: E402


def cell(
    coordinate: str,
    value: str,
    *,
    merged_range: str | None = None,
) -> dict[str, object]:
    column = "".join(character for character in coordinate if character.isalpha())
    row = int("".join(character for character in coordinate if character.isdigit()))
    column_index = 0
    for character in column:
        column_index = column_index * 26 + ord(character.upper()) - ord("A") + 1
    return {
        "coordinate": coordinate,
        "row": row,
        "column": column,
        "column_index": column_index,
        "formatted_value": value,
        "is_merged": merged_range is not None,
        "is_merge_anchor": merged_range is not None,
        "merged_range": merged_range,
    }


def fixture_layout() -> dict[str, object]:
    return {
        "status": "ok",
        "non_empty_cells": [
            cell("A1", "Type\n类型"),
            cell("B1", "Body type\n身型"),
            cell("C1", "FOB Price\n出厂价格"),
            cell("D1", "Upper Chest\n上胸围"),
            cell("E1", "Lower Chest\n下胸围"),
            cell("F1", "Notes"),
            cell(
                "A2",
                "Full Silicone",
                merged_range="A2:A3",
            ),
            cell("B2", "FD140cm"),
            cell("C2", "￥5,500.00"),
            cell("D2", "98cm\n(38.58in)"),
            cell("E2", "/"),
            cell(
                "F2",
                "https://supplier.example/private?token=hidden "
                + "ck_"
                + "a" * 24,
            ),
            cell("B3", "BW82# Torso"),
            cell("D3", "90cm", merged_range="D3:E3"),
            cell("B4", "J60cm XS"),
            cell("C4", "￥2,200.00"),
        ],
        "merged_ranges": [
            {
                "range": "A2:A3",
                "start_row": 2,
                "end_row": 3,
                "start_column": "A",
                "end_column": "A",
                "anchor": "A2",
            },
            {
                "range": "D3:E3",
                "start_row": 3,
                "end_row": 3,
                "start_column": "D",
                "end_column": "E",
                "anchor": "D3",
            },
        ],
    }


class SizeListDryRunTests(unittest.TestCase):
    def test_01_cli_registers_parse_size_list(self) -> None:
        arguments = build_parser().parse_args(
            ["parse-size-list", "--input", "fixture.json"]
        )
        self.assertEqual(arguments.command, "parse-size-list")
        self.assertEqual(arguments.input_path, Path("fixture.json"))

    def test_02_cli_requires_input(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["parse-size-list"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_local_fixture_json_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(fixture_layout()), encoding="utf-8")
            loaded = load_local_size_layout(path)
        self.assertEqual(loaded["status"], "ok")
        self.assertEqual(len(loaded["non_empty_cells"]), 16)

    def test_04_report_calls_existing_size_parser(self) -> None:
        with patch(
            "sync_worker.size_list_dry_run.parse_size_list",
            wraps=parse_size_list,
        ) as parser:
            build_size_list_report(fixture_layout(), input_file="fixture.json")
        parser.assert_called_once()

    def test_05_report_record_count(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        self.assertEqual(report["detected_record_count"], 3)
        self.assertEqual(len(report["records"]), 3)

    def test_06_type_summary_counts_only_explicit_or_merged_types(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        self.assertEqual(report["type_summary"], {"Full Silicone": 2})

    def test_07_fob_is_only_under_supplier_costs(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        first = report["records"][0]
        self.assertEqual(first["supplier_costs"]["fob_price"]["amount"], 5500)
        self.assertEqual(first["supplier_costs"]["fob_price"]["currency"], "RMB")

    def test_08_report_contains_no_customer_or_retail_price_fields(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        serialized = json.dumps(report, ensure_ascii=False).casefold()
        for forbidden in (
            "retail_price",
            "regular_price",
            "sale_price",
            "customer_price",
            "minimum_retail_price",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_09_warning_summary(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        self.assertEqual(report["records_with_warnings"], 3)
        self.assertEqual(report["warnings_count"], 3)

    def test_10_missing_type_summary(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        self.assertEqual(report["records_with_missing_type"], 1)
        self.assertIsNone(report["records"][2]["type"])

    def test_11_ambiguous_merge_summary_and_record_shape(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        torso = report["records"][1]
        self.assertEqual(report["records_with_ambiguous_merge"], 1)
        self.assertIsNone(torso["measurements"]["upper_chest"])
        self.assertIsNone(torso["measurements"]["lower_chest"])
        self.assertEqual(torso["raw_measurements"][0]["merged_range"], "D3:E3")

    def test_12_urls_and_credentials_are_sanitized(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("supplier.example", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("ck_" + "a" * 24, serialized)
        self.assertIn("[URL_REDACTED]", serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)

    def test_13_request_counters_and_fob_summary_are_zero_safe(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        self.assertEqual(report["network_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(report["records_with_fob_price"], 2)
        self.assertEqual(report["records_without_fob_price"], 1)

    def test_14_special_raw_values_remain_visible_in_report(self) -> None:
        report = build_size_list_report(
            fixture_layout(), input_file="fixture.json"
        )
        first, torso, suffix = report["records"]
        self.assertEqual(first["raw_measurements"][1]["raw_value"], "/")
        self.assertEqual(torso["raw_body_type"], "BW82# Torso")
        self.assertEqual(suffix["raw_body_type"], "J60cm XS")

    def test_15_cli_uses_no_config_google_network_or_real_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "mock-size-layout.json"
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
                    ["parse-size-list", "--input", str(input_path)]
                )

            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()
            report_path = root / "reports" / "size-list-dry-run.json"
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["write_requests_performed"], 0)
            self.assertEqual(saved["input_file"], "mock-size-layout.json")


if __name__ == "__main__":
    unittest.main()
