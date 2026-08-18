from __future__ import annotations

import builtins
import copy
import socket
import sys
import unittest
import urllib.request
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import option_pricing_policy  # noqa: E402
from sync_worker.additional_option_parser import (  # noqa: E402
    AdditionalOptionIdentity,
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
)
from sync_worker.option_mapping_registry import (  # noqa: E402
    REGISTRY_VERSION,
    OptionMappingRegistry,
)
from sync_worker.product_model import (  # noqa: E402
    MonetaryValue,
    ProductIdentity,
    ProductMedia,
    ProductOptions,
    ProductRecord,
    ProductSource,
    ProductSpecifications,
    RetailPricing,
    SupplierCosts,
    UnknownFields,
    UpgradeOptionRecord,
)
from sync_worker.product_option_linker import link_products_to_options  # noqa: E402
from sync_worker.product_option_pricing import (  # noqa: E402
    PricedLinkedOption,
    enrich_product_option_pricing,
    summarize_product_option_pricing,
)


FX_RATE = Decimal("0.1500")


def upgrade(name: str, *, raw_value: str | None = None) -> UpgradeOptionRecord:
    return UpgradeOptionRecord(
        name=name,
        raw_value=raw_value or name,
        supplier_cost=None,
    )


def catalog_option(
    name: str,
    *,
    amount: Decimal | None = Decimal("500"),
    currency: str | None = "RMB",
    raw_price: str | None = "￥500",
    coordinate: str = "A2",
    warnings: tuple[str, ...] = (),
) -> AdditionalOptionRecord:
    column = "".join(character for character in coordinate if character.isalpha())
    row = int("".join(character for character in coordinate if character.isdigit()))
    return AdditionalOptionRecord(
        identity=AdditionalOptionIdentity(option_name=name, raw_name=name),
        pricing=AdditionalOptionPricing(
            amount=amount,
            currency=currency,
            raw_price=raw_price,
        ),
        category="product_extra_option",
        source=AdditionalOptionSource(
            row=row,
            column=column,
            raw_coordinate=coordinate,
        ),
        warnings=warnings,
    )


def product(
    model: str,
    *upgrades: UpgradeOptionRecord,
    retail_amount: int = 999,
    warnings: tuple[str, ...] = (),
) -> ProductRecord:
    retail = MonetaryValue(
        raw_value=f"US${retail_amount}",
        currency="USD",
        amount=retail_amount,
        context="minimum_retail_price",
    )
    return ProductRecord(
        identity=ProductIdentity(
            series="ultra",
            model=model,
            raw_series_title="CLM Ultra",
            raw_model=model,
        ),
        specifications=ProductSpecifications(normalized={}, raw=()),
        supplier_costs=SupplierCosts(
            fob_unit_price=None,
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(minimum_retail_price=retail),
        options=ProductOptions(
            normal_options_price=None,
            upgrade_options=tuple(upgrades),
        ),
        media=ProductMedia(photo_download_link=None),
        source=ProductSource(start_row=10, end_row=20),
        included_features=("EVO skeleton",),
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=warnings,
    )


def registry() -> OptionMappingRegistry:
    return OptionMappingRegistry.approved_v1()


def linked(
    products: list[ProductRecord],
    catalog: list[AdditionalOptionRecord],
    *,
    use_registry: bool = True,
):
    return link_products_to_options(
        products,
        catalog,
        mapping_registry=registry() if use_registry else None,
    )


def price_results(link_results):
    return enrich_product_option_pricing(
        link_results,
        rmb_to_usd_rate=FX_RATE,
    )


def exact_priced(
    *,
    amount: Decimal | None = Decimal("500"),
    currency: str | None = "RMB",
    raw_price: str | None = "￥500",
    warnings: tuple[str, ...] = (),
):
    link_results = linked(
        [product("P1", upgrade("Exact Option", raw_value="1. Exact Option"))],
        [
            catalog_option(
                "Exact Option",
                amount=amount,
                currency=currency,
                raw_price=raw_price,
                warnings=warnings,
            )
        ],
    )
    return price_results(link_results)[0]


def approved_catalog() -> list[AdditionalOptionRecord]:
    return [
        catalog_option(
            "凝胶屁股", amount=Decimal("500"), coordinate="A2"
        ),
        catalog_option(
            "硅胶头植发", amount=Decimal("500"), coordinate="A3"
        ),
        catalog_option(
            "硬手硬脚(仅限硅胶)",
            amount=Decimal("400"),
            raw_price="￥400",
            coordinate="A4",
        ),
        catalog_option(
            "硅胶头植眉毛",
            amount=Decimal("300"),
            raw_price="￥300",
            coordinate="A5",
        ),
        catalog_option(
            "硅胶头植睫毛",
            amount=Decimal("300"),
            raw_price="￥300",
            coordinate="A6",
        ),
    ]


def all_approved_priced():
    record = product(
        "U1",
        upgrade("Gel Butt", raw_value="1. Gel Butt"),
        upgrade("Hair Implant", raw_value="2. Hair Implant"),
        upgrade(
            "Eyebrows/Eyelashes Implant",
            raw_value="3. Eyebrows/Eyelashes Implant",
        ),
        upgrade("Hard Hands and Feet", raw_value="4. Hard Hands and Feet"),
    )
    return price_results(linked([record], approved_catalog()))[0]


class ProductOptionPricingTests(unittest.TestCase):
    def test_01_exact_linked_option_is_priced(self) -> None:
        result = exact_priced()
        self.assertEqual(len(result.priced_upgrade_options), 1)
        self.assertEqual(
            result.priced_upgrade_options[0].mapping.mapping_type, "exact"
        )

    def test_02_gel_butt_alias_rmb_500(self) -> None:
        option = all_approved_priced().priced_upgrade_options[0]
        self.assertEqual(option.product_upgrade_name, "Gel Butt")
        self.assertEqual(option.supplier_cost.amount, Decimal("500"))

    def test_03_hair_implant_alias_rmb_500(self) -> None:
        option = all_approved_priced().priced_upgrade_options[1]
        self.assertEqual(option.product_upgrade_name, "Hair Implant")
        self.assertEqual(option.supplier_cost.amount, Decimal("500"))

    def test_04_hard_hands_alias_rmb_400(self) -> None:
        option = all_approved_priced().priced_upgrade_options[2]
        self.assertEqual(option.product_upgrade_name, "Hard Hands and Feet")
        self.assertEqual(option.supplier_cost.amount, Decimal("400"))

    def test_05_composite_retains_two_rmb_300_components(self) -> None:
        option = all_approved_priced().priced_upgrade_options[3]
        self.assertEqual(
            [component.supplier_cost.amount for component in option.mapping.components],
            [Decimal("300"), Decimal("300")],
        )

    def test_06_composite_uses_combined_supplier_cost(self) -> None:
        option = all_approved_priced().priced_upgrade_options[3]
        self.assertEqual(option.supplier_cost.amount, Decimal("600"))
        self.assertEqual(option.supplier_cost.currency, "RMB")

    def test_07_composite_is_not_split_into_two_customer_options(self) -> None:
        record = product("U1", upgrade("Eyebrows/Eyelashes Implant"))
        link_results = linked([record], approved_catalog()[3:])
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price",
            wraps=option_pricing_policy.calculate_option_retail_price,
        ) as policy:
            result = price_results(link_results)[0]
        self.assertEqual(len(result.priced_upgrade_options), 1)
        self.assertEqual(policy.call_count, 1)

    def test_08_explicit_decimal_fx_rate_is_preserved(self) -> None:
        option = exact_priced().priced_upgrade_options[0]
        self.assertEqual(option.pricing.fx.rate, FX_RATE)

    def test_09_rmb_500_retail_is_112_50(self) -> None:
        option = exact_priced().priced_upgrade_options[0]
        self.assertEqual(
            option.pricing.calculation.cost_usd, Decimal("75.0000")
        )
        self.assertEqual(
            option.pricing.retail.target_retail_usd, Decimal("112.50")
        )

    def test_10_rmb_400_retail_is_90_00(self) -> None:
        result = exact_priced(
            amount=Decimal("400"), raw_price="￥400"
        )
        option = result.priced_upgrade_options[0]
        self.assertEqual(option.pricing.retail.target_retail_usd, Decimal("90.00"))

    def test_11_rmb_600_retail_is_135_00(self) -> None:
        option = all_approved_priced().priced_upgrade_options[3]
        self.assertEqual(
            option.pricing.calculation.cost_usd, Decimal("90.0000")
        )
        self.assertEqual(
            option.pricing.retail.target_retail_usd, Decimal("135.00")
        )

    def test_12_existing_pricing_policy_is_called(self) -> None:
        link_results = linked(
            [product("P1", upgrade("Exact Option"))],
            [catalog_option("Exact Option")],
        )
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price",
            wraps=option_pricing_policy.calculate_option_retail_price,
        ) as policy:
            price_results(link_results)
        policy.assert_called_once()
        self.assertEqual(policy.call_args.kwargs["rmb_to_usd_rate"], FX_RATE)

    def test_13_enrichment_does_not_reimplement_policy_formula(self) -> None:
        sentinel = option_pricing_policy.calculate_option_retail_price(
            AdditionalOptionPricing(
                amount=Decimal("10"), currency="USD", raw_price="US$10"
            ),
            rmb_to_usd_rate=FX_RATE,
        )
        link_results = linked(
            [product("P1", upgrade("Exact Option"))],
            [catalog_option("Exact Option")],
        )
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price",
            return_value=sentinel,
        ):
            option = price_results(link_results)[0].priced_upgrade_options[0]
        self.assertIs(option.pricing, sentinel)
        self.assertEqual(
            option.pricing.retail.target_retail_usd, Decimal("25.00")
        )

    def test_14_supplier_cost_and_raw_provenance_are_preserved(self) -> None:
        option = exact_priced().priced_upgrade_options[0]
        self.assertEqual(option.supplier_cost.amount, Decimal("500"))
        self.assertEqual(option.supplier_cost.currency, "RMB")
        self.assertEqual(option.supplier_cost.raw_values, ("￥500",))

    def test_15_option_retail_is_isolated_inside_option_pricing(self) -> None:
        payload = exact_priced().to_dict()
        self.assertEqual(
            payload["priced_upgrade_options"][0]["pricing"]["retail"][
                "target_retail_usd"
            ],
            "112.50",
        )
        self.assertNotIn("target_retail_usd", payload["retail_pricing"])

    def test_16_product_minimum_retail_price_is_unchanged(self) -> None:
        record = product("P1", upgrade("Exact Option"), retail_amount=999)
        before = record.retail_pricing
        result = price_results(
            linked([record], [catalog_option("Exact Option")])
        )[0]
        self.assertEqual(record.retail_pricing, before)
        self.assertEqual(result.retail_pricing, before)

    def test_17_base_and_option_prices_are_never_added(self) -> None:
        result = exact_priced()
        option_retail = result.priced_upgrade_options[0].pricing.retail
        self.assertEqual(option_retail.target_retail_usd, Decimal("112.50"))
        self.assertNotEqual(option_retail.target_retail_usd, Decimal("1111.50"))

    def test_18_missing_supplier_price_is_unpriced(self) -> None:
        result = exact_priced(amount=None, currency=None, raw_price=None)
        self.assertEqual(result.priced_upgrade_options, ())
        unpriced = result.unpriced_upgrade_options[0]
        self.assertEqual(unpriced.status, "no_supplier_price")
        self.assertEqual(unpriced.pricing.status, "no_supplier_price")

    def test_19_unsupported_currency_is_unpriced(self) -> None:
        result = exact_priced(
            amount=Decimal("500"), currency="EUR", raw_price="EUR500"
        )
        unpriced = result.unpriced_upgrade_options[0]
        self.assertEqual(unpriced.status, "unsupported_currency")
        self.assertIsNone(unpriced.pricing)

    def test_20_incomplete_composite_is_not_priceable(self) -> None:
        result = price_results(
            linked(
                [product("P1", upgrade("Eyebrows/Eyelashes Implant"))],
                [approved_catalog()[3]],
            )
        )[0]
        unpriced = result.unpriced_upgrade_options[0]
        self.assertEqual(unpriced.status, "mapping_not_priceable")
        self.assertEqual(unpriced.mapping.mapping_status, "incomplete_composite")

    def test_21_ambiguous_mapping_is_not_priceable(self) -> None:
        catalog = [
            approved_catalog()[3],
            catalog_option("硅胶头植眉毛", coordinate="A8"),
            approved_catalog()[4],
        ]
        result = price_results(
            linked(
                [product("P1", upgrade("Eyebrows/Eyelashes Implant"))],
                catalog,
            )
        )[0]
        self.assertEqual(result.unpriced_upgrade_options[0].status, "mapping_not_priceable")
        self.assertEqual(
            result.unpriced_upgrade_options[0].mapping.mapping_status,
            "ambiguous",
        )

    def test_22_composite_currency_conflict_is_not_priceable(self) -> None:
        catalog = approved_catalog()[3:]
        catalog[1] = catalog_option(
            "硅胶头植睫毛",
            amount=Decimal("50"),
            currency="USD",
            raw_price="US$50",
            coordinate="A6",
        )
        result = price_results(
            linked(
                [product("P1", upgrade("Eyebrows/Eyelashes Implant"))],
                catalog,
            )
        )[0]
        self.assertEqual(
            result.unpriced_upgrade_options[0].mapping.mapping_status,
            "currency_conflict",
        )

    def test_23_missing_composite_component_price_is_not_priceable(self) -> None:
        catalog = approved_catalog()[3:]
        catalog[1] = catalog_option(
            "硅胶头植睫毛",
            amount=None,
            currency=None,
            raw_price=None,
            coordinate="A6",
        )
        result = price_results(
            linked(
                [product("P1", upgrade("Eyebrows/Eyelashes Implant"))],
                catalog,
            )
        )[0]
        self.assertEqual(
            result.unpriced_upgrade_options[0].mapping.mapping_status,
            "missing_component_price",
        )

    def test_24_exact_mapping_type_is_preserved(self) -> None:
        self.assertEqual(
            exact_priced().priced_upgrade_options[0].mapping.mapping_status,
            "exact_catalog",
        )

    def test_25_alias_mapping_type_is_preserved(self) -> None:
        option = all_approved_priced().priced_upgrade_options[0]
        self.assertEqual(option.mapping.mapping_type, "alias")
        self.assertEqual(option.mapping.mapping_status, "alias")

    def test_26_composite_mapping_type_is_preserved(self) -> None:
        option = all_approved_priced().priced_upgrade_options[3]
        self.assertEqual(option.mapping.mapping_type, "composite")
        self.assertEqual(option.mapping.mapping_status, "composite")

    def test_27_registry_version_is_preserved(self) -> None:
        options = all_approved_priced().priced_upgrade_options
        self.assertTrue(
            all(option.mapping.registry_version == REGISTRY_VERSION for option in options)
        )

    def test_28_source_coordinates_are_preserved(self) -> None:
        options = all_approved_priced().priced_upgrade_options
        self.assertEqual(options[0].mapping.source_coordinates, ("A2",))
        self.assertEqual(options[3].mapping.source_coordinates, ("A5", "A6"))

    def test_29_raw_product_option_is_preserved(self) -> None:
        options = all_approved_priced().priced_upgrade_options
        self.assertEqual(options[0].product_raw_value, "1. Gel Butt")
        self.assertEqual(
            options[3].product_raw_value,
            "3. Eyebrows/Eyelashes Implant",
        )

    def test_30_warnings_are_propagated(self) -> None:
        result = exact_priced(warnings=("catalog warning",))
        self.assertEqual(
            result.priced_upgrade_options[0].warnings,
            ("catalog warning",),
        )

    def test_31_input_link_results_are_not_mutated(self) -> None:
        link_results = linked(
            [product("P1", upgrade("Gel Butt"))],
            [approved_catalog()[0]],
        )
        original = copy.deepcopy(link_results)
        price_results(link_results)
        self.assertEqual(link_results, original)

    def test_32_output_order_is_stable(self) -> None:
        record = product(
            "P1",
            upgrade("First Exact"),
            upgrade("Second Exact"),
        )
        catalog = [
            catalog_option("First Exact", coordinate="A2"),
            catalog_option("Second Exact", coordinate="A3"),
        ]
        result = price_results(linked([record], catalog))[0]
        self.assertEqual(
            [option.product_upgrade_name for option in result.priced_upgrade_options],
            ["First Exact", "Second Exact"],
        )

    def test_33_no_psychological_price_rounding(self) -> None:
        targets = [
            option.pricing.retail.target_retail_usd
            for option in all_approved_priced().priced_upgrade_options
        ]
        self.assertEqual(
            targets,
            [
                Decimal("112.50"),
                Decimal("112.50"),
                Decimal("90.00"),
                Decimal("135.00"),
            ],
        )

    def test_34_no_fx_api_is_accessed(self) -> None:
        link_results = linked(
            [product("P1", upgrade("Exact Option"))],
            [catalog_option("Exact Option")],
        )
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=AssertionError("FX API access is forbidden"),
        ) as urlopen:
            price_results(link_results)
        urlopen.assert_not_called()

    def test_35_no_network_request_is_performed(self) -> None:
        link_results = linked(
            [product("P1", upgrade("Exact Option"))],
            [catalog_option("Exact Option")],
        )
        with patch.object(
            socket,
            "socket",
            side_effect=AssertionError("network access is forbidden"),
        ) as network:
            price_results(link_results)
        network.assert_not_called()

    def test_36_no_file_or_external_write_is_performed(self) -> None:
        link_results = linked(
            [product("P1", upgrade("Exact Option"))],
            [catalog_option("Exact Option")],
        )
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("file access is forbidden"),
        ) as file_open:
            price_results(link_results)
        file_open.assert_not_called()

    def test_37_summary_product_counts(self) -> None:
        results = price_results(
            linked(
                [
                    product("P1", upgrade("Exact Option")),
                    product("P2"),
                ],
                [catalog_option("Exact Option")],
            )
        )
        summary = summarize_product_option_pricing(results)
        self.assertEqual(summary.total_products, 2)
        self.assertEqual(summary.products_with_priced_options, 1)
        self.assertEqual(summary.products_without_priced_options, 1)

    def test_38_summary_priced_mapping_counts(self) -> None:
        result = all_approved_priced()
        summary = summarize_product_option_pricing([result])
        self.assertEqual(summary.total_linked_options, 4)
        self.assertEqual(summary.priced_options, 4)
        self.assertEqual(summary.exact_priced, 0)
        self.assertEqual(summary.alias_priced, 3)
        self.assertEqual(summary.composite_priced, 1)

    def test_39_summary_unpriced_status_counts(self) -> None:
        no_price = exact_priced(amount=None, currency=None, raw_price=None)
        unsupported = exact_priced(
            amount=Decimal("10"), currency="EUR", raw_price="EUR10"
        )
        unmatched = price_results(
            linked([product("P3", upgrade("Unknown"))], [])
        )[0]
        summary = summarize_product_option_pricing(
            [no_price, unsupported, unmatched]
        )
        self.assertEqual(summary.unpriced_options, 3)
        self.assertEqual(summary.no_supplier_price, 1)
        self.assertEqual(summary.unsupported_currency, 1)
        self.assertEqual(summary.mapping_not_priceable, 1)

    def test_40_summary_total_option_retail_is_not_an_order_total(self) -> None:
        summary = summarize_product_option_pricing([all_approved_priced()])
        self.assertEqual(summary.total_option_retail_usd, Decimal("450.00"))
        self.assertNotEqual(summary.total_option_retail_usd, Decimal("1449.00"))


if __name__ == "__main__":
    unittest.main()
