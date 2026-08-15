from __future__ import annotations

import builtins
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.product_model import (  # noqa: E402
    MonetaryValue,
    ProductIdentity,
    ProductMedia,
    ProductOptions,
    ProductRecord,
    ProductSource,
    ProductSpecifications,
    RawSpecificationRecord,
    RetailPricing,
    SupplierCosts,
    UnknownFields,
)
from sync_worker.product_size_enricher import (  # noqa: E402
    enrich_products_with_sizes,
    summarize_enrichment,
)
from sync_worker.size_list_parser import (  # noqa: E402
    NormalizedMeasurement,
    SizeClassification,
    SizeIdentity,
    SizeMeasurements,
    SizeRecord,
    SizeSource,
    SizeSupplierCosts,
    SupplierFOBCost,
    UnitValue,
)


def money(amount: int, *, currency: str = "RMB", context: str = "test") -> MonetaryValue:
    return MonetaryValue(
        raw_value=f"{currency}{amount}",
        currency=currency,
        amount=amount,
        context=context,
    )


def product_record(
    model: str | None,
    *,
    raw_model: str | None = None,
    specifications: dict[str, str] | None = None,
    raw_specifications: tuple[RawSpecificationRecord, ...] = (),
    warnings: tuple[str, ...] = (),
    fob: MonetaryValue | None = None,
    retail: MonetaryValue | None = None,
    source_rows: tuple[int, int] = (10, 20),
) -> ProductRecord:
    return ProductRecord(
        identity=ProductIdentity(
            series="pro",
            model=model,
            raw_series_title="CLM Pro",
            raw_model=model if raw_model is None else raw_model,
        ),
        specifications=ProductSpecifications(
            normalized=dict(specifications or {}),
            raw=raw_specifications,
        ),
        supplier_costs=SupplierCosts(
            fob_unit_price=fob,
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(minimum_retail_price=retail),
        options=ProductOptions(normal_options_price=None, upgrade_options=()),
        media=ProductMedia(photo_download_link=None),
        source=ProductSource(start_row=source_rows[0], end_row=source_rows[1]),
        included_features=(),
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=warnings,
    )


def measurement(value: int | float, unit: str = "cm") -> NormalizedMeasurement:
    return NormalizedMeasurement(
        metric=UnitValue(value=value, unit=unit),
        imperial=None,
        raw_value=f"{value}{unit}",
    )


def size_record(
    body_type: str,
    *,
    warnings: tuple[str, ...] = (),
    measurements: dict[str, NormalizedMeasurement] | None = None,
    fob: SupplierFOBCost | None = None,
    row: int = 30,
) -> SizeRecord:
    return SizeRecord(
        identity=SizeIdentity(
            body_type=body_type,
            raw_body_type=body_type,
            normalized_body_type=" ".join(body_type.split()),
            comparison_key=" ".join(body_type.split()).casefold(),
        ),
        classification=SizeClassification(type="Full Silicone", raw_type="Full Silicone"),
        supplier_costs=SizeSupplierCosts(fob_price=fob),
        measurements=SizeMeasurements(**(measurements or {})),
        raw_measurements=(),
        source=SizeSource(
            row=row,
            coordinates={"body_type": f"B{row}"},
            type_merged_range=None,
        ),
        warnings=warnings,
    )


def enrich_one(product: ProductRecord, *sizes: SizeRecord):
    return enrich_products_with_sizes([product], list(sizes))[0]


class ProductSizeEnricherTests(unittest.TestCase):
    def test_01_exact_model_match(self) -> None:
        result = enrich_one(product_record("FD140cm"), size_record("FD140cm"))
        self.assertEqual(result.match.status, "matched")
        self.assertEqual(result.match.method, "exact")

    def test_02_exact_match_is_case_insensitive(self) -> None:
        result = enrich_one(product_record("siq157CM"), size_record("SiQ157cm"))
        self.assertEqual(result.match.status, "matched")
        self.assertEqual(result.match.matched_body_type, "SiQ157cm")

    def test_03_exact_match_normalizes_unicode_and_repeated_whitespace(self) -> None:
        result = enrich_one(
            product_record("\u00a0J60cm\u2003  XS "),
            size_record("J60cm XS"),
        )
        self.assertEqual(result.match.status, "matched")

    def test_04_hash_is_preserved_in_match_key(self) -> None:
        matched = enrich_one(product_record("BW82# Torso"), size_record("BW82# Torso"))
        unmatched = enrich_one(product_record("BW82 Torso"), size_record("BW82# Torso"))
        self.assertEqual(matched.match.status, "matched")
        self.assertEqual(unmatched.match.status, "unmatched")

    def test_05_torso_is_preserved_in_match_key(self) -> None:
        matched = enrich_one(product_record("870# Torso"), size_record("870# Torso"))
        unmatched = enrich_one(product_record("870#"), size_record("870# Torso"))
        self.assertEqual(matched.match.status, "matched")
        self.assertEqual(unmatched.match.status, "unmatched")

    def test_06_size_suffix_is_preserved(self) -> None:
        result = enrich_one(
            product_record("Si60cm XL"),
            size_record("Si60cm S"),
            size_record("Si60cm XL"),
        )
        self.assertEqual(result.match.matched_body_type, "Si60cm XL")

    def test_07_plus_and_plus_plus_are_not_confused(self) -> None:
        result = enrich_one(product_record("100cm Plus"), size_record("100cm Plus+"))
        self.assertEqual(result.match.status, "unmatched")

    def test_08_siq_verified_suffix_match(self) -> None:
        result = enrich_one(product_record("SiQ157cm-Miko"), size_record("SiQ157cm"))
        self.assertEqual(result.match.method, "verified_suffix_match")
        self.assertEqual(result.match.confidence, "deterministic")

    def test_09_siw_verified_suffix_match(self) -> None:
        result = enrich_one(product_record("SiW160cm-Imani"), size_record("SiW160cm"))
        self.assertEqual(result.match.status, "matched")
        self.assertEqual(result.match.matched_body_type, "SiW160cm")

    def test_10_suffix_candidate_absent_is_unmatched(self) -> None:
        result = enrich_one(product_record("SiQ157cm-Miko"), size_record("SiW160cm"))
        self.assertEqual(result.match.status, "unmatched")
        self.assertIn("SiQ157cm", result.match.candidate_keys)

    def test_11_duplicate_suffix_candidate_is_ambiguous(self) -> None:
        result = enrich_one(
            product_record("SiQ157cm-Miko"),
            size_record("SiQ157cm", row=30),
            size_record("SiQ157cm", row=31),
        )
        self.assertEqual(result.match.status, "ambiguous")
        self.assertIsNone(result.size)

    def test_12_general_substring_matching_is_forbidden(self) -> None:
        result = enrich_one(product_record("SiQ157cmExtra"), size_record("SiQ157cm"))
        self.assertEqual(result.match.status, "unmatched")

    def test_13_null_model_without_height_identity_is_unmatched(self) -> None:
        result = enrich_one(product_record(None), size_record("FD140cm"))
        self.assertEqual(result.match.status, "unmatched")
        self.assertIsNone(result.match.product_raw_identity)

    def test_14_height_model_normalized_value_can_generate_suffix_candidate(self) -> None:
        product = product_record(
            None,
            specifications={"height_model": "SiT163-Harriet"},
        )
        result = enrich_one(product, size_record("SiT163"))
        self.assertEqual(result.match.method, "verified_suffix_match")

    def test_15_raw_height_model_field_can_generate_candidate(self) -> None:
        raw_spec = RawSpecificationRecord(
            field="Height(Model)",
            value="SiR161-Vica",
            field_coordinate="A12",
            value_coordinate="B12",
        )
        result = enrich_one(
            product_record(None, raw_specifications=(raw_spec,)),
            size_record("SiR161"),
        )
        self.assertEqual(result.match.method, "verified_suffix_match")

    def test_16_height_model_exact_candidate_matches_uniquely(self) -> None:
        product = product_record(None, specifications={"height_model": "J54cm"})
        result = enrich_one(product, size_record("J54cm"))
        self.assertEqual(result.match.method, "exact")

    def test_17_duplicate_exact_body_type_is_ambiguous(self) -> None:
        result = enrich_one(
            product_record("FD140cm"),
            size_record("FD140cm", row=30),
            size_record("FD140cm", row=31),
        )
        self.assertEqual(result.match.status, "ambiguous")
        self.assertEqual(result.match.method, "exact")

    def test_18_no_size_records_is_unmatched(self) -> None:
        result = enrich_one(product_record("FD140cm"))
        self.assertEqual(result.match.status, "unmatched")

    def test_19_product_and_size_source_trace_are_retained(self) -> None:
        result = enrich_one(
            product_record("FD140cm", source_rows=(12, 19)),
            size_record("FD140cm", row=44),
        )
        self.assertEqual((result.product.source.start_row, result.product.source.end_row), (12, 19))
        self.assertEqual(result.size.source.row, 44)

    def test_20_product_warning_is_propagated(self) -> None:
        product = product_record("FD140cm", warnings=("product warning",))
        result = enrich_one(product, size_record("FD140cm"))
        self.assertIn("product warning", result.match.warnings)

    def test_21_size_warning_is_propagated(self) -> None:
        result = enrich_one(
            product_record("FD140cm"),
            size_record("FD140cm", warnings=("size warning",)),
        )
        self.assertIn("size warning", result.match.warnings)

    def test_22_product_specifications_are_not_overwritten(self) -> None:
        product = product_record("FD140cm", specifications={"waist": "58cm"})
        size = size_record("FD140cm", measurements={"waist": measurement(60)})
        result = enrich_one(product, size)
        self.assertEqual(result.product_specifications.normalized["waist"], "58cm")
        self.assertEqual(result.size_specifications.waist.metric.value, 60)

    def test_23_specification_conflict_is_recorded_unresolved(self) -> None:
        product = product_record("FD140cm", specifications={"waist": "58cm"})
        size = size_record("FD140cm", measurements={"waist": measurement(60)})
        conflict = enrich_one(product, size).conflicts[0]
        self.assertEqual(conflict.field, "waist")
        self.assertEqual(conflict.product_value, "58cm")
        self.assertEqual(conflict.size_value.metric.value, 60)
        self.assertEqual(conflict.resolution, "unresolved")

    def test_24_equal_specification_has_no_conflict(self) -> None:
        product = product_record("FD140cm", specifications={"waist": "60cm"})
        size = size_record("FD140cm", measurements={"waist": measurement(60)})
        self.assertEqual(enrich_one(product, size).conflicts, ())

    def test_25_retail_price_is_preserved_unchanged(self) -> None:
        retail = money(850, currency="USD", context="minimum_retail_price")
        result = enrich_one(
            product_record("FD140cm", retail=retail),
            size_record("FD140cm"),
        )
        self.assertIs(result.retail_pricing.minimum_retail_price, retail)

    def test_26_size_fob_never_enters_retail_pricing(self) -> None:
        size_fob = SupplierFOBCost(amount=2200, currency="RMB", raw_value="RMB2200")
        result = enrich_one(
            product_record("FD140cm", retail=None),
            size_record("FD140cm", fob=size_fob),
        )
        self.assertIsNone(result.retail_pricing.minimum_retail_price)
        self.assertIs(result.supplier_costs.size_list_fob, size_fob)

    def test_27_product_and_size_fob_are_kept_separate(self) -> None:
        price_list_fob = money(2250, context="fob_unit_price")
        size_fob = SupplierFOBCost(amount=2200, currency="RMB", raw_value="RMB2200")
        result = enrich_one(
            product_record("FD140cm", fob=price_list_fob),
            size_record("FD140cm", fob=size_fob),
        )
        self.assertIs(result.supplier_costs.price_list_fob, price_list_fob)
        self.assertIs(result.supplier_costs.size_list_fob, size_fob)

    def test_28_supplier_fob_conflict_is_recorded_without_resolution(self) -> None:
        result = enrich_one(
            product_record("FD140cm", fob=money(2250, context="fob_unit_price")),
            size_record(
                "FD140cm",
                fob=SupplierFOBCost(2200, "RMB", "RMB2200"),
            ),
        )
        self.assertIsNotNone(result.supplier_cost_conflict)
        self.assertEqual(result.supplier_cost_conflict.resolution, "unresolved")

    def test_29_equal_supplier_fobs_do_not_create_conflict(self) -> None:
        result = enrich_one(
            product_record("FD140cm", fob=money(2200, context="fob_unit_price")),
            size_record(
                "FD140cm",
                fob=SupplierFOBCost(2200, "RMB", "RMB2200"),
            ),
        )
        self.assertIsNone(result.supplier_cost_conflict)

    def test_30_product_record_is_not_mutated(self) -> None:
        product = product_record("FD140cm", specifications={"waist": "58cm"})
        before = product.to_dict()
        enrich_one(product, size_record("FD140cm", measurements={"waist": measurement(60)}))
        self.assertEqual(product.to_dict(), before)

    def test_31_size_record_is_not_mutated(self) -> None:
        size = size_record("FD140cm", measurements={"waist": measurement(60)})
        before = size.to_dict()
        enrich_one(product_record("FD140cm", specifications={"waist": "58cm"}), size)
        self.assertEqual(size.to_dict(), before)

    def test_32_result_order_follows_product_order(self) -> None:
        products = [product_record("J60cm XS"), product_record("J54cm")]
        sizes = [size_record("J54cm"), size_record("J60cm XS")]
        results = enrich_products_with_sizes(products, sizes)
        self.assertEqual([item.product.identity.model for item in results], ["J60cm XS", "J54cm"])

    def test_33_duplicate_product_candidate_is_deduplicated(self) -> None:
        product = product_record(
            "J54cm",
            specifications={"height_model": "J54cm"},
        )
        result = enrich_one(product, size_record("J54cm"))
        self.assertEqual(result.match.status, "matched")
        self.assertEqual(result.match.candidate_keys.count("J54cm"), 1)

    def test_34_multiple_exact_product_candidates_are_ambiguous(self) -> None:
        product = product_record(
            "J54cm",
            specifications={"height_model": "J60cm XS"},
        )
        result = enrich_one(product, size_record("J54cm"), size_record("J60cm XS"))
        self.assertEqual(result.match.status, "ambiguous")
        self.assertIsNone(result.size)

    def test_35_hyphenated_product_code_is_not_treated_as_suffix_rule(self) -> None:
        result = enrich_one(product_record("PW-L31"), size_record("PW"))
        self.assertEqual(result.match.status, "unmatched")
        self.assertNotIn("PW", result.match.candidate_keys)

    def test_36_suffix_rule_requires_a_name_like_suffix(self) -> None:
        result = enrich_one(product_record("SiQ157cm-123"), size_record("SiQ157cm"))
        self.assertEqual(result.match.status, "unmatched")

    def test_37_raw_product_identity_is_preserved_after_suffix_match(self) -> None:
        raw = "  SiQ157cm-Miko  "
        product = product_record("SiQ157cm-Miko", raw_model=raw)
        result = enrich_one(product, size_record("SiQ157cm"))
        self.assertEqual(result.match.product_raw_identity, raw)
        self.assertEqual(result.match.matched_body_type, "SiQ157cm")

    def test_38_summary_exposes_future_dry_run_counts(self) -> None:
        products = [
            product_record("J54cm"),
            product_record("SiQ157cm-Miko"),
            product_record("missing"),
            product_record("FD140cm"),
        ]
        sizes = [
            size_record("J54cm"),
            size_record("SiQ157cm"),
            size_record("FD140cm", row=30),
            size_record("FD140cm", row=31),
        ]
        summary = summarize_enrichment(enrich_products_with_sizes(products, sizes))
        self.assertEqual(summary.total_products, 4)
        self.assertEqual((summary.matched, summary.unmatched, summary.ambiguous), (2, 1, 1))
        self.assertEqual((summary.exact_matches, summary.suffix_matches), (1, 1))
        self.assertEqual(summary.conflicts, 0)

    def test_39_enrichment_performs_no_file_or_network_io(self) -> None:
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network")),
            patch.object(builtins, "open", side_effect=AssertionError("file I/O")),
        ):
            result = enrich_one(product_record("FD140cm"), size_record("FD140cm"))
        self.assertEqual(result.match.status, "matched")

    def test_40_hyphenated_plus_is_not_removed_as_a_name_suffix(self) -> None:
        result = enrich_one(product_record("100cm-Plus"), size_record("100cm"))
        self.assertEqual(result.match.status, "unmatched")

    def test_41_bare_numeric_prefix_is_not_a_verified_body_token(self) -> None:
        result = enrich_one(product_record("161-Vica"), size_record("161"))
        self.assertEqual(result.match.status, "unmatched")


if __name__ == "__main__":
    unittest.main()
