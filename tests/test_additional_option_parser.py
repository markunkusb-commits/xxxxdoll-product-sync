from __future__ import annotations

import builtins
import socket
import sys
import unittest
from decimal import Decimal
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

    def test_fullwidth_yen_decimal_price_is_parsed_as_rmb(self) -> None:
        result = self.parser.parse(
            layout(
                cell("硅胶头植发", coordinate="A2"),
                cell("￥500.00", coordinate="B2"),
            )
        )

        pricing = result.options[0].pricing
        self.assertEqual(pricing.amount, Decimal("500.00"))
        self.assertEqual(pricing.currency, "RMB")

    def test_fullwidth_yen_thousands_separator_is_parsed(self) -> None:
        result = self.parser.parse(
            layout(
                cell("硅胶头植毛", coordinate="A3"),
                cell("￥1,200.00", coordinate="B3"),
            )
        )

        self.assertEqual(
            result.options[0].pricing.amount,
            Decimal("1200.00"),
        )

    def test_fullwidth_yen_small_decimal_price_is_parsed(self) -> None:
        result = self.parser.parse(
            layout(
                cell("肤色选项", coordinate="A4"),
                cell("￥30.00", coordinate="B4"),
            )
        )

        self.assertEqual(result.options[0].pricing.amount, Decimal("30.00"))

    def test_fullwidth_yen_raw_price_is_unchanged(self) -> None:
        result = self.parser.parse(
            layout(
                cell("硅胶头植发", coordinate="A2"),
                cell("￥1,000.00", coordinate="B2"),
            )
        )

        self.assertEqual(result.options[0].pricing.raw_price, "￥1,000.00")

    def test_a_b_region_sets_product_extra_option_category(self) -> None:
        result = self.parser.parse(
            layout(
                cell("硅胶头植发", coordinate="A2"),
                cell("￥500.00", coordinate="B2"),
            )
        )

        self.assertEqual(result.options[0].category, "product_extra_option")

    def test_a_b_category_does_not_depend_on_chinese_option_name(self) -> None:
        fixture = layout(
            cell("硅胶头植毛", coordinate="A3"),
            cell("￥300.00", coordinate="B3"),
            cell("Si70系列硅胶头", coordinate="A14"),
            cell("￥50.00", coordinate="B14"),
        )

        result = self.parser.parse(fixture)

        self.assertEqual(
            [option.category for option in result.options],
            ["product_extra_option", "product_extra_option"],
        )

    def test_d_e_region_sets_accessory_category(self) -> None:
        result = self.parser.parse(
            layout(
                cell("挂钩", coordinate="D2"),
                cell("￥50.00", coordinate="E2"),
            )
        )

        self.assertEqual(result.options[0].category, "accessory")

    def test_structured_region_does_not_add_unknown_category_warning(self) -> None:
        result = self.parser.parse(
            layout(
                cell("无法推断的中文自定义项", coordinate="A5"),
                cell("￥30.00", coordinate="B5"),
            )
        )

        option = result.options[0]
        self.assertEqual(option.category, "product_extra_option")
        self.assertNotIn("unknown option category", option.warnings)

    def test_unknown_secondary_category_is_not_guessed(self) -> None:
        result = self.parser.parse(
            layout(
                cell("硅胶头植发", coordinate="A2"),
                cell("￥500.00", coordinate="B2"),
            )
        )

        option = result.options[0]
        self.assertFalse(hasattr(option, "secondary_category"))
        self.assertEqual(option.warnings, ())

    def test_merged_fullwidth_price_is_consumed_once_and_range_is_kept(
        self,
    ) -> None:
        fixture = {
            "non_empty_cells": [
                cell("硅胶头植发", coordinate="A3"),
                {
                    **cell("￥500.00", coordinate="B3"),
                    "merged_range": "B3:B4",
                },
                cell("硅胶头植毛", coordinate="A4"),
            ],
            "merged_ranges": [
                {
                    "range": "B3:B4",
                    "start_row": 3,
                    "end_row": 4,
                    "start_column": "B",
                    "end_column": "B",
                }
            ],
        }

        result = self.parser.parse(fixture)
        priced = [
            option
            for option in result.options
            if option.pricing.amount == Decimal("500.00")
        ]

        self.assertEqual(len(priced), 1)
        self.assertEqual(
            [option.pricing.price_range for option in result.options],
            ["B3:B4", "B3:B4"],
        )
        self.assertIn(
            "merged price range requires review",
            result.options[0].warnings,
        )
        self.assertIn(
            "merged price range not reused",
            result.options[1].warnings,
        )


if __name__ == "__main__":
    unittest.main()
