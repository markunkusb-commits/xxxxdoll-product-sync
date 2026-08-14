from __future__ import annotations

import builtins
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.additional_option_parser import (  # noqa: E402
    AdditionalOptionParser,
    parse_additional_options,
)


def cell(value: str, *, coordinate: str = "B2") -> dict[str, object]:
    column = "".join(character for character in coordinate if character.isalpha())
    row = int("".join(character for character in coordinate if character.isdigit()))
    return {
        "coordinate": coordinate,
        "row": row,
        "column": column,
        "formatted_value": value,
    }


def layout(*cells: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "non_empty_cells": list(cells)}


class AdditionalOptionParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AdditionalOptionParser()

    def test_option_name_is_split_from_attached_price(self) -> None:
        result = self.parser.parse(layout(cell("Gel Butt +¥300")))

        option = result.options[0]
        self.assertEqual(option.identity.option_name, "Gel Butt")
        self.assertEqual(option.identity.raw_name, "Gel Butt +¥300")
        self.assertEqual(option.category, "function")

    def test_rmb_price_is_parsed(self) -> None:
        result = self.parser.parse(layout(cell("Hair Implant RMB500")))

        pricing = result.options[0].pricing
        self.assertEqual(pricing.amount, 500)
        self.assertEqual(pricing.currency, "RMB")
        self.assertEqual(pricing.raw_price, "RMB500")

    def test_yuan_price_is_normalized_to_rmb(self) -> None:
        result = self.parser.parse(layout(cell("Wigs ¥500")))

        pricing = result.options[0].pricing
        self.assertEqual(pricing.amount, 500)
        self.assertEqual(pricing.currency, "RMB")
        self.assertEqual(pricing.raw_price, "¥500")

    def test_plus_yuan_price_is_normalized_to_rmb(self) -> None:
        result = self.parser.parse(layout(cell("Gel Butt +¥300")))

        pricing = result.options[0].pricing
        self.assertEqual(pricing.amount, 300)
        self.assertEqual(pricing.currency, "RMB")
        self.assertEqual(pricing.raw_price, "+¥300")

    def test_option_without_price_keeps_raw_value(self) -> None:
        result = self.parser.parse(layout(cell("Eyes Option")))

        option = result.options[0]
        self.assertEqual(option.identity.option_name, "Eyes Option")
        self.assertEqual(option.identity.raw_name, "Eyes Option")
        self.assertIsNone(option.pricing.amount)
        self.assertIsNone(option.pricing.currency)
        self.assertIsNone(option.pricing.raw_price)

    def test_unknown_option_is_retained_as_other(self) -> None:
        result = self.parser.parse(layout(cell("Custom Shoulder Setup")))

        option = result.options[0]
        self.assertEqual(option.identity.option_name, "Custom Shoulder Setup")
        self.assertEqual(option.category, "other")
        self.assertIn("unknown option category", option.warnings)

    def test_raw_coordinate_is_preserved(self) -> None:
        result = self.parser.parse(
            layout(cell("Skin Tone RMB200", coordinate="AZ47"))
        )

        source = result.options[0].source
        self.assertEqual(source.row, 47)
        self.assertEqual(source.column, "AZ")
        self.assertEqual(source.raw_coordinate, "AZ47")

    def test_unparseable_usd_price_adds_warning_without_dropping_option(
        self,
    ) -> None:
        result = self.parser.parse(layout(cell("Color Option US$xxx")))

        option = result.options[0]
        self.assertEqual(option.identity.option_name, "Color Option")
        self.assertEqual(option.pricing.currency, "USD")
        self.assertEqual(option.pricing.raw_price, "US$xxx")
        self.assertIsNone(option.pricing.amount)
        self.assertIn("unable to parse price", option.warnings)

    def test_price_without_name_enters_raw_entries(self) -> None:
        result = self.parser.parse(layout(cell("RMB500", coordinate="C9")))

        self.assertEqual(result.options, ())
        self.assertEqual(len(result.raw_entries), 1)
        entry = result.raw_entries[0]
        self.assertEqual(entry.raw_value, "RMB500")
        self.assertEqual(entry.source.raw_coordinate, "C9")
        self.assertIn("unknown format", entry.warnings)

    def test_parser_performs_no_network_or_file_writes(self) -> None:
        fixture = layout(
            cell("Hands Option"),
            cell("Feet Option +¥100", coordinate="B3"),
        )

        with (
            patch.object(socket, "socket", side_effect=AssertionError("network")),
            patch.object(builtins, "open", side_effect=AssertionError("file I/O")),
        ):
            result = parse_additional_options(fixture)

        self.assertEqual(len(result.options), 2)
        self.assertEqual(result.raw_entries, ())


if __name__ == "__main__":
    unittest.main()
