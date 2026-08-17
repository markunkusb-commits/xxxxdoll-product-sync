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

from sync_worker.additional_option_parser import (  # noqa: E402
    AdditionalOptionIdentity,
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
)
from sync_worker.product_model import (  # noqa: E402
    MonetaryValue,
    ProductIdentity,
    ProductMedia,
    ProductOptions,
    ProductRecord,
    ProductSource,
    ProductSpecifications,
    RawCommercialField,
    RetailPricing,
    SupplierCosts,
    UnknownFields,
    UpgradeOptionRecord,
)
from sync_worker.product_option_linker import (  # noqa: E402
    OptionAliasRegistry,
    link_products_to_options,
    summarize_option_linking,
)


def money(
    amount: int | float,
    *,
    currency: str = "RMB",
    context: str = "test",
    raw_value: str | None = None,
) -> MonetaryValue:
    return MonetaryValue(
        raw_value=raw_value or f"{currency}{amount}",
        currency=currency,
        amount=amount,
        context=context,
    )


def upgrade(
    name: str,
    *,
    raw_value: str | None = None,
    supplier_cost: MonetaryValue | None = None,
) -> UpgradeOptionRecord:
    return UpgradeOptionRecord(
        name=name,
        raw_value=raw_value or name,
        supplier_cost=supplier_cost,
    )


def product(
    model: str,
    *,
    upgrades: tuple[UpgradeOptionRecord, ...] = (),
    included: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    retail: MonetaryValue | None = None,
    raw_commercial_entries: tuple[RawCommercialField, ...] = (),
    start_row: int = 10,
) -> ProductRecord:
    return ProductRecord(
        identity=ProductIdentity(
            series="pro",
            model=model,
            raw_series_title="CLM Pro",
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
            upgrade_options=upgrades,
        ),
        media=ProductMedia(photo_download_link=None),
        source=ProductSource(start_row=start_row, end_row=start_row + 5),
        included_features=included,
        notices=(),
        unknown_fields=UnknownFields(
            raw_commercial_entries=raw_commercial_entries
        ),
        warnings=warnings,
    )


def catalog_option(
    name: str,
    *,
    category: str = "function",
    amount: int | float | None = 300,
    currency: str | None = "RMB",
    raw_price: str | None = "¥300",
    warnings: tuple[str, ...] = (),
    coordinate: str = "A2",
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
        category=category,
        source=AdditionalOptionSource(
            row=row,
            column=column,
            raw_coordinate=coordinate,
        ),
        warnings=warnings,
    )


def link_one(
    product_record: ProductRecord,
    *catalog: AdditionalOptionRecord,
    aliases: OptionAliasRegistry | None = None,
):
    return link_products_to_options(
        [product_record],
        list(catalog),
        alias_registry=aliases,
    )[0]


class ProductOptionLinkerTests(unittest.TestCase):
    def test_01_exact_option_name_match(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(len(result.linked_upgrade_options), 1)
        self.assertEqual(result.linked_upgrade_options[0].match_method, "exact")

    def test_02_exact_match_is_case_insensitive(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("gel butt"),)),
            catalog_option("GEL BUTT"),
        )
        self.assertEqual(len(result.linked_upgrade_options), 1)

    def test_03_exact_match_normalizes_unicode_and_repeated_whitespace(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("\u00a0Gel\u2003  Butt "),)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(len(result.linked_upgrade_options), 1)

    def test_04_general_substring_fuzzy_matching_is_forbidden(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Gel"),)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(len(result.unmatched_upgrade_options), 1)

    def test_05_hair_implant_does_not_match_longer_catalog_name(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Hair Implant"),)),
            catalog_option("Silicone Head Hair Implant"),
        )
        self.assertEqual(len(result.unmatched_upgrade_options), 1)
        self.assertEqual(result.linked_upgrade_options, ())

    def test_06_unknown_upgrade_is_preserved_as_unmatched(self) -> None:
        raw = "Custom Shoulder Setup +¥200"
        result = link_one(
            product("P1", upgrades=(upgrade("Custom Shoulder Setup", raw_value=raw),)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(result.unmatched_upgrade_options[0].product_raw_option, raw)

    def test_07_duplicate_catalog_name_is_ambiguous(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),)),
            catalog_option("Gel Butt", coordinate="A2"),
            catalog_option("Gel Butt", coordinate="A3"),
        )
        self.assertEqual(len(result.ambiguous_upgrade_options), 1)
        self.assertEqual(result.linked_upgrade_options, ())
        self.assertEqual(len(result.ambiguous_upgrade_options[0].catalog_candidates), 2)

    def test_08_included_feature_is_not_a_charged_upgrade(self) -> None:
        result = link_one(
            product("P1", included=("Gel Butt",)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(result.included_features, ("Gel Butt",))
        self.assertEqual(result.linked_upgrade_options, ())

    def test_09_linked_upgrade_uses_catalog_price(self) -> None:
        option = catalog_option("Gel Butt", amount=500, raw_price="RMB500")
        linked = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),)), option
        ).linked_upgrade_options[0]
        self.assertIs(linked.pricing, option.pricing)
        self.assertEqual(linked.pricing.amount, 500)

    def test_10_included_and_upgrade_evidence_creates_conflict(self) -> None:
        result = link_one(
            product(
                "P1",
                upgrades=(upgrade("Gel Butt"),),
                included=("gel butt",),
            ),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(len(result.included_upgrade_conflicts), 1)
        self.assertEqual(result.linked_upgrade_options, ())
        self.assertIn("option appears as both included and upgrade", result.warnings)

    def test_11_product_without_upgrade_does_not_receive_catalog(self) -> None:
        result = link_one(
            product("P1"),
            catalog_option("Gel Butt"),
            catalog_option("Hair Implant"),
        )
        self.assertEqual(result.linked_upgrade_options, ())
        self.assertEqual(result.unmatched_upgrade_options, ())

    def test_12_accessory_is_not_attached_to_every_product(self) -> None:
        result = link_one(
            product("P1"),
            catalog_option("Hands Option", category="accessory"),
        )
        self.assertEqual(result.linked_upgrade_options, ())

    def test_13_explicit_accessory_upgrade_can_link(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Hands Option"),)),
            catalog_option("Hands Option", category="accessory"),
        )
        linked = result.linked_upgrade_options[0]
        self.assertEqual(linked.category, "accessory")
        self.assertEqual(linked.match_method, "exact")

    def test_14_non_accessory_product_option_links_normally(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Skin Tone"),)),
            catalog_option("Skin Tone", category="appearance"),
        )
        self.assertEqual(result.linked_upgrade_options[0].category, "appearance")

    def test_15_catalog_raw_price_is_preserved(self) -> None:
        option = catalog_option("Gel Butt", raw_price="+¥300")
        linked = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),)), option
        ).linked_upgrade_options[0]
        self.assertEqual(linked.pricing.raw_price, "+¥300")

    def test_16_catalog_price_currency_is_preserved(self) -> None:
        option = catalog_option("Hair Implant", amount=50, currency="USD", raw_price="US$50")
        linked = link_one(
            product("P1", upgrades=(upgrade("Hair Implant"),)), option
        ).linked_upgrade_options[0]
        self.assertEqual(linked.pricing.currency, "USD")

    def test_17_linker_performs_no_currency_conversion(self) -> None:
        option = catalog_option("Hair Implant", amount=50, currency="USD", raw_price="US$50")
        linked = link_one(
            product("P1", upgrades=(upgrade("Hair Implant"),)), option
        ).linked_upgrade_options[0]
        self.assertEqual(linked.pricing.amount, 50)
        self.assertEqual(linked.pricing.currency, "USD")

    def test_18_minimum_retail_price_is_not_modified(self) -> None:
        retail = money(850, currency="USD", context="minimum_retail_price")
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),), retail=retail),
            catalog_option("Gel Butt", amount=300),
        )
        self.assertIs(result.retail_pricing.minimum_retail_price, retail)
        self.assertEqual(result.retail_pricing.minimum_retail_price.amount, 850)

    def test_19_linker_does_not_calculate_base_plus_option_price(self) -> None:
        retail = money(850, currency="USD", context="minimum_retail_price")
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),), retail=retail),
            catalog_option("Gel Butt", amount=300),
        )
        serialized = json.dumps(result.to_dict()).casefold()
        self.assertNotIn("final_price", serialized)
        self.assertNotIn("calculated_price", serialized)
        self.assertEqual(result.retail_pricing.minimum_retail_price.amount, 850)

    def test_20_product_warning_is_propagated(self) -> None:
        result = link_one(
            product("P1", warnings=("product warning",)),
            catalog_option("Gel Butt"),
        )
        self.assertIn("product warning", result.warnings)

    def test_21_catalog_warning_is_propagated_to_link(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),)),
            catalog_option("Gel Butt", warnings=("catalog warning",)),
        )
        self.assertIn("catalog warning", result.linked_upgrade_options[0].warnings)

    def test_22_product_and_catalog_source_trace_are_preserved(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),), start_row=40),
            catalog_option("Gel Butt", coordinate="D27"),
        )
        self.assertEqual((result.source.start_row, result.source.end_row), (40, 45))
        self.assertEqual(
            result.linked_upgrade_options[0].pricing_source.raw_coordinate,
            "D27",
        )

    def test_23_explicit_alias_registry_can_link(self) -> None:
        aliases = OptionAliasRegistry.from_mapping(
            {"Mock Gel Bottom": ["Gel Butt"]}
        )
        result = link_one(
            product("P1", upgrades=(upgrade(" mock  gel bottom "),)),
            catalog_option("Gel Butt"),
            aliases=aliases,
        )
        self.assertEqual(result.linked_upgrade_options[0].match_method, "approved_alias")

    def test_24_unapproved_alias_does_not_apply(self) -> None:
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Bottom"),)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(len(result.unmatched_upgrade_options), 1)

    def test_25_alias_with_multiple_catalog_candidates_is_ambiguous(self) -> None:
        aliases = OptionAliasRegistry.from_mapping(
            {"Mock Hair Choice": ["Hair Implant", "Wigs"]}
        )
        result = link_one(
            product("P1", upgrades=(upgrade("Mock Hair Choice"),)),
            catalog_option("Hair Implant"),
            catalog_option("Wigs", coordinate="A3"),
            aliases=aliases,
        )
        ambiguous = result.ambiguous_upgrade_options[0]
        self.assertEqual(ambiguous.match_method, "approved_alias")
        self.assertEqual(len(ambiguous.catalog_candidates), 2)

    def test_26_product_record_is_not_mutated(self) -> None:
        record = product("P1", upgrades=(upgrade("Gel Butt"),))
        before = record.to_dict()
        link_one(record, catalog_option("Gel Butt"))
        self.assertEqual(record.to_dict(), before)

    def test_27_catalog_record_is_not_mutated(self) -> None:
        option = catalog_option("Gel Butt")
        before = option.to_dict()
        link_one(product("P1", upgrades=(upgrade("Gel Butt"),)), option)
        self.assertEqual(option.to_dict(), before)

    def test_28_result_order_follows_product_order(self) -> None:
        products = [
            product("P2", upgrades=(upgrade("Wigs"),)),
            product("P1", upgrades=(upgrade("Gel Butt"),)),
        ]
        results = link_products_to_options(
            products,
            [catalog_option("Gel Butt"), catalog_option("Wigs", coordinate="A3")],
        )
        self.assertEqual(
            [result.product_identity.model for result in results],
            ["P2", "P1"],
        )

    def test_29_upgrade_link_order_is_stable(self) -> None:
        record = product(
            "P1",
            upgrades=(upgrade("Wigs"), upgrade("Gel Butt")),
        )
        result = link_one(
            record,
            catalog_option("Gel Butt"),
            catalog_option("Wigs", coordinate="A3"),
        )
        self.assertEqual(
            [item.product_option.name for item in result.linked_upgrade_options],
            ["Wigs", "Gel Butt"],
        )

    def test_30_summary_product_counts(self) -> None:
        results = self._summary_fixture()
        summary = summarize_option_linking(results)
        self.assertEqual(summary.total_products, 4)
        self.assertEqual(summary.products_with_upgrade_options, 3)
        self.assertEqual(summary.products_without_options, 1)

    def test_31_summary_linked_count(self) -> None:
        summary = summarize_option_linking(self._summary_fixture())
        self.assertEqual(summary.linked_options, 1)

    def test_32_summary_unmatched_count(self) -> None:
        summary = summarize_option_linking(self._summary_fixture())
        self.assertEqual(summary.unmatched_options, 1)

    def test_33_summary_ambiguous_count(self) -> None:
        summary = summarize_option_linking(self._summary_fixture())
        self.assertEqual(summary.ambiguous_options, 1)

    def test_34_summary_included_features_and_conflicts(self) -> None:
        summary = summarize_option_linking(self._summary_fixture())
        self.assertEqual(summary.included_features_count, 2)
        self.assertEqual(summary.conflicts, 1)

    def test_35_raw_commercial_entry_is_not_upgrade_evidence(self) -> None:
        evidence = RawCommercialField(
            field="Option",
            value="Gel Butt",
            coordinate="AH18",
        )
        result = link_one(
            product("P1", raw_commercial_entries=(evidence,)),
            catalog_option("Gel Butt"),
        )
        self.assertEqual(result.linked_upgrade_options, ())

    def test_36_exact_match_takes_precedence_over_alias(self) -> None:
        aliases = OptionAliasRegistry.from_mapping(
            {"Gel Butt": ["Mock Gel Alternative"]}
        )
        result = link_one(
            product("P1", upgrades=(upgrade("Gel Butt"),)),
            catalog_option("Gel Butt"),
            catalog_option("Mock Gel Alternative", coordinate="A3"),
            aliases=aliases,
        )
        self.assertEqual(result.linked_upgrade_options[0].match_method, "exact")
        self.assertEqual(
            result.linked_upgrade_options[0].matched_catalog_option.option_name,
            "Gel Butt",
        )

    def test_37_approved_alias_also_protects_included_upgrade_conflict(self) -> None:
        aliases = OptionAliasRegistry.from_mapping(
            {"Mock Gel Bottom": ["Gel Butt"]}
        )
        result = link_one(
            product(
                "P1",
                upgrades=(upgrade("Mock Gel Bottom"),),
                included=("Gel Butt",),
            ),
            catalog_option("Gel Butt"),
            aliases=aliases,
        )
        self.assertEqual(len(result.included_upgrade_conflicts), 1)
        self.assertEqual(result.linked_upgrade_options, ())

    def test_38_linking_performs_no_network_or_file_io(self) -> None:
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network")),
            patch.object(builtins, "open", side_effect=AssertionError("file I/O")),
        ):
            result = link_one(
                product("P1", upgrades=(upgrade("Gel Butt"),)),
                catalog_option("Gel Butt"),
            )
        self.assertEqual(len(result.linked_upgrade_options), 1)

    @staticmethod
    def _summary_fixture():
        products = [
            product(
                "P1",
                upgrades=(upgrade("Gel Butt"), upgrade("Unknown")),
            ),
            product("P2", upgrades=(upgrade("Wigs"),)),
            product("P3", included=("EVO Skeleton",)),
            product(
                "P4",
                upgrades=(upgrade("Hair Implant"),),
                included=("Hair Implant",),
            ),
        ]
        catalog = [
            catalog_option("Gel Butt"),
            catalog_option("Wigs", coordinate="A3"),
            catalog_option("Wigs", coordinate="A4"),
            catalog_option("Hair Implant", coordinate="A5"),
        ]
        return link_products_to_options(products, catalog)


if __name__ == "__main__":
    unittest.main()
