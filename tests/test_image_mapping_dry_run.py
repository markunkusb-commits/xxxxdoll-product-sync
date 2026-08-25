from __future__ import annotations

import copy
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
from sync_worker.google_api import OfficialGoogleClientFactory  # noqa: E402
from sync_worker.http_client import ReadOnlyHttpClient  # noqa: E402
from sync_worker.image_mapping import (  # noqa: E402
    REDACTED_REFERENCE,
    is_photo_download_link_marker,
    map_product_media_sources,
)
from sync_worker.image_mapping_dry_run import (  # noqa: E402
    ImageMappingDryRunSafetyError,
    build_image_mapping_dry_run_report,
    extract_supplier_media_source_references,
    run_image_mapping_dry_run,
)
from sync_worker.product_model import ProductRecord, from_clm_product  # noqa: E402
from sync_worker.product_size_enrichment_dry_run import (  # noqa: E402
    restore_product_records,
)
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    StdlibWooCategoryTransport,
)


def product_item(
    model: str,
    *,
    series: str = "ultra",
    start_row: int = 10,
    end_row: int = 20,
) -> dict[str, object]:
    return {
        "series": series,
        "raw_series_title": f"CLM {series.title()}",
        "model": model,
        "specifications": {},
        "pricing": {},
        "included_features": [],
        "upgrade_options": [],
        "notices": [],
        "source": {"start_row": start_row, "end_row": end_row},
        "warnings": [],
    }


def product_report(*items: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "products": list(items)}


def cell(
    coordinate: str,
    value: str,
    *,
    merged_range: str | None = None,
) -> dict[str, object]:
    letters = "".join(character for character in coordinate if character.isalpha())
    digits = "".join(character for character in coordinate if character.isdigit())
    column_index = 0
    for character in letters.upper():
        column_index = column_index * 26 + ord(character) - ord("A") + 1
    return {
        "coordinate": coordinate.upper(),
        "row": int(digits),
        "column": letters.upper(),
        "column_index": column_index,
        "formatted_value": value,
        "is_merged": merged_range is not None,
        "is_merge_anchor": merged_range is not None,
        "merged_range": merged_range,
    }


def layout(*items: dict[str, object]) -> dict[str, object]:
    return {"status": "ok", "non_empty_cells": list(items)}


def restored_products(*items: dict[str, object]) -> list[ProductRecord]:
    return restore_product_records(product_report(*items))


def basic_products() -> list[ProductRecord]:
    return restored_products(product_item("U-170"))


def basic_layout(
    reference: str = REDACTED_REFERENCE,
    *,
    marker: str = "Photo download link",
) -> dict[str, object]:
    return layout(cell("B15", marker), cell("I16", reference))


def built_report(
    *,
    products: list[ProductRecord] | None = None,
    sheet_layout: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_image_mapping_dry_run_report(
        products or basic_products(),
        sheet_layout or basic_layout(),
        product_input_file="mock-products.json",
        layout_input_file="mock-layout.json",
    )


class ImageMappingDryRunTests(unittest.TestCase):
    def test_01_cli_registers_map_product_images(self) -> None:
        arguments = build_parser().parse_args(
            [
                "map-product-images",
                "--products",
                "products.json",
                "--layout",
                "layout.json",
            ]
        )
        self.assertEqual(arguments.command, "map-product-images")
        self.assertEqual(arguments.product_input_path, Path("products.json"))
        self.assertEqual(arguments.layout_input_path, Path("layout.json"))

    def test_02_cli_requires_products(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["map-product-images", "--layout", "layout.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_03_cli_requires_layout(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["map-product-images", "--products", "products.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_04_product_json_restores_product_records(self) -> None:
        products = restored_products(product_item("U-170"))
        self.assertIsInstance(products[0], ProductRecord)
        self.assertEqual(products[0].identity.model, "U-170")

    def test_05_product_restore_reuses_from_clm_product(self) -> None:
        report = product_report(product_item("U-170"), product_item("U-175"))
        with patch(
            "sync_worker.product_size_enrichment_dry_run.from_clm_product",
            wraps=from_clm_product,
        ) as converter:
            products = restore_product_records(report)
        self.assertEqual(len(products), 2)
        self.assertEqual(converter.call_count, 2)

    def test_06_layout_non_empty_cells_are_read(self) -> None:
        extracted = extract_supplier_media_source_references(
            basic_layout(), basic_products()
        )
        self.assertEqual(len(extracted.references), 1)

    def test_07_exact_marker_is_recognized(self) -> None:
        self.assertTrue(is_photo_download_link_marker("Photo download link"))

    def test_08_marker_case_is_normalized(self) -> None:
        extracted = extract_supplier_media_source_references(
            basic_layout(marker="PHOTO DOWNLOAD LINK"), basic_products()
        )
        self.assertEqual(len(extracted.references), 1)

    def test_09_marker_unicode_whitespace_is_normalized(self) -> None:
        extracted = extract_supplier_media_source_references(
            basic_layout(marker="Photo\u2003download\tlink"), basic_products()
        )
        self.assertEqual(len(extracted.references), 1)

    def test_10_photo_alone_is_not_a_marker(self) -> None:
        self.assertFalse(is_photo_download_link_marker("Photo"))

    def test_11_image_alone_is_not_a_marker(self) -> None:
        self.assertFalse(is_photo_download_link_marker("Image"))

    def test_12_download_alone_is_not_a_marker(self) -> None:
        self.assertFalse(is_photo_download_link_marker("Download"))

    def test_13_picture_alone_is_not_a_marker(self) -> None:
        self.assertFalse(is_photo_download_link_marker("Picture"))

    def test_14_marker_and_reference_pair_on_next_row(self) -> None:
        extracted = extract_supplier_media_source_references(
            basic_layout(), basic_products()
        )
        self.assertEqual(extracted.references[0].source_coordinate, "I16")

    def test_15_marker_and_reference_pair_on_same_row(self) -> None:
        sheet_layout = layout(
            cell("B15", "Photo download link"),
            cell("I15", REDACTED_REFERENCE),
        )
        extracted = extract_supplier_media_source_references(
            sheet_layout, basic_products()
        )
        self.assertEqual(extracted.references[0].source_coordinate, "I15")

    def test_16_marker_coordinate_is_preserved(self) -> None:
        extracted = extract_supplier_media_source_references(
            basic_layout(), basic_products()
        )
        self.assertEqual(extracted.references[0].marker_coordinate, "B15")

    def test_17_marker_and_reference_rows_are_preserved(self) -> None:
        extracted = extract_supplier_media_source_references(
            basic_layout(), basic_products()
        )
        self.assertEqual(extracted.references[0].source_row, 16)
        self.assertEqual(extracted.references[0].product_source_candidate.start_row, 10)

    def test_17b_report_preserves_marker_and_reference_rows(self) -> None:
        media = built_report()["results"][0]["media_sources"][0]
        self.assertEqual(media["marker_row"], 15)
        self.assertEqual(media["reference_row"], 16)

    def test_18_cross_product_pair_is_rejected(self) -> None:
        products = restored_products(
            product_item("A", start_row=10, end_row=15),
            product_item("B", start_row=16, end_row=25),
        )
        extracted = extract_supplier_media_source_references(
            basic_layout(), products
        )
        self.assertEqual(extracted.references, ())
        self.assertIn(
            "cross_product_media_reference_pair", extracted.issues[0].warnings
        )

    def test_19_missing_reference_is_recorded(self) -> None:
        extracted = extract_supplier_media_source_references(
            layout(cell("B15", "Photo download link")), basic_products()
        )
        self.assertEqual(extracted.missing_media_references, 1)

    def test_20_ambiguous_reference_pair_is_recorded(self) -> None:
        sheet_layout = layout(
            cell("B15", "Photo download link"),
            cell("I16", REDACTED_REFERENCE),
            cell("J16", "https://media.example/other"),
        )
        extracted = extract_supplier_media_source_references(
            sheet_layout, basic_products()
        )
        self.assertEqual(extracted.ambiguous_media_reference_pairs, 1)
        self.assertEqual(extracted.references, ())

    def test_21_redacted_reference_is_extracted(self) -> None:
        reference = extract_supplier_media_source_references(
            basic_layout(), basic_products()
        ).references[0]
        self.assertEqual(reference.reference_status, "redacted")

    def test_22_redacted_reference_maps_successfully(self) -> None:
        report = built_report()
        self.assertEqual(report["summary"]["mapped_media_sources"], 1)
        self.assertEqual(report["results"][0]["status"], "mapped")

    def test_23_redacted_reference_is_never_download_ready(self) -> None:
        media = built_report()["results"][0]["media_sources"][0]
        self.assertIs(media["download_ready"], False)

    def test_24_existing_image_mapping_core_is_called(self) -> None:
        with patch(
            "sync_worker.image_mapping_dry_run.map_product_media_sources",
            wraps=map_product_media_sources,
        ) as mapper:
            built_report()
        mapper.assert_called_once()

    def test_25_no_media_product_is_not_a_parser_error(self) -> None:
        report = built_report(sheet_layout=layout())
        result = report["results"][0]
        self.assertEqual(result["status"], "no_media_source")
        self.assertIn("images_not_mapped", result["warnings"])

    def test_26_multiple_media_sources_map_to_one_product(self) -> None:
        sheet_layout = layout(
            cell("B12", "Photo download link"),
            cell("I13", REDACTED_REFERENCE),
            cell("B17", "Photo download link"),
            cell("I18", "https://media.example/gallery"),
        )
        report = built_report(sheet_layout=sheet_layout)
        self.assertEqual(len(report["results"][0]["media_sources"]), 2)

    def test_27_duplicate_summary_is_present(self) -> None:
        summary = built_report()["summary"]
        self.assertIn("duplicate_media_references", summary)
        self.assertEqual(summary["duplicate_media_references"], 0)

    def test_28_shared_reference_summary_comes_from_core(self) -> None:
        products = restored_products(
            product_item("A", start_row=1, end_row=10),
            product_item("B", start_row=20, end_row=30),
        )
        shared = "https://media.example/shared/gallery"
        sheet_layout = layout(
            cell("B3", "Photo download link"),
            cell("I4", shared),
            cell("B23", "Photo download link"),
            cell("I24", shared),
        )
        self.assertEqual(
            built_report(products=products, sheet_layout=sheet_layout)["summary"][
                "shared_media_references"
            ],
            1,
        )

    def test_29_unmatched_summary_comes_from_core(self) -> None:
        sheet_layout = layout(
            cell("B40", "Photo download link"),
            cell("I41", REDACTED_REFERENCE),
        )
        self.assertEqual(
            built_report(sheet_layout=sheet_layout)["summary"][
                "unmatched_media_sources"
            ],
            1,
        )

    def test_30_ambiguous_source_summary_comes_from_core(self) -> None:
        products = restored_products(
            product_item("A", start_row=10, end_row=20),
            product_item("B", start_row=12, end_row=22),
        )
        self.assertEqual(
            built_report(products=products)["summary"]["ambiguous_media_sources"],
            1,
        )

    def test_31_redacted_summary_is_counted(self) -> None:
        self.assertEqual(built_report()["summary"]["redacted_media_sources"], 1)

    def test_32_raw_url_is_not_in_report(self) -> None:
        raw = "https://media.example/private/folder?access_token=secret#private"
        serialized = json.dumps(built_report(sheet_layout=basic_layout(raw)))
        self.assertNotIn(raw, serialized)
        self.assertNotIn("secret", serialized)

    def test_33_query_is_stripped(self) -> None:
        report = built_report(
            sheet_layout=basic_layout(
                "https://media.example/private/photo.webp?signature=hidden"
            )
        )
        safe = report["results"][0]["media_sources"][0]["safe_reference"]
        self.assertTrue(safe.endswith("/photo.webp"))
        self.assertNotIn("signature", safe)
        self.assertNotIn("hidden", safe)

    def test_34_fragment_is_stripped(self) -> None:
        report = built_report(
            sheet_layout=basic_layout(
                "https://media.example/private/photo.webp#hidden"
            )
        )
        safe = report["results"][0]["media_sources"][0]["safe_reference"]
        self.assertNotIn("#", safe)

    def test_35_url_credentials_are_stripped(self) -> None:
        report = built_report(
            sheet_layout=basic_layout(
                "https://user:password@media.example/private/photo.webp"
            )
        )
        serialized = json.dumps(report)
        self.assertNotIn("user:password", serialized)

    def test_36_fingerprint_is_deterministic(self) -> None:
        first = built_report()["results"][0]["media_sources"][0]
        second = built_report()["results"][0]["media_sources"][0]
        self.assertEqual(
            first["reference_fingerprint"], second["reference_fingerprint"]
        )

    def test_37_output_order_is_stable(self) -> None:
        products = restored_products(
            product_item("B", start_row=20, end_row=30),
            product_item("A", start_row=1, end_row=10),
        )
        report = built_report(products=products, sheet_layout=layout())
        self.assertEqual(
            [item["product_source"]["start_row"] for item in report["results"]],
            [1, 20],
        )

    def test_38_product_name_does_not_create_a_pair(self) -> None:
        sheet_layout = layout(cell("I16", "U-170-photo.webp"))
        self.assertEqual(
            extract_supplier_media_source_references(
                sheet_layout, basic_products()
            ).references,
            (),
        )

    def test_39_series_name_does_not_create_a_pair(self) -> None:
        sheet_layout = layout(
            cell("B15", "CLM Ultra image"),
            cell("I16", REDACTED_REFERENCE),
        )
        self.assertEqual(
            extract_supplier_media_source_references(
                sheet_layout, basic_products()
            ).references,
            (),
        )

    def test_40_array_order_does_not_join_unrelated_cell(self) -> None:
        sheet_layout = layout(
            cell("I14", REDACTED_REFERENCE),
            cell("B15", "Photo download link"),
        )
        extracted = extract_supplier_media_source_references(
            sheet_layout, basic_products()
        )
        self.assertEqual(extracted.references, ())

    def test_41_product_input_is_not_mutated(self) -> None:
        products = basic_products()
        before = copy.deepcopy(products)
        built_report(products=products)
        self.assertEqual(products, before)

    def test_42_layout_input_is_not_mutated(self) -> None:
        sheet_layout = basic_layout()
        before = copy.deepcopy(sheet_layout)
        built_report(sheet_layout=sheet_layout)
        self.assertEqual(sheet_layout, before)

    def test_43_network_request_counter_is_zero(self) -> None:
        self.assertEqual(built_report()["network_requests_performed"], 0)

    def test_44_external_write_request_counter_is_zero(self) -> None:
        self.assertEqual(built_report()["write_requests_performed"], 0)

    def test_45_no_socket_network_is_used(self) -> None:
        with patch.object(socket.socket, "connect") as connect:
            built_report()
        connect.assert_not_called()

    def test_46_no_google_api_client_is_created(self) -> None:
        with patch.object(OfficialGoogleClientFactory, "create") as create:
            built_report()
        create.assert_not_called()

    def test_47_no_wordpress_http_client_is_used(self) -> None:
        with patch.object(ReadOnlyHttpClient, "request") as request:
            built_report()
        request.assert_not_called()

    def test_48_no_woo_api_transport_is_used(self) -> None:
        with patch.object(StdlibWooCategoryTransport, "get_categories") as get:
            built_report()
        get.assert_not_called()

    def test_49_run_writes_only_the_local_dry_run_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products_path = root / "products.json"
            layout_path = root / "layout.json"
            products_path.write_text(
                json.dumps(product_report(product_item("U-170"))), encoding="utf-8"
            )
            layout_path.write_text(json.dumps(basic_layout()), encoding="utf-8")
            report, output = run_image_mapping_dry_run(
                products_path, layout_path, project_root=root
            )
            self.assertEqual(output, root / "reports" / "image-mapping-dry-run.json")
            self.assertTrue(output.is_file())
            self.assertEqual(report["status"], "ok")

    def test_50_unsafe_report_leak_blocks_before_write(self) -> None:
        with self.assertRaisesRegex(
            ImageMappingDryRunSafetyError, "unsafe_media_reference_leak"
        ):
            build_image_mapping_dry_run_report(
                basic_products(),
                basic_layout(),
                product_input_file="products.json?token=unsafe",
                layout_input_file="layout.json",
            )

    def test_51_missing_pair_is_attached_as_product_blocker(self) -> None:
        report = built_report(
            sheet_layout=layout(cell("B15", "Photo download link"))
        )
        self.assertEqual(report["results"][0]["status"], "invalid")
        self.assertIn(
            "missing_media_reference", report["results"][0]["blocking_issues"]
        )

    def test_52_ambiguous_pair_is_attached_as_product_blocker(self) -> None:
        sheet_layout = layout(
            cell("B15", "Photo download link"),
            cell("I16", REDACTED_REFERENCE),
            cell("J16", "https://media.example/second"),
        )
        report = built_report(sheet_layout=sheet_layout)
        self.assertEqual(report["results"][0]["status"], "ambiguous")

    def test_53_vertical_merged_marker_band_pairs_to_following_row(self) -> None:
        sheet_layout = layout(
            cell("B14", "Photo download link", merged_range="B14:H15"),
            cell("I16", REDACTED_REFERENCE),
        )
        extracted = extract_supplier_media_source_references(
            sheet_layout, basic_products()
        )
        self.assertEqual(extracted.references[0].source_coordinate, "I16")

    def test_54_cell_left_of_marker_band_is_not_paired(self) -> None:
        sheet_layout = layout(
            cell("I15", "Photo download link"), cell("B16", REDACTED_REFERENCE)
        )
        extracted = extract_supplier_media_source_references(
            sheet_layout, basic_products()
        )
        self.assertEqual(extracted.references, ())

    def test_55_cli_main_uses_only_mock_local_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products_path = root / "products.json"
            layout_path = root / "layout.json"
            products_path.write_text(
                json.dumps(product_report(product_item("U-170"))), encoding="utf-8"
            )
            layout_path.write_text(json.dumps(basic_layout()), encoding="utf-8")
            with patch("sync_worker.cli.PROJECT_ROOT", root), patch.object(
                socket.socket, "connect"
            ) as connect:
                exit_code = main(
                    [
                        "map-product-images",
                        "--products",
                        str(products_path),
                        "--layout",
                        str(layout_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
