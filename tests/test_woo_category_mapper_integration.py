from __future__ import annotations

import copy
import json
import socket
import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.category_mapping import (  # noqa: E402
    CATEGORY_REGISTRY_VERSION,
    CategoryRegistry,
    WooCategoryBinding,
    map_category,
    map_categories,
)
from sync_worker.cli import build_parser, main  # noqa: E402
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
from sync_worker.sku_policy import generate_sku  # noqa: E402
from sync_worker.woo_category_binding import (  # noqa: E402
    STAGING_BINDING_PROFILE_VERSION,
    STAGING_EXPECTED_HOST,
    staging_category_binding_profile,
    verify_woo_category_bindings,
)
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    StdlibWooCategoryTransport,
    WooCategoryRecord,
    WooCategorySource,
)
from sync_worker.woocommerce_payload_dry_run import (  # noqa: E402
    WooCommercePayloadDryRunInputError,
    build_woocommerce_payload_report,
    restore_woo_category_records,
    run_woocommerce_payload_dry_run,
)
from sync_worker.woocommerce_product_mapper import (  # noqa: E402
    WOO_CORE_PAYLOAD_ALLOWLIST,
    build_woocommerce_product_payload,
    validate_woocommerce_product_payload,
)


def product(series: str = "pro", model: str = "FD155cm-Ada") -> ProductRecord:
    return ProductRecord(
        identity=ProductIdentity(
            series=series,
            model=model,
            raw_series_title=f"{series} Series",
            raw_model=model,
        ),
        specifications=ProductSpecifications(
            normalized={"height": "155cm"}, raw=()
        ),
        supplier_costs=SupplierCosts(
            fob_unit_price=MonetaryValue(
                raw_value="RMB2250",
                currency="RMB",
                amount=2250,
                context="fob_unit_price",
            ),
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(
            minimum_retail_price=MonetaryValue(
                raw_value="US$270",
                currency="USD",
                amount=270,
                context="minimum_retail_price",
            )
        ),
        options=ProductOptions(normal_options_price=None, upgrade_options=()),
        media=ProductMedia(
            photo_download_link="https://supplier.invalid/private.webp"
        ),
        source=ProductSource(start_row=10, end_row=20),
        included_features=(),
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=(),
    )


def record(category_id: int, name: str, parent: int = 0) -> WooCategoryRecord:
    return WooCategoryRecord(
        id=category_id,
        name=name,
        slug=name.casefold().replace(" ", "-"),
        parent=parent,
        count=1,
        description=None,
        display=None,
        parent_name=None,
        category_path=name,
        source=WooCategorySource(),
        warnings=(),
    )


def records() -> tuple[WooCategoryRecord, ...]:
    return (
        record(1412, "DOLLS"),
        record(1431, "Realistic sex dolls", 1412),
        record(1432, "Silicone sex dolls", 1412),
        record(1488, "MD DOLLS", 1432),
        record(1437, "Uncategorized"),
    )


def inputs(series: str = "pro", *, discovered=None, host=STAGING_EXPECTED_HOST):
    item = product(series, model=f"{series}-model")
    mapping = map_category(item)
    verification = verify_woo_category_bindings(
        staging_category_binding_profile(),
        environment="staging",
        host=host,
        discovery_records=records() if discovered is None else discovered,
    )
    binding = next(
        (
            result
            for result in verification.results
            if result.internal_category_key == mapping.category_key
        ),
        None,
    )
    return item, mapping, binding, verification


def candidate(series: str = "pro", *, discovered=None, host=STAGING_EXPECTED_HOST):
    item, mapping, binding, verification = inputs(
        series, discovered=discovered, host=host
    )
    return build_woocommerce_product_payload(
        item,
        sku_result=generate_sku(item),
        category_mapping_result=mapping,
        woo_category_binding_result=binding,
        category_binding_verification=verification,
    )


def dry_report(series: str = "pro", *, discovered=None, host=STAGING_EXPECTED_HOST):
    return build_woocommerce_payload_report(
        [product(series, model=f"{series}-model")],
        [],
        [],
        product_input_file="mock-products.json",
        size_input_file="mock-sizes.json",
        presented_option_input_file="mock-options.json",
        category_binding_profile_version=STAGING_BINDING_PROFILE_VERSION,
        woo_category_discovery_records=(
            records() if discovered is None else discovered
        ),
        woo_category_discovery_input_file="mock-discovery.json",
        target_base_url=f"https://{host}",
    )


def discovery_report() -> dict[str, object]:
    return {
        "status": "ok",
        "categories": json.loads(
            json.dumps([item.to_dict() for item in records()])
        ),
        "network_requests_performed": 1,
        "write_requests_performed": 0,
    }


class WooCategoryMapperIntegrationTests(unittest.TestCase):
    def test_01_mapper_accepts_category_mapping_result(self) -> None:
        _, mapping, _, _ = inputs()
        self.assertEqual(mapping.category_key, "clm-pro")

    def test_02_mapper_accepts_verified_binding_result(self) -> None:
        self.assertEqual(candidate().payload["categories"], [{"id": 1431}])

    def test_03_pro_payload_uses_1431(self) -> None:
        self.assertEqual(candidate("pro").payload["categories"], [{"id": 1431}])

    def test_04_ultra_payload_uses_1432(self) -> None:
        self.assertEqual(candidate("ultra").payload["categories"], [{"id": 1432}])

    def test_05_categories_is_allowlisted(self) -> None:
        self.assertIn("categories", WOO_CORE_PAYLOAD_ALLOWLIST)

    def test_06_category_item_has_id_only(self) -> None:
        self.assertEqual(set(candidate().payload["categories"][0]), {"id"})

    def test_07_category_id_is_positive_integer(self) -> None:
        value = candidate().payload["categories"][0]["id"]
        self.assertIs(type(value), int)
        self.assertGreater(value, 0)

    def test_08_category_name_not_in_payload(self) -> None:
        self.assertNotIn("Realistic sex dolls", json.dumps(candidate().payload))

    def test_09_category_slug_not_in_payload(self) -> None:
        self.assertNotIn("slug", json.dumps(candidate().payload))

    def test_10_internal_key_not_in_payload(self) -> None:
        self.assertNotIn("clm-pro", json.dumps(candidate().payload))

    def test_11_default_does_not_select_staging_profile(self) -> None:
        item = product()
        result = build_woocommerce_product_payload(item, sku_result=generate_sku(item))
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_binding_not_selected", result.warnings)

    def test_12_default_has_no_old_category_warning(self) -> None:
        item = product()
        result = build_woocommerce_product_payload(item, sku_result=generate_sku(item))
        self.assertNotIn("category_mapping_not_configured", result.warnings)

    def test_13_unknown_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            WooCommercePayloadDryRunInputError,
            "unknown_category_binding_profile",
        ):
            build_woocommerce_payload_report(
                [product()], [], [],
                product_input_file="p", size_input_file="s",
                presented_option_input_file="o",
                category_binding_profile_version="fake-profile",
            )

    def test_14_cli_rejects_unknown_profile(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["build-woocommerce-payloads", "--products", "p", "--sizes", "s",
                 "--presented-options", "o", "--category-binding-profile", "fake-profile"]
            )

    def test_15_exact_staging_host_verifies(self) -> None:
        self.assertEqual(candidate().audit["category"]["target_host"], STAGING_EXPECTED_HOST)

    def test_16_production_host_is_blocked(self) -> None:
        result = candidate(host="xxxxdoll.com")
        self.assertIn("category_binding_environment_mismatch", result.blocking_issues)
        self.assertNotIn("categories", result.payload)

    def test_17_other_staging_host_is_blocked(self) -> None:
        result = candidate(host="other.wpcomstaging.com")
        self.assertIn("category_binding_environment_mismatch", result.blocking_issues)

    def test_18_missing_1431_is_blocked(self) -> None:
        missing = tuple(item for item in records() if item.id != 1431)
        result = candidate(discovered=missing)
        self.assertIn("binding_target_missing", result.blocking_issues)
        self.assertNotIn("categories", result.payload)

    def test_19_changed_1431_name_is_blocked(self) -> None:
        changed = tuple(
            record(item.id, "Changed", item.parent or 0) if item.id == 1431 else item
            for item in records()
        )
        result = candidate(discovered=changed)
        self.assertIn("binding_target_changed", result.blocking_issues)
        self.assertNotIn("categories", result.payload)

    def test_20_missing_1432_is_blocked(self) -> None:
        missing = tuple(item for item in records() if item.id != 1432)
        self.assertIn("binding_target_missing", candidate("ultra", discovered=missing).blocking_issues)

    def test_21_changed_1432_name_is_blocked(self) -> None:
        changed = tuple(
            record(item.id, "Changed", item.parent or 0) if item.id == 1432 else item
            for item in records()
        )
        self.assertIn("binding_target_changed", candidate("ultra", discovered=changed).blocking_issues)

    def test_22_md_dolls_is_not_fallback(self) -> None:
        missing = tuple(item for item in records() if item.id != 1432)
        self.assertNotEqual(candidate("ultra", discovered=missing).payload.get("categories"), [{"id": 1488}])

    def test_23_uncategorized_is_not_fallback(self) -> None:
        result = candidate(discovered=(record(1437, "Uncategorized"),))
        self.assertNotEqual(result.payload.get("categories"), [{"id": 1437}])

    def test_24_classic_is_unbound(self) -> None:
        result = candidate("classic")
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_unbound", result.warnings)

    def test_25_ulw_is_unbound(self) -> None:
        result = candidate("ulw")
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_unbound", result.warnings)

    def test_26_unbound_does_not_fallback(self) -> None:
        self.assertNotIn("categories", candidate("classic").payload)

    def test_27_success_removes_unbound_warning(self) -> None:
        self.assertNotIn("category_unbound", candidate("pro").warnings)

    def test_28_success_removes_not_selected_warning(self) -> None:
        self.assertNotIn("category_binding_not_selected", candidate("pro").warnings)

    def test_29_other_mapper_warnings_are_preserved(self) -> None:
        self.assertIn("images_not_mapped", candidate().warnings)
        self.assertIn("customer_description_not_generated", candidate().warnings)

    def test_30_audit_contains_registry_version(self) -> None:
        self.assertEqual(candidate().audit["category"]["internal_registry_version"], CATEGORY_REGISTRY_VERSION)

    def test_31_audit_contains_profile_version(self) -> None:
        self.assertEqual(candidate().audit["category"]["binding_profile_version"], STAGING_BINDING_PROFILE_VERSION)

    def test_32_audit_contains_verified_name(self) -> None:
        self.assertEqual(candidate().audit["category"]["verified_name"], "Realistic sex dolls")

    def test_33_audit_is_not_merged_into_payload(self) -> None:
        self.assertNotIn("binding_profile_version", json.dumps(candidate().payload))

    def test_34_validator_accepts_verified_category(self) -> None:
        self.assertNotIn("invalid_category_binding_payload", validate_woocommerce_product_payload(candidate()))

    def test_35_validator_rejects_zero_category_id(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["categories"] = [{"id": 0}]
        self.assertIn("invalid_category_binding_payload", validate_woocommerce_product_payload(raw))

    def test_36_validator_rejects_string_category_id(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["categories"] = [{"id": "1431"}]
        self.assertIn("invalid_category_binding_payload", validate_woocommerce_product_payload(raw))

    def test_37_validator_rejects_category_name_field(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["categories"] = [{"id": 1431, "name": "Realistic sex dolls"}]
        self.assertIn("invalid_category_binding_payload", validate_woocommerce_product_payload(raw))

    def test_38_validator_rejects_unverified_host(self) -> None:
        raw = candidate().to_dict()
        raw["audit"]["category"]["host_verified"] = False
        self.assertIn("invalid_category_binding_payload", validate_woocommerce_product_payload(raw))

    def test_39_validator_rejects_unverified_discovery(self) -> None:
        raw = candidate().to_dict()
        raw["audit"]["category"]["discovery_verified"] = False
        self.assertIn("invalid_category_binding_payload", validate_woocommerce_product_payload(raw))

    def test_40_restore_discovery_ignores_report_source(self) -> None:
        report = discovery_report()
        report["categories"][0]["source"] = {"endpoint": "https://supplier.invalid/private"}
        restored = restore_woo_category_records(report)
        self.assertEqual(restored[0].source, WooCategorySource())

    def test_41_report_summary_counts_bound(self) -> None:
        summary = dry_report()["summary"]
        self.assertEqual(summary["products_with_bound_category"], 1)
        self.assertEqual(summary["category_binding_verified"], 1)

    def test_42_report_summary_counts_unbound(self) -> None:
        summary = dry_report("classic")["summary"]
        self.assertEqual(summary["products_without_bound_category"], 1)
        self.assertEqual(summary["category_unbound"], 1)

    def test_43_report_summary_counts_environment_mismatch(self) -> None:
        summary = dry_report(host="xxxxdoll.com")["summary"]
        self.assertEqual(summary["category_binding_environment_mismatch"], 1)

    def test_44_report_summary_counts_missing_target(self) -> None:
        missing = tuple(item for item in records() if item.id != 1431)
        self.assertEqual(dry_report(discovered=missing)["summary"]["category_binding_target_missing"], 1)

    def test_45_report_summary_counts_changed_target(self) -> None:
        changed = tuple(record(item.id, "Changed", item.parent or 0) if item.id == 1431 else item for item in records())
        self.assertEqual(dry_report(discovered=changed)["summary"]["category_binding_target_changed"], 1)

    def test_46_cli_registers_all_binding_arguments(self) -> None:
        args = build_parser().parse_args([
            "build-woocommerce-payloads", "--products", "p", "--sizes", "s",
            "--presented-options", "o", "--category-binding-profile", STAGING_BINDING_PROFILE_VERSION,
            "--woo-category-discovery", "d", "--target-base-url", f"https://{STAGING_EXPECTED_HOST}",
        ])
        self.assertEqual(args.category_binding_profile_version, STAGING_BINDING_PROFILE_VERSION)
        self.assertEqual(args.woo_category_discovery_path, Path("d"))
        self.assertEqual(args.target_base_url, f"https://{STAGING_EXPECTED_HOST}")

    def test_47_cli_passes_binding_arguments_to_runner(self) -> None:
        mock_report = {"status": "ok", "summary": {}}
        with patch("sync_worker.cli.run_woocommerce_payload_dry_run", return_value=(mock_report, Path("r"))) as runner:
            status = main([
                "build-woocommerce-payloads", "--products", "p", "--sizes", "s",
                "--presented-options", "o", "--category-binding-profile", STAGING_BINDING_PROFILE_VERSION,
                "--woo-category-discovery", "d", "--target-base-url", f"https://{STAGING_EXPECTED_HOST}",
            ])
        self.assertEqual(status, 0)
        self.assertEqual(runner.call_args.kwargs["category_binding_profile_version"], STAGING_BINDING_PROFILE_VERSION)

    def test_48_runner_requires_discovery_before_file_reads(self) -> None:
        with self.assertRaisesRegex(WooCommercePayloadDryRunInputError, "woo_category_discovery_required"):
            run_woocommerce_payload_dry_run(
                Path("missing-p"), Path("missing-s"), Path("missing-o"),
                project_root=PROJECT_ROOT,
                category_binding_profile_version=STAGING_BINDING_PROFILE_VERSION,
                target_base_url=f"https://{STAGING_EXPECTED_HOST}",
            )

    def test_49_runner_requires_target_before_file_reads(self) -> None:
        with self.assertRaisesRegex(WooCommercePayloadDryRunInputError, "target_base_url_required"):
            run_woocommerce_payload_dry_run(
                Path("missing-p"), Path("missing-s"), Path("missing-o"),
                project_root=PROJECT_ROOT,
                category_binding_profile_version=STAGING_BINDING_PROFILE_VERSION,
                woo_category_discovery_path=Path("missing-d"),
            )

    def test_50_ready_for_write_remains_false(self) -> None:
        self.assertIs(candidate().ready_for_write, False)
        self.assertEqual(dry_report()["summary"]["ready_for_write_count"], 0)

    def test_51_public_payload_has_no_supplier_cost_or_url(self) -> None:
        serialized = json.dumps(candidate().payload, ensure_ascii=False)
        self.assertNotIn("RMB2250", serialized)
        self.assertNotIn("supplier.invalid", serialized)

    def test_52_inputs_are_not_mutated(self) -> None:
        items = records()
        before = copy.deepcopy(items)
        dry_report(discovered=items)
        self.assertEqual(items, before)

    def test_53_no_woo_api_call(self) -> None:
        with patch.object(StdlibWooCategoryTransport, "get_categories", side_effect=AssertionError("Woo API forbidden")):
            result = dry_report()
        self.assertEqual(result["network_requests_performed"], 0)

    def test_54_no_network_socket(self) -> None:
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            result = dry_report()
        self.assertEqual(result["network_requests_performed"], 0)

    def test_55_no_external_write_in_core(self) -> None:
        with patch.object(Path, "write_text", side_effect=AssertionError("external write forbidden")):
            result = dry_report()
        self.assertEqual(result["write_requests_performed"], 0)

    def test_56_deterministic_output(self) -> None:
        self.assertEqual(dry_report(), dry_report())

    def test_57_category_mapping_is_reused(self) -> None:
        products = [product()]
        with patch("sync_worker.woocommerce_payload_dry_run.category_mapping.map_categories", wraps=map_categories) as mapper:
            dry_report()
        mapper.assert_called_once()

    def test_58_mapper_receives_binding_result(self) -> None:
        with patch("sync_worker.woocommerce_payload_dry_run.woocommerce_product_mapper.build_woocommerce_product_payload", wraps=build_woocommerce_product_payload) as mapper:
            dry_report()
        self.assertIsNotNone(mapper.call_args.kwargs["woo_category_binding_result"])

    def test_59_report_does_not_expose_discovery_path_metadata(self) -> None:
        report = dry_report()
        public = json.dumps(report["candidates"][0]["payload"])
        for forbidden in ("category_path", "parent", "count", "source"):
            self.assertNotIn(forbidden, public)

    def test_60_network_and_write_counters_are_zero(self) -> None:
        report = dry_report()
        self.assertEqual(report["network_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_61_binding_result_must_belong_to_verification(self) -> None:
        item, mapping, binding, verification = inputs()
        forged = replace(
            binding,
            woo_category_id=9999,
            expected_name="Forged",
            discovered_name="Forged",
        )
        result = build_woocommerce_product_payload(
            item,
            sku_result=generate_sku(item),
            category_mapping_result=mapping,
            woo_category_binding_result=forged,
            category_binding_verification=verification,
        )
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_binding_verification_mismatch", result.blocking_issues)

    def test_62_mapping_result_must_match_product_series(self) -> None:
        _, pro_mapping, pro_binding, verification = inputs("pro")
        ultra = product("ultra", "ultra-model")
        result = build_woocommerce_product_payload(
            ultra,
            sku_result=generate_sku(ultra),
            category_mapping_result=pro_mapping,
            woo_category_binding_result=pro_binding,
            category_binding_verification=verification,
        )
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_binding_verification_mismatch", result.blocking_issues)

    def test_63_mapper_rejects_woo_id_from_internal_registry(self) -> None:
        item, _, binding, verification = inputs("pro")
        prebound_mapping = CategoryRegistry(
            (WooCategoryBinding("clm-pro", 9999),)
        ).map_product(item)
        result = build_woocommerce_product_payload(
            item,
            sku_result=generate_sku(item),
            category_mapping_result=prebound_mapping,
            woo_category_binding_result=binding,
            category_binding_verification=verification,
        )
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_binding_verification_mismatch", result.blocking_issues)


if __name__ == "__main__":
    unittest.main()
