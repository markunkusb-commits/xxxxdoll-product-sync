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

from sync_worker.additional_option_parser import (  # noqa: E402
    AdditionalOptionIdentity,
    AdditionalOptionPricing,
    AdditionalOptionRecord,
    AdditionalOptionSource,
)
from sync_worker.option_mapping_registry import (  # noqa: E402
    APPROVED_ALIAS_MAPPINGS,
    APPROVED_COMPOSITE_MAPPINGS,
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
from sync_worker.product_option_linker import (  # noqa: E402
    link_products_to_options,
    summarize_option_linking,
)


def upgrade(name: str, *, raw_value: str | None = None) -> UpgradeOptionRecord:
    return UpgradeOptionRecord(
        name=name,
        raw_value=raw_value or name,
        supplier_cost=None,
    )


def catalog_option(
    name: str,
    *,
    amount: Decimal | None = Decimal("300"),
    currency: str | None = "RMB",
    raw_name: str | None = None,
    raw_price: str | None = "￥300",
    coordinate: str = "A2",
    category: str = "product_extra_option",
) -> AdditionalOptionRecord:
    column = "".join(character for character in coordinate if character.isalpha())
    row = int("".join(character for character in coordinate if character.isdigit()))
    return AdditionalOptionRecord(
        identity=AdditionalOptionIdentity(
            option_name=name,
            raw_name=raw_name or name,
        ),
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
        warnings=(),
    )


def composite_catalog() -> list[AdditionalOptionRecord]:
    return [
        catalog_option("硅胶头植眉毛", coordinate="A8"),
        catalog_option("硅胶头植睫毛", coordinate="A9"),
    ]


def product(
    *upgrades: UpgradeOptionRecord,
    included: tuple[str, ...] = (),
) -> ProductRecord:
    retail = MonetaryValue(
        raw_value="US$999",
        currency="USD",
        amount=999,
        context="minimum_retail_price",
    )
    return ProductRecord(
        identity=ProductIdentity(
            series="ultra",
            model="U1",
            raw_series_title="CLM Ultra",
            raw_model="U1",
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
        included_features=included,
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=(),
    )


class OptionMappingRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = OptionMappingRegistry.approved_v1()

    def test_01_registry_has_auditable_version(self) -> None:
        self.assertEqual(self.registry.version, "clm-option-map-v1")
        self.assertEqual(self.registry.version, REGISTRY_VERSION)

    def test_02_alias_mappings_are_centralized(self) -> None:
        self.assertEqual(
            dict(APPROVED_ALIAS_MAPPINGS),
            {
                "Gel Butt": "凝胶屁股",
                "Hard Hands and Feet": "硬手硬脚(仅限硅胶)",
                "Hair Implant": "硅胶头植发",
            },
        )

    def test_03_composite_mapping_is_centralized(self) -> None:
        self.assertEqual(
            APPROVED_COMPOSITE_MAPPINGS["Eyebrows/Eyelashes Implant"],
            ("硅胶头植眉毛", "硅胶头植睫毛"),
        )

    def test_04_gel_butt_maps_to_approved_catalog_option(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel Butt"), [catalog_option("凝胶屁股")]
        )
        self.assertEqual((result.status, result.catalog_option_name), (
            "alias", "凝胶屁股"
        ))

    def test_05_hard_hands_and_feet_maps_to_approved_catalog_option(self) -> None:
        result = self.registry.resolve(
            upgrade("Hard Hands and Feet"),
            [catalog_option("硬手硬脚(仅限硅胶)")],
        )
        self.assertEqual(result.status, "alias")
        self.assertEqual(result.catalog_option_name, "硬手硬脚(仅限硅胶)")

    def test_06_hair_implant_maps_to_approved_catalog_option(self) -> None:
        result = self.registry.resolve(
            upgrade("Hair Implant"), [catalog_option("硅胶头植发")]
        )
        self.assertEqual(result.status, "alias")
        self.assertEqual(result.catalog_option_name, "硅胶头植发")

    def test_07_exact_catalog_match_has_priority_over_alias(self) -> None:
        exact = catalog_option("Gel Butt", coordinate="A2")
        alias = catalog_option("凝胶屁股", coordinate="A3")
        result = self.registry.resolve(upgrade("Gel Butt"), [alias, exact])
        self.assertEqual(result.status, "exact_catalog")
        self.assertEqual(result.source.raw_coordinate, "A2")

    def test_08_missing_alias_catalog_is_unmatched(self) -> None:
        result = self.registry.resolve(upgrade("Gel Butt"), [])
        self.assertEqual(result.status, "unmatched")
        self.assertEqual(result.missing_component_names, ("凝胶屁股",))

    def test_09_duplicate_alias_catalog_is_ambiguous(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel Butt"),
            [
                catalog_option("凝胶屁股", coordinate="A2"),
                catalog_option("凝胶屁股", coordinate="A3"),
            ],
        )
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.catalog_candidates), 2)

    def test_10_composite_mapping_is_recognized(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
        )
        self.assertEqual((result.status, result.mapping_type), (
            "composite", "composite"
        ))

    def test_11_composite_retains_both_components(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
        )
        self.assertEqual(
            [component.option_name for component in result.components],
            ["硅胶头植眉毛", "硅胶头植睫毛"],
        )

    def test_12_composite_combines_rmb_supplier_cost(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
        )
        self.assertEqual(result.combined_supplier_cost.amount, Decimal("600"))
        self.assertEqual(result.combined_supplier_cost.currency, "RMB")

    def test_13_missing_eyebrow_component_is_incomplete(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"),
            [catalog_option("硅胶头植睫毛")],
        )
        self.assertEqual(result.status, "incomplete_composite")
        self.assertEqual(result.missing_component_names, ("硅胶头植眉毛",))
        self.assertIsNone(result.combined_supplier_cost)

    def test_14_missing_eyelash_component_is_incomplete(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"),
            [catalog_option("硅胶头植眉毛")],
        )
        self.assertEqual(result.status, "incomplete_composite")
        self.assertEqual(result.missing_component_names, ("硅胶头植睫毛",))

    def test_15_duplicate_composite_component_is_ambiguous(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"),
            [
                catalog_option("硅胶头植眉毛", coordinate="A2"),
                catalog_option("硅胶头植眉毛", coordinate="A3"),
                catalog_option("硅胶头植睫毛", coordinate="A4"),
            ],
        )
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.combined_supplier_cost)

    def test_16_second_component_duplicate_is_also_ambiguous(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"),
            [
                catalog_option("硅胶头植眉毛", coordinate="A2"),
                catalog_option("硅胶头植睫毛", coordinate="A3"),
                catalog_option("硅胶头植睫毛", coordinate="A4"),
            ],
        )
        self.assertEqual(result.status, "ambiguous")

    def test_17_missing_component_price_blocks_combined_cost(self) -> None:
        catalog = composite_catalog()
        catalog[1] = catalog_option(
            "硅胶头植睫毛", amount=None, currency=None, raw_price=None
        )
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), catalog
        )
        self.assertEqual(result.status, "missing_component_price")
        self.assertIsNone(result.combined_supplier_cost)

    def test_18_composite_currency_mismatch_is_not_converted(self) -> None:
        catalog = composite_catalog()
        catalog[1] = catalog_option(
            "硅胶头植睫毛", currency="USD", raw_price="US$300"
        )
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), catalog
        )
        self.assertEqual(result.status, "currency_conflict")
        self.assertIsNone(result.combined_supplier_cost)

    def test_19_mapping_layer_retail_pricing_is_always_null(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
        )
        self.assertIsNone(result.pricing)

    def test_20_composite_does_not_call_fx_logic(self) -> None:
        with patch(
            "sync_worker.option_pricing_policy._rmb_rate"
        ) as fx_conversion:
            self.registry.resolve(
                upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
            )
        fx_conversion.assert_not_called()

    def test_21_composite_does_not_call_pricing_policy(self) -> None:
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price"
        ) as pricing_policy:
            self.registry.resolve(
                upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
            )
        pricing_policy.assert_not_called()

    def test_22_combined_supplier_cost_does_not_change_product_retail(self) -> None:
        record = product(upgrade("Eyebrows/Eyelashes Implant"))
        before = record.retail_pricing
        linked = link_products_to_options(
            [record], composite_catalog(), mapping_registry=self.registry
        )[0]
        self.assertEqual(linked.retail_pricing, before)
        self.assertEqual(
            linked.mapping_resolutions[0].combined_supplier_cost.amount,
            Decimal("600"),
        )

    def test_23_included_upgrade_conflict_blocks_registry_linking(self) -> None:
        record = product(upgrade("Gel Butt"), included=("Gel Butt",))
        linked = link_products_to_options(
            [record],
            [catalog_option("凝胶屁股")],
            mapping_registry=self.registry,
        )[0]
        self.assertEqual(len(linked.included_upgrade_conflicts), 1)
        self.assertEqual(linked.linked_upgrade_options, ())
        self.assertEqual(linked.mapping_resolutions, ())

    def test_24_raw_product_option_is_preserved(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel Butt", raw_value="1. Gel Butt"),
            [catalog_option("凝胶屁股")],
        )
        self.assertEqual(result.product_raw_value, "1. Gel Butt")

    def test_25_raw_catalog_option_is_preserved(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel Butt"),
            [catalog_option("凝胶屁股", raw_name="  凝胶屁股  ")],
        )
        self.assertEqual(result.catalog_raw_option, "  凝胶屁股  ")

    def test_26_catalog_source_coordinate_is_preserved(self) -> None:
        result = self.registry.resolve(
            upgrade("Hair Implant"),
            [catalog_option("硅胶头植发", coordinate="D17")],
        )
        self.assertEqual(result.source.raw_coordinate, "D17")

    def test_27_resolution_retains_registry_version(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel Butt"), [catalog_option("凝胶屁股")]
        )
        self.assertEqual(result.registry_version, REGISTRY_VERSION)

    def test_28_unknown_upgrade_is_unmatched(self) -> None:
        result = self.registry.resolve(
            upgrade("Unknown Upgrade"), composite_catalog()
        )
        self.assertEqual(result.status, "unmatched")
        self.assertIsNone(result.mapping_type)

    def test_29_registry_does_not_use_fuzzy_matching(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel But"), [catalog_option("凝胶屁股")]
        )
        self.assertEqual(result.status, "unmatched")

    def test_30_registry_does_not_use_substring_matching(self) -> None:
        result = self.registry.resolve(
            upgrade("Gel"), [catalog_option("凝胶屁股")]
        )
        self.assertEqual(result.status, "unmatched")

    def test_31_registry_does_not_auto_translate(self) -> None:
        result = self.registry.resolve(
            upgrade("凝胶臀部"), [catalog_option("凝胶屁股")]
        )
        self.assertEqual(result.status, "unmatched")

    def test_32_product_record_is_not_mutated(self) -> None:
        record = product(upgrade("Gel Butt", raw_value="1. Gel Butt"))
        original = copy.deepcopy(record)
        link_products_to_options(
            [record],
            [catalog_option("凝胶屁股")],
            mapping_registry=self.registry,
        )
        self.assertEqual(record, original)

    def test_33_catalog_records_are_not_mutated(self) -> None:
        catalog = composite_catalog()
        original = copy.deepcopy(catalog)
        self.registry.resolve(upgrade("Eyebrows/Eyelashes Implant"), catalog)
        self.assertEqual(catalog, original)

    def test_34_composite_component_order_is_registry_stable(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"),
            list(reversed(composite_catalog())),
        )
        self.assertEqual(
            [component.option_name for component in result.components],
            ["硅胶头植眉毛", "硅胶头植睫毛"],
        )

    def test_35_linker_integrates_approved_alias(self) -> None:
        linked = link_products_to_options(
            [product(upgrade("Gel Butt"))],
            [catalog_option("凝胶屁股")],
            mapping_registry=self.registry,
        )[0]
        self.assertEqual(linked.linked_upgrade_options[0].match_method, "approved_alias")
        self.assertEqual(
            linked.linked_upgrade_options[0].registry_version,
            REGISTRY_VERSION,
        )

    def test_36_linker_integrates_composite_as_one_customer_option(self) -> None:
        linked = link_products_to_options(
            [product(upgrade("Eyebrows/Eyelashes Implant"))],
            composite_catalog(),
            mapping_registry=self.registry,
        )[0]
        self.assertEqual(len(linked.mapping_resolutions), 1)
        self.assertEqual(linked.mapping_resolutions[0].status, "composite")
        summary = summarize_option_linking([linked])
        self.assertEqual(summary.linked_options, 1)

    def test_37_linker_keeps_existing_exact_match_priority(self) -> None:
        linked = link_products_to_options(
            [product(upgrade("Gel Butt"))],
            [catalog_option("Gel Butt"), catalog_option("凝胶屁股")],
            mapping_registry=self.registry,
        )[0]
        self.assertEqual(linked.linked_upgrade_options[0].match_method, "exact")
        self.assertEqual(linked.mapping_resolutions, ())

    def test_38_registry_performs_no_network_request(self) -> None:
        with patch.object(
            socket,
            "socket",
            side_effect=AssertionError("network access is forbidden"),
        ) as network:
            self.registry.resolve(
                upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
            )
        network.assert_not_called()

    def test_39_registry_performs_no_file_or_external_write(self) -> None:
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("file access is forbidden"),
        ) as file_open:
            self.registry.resolve(
                upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
            )
        file_open.assert_not_called()

    def test_40_resolution_serializes_decimal_without_float_conversion(self) -> None:
        result = self.registry.resolve(
            upgrade("Eyebrows/Eyelashes Implant"), composite_catalog()
        )
        self.assertEqual(
            result.to_dict()["combined_supplier_cost"]["amount"], "600"
        )
