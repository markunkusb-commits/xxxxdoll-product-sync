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

from sync_worker.google_api import OfficialGoogleClientFactory  # noqa: E402
from sync_worker.http_client import ReadOnlyHttpClient  # noqa: E402
from sync_worker.image_mapping import (  # noqa: E402
    REDACTED_REFERENCE,
    ImageMappingError,
    ProductSourceRange,
    create_supplier_media_source_reference,
    map_product_media_sources,
    summarize_image_mapping,
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
)
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    StdlibWooCategoryTransport,
)


def product(
    start_row: int = 10,
    end_row: int = 20,
    *,
    series: str = "pro",
    model: str = "P-170",
) -> ProductRecord:
    return ProductRecord(
        identity=ProductIdentity(
            series=series,
            model=model,
            raw_series_title=f"CLM {series.title()}",
            raw_model=model,
        ),
        specifications=ProductSpecifications(normalized={}, raw=()),
        supplier_costs=SupplierCosts(
            fob_unit_price=MonetaryValue(
                raw_value="RMB2000",
                currency="RMB",
                amount=Decimal("2000"),
                context="fob_unit_price",
            ),
            body_only_fob=None,
            including_head_fob=None,
        ),
        retail_pricing=RetailPricing(minimum_retail_price=None),
        options=ProductOptions(normal_options_price=None, upgrade_options=()),
        media=ProductMedia(photo_download_link=REDACTED_REFERENCE),
        source=ProductSource(start_row=start_row, end_row=end_row),
        included_features=(),
        notices=(),
        unknown_fields=UnknownFields(raw_commercial_entries=()),
        warnings=(),
    )


def source(
    reference_row: int = 16,
    *,
    marker_row: int = 15,
    raw_reference: str | None = "https://media.example/private/photo.webp",
    marker_text: str = "Photo download link",
    reference_column: str = "I",
    marker_column: str = "B",
    source_row: int | None = None,
    product_source_candidate: ProductSourceRange | None = None,
    media_source_kind: str = "unknown",
):
    return create_supplier_media_source_reference(
        source_coordinate=f"{reference_column}{reference_row}",
        source_row=source_row,
        marker_coordinate=f"{marker_column}{marker_row}",
        marker_text=marker_text,
        raw_reference=raw_reference,
        product_source_candidate=product_source_candidate,
        media_source_kind=media_source_kind,
    )


def mapped(*, products=None, sources=None):
    return map_product_media_sources(
        products if products is not None else [product()],
        sources if sources is not None else [source()],
    )


class ImageMappingTests(unittest.TestCase):
    def test_01_basic_product_media_mapping(self) -> None:
        result = mapped()
        self.assertEqual(result.product_results[0].status, "mapped")
        self.assertEqual(len(result.product_results[0].media_sources), 1)

    def test_02_reference_inside_product_range(self) -> None:
        item = mapped().product_results[0].media_sources[0]
        self.assertEqual(item.reference_coordinate, "I16")

    def test_03_exact_source_range_match(self) -> None:
        item = mapped().media_source_results[0]
        self.assertEqual(item.match_status, "exact_source_range_match")
        self.assertEqual(item.match_method, "source_range")

    def test_04_reference_before_product_range_is_unmatched(self) -> None:
        result = mapped(sources=[source(5, marker_row=4)])
        self.assertEqual(
            result.media_source_results[0].match_status,
            "unmatched_media_source",
        )

    def test_05_reference_after_product_range_is_unmatched(self) -> None:
        result = mapped(sources=[source(25, marker_row=24)])
        self.assertEqual(
            result.media_source_results[0].match_status,
            "unmatched_media_source",
        )

    def test_06_overlapping_product_ranges_are_ambiguous(self) -> None:
        result = mapped(
            products=[product(10, 20, model="A"), product(15, 25, model="B")],
            sources=[source(17, marker_row=16)],
        )
        self.assertEqual(
            result.media_source_results[0].match_status,
            "ambiguous_media_source",
        )
        self.assertTrue(
            all(item.status == "ambiguous" for item in result.product_results)
        )

    def test_07_marker_exact_match(self) -> None:
        self.assertNotIn(
            "unsupported_media_marker",
            mapped().media_source_results[0].warnings,
        )

    def test_08_marker_whitespace_normalization(self) -> None:
        item = source(marker_text=" \u2003Photo\t download\n link ")
        self.assertEqual(mapped(sources=[item]).product_results[0].status, "mapped")

    def test_09_marker_case_normalization(self) -> None:
        item = source(marker_text="PHOTO DOWNLOAD LINK")
        self.assertEqual(mapped(sources=[item]).product_results[0].status, "mapped")

    def test_10_unsupported_marker_is_not_fuzzy_matched(self) -> None:
        for marker in ("Photo", "Picture", "Image"):
            with self.subTest(marker=marker):
                result = mapped(sources=[source(marker_text=marker)])
                self.assertEqual(
                    result.media_source_results[0].match_status,
                    "unsupported_media_marker",
                )

    def test_11_marker_coordinate_is_preserved(self) -> None:
        self.assertEqual(
            mapped(sources=[source(marker_row=14)]).media_source_results[0].marker_coordinate,
            "B14",
        )

    def test_12_reference_coordinate_is_preserved(self) -> None:
        self.assertEqual(mapped().media_source_results[0].reference_coordinate, "I16")

    def test_13_redacted_url_is_accepted_for_mapping(self) -> None:
        result = mapped(sources=[source(raw_reference=REDACTED_REFERENCE)])
        self.assertEqual(result.product_results[0].status, "mapped")

    def test_14_redacted_reference_status(self) -> None:
        item = mapped(sources=[source(raw_reference=REDACTED_REFERENCE)]).media_source_results[0]
        self.assertEqual(item.reference_status, "redacted")
        self.assertEqual(item.safe_reference, REDACTED_REFERENCE)

    def test_15_redacted_reference_is_not_download_ready(self) -> None:
        item = mapped(sources=[source(raw_reference=REDACTED_REFERENCE)]).media_source_results[0]
        self.assertIs(item.download_ready, False)

    def test_16_product_with_no_media_source(self) -> None:
        result = mapped(sources=[]).product_results[0]
        self.assertEqual(result.status, "no_media_source")
        self.assertIn("images_not_mapped", result.warnings)

    def test_17_multiple_media_sources_per_product(self) -> None:
        result = mapped(sources=[source(15, marker_row=14), source(18, marker_row=17)])
        self.assertEqual(len(result.product_results[0].media_sources), 2)

    def test_18_duplicate_same_reference_is_not_copied_twice(self) -> None:
        item = source()
        result = mapped(sources=[item, item])
        self.assertEqual(len(result.product_results[0].media_sources), 1)
        self.assertEqual(result.summary.duplicate_media_references, 1)

    def test_19_duplicate_provenance_is_marked(self) -> None:
        item = source()
        result = mapped(sources=[item, item])
        self.assertTrue(
            all(
                "duplicate_media_reference" in value.warnings
                for value in result.media_source_results
            )
        )

    def test_20_shared_reference_across_products_is_review_warning(self) -> None:
        shared = "https://media.example/shared/gallery"
        result = mapped(
            products=[product(1, 10, model="A"), product(20, 30, model="B")],
            sources=[
                source(4, marker_row=3, raw_reference=shared),
                source(24, marker_row=23, raw_reference=shared),
            ],
        )
        self.assertEqual(result.summary.shared_media_references, 1)
        self.assertTrue(
            all(
                "shared_media_reference" in value.warnings
                for value in result.media_source_results
            )
        )

    def test_21_safe_url_removes_query(self) -> None:
        item = source(raw_reference="https://media.example/path/photo.webp?token=secret")
        self.assertEqual(item.safe_reference, "https://media.example/photo.webp")
        self.assertNotIn("token", item.safe_reference)

    def test_22_safe_url_removes_fragment(self) -> None:
        item = source(raw_reference="https://media.example/path/photo.webp#private")
        self.assertEqual(item.safe_reference, "https://media.example/photo.webp")

    def test_23_safe_url_removes_credentials(self) -> None:
        item = source(raw_reference="https://user:pass@media.example/path/photo.webp")
        self.assertEqual(item.safe_reference, "https://media.example/photo.webp")
        self.assertNotIn("user", item.safe_reference)
        self.assertNotIn("pass", item.safe_reference)

    def test_24_raw_sensitive_url_is_not_in_report(self) -> None:
        raw = "https://user:pass@media.example/private/photo.webp?token=verysecret#auth"
        report = mapped(sources=[source(raw_reference=raw)]).to_report_dict()
        serialized = json.dumps(report)
        self.assertNotIn(raw, serialized)
        self.assertNotIn("verysecret", serialized)
        self.assertNotIn("user:pass", serialized)

    def test_25_fingerprint_is_deterministic(self) -> None:
        self.assertEqual(source().reference_fingerprint, source().reference_fingerprint)

    def test_26_same_available_reference_has_same_fingerprint(self) -> None:
        first = source(15, raw_reference="https://media.example/same")
        second = source(18, raw_reference="https://media.example/same")
        self.assertEqual(first.reference_fingerprint, second.reference_fingerprint)

    def test_27_url_filename_does_not_fuzzy_match_product(self) -> None:
        item = source(
            40,
            marker_row=39,
            raw_reference="https://media.example/P-170.webp",
        )
        result = mapped(sources=[item])
        self.assertEqual(result.media_source_results[0].match_status, "unmatched_media_source")

    def test_28_product_name_substring_does_not_match(self) -> None:
        item = source(
            40,
            marker_row=39,
            raw_reference="https://media.example/folder/P-170/gallery",
        )
        self.assertEqual(
            mapped(sources=[item]).summary.mapped_media_sources,
            0,
        )

    def test_29_series_only_does_not_match(self) -> None:
        result = mapped(
            products=[product(1, 5, series="pro", model="A"), product(10, 20, series="pro", model="B")],
            sources=[source(15, marker_row=14)],
        )
        mapped_product = next(item for item in result.product_results if item.media_sources)
        self.assertEqual(mapped_product.product_identity.model, "B")

    def test_30_array_order_is_not_used_for_matching(self) -> None:
        products = [product(20, 30, model="B"), product(1, 10, model="A")]
        result = mapped(products=products, sources=[source(5, marker_row=4)])
        self.assertEqual(result.product_results[0].product_identity.model, "A")
        self.assertEqual(result.product_results[0].status, "mapped")

    def test_31_product_source_start_and_end_are_preserved(self) -> None:
        result = mapped(products=[product(8, 19)], sources=[source(12, marker_row=11)])
        self.assertEqual(result.product_results[0].product_source.start_row, 8)
        self.assertEqual(result.product_results[0].product_source.end_row, 19)

    def test_32_product_identity_is_preserved(self) -> None:
        result = mapped(products=[product(series="ultra", model="UL-170")])
        identity = result.product_results[0].product_identity
        self.assertEqual(identity.series, "ultra")
        self.assertEqual(identity.model, "UL-170")

    def test_33_summary_total_products(self) -> None:
        result = mapped(products=[product(1, 10), product(20, 30)], sources=[])
        self.assertEqual(result.summary.total_products, 2)

    def test_34_summary_products_with_media(self) -> None:
        self.assertEqual(mapped().summary.products_with_media_source, 1)

    def test_35_summary_products_without_media(self) -> None:
        self.assertEqual(mapped(sources=[]).summary.products_without_media_source, 1)

    def test_36_summary_mapped_source_count(self) -> None:
        self.assertEqual(mapped().summary.mapped_media_sources, 1)

    def test_37_summary_unmatched_source_count(self) -> None:
        self.assertEqual(
            mapped(sources=[source(30, marker_row=29)]).summary.unmatched_media_sources,
            1,
        )

    def test_38_summary_ambiguous_source_count(self) -> None:
        result = mapped(
            products=[product(10, 20), product(15, 25)],
            sources=[source(17, marker_row=16)],
        )
        self.assertEqual(result.summary.ambiguous_media_sources, 1)

    def test_39_summary_redacted_source_count(self) -> None:
        result = mapped(sources=[source(raw_reference=REDACTED_REFERENCE)])
        self.assertEqual(result.summary.redacted_media_sources, 1)

    def test_40_summary_duplicate_count(self) -> None:
        item = source()
        self.assertEqual(mapped(sources=[item, item]).summary.duplicate_media_references, 1)

    def test_41_summary_shared_count(self) -> None:
        raw = "https://media.example/shared"
        result = mapped(
            products=[product(1, 10), product(20, 30)],
            sources=[source(4, marker_row=3, raw_reference=raw), source(24, marker_row=23, raw_reference=raw)],
        )
        self.assertEqual(result.summary.shared_media_references, 1)

    def test_42_product_record_is_not_mutated(self) -> None:
        item = product()
        before = item.to_dict()
        mapped(products=[item])
        self.assertEqual(item.to_dict(), before)

    def test_43_source_reference_is_not_mutated(self) -> None:
        item = source()
        before = copy.deepcopy(item)
        mapped(sources=[item])
        self.assertEqual(item, before)

    def test_44_output_order_is_deterministic(self) -> None:
        products = [product(20, 30, model="B"), product(1, 10, model="A")]
        sources = [source(24, marker_row=23), source(4, marker_row=3)]
        first = mapped(products=products, sources=sources).to_report_dict()
        second = mapped(products=list(reversed(products)), sources=list(reversed(sources))).to_report_dict()
        self.assertEqual(first, second)

    def test_45_no_http_request(self) -> None:
        with patch.object(
            ReadOnlyHttpClient,
            "get",
            side_effect=AssertionError("HTTP forbidden"),
        ):
            result = mapped()
        self.assertEqual(result.network_requests_performed, 0)

    def test_46_no_google_api(self) -> None:
        with patch.object(
            OfficialGoogleClientFactory,
            "create",
            side_effect=AssertionError("Google API forbidden"),
        ):
            result = mapped()
        self.assertEqual(result.network_requests_performed, 0)

    def test_47_no_wordpress_api(self) -> None:
        with patch.object(
            ReadOnlyHttpClient,
            "head",
            side_effect=AssertionError("WordPress API forbidden"),
        ):
            result = mapped()
        self.assertEqual(result.network_requests_performed, 0)

    def test_48_no_woocommerce_api(self) -> None:
        with patch.object(
            StdlibWooCategoryTransport,
            "get_categories",
            side_effect=AssertionError("Woo API forbidden"),
        ):
            result = mapped()
        self.assertEqual(result.network_requests_performed, 0)

    def test_49_network_requests_are_zero(self) -> None:
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            result = mapped()
        self.assertEqual(result.network_requests_performed, 0)

    def test_50_external_writes_are_zero(self) -> None:
        with patch.object(Path, "write_text", side_effect=AssertionError("write forbidden")):
            result = mapped()
        self.assertEqual(result.write_requests_performed, 0)

    def test_51_media_kind_defaults_to_unknown_even_for_jpg(self) -> None:
        item = source(raw_reference="https://media.example/photo.jpg")
        self.assertEqual(item.media_source_kind, "unknown")

    def test_52_explicit_media_kind_is_preserved(self) -> None:
        item = source(media_source_kind="folder")
        self.assertEqual(mapped(sources=[item]).media_source_results[0].media_source_kind, "folder")

    def test_53_non_merged_blank_reference_is_invalid_not_inherited(self) -> None:
        result = mapped(sources=[source(raw_reference=None)])
        self.assertEqual(result.media_source_results[0].reference_status, "missing")
        self.assertEqual(result.media_source_results[0].match_status, "invalid_reference")

    def test_54_marker_and_reference_must_share_one_product_range(self) -> None:
        result = mapped(
            products=[product(1, 10, model="A"), product(20, 30, model="B")],
            sources=[source(24, marker_row=4)],
        )
        self.assertEqual(result.media_source_results[0].match_status, "unmatched_media_source")

    def test_55_misleading_product_source_candidate_is_not_authority(self) -> None:
        item = source(product_source_candidate=ProductSourceRange(20, 30))
        result = mapped(products=[product(10, 20, model="A"), product(30, 40, model="B")], sources=[item])
        mapped_product = next(value for value in result.product_results if value.media_sources)
        self.assertEqual(mapped_product.product_identity.model, "A")

    def test_56_redacted_placeholders_at_different_coordinates_are_not_shared(self) -> None:
        result = mapped(
            products=[product(1, 10), product(20, 30)],
            sources=[
                source(4, marker_row=3, raw_reference=REDACTED_REFERENCE),
                source(24, marker_row=23, raw_reference=REDACTED_REFERENCE),
            ],
        )
        self.assertEqual(result.summary.shared_media_references, 0)

    def test_57_tampered_safe_reference_is_recomputed(self) -> None:
        raw = "https://media.example/photo.webp?token=secret"
        item = replace(source(raw_reference=raw), safe_reference=raw)
        serialized = json.dumps(mapped(sources=[item]).to_report_dict())
        self.assertNotIn("token=secret", serialized)

    def test_58_invalid_product_range_is_rejected(self) -> None:
        with self.assertRaises(ImageMappingError):
            mapped(products=[product(20, 10)])

    def test_59_summary_function_matches_batch_summary(self) -> None:
        result = mapped()
        self.assertEqual(
            summarize_image_mapping(result.product_results, result.media_source_results),
            result.summary,
        )

    def test_60_report_is_deterministic_and_has_no_timestamp(self) -> None:
        report = mapped().to_report_dict()
        self.assertNotIn("timestamp", report)
        self.assertEqual(report, mapped().to_report_dict())

    def test_61_raw_reference_is_excluded_from_repr(self) -> None:
        raw = "https://user:pass@media.example/photo.webp?token=secret"
        self.assertNotIn(raw, repr(source(raw_reference=raw)))

    def test_62_direct_source_report_recomputes_tampered_safe_reference(self) -> None:
        raw = "https://media.example/photo.webp?token=secret"
        item = replace(source(raw_reference=raw), safe_reference=raw)
        serialized = json.dumps(item.to_report_dict())
        self.assertNotIn("token=secret", serialized)


if __name__ == "__main__":
    unittest.main()
