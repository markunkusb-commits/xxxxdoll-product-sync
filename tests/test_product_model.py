from __future__ import annotations

import builtins
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.clm_price_parser import (  # noqa: E402
    BlockSource,
    CLMProductBlock,
    ParsedPrice,
    Pricing,
    RawCommercialEntry,
    RawSpecification,
    UpgradeOption,
)
from sync_worker.product_model import from_clm_product  # noqa: E402


def _money(raw_value: str, currency: str, amount: int, context: str) -> ParsedPrice:
    return ParsedPrice(
        raw_value=raw_value,
        currency=currency,
        amount=amount,
        context=context,
    )


def _clm_product() -> CLMProductBlock:
    return CLMProductBlock(
        series="pro",
        raw_series_title="⭐CLM Pro⭐",
        model="P-170",
        model_raw="P-170",
        cup=None,
        specifications={"model": "P-170", "torso_depth": "21cm"},
        raw_specifications=[
            RawSpecification(
                field="Torso Depth",
                value="21cm",
                field_coordinate="I10",
                value_coordinate="O10",
            )
        ],
        included_features=["EVO skeleton"],
        upgrade_options=[
            UpgradeOption(
                name="Gel butt",
                raw_value="Gel butt +¥300",
                price=_money("+¥300", "CNY", 300, "upgrade_option"),
            )
        ],
        notices=["Reference notice"],
        pricing=Pricing(
            fob_unit_price=_money("RMB2250", "RMB", 2250, "fob_unit_price"),
            minimum_retail_price=_money(
                "US$850", "USD", 850, "minimum_retail_price"
            ),
            normal_options_price=_money(
                "¥2500", "CNY", 2500, "normal_options_price"
            ),
            body_only_price=_money(
                "FOB Unit Price RMB2000",
                "RMB",
                2000,
                "body_only_price",
            ),
            including_head_price=_money(
                "FOB Unit Price RMB2750",
                "RMB",
                2750,
                "including_head_price",
            ),
        ),
        photo_download_link="[URL_REDACTED]",
        raw_commercial_entries=[
            RawCommercialEntry(
                field="Packing Rule",
                value="Manual review",
                coordinate="AH18",
            )
        ],
        source=BlockSource(start_row=8, end_row=19),
        warnings=["Unknown specification preserved: Torso Depth"],
    )


class ProductIntermediateModelTests(unittest.TestCase):
    def test_fob_costs_never_enter_retail_pricing(self) -> None:
        record = from_clm_product(_clm_product())
        serialized = record.to_dict()

        self.assertEqual(record.supplier_costs.fob_unit_price.amount, 2250)
        self.assertEqual(record.supplier_costs.body_only_fob.amount, 2000)
        self.assertEqual(record.supplier_costs.including_head_fob.amount, 2750)
        self.assertNotIn("fob_unit_price", serialized["retail_pricing"])
        self.assertNotIn("body_only_fob", serialized["retail_pricing"])

    def test_minimum_retail_price_enters_retail_pricing(self) -> None:
        record = from_clm_product(_clm_product())

        retail = record.retail_pricing.minimum_retail_price
        self.assertEqual(retail.raw_value, "US$850")
        self.assertEqual(retail.currency, "USD")
        self.assertEqual(retail.amount, 850)

    def test_upgrade_options_and_normal_option_price_are_preserved(self) -> None:
        record = from_clm_product(_clm_product())

        self.assertEqual(record.options.normal_options_price.amount, 2500)
        self.assertEqual(len(record.options.upgrade_options), 1)
        upgrade = record.options.upgrade_options[0]
        self.assertEqual(upgrade.name, "Gel butt")
        self.assertEqual(upgrade.raw_value, "Gel butt +¥300")
        self.assertEqual(upgrade.supplier_cost.amount, 300)

    def test_unknown_specs_and_commercial_fields_are_preserved(self) -> None:
        record = from_clm_product(_clm_product())

        self.assertEqual(record.specifications.normalized["torso_depth"], "21cm")
        self.assertEqual(record.specifications.raw[0].field, "Torso Depth")
        unknown = record.unknown_fields.raw_commercial_entries[0]
        self.assertEqual(unknown.field, "Packing Rule")
        self.assertEqual(unknown.value, "Manual review")
        self.assertEqual(unknown.coordinate, "AH18")

    def test_source_rows_are_preserved_exactly(self) -> None:
        record = from_clm_product(_clm_product())

        self.assertEqual(record.source.start_row, 8)
        self.assertEqual(record.source.end_row, 19)

    def test_conversion_performs_no_file_or_network_io(self) -> None:
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network")),
            patch.object(builtins, "open", side_effect=AssertionError("file I/O")),
        ):
            record = from_clm_product(_clm_product())

        self.assertEqual(record.identity.series, "pro")


if __name__ == "__main__":
    unittest.main()
