from __future__ import annotations

import builtins
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.option_mapping_registry import (  # noqa: E402
    REGISTRY_VERSION,
    OptionMappingRegistry,
)
from sync_worker.product_model import from_clm_product  # noqa: E402
from sync_worker.product_option_linker import (  # noqa: E402
    OptionAliasRegistry,
    link_products_to_options,
)
from sync_worker.product_option_linking_dry_run import (  # noqa: E402
    ProductOptionLinkingInputError,
    build_product_option_linking_report,
    load_local_json_report,
    restore_option_records,
    restore_product_records,
    run_product_option_linking_dry_run,
    select_option_mapping_registry,
)


TEST_KEY = "ck_" + "x" * 24


def price(amount: int, *, currency: str, context: str) -> dict[str, object]:
    return {
        "raw_value": f"{currency}{amount}",
        "currency": currency,
        "amount": amount,
        "context": context,
    }


def upgrade(name: str, *, raw_value: str | None = None) -> dict[str, object]:
    return {
        "name": name,
        "raw_value": raw_value or name,
        "price": None,
    }


def product_item(
    model: str,
    *,
    upgrades: list[dict[str, object]] | None = None,
    included: list[str] | None = None,
    warnings: list[str] | None = None,
    start_row: int = 10,
    retail: int | None = None,
) -> dict[str, object]:
    return {
        "series": "pro",
        "raw_series_title": "CLM Pro",
        "model": model,
        "specifications": {},
        "pricing": {
            "fob_unit_price": None,
            "minimum_retail_price": (
                price(retail, currency="USD", context="minimum_retail_price")
                if retail is not None
                else None
            ),
            "normal_options_price": None,
            "body_only_price": None,
            "including_head_price": None,
        },
        "included_features": list(included or []),
        "upgrade_options": list(upgrades or []),
        "notices": [],
        "source": {"start_row": start_row, "end_row": start_row + 5},
        "warnings": list(warnings or []),
        "photo_download_link": "https://supplier.example/private-image.jpg",
    }


def option_item(
    name: str,
    *,
    category: str = "product_extra_option",
    amount: str | None = "500.00",
    currency: str | None = "RMB",
    raw_price: str | None = "￥500.00",
    coordinate: str = "A2",
    warnings: list[str] | None = None,
    price_range: str | None = None,
    price_anchor: str | None = None,
    shared: bool = False,
) -> dict[str, object]:
    return {
        "category": category,
        "option_name": name,
        "price": {
            "amount": amount,
            "currency": currency if amount is not None else None,
            "raw_price": raw_price,
            "price_range": price_range,
            "price_anchor": price_anchor,
            "shared_price_source": shared,
        },
        "source_coordinate": coordinate,
        "warnings": list(warnings or []),
    }


def fixture_reports() -> tuple[dict[str, object], dict[str, object]]:
    product_report = {
        "status": "ok",
        "products": [
            product_item(
                "P1",
                upgrades=[upgrade("Gel Butt", raw_value="Gel Butt +￥500")],
                warnings=[
                    "https://supplier.example/private?token=hidden "
                    f"{TEST_KEY} Authorization: Bearer hidden Cookie=session"
                ],
                start_row=10,
                retail=999,
            ),
            product_item("P2", included=["Gel Butt"], start_row=20),
            product_item(
                "P3",
                upgrades=[upgrade("Unknown Upgrade")],
                start_row=30,
            ),
            product_item("P4", upgrades=[upgrade("Duplicate")], start_row=40),
            product_item(
                "P5",
                upgrades=[upgrade("Gel Butt")],
                included=["Gel Butt"],
                start_row=50,
            ),
            product_item("P6", start_row=60),
        ],
    }
    option_report = {
        "status": "ok",
        "options": [
            option_item(
                "Gel Butt",
                warnings=["catalog warning"],
                coordinate="A2",
                price_range="B2:B3",
                price_anchor="B2",
                shared=True,
            ),
            option_item("Duplicate", amount="300", raw_price="￥300", coordinate="A3"),
            option_item("Duplicate", amount="350", raw_price="￥350", coordinate="A4"),
            option_item(
                "Wig",
                category="accessory",
                amount="100",
                raw_price="￥100",
                coordinate="D2",
            ),
            option_item(
                "硅胶头植发",
                amount="200",
                raw_price="￥200",
                coordinate="A5",
            ),
        ],
    }
    return product_report, option_report


def restored_fixture():
    product_report, option_report = fixture_reports()
    return (
        restore_product_records(product_report),
        restore_option_records(option_report),
    )


def approved_fixture_reports() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "products": [
                product_item(
                    "U1",
                    upgrades=[
                        upgrade("Gel Butt", raw_value="1. Gel Butt"),
                        upgrade("Hair Implant", raw_value="2. Hair Implant"),
                        upgrade(
                            "Eyebrows/Eyelashes Implant",
                            raw_value="3. Eyebrows/Eyelashes Implant",
                        ),
                        upgrade(
                            "Hard Hands and Feet",
                            raw_value="4. Hard Hands and Feet",
                        ),
                    ],
                    retail=999,
                ),
                product_item(
                    "E1",
                    upgrades=[upgrade("Exact Option")],
                    start_row=30,
                ),
                product_item(
                    "C1",
                    upgrades=[upgrade("Gel Butt")],
                    included=["Gel Butt"],
                    start_row=40,
                ),
            ]
        },
        {
            "options": [
                option_item("凝胶屁股", amount="500", coordinate="A2"),
                option_item("硅胶头植发", amount="500", coordinate="A3"),
                option_item(
                    "硬手硬脚(仅限硅胶)", amount="400", coordinate="A4"
                ),
                option_item("硅胶头植眉毛", amount="300", coordinate="A5"),
                option_item("硅胶头植睫毛", amount="300", coordinate="A6"),
                option_item("Exact Option", amount="250", coordinate="A7"),
            ]
        },
    )


def enabled_report_for(
    product_report: dict[str, object],
    option_report: dict[str, object],
) -> dict[str, object]:
    return build_product_option_linking_report(
        restore_product_records(product_report),
        restore_option_records(option_report),
        product_input_file="mock-products.json",
        option_input_file="mock-options.json",
        mapping_registry_version=REGISTRY_VERSION,
    )


def approved_built_report() -> dict[str, object]:
    return enabled_report_for(*approved_fixture_reports())


def built_report() -> dict[str, object]:
    products, options = restored_fixture()
    return build_product_option_linking_report(
        products,
        options,
        product_input_file="mock-products.json",
        option_input_file="mock-options.json",
    )


class ProductOptionLinkingDryRunTests(unittest.TestCase):
    def test_01_cli_command_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            [
                "link-product-options",
                "--products",
                "products.json",
                "--options",
                "options.json",
            ]
        )
        self.assertEqual(arguments.command, "link-product-options")
        self.assertEqual(arguments.product_input_path, Path("products.json"))
        self.assertEqual(arguments.option_input_path, Path("options.json"))

    def test_02_products_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["link-product-options", "--options", "options.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_03_options_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["link-product-options", "--products", "products.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_04_product_json_is_read_locally(self) -> None:
        product_report, _ = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "products.json"
            path.write_text(json.dumps(product_report), encoding="utf-8")
            products = restore_product_records(load_local_json_report(path))
        self.assertEqual(len(products), 6)
        self.assertEqual(products[0].identity.model, "P1")

    def test_05_option_json_is_read_locally(self) -> None:
        _, option_report = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps(option_report), encoding="utf-8")
            options = restore_option_records(load_local_json_report(path))
        self.assertEqual(len(options), 5)
        self.assertEqual(options[0].identity.option_name, "Gel Butt")

    def test_06_product_restore_reuses_from_clm_product(self) -> None:
        product_report, _ = fixture_reports()
        with patch(
            "sync_worker.product_option_linking_dry_run.from_clm_product",
            wraps=from_clm_product,
        ) as converter:
            products = restore_product_records(product_report)
        self.assertEqual(len(products), 6)
        self.assertEqual(converter.call_count, 6)

    def test_07_report_reuses_existing_product_option_linker(self) -> None:
        products, options = restored_fixture()
        with (
            patch(
                "sync_worker.product_option_linking_dry_run.link_products_to_options",
                wraps=link_products_to_options,
            ) as linker,
            patch.object(
                OptionMappingRegistry,
                "resolve",
                side_effect=AssertionError("disabled registry must not resolve"),
            ) as resolver,
        ):
            build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
            )
        linker.assert_called_once()
        self.assertIs(linker.call_args.args[0], products)
        self.assertIs(linker.call_args.args[1], options)
        resolver.assert_not_called()

    def test_08_default_alias_registry_is_explicitly_empty(self) -> None:
        products, options = restored_fixture()
        with patch(
            "sync_worker.product_option_linking_dry_run.link_products_to_options",
            wraps=link_products_to_options,
        ) as linker:
            build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
            )
        registry = linker.call_args.kwargs["alias_registry"]
        self.assertIsInstance(registry, OptionAliasRegistry)
        self.assertEqual(registry.entries, ())

    def test_09_exact_match_summary(self) -> None:
        self.assertEqual(built_report()["summary"]["linked_options"], 1)
        linked = built_report()["results"][0]["linked_upgrade_options"][0]
        self.assertEqual(linked["match_method"], "exact")

    def test_10_unmatched_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["unmatched_options"], 1)
        self.assertEqual(
            report["results"][2]["unmatched_upgrade_options"][0][
                "product_raw_option"
            ],
            "Unknown Upgrade",
        )

    def test_11_ambiguous_summary(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["ambiguous_options"], 1)
        candidates = report["results"][3]["ambiguous_upgrade_options"][0][
            "catalog_candidates"
        ]
        self.assertEqual(len(candidates), 2)

    def test_12_products_without_upgrade_options_are_counted(self) -> None:
        summary = built_report()["summary"]
        self.assertEqual(summary["products_with_upgrade_options"], 4)
        self.assertEqual(summary["products_without_upgrade_options"], 2)

    def test_13_included_feature_is_not_linked_as_paid_option(self) -> None:
        result = built_report()["results"][1]
        self.assertEqual(result["included_features"], ["Gel Butt"])
        self.assertEqual(result["raw_upgrade_options"], [])
        self.assertEqual(result["linked_upgrade_options"], [])

    def test_14_included_and_upgrade_conflict_is_retained(self) -> None:
        report = built_report()
        result = report["results"][4]
        self.assertEqual(report["summary"]["included_upgrade_conflicts"], 1)
        self.assertEqual(len(result["included_upgrade_conflicts"]), 1)
        self.assertEqual(result["linked_upgrade_options"], [])
        self.assertIn("both included and upgrade", result["warnings"][0])

    def test_15_accessory_is_not_automatically_attached(self) -> None:
        serialized = json.dumps(built_report(), ensure_ascii=False)
        self.assertNotIn('"matched_catalog_option": "Wig"', serialized)

    def test_16_supplier_pricing_snapshot_is_preserved(self) -> None:
        linked = built_report()["results"][0]["linked_upgrade_options"][0]
        pricing = linked["supplier_pricing"]
        self.assertEqual(pricing["amount"], "500.00")
        self.assertEqual(pricing["currency"], "RMB")
        self.assertEqual(pricing["raw_price"], "￥500.00")

    def test_17_option_pricing_policy_is_not_called(self) -> None:
        products, options = restored_fixture()
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price"
        ) as pricing_policy:
            build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
            )
        pricing_policy.assert_not_called()

    def test_18_product_minimum_retail_price_is_not_modified(self) -> None:
        products, options = restored_fixture()
        before = products[0].retail_pricing.minimum_retail_price
        report = build_product_option_linking_report(
            products,
            options,
            product_input_file="products.json",
            option_input_file="options.json",
        )
        after = products[0].retail_pricing.minimum_retail_price
        self.assertEqual(before, after)
        self.assertEqual(
            report["results"][0]["retail_pricing"]["minimum_retail_price"][
                "amount"
            ],
            999,
        )

    def test_19_product_and_catalog_source_trace_are_preserved(self) -> None:
        result = built_report()["results"][0]
        linked = result["linked_upgrade_options"][0]
        self.assertEqual(result["source_rows"], {"start_row": 10, "end_row": 15})
        self.assertEqual(linked["catalog_source_coordinate"], "A2")

    def test_20_product_and_catalog_warnings_are_propagated(self) -> None:
        result = built_report()["results"][0]
        linked = result["linked_upgrade_options"][0]
        self.assertEqual(linked["warnings"], ["catalog warning"])
        self.assertTrue(result["warnings"])

    def test_21_urls_and_credentials_do_not_enter_report(self) -> None:
        serialized = json.dumps(built_report(), ensure_ascii=False)
        for forbidden in (
            "supplier.example",
            "token=hidden",
            TEST_KEY,
            "Bearer hidden",
            "Cookie=session",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_22_report_build_performs_no_network_request(self) -> None:
        products, options = restored_fixture()
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            report = build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
            )
        self.assertEqual(report["network_requests_performed"], 0)
        create_connection.assert_not_called()
        socket_connect.assert_not_called()

    def test_23_report_build_performs_no_external_or_file_write(self) -> None:
        products, options = restored_fixture()
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            report = build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
            )
        self.assertEqual(report["write_requests_performed"], 0)
        open_mock.assert_not_called()

    def test_24_total_upgrade_options_are_counted(self) -> None:
        self.assertEqual(built_report()["summary"]["total_upgrade_options"], 4)

    def test_25_raw_upgrade_options_are_preserved(self) -> None:
        result = built_report()["results"][0]
        self.assertEqual(result["raw_upgrade_options"], ["Gel Butt +￥500"])

    def test_26_no_automatic_chinese_english_alias_or_fuzzy_match(self) -> None:
        product_report = {
            "products": [product_item("Alias", upgrades=[upgrade("Hair Implant")])]
        }
        option_report = {"options": [option_item("硅胶头植发")]}
        report = build_product_option_linking_report(
            restore_product_records(product_report),
            restore_option_records(option_report),
            product_input_file="products.json",
            option_input_file="options.json",
        )
        self.assertEqual(report["summary"]["linked_options"], 0)
        self.assertEqual(report["summary"]["unmatched_options"], 1)

    def test_27_shared_supplier_price_provenance_is_preserved(self) -> None:
        pricing = built_report()["results"][0]["linked_upgrade_options"][0][
            "supplier_pricing"
        ]
        self.assertEqual(pricing["price_range"], "B2:B3")
        self.assertEqual(pricing["price_anchor"], "B2")
        self.assertTrue(pricing["shared_price_source"])

    def test_28_cli_uses_local_files_without_config_or_network(self) -> None:
        product_report, option_report = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products_path = root / "products.json"
            options_path = root / "options.json"
            products_path.write_text(json.dumps(product_report), encoding="utf-8")
            options_path.write_text(json.dumps(option_report), encoding="utf-8")
            with (
                patch.object(socket, "socket", side_effect=AssertionError("network")),
                patch(
                    "sync_worker.cli.load_config",
                    side_effect=AssertionError(".env configuration"),
                ) as wp_config,
                patch(
                    "sync_worker.cli.load_google_config",
                    side_effect=AssertionError("Google configuration"),
                ) as google_config,
                patch(
                    "sync_worker.cli.OfficialGoogleClientFactory",
                    side_effect=AssertionError("Google client"),
                ) as google_factory,
                patch("sync_worker.cli.PROJECT_ROOT", root),
            ):
                exit_code = main(
                    [
                        "link-product-options",
                        "--products",
                        str(products_path),
                        "--options",
                        str(options_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            google_factory.assert_not_called()
            report_path = root / "reports" / "product-option-linking-dry-run.json"
            self.assertTrue(report_path.is_file())
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["write_requests_performed"], 0)

    def test_29_run_processes_only_the_two_explicit_input_files(self) -> None:
        product_report, option_report = fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products_path = root / "products.json"
            options_path = root / "options.json"
            products_path.write_text(json.dumps(product_report), encoding="utf-8")
            options_path.write_text(json.dumps(option_report), encoding="utf-8")
            (root / "overlap-products.json").write_text(
                json.dumps({"products": [product_item("OVERLAP")]}),
                encoding="utf-8",
            )

            report, report_path = run_product_option_linking_dry_run(
                products_path,
                options_path,
                project_root=root,
            )

        self.assertEqual(report["summary"]["total_products"], 6)
        self.assertEqual(report_path.name, "product-option-linking-dry-run.json")

    def test_30_summary_contains_exact_required_fields(self) -> None:
        self.assertEqual(
            set(built_report()["summary"]),
            {
                "total_products",
                "products_with_upgrade_options",
                "products_without_upgrade_options",
                "total_upgrade_options",
                "linked_options",
                "exact_matches",
                "alias_matches",
                "composite_matches",
                "unmatched_options",
                "ambiguous_options",
                "incomplete_composites",
                "currency_conflicts",
                "missing_component_prices",
                "included_features_count",
                "included_upgrade_conflicts",
            },
        )

    def test_31_cli_mapping_registry_defaults_to_none(self) -> None:
        arguments = build_parser().parse_args(
            [
                "link-product-options",
                "--products",
                "products.json",
                "--options",
                "options.json",
            ]
        )
        self.assertIsNone(arguments.mapping_registry_version)

    def test_32_cli_accepts_the_approved_registry_version(self) -> None:
        arguments = build_parser().parse_args(
            [
                "link-product-options",
                "--products",
                "products.json",
                "--options",
                "options.json",
                "--mapping-registry",
                REGISTRY_VERSION,
            ]
        )
        self.assertEqual(arguments.mapping_registry_version, REGISTRY_VERSION)

    def test_33_cli_rejects_unknown_registry_version(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                [
                    "link-product-options",
                    "--products",
                    "products.json",
                    "--options",
                    "options.json",
                    "--mapping-registry",
                    "fake-version",
                ]
            )
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(ProductOptionLinkingInputError):
            select_option_mapping_registry("fake-version")

    def test_34_approved_registry_version_loads_existing_registry(self) -> None:
        registry = select_option_mapping_registry(REGISTRY_VERSION)
        self.assertIsInstance(registry, OptionMappingRegistry)
        self.assertEqual(registry.version, REGISTRY_VERSION)
        self.assertEqual(len(registry.aliases), 3)
        self.assertEqual(len(registry.composites), 1)

    def test_35_default_path_passes_an_explicitly_empty_mapping_registry(
        self,
    ) -> None:
        products, options = restored_fixture()
        with patch(
            "sync_worker.product_option_linking_dry_run.link_products_to_options",
            wraps=link_products_to_options,
        ) as linker:
            build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
            )
        registry = linker.call_args.kwargs["mapping_registry"]
        self.assertIsInstance(registry, OptionMappingRegistry)
        self.assertEqual(registry.aliases, ())
        self.assertEqual(registry.composites, ())

    def test_36_default_report_marks_mapping_registry_disabled(self) -> None:
        self.assertEqual(
            built_report()["mapping_registry"],
            {"enabled": False, "version": None},
        )

    def test_37_approved_report_records_registry_version(self) -> None:
        self.assertEqual(
            approved_built_report()["mapping_registry"],
            {"enabled": True, "version": REGISTRY_VERSION},
        )

    def test_38_exact_match_remains_higher_priority_than_alias(self) -> None:
        report = enabled_report_for(
            {"products": [product_item("P1", upgrades=[upgrade("Gel Butt")])]},
            {
                "options": [
                    option_item("凝胶屁股", coordinate="A2"),
                    option_item("Gel Butt", coordinate="A3"),
                ]
            },
        )
        linked = report["results"][0]["linked_upgrade_options"][0]
        self.assertEqual(linked["mapping_type"], "exact")
        self.assertEqual(linked["catalog_source_coordinate"], "A3")
        self.assertEqual(report["summary"]["alias_matches"], 0)

    def test_39_gel_butt_alias_report_structure(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ][0]
        self.assertEqual(linked["product_upgrade_name"], "Gel Butt")
        self.assertEqual(linked["product_raw_value"], "1. Gel Butt")
        self.assertEqual(linked["mapping_type"], "alias")
        self.assertEqual(linked["catalog_option_name"], "凝胶屁股")
        self.assertEqual(linked["supplier_cost"]["amount"], "500")

    def test_40_hair_implant_alias_is_enabled(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ][1]
        self.assertEqual(linked["product_upgrade_name"], "Hair Implant")
        self.assertEqual(linked["catalog_option_name"], "硅胶头植发")

    def test_41_hard_hands_and_feet_alias_is_enabled(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ][2]
        self.assertEqual(linked["product_upgrade_name"], "Hard Hands and Feet")
        self.assertEqual(linked["catalog_option_name"], "硬手硬脚(仅限硅胶)")
        self.assertEqual(linked["supplier_cost"]["amount"], "400")

    def test_42_composite_is_one_linked_customer_option(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ][3]
        self.assertEqual(linked["mapping_type"], "composite")
        self.assertEqual(
            linked["product_upgrade_name"], "Eyebrows/Eyelashes Implant"
        )
        self.assertEqual(len(linked["components"]), 2)

    def test_43_composite_combines_rmb_300_plus_rmb_300(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ][3]
        self.assertEqual(
            linked["combined_supplier_cost"],
            {"amount": "600", "currency": "RMB"},
        )

    def test_44_exact_match_summary_count(self) -> None:
        self.assertEqual(approved_built_report()["summary"]["exact_matches"], 1)

    def test_45_alias_match_summary_count(self) -> None:
        self.assertEqual(approved_built_report()["summary"]["alias_matches"], 3)

    def test_46_composite_match_summary_count(self) -> None:
        report = approved_built_report()
        self.assertEqual(report["summary"]["composite_matches"], 1)
        self.assertEqual(report["summary"]["linked_options"], 5)

    def test_47_unmatched_summary_with_registry_enabled(self) -> None:
        report = enabled_report_for(
            {
                "products": [
                    product_item("P1", upgrades=[upgrade("Unknown Upgrade")])
                ]
            },
            {"options": []},
        )
        self.assertEqual(report["summary"]["unmatched_options"], 1)

    def test_48_ambiguous_alias_summary(self) -> None:
        report = enabled_report_for(
            {"products": [product_item("P1", upgrades=[upgrade("Gel Butt")])]},
            {
                "options": [
                    option_item("凝胶屁股", coordinate="A2"),
                    option_item("凝胶屁股", coordinate="A3"),
                ]
            },
        )
        self.assertEqual(report["summary"]["ambiguous_options"], 1)

    def test_49_incomplete_composite_summary(self) -> None:
        report = enabled_report_for(
            {
                "products": [
                    product_item(
                        "P1",
                        upgrades=[upgrade("Eyebrows/Eyelashes Implant")],
                    )
                ]
            },
            {"options": [option_item("硅胶头植眉毛", amount="300")]},
        )
        self.assertEqual(report["summary"]["incomplete_composites"], 1)
        self.assertEqual(
            report["results"][0]["mapping_issues"][0]["status"],
            "incomplete_composite",
        )

    def test_50_currency_conflict_summary(self) -> None:
        report = enabled_report_for(
            {
                "products": [
                    product_item(
                        "P1",
                        upgrades=[upgrade("Eyebrows/Eyelashes Implant")],
                    )
                ]
            },
            {
                "options": [
                    option_item("硅胶头植眉毛", amount="300", currency="RMB"),
                    option_item(
                        "硅胶头植睫毛",
                        amount="50",
                        currency="USD",
                        coordinate="A3",
                    ),
                ]
            },
        )
        self.assertEqual(report["summary"]["currency_conflicts"], 1)

    def test_51_missing_component_price_summary(self) -> None:
        report = enabled_report_for(
            {
                "products": [
                    product_item(
                        "P1",
                        upgrades=[upgrade("Eyebrows/Eyelashes Implant")],
                    )
                ]
            },
            {
                "options": [
                    option_item("硅胶头植眉毛", amount="300"),
                    option_item(
                        "硅胶头植睫毛",
                        amount=None,
                        raw_price=None,
                        coordinate="A3",
                    ),
                ]
            },
        )
        self.assertEqual(report["summary"]["missing_component_prices"], 1)

    def test_52_included_conflict_precedes_registry_mapping(self) -> None:
        report = approved_built_report()
        conflict_product = report["results"][2]
        self.assertEqual(report["summary"]["included_upgrade_conflicts"], 1)
        self.assertEqual(conflict_product["linked_upgrade_options"], [])

    def test_53_registry_does_not_change_product_retail_price(self) -> None:
        report = approved_built_report()
        retail = report["results"][0]["retail_pricing"][
            "minimum_retail_price"
        ]
        self.assertEqual(retail["amount"], 999)
        self.assertEqual(retail["currency"], "USD")

    def test_54_registry_report_does_not_call_pricing_policy(self) -> None:
        products_report, options_report = approved_fixture_reports()
        with patch(
            "sync_worker.option_pricing_policy.calculate_option_retail_price"
        ) as pricing_policy:
            enabled_report_for(products_report, options_report)
        pricing_policy.assert_not_called()

    def test_55_registry_report_does_not_call_fx_logic(self) -> None:
        products_report, options_report = approved_fixture_reports()
        with patch("sync_worker.option_pricing_policy._rmb_rate") as fx_logic:
            enabled_report_for(products_report, options_report)
        fx_logic.assert_not_called()

    def test_56_registry_report_has_no_psychological_or_retail_option_price(
        self,
    ) -> None:
        serialized = json.dumps(approved_built_report(), ensure_ascii=False)
        for forbidden in (
            "target_retail_usd",
            "markup_price",
            "minimum_profit",
            "psychological_price",
            "regular_price",
            "sale_price",
            "customer_price",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_57_alias_and_composite_source_trace_is_preserved(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ]
        self.assertEqual(linked[0]["catalog_source_coordinate"], "A2")
        self.assertEqual(
            [item["source_coordinate"] for item in linked[3]["components"]],
            ["A5", "A6"],
        )

    def test_58_linked_alias_and_composite_retain_registry_version(self) -> None:
        linked = approved_built_report()["results"][0][
            "linked_upgrade_options"
        ]
        self.assertTrue(
            all(item["registry_version"] == REGISTRY_VERSION for item in linked)
        )

    def test_59_cli_enabled_registry_uses_only_local_mock_files(self) -> None:
        products_report, options_report = approved_fixture_reports()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products_path = root / "products.json"
            options_path = root / "options.json"
            products_path.write_text(json.dumps(products_report), encoding="utf-8")
            options_path.write_text(json.dumps(options_report), encoding="utf-8")
            with (
                patch.object(socket, "socket", side_effect=AssertionError("network")),
                patch(
                    "sync_worker.cli.load_config",
                    side_effect=AssertionError(".env configuration"),
                ) as wp_config,
                patch(
                    "sync_worker.cli.load_google_config",
                    side_effect=AssertionError("Google configuration"),
                ) as google_config,
                patch("sync_worker.cli.PROJECT_ROOT", root),
            ):
                exit_code = main(
                    [
                        "link-product-options",
                        "--products",
                        str(products_path),
                        "--options",
                        str(options_path),
                        "--mapping-registry",
                        REGISTRY_VERSION,
                    ]
                )
            self.assertEqual(exit_code, 0)
            wp_config.assert_not_called()
            google_config.assert_not_called()
            saved = json.loads(
                (root / "reports" / "product-option-linking-dry-run.json")
                .read_text(encoding="utf-8")
            )
        self.assertEqual(saved["mapping_registry"]["version"], REGISTRY_VERSION)
        self.assertEqual(saved["network_requests_performed"], 0)

    def test_60_enabled_report_build_performs_no_external_write(self) -> None:
        products_report, options_report = approved_fixture_reports()
        products = restore_product_records(products_report)
        options = restore_option_records(options_report)
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file access"),
        ) as open_mock:
            report = build_product_option_linking_report(
                products,
                options,
                product_input_file="products.json",
                option_input_file="options.json",
                mapping_registry_version=REGISTRY_VERSION,
            )
        open_mock.assert_not_called()
        self.assertEqual(report["write_requests_performed"], 0)


if __name__ == "__main__":
    unittest.main()
