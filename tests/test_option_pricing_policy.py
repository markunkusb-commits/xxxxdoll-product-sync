from __future__ import annotations

import builtins
import inspect
import socket
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.additional_option_parser import (  # noqa: E402
    AdditionalOptionPricing,
)
from sync_worker.option_pricing_policy import (  # noqa: E402
    POLICY_VERSION,
    InvalidExchangeRateError,
    InvalidSupplierCostError,
    MissingExchangeRateError,
    UnsupportedOptionCurrencyError,
    calculate_option_retail_price,
)


def supplier_price(
    amount: int | float | Decimal | None,
    *,
    currency: str | None = "RMB",
    raw_price: str | None = None,
) -> AdditionalOptionPricing:
    return AdditionalOptionPricing(
        amount=amount,
        currency=currency,
        raw_price=raw_price or (f"{currency}{amount}" if amount is not None else None),
    )


def price_rmb(amount: int | float | Decimal):
    return calculate_option_retail_price(
        supplier_price(amount),
        rmb_to_usd_rate=Decimal("0.14"),
    )


class OptionRetailPricingPolicyTests(unittest.TestCase):
    def test_01_rmb_30_example(self) -> None:
        result = price_rmb(30)
        self.assertEqual(result.calculation.cost_usd, Decimal("4.2000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("19.20"))

    def test_02_rmb_50_example(self) -> None:
        result = price_rmb(50)
        self.assertEqual(result.calculation.cost_usd, Decimal("7.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("22.00"))

    def test_03_rmb_100_example(self) -> None:
        result = price_rmb(100)
        self.assertEqual(result.calculation.cost_usd, Decimal("14.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("29.00"))

    def test_04_rmb_200_example(self) -> None:
        result = price_rmb(200)
        self.assertEqual(result.calculation.cost_usd, Decimal("28.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("43.00"))

    def test_05_rmb_300_example(self) -> None:
        result = price_rmb(300)
        self.assertEqual(result.calculation.cost_usd, Decimal("42.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("63.00"))

    def test_06_rmb_500_example(self) -> None:
        result = price_rmb(500)
        self.assertEqual(result.calculation.cost_usd, Decimal("70.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("105.00"))

    def test_07_rmb_800_example(self) -> None:
        result = price_rmb(800)
        self.assertEqual(result.calculation.cost_usd, Decimal("112.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("168.00"))

    def test_08_rmb_1200_example(self) -> None:
        result = price_rmb(1200)
        self.assertEqual(result.calculation.cost_usd, Decimal("168.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("252.00"))

    def test_09_markup_is_fifty_percent(self) -> None:
        calculation = price_rmb(500).calculation
        self.assertEqual(calculation.markup_rate, Decimal("0.50"))
        self.assertEqual(calculation.markup_price_usd, Decimal("105.0000"))

    def test_10_minimum_profit_is_fifteen_usd(self) -> None:
        calculation = price_rmb(100).calculation
        self.assertEqual(calculation.minimum_profit_usd, Decimal("15.00"))
        self.assertEqual(
            calculation.minimum_profit_price_usd,
            Decimal("29.0000"),
        )

    def test_11_markup_branch_wins(self) -> None:
        result = price_rmb(500)
        self.assertGreater(
            result.calculation.markup_price_usd,
            result.calculation.minimum_profit_price_usd,
        )
        self.assertEqual(
            result.retail.target_retail_usd,
            result.calculation.markup_price_usd,
        )

    def test_12_minimum_profit_branch_wins(self) -> None:
        result = price_rmb(30)
        self.assertGreater(
            result.calculation.minimum_profit_price_usd,
            result.calculation.markup_price_usd,
        )
        self.assertEqual(
            result.retail.target_retail_usd,
            result.calculation.minimum_profit_price_usd,
        )

    def test_13_equal_branch_boundary(self) -> None:
        result = calculate_option_retail_price(supplier_price(30, currency="USD"))
        self.assertEqual(
            result.calculation.markup_price_usd,
            Decimal("45.0000"),
        )
        self.assertEqual(
            result.calculation.minimum_profit_price_usd,
            Decimal("45.0000"),
        )
        self.assertEqual(result.retail.target_retail_usd, Decimal("45.00"))

    def test_14_decimal_intermediate_precision(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(123),
            rmb_to_usd_rate=Decimal("0.1375"),
        )
        self.assertEqual(result.calculation.cost_usd, Decimal("16.9125"))
        self.assertEqual(result.calculation.markup_price_usd, Decimal("25.3688"))

    def test_15_target_is_quantized_to_cents(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(33),
            rmb_to_usd_rate=Decimal("0.137"),
        )
        self.assertEqual(result.retail.target_retail_usd, Decimal("19.52"))
        self.assertEqual(result.retail.target_retail_usd.as_tuple().exponent, -2)

    def test_16_rmb_currency_is_supported(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(100, currency="rmb"),
            rmb_to_usd_rate=Decimal("0.14"),
        )
        self.assertEqual(result.supplier_cost.currency, "RMB")
        self.assertEqual(result.fx.source_currency, "RMB")

    def test_17_cny_currency_alias_is_supported(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(100, currency="CNY"),
            rmb_to_usd_rate=Decimal("0.14"),
        )
        self.assertEqual(result.supplier_cost.currency, "CNY")
        self.assertEqual(result.retail.target_retail_usd, Decimal("29.00"))

    def test_18_usd_supplier_cost_needs_no_fx_rate(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(10, currency="USD", raw_price="US$10")
        )
        self.assertEqual(result.fx.rate, Decimal("1"))
        self.assertEqual(result.fx.rate_source, "not_required")
        self.assertEqual(result.retail.target_retail_usd, Decimal("25.00"))

    def test_19_unsupported_currency_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedOptionCurrencyError):
            calculate_option_retail_price(
                supplier_price(100, currency="EUR"),
                rmb_to_usd_rate=Decimal("0.14"),
            )

    def test_20_missing_rmb_fx_rate_is_rejected(self) -> None:
        with self.assertRaises(MissingExchangeRateError):
            calculate_option_retail_price(supplier_price(100))

    def test_21_missing_amount_has_no_retail_candidate(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(None, currency="USD", raw_price="US$xxx")
        )
        self.assertEqual(result.status, "no_supplier_price")
        self.assertIsNone(result.fx)
        self.assertIsNone(result.calculation)
        self.assertIsNone(result.retail)

    def test_22_missing_pricing_object_has_no_retail_candidate(self) -> None:
        result = calculate_option_retail_price(None)
        self.assertEqual(result.status, "no_supplier_price")
        self.assertIsNone(result.supplier_cost.amount)
        self.assertIsNone(result.retail)

    def test_23_zero_supplier_cost_remains_free(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(0),
            rmb_to_usd_rate=Decimal("0.14"),
        )
        self.assertEqual(result.status, "zero_supplier_cost")
        self.assertEqual(result.retail.target_retail_usd, Decimal("0.00"))
        self.assertIn("free option", result.metadata.warnings[0])

    def test_24_negative_supplier_cost_is_rejected(self) -> None:
        with self.assertRaises(InvalidSupplierCostError):
            calculate_option_retail_price(
                supplier_price(-1),
                rmb_to_usd_rate=Decimal("0.14"),
            )

    def test_25_raw_supplier_price_is_preserved(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(500, raw_price="￥500.00"),
            rmb_to_usd_rate=Decimal("0.14"),
        )
        self.assertEqual(result.supplier_cost.raw_value, "￥500.00")

    def test_26_retail_does_not_overwrite_supplier_cost(self) -> None:
        original = supplier_price(500, raw_price="￥500.00")
        result = calculate_option_retail_price(
            original,
            rmb_to_usd_rate=Decimal("0.14"),
        )
        self.assertEqual(result.supplier_cost.amount, Decimal("500"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("105.00"))
        self.assertEqual(original.amount, 500)
        self.assertEqual(original.raw_price, "￥500.00")

    def test_27_policy_version_is_preserved(self) -> None:
        result = price_rmb(100)
        self.assertEqual(POLICY_VERSION, "option-retail-v1")
        self.assertEqual(result.metadata.policy_version, "option-retail-v1")

    def test_28_product_retail_minimum_is_not_an_input(self) -> None:
        parameters = inspect.signature(calculate_option_retail_price).parameters
        self.assertNotIn("product", parameters)
        self.assertNotIn("minimum_retail_price", parameters)

    def test_29_base_product_price_is_not_added_to_option(self) -> None:
        unrelated_base_product_price = Decimal("100.00")
        result = price_rmb(100)
        self.assertEqual(result.retail.target_retail_usd, Decimal("29.00"))
        self.assertNotEqual(
            result.retail.target_retail_usd,
            unrelated_base_product_price + Decimal("29.00"),
        )

    def test_30_no_psychological_price_rounding(self) -> None:
        result = price_rmb(30)
        self.assertEqual(result.retail.target_retail_usd, Decimal("19.20"))
        self.assertNotEqual(result.retail.target_retail_usd, Decimal("19.99"))

    def test_31_injected_fx_metadata_is_auditable(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(100),
            rmb_to_usd_rate=Decimal("0.14"),
            rate_source="manual-test-fixture",
        )
        self.assertEqual(result.fx.rate, Decimal("0.14"))
        self.assertEqual(result.fx.rate_source, "manual-test-fixture")

    def test_32_fx_timestamp_is_preserved(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(100),
            rmb_to_usd_rate=Decimal("0.14"),
            rate_timestamp="2026-08-17T00:00:00Z",
        )
        self.assertEqual(result.fx.rate_timestamp, "2026-08-17T00:00:00Z")

    def test_33_zero_fx_rate_is_rejected(self) -> None:
        with self.assertRaises(InvalidExchangeRateError):
            calculate_option_retail_price(
                supplier_price(100),
                rmb_to_usd_rate=Decimal("0"),
            )

    def test_34_negative_fx_rate_is_rejected(self) -> None:
        with self.assertRaises(InvalidExchangeRateError):
            calculate_option_retail_price(
                supplier_price(100),
                rmb_to_usd_rate=Decimal("-0.14"),
            )

    def test_35_non_decimal_fx_rate_is_rejected(self) -> None:
        with self.assertRaises(InvalidExchangeRateError):
            calculate_option_retail_price(
                supplier_price(100),
                rmb_to_usd_rate=0.14,  # type: ignore[arg-type]
            )

    def test_36_non_finite_supplier_cost_is_rejected(self) -> None:
        with self.assertRaises(InvalidSupplierCostError):
            calculate_option_retail_price(
                supplier_price(Decimal("NaN")),
                rmb_to_usd_rate=Decimal("0.14"),
            )

    def test_37_calculation_performs_no_network_request(self) -> None:
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            result = price_rmb(100)
        self.assertEqual(result.status, "priced")
        create_connection.assert_not_called()
        socket_connect.assert_not_called()

    def test_38_calculation_performs_no_external_write(self) -> None:
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            result = price_rmb(100)
        self.assertEqual(result.status, "priced")
        open_mock.assert_not_called()

    def test_39_json_safe_output_does_not_convert_money_to_float(self) -> None:
        payload = price_rmb(30).to_dict()
        self.assertEqual(payload["supplier_cost"]["amount"], "30")
        self.assertEqual(payload["retail"]["target_retail_usd"], "19.20")
        self.assertNotIsInstance(payload["retail"]["target_retail_usd"], float)

    def test_40_usd_path_ignores_unneeded_rmb_rate(self) -> None:
        result = calculate_option_retail_price(
            supplier_price(100, currency="USD"),
            rmb_to_usd_rate=None,
        )
        self.assertEqual(result.calculation.cost_usd, Decimal("100.0000"))
        self.assertEqual(result.retail.target_retail_usd, Decimal("150.00"))


if __name__ == "__main__":
    unittest.main()
