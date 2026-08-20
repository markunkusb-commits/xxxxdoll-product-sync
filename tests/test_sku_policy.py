from __future__ import annotations

import copy
import socket
import sys
import unittest
import uuid
from dataclasses import replace
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
    RetailPricing,
    SupplierCosts,
    UnknownFields,
)
from sync_worker.sku_policy import (  # noqa: E402
    MAX_SKU_LENGTH,
    SKU_POLICY_VERSION,
    generate_sku,
    generate_skus,
    normalize_sku_identity,
    select_sku_identity,
    validate_sku_uniqueness,
)


def product(
    *,
    series: str = "ultra",
    model: str | None = "SiQ157cm-Miko",
    raw_model: str | None = None,
    height_model: str | None = None,
    start_row: int = 480,
    supplier_amount: int = 2250,
) -> ProductRecord:
    specifications: dict[str, str] = {"height": "157cm"}
    if height_model is not None:
        specifications["height_model"] = height_model
    return ProductRecord(
        identity=ProductIdentity(
            series=series,
            model=model,
            raw_series_title=f"{series} Series",
            raw_model=model if raw_model is None else raw_model,
        ),
        specifications=ProductSpecifications(normalized=specifications, raw=()),
        supplier_costs=SupplierCosts(
            fob_unit_price=MonetaryValue(
                raw_value=f"RMB{supplier_amount}",
                currency="RMB",
                amount=supplier_amount,
                context="fob_unit_price",
            ),
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(minimum_retail_price=None),
        options=ProductOptions(normal_options_price=None, upgrade_options=()),
        media=ProductMedia(photo_download_link="https://supplier.example/private"),
        source=ProductSource(start_row=start_row, end_row=start_row + 10),
        included_features=(),
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=(),
    )


class SkuPolicyTests(unittest.TestCase):
    def test_01_classic_sku(self) -> None:
        result = generate_sku(product(series="classic", model="J59cm"))
        self.assertEqual(result.sku, "CLM-CLASSIC-J59CM")

    def test_02_pro_sku(self) -> None:
        result = generate_sku(product(series="pro", model="PW-L31"))
        self.assertEqual(result.sku, "CLM-PRO-PW-L31")

    def test_03_ulw_sku(self) -> None:
        result = generate_sku(product(series="ulw", model="ULW-170"))
        self.assertEqual(result.sku, "CLM-ULW-ULW-170")

    def test_04_ultra_sku(self) -> None:
        result = generate_sku(product(series="ultra", model="SiQ157cm-Miko"))
        self.assertEqual(result.sku, "CLM-ULTRA-SIQ157CM-MIKO")

    def test_05_model_has_first_priority(self) -> None:
        value = product(model="PW-L31", raw_model="RAW-SECOND", height_model="HEIGHT-THIRD")
        identity, source = select_sku_identity(value)
        self.assertEqual((identity, source), ("PW-L31", "model"))

    def test_06_raw_model_is_fallback(self) -> None:
        value = product(model=None, raw_model="FD177-Zara", height_model="HEIGHT-THIRD")
        result = generate_sku(value)
        self.assertEqual(result.sku, "CLM-ULTRA-FD177-ZARA")
        self.assertEqual(result.audit.identity_source, "raw_model")

    def test_07_height_model_is_fallback(self) -> None:
        value = product(model=None, raw_model=None, height_model="SiW160cm-Imani")
        result = generate_sku(value)
        self.assertEqual(result.sku, "CLM-ULTRA-SIW160CM-IMANI")
        self.assertEqual(result.audit.identity_source, "height_model")

    def test_08_missing_identity_is_blocked(self) -> None:
        result = generate_sku(product(model=None, raw_model=None))
        self.assertEqual(result.status, "missing_identity")
        self.assertIn("missing_sku_identity", result.blocking_issues)
        self.assertIsNone(result.sku)

    def test_09_uppercase_normalization(self) -> None:
        self.assertEqual(normalize_sku_identity("SiQ157cm-Miko"), "SIQ157CM-MIKO")

    def test_10_whitespace_becomes_hyphen(self) -> None:
        self.assertEqual(normalize_sku_identity("PA Ray Butt Torso"), "PA-RAY-BUTT-TORSO")

    def test_11_repeated_whitespace_collapses(self) -> None:
        self.assertEqual(normalize_sku_identity("J60cm   XS"), "J60CM-XS")

    def test_12_repeated_hyphens_collapse(self) -> None:
        self.assertEqual(normalize_sku_identity("PW---L31"), "PW-L31")

    def test_13_slash_becomes_hyphen(self) -> None:
        self.assertEqual(normalize_sku_identity("A/B"), "A-B")

    def test_14_underscore_becomes_hyphen(self) -> None:
        self.assertEqual(normalize_sku_identity("A_B"), "A-B")

    def test_15_plus_becomes_explicit_token(self) -> None:
        self.assertEqual(normalize_sku_identity("100cm Plus+"), "100CM-PLUS-PLUS")

    def test_16_hash_is_removed(self) -> None:
        self.assertEqual(normalize_sku_identity("58# Torso"), "58-TORSO")

    def test_17_decorations_are_filtered(self) -> None:
        self.assertEqual(normalize_sku_identity("⭐ SiQ157 ❤"), "SIQ157")

    def test_18_generation_is_deterministic(self) -> None:
        value = product()
        self.assertEqual(generate_sku(value), generate_sku(value))

    def test_19_same_input_has_same_sku(self) -> None:
        value = product(model="FD160cm-Meru")
        self.assertEqual(generate_sku(value).sku, generate_sku(value).sku)

    def test_20_sku_has_no_timestamp(self) -> None:
        sku = generate_sku(product()).sku or ""
        self.assertNotRegex(sku, r"20[0-9]{2}-[0-9]{2}-[0-9]{2}")

    def test_21_generation_does_not_call_uuid(self) -> None:
        with patch.object(uuid, "uuid4", side_effect=AssertionError("UUID forbidden")):
            result = generate_sku(product())
        self.assertEqual(result.status, "ok")

    def test_22_source_row_is_not_used(self) -> None:
        first = generate_sku(product(start_row=480))
        second = generate_sku(product(start_row=999))
        self.assertEqual(first.sku, second.sku)
        self.assertNotIn("480", first.sku or "")
        self.assertNotIn("999", second.sku or "")

    def test_23_normalization_collision_is_detected(self) -> None:
        batch = generate_skus([product(model="A/B"), product(model="A_B", start_row=600)])
        self.assertEqual(len(batch.collisions), 1)
        self.assertTrue(all(result.status == "collision" for result in batch.results))

    def test_24_collision_does_not_append_counter(self) -> None:
        batch = generate_skus([product(model="A/B"), product(model="A_B")])
        self.assertEqual([result.sku for result in batch.results], ["CLM-ULTRA-A-B", "CLM-ULTRA-A-B"])
        self.assertFalse(any((result.sku or "").endswith("-2") for result in batch.results))

    def test_25_collision_does_not_append_row(self) -> None:
        batch = generate_skus([product(model="A/B", start_row=10), product(model="A_B", start_row=99)])
        self.assertNotIn("-10", batch.results[0].sku or "")
        self.assertNotIn("-99", batch.results[1].sku or "")

    def test_26_duplicate_input_has_separate_audit(self) -> None:
        batch = generate_skus([product(start_row=10), product(start_row=99)])
        self.assertEqual(len(batch.duplicate_inputs), 1)
        self.assertEqual(batch.duplicate_inputs[0].occurrences, 2)
        self.assertTrue(all(result.status == "duplicate_input" for result in batch.results))
        self.assertEqual(batch.collisions, ())

    def test_27_unsupported_series_is_blocked(self) -> None:
        result = generate_sku(product(series="future"))
        self.assertEqual(result.status, "unsupported_series")
        self.assertEqual(result.blocking_issues, ("unsupported_series",))

    def test_28_invalid_sensitive_identity_is_blocked(self) -> None:
        result = generate_sku(product(model="FOB RMB 2250"))
        self.assertEqual(result.status, "invalid_identity")
        self.assertEqual(result.blocking_issues, ("invalid_sku_identity",))

    def test_29_empty_identity_is_missing(self) -> None:
        result = generate_sku(product(model="  ", raw_model="", height_model=""))
        self.assertEqual(result.status, "missing_identity")

    def test_30_exact_maximum_length_is_allowed(self) -> None:
        prefix_length = len("CLM-ULTRA-")
        identity = "A" * (MAX_SKU_LENGTH - prefix_length)
        result = generate_sku(product(model=identity))
        self.assertEqual(len(result.sku or ""), MAX_SKU_LENGTH)
        self.assertEqual(result.status, "ok")

    def test_31_too_long_is_blocked_without_truncation(self) -> None:
        identity = "A" * MAX_SKU_LENGTH
        result = generate_sku(product(model=identity))
        self.assertEqual(result.status, "too_long")
        self.assertIn("sku_too_long", result.blocking_issues)
        self.assertTrue((result.sku or "").endswith(identity))

    def test_32_policy_version_is_in_result_and_audit(self) -> None:
        result = generate_sku(product())
        self.assertEqual(result.policy_version, SKU_POLICY_VERSION)
        self.assertEqual(result.audit.policy_version, SKU_POLICY_VERSION)

    def test_33_product_record_is_not_mutated(self) -> None:
        value = product()
        before = copy.deepcopy(value.to_dict())
        generate_skus([value])
        self.assertEqual(value.to_dict(), before)

    def test_34_batch_preserves_input_order(self) -> None:
        values = [product(series="pro", model="FD177-Zara"), product(series="classic", model="J59cm")]
        batch = generate_skus(values)
        self.assertEqual([result.sku for result in batch.results], ["CLM-PRO-FD177-ZARA", "CLM-CLASSIC-J59CM"])

    def test_35_supplier_cost_never_enters_sku(self) -> None:
        first = generate_sku(product(supplier_amount=2250))
        second = generate_sku(product(supplier_amount=9999))
        self.assertEqual(first.sku, second.sku)
        self.assertNotIn("RMB", first.sku or "")
        self.assertNotIn("2250", first.sku or "")

    def test_36_url_identity_is_blocked(self) -> None:
        result = generate_sku(product(model="https://supplier.example/model"))
        self.assertEqual(result.status, "invalid_identity")
        self.assertIsNone(result.sku)

    def test_37_policy_opens_no_network_socket(self) -> None:
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            result = generate_skus([product()])
        self.assertEqual(len(result.results), 1)

    def test_38_policy_has_no_external_write_side_effect(self) -> None:
        value = product()
        before = value.to_dict()
        generate_sku(value)
        self.assertEqual(value.to_dict(), before)

    def test_39_validate_uniqueness_uses_batch_policy(self) -> None:
        values = [product(model="A/B"), product(model="A_B")]
        self.assertEqual(validate_sku_uniqueness(values), generate_skus(values))

    def test_40_collision_records_conflicting_raw_identities(self) -> None:
        batch = generate_skus([product(model="A/B"), product(model="A_B")])
        self.assertEqual(batch.collisions[0].conflicting_product_identities, ("A/B", "A_B"))

    def test_41_pure_decoration_is_invalid(self) -> None:
        result = generate_sku(product(model="❤️ ⭐ ◆"))
        self.assertEqual(result.status, "invalid_identity")
        self.assertIsNone(result.normalized_identity)

    def test_42_fullwidth_text_is_unicode_normalized(self) -> None:
        self.assertEqual(normalize_sku_identity("ＳｉＱ１５７ｃｍ"), "SIQ157CM")

    def test_43_accented_ascii_identity_is_stable(self) -> None:
        self.assertEqual(normalize_sku_identity("Mikó"), "MIKO")

    def test_44_mapper_safe_priority_skips_url_and_uses_raw_model(self) -> None:
        value = product(model="https://unsafe.example", raw_model="SAFE-MODEL", height_model="SAFE-HEIGHT")
        result = generate_sku(value)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.raw_identity, "SAFE-MODEL")
        self.assertEqual(result.audit.identity_source, "raw_model")

    def test_45_series_is_part_of_namespace(self) -> None:
        pro = generate_sku(product(series="pro", model="SHARED"))
        ultra = generate_sku(product(series="ultra", model="SHARED"))
        self.assertNotEqual(pro.sku, ultra.sku)

    def test_46_series_case_and_whitespace_are_normalized(self) -> None:
        result = generate_sku(product(series="  Ultra ", model="Miko"))
        self.assertEqual(result.sku, "CLM-ULTRA-MIKO")

    def test_47_batch_policy_version_is_explicit(self) -> None:
        self.assertEqual(generate_skus([product()]).policy_version, SKU_POLICY_VERSION)

    def test_48_batch_rejects_non_sequence(self) -> None:
        with self.assertRaises(TypeError):
            generate_skus("not products")  # type: ignore[arg-type]

    def test_49_single_generator_rejects_non_product(self) -> None:
        with self.assertRaises(TypeError):
            generate_sku(object())  # type: ignore[arg-type]

    def test_50_result_is_a_detached_serializable_audit(self) -> None:
        result = generate_sku(product()).to_dict()
        self.assertEqual(result["sku"], "CLM-ULTRA-SIQ157CM-MIKO")
        self.assertEqual(result["audit"]["policy_version"], SKU_POLICY_VERSION)


if __name__ == "__main__":
    unittest.main()
