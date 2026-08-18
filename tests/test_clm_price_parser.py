from __future__ import annotations

import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.clm_price_parser import (  # noqa: E402
    CLMPriceListParser,
    CLMPriceParserError,
    parse_clm_price_layout,
    parse_price,
    recognize_series_title,
)
from sync_worker.product_model import from_clm_product  # noqa: E402
from sync_worker.sheet_layout import column_index_to_label  # noqa: E402


def cell(
    row: int,
    column_index: int,
    value: str,
    *,
    merged_range: str | None = None,
) -> dict[str, object]:
    return {
        "coordinate": f"{column_index_to_label(column_index)}{row}",
        "row": row,
        "column": column_index_to_label(column_index),
        "column_index": column_index,
        "formatted_value": value,
        "is_merged": merged_range is not None,
        "is_merge_anchor": merged_range is not None,
        "merged_range": merged_range,
    }


def layout(*cells: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "non_empty_cells": list(cells)}


def block(
    start_row: int,
    title: str,
    *,
    specification_rows: list[list[tuple[str, str]]] | None = None,
    features: list[str] | None = None,
    upgrades: list[str | tuple[str, str]] | None = None,
    prices: list[tuple[str, str]] | None = None,
    notices: list[str] | None = None,
    unknown_commercial: list[str] | None = None,
    photo_url: str | None = "https://example.invalid/photo",
    gap_before_photo: int = 0,
) -> tuple[list[dict[str, object]], int]:
    result = [cell(start_row, 2, title)]
    row = start_row + 1
    for pairs in specification_rows or []:
        positions = ((9, 15), (23, 28))
        for (field_name, value), (field_column, value_column) in zip(
            pairs, positions, strict=False
        ):
            result.extend(
                [cell(row, field_column, field_name), cell(row, value_column, value)]
            )
        row += 1
    if features is not None:
        result.append(cell(row, 34, "Price includes the following:"))
        row += 1
        for feature in features:
            result.append(cell(row, 34, feature))
            row += 1
    if upgrades is not None:
        result.append(cell(row, 34, "Upgrade options"))
        row += 1
        for upgrade in upgrades:
            if isinstance(upgrade, tuple):
                result.extend(
                    [cell(row, 34, upgrade[0]), cell(row, 45, upgrade[1])]
                )
            else:
                result.append(cell(row, 34, upgrade))
            row += 1
    for price_name, price_value in prices or []:
        result.extend([cell(row, 34, price_name), cell(row, 45, price_value)])
        row += 1
    for notice in notices or []:
        result.append(cell(row, 34, notice))
        row += 1
    for entry in unknown_commercial or []:
        result.append(cell(row, 34, entry))
        row += 1
    row += gap_before_photo
    result.append(cell(row, 2, "Photo download link"))
    if photo_url is not None:
        result.append(cell(row + 1, 9, photo_url))
        end_row = row + 1
    else:
        end_row = row
    return result, end_row


def parallel_commercial_layout() -> dict[str, object]:
    return layout(
        cell(2, 2, "CLM Ultra"),
        cell(
            3,
            34,
            "Price includes the following:",
            merged_range="AH3:AR3",
        ),
        cell(3, 45, "Upgrade options", merged_range="AS3:AZ3"),
        cell(4, 34, "1: articulated fingers"),
        cell(4, 45, "1. Gel Butt"),
        cell(5, 34, "2: real oral sex"),
        cell(5, 45, "2.Hair Implant"),
        cell(6, 34, "3: movable jaw"),
        cell(6, 45, "3.Eyebrows/Eyelashes Implant"),
        cell(7, 34, "4: realistic body makeup"),
        cell(7, 45, "4. Hard Hands and Feet"),
        cell(8, 34, "5: simulated scalp wig"),
        cell(9, 2, "Photo download link"),
    )


class CLMPriceParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = CLMPriceListParser()

    def test_recognizes_all_four_decorated_series_titles(self) -> None:
        cases = {
            "◆ CLM Classic ◆": "classic",
            "⭐CLM Pro⭐": "pro",
            "● CLM ULW ●": "ulw",
            "❤️CLM Ultra❤️": "ultra",
        }
        for raw_title, expected in cases.items():
            with self.subTest(raw_title=raw_title):
                self.assertEqual(recognize_series_title(raw_title), expected)

    def test_preserves_raw_series_title(self) -> None:
        cells, _ = block(8, "❤️CLM Ultra❤️")

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.series, "ultra")
        self.assertEqual(product.raw_series_title, "❤️CLM Ultra❤️")

    def test_commercial_mention_of_series_does_not_start_a_block(self) -> None:
        cells, _ = block(
            2,
            "CLM Classic",
            unknown_commercial=["Compatible with CLM Pro options"],
        )

        products = self.parser.parse(layout(*cells))

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].series, "classic")

    def test_interleaved_series_follow_dynamic_titles(self) -> None:
        all_cells: list[dict[str, object]] = []
        row = 2
        expected = ["classic", "ultra", "pro", "classic", "ulw", "ultra"]
        titles = [
            "CLM Classic",
            "CLM Ultra",
            "CLM Pro",
            "◆ CLM Classic ◆",
            "CLM ULW",
            "❤️CLM Ultra❤️",
        ]
        for title in titles:
            current, end = block(row, title, specification_rows=[[("Model", f"M{row}")]])
            all_cells.extend(current)
            row = end + 2

        products = self.parser.parse(layout(*all_cells))

        self.assertEqual([item.series for item in products], expected)

    def _assert_consecutive_series(self, title: str, expected: str) -> None:
        first, first_end = block(
            2, title, specification_rows=[[("Model", "First")]]
        )
        second, _ = block(
            first_end + 1, title, specification_rows=[[("Model", "Second")]]
        )

        products = self.parser.parse(layout(*first, *second))

        self.assertEqual([item.series for item in products], [expected, expected])
        self.assertEqual([item.model for item in products], ["First", "Second"])

    def test_two_consecutive_pro_blocks(self) -> None:
        self._assert_consecutive_series("⭐CLM Pro⭐", "pro")

    def test_two_consecutive_ulw_blocks(self) -> None:
        self._assert_consecutive_series("● CLM ULW ●", "ulw")

    def test_two_consecutive_classic_blocks(self) -> None:
        self._assert_consecutive_series("◆ CLM Classic ◆", "classic")

    def test_two_consecutive_ultra_blocks(self) -> None:
        self._assert_consecutive_series("❤️CLM Ultra❤️", "ultra")

    def test_variable_block_lengths_do_not_change_boundaries(self) -> None:
        short, short_end = block(
            2,
            "CLM Pro",
            specification_rows=[[("Model", "P1")]],
        )
        long, long_end = block(
            short_end + 1,
            "CLM Ultra",
            specification_rows=[
                [("Cup", "D"), ("Height", "165cm")],
                [("Waist", "60cm"), ("Hip", "90cm")],
                [("N.W", "28kg"), ("G.W", "34kg")],
            ],
            features=["Full Silicone", "EVO skeleton", "movable jaw"],
            upgrades=["Gel Butt +¥300", ("Hair Implant", "RMB500")],
        )
        final, _ = block(long_end + 1, "CLM Classic")

        products = self.parser.parse(layout(*short, *long, *final))

        self.assertEqual(len(products), 3)
        self.assertGreater(
            products[1].source.end_row - products[1].source.start_row,
            products[0].source.end_row - products[0].source.start_row,
        )

    def test_blank_row_is_not_a_block_boundary(self) -> None:
        cells, _ = block(
            5,
            "CLM Pro",
            specification_rows=[
                [("Model", "P100")],
                [("Waist", "58cm")],
            ],
            gap_before_photo=3,
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.model, "P100")
        self.assertEqual(product.specifications["waist"], "58cm")

    def test_photo_download_link_is_associated_and_redacted(self) -> None:
        cells, _ = block(8, "CLM Pro", photo_url="https://example.invalid/pro")

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.photo_download_link, "[URL_REDACTED]")
        self.assertNotIn("example.invalid", repr(product))

    def test_photo_label_without_url_adds_warning(self) -> None:
        cells, _ = block(8, "CLM ULW", photo_url=None)

        product = self.parser.parse(layout(*cells))[0]

        self.assertIsNone(product.photo_download_link)
        self.assertTrue(any("without a safe URL" in item for item in product.warnings))

    def test_independent_model_field_populates_model(self) -> None:
        cells, _ = block(
            8,
            "CLM Pro",
            specification_rows=[[("Model", "PRO-170-A"), ("Height", "170cm")]],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.model, "PRO-170-A")
        self.assertEqual(product.model_raw, "PRO-170-A")
        self.assertEqual(product.specifications["model"], "PRO-170-A")

    def test_height_model_is_not_split_or_guessed(self) -> None:
        cells, _ = block(
            8,
            "CLM Classic",
            specification_rows=[[("Height(Model)", "S170cm AT")]],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertIsNone(product.model)
        self.assertNotIn("height", product.specifications)
        self.assertEqual(product.specifications["height_model"], "S170cm AT")
        self.assertTrue(any("without splitting" in item for item in product.warnings))

    def test_left_and_right_specification_pairs_keep_coordinates(self) -> None:
        cells, _ = block(
            9,
            "CLM Ultra",
            specification_rows=[
                [("Height(Model)", "J59cm"), ("Upper Chest", "33cm")]
            ],
        )

        product = self.parser.parse(layout(*cells))[0]
        raw = product.raw_specifications

        self.assertEqual(product.specifications["height_model"], "J59cm")
        self.assertEqual(product.specifications["upper_chest"], "33cm")
        self.assertEqual(raw[0].field_coordinate, "I10")
        self.assertEqual(raw[0].value_coordinate, "O10")
        self.assertEqual(raw[1].field_coordinate, "W10")
        self.assertEqual(raw[1].value_coordinate, "AB10")

    def test_variable_specification_count_is_supported(self) -> None:
        cells, _ = block(
            2,
            "CLM ULW",
            specification_rows=[
                [("Model", "U1"), ("Cup", "C")],
                [("Height", "155cm"), ("Waist", "52cm")],
                [("Hip", "82cm"), ("Carton Size", "120x40x35cm")],
            ],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(len(product.raw_specifications), 6)
        self.assertEqual(product.cup, "C")

    def test_unknown_specification_is_preserved_with_warning(self) -> None:
        cells, _ = block(
            2,
            "CLM Pro",
            specification_rows=[[("Torso Depth", "21cm")]],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.specifications["torso_depth"], "21cm")
        self.assertEqual(product.raw_specifications[0].field, "Torso Depth")
        self.assertTrue(any("Unknown specification" in item for item in product.warnings))

    def test_missing_first_value_does_not_shift_second_pair_left(self) -> None:
        cells = [
            cell(2, 2, "CLM Pro"),
            cell(3, 9, "Waist"),
            cell(3, 23, "Hip"),
            cell(3, 28, "90cm"),
            cell(4, 2, "Photo download link"),
        ]

        product = self.parser.parse(layout(*cells))[0]

        self.assertNotIn("waist", product.specifications)
        self.assertEqual(product.specifications["hip"], "90cm")

    def test_feature_count_is_dynamic(self) -> None:
        cells, _ = block(
            2,
            "CLM Ultra",
            features=[
                "Full Silicone",
                "articulated Fingers",
                "real oral sex",
                "movable jaw",
                "EVO skeleton",
            ],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(len(product.included_features), 5)
        self.assertIn("EVO skeleton", product.included_features)

    def test_heart_only_commercial_feature_is_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["❤", "EVO skeleton"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["EVO skeleton"])

    def test_star_only_commercial_feature_is_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["⭐", "EVO skeleton"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["EVO skeleton"])

    def test_diamond_only_commercial_feature_is_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["◆", "EVO skeleton"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["EVO skeleton"])

    def test_whitespace_wrapped_heart_is_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=[" \t❤️ \n", "EVO skeleton"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["EVO skeleton"])

    def test_variation_selector_heart_is_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["\u2764\ufe0f", "EVO skeleton"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["EVO skeleton"])

    def test_gel_butt_text_is_not_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["Gel Butt"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["Gel Butt"])

    def test_business_text_with_digit_is_not_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["3D Soft vagina"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["3D Soft vagina"])

    def test_business_text_with_plus_is_not_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", features=["Plus+"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["Plus+"])

    def test_upgrade_text_with_symbol_and_price_is_not_filtered(self) -> None:
        cells, _ = block(2, "CLM Ultra", upgrades=["Gel Butt +¥300"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.upgrade_options[0].name, "Gel Butt")
        self.assertEqual(product.upgrade_options[0].price.amount, 300)

    def test_filtered_decoration_is_preserved_in_raw_commercial_source(
        self,
    ) -> None:
        cells, _ = block(2, "CLM Ultra", features=["❤️", "EVO skeleton"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertNotIn("❤️", product.included_features)
        self.assertTrue(
            any(
                entry.field == "included_feature"
                and entry.value == "❤️"
                and entry.coordinate == "AH4"
                for entry in product.raw_commercial_entries
            )
        )

    def test_upgrade_name_and_raw_value_normalization_is_unchanged(self) -> None:
        cells, _ = block(2, "CLM Ultra", upgrades=["1. Gel Butt"])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.upgrade_options[0].name, "Gel Butt")
        self.assertEqual(product.upgrade_options[0].raw_value, "1. Gel Butt")

    def test_parallel_commercial_columns_do_not_join_same_row_values(
        self,
    ) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertEqual(len(product.included_features), 5)
        self.assertEqual(len(product.upgrade_options), 4)
        self.assertTrue(
            all("|" not in option.name for option in product.upgrade_options)
        )

    def test_real_oral_sex_and_gel_butt_are_separate_business_roles(
        self,
    ) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertIn("real oral sex", product.included_features)
        self.assertEqual(product.upgrade_options[0].name, "Gel Butt")
        self.assertNotIn("Gel Butt", product.included_features)

    def test_movable_jaw_and_hair_implant_are_separate_business_roles(
        self,
    ) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertIn("movable jaw", product.included_features)
        self.assertEqual(product.upgrade_options[1].name, "Hair Implant")
        self.assertNotIn("movable jaw", [
            option.name for option in product.upgrade_options
        ])

    def test_body_makeup_and_eyebrow_implant_are_separate_business_roles(
        self,
    ) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertIn("realistic body makeup", product.included_features)
        self.assertEqual(
            product.upgrade_options[2].name,
            "Eyebrows/Eyelashes Implant",
        )

    def test_scalp_wig_and_hard_hands_are_separate_business_roles(self) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertIn("simulated scalp wig", product.included_features)
        self.assertEqual(
            product.upgrade_options[3].name,
            "Hard Hands and Feet",
        )

    def test_left_commercial_band_can_contain_only_an_included_feature(
        self,
    ) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertEqual(product.included_features[-1], "simulated scalp wig")
        self.assertEqual(len(product.upgrade_options), 4)

    def test_right_commercial_band_can_contain_only_an_upgrade(self) -> None:
        fixture = parallel_commercial_layout()
        fixture["non_empty_cells"].insert(
            -1,
            cell(8, 45, "5. Extra Upgrade"),
        )

        product = self.parser.parse(fixture)[0]

        self.assertEqual(product.upgrade_options[-1].name, "Extra Upgrade")
        self.assertNotIn("Extra Upgrade", product.included_features)

    def test_numeric_dot_and_colon_prefixes_are_safely_removed(self) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertEqual(
            product.included_features[:2],
            ["articulated fingers", "real oral sex"],
        )
        self.assertEqual(
            [option.name for option in product.upgrade_options[:2]],
            ["Gel Butt", "Hair Implant"],
        )

    def test_sequence_normalization_preserves_raw_commercial_sources(
        self,
    ) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]
        raw_by_coordinate = {
            entry.coordinate: (entry.field, entry.value)
            for entry in product.raw_commercial_entries
        }

        self.assertEqual(
            raw_by_coordinate["AH5"],
            ("included_feature", "2: real oral sex"),
        )
        self.assertEqual(
            raw_by_coordinate["AS5"],
            ("upgrade_option", "2.Hair Implant"),
        )
        self.assertEqual(
            product.upgrade_options[1].raw_value,
            "2.Hair Implant",
        )

    def test_included_features_never_enter_upgrade_options(self) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        upgrade_names = {option.name for option in product.upgrade_options}
        self.assertTrue(set(product.included_features).isdisjoint(upgrade_names))
        self.assertNotIn("articulated fingers", upgrade_names)

    def test_upgrade_options_never_enter_included_features(self) -> None:
        product = self.parser.parse(parallel_commercial_layout())[0]

        self.assertNotIn("Gel Butt", product.included_features)
        self.assertNotIn("Hair Implant", product.included_features)
        self.assertNotIn("Hard Hands and Feet", product.included_features)

    def test_product_model_conversion_keeps_commercial_roles_separate(
        self,
    ) -> None:
        parsed = self.parser.parse(parallel_commercial_layout())[0]
        record = from_clm_product(parsed)

        self.assertEqual(record.included_features, tuple(parsed.included_features))
        self.assertEqual(
            [option.name for option in record.options.upgrade_options],
            [
                "Gel Butt",
                "Hair Implant",
                "Eyebrows/Eyelashes Implant",
                "Hard Hands and Feet",
            ],
        )
        self.assertEqual(
            record.unknown_fields.raw_commercial_entries[0].coordinate,
            "AH4",
        )

    def test_upgrade_option_count_is_dynamic(self) -> None:
        cells, _ = block(
            2,
            "CLM Classic",
            upgrades=[
                "Gel butt +¥300",
                "Implant Hair (¥500)",
                ("Hard Hands and Feet", "RMB200"),
            ],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(len(product.upgrade_options), 3)
        self.assertEqual(product.upgrade_options[0].name, "Gel butt")
        self.assertEqual(product.upgrade_options[0].price.amount, 300)
        self.assertEqual(product.upgrade_options[1].price.amount, 500)

    def test_unknown_commercial_entry_is_not_dropped(self) -> None:
        cells, _ = block(
            2,
            "CLM Pro",
            unknown_commercial=["Special packing rule applies"],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(len(product.raw_commercial_entries), 1)
        self.assertEqual(
            product.raw_commercial_entries[0].value,
            "Special packing rule applies",
        )

    def test_confirmed_ag_more_collocation_field_is_preserved(self) -> None:
        cells = [
            cell(8, 2, "◆ CLM Classic ◆"),
            cell(8, 33, "More collocation"),
            cell(9, 2, "Photo download link"),
        ]

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.raw_commercial_entries[0].value, "More collocation")
        self.assertEqual(product.raw_commercial_entries[0].coordinate, "AG8")

    def test_commercial_semantics_work_when_section_moves_left(self) -> None:
        cells = [
            cell(2, 2, "CLM Ultra"),
            cell(3, 4, "Price includes the following:"),
            cell(4, 4, "EVO skeleton"),
            cell(5, 4, "FOB Unit Price"),
            cell(5, 15, "US$270"),
            cell(6, 2, "Photo download link"),
        ]

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.included_features, ["EVO skeleton"])
        self.assertEqual(product.pricing.fob_unit_price.amount, 270)

    def test_fob_unit_price_is_parsed(self) -> None:
        cells, _ = block(
            2, "CLM Pro", prices=[("FOB Unit Price", "RMB2250")]
        )

        price = self.parser.parse(layout(*cells))[0].pricing.fob_unit_price

        self.assertEqual(price.currency, "RMB")
        self.assertEqual(price.amount, 2250)
        self.assertEqual(price.context, "fob_unit_price")

    def test_minimum_retail_price_is_parsed(self) -> None:
        cells, _ = block(
            2,
            "CLM Pro",
            prices=[("Minimum Retail Price", "US$850")],
        )

        price = self.parser.parse(layout(*cells))[0].pricing.minimum_retail_price

        self.assertEqual(price.currency, "USD")
        self.assertEqual(price.amount, 850)

    def test_minimum_retail_label_and_value_on_separate_rows_are_parsed(
        self,
    ) -> None:
        cells = [
            cell(2, 2, "CLM Pro"),
            cell(3, 34, "Minimum Retail Price"),
            cell(4, 34, "US$270"),
            cell(5, 2, "Photo download link"),
        ]

        price = self.parser.parse(layout(*cells))[0].pricing.minimum_retail_price

        self.assertEqual(price.raw_value, "US$270")
        self.assertEqual(price.currency, "USD")
        self.assertEqual(price.amount, 270)

    def test_normal_options_price_is_parsed(self) -> None:
        cells, _ = block(
            2,
            "CLM Classic",
            prices=[("Normal options Price", "¥2500")],
        )

        price = self.parser.parse(layout(*cells))[0].pricing.normal_options_price

        self.assertEqual(price.currency, "CNY")
        self.assertEqual(price.amount, 2500)

    def test_normal_options_label_and_yuan_value_on_separate_rows_are_parsed(
        self,
    ) -> None:
        cells = [
            cell(2, 2, "CLM Classic"),
            cell(3, 34, "Normal options Price"),
            cell(4, 34, "¥2500"),
            cell(5, 2, "Photo download link"),
        ]

        price = self.parser.parse(layout(*cells))[0].pricing.normal_options_price

        self.assertEqual(price.raw_value, "¥2500")
        self.assertEqual(price.currency, "CNY")
        self.assertEqual(price.amount, 2500)

    def test_body_only_and_including_head_contexts_are_separate(self) -> None:
        cells, _ = block(
            2,
            "CLM ULW",
            prices=[
                ("Only Body", "RMB500"),
                ("Price including head", "RMB800"),
            ],
        )

        pricing = self.parser.parse(layout(*cells))[0].pricing

        self.assertEqual(pricing.body_only_price.amount, 500)
        self.assertEqual(pricing.body_only_price.context, "body_only_price")
        self.assertEqual(pricing.including_head_price.amount, 800)
        self.assertEqual(
            pricing.including_head_price.context, "including_head_price"
        )

    def test_fob_prices_use_following_body_and_head_context_rows(self) -> None:
        cells = [
            cell(2, 2, "CLM Ultra"),
            cell(3, 34, "FOB Unit Price RMB2250"),
            cell(4, 34, "(Only Body)"),
            cell(5, 34, "FOB Unit Price RMB2750"),
            cell(6, 34, "(Price including head)"),
            cell(7, 2, "Photo download link"),
        ]

        pricing = self.parser.parse(layout(*cells))[0].pricing

        self.assertIsNone(pricing.fob_unit_price)
        self.assertEqual(pricing.body_only_price.raw_value, "FOB Unit Price RMB2250")
        self.assertEqual(pricing.body_only_price.currency, "RMB")
        self.assertEqual(pricing.body_only_price.amount, 2250)
        self.assertEqual(
            pricing.including_head_price.raw_value, "FOB Unit Price RMB2750"
        )
        self.assertEqual(pricing.including_head_price.currency, "RMB")
        self.assertEqual(pricing.including_head_price.amount, 2750)

    def test_ambiguous_price_is_preserved_as_raw_commercial_entry(self) -> None:
        cells = [
            cell(2, 2, "CLM Pro"),
            cell(3, 34, "Minimum Retail Price"),
            cell(4, 34, "Ask sales"),
            cell(5, 2, "Photo download link"),
        ]

        product = self.parser.parse(layout(*cells))[0]

        self.assertIsNone(product.pricing.minimum_retail_price)
        self.assertEqual(len(product.raw_commercial_entries), 1)
        self.assertEqual(
            product.raw_commercial_entries[0].field, "Minimum Retail Price"
        )
        self.assertEqual(product.raw_commercial_entries[0].value, "Ask sales")
        self.assertTrue(any("Ambiguous price" in item for item in product.warnings))

    def test_pricing_normalization_does_not_change_block_boundaries(self) -> None:
        first = [
            cell(8, 2, "◆ CLM Classic ◆"),
            cell(9, 34, "Minimum Retail Price"),
            cell(10, 34, "US$270"),
            cell(11, 2, "Photo download link"),
        ]
        second = [
            cell(20, 2, "⭐CLM Pro⭐"),
            cell(21, 34, "FOB Unit Price RMB2250"),
            cell(22, 34, "(Only Body)"),
            cell(23, 2, "Photo download link"),
        ]

        products = self.parser.parse(layout(*first, *second))

        self.assertEqual([item.series for item in products], ["classic", "pro"])
        self.assertEqual(products[0].source.start_row, 8)
        self.assertEqual(products[0].source.end_row, 19)
        self.assertEqual(products[1].source.start_row, 20)
        self.assertEqual(products[1].source.end_row, 23)

    def test_rmb_usd_and_yuan_price_tokens_are_supported(self) -> None:
        cases = (
            ("RMB500", "RMB", 500),
            ("US$270", "USD", 270),
            ("¥2500", "CNY", 2500),
            ("+¥300", "CNY", 300),
        )
        for raw_value, currency, amount in cases:
            with self.subTest(raw_value=raw_value):
                price = parse_price(raw_value, context="test")
                self.assertEqual(price.currency, currency)
                self.assertEqual(price.amount, amount)
                self.assertEqual(price.raw_value, raw_value)

    def test_unparseable_price_is_preserved_with_warning(self) -> None:
        cells, _ = block(
            2,
            "CLM Pro",
            prices=[("FOB Unit Price", "Ask sales")],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertIsNone(product.pricing.fob_unit_price)
        self.assertEqual(len(product.raw_commercial_entries), 1)
        self.assertEqual(
            product.raw_commercial_entries[0].field, "FOB Unit Price"
        )
        self.assertEqual(product.raw_commercial_entries[0].value, "Ask sales")
        self.assertTrue(any("Ambiguous price" in item for item in product.warnings))

    def test_upgrade_price_is_not_misclassified_as_fob(self) -> None:
        cells, _ = block(
            2,
            "CLM Classic",
            upgrades=["Gel butt +¥300"],
        )

        product = self.parser.parse(layout(*cells))[0]

        self.assertIsNone(product.pricing.fob_unit_price)
        self.assertEqual(product.upgrade_options[0].price.context, "upgrade_option")

    def test_notice_is_extracted_without_business_inference(self) -> None:
        notice = "The wig in photo can only be implanted"
        cells, _ = block(2, "CLM Classic", notices=[notice])

        product = self.parser.parse(layout(*cells))[0]

        self.assertEqual(product.notices, [notice])

    def test_source_rows_trace_hard_series_boundaries(self) -> None:
        first, _ = block(8, "CLM Pro", gap_before_photo=2)
        second, second_end = block(20, "CLM ULW")

        products = self.parser.parse(layout(*first, *second))

        self.assertEqual(products[0].source.start_row, 8)
        self.assertEqual(products[0].source.end_row, 19)
        self.assertEqual(products[1].source.start_row, 20)
        self.assertEqual(products[1].source.end_row, second_end)

    def test_product_model_is_json_serializable(self) -> None:
        cells, _ = block(
            2,
            "CLM Ultra",
            specification_rows=[[("Cup", "D"), ("N.W", "28kg")]],
            prices=[("FOB Unit Price", "US$270")],
        )

        product = self.parser.parse(layout(*cells))[0]
        serialized = json.dumps(product.to_dict(), ensure_ascii=False)

        self.assertIn('"series": "ultra"', serialized)
        self.assertIn('"net_weight": "28kg"', serialized)

    def test_urls_and_long_keys_are_redacted_without_false_positives(self) -> None:
        secret_key = "ck_" + "a" * 24
        cells, _ = block(
            2,
            "CLM Pro",
            unknown_commercial=[
                f"stock_status ck_short {secret_key} https://example.invalid/private"
            ],
        )

        product = self.parser.parse(layout(*cells))[0]
        serialized = json.dumps(product.to_dict(), ensure_ascii=False)

        self.assertIn("stock_status", serialized)
        self.assertIn("ck_short", serialized)
        self.assertNotIn(secret_key, serialized)
        self.assertNotIn("example.invalid", serialized)
        self.assertIn("[URL_REDACTED]", serialized)

    def test_public_repr_and_output_never_contain_photo_url(self) -> None:
        supplier_url = "https://example.invalid/vendor/private-photo"
        cells, _ = block(2, "CLM Ultra", photo_url=supplier_url)

        product = self.parser.parse(layout(*cells))[0]
        combined = repr(product) + json.dumps(product.to_dict())

        self.assertNotIn(supplier_url, combined)
        self.assertNotIn("example.invalid", combined)
        self.assertIn("[URL_REDACTED]", combined)

    def test_parser_makes_no_network_calls_and_has_no_write_counter(self) -> None:
        cells, _ = block(2, "CLM Pro")

        with patch.object(socket, "socket", side_effect=AssertionError("network")):
            products = parse_clm_price_layout(layout(*cells))

        self.assertEqual(len(products), 1)
        self.assertNotIn("write_requests_performed", products[0].to_dict())

    def test_no_series_returns_an_empty_product_list(self) -> None:
        result = self.parser.parse(layout(cell(2, 9, "Height"), cell(2, 15, "170cm")))

        self.assertEqual(result, [])

    def test_malformed_layout_raises_only_safe_structural_error(self) -> None:
        with self.assertRaisesRegex(CLMPriceParserError, "non_empty_cells") as caught:
            self.parser.parse({"secret": "https://example.invalid"})

        self.assertNotIn("example.invalid", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
