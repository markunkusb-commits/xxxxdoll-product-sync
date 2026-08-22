from __future__ import annotations

import builtins
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

from sync_worker.additional_option_parser import AdditionalOptionPricing  # noqa: E402
from sync_worker.option_pricing_policy import (  # noqa: E402
    calculate_option_retail_price,
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
)
from sync_worker.product_option_pricing import (  # noqa: E402
    PricedLinkedOption,
    PricedMappingComponent,
    PricingMappingSnapshot,
    SupplierCostProvenance,
)
from sync_worker.product_size_enricher import (  # noqa: E402
    EnrichedSupplierCosts,
    MatchMetadata,
    ProductSizeMatchResult,
    SpecificationConflict,
)
from sync_worker.retail_price_presentation import (  # noqa: E402
    present_retail_price,
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
from sync_worker.sku_policy import (  # noqa: E402
    SKU_POLICY_VERSION,
    SkuGenerationResult,
    generate_sku,
)
from sync_worker.woocommerce_product_mapper import (  # noqa: E402
    PUBLIC_SPECIFICATION_ALLOWLIST,
    WOO_CORE_PAYLOAD_ALLOWLIST,
    PresentedUpgradeOption,
    build_woocommerce_product_payload,
    validate_woocommerce_product_payload,
)


RATE = Decimal("0.1500")


def money(
    amount: int | float | Decimal | None,
    *,
    currency: str | None,
    raw: str,
    context: str,
) -> MonetaryValue:
    return MonetaryValue(
        raw_value=raw,
        currency=currency,
        amount=amount,
        context=context,
    )


def product_record(
    *,
    series: str = "ultra",
    model: str | None = "PW-L31",
    raw_model: str | None = None,
    base_amount: int | float | Decimal | None = Decimal("2100"),
    base_currency: str | None = "USD",
    specifications: dict[str, str] | None = None,
    included_features: tuple[str, ...] = (
        "articulated fingers",
        "real oral sex",
    ),
    photo_url: str | None = "https://supplier.example/private/photo.webp",
) -> ProductRecord:
    normalized = {
        "height": "170cm",
        "upper_chest": "88cm",
        "waist": "60cm",
        "hip": "92cm",
        "net_weight": "38kg",
    }
    if specifications is not None:
        normalized = dict(specifications)
    return ProductRecord(
        identity=ProductIdentity(
            series=series,
            model=model,
            raw_series_title=f"{series} Series",
            raw_model=model if raw_model is None else raw_model,
        ),
        specifications=ProductSpecifications(normalized=normalized, raw=()),
        supplier_costs=SupplierCosts(
            fob_unit_price=money(
                Decimal("1500"),
                currency="RMB",
                raw="RMB1500",
                context="fob_unit_price",
            ),
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(
            minimum_retail_price=(
                money(
                    base_amount,
                    currency=base_currency,
                    raw=(
                        f"US${base_amount}"
                        if base_amount is not None
                        else ""
                    ),
                    context="minimum_retail_price",
                )
                if base_amount is not None or base_currency is not None
                else None
            )
        ),
        options=ProductOptions(normal_options_price=None, upgrade_options=()),
        media=ProductMedia(photo_download_link=photo_url),
        source=ProductSource(start_row=480, end_row=490),
        included_features=included_features,
        notices=(),
        unknown_fields=UnknownFields(
            raw_commercial_entries=(
                RawCommercialField(
                    field="supplier_only",
                    value="internal",
                    coordinate="AZ490",
                ),
            )
        ),
        warnings=(),
    )


def measurement(raw: str, value: int | float, unit: str) -> NormalizedMeasurement:
    return NormalizedMeasurement(
        metric=UnitValue(value=value, unit=unit),
        imperial=None,
        raw_value=raw,
    )


def size_result(
    product: ProductRecord,
    *,
    status: str = "matched",
    conflicts: tuple[SpecificationConflict, ...] = (),
) -> ProductSizeMatchResult:
    measurements = SizeMeasurements(
        upper_chest=measurement("90cm", 90, "cm"),
        waist=measurement("62cm", 62, "cm"),
        hip=measurement("94cm", 94, "cm"),
        net_weight=measurement("40kg", 40, "kg"),
    )
    size = SizeRecord(
        identity=SizeIdentity(
            body_type="PW-L31",
            raw_body_type="PW-L31",
            normalized_body_type="PW-L31",
            comparison_key="pw l31",
        ),
        classification=SizeClassification(
            type="Full Silicone",
            raw_type="Full Silicone",
        ),
        supplier_costs=SizeSupplierCosts(
            fob_price=SupplierFOBCost(
                amount=Decimal("1450"),
                currency="RMB",
                raw_value="RMB1450",
            )
        ),
        measurements=measurements,
        raw_measurements=(),
        source=SizeSource(row=2, coordinates={"upper_chest": "D2"}, type_merged_range=None),
        warnings=(),
    )
    return ProductSizeMatchResult(
        product=product,
        size=size if status == "matched" else None,
        product_specifications=product.specifications,
        size_specifications=measurements if status == "matched" else None,
        supplier_costs=EnrichedSupplierCosts(
            price_list_fob=product.supplier_costs.fob_unit_price,
            price_list_body_only_fob=None,
            price_list_including_head_fob=None,
            size_list_fob=size.supplier_costs.fob_price,
        ),
        retail_pricing=product.retail_pricing,
        match=MatchMetadata(
            status=status,  # type: ignore[arg-type]
            method="exact" if status == "matched" else None,
            product_raw_identity=product.identity.model,
            matched_body_type=("PW-L31" if status == "matched" else None),
            candidate_keys=("pw l31",),
            confidence="exact" if status == "matched" else "none",
            warnings=(),
        ),
        conflicts=conflicts,
        supplier_cost_conflict=None,
    )


def presented_option(
    name: str,
    supplier_amount: str,
    *,
    mapping_type: str = "alias",
) -> PresentedUpgradeOption:
    supplier_pricing = AdditionalOptionPricing(
        amount=Decimal(supplier_amount),
        currency="RMB",
        raw_price=f"￥{supplier_amount}",
    )
    pricing = calculate_option_retail_price(
        supplier_pricing,
        rmb_to_usd_rate=RATE,
    )
    components: tuple[PricedMappingComponent, ...] = ()
    source_coordinates = ("A2",)
    catalog_name: str | None = "目录选项"
    category: str | None = "product_extra_option"
    if mapping_type == "composite":
        components = (
            PricedMappingComponent(
                option_name="硅胶头植眉毛",
                category="product_extra_option",
                supplier_cost=AdditionalOptionPricing(
                    Decimal("300"), "RMB", "￥300"
                ),
                source_coordinate="A3",
            ),
            PricedMappingComponent(
                option_name="硅胶头植睫毛",
                category="product_extra_option",
                supplier_cost=AdditionalOptionPricing(
                    Decimal("300"), "RMB", "￥300"
                ),
                source_coordinate="A4",
            ),
        )
        source_coordinates = ("A3", "A4")
        catalog_name = None
        category = None
    option = PricedLinkedOption(
        product_upgrade_name=name,
        product_raw_value=f"1. {name}",
        mapping=PricingMappingSnapshot(
            mapping_type=mapping_type,  # type: ignore[arg-type]
            mapping_status=(
                "composite" if mapping_type == "composite" else "alias"
            ),
            registry_version="clm-option-map-v1",
            catalog_option_name=catalog_name,
            catalog_category=category,  # type: ignore[arg-type]
            components=components,
            candidate_option_names=(),
            missing_component_names=(),
            source_coordinates=source_coordinates,
        ),
        supplier_cost=SupplierCostProvenance(
            amount=Decimal(supplier_amount),
            currency="RMB",
            raw_values=(f"￥{supplier_amount}",),
            source_coordinates=source_coordinates,
        ),
        pricing=pricing,
        warnings=(),
    )
    target = pricing.retail.target_retail_usd
    return PresentedUpgradeOption(
        priced_option=option,
        presentation=present_retail_price(target),
    )


_DEFAULT_SKU_RESULT = object()


def candidate(
    product: ProductRecord | None = None,
    *,
    size: ProductSizeMatchResult | None = None,
    options: tuple[PresentedUpgradeOption, ...] = (),
    sku_result: SkuGenerationResult | None | object = _DEFAULT_SKU_RESULT,
):
    active_product = product or product_record()
    active_sku_result = (
        generate_sku(active_product)
        if sku_result is _DEFAULT_SKU_RESULT
        else sku_result
    )
    return build_woocommerce_product_payload(
        active_product,
        sku_result=active_sku_result,  # type: ignore[arg-type]
        size_enrichment=size,
        presented_options=options,
    )


class WooCommerceProductMapperTests(unittest.TestCase):
    def test_01_basic_woo_payload_candidate(self) -> None:
        result = candidate()
        self.assertEqual(
            result.payload,
            {
                "name": "PW-L31",
                "sku": "CLM-ULTRA-PW-L31",
                "type": "simple",
                "status": "draft",
                "regular_price": "2100.00",
                "attributes": result.payload["attributes"],
            },
        )
        self.assertEqual(result.blocking_issues, ())

    def test_02_api_schema_is_wc_v3_products_post_candidate(self) -> None:
        result = candidate()
        self.assertEqual(result.api["version"], "wc/v3")
        self.assertEqual(result.api["resource"], "products")
        self.assertEqual(result.api["method"], "POST")

    def test_03_product_type_is_simple(self) -> None:
        self.assertEqual(candidate().payload["type"], "simple")

    def test_04_product_status_is_draft(self) -> None:
        result = candidate()
        self.assertEqual(result.payload["status"], "draft")
        self.assertNotEqual(result.payload["status"], "publish")

    def test_05_explicit_model_is_product_name(self) -> None:
        self.assertEqual(candidate().payload["name"], "PW-L31")

    def test_06_raw_model_is_safe_name_fallback(self) -> None:
        product = product_record(model=None, raw_model="SiQ157cm-Miko")
        self.assertEqual(candidate(product).payload["name"], "SiQ157cm-Miko")

    def test_07_missing_name_is_blocking(self) -> None:
        product = product_record(model=None, raw_model=None, specifications={})
        result = candidate(product)
        self.assertNotIn("name", result.payload)
        self.assertIn("missing_product_name", result.blocking_issues)

    def test_08_usd_minimum_retail_becomes_regular_price(self) -> None:
        self.assertEqual(candidate().payload["regular_price"], "2100.00")

    def test_09_regular_price_is_formatted_to_two_decimals(self) -> None:
        product = product_record(base_amount=Decimal("2100.5"))
        self.assertEqual(candidate(product).payload["regular_price"], "2100.50")

    def test_10_missing_retail_price_is_blocking(self) -> None:
        product = product_record(base_amount=None, base_currency=None)
        result = candidate(product)
        self.assertNotIn("regular_price", result.payload)
        self.assertIn("missing_base_retail_price", result.blocking_issues)

    def test_11_non_usd_retail_price_is_blocking_without_fx(self) -> None:
        product = product_record(base_amount=Decimal("2100"), base_currency="RMB")
        result = candidate(product)
        self.assertNotIn("regular_price", result.payload)
        self.assertIn("unsupported_base_price_currency", result.blocking_issues)

    def test_12_fob_never_becomes_regular_price(self) -> None:
        product = product_record(base_amount=Decimal("2100"))
        result = candidate(product)
        self.assertEqual(result.payload["regular_price"], "2100.00")
        self.assertNotEqual(result.payload["regular_price"], "1500.00")

    def test_13_size_fob_never_enters_payload(self) -> None:
        product = product_record()
        result = candidate(product, size=size_result(product))
        serialized = json.dumps(result.payload, ensure_ascii=False)
        self.assertNotIn("1450", serialized)
        self.assertNotIn("fob", serialized.casefold())

    def test_14_option_supplier_cost_never_enters_storefront_or_payload(self) -> None:
        option = presented_option("Gel Butt", "500")
        result = candidate(options=(option,))
        exposed = json.dumps(
            {"payload": result.payload, "storefront": result.storefront_options},
            ensure_ascii=False,
        )
        self.assertNotIn("500", exposed)
        self.assertNotIn("RMB", exposed)

    def test_15_economic_target_never_enters_storefront_option(self) -> None:
        result = candidate(options=(presented_option("Gel Butt", "500"),))
        storefront = result.storefront_options[0]
        self.assertNotIn("economic", storefront)
        self.assertNotIn("112.50", storefront.values())

    def test_16_display_price_enters_storefront_option(self) -> None:
        result = candidate(options=(presented_option("Gel Butt", "500"),))
        self.assertEqual(result.storefront_options[0]["price_usd"], "119.00")

    def test_17_gel_butt_display_is_119(self) -> None:
        result = candidate(options=(presented_option("Gel Butt", "500"),))
        self.assertEqual(result.storefront_options[0], {
            "name": "Gel Butt", "price_usd": "119.00", "option_type": "paid_upgrade"
        })

    def test_18_hair_implant_display_is_119(self) -> None:
        result = candidate(options=(presented_option("Hair Implant", "500"),))
        self.assertEqual(result.storefront_options[0]["price_usd"], "119.00")

    def test_19_hard_hands_and_feet_display_is_99(self) -> None:
        result = candidate(
            options=(presented_option("Hard Hands and Feet", "400"),)
        )
        self.assertEqual(result.storefront_options[0]["price_usd"], "99.00")

    def test_20_composite_display_is_139(self) -> None:
        result = candidate(
            options=(
                presented_option(
                    "Eyebrows/Eyelashes Implant", "600", mapping_type="composite"
                ),
            )
        )
        self.assertEqual(result.storefront_options[0]["price_usd"], "139.00")

    def test_21_composite_remains_one_storefront_option(self) -> None:
        result = candidate(
            options=(
                presented_option(
                    "Eyebrows/Eyelashes Implant", "600", mapping_type="composite"
                ),
            )
        )
        self.assertEqual(len(result.storefront_options), 1)
        self.assertEqual(
            result.storefront_options[0]["name"],
            "Eyebrows/Eyelashes Implant",
        )

    def test_22_included_features_do_not_become_paid_options(self) -> None:
        result = candidate()
        self.assertEqual(result.storefront_options, ())
        storefront_text = json.dumps(result.storefront_options)
        self.assertNotIn("articulated fingers", storefront_text)

    def test_23_payload_keys_are_from_explicit_allowlist(self) -> None:
        self.assertTrue(set(candidate().payload).issubset(WOO_CORE_PAYLOAD_ALLOWLIST))
        self.assertEqual(candidate().payload["sku"], "CLM-ULTRA-PW-L31")

    def test_24_upper_chest_is_public_attribute(self) -> None:
        attributes = candidate().payload["attributes"]
        item = next(item for item in attributes if item["name"] == "Upper Chest")
        self.assertEqual(item["options"], ["88cm"])

    def test_25_waist_is_public_attribute(self) -> None:
        attributes = candidate().payload["attributes"]
        item = next(item for item in attributes if item["name"] == "Waist")
        self.assertEqual(item["options"], ["60cm"])

    def test_26_hip_is_public_attribute(self) -> None:
        attributes = candidate().payload["attributes"]
        item = next(item for item in attributes if item["name"] == "Hip")
        self.assertEqual(item["options"], ["92cm"])

    def test_27_net_weight_is_public_attribute(self) -> None:
        attributes = candidate().payload["attributes"]
        item = next(item for item in attributes if item["name"] == "Net Weight")
        self.assertEqual(item["options"], ["38kg"])

    def test_28_net_weight_does_not_map_to_woo_weight(self) -> None:
        self.assertNotIn("weight", candidate().payload)

    def test_29_attributes_are_visible(self) -> None:
        self.assertTrue(
            all(item["visible"] is True for item in candidate().payload["attributes"])
        )

    def test_30_attributes_are_not_variations(self) -> None:
        self.assertTrue(
            all(
                item["variation"] is False
                for item in candidate().payload["attributes"]
            )
        )

    def test_31_unknown_specification_is_not_public(self) -> None:
        product = product_record(specifications={"supplier_secret_spec": "hidden"})
        result = candidate(product)
        self.assertNotIn("attributes", result.payload)

    def test_32_carton_size_is_not_public_attribute(self) -> None:
        product = product_record(
            specifications={"height": "170cm", "carton_size": "160*50*40cm"}
        )
        serialized = json.dumps(candidate(product).payload)
        self.assertNotIn("Carton", serialized)
        self.assertNotIn("160*50*40cm", serialized)

    def test_33_gross_weight_is_audit_only(self) -> None:
        product = product_record(
            specifications={"height": "170cm", "gross_weight": "45kg"}
        )
        result = candidate(product)
        self.assertEqual(result.audit["shipping_candidates"]["gross_weight"], "45kg")
        self.assertNotIn("45kg", json.dumps(result.payload))

    def test_34_no_category_ids_are_guessed(self) -> None:
        result = candidate()
        self.assertNotIn("categories", result.payload)
        self.assertIn("category_mapping_not_configured", result.warnings)

    def test_35_no_images_are_guessed(self) -> None:
        result = candidate()
        self.assertNotIn("images", result.payload)
        self.assertIn("images_not_mapped", result.warnings)

    def test_36_supplier_photo_url_does_not_enter_candidate(self) -> None:
        result = candidate()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("supplier.example", serialized)
        self.assertNotIn("photo.webp", serialized)

    def test_37_description_is_not_generated(self) -> None:
        result = candidate()
        self.assertNotIn("description", result.payload)
        self.assertIn("customer_description_not_generated", result.warnings)

    def test_38_short_description_is_not_generated(self) -> None:
        self.assertNotIn("short_description", candidate().payload)

    def test_39_meta_data_does_not_contain_audit(self) -> None:
        result = candidate()
        self.assertNotIn("meta_data", result.payload)
        self.assertIn("internal_supplier_costs", result.audit)

    def test_40_source_coordinates_do_not_enter_payload(self) -> None:
        result = candidate(options=(presented_option("Gel Butt", "500"),))
        serialized = json.dumps(result.payload)
        self.assertNotIn("A2", serialized)
        self.assertNotIn("source", serialized.casefold())

    def test_41_fx_rate_does_not_enter_payload(self) -> None:
        serialized = json.dumps(candidate().payload).casefold()
        self.assertNotIn("fx", serialized)
        self.assertNotIn("0.1500", serialized)

    def test_42_margin_does_not_enter_payload(self) -> None:
        serialized = json.dumps(candidate().payload).casefold()
        self.assertNotIn("margin", serialized)
        self.assertNotIn("markup", serialized)

    def test_43_validator_rejects_unknown_payload_key(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["unknown_field"] = "unsafe"
        self.assertIn(
            "unsafe_field_detected_in_payload",
            validate_woocommerce_product_payload(raw),
        )

    def test_44_validator_rejects_credentials_in_payload(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["name"] = "Authorization: Bearer fixture"
        self.assertIn(
            "unsafe_field_detected_in_payload",
            validate_woocommerce_product_payload(raw),
        )

    def test_45_validator_rejects_url_in_payload(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["name"] = "https://supplier.example/private"
        self.assertIn(
            "unsafe_field_detected_in_payload",
            validate_woocommerce_product_payload(raw),
        )

    def test_46_validator_detects_missing_name(self) -> None:
        raw = candidate().to_dict()
        raw["payload"].pop("name")
        self.assertIn(
            "missing_product_name",
            validate_woocommerce_product_payload(raw),
        )

    def test_47_validator_detects_invalid_regular_price(self) -> None:
        for invalid in ("2100", "2,100.00", "USD2100", "-1.00"):
            with self.subTest(price=invalid):
                raw = candidate().to_dict()
                raw["payload"]["regular_price"] = invalid
                self.assertIn(
                    "invalid_regular_price",
                    validate_woocommerce_product_payload(raw),
                )

    def test_48_validator_requires_draft_status(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["status"] = "publish"
        self.assertIn(
            "invalid_product_status",
            validate_woocommerce_product_payload(raw),
        )

    def test_49_validator_requires_simple_type(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["type"] = "variable"
        self.assertIn(
            "invalid_product_type",
            validate_woocommerce_product_payload(raw),
        )

    def test_50_ready_for_write_is_always_false(self) -> None:
        self.assertIs(candidate().ready_for_write, False)
        blocked = candidate(
            product_record(model=None, raw_model=None, specifications={})
        )
        self.assertIs(blocked.ready_for_write, False)
        self.assertIs(blocked.to_dict()["ready_for_write"], False)

    def test_51_product_record_is_not_mutated(self) -> None:
        product = product_record()
        before = product.to_dict()
        candidate(product)
        self.assertEqual(product.to_dict(), before)

    def test_52_size_result_is_not_mutated(self) -> None:
        product = product_record()
        size = size_result(product)
        before = size.to_dict()
        candidate(product, size=size)
        self.assertEqual(size.to_dict(), before)

    def test_53_pricing_and_presentation_inputs_are_not_mutated(self) -> None:
        option = presented_option("Gel Butt", "500")
        before_option = copy.deepcopy(option.priced_option)
        before_presentation = option.presentation.to_dict()
        candidate(options=(option,))
        self.assertEqual(option.priced_option, before_option)
        self.assertEqual(option.presentation.to_dict(), before_presentation)

    def test_54_output_is_deterministic(self) -> None:
        product = product_record()
        options = (presented_option("Gel Butt", "500"),)
        first = candidate(product, options=options).to_dict()
        second = candidate(product, options=options).to_dict()
        self.assertEqual(first, second)

    def test_55_mapper_performs_no_network_request(self) -> None:
        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
        ):
            result = candidate()
        create_connection.assert_not_called()
        socket_connect.assert_not_called()
        self.assertIs(result.ready_for_write, False)

    def test_56_mapper_performs_no_file_or_external_write(self) -> None:
        with patch.object(
            builtins,
            "open",
            side_effect=AssertionError("unexpected file or API write"),
        ) as open_mock:
            result = candidate()
        open_mock.assert_not_called()
        self.assertIs(result.ready_for_write, False)

    def test_57_height_model_is_name_fallback(self) -> None:
        product = product_record(
            model=None,
            raw_model=None,
            specifications={"height_model": "SiQ157cm-Miko"},
        )
        self.assertEqual(candidate(product).payload["name"], "SiQ157cm-Miko")

    def test_58_verified_size_measurements_take_priority(self) -> None:
        product = product_record()
        result = candidate(product, size=size_result(product))
        attributes = result.payload["attributes"]
        upper = next(item for item in attributes if item["name"] == "Upper Chest")
        self.assertEqual(upper["options"], ["90cm"])
        self.assertIn(
            {"field": "upper_chest", "source": "verified_size_match"},
            result.audit["public_attribute_provenance"],
        )

    def test_59_conflicting_size_measurement_falls_back_to_product(self) -> None:
        product = product_record()
        conflict = SpecificationConflict(
            field="upper_chest",
            product_raw_value="88cm",
            size_raw_value="90cm",
            comparison_reason="different numeric value",
        )
        result = candidate(
            product,
            size=size_result(product, conflicts=(conflict,)),
        )
        attributes = result.payload["attributes"]
        upper = next(item for item in attributes if item["name"] == "Upper Chest")
        self.assertEqual(upper["options"], ["88cm"])

    def test_60_audit_isolated_cost_and_policy_provenance(self) -> None:
        product = product_record()
        option = presented_option("Gel Butt", "500")
        result = candidate(product, size=size_result(product), options=(option,))
        audit = result.audit
        self.assertEqual(audit["registry_versions"], ["clm-option-map-v1"])
        self.assertEqual(audit["pricing_policy_versions"], ["option-retail-v1"])
        self.assertEqual(
            audit["presentation_policy_versions"],
            ["retail-presentation-v1"],
        )
        self.assertEqual(
            audit["internal_supplier_costs"]["options"][0]["supplier_cost"][
                "amount"
            ],
            Decimal("500"),
        )
        self.assertNotIn("internal_supplier_costs", result.payload)

    def test_61_standard_unmapped_fields_are_warnings(self) -> None:
        self.assertEqual(
            candidate().warnings,
            (
                "category_mapping_not_configured",
                "images_not_mapped",
                "customer_description_not_generated",
            ),
        )

    def test_62_unmatched_size_enrichment_is_warning_only(self) -> None:
        product = product_record()
        result = candidate(product, size=size_result(product, status="unmatched"))
        self.assertIn("size_enrichment_unmatched", result.warnings)
        self.assertEqual(result.blocking_issues, ())

    def test_63_validator_rejects_variable_attribute(self) -> None:
        raw = candidate().to_dict()
        raw["payload"]["attributes"][0]["variation"] = True
        self.assertIn(
            "attribute_variation_must_be_false",
            validate_woocommerce_product_payload(raw),
        )

    def test_64_validator_rejects_display_below_economic_target(self) -> None:
        raw = candidate(
            options=(presented_option("Gel Butt", "500"),)
        ).to_dict()
        raw["storefront_options"][0]["price_usd"] = "100.00"
        self.assertIn(
            "invalid_storefront_option_price",
            validate_woocommerce_product_payload(raw),
        )

    def test_65_validator_rejects_split_composite_component(self) -> None:
        raw = candidate(
            options=(
                presented_option(
                    "Eyebrows/Eyelashes Implant",
                    "600",
                    mapping_type="composite",
                ),
            )
        ).to_dict()
        raw["storefront_options"].append(
            {
                "name": "硅胶头植眉毛",
                "price_usd": "69.00",
                "option_type": "paid_upgrade",
            }
        )
        self.assertIn(
            "composite_option_was_split",
            validate_woocommerce_product_payload(raw),
        )

    def test_66_mapper_accepts_external_sku_result(self) -> None:
        product = product_record()
        sku_result = generate_sku(product)
        result = build_woocommerce_product_payload(product, sku_result=sku_result)
        self.assertEqual(result.payload["sku"], sku_result.sku)

    def test_67_sku_is_written_only_to_payload_and_audit(self) -> None:
        result = candidate()
        self.assertEqual(result.payload["sku"], "CLM-ULTRA-PW-L31")
        self.assertEqual(result.audit["sku"]["value"], "CLM-ULTRA-PW-L31")

    def test_68_sku_is_not_written_to_description(self) -> None:
        result = candidate()
        self.assertNotIn("description", result.payload)
        self.assertNotIn(
            result.payload["sku"],
            str(result.payload.get("description", "")),
        )

    def test_69_sku_is_not_written_to_public_content(self) -> None:
        result = candidate()
        self.assertNotIn(result.payload["sku"], json.dumps(result.public_content))

    def test_70_sku_is_not_written_to_storefront_options(self) -> None:
        result = candidate(options=(presented_option("Gel Butt", "500"),))
        self.assertNotIn(
            result.payload["sku"],
            json.dumps(result.storefront_options),
        )

    def test_71_fd160_meru_sku_is_injected(self) -> None:
        product = product_record(series="pro", model="FD160cm-Meru")
        self.assertEqual(candidate(product).payload["sku"], "CLM-PRO-FD160CM-MERU")

    def test_72_siq157_miko_sku_is_injected(self) -> None:
        product = product_record(model="SiQ157cm-Miko")
        self.assertEqual(candidate(product).payload["sku"], "CLM-ULTRA-SIQ157CM-MIKO")

    def test_73_siw160_imani_sku_is_injected(self) -> None:
        product = product_record(model="SiW160cm-Imani")
        self.assertEqual(candidate(product).payload["sku"], "CLM-ULTRA-SIW160CM-IMANI")

    def test_74_sir161_vica_sku_is_injected(self) -> None:
        product = product_record(model="SiR161-Vica")
        self.assertEqual(candidate(product).payload["sku"], "CLM-ULTRA-SIR161-VICA")

    def test_75_sit163_harriet_sku_is_injected(self) -> None:
        product = product_record(model="SiT163-Harriet")
        self.assertEqual(candidate(product).payload["sku"], "CLM-ULTRA-SIT163-HARRIET")

    def test_76_fd177_zara_sku_is_injected(self) -> None:
        product = product_record(series="pro", model="FD177-Zara")
        self.assertEqual(candidate(product).payload["sku"], "CLM-PRO-FD177-ZARA")

    def test_77_missing_sku_is_blocking(self) -> None:
        result = candidate(sku_result=None)
        self.assertNotIn("sku", result.payload)
        self.assertIn("missing_sku", result.blocking_issues)

    def test_78_invalid_sku_is_blocking_and_not_exposed(self) -> None:
        invalid = replace(generate_sku(product_record()), sku="BAD_SKU")
        result = candidate(sku_result=invalid)
        self.assertNotIn("sku", result.payload)
        self.assertIn("invalid_sku", result.blocking_issues)

    def test_79_too_long_sku_is_blocking_and_not_exposed(self) -> None:
        too_long = replace(
            generate_sku(product_record()),
            status="too_long",
            sku="CLM-ULTRA-" + "A" * 65,
            blocking_issues=("sku_too_long",),
        )
        result = candidate(sku_result=too_long)
        self.assertNotIn("sku", result.payload)
        self.assertIn("sku_too_long", result.blocking_issues)

    def test_80_sensitive_sku_tokens_are_rejected(self) -> None:
        baseline = generate_sku(product_record())
        for token in ("FOB", "RMB", "USD", "SUPPLIER", "COST", "PRICE", "SOURCE", "ROW", "TIMESTAMP", "UUID"):
            with self.subTest(token=token):
                invalid = replace(baseline, sku=f"CLM-ULTRA-{token}")
                result = candidate(sku_result=invalid)
                self.assertNotIn("sku", result.payload)
                self.assertIn("invalid_sku", result.blocking_issues)

    def test_81_fob_does_not_enter_payload_after_sku_integration(self) -> None:
        serialized = json.dumps(candidate().payload)
        self.assertNotIn("FOB", serialized.upper())
        self.assertNotIn("1500", serialized)

    def test_82_supplier_cost_does_not_enter_payload_after_sku_integration(self) -> None:
        serialized = json.dumps(candidate().payload).casefold()
        self.assertNotIn("supplier", serialized)
        self.assertNotIn("cost", serialized)

    def test_83_audit_retains_sku_policy_version(self) -> None:
        self.assertEqual(candidate().audit["sku"]["policy_version"], SKU_POLICY_VERSION)

    def test_84_audit_retains_raw_and_normalized_sku_identity(self) -> None:
        audit = candidate().audit["sku"]
        self.assertEqual(audit["raw_identity"], "PW-L31")
        self.assertEqual(audit["normalized_identity"], "PW-L31")

    def test_85_payload_does_not_contain_sku_audit_structure(self) -> None:
        payload = candidate().payload
        self.assertNotIn("policy_version", payload)
        self.assertNotIn("raw_identity", payload)
        self.assertNotIn("normalized_identity", payload)

    def test_86_sku_not_assigned_warning_is_removed(self) -> None:
        self.assertNotIn("sku_not_assigned", candidate().warnings)

    def test_87_sku_does_not_change_ready_for_write(self) -> None:
        self.assertIs(candidate().ready_for_write, False)

    def test_88_sku_result_is_not_mutated(self) -> None:
        product = product_record()
        sku_result = generate_sku(product)
        before = sku_result.to_dict()
        candidate(product, sku_result=sku_result)
        self.assertEqual(sku_result.to_dict(), before)

    def test_89_invalid_policy_version_is_blocking(self) -> None:
        invalid = replace(generate_sku(product_record()), policy_version="future")
        result = candidate(sku_result=invalid)
        self.assertNotIn("sku", result.payload)
        self.assertIn("invalid_sku_policy_version", result.blocking_issues)

    def test_90_collision_issue_is_preserved_without_repair(self) -> None:
        collision = replace(
            generate_sku(product_record()),
            status="collision",
            blocking_issues=("sku_collision",),
        )
        result = candidate(sku_result=collision)
        self.assertEqual(result.payload["sku"], "CLM-ULTRA-PW-L31")
        self.assertIn("sku_collision", result.blocking_issues)


if __name__ == "__main__":
    unittest.main()
