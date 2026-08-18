from __future__ import annotations

import builtins
import copy
import socket
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.retail_price_presentation import (  # noqa: E402
    MAX_PRESENTATION_UPLIFT_RATE,
    POLICY_VERSION,
    RetailPricePresentationValidationError,
    present_retail_price,
    present_retail_prices,
)


def display(value: str) -> Decimal:
    result = present_retail_price(Decimal(value))
    presented = result.presentation.display_price_usd
    if presented is None:  # pragma: no cover - helper guard
        raise AssertionError("Expected a display price")
    return presented


class RetailPricePresentationTests(unittest.TestCase):
    def test_01_19_50_to_19_99(self) -> None:
        self.assertEqual(display("19.50"), Decimal("19.99"))

    def test_02_21_to_21_99(self) -> None:
        self.assertEqual(display("21.00"), Decimal("21.99"))

    def test_03_22_50_to_22_99(self) -> None:
        self.assertEqual(display("22.50"), Decimal("22.99"))

    def test_04_30_to_30_99(self) -> None:
        self.assertEqual(display("30.00"), Decimal("30.99"))

    def test_05_45_to_45_99(self) -> None:
        self.assertEqual(display("45.00"), Decimal("45.99"))

    def test_06_47_25_to_47_99(self) -> None:
        self.assertEqual(display("47.25"), Decimal("47.99"))

    def test_07_56_25_to_59(self) -> None:
        self.assertEqual(display("56.25"), Decimal("59.00"))

    def test_08_67_50_to_69(self) -> None:
        self.assertEqual(display("67.50"), Decimal("69.00"))

    def test_09_78_75_to_79(self) -> None:
        self.assertEqual(display("78.75"), Decimal("79.00"))

    def test_10_90_to_99(self) -> None:
        self.assertEqual(display("90.00"), Decimal("99.00"))

    def test_11_112_50_to_119(self) -> None:
        self.assertEqual(display("112.50"), Decimal("119.00"))

    def test_12_135_to_139(self) -> None:
        self.assertEqual(display("135.00"), Decimal("139.00"))

    def test_13_180_to_189(self) -> None:
        self.assertEqual(display("180.00"), Decimal("189.00"))

    def test_14_225_to_229(self) -> None:
        self.assertEqual(display("225.00"), Decimal("229.00"))

    def test_15_270_to_279(self) -> None:
        self.assertEqual(display("270.00"), Decimal("279.00"))

    def test_16_display_is_never_below_target(self) -> None:
        targets = (
            "0",
            "0.01",
            "19.50",
            "19.995",
            "49.999",
            "50",
            "51",
            "59.01",
            "90",
            "112.50",
            "999.99",
        )
        for raw_target in targets:
            with self.subTest(target=raw_target):
                target = Decimal(raw_target)
                self.assertGreaterEqual(display(raw_target), target)

    def test_17_downward_rounding_is_never_allowed(self) -> None:
        result = present_retail_price(Decimal("112.50"))
        shown = result.presentation.display_price_usd
        self.assertEqual(shown, Decimal("119.00"))
        self.assertNotIn(shown, {Decimal("109"), Decimal("99"), Decimal("112")})

    def test_18_uplift_amount_is_calculated_from_display(self) -> None:
        result = present_retail_price(Decimal("112.50"))
        self.assertEqual(result.calculation.uplift_amount, Decimal("6.50"))

    def test_19_uplift_rate_is_decimal_and_auditable(self) -> None:
        result = present_retail_price(Decimal("112.50"))
        self.assertEqual(result.calculation.uplift_rate, Decimal("0.0578"))
        self.assertIsInstance(result.calculation.uplift_rate, Decimal)

    def test_20_exact_ten_percent_boundary_allows_nine_ending(self) -> None:
        result = present_retail_price(Decimal("90.00"))
        self.assertEqual(MAX_PRESENTATION_UPLIFT_RATE, Decimal("0.10"))
        self.assertEqual(result.presentation.display_price_usd, Decimal("99.00"))
        self.assertFalse(result.calculation.fallback_used)
        self.assertEqual(result.calculation.uplift_rate, Decimal("0.1000"))

    def test_21_above_ten_percent_uses_x_99_fallback(self) -> None:
        result = present_retail_price(Decimal("50.00"))
        self.assertEqual(result.calculation.candidate_price, Decimal("59.00"))
        self.assertEqual(result.presentation.display_price_usd, Decimal("50.99"))
        self.assertEqual(result.calculation.strategy, "x_99_fallback")
        self.assertTrue(result.calculation.fallback_used)

    def test_22_existing_19_99_is_preserved(self) -> None:
        self.assertEqual(display("19.99"), Decimal("19.99"))

    def test_23_existing_59_is_preserved(self) -> None:
        self.assertEqual(display("59.00"), Decimal("59.00"))

    def test_24_existing_119_is_preserved(self) -> None:
        self.assertEqual(display("119.00"), Decimal("119.00"))

    def test_25_policy_is_idempotent(self) -> None:
        for raw_target in ("19.50", "50.00", "59.00", "112.50", "135.00"):
            with self.subTest(target=raw_target):
                first = display(raw_target)
                second = present_retail_price(first).presentation.display_price_usd
                self.assertEqual(second, first)

    def test_26_zero_is_preserved_as_zero(self) -> None:
        result = present_retail_price(Decimal("0"))
        self.assertEqual(result.presentation.display_price_usd, Decimal("0.00"))
        self.assertEqual(result.calculation.strategy, "zero_preserved")
        self.assertEqual(result.calculation.uplift_amount, Decimal("0.00"))

    def test_27_negative_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            RetailPricePresentationValidationError,
            "cannot be negative",
        ):
            present_retail_price(Decimal("-0.01"))

    def test_28_none_returns_no_target_price(self) -> None:
        result = present_retail_price(None)
        self.assertEqual(result.status, "no_target_price")
        self.assertIsNone(result.economic.target_retail_usd)
        self.assertIsNone(result.presentation.display_price_usd)
        self.assertIsNone(result.calculation.candidate_price)

    def test_29_only_decimal_or_none_is_accepted(self) -> None:
        for invalid in (112, 112.5, "112.50"):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(
                    RetailPricePresentationValidationError,
                    "Decimal or None",
                ):
                    present_retail_price(invalid)  # type: ignore[arg-type]

    def test_30_display_price_is_quantized_to_usd_cent(self) -> None:
        for target in ("19.50", "56.25", "112.50", "135.00"):
            with self.subTest(target=target):
                self.assertEqual(display(target).as_tuple().exponent, -2)

    def test_31_economic_target_is_preserved(self) -> None:
        target = Decimal("112.50")
        result = present_retail_price(target)
        self.assertEqual(result.economic.target_retail_usd, target)
        self.assertEqual(result.economic.target_retail_usd.as_tuple().exponent, -2)

    def test_32_presentation_price_is_separate_from_economic_target(self) -> None:
        result = present_retail_price(Decimal("112.50"))
        self.assertEqual(result.economic.target_retail_usd, Decimal("112.50"))
        self.assertEqual(
            result.presentation.display_price_usd,
            Decimal("119.00"),
        )
        self.assertNotEqual(
            result.economic.target_retail_usd,
            result.presentation.display_price_usd,
        )

    def test_33_policy_version_is_recorded(self) -> None:
        result = present_retail_price(Decimal("112.50"))
        self.assertEqual(POLICY_VERSION, "retail-presentation-v1")
        self.assertEqual(result.metadata.policy_version, POLICY_VERSION)

    def test_34_strategy_metadata_distinguishes_rules(self) -> None:
        low = present_retail_price(Decimal("22.50"))
        high = present_retail_price(Decimal("112.50"))
        unchanged = present_retail_price(Decimal("119.00"))
        self.assertEqual(low.calculation.strategy, "x_99")
        self.assertEqual(high.calculation.strategy, "nine_ending")
        self.assertEqual(unchanged.calculation.strategy, "already_presented")

    def test_35_fallback_metadata_preserves_rejected_candidate(self) -> None:
        result = present_retail_price(Decimal("51.00"))
        self.assertEqual(result.calculation.candidate_price, Decimal("59.00"))
        self.assertEqual(result.presentation.display_price_usd, Decimal("51.99"))
        self.assertTrue(result.calculation.fallback_used)
        self.assertEqual(result.calculation.uplift_amount, Decimal("0.99"))

    def test_36_option_pricing_policy_is_not_called_or_modified(self) -> None:
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price",
            side_effect=AssertionError("pricing policy must remain separate"),
        ) as pricing_policy:
            result = present_retail_price(Decimal("112.50"))
        pricing_policy.assert_not_called()
        self.assertEqual(result.economic.target_retail_usd, Decimal("112.50"))

    def test_37_product_base_price_is_not_modified(self) -> None:
        product = {
            "retail_pricing": {
                "minimum_retail_price": Decimal("270.00"),
            },
            "upgrade_target": Decimal("112.50"),
        }
        original = copy.deepcopy(product)
        result = present_retail_price(product["upgrade_target"])
        self.assertEqual(product, original)
        self.assertEqual(
            product["retail_pricing"]["minimum_retail_price"],
            Decimal("270.00"),
        )
        self.assertEqual(result.presentation.display_price_usd, Decimal("119.00"))

    def test_38_input_sequence_is_not_mutated(self) -> None:
        targets = [Decimal("19.50"), None, Decimal("112.50")]
        original = list(targets)
        present_retail_prices(targets)
        self.assertEqual(targets, original)

    def test_39_batch_order_is_stable(self) -> None:
        targets = [
            Decimal("135.00"),
            Decimal("19.50"),
            Decimal("90.00"),
        ]
        results = present_retail_prices(targets)
        self.assertEqual(
            [result.presentation.display_price_usd for result in results],
            [Decimal("139.00"), Decimal("19.99"), Decimal("99.00")],
        )

    def test_40_policy_performs_no_network_request(self) -> None:
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            result = present_retail_price(Decimal("112.50"))
        create_connection.assert_not_called()
        socket_connect.assert_not_called()
        self.assertEqual(result.presentation.display_price_usd, Decimal("119.00"))

    def test_41_policy_performs_no_file_or_external_write(self) -> None:
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            result = present_retail_price(Decimal("112.50"))
        open_mock.assert_not_called()
        self.assertEqual(result.presentation.display_price_usd, Decimal("119.00"))

    def test_42_result_serialization_keeps_decimal_strings(self) -> None:
        serialized = present_retail_price(Decimal("112.50")).to_dict()
        self.assertEqual(serialized["economic"]["target_retail_usd"], "112.50")
        self.assertEqual(
            serialized["presentation"]["display_price_usd"],
            "119.00",
        )
        self.assertEqual(serialized["calculation"]["uplift_rate"], "0.0578")


if __name__ == "__main__":
    unittest.main()
