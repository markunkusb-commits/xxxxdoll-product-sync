from __future__ import annotations

import builtins
import copy
import socket
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.category_mapping import (  # noqa: E402
    CATEGORY_REGISTRY_VERSION,
    CategoryBindingConflictError,
    CategoryRegistry,
    InvalidWooCategoryIdError,
    UnknownCategoryKeyError,
    WooCategoryBinding,
    map_categories,
    map_category,
)
from sync_worker.product_model import (  # noqa: E402
    ProductIdentity,
    ProductMedia,
    ProductOptions,
    ProductRecord,
    ProductSource,
    ProductSpecifications,
    RetailPricing,
    SupplierCosts,
    UnknownFields,
)


def product(
    *,
    series: str | None = "ultra",
    model: str | None = "SiQ157cm-Miko",
    raw_series_title: str = "Ultra Series",
    start_row: int = 480,
) -> ProductRecord:
    return ProductRecord(
        identity=ProductIdentity(
            series=series,  # type: ignore[arg-type]
            model=model,
            raw_series_title=raw_series_title,
            raw_model=model,
        ),
        specifications=ProductSpecifications(normalized={}, raw=()),
        supplier_costs=SupplierCosts(
            fob_unit_price=None,
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(minimum_retail_price=None),
        options=ProductOptions(normal_options_price=None, upgrade_options=()),
        media=ProductMedia(photo_download_link=None),
        source=ProductSource(start_row=start_row, end_row=start_row + 10),
        included_features=(),
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=(),
    )


class CategoryMappingTests(unittest.TestCase):
    def test_01_classic_mapping(self) -> None:
        result = map_category(product(series="classic"))
        self.assertEqual(result.status, "mapped_internal")

    def test_02_pro_mapping(self) -> None:
        result = map_category(product(series="pro"))
        self.assertEqual(result.status, "mapped_internal")

    def test_03_ulw_mapping(self) -> None:
        result = map_category(product(series="ulw"))
        self.assertEqual(result.status, "mapped_internal")

    def test_04_ultra_mapping(self) -> None:
        result = map_category(product(series="ultra"))
        self.assertEqual(result.status, "mapped_internal")

    def test_05_classic_key(self) -> None:
        self.assertEqual(map_category(product(series="classic")).category_key, "clm-classic")

    def test_06_pro_key(self) -> None:
        self.assertEqual(map_category(product(series="pro")).category_key, "clm-pro")

    def test_07_ulw_key(self) -> None:
        self.assertEqual(map_category(product(series="ulw")).category_key, "clm-ulw")

    def test_08_ultra_key(self) -> None:
        self.assertEqual(map_category(product(series="ultra")).category_key, "clm-ultra")

    def test_09_display_labels_are_explicit(self) -> None:
        expected = {
            "classic": "CLM Classic",
            "pro": "CLM Pro",
            "ulw": "CLM ULW",
            "ultra": "CLM Ultra",
        }
        for series, label in expected.items():
            with self.subTest(series=series):
                self.assertEqual(map_category(product(series=series)).display_name, label)

    def test_10_registry_version_is_explicit(self) -> None:
        result = map_category(product())
        self.assertEqual(result.registry_version, "clm-category-map-v1")
        self.assertEqual(result.registry_version, CATEGORY_REGISTRY_VERSION)

    def test_11_missing_none_series_is_blocked(self) -> None:
        result = map_category(product(series=None))
        self.assertEqual(result.status, "missing_series")
        self.assertEqual(result.blocking_issues, ("missing_series",))
        self.assertIsNone(result.category_key)

    def test_12_missing_blank_series_is_blocked(self) -> None:
        result = map_category(product(series="  "))
        self.assertEqual(result.status, "missing_series")

    def test_13_unsupported_series_is_blocked(self) -> None:
        result = map_category(product(series="future"))
        self.assertEqual(result.status, "unsupported_series")
        self.assertEqual(result.blocking_issues, ("unsupported_series",))
        self.assertIsNone(result.category_key)

    def test_14_product_name_does_not_guess_series(self) -> None:
        result = map_category(product(series=None, model="SiQ157cm-Miko"))
        self.assertEqual(result.status, "missing_series")

    def test_15_model_does_not_guess_category(self) -> None:
        result = map_category(product(series="future", model="FD177-Zara"))
        self.assertEqual(result.status, "unsupported_series")

    def test_16_fuzzy_series_matching_is_not_used(self) -> None:
        result = map_category(product(series="ultras"))
        self.assertEqual(result.status, "unsupported_series")

    def test_17_default_woo_id_is_null(self) -> None:
        self.assertIsNone(map_category(product()).woo_category_id)

    def test_18_positive_integer_woo_id_is_accepted(self) -> None:
        registry = CategoryRegistry([WooCategoryBinding("clm-ultra", 123)])
        self.assertEqual(registry.map_product(product()).woo_category_id, 123)

    def test_19_string_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidWooCategoryIdError):
            CategoryRegistry([WooCategoryBinding("clm-ultra", "123")])  # type: ignore[arg-type]

    def test_20_negative_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidWooCategoryIdError):
            CategoryRegistry([WooCategoryBinding("clm-ultra", -1)])

    def test_21_zero_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidWooCategoryIdError):
            CategoryRegistry([WooCategoryBinding("clm-ultra", 0)])

    def test_22_boolean_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidWooCategoryIdError):
            CategoryRegistry([WooCategoryBinding("clm-ultra", True)])  # type: ignore[arg-type]

    def test_23_unbound_result_has_mapped_internal_status(self) -> None:
        result = map_category(product())
        self.assertEqual(result.status, "mapped_internal")
        self.assertEqual(result.warnings, ("category_waiting_for_woo_binding",))

    def test_24_bound_result_has_mapped_woo_status(self) -> None:
        registry = CategoryRegistry([WooCategoryBinding("clm-ultra", 123)])
        result = registry.map_product(product())
        self.assertEqual(result.status, "mapped_woo")
        self.assertEqual(result.warnings, ())

    def test_25_multiple_products_same_category_are_allowed(self) -> None:
        batch = map_categories([product(model="Miko"), product(model="Imani")])
        self.assertEqual([item.category_key for item in batch.results], ["clm-ultra", "clm-ultra"])
        self.assertEqual(batch.summary.mapped_internal, 2)

    def test_26_registry_binding_conflict_is_blocked(self) -> None:
        with self.assertRaises(CategoryBindingConflictError):
            CategoryRegistry(
                [
                    WooCategoryBinding("clm-ultra", 123),
                    WooCategoryBinding("clm-ultra", 456),
                ]
            )

    def test_27_duplicate_identical_binding_is_allowed(self) -> None:
        registry = CategoryRegistry(
            [
                WooCategoryBinding("clm-ultra", 123),
                WooCategoryBinding("clm-ultra", 123),
            ]
        )
        self.assertEqual(registry.woo_bindings, (WooCategoryBinding("clm-ultra", 123),))
        self.assertEqual(registry.map_product(product()).woo_category_id, 123)

    def test_28_product_record_is_not_mutated(self) -> None:
        value = product()
        before = copy.deepcopy(value.to_dict())
        map_category(value)
        self.assertEqual(value.to_dict(), before)

    def test_29_single_result_is_deterministic(self) -> None:
        value = product()
        self.assertEqual(map_category(value), map_category(value))

    def test_30_batch_preserves_input_order(self) -> None:
        values = [product(series="ultra"), product(series="pro"), product(series="classic")]
        batch = map_categories(values)
        self.assertEqual([item.series for item in batch.results], ["ultra", "pro", "classic"])

    def test_31_batch_summary_total(self) -> None:
        batch = map_categories([product(series="pro"), product(series="ultra")])
        self.assertEqual(batch.summary.total_products, 2)

    def test_32_batch_summary_mapped_internal(self) -> None:
        batch = map_categories([product(series="pro"), product(series="ultra")])
        self.assertEqual(batch.summary.mapped_internal, 2)

    def test_33_batch_summary_mapped_woo(self) -> None:
        registry = CategoryRegistry([WooCategoryBinding("clm-ultra", 123)])
        batch = map_categories([product(series="ultra"), product(series="pro")], registry)
        self.assertEqual(batch.summary.mapped_woo, 1)
        self.assertEqual(batch.summary.mapped_internal, 1)

    def test_34_batch_summary_missing(self) -> None:
        batch = map_categories([product(series=None), product(series="pro")])
        self.assertEqual(batch.summary.missing_series, 1)

    def test_35_batch_summary_unsupported(self) -> None:
        batch = map_categories([product(series="future"), product(series="pro")])
        self.assertEqual(batch.summary.unsupported_series, 1)

    def test_36_batch_summary_unbound_woo(self) -> None:
        registry = CategoryRegistry([WooCategoryBinding("clm-ultra", 123)])
        batch = map_categories(
            [product(series="ultra"), product(series="pro"), product(series=None)],
            registry,
        )
        self.assertEqual(batch.summary.unbound_woo_category, 1)

    def test_37_registry_has_no_category_creation_operation(self) -> None:
        registry = CategoryRegistry()
        self.assertFalse(hasattr(registry, "create_category"))
        self.assertFalse(hasattr(registry, "create"))

    def test_38_unknown_internal_key_binding_is_rejected(self) -> None:
        with self.assertRaises(UnknownCategoryKeyError):
            CategoryRegistry([WooCategoryBinding("guessed-other", 123)])

    def test_39_mapping_opens_no_network_socket(self) -> None:
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            batch = map_categories([product(series="pro"), product(series="ultra")])
        self.assertEqual(batch.summary.total_products, 2)

    def test_40_mapping_performs_no_external_file_write(self) -> None:
        original_open = builtins.open

        def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("external write forbidden")
            return original_open(file, mode, *args, **kwargs)

        with patch.object(builtins, "open", side_effect=guarded_open):
            result = map_category(product())
        self.assertEqual(result.status, "mapped_internal")

    def test_41_batch_rejects_non_sequence(self) -> None:
        with self.assertRaises(TypeError):
            map_categories("not products")  # type: ignore[arg-type]

    def test_42_single_mapping_rejects_non_product(self) -> None:
        with self.assertRaises(TypeError):
            map_category(object())  # type: ignore[arg-type]

    def test_43_batch_rejects_non_product_member(self) -> None:
        with self.assertRaises(TypeError):
            map_categories([product(), object()])  # type: ignore[list-item]

    def test_44_registry_rejects_non_sequence_bindings(self) -> None:
        with self.assertRaises(TypeError):
            CategoryRegistry("clm-ultra=123")  # type: ignore[arg-type]

    def test_45_registry_rejects_non_binding_members(self) -> None:
        with self.assertRaises(TypeError):
            CategoryRegistry([("clm-ultra", 123)])  # type: ignore[list-item]

    def test_46_series_case_and_whitespace_normalization_is_exact(self) -> None:
        result = map_category(product(series="  Ultra "))
        self.assertEqual(result.category_key, "clm-ultra")

    def test_47_result_to_dict_is_serializable_audit_data(self) -> None:
        payload = map_category(product(series="pro")).to_dict()
        self.assertEqual(payload["category_key"], "clm-pro")
        self.assertEqual(payload["woo_category_id"], None)

    def test_48_batch_to_dict_contains_summary_and_version(self) -> None:
        payload = map_categories([product(series="pro")]).to_dict()
        self.assertEqual(payload["registry_version"], CATEGORY_REGISTRY_VERSION)
        self.assertEqual(payload["summary"]["total_products"], 1)

    def test_49_internal_definitions_have_stable_order(self) -> None:
        registry = CategoryRegistry()
        self.assertEqual(
            [item.series for item in registry.internal_definitions],
            ["classic", "pro", "ulw", "ultra"],
        )

    def test_50_batch_does_not_mutate_any_product(self) -> None:
        values = [product(series="pro"), product(series="ultra")]
        before = copy.deepcopy([value.to_dict() for value in values])
        map_categories(values)
        self.assertEqual([value.to_dict() for value in values], before)


if __name__ == "__main__":
    unittest.main()
