from __future__ import annotations

import builtins
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.sheet_layout import column_index_to_label  # noqa: E402
from sync_worker.size_list_parser import (  # noqa: E402
    SizeListParser,
    SizeListParserError,
    parse_size_list,
)


HEADERS = (
    "Type",
    "Body type",
    "FOB Price",
    "Upper Chest",
    "Lower Chest",
    "Waist",
    "Hip",
    "Shoulder",
    "Leg Length",
    "Thigh",
    "Arm Length",
    "Sole",
    "N.W.",
    "Oral",
    "Vagina",
    "Anus",
)

COMBINED_HEADERS = (
    "Type\n类型",
    "Body type\n身型",
    "FOB Price\n出厂价格",
    "Upper Chest\n上胸围",
    "Lower Chest\n下胸围",
    "Waist\n腰围",
    "Hip\n臀围",
    "Shoulder\n肩宽",
    "Leg Length\n小腿长度",
    "Thigh\n大腿长度",
    "Arm Length\n手臂长",
    "Sole\n脚板长度",
    "N.W.\n净重",
    "Oral\n口腔深度",
    "Vagina\n阴部深度",
    "Anus\n肛门深度",
)


def cell(
    row: int,
    column_index: int,
    value: str,
    *,
    merged_range: str | None = None,
) -> dict[str, object]:
    column = column_index_to_label(column_index)
    return {
        "coordinate": f"{column}{row}",
        "row": row,
        "column": column,
        "column_index": column_index,
        "formatted_value": value,
        "is_merged": merged_range is not None,
        "is_merge_anchor": merged_range is not None,
        "merged_range": merged_range,
    }


def header_cells(
    row: int = 1, headers: tuple[str, ...] = HEADERS
) -> list[dict[str, object]]:
    return [
        cell(row, column_index, value)
        for column_index, value in enumerate(headers, start=1)
    ]


def merge(
    raw_range: str,
    start_row: int,
    end_row: int,
    start_column: str,
    end_column: str,
) -> dict[str, object]:
    return {
        "range": raw_range,
        "start_row": start_row,
        "end_row": end_row,
        "start_column": start_column,
        "end_column": end_column,
        "anchor": f"{start_column}{start_row}",
    }


def layout(
    *cells: dict[str, object],
    merges: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "status": "ok",
        "non_empty_cells": list(cells),
        "merged_ranges": list(merges or []),
    }


def one_record(
    *extra_cells: dict[str, object],
    body_type: str = "FD155cm",
    type_value: str | None = "Full Silicone",
    fob: str | None = "￥5,500.00",
    headers: tuple[str, ...] = HEADERS,
) -> object:
    cells = header_cells(headers=headers)
    if type_value is not None:
        cells.append(cell(2, 1, type_value))
    cells.append(cell(2, 2, body_type))
    if fob is not None:
        cells.append(cell(2, 3, fob))
    cells.extend(extra_cells)
    return parse_size_list(layout(*cells))[0]


class SizeListParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = SizeListParser()

    def test_01_recognizes_standard_header(self) -> None:
        record = one_record()
        self.assertEqual(record.identity.body_type, "FD155cm")

    def test_02_header_does_not_have_to_be_row_one(self) -> None:
        cells = [cell(1, 1, "Size List Reference"), *header_cells(row=3)]
        cells.extend([cell(4, 1, "Full Silicone"), cell(4, 2, "FD140cm")])
        records = self.parser.parse(layout(*cells))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source.row, 4)

    def test_03_recognizes_bilingual_newline_headers(self) -> None:
        record = one_record(headers=COMBINED_HEADERS)
        self.assertEqual(record.identity.body_type, "FD155cm")

    def test_04_recognizes_reasonable_header_whitespace(self) -> None:
        headers = list(COMBINED_HEADERS)
        headers[1] = "  Body   type \n 身型  "
        record = one_record(headers=tuple(headers))
        self.assertEqual(record.identity.body_type, "FD155cm")

    def test_05_header_matching_is_case_insensitive(self) -> None:
        headers = list(HEADERS)
        headers[0:3] = ["TYPE", "body TYPE", "fob PRICE"]
        record = one_record(headers=tuple(headers))
        self.assertEqual(record.supplier_costs.fob_price.amount, 5500)

    def test_06_missing_core_header_is_an_explicit_error(self) -> None:
        headers = list(HEADERS)
        headers[2] = "Customer Price"
        with self.assertRaisesRegex(SizeListParserError, "Required Size List"):
            one_record(headers=tuple(headers))

    def test_07_each_body_cell_creates_one_record(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "Full Silicone"),
                cell(2, 2, "FD140cm"),
                cell(3, 1, "Full Silicone"),
                cell(3, 2, "FD155cm"),
            ]
        )
        records = self.parser.parse(layout(*cells))
        self.assertEqual([item.identity.body_type for item in records], ["FD140cm", "FD155cm"])

    def test_08_merged_type_is_propagated_within_explicit_range(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "TPE body + Silicone Head", merged_range="A2:A4"),
                cell(2, 2, "FD140cm"),
                cell(3, 2, "FD155cm"),
                cell(4, 2, "SiQ157cm"),
            ]
        )
        records = self.parser.parse(
            layout(*cells, merges=[merge("A2:A4", 2, 4, "A", "A")])
        )
        self.assertEqual(
            [item.classification.type for item in records],
            ["TPE body + Silicone Head"] * 3,
        )

    def test_09_type_is_not_propagated_beyond_merged_range(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "Full Silicone", merged_range="A2:A3"),
                cell(2, 2, "FD140cm"),
                cell(3, 2, "FD155cm"),
                cell(4, 2, "SiQ157cm"),
            ]
        )
        records = self.parser.parse(
            layout(*cells, merges=[merge("A2:A3", 2, 3, "A", "A")])
        )
        self.assertIsNone(records[2].classification.type)
        self.assertIn("missing explicit type classification", records[2].warnings)

    def test_10_raw_type_preserves_supplier_newline(self) -> None:
        raw_type = "Silicone body + vinyl head.\n(J Series Minidoll)"
        record = one_record(type_value=raw_type)
        self.assertEqual(record.classification.raw_type, raw_type)
        self.assertEqual(
            record.classification.type,
            "Silicone body + vinyl head. (J Series Minidoll)",
        )

    def test_11_missing_type_is_null_and_warned(self) -> None:
        record = one_record(type_value=None)
        self.assertIsNone(record.classification.type)
        self.assertIsNone(record.classification.raw_type)
        self.assertIn("missing explicit type classification", record.warnings)

    def test_12_raw_body_type_preserves_hash_and_torso(self) -> None:
        record = one_record(body_type="BW82# Torso")
        self.assertEqual(record.identity.raw_body_type, "BW82# Torso")
        self.assertEqual(record.identity.normalized_body_type, "BW82# Torso")

    def test_13_body_type_preserves_size_suffix(self) -> None:
        for body_type in ("J60cm XS", "Si60cm S", "100cm Plus"):
            with self.subTest(body_type=body_type):
                record = one_record(body_type=body_type)
                self.assertEqual(record.identity.body_type, body_type)

    def test_14_body_type_normalization_only_collapses_whitespace(self) -> None:
        record = one_record(body_type="  J60cm   XS  ")
        self.assertEqual(record.identity.raw_body_type, "  J60cm   XS  ")
        self.assertEqual(record.identity.normalized_body_type, "J60cm XS")
        self.assertEqual(record.identity.comparison_key, "j60cm xs")

    def test_15_fullwidth_yuan_fob_is_parsed_as_supplier_cost(self) -> None:
        record = one_record(fob="￥2,200.00")
        fob = record.supplier_costs.fob_price
        self.assertEqual(fob.amount, 2200)
        self.assertEqual(fob.currency, "RMB")

    def test_16_fob_thousands_separator_is_parsed(self) -> None:
        record = one_record(fob="￥5,500.00")
        self.assertEqual(record.supplier_costs.fob_price.amount, 5500)

    def test_17_fob_raw_value_is_preserved(self) -> None:
        record = one_record(fob="￥5,500.00")
        self.assertEqual(record.supplier_costs.fob_price.raw_value, "￥5,500.00")

    def test_18_fob_exists_only_under_supplier_costs(self) -> None:
        serialized = one_record().to_dict()
        self.assertIn("fob_price", serialized["supplier_costs"])
        text = json.dumps(serialized, ensure_ascii=False).casefold()
        self.assertNotIn("regular_price", text)
        self.assertNotIn("retail_price", text)
        self.assertNotIn("customer_pricing", text)

    def test_19_malformed_fob_is_preserved_and_warned(self) -> None:
        record = one_record(fob="￥ ask sales")
        fob = record.supplier_costs.fob_price
        self.assertIsNone(fob.amount)
        self.assertEqual(fob.raw_value, "￥ ask sales")
        self.assertIn("unable to parse FOB price", record.warnings)

    def test_20_cm_measurement_is_parsed(self) -> None:
        record = one_record(cell(2, 4, "98cm"))
        upper = record.measurements.upper_chest
        self.assertEqual(upper.metric.value, 98)
        self.assertEqual(upper.metric.unit, "cm")

    def test_21_cm_and_inch_are_parsed_without_recalculation(self) -> None:
        record = one_record(cell(2, 4, "98cm\n(38.58in)"))
        upper = record.measurements.upper_chest
        self.assertEqual(upper.metric.value, 98)
        self.assertEqual(upper.imperial.value, 38.58)
        self.assertEqual(upper.raw_value, "98cm\n(38.58in)")

    def test_22_kg_and_lb_are_parsed(self) -> None:
        record = one_record(cell(2, 13, "54kg\n(119.05LB)"))
        weight = record.measurements.net_weight
        self.assertEqual(weight.metric, type(weight.metric)(54, "kg"))
        self.assertEqual(weight.imperial, type(weight.imperial)(119.05, "lb"))

    def test_23_parser_does_not_invent_imperial_value(self) -> None:
        record = one_record(cell(2, 6, "60cm"))
        self.assertEqual(record.measurements.waist.metric.value, 60)
        self.assertIsNone(record.measurements.waist.imperial)

    def test_24_rounding_difference_is_preserved_as_supplied(self) -> None:
        record = one_record(cell(2, 7, "100cm\n(39.36in)"))
        self.assertEqual(record.measurements.hip.metric.value, 100)
        self.assertEqual(record.measurements.hip.imperial.value, 39.36)

    def test_25_slash_normalizes_to_null(self) -> None:
        record = one_record(cell(2, 8, "/"))
        self.assertIsNone(record.measurements.shoulder)

    def test_26_slash_raw_value_is_preserved(self) -> None:
        record = one_record(cell(2, 8, "/"))
        raw = next(item for item in record.raw_measurements if item.fields == ("shoulder",))
        self.assertEqual(raw.raw_value, "/")

    def test_27_merged_upper_and_lower_chest_are_not_duplicated(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "TPE body + Silicone Head"),
                cell(2, 2, "870# Torso"),
                cell(2, 4, "98cm", merged_range="D2:E2"),
            ]
        )
        record = self.parser.parse(
            layout(*cells, merges=[merge("D2:E2", 2, 2, "D", "E")])
        )[0]
        self.assertIsNone(record.measurements.upper_chest)
        self.assertIsNone(record.measurements.lower_chest)
        self.assertEqual(len(record.raw_measurements), 1)

    def test_28_ambiguous_merge_retains_range_and_warning(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "TPE body + Silicone Head"),
                cell(2, 2, "870# Torso"),
                cell(2, 4, "98cm", merged_range="D2:E2"),
            ]
        )
        record = self.parser.parse(
            layout(*cells, merges=[merge("D2:E2", 2, 2, "D", "E")])
        )[0]
        self.assertEqual(record.raw_measurements[0].merged_range, "D2:E2")
        self.assertIn("ambiguous merged measurement D:E", record.warnings)

    def test_29_oral_vagina_and_anus_are_parsed(self) -> None:
        record = one_record(
            cell(2, 14, "9cm"),
            cell(2, 15, "16cm"),
            cell(2, 16, "14cm"),
        )
        self.assertEqual(record.measurements.oral.metric.value, 9)
        self.assertEqual(record.measurements.vagina.metric.value, 16)
        self.assertEqual(record.measurements.anus.metric.value, 14)

    def test_30_torso_missing_measurements_is_safe(self) -> None:
        record = one_record(body_type="870# Torso")
        self.assertIsNone(record.measurements.leg_length)
        self.assertIsNone(record.measurements.sole)

    def test_31_unknown_measurement_column_is_not_dropped(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(1, 17, "Head Circumference"),
                cell(2, 1, "Full Silicone"),
                cell(2, 2, "FD155cm"),
                cell(2, 17, "55cm"),
            ]
        )
        record = self.parser.parse(layout(*cells))[0]
        raw = next(item for item in record.raw_measurements if item.fields[0].startswith("unknown:"))
        self.assertEqual(raw.raw_value, "55cm")
        self.assertTrue(any("unknown measurement header" in item for item in record.warnings))

    def test_32_malformed_measurement_is_raw_and_warned(self) -> None:
        record = one_record(cell(2, 4, "about 98cm"))
        self.assertIsNone(record.measurements.upper_chest)
        self.assertTrue(any("malformed measurement" in item for item in record.warnings))
        self.assertEqual(record.raw_measurements[0].raw_value, "about 98cm")

    def test_33_blank_row_is_skipped(self) -> None:
        cells = header_cells()
        cells.extend([cell(3, 1, "Full Silicone"), cell(3, 2, "FD155cm")])
        records = self.parser.parse(layout(*cells))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source.row, 3)

    def test_34_classification_only_row_is_not_a_product(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "Classic and Pro"),
                cell(3, 1, "Full Silicone"),
                cell(3, 2, "FD155cm"),
            ]
        )
        records = self.parser.parse(layout(*cells))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].identity.body_type, "FD155cm")

    def test_35_source_row_and_coordinates_are_preserved(self) -> None:
        record = one_record(cell(2, 4, "98cm"))
        self.assertEqual(record.source.row, 2)
        self.assertEqual(record.source.coordinates["body_type"], "B2")
        self.assertEqual(record.source.coordinates["upper_chest"], "D2")

    def test_36_merged_type_source_range_and_anchor_are_preserved(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(2, 1, "Full Silicone", merged_range="A2:A3"),
                cell(2, 2, "FD140cm"),
                cell(3, 2, "FD155cm"),
            ]
        )
        records = self.parser.parse(
            layout(*cells, merges=[merge("A2:A3", 2, 3, "A", "A")])
        )
        self.assertEqual(records[1].source.type_merged_range, "A2:A3")
        self.assertEqual(records[1].source.coordinates["type"], "A2")

    def test_37_output_keeps_original_row_order(self) -> None:
        cells = header_cells()
        cells.extend(
            [
                cell(10, 1, "Full Silicone"),
                cell(10, 2, "FD155cm"),
                cell(4, 1, "Full Silicone"),
                cell(4, 2, "FD140cm"),
            ]
        )
        records = self.parser.parse(layout(*cells))
        self.assertEqual([item.source.row for item in records], [4, 10])

    def test_38_parser_performs_no_network_or_file_io(self) -> None:
        fixture = layout(
            *header_cells(),
            cell(2, 1, "Full Silicone"),
            cell(2, 2, "FD155cm"),
        )
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network")),
            patch.object(builtins, "open", side_effect=AssertionError("file I/O")),
        ):
            records = parse_size_list(fixture)
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
