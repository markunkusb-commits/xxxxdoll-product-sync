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
from sync_worker.clm_price_dry_run import (  # noqa: E402
    CLMPriceListInputError,
    build_clm_parser_report,
    load_local_sheet_layout,
    run_clm_parser_dry_run,
)


def _cell(row: int, column: int, value: str) -> dict[str, object]:
    labels = {
        2: "B",
        9: "I",
        15: "O",
        23: "W",
        28: "AB",
        34: "AH",
        45: "AS",
    }
    return {
        "coordinate": f"{labels[column]}{row}",
        "row": row,
        "column_index": column,
        "formatted_value": value,
    }


def _fixture_layout() -> dict[str, object]:
    return {
        "status": "ok",
        "non_empty_cells": [
            _cell(2, 2, "◆ CLM Classic ◆"),
            _cell(3, 9, "Model"),
            _cell(3, 15, "C-165"),
            _cell(3, 23, "Manual"),
            _cell(3, 28, "https://supplier.example/private"),
            _cell(4, 34, "Price includes the following:"),
            _cell(5, 34, "EVO skeleton"),
            _cell(6, 34, "FOB Unit Price"),
            _cell(6, 45, "RMB2250"),
            _cell(7, 2, "Photo download link"),
            _cell(8, 2, "⭐CLM Pro⭐"),
            _cell(9, 9, "Model"),
            _cell(9, 15, "P-170"),
            _cell(10, 34, "Upgrade options"),
            _cell(11, 34, "Gel butt +¥300"),
            _cell(12, 2, "Photo download link"),
        ],
    }


def _write_fixture(path: Path) -> None:
    path.write_text(
        json.dumps(_fixture_layout(), ensure_ascii=False),
        encoding="utf-8",
    )


class CLMPriceDryRunTests(unittest.TestCase):
    def test_cli_command_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            ["parse-clm-price-list", "--input", "fixture.json"]
        )

        self.assertEqual(arguments.command, "parse-clm-price-list")
        self.assertEqual(arguments.input_path, Path("fixture.json"))

    def test_cli_requires_input_argument(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["parse-clm-price-list"])

        self.assertEqual(caught.exception.code, 2)

    def test_input_must_be_an_existing_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CLMPriceListInputError, "JSON file"):
                load_local_sheet_layout(root / "layout.txt")
            with self.assertRaisesRegex(CLMPriceListInputError, "does not exist"):
                load_local_sheet_layout(root / "missing.json")

    def test_local_fixture_is_parsed_into_allowlisted_summary(self) -> None:
        report = build_clm_parser_report(
            _fixture_layout(), input_file="fixture.json"
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["detected_product_count"], 2)
        self.assertEqual(
            report["series_summary"],
            {"classic": 1, "pro": 1, "ulw": 0, "ultra": 0},
        )
        first = report["products"][0]
        self.assertEqual(
            set(first),
            {
                "series",
                "raw_series_title",
                "model",
                "specifications",
                "pricing",
                "included_features",
                "upgrade_options",
                "notices",
                "source",
                "warnings",
            },
        )
        self.assertEqual(first["source"], {"start_row": 2, "end_row": 7})

    def test_command_uses_no_network_config_or_google_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.json"
            _write_fixture(input_path)
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
                    ["parse-clm-price-list", "--input", str(input_path)]
                )

            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()
            self.assertTrue((root / "reports" / "clm-parser-dry-run.json").is_file())

    def test_report_redacts_urls_and_external_absolute_input_path(self) -> None:
        with tempfile.TemporaryDirectory() as input_directory:
            with tempfile.TemporaryDirectory() as project_directory:
                input_path = Path(input_directory) / "supplier-layout.json"
                project_root = Path(project_directory)
                _write_fixture(input_path)

                _, report_path = run_clm_parser_dry_run(
                    input_path, project_root=project_root
                )
                report_text = report_path.read_text(encoding="utf-8")
                report = json.loads(report_text)

        self.assertEqual(report["input_file"], "supplier-layout.json")
        self.assertNotIn(str(input_path), report_text)
        self.assertNotIn("supplier.example", report_text)
        self.assertNotIn("https://", report_text)
        self.assertIn("[URL_REDACTED]", report_text)

    def test_report_counters_are_always_zero(self) -> None:
        report = build_clm_parser_report(
            _fixture_layout(), input_file="fixture.json"
        )

        self.assertEqual(report["network_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(report["errors_count"], 0)


if __name__ == "__main__":
    unittest.main()
