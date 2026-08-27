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
    ProductSourceRange,
    create_supplier_media_source_reference,
)
from sync_worker.media_source_discovery import discover_media_source  # noqa: E402
from sync_worker.media_source_discovery_dry_run import (  # noqa: E402
    MediaSourceDiscoveryDryRunInputError,
    build_media_source_discovery_report,
    join_verified_sku,
    restore_verified_sku_entries,
    run_media_source_discovery_dry_run,
    validate_sku_snapshot_compatibility,
)
from sync_worker.sanitization import Redactor  # noqa: E402
from sync_worker.secure_media_reference_read import (  # noqa: E402
    SecureMediaReferenceReadBatch,
    SecureMediaReferenceReadResult,
    ValidatedMappedMediaSource,
)
from sync_worker.sku_policy import SKU_POLICY_VERSION  # noqa: E402
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    StdlibWooCategoryTransport,
)
from tests.test_secure_media_reference_read import (  # noqa: E402
    FakeFactory,
    FakeSettings,
    SHEET,
    mapping_report,
    media_item,
    metadata_response,
    product_result,
    value_range,
)


def fresh_fingerprint(raw: str, coordinate: str = "I16") -> str:
    return create_supplier_media_source_reference(
        source_coordinate=coordinate,
        marker_coordinate="B15",
        marker_text="Photo download link",
        raw_reference=raw,
    ).reference_fingerprint


def read_batch(
    raw: str = "https://drive.google.com/drive/folders/FOLDER_ID_PRIVATE",
    *,
    mapping_status: str = "available",
    mapping_raw: str | None = None,
    coordinate: str = "I16",
    start_row: int = 10,
    end_row: int = 20,
    model: str | None = "VICA",
) -> SecureMediaReferenceReadBatch:
    expected_raw = raw if mapping_raw is None else mapping_raw
    mapping_fingerprint = (
        fresh_fingerprint(REDACTED_REFERENCE, coordinate)
        if mapping_status == "redacted"
        else fresh_fingerprint(expected_raw, coordinate)
    )
    mapped = ValidatedMappedMediaSource(
        product_source=ProductSourceRange(start_row, end_row),
        product_series="ultra",
        product_identity_values=() if model is None else (model,),
        marker_coordinate=f"B{max(start_row + 5, 1)}",
        reference_coordinate=coordinate,
        mapping_reference_status=mapping_status,
        mapping_reference_fingerprint=mapping_fingerprint,
    )
    fresh = fresh_fingerprint(raw, coordinate)
    if mapping_status == "redacted":
        verification = "mapping_reference_redacted"
        warnings = ("mapping_reference_redacted",)
        blockers = ()
    elif fresh == mapping_fingerprint:
        verification = "verified_unchanged"
        warnings = ()
        blockers = ()
    else:
        verification = "reference_changed_since_mapping"
        warnings = ("reference_changed_since_mapping",)
        blockers = ("reference_changed_since_mapping",)
    result = SecureMediaReferenceReadResult(
        mapped_source=mapped,
        read_status="read",
        reference_verification=verification,  # type: ignore[arg-type]
        fresh_reference_fingerprint=fresh,
        raw_reference=raw,
        warnings=warnings,
        blocking_issues=blockers,
    )
    return SecureMediaReferenceReadBatch(
        results=(result,),
        coordinates_requested=1,
        read_requests_performed=1,
        write_requests_performed=0,
    )


def missing_batch(status: str) -> SecureMediaReferenceReadBatch:
    mapped = ValidatedMappedMediaSource(
        product_source=ProductSourceRange(10, 20),
        product_series="ultra",
        product_identity_values=("VICA",),
        marker_coordinate="B15",
        reference_coordinate="I16",
        mapping_reference_status="redacted",
        mapping_reference_fingerprint=fresh_fingerprint(REDACTED_REFERENCE),
    )
    result = SecureMediaReferenceReadResult(
        mapped_source=mapped,
        read_status=status,  # type: ignore[arg-type]
        reference_verification="not_read",
        fresh_reference_fingerprint=None,
        raw_reference=None,
        warnings=(status,),
        blocking_issues=(status,),
    )
    return SecureMediaReferenceReadBatch(
        results=(result,),
        coordinates_requested=1,
        read_requests_performed=1,
        write_requests_performed=0,
    )


def sku_item(
    sku: str,
    *,
    start_row: int = 10,
    end_row: int = 20,
    series: str = "ultra",
    identity: str = "VICA",
) -> dict[str, object]:
    return {
        "series": series,
        "product_identity": identity,
        "raw_identity": identity,
        "normalized_identity": identity,
        "sku": sku,
        "status": "ok",
        "policy_version": SKU_POLICY_VERSION,
        "product_source": {"start_row": start_row, "end_row": end_row},
        "warnings": [],
        "blocking_issues": [],
        "conflicting_product_identities": [],
        "audit": {
            "policy_version": SKU_POLICY_VERSION,
            "identity_source": "model",
            "series_namespace": series.upper(),
        },
    }


def sku_report(*items: dict[str, object]) -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": SKU_POLICY_VERSION,
        "results": list(items),
    }


def built_report(
    batch: SecureMediaReferenceReadBatch | None = None,
    *,
    skus: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_media_source_discovery_report(
        batch or read_batch(),
        mapping_input_file="mock-mapping.json",
        sheet_title=SHEET,
        sku_report_input_file="mock-sku.json" if skus is not None else None,
        sku_report=skus,
    )


class MediaSourceDiscoveryDryRunTests(unittest.TestCase):
    def test_01_cli_registers_discover_mapped_media_sources(self) -> None:
        arguments = build_parser().parse_args(
            [
                "discover-mapped-media-sources",
                "--mapping",
                "mapping.json",
                "--sheet",
                SHEET,
            ]
        )
        self.assertEqual(arguments.command, "discover-mapped-media-sources")
        self.assertEqual(arguments.mapping_input_path, Path("mapping.json"))
        self.assertEqual(arguments.sheet_title, SHEET)

    def test_02_cli_requires_mapping(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["discover-mapped-media-sources", "--sheet", SHEET]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_03_cli_requires_sheet(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(
                ["discover-mapped-media-sources", "--mapping", "mapping.json"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_04_sku_report_is_optional(self) -> None:
        arguments = build_parser().parse_args(
            [
                "discover-mapped-media-sources",
                "--mapping",
                "mapping.json",
                "--sheet",
                SHEET,
            ]
        )
        self.assertIsNone(arguments.sku_report_input_path)

    def test_05_sku_report_argument_is_accepted(self) -> None:
        arguments = build_parser().parse_args(
            [
                "discover-mapped-media-sources",
                "--mapping",
                "mapping.json",
                "--sheet",
                SHEET,
                "--sku-report",
                "sku.json",
            ]
        )
        self.assertEqual(arguments.sku_report_input_path, Path("sku.json"))

    def test_06_calls_existing_media_source_discovery(self) -> None:
        with patch(
            "sync_worker.media_source_discovery_dry_run.discover_media_source",
            wraps=discover_media_source,
        ) as discovery:
            built_report()
        discovery.assert_called_once()

    def test_07_google_drive_classification_integration(self) -> None:
        report = built_report()
        self.assertEqual(report["results"][0]["provider"], "google_drive")
        self.assertEqual(report["results"][0]["resource_kind"], "folder")

    def test_08_dropbox_classification_integration(self) -> None:
        report = built_report(
            read_batch("https://dropbox.com/scl/fi/SHARE_ID/a.webp")
        )
        self.assertEqual(report["results"][0]["provider"], "dropbox")

    def test_09_onedrive_classification_integration(self) -> None:
        report = built_report(read_batch("https://1drv.ms/u/s!opaque"))
        self.assertEqual(report["results"][0]["provider"], "onedrive")

    def test_10_direct_web_classification_integration(self) -> None:
        report = built_report(read_batch("https://example.test/a.jpg"))
        self.assertEqual(report["results"][0]["provider"], "direct_web")

    def test_11_download_ready_is_always_false(self) -> None:
        self.assertIs(built_report()["results"][0]["download_ready"], False)

    def test_12_fresh_fingerprint_is_reported(self) -> None:
        raw = "https://example.test/a.jpg"
        report = built_report(read_batch(raw))
        self.assertEqual(
            report["results"][0]["reference_fingerprint"],
            fresh_fingerprint(raw),
        )

    def test_13_verified_unchanged_summary(self) -> None:
        self.assertEqual(built_report()["summary"]["verified_unchanged"], 1)

    def test_14_changed_reference_is_blocked(self) -> None:
        batch = read_batch(
            "https://example.test/new.jpg",
            mapping_raw="https://example.test/old.jpg",
        )
        report = built_report(batch)
        self.assertEqual(
            report["summary"]["reference_changed_since_mapping"], 1
        )
        self.assertIn(
            "reference_changed_since_mapping",
            report["results"][0]["blocking_issues"],
        )

    def test_15_redacted_mapping_classifies_real_cell_in_memory(self) -> None:
        report = built_report(read_batch(mapping_status="redacted"))
        self.assertEqual(report["results"][0]["provider"], "google_drive")
        self.assertEqual(
            report["results"][0]["reference_verification"],
            "mapping_reference_redacted",
        )

    def test_16_correct_sku_join(self) -> None:
        skus = sku_report(sku_item("CLM-ULTRA-VICA"))
        report = built_report(skus=skus)
        self.assertEqual(report["results"][0]["sku"], "CLM-ULTRA-VICA")

    def test_17_sku_join_uses_source_range_and_identity(self) -> None:
        entries = restore_verified_sku_entries(
            sku_report(
                sku_item("CLM-ULTRA-WRONG", start_row=30, end_row=40),
                sku_item("CLM-ULTRA-VICA"),
            )
        )
        mapped = read_batch().results[0].mapped_source
        result, warnings = join_verified_sku(mapped, entries)
        self.assertEqual(result.sku, "CLM-ULTRA-VICA")
        self.assertFalse(warnings)

    def test_18_series_only_sku_join_is_forbidden(self) -> None:
        skus = sku_report(
            sku_item("CLM-ULTRA-OTHER", start_row=30, end_row=40)
        )
        report = built_report(skus=skus)
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn("sku_join_not_found", report["results"][0]["warnings"])

    def test_19_sku_join_does_not_use_array_order(self) -> None:
        skus = sku_report(
            sku_item("CLM-ULTRA-OTHER", start_row=30, end_row=40),
            sku_item("CLM-ULTRA-VICA"),
        )
        forward = built_report(skus=skus)
        reverse = built_report(skus=sku_report(*reversed(skus["results"])))
        self.assertEqual(forward["results"][0]["sku"], "CLM-ULTRA-VICA")
        self.assertEqual(forward, reverse)

    def test_20_ambiguous_sku_join_does_not_guess(self) -> None:
        skus = sku_report(
            sku_item("CLM-ULTRA-VICA-A"),
            sku_item("CLM-ULTRA-VICA-B"),
        )
        report = built_report(skus=skus)
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn("sku_join_ambiguous", report["results"][0]["warnings"])

    def test_21_sku_absent_is_allowed(self) -> None:
        report = built_report()
        self.assertIsNone(report["results"][0]["sku"])
        self.assertNotIn("sku_join_not_found", report["results"][0]["warnings"])

    def test_22_safe_report_contains_no_query(self) -> None:
        raw = "https://example.test/a.jpg?download=1"
        self.assertNotIn("?", json.dumps(built_report(read_batch(raw))))

    def test_23_safe_report_contains_no_fragment(self) -> None:
        raw = "https://example.test/a.jpg#private-fragment"
        self.assertNotIn("private-fragment", json.dumps(built_report(read_batch(raw))))

    def test_24_safe_report_contains_no_raw_resource_id(self) -> None:
        resource_id = "GOOGLE_DRIVE_RESOURCE_ID_PRIVATE"
        raw = f"https://drive.google.com/drive/folders/{resource_id}"
        self.assertNotIn(resource_id, json.dumps(built_report(read_batch(raw))))

    def test_25_safe_report_contains_no_credentials(self) -> None:
        raw = "https://supplier-user:private-pass@example.test/a.jpg"
        serialized = json.dumps(built_report(read_batch(raw)))
        self.assertNotIn("supplier-user", serialized)
        self.assertNotIn("private-pass", serialized)

    def test_26_safe_report_contains_no_token(self) -> None:
        raw = "https://example.test/a.jpg?token=private-token"
        serialized = json.dumps(built_report(read_batch(raw)))
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("token=", serialized)

    def test_27_sensitive_scan_blocks_unsafe_input_projection(self) -> None:
        with self.assertRaisesRegex(
            MediaSourceDiscoveryDryRunInputError,
            "unsafe_media_reference_leak",
        ):
            build_media_source_discovery_report(
                read_batch(),
                mapping_input_file="mapping.json?token=unsafe",
                sheet_title=SHEET,
                sku_report_input_file=None,
            )

    def test_28_deterministic_ordering(self) -> None:
        first = read_batch(
            "https://example.test/b.jpg",
            coordinate="I30",
            start_row=21,
            end_row=40,
            model="B",
        ).results[0]
        second = read_batch(
            "https://example.test/a.jpg",
            coordinate="I10",
            start_row=1,
            end_row=20,
            model="A",
        ).results[0]
        batch_a = SecureMediaReferenceReadBatch((first, second), 2, 1, 0)
        batch_b = SecureMediaReferenceReadBatch((second, first), 2, 1, 0)
        self.assertEqual(built_report(batch_a), built_report(batch_b))

    def test_29_input_batch_is_not_mutated(self) -> None:
        batch = read_batch()
        before = copy.deepcopy(batch)
        built_report(batch)
        self.assertEqual(batch, before)

    def test_30_summary_fields(self) -> None:
        summary = built_report()["summary"]
        for field in (
            "total_mapped_sources",
            "coordinates_requested",
            "references_read",
            "classified_sources",
            "blocked_sources",
            "cell_missing",
            "cell_empty",
        ):
            self.assertIn(field, summary)

    def test_31_provider_and_kind_summaries(self) -> None:
        summary = built_report()["summary"]
        self.assertEqual(summary["google_drive_sources"], 1)
        self.assertEqual(summary["folder_candidates"], 1)

    def test_32_missing_and_empty_summaries(self) -> None:
        missing = built_report(missing_batch("media_reference_response_missing"))
        empty = built_report(missing_batch("empty_media_reference"))
        self.assertEqual(missing["summary"]["cell_missing"], 1)
        self.assertEqual(empty["summary"]["cell_empty"], 1)

    def test_33_read_request_count_is_preserved(self) -> None:
        self.assertEqual(built_report()["read_requests_performed"], 1)

    def test_34_network_requests_are_zero(self) -> None:
        with patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("network forbidden"),
        ):
            report = built_report()
        self.assertEqual(report["network_requests_performed"], 0)

    def test_35_write_requests_are_zero(self) -> None:
        self.assertEqual(built_report()["write_requests_performed"], 0)

    def test_36_no_google_drive_api(self) -> None:
        with patch.object(
            OfficialGoogleClientFactory,
            "create",
            side_effect=AssertionError("Drive API forbidden"),
        ):
            report = built_report()
        self.assertEqual(report["results"][0]["provider"], "google_drive")

    def test_37_no_dropbox_api(self) -> None:
        with patch("builtins.__import__", wraps=__import__) as importer:
            built_report(read_batch("https://dropbox.com/scl/fo/ID/a"))
        names = [call.args[0] for call in importer.call_args_list]
        self.assertFalse(any(name.startswith("dropbox") for name in names))

    def test_38_no_http_media_probe(self) -> None:
        with patch.object(
            ReadOnlyHttpClient,
            "get",
            side_effect=AssertionError("media probe forbidden"),
        ):
            report = built_report(read_batch("https://example.test/a.jpg"))
        self.assertTrue(report["results"][0]["requires_http_probe"])

    def test_39_no_wordpress_api(self) -> None:
        with patch.object(
            ReadOnlyHttpClient,
            "head",
            side_effect=AssertionError("WordPress forbidden"),
        ):
            report = built_report()
        self.assertEqual(report["status"], "ok")

    def test_40_no_woo_api(self) -> None:
        with patch.object(
            StdlibWooCategoryTransport,
            "get_categories",
            side_effect=AssertionError("Woo forbidden"),
        ):
            report = built_report()
        self.assertEqual(report["status"], "ok")

    def test_41_run_uses_only_mock_google_and_local_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping_path = root / "mapping.json"
            mapping_payload = mapping_report(
                product_result(media_item(status="redacted"))
            )
            mapping_path.write_text(json.dumps(mapping_payload), encoding="utf-8")
            settings = FakeSettings()
            factory = FakeFactory(
                metadata_response(
                    value_range("I16", "https://example.test/a.jpg")
                )
            )
            report, output = run_media_source_discovery_dry_run(
                mapping_path,
                SHEET,
                settings,
                factory,
                project_root=root,
            )
            self.assertEqual(
                output,
                root / "reports" / "media-source-discovery-dry-run.json",
            )
            self.assertEqual(report["status"], "ok")
            self.assertEqual(factory.calls, 1)

    def test_42_cli_main_is_mocked_and_logs_no_raw_reference(self) -> None:
        mock_report = {
            "status": "ok",
            "summary": {"total_mapped_sources": 1},
            "read_requests_performed": 1,
            "network_requests_performed": 0,
            "write_requests_performed": 0,
        }
        with patch(
            "sync_worker.cli.load_google_sheets_readonly_config",
            return_value=object(),
        ) as sheets_loader, patch(
            "sync_worker.cli.load_google_config",
            side_effect=AssertionError("full config loader must not be used"),
        ) as full_loader, patch(
            "sync_worker.cli.google_redactor_for_settings",
            return_value=Redactor(),
        ), patch(
            "sync_worker.cli.run_media_source_discovery_dry_run",
            return_value=(mock_report, Path("mock-report.json")),
        ) as runner:
            exit_code = main(
                [
                    "discover-mapped-media-sources",
                    "--mapping",
                    "mapping.json",
                    "--sheet",
                    SHEET,
                ]
            )
        self.assertEqual(exit_code, 0)
        runner.assert_called_once()
        sheets_loader.assert_called_once_with()
        full_loader.assert_not_called()

    def test_43_regenerated_sku_source_bindings_are_restored(self) -> None:
        item = sku_item("CLM-ULTRA-VICA")
        item_without_source = dict(item)
        item_without_source.pop("product_source")
        report = sku_report(item_without_source)
        report["product_source_bindings"] = [
            {
                "sku": "CLM-ULTRA-VICA",
                "series": "ultra",
                "product_identity": "VICA",
                "product_source": {"start_row": 10, "end_row": 20},
            }
        ]
        entries = restore_verified_sku_entries(report)
        self.assertEqual(entries[0].product_start_row, 10)
        self.assertEqual(entries[0].result.sku, "CLM-ULTRA-VICA")

    def test_44_exact_range_479_489_joins(self) -> None:
        batch = read_batch(start_row=479, end_row=489, model="SI70CM-AR")
        skus = sku_report(
            sku_item(
                "CLM-CLASSIC-SI70CM-AR",
                start_row=479,
                end_row=489,
                series="classic",
                identity="SI70CM-AR",
            )
        )
        report = built_report(batch, skus=skus)
        self.assertEqual(
            report["results"][0]["sku"],
            "CLM-CLASSIC-SI70CM-AR",
        )

    def test_45_exact_range_490_500_joins(self) -> None:
        batch = read_batch(start_row=490, end_row=500, model="FD160CM-MERU")
        skus = sku_report(
            sku_item(
                "CLM-PRO-FD160CM-MERU",
                start_row=490,
                end_row=500,
                series="pro",
                identity="FD160CM-MERU",
            )
        )
        report = built_report(batch, skus=skus)
        self.assertEqual(
            report["results"][0]["sku"],
            "CLM-PRO-FD160CM-MERU",
        )

    def test_46_source_order_is_irrelevant_for_multiple_ranges(self) -> None:
        first = read_batch(
            coordinate="I488",
            start_row=479,
            end_row=489,
            model="FIRST",
        ).results[0]
        second = read_batch(
            coordinate="I499",
            start_row=490,
            end_row=500,
            model="SECOND",
        ).results[0]
        batch = SecureMediaReferenceReadBatch((second, first), 2, 1, 0)
        skus = sku_report(
            sku_item(
                "CLM-ULTRA-FIRST",
                start_row=479,
                end_row=489,
                identity="FIRST",
            ),
            sku_item(
                "CLM-ULTRA-SECOND",
                start_row=490,
                end_row=500,
                identity="SECOND",
            ),
        )
        report = built_report(batch, skus=skus)
        self.assertEqual(
            [item["sku"] for item in report["results"]],
            ["CLM-ULTRA-FIRST", "CLM-ULTRA-SECOND"],
        )

    def test_47_missing_media_identity_allows_unique_exact_range(self) -> None:
        batch = read_batch(model=None)
        report = built_report(
            batch,
            skus=sku_report(sku_item("CLM-ULTRA-VICA")),
        )
        self.assertEqual(report["results"][0]["sku"], "CLM-ULTRA-VICA")
        self.assertNotIn(
            "sku_join_identity_conflict",
            report["results"][0]["warnings"],
        )

    def test_48_present_conflicting_identity_is_not_joined(self) -> None:
        batch = read_batch(model="OTHER")
        report = built_report(
            batch,
            skus=sku_report(sku_item("CLM-ULTRA-VICA")),
        )
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn(
            "sku_join_identity_conflict",
            report["results"][0]["warnings"],
        )

    def test_49_no_nearest_row_fallback(self) -> None:
        skus = sku_report(
            sku_item(
                "CLM-ULTRA-NEARBY",
                start_row=11,
                end_row=21,
            )
        )
        report = built_report(skus=skus)
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn("sku_join_not_found", report["results"][0]["warnings"])

    def test_50_no_identity_substring_fallback(self) -> None:
        skus = sku_report(
            sku_item(
                "CLM-ULTRA-VICA-EXTENDED",
                identity="VICA-EXTENDED",
            )
        )
        report = built_report(skus=skus)
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn(
            "sku_join_identity_conflict",
            report["results"][0]["warnings"],
        )

    def test_51_distinct_skus_on_same_range_are_ambiguous_first(self) -> None:
        skus = sku_report(
            sku_item("CLM-ULTRA-VICA", identity="VICA"),
            sku_item("CLM-ULTRA-OTHER", identity="OTHER"),
        )
        report = built_report(skus=skus)
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn("sku_join_ambiguous", report["results"][0]["warnings"])

    def test_52_missing_product_source_becomes_not_found(self) -> None:
        item = sku_item("CLM-ULTRA-VICA")
        item["product_source"] = None
        report = built_report(skus=sku_report(item))
        self.assertIsNone(report["results"][0]["sku"])
        self.assertIn("sku_join_not_found", report["results"][0]["warnings"])

    def test_53_sku_report_input_is_immutable(self) -> None:
        skus = sku_report(sku_item("CLM-ULTRA-VICA"))
        before = copy.deepcopy(skus)
        built_report(skus=skus)
        self.assertEqual(skus, before)

    def test_54_media_result_input_is_immutable_during_sku_join(self) -> None:
        batch = read_batch()
        before = copy.deepcopy(batch)
        built_report(batch, skus=sku_report(sku_item("CLM-ULTRA-VICA")))
        self.assertEqual(batch, before)

    def test_55_sku_join_summary_counts_joined(self) -> None:
        summary = built_report(
            skus=sku_report(sku_item("CLM-ULTRA-VICA"))
        )["summary"]
        self.assertEqual(summary["sku_joined"], 1)
        self.assertEqual(summary["sku_join_not_found"], 0)
        self.assertEqual(summary["sku_join_ambiguous"], 0)

    def test_56_sku_join_summary_counts_not_found(self) -> None:
        summary = built_report(
            skus=sku_report(
                sku_item(
                    "CLM-ULTRA-OTHER",
                    start_row=30,
                    end_row=40,
                )
            )
        )["summary"]
        self.assertEqual(summary["sku_joined"], 0)
        self.assertEqual(summary["sku_join_not_found"], 1)

    def test_57_sku_join_summary_counts_ambiguous(self) -> None:
        summary = built_report(
            skus=sku_report(
                sku_item("CLM-ULTRA-VICA-A"),
                sku_item("CLM-ULTRA-VICA-B"),
            )
        )["summary"]
        self.assertEqual(summary["sku_joined"], 0)
        self.assertEqual(summary["sku_join_ambiguous"], 1)

    def test_58_matching_snapshot_provenance_is_accepted(self) -> None:
        mapping = {"inputs": {"products": "reports/products.json"}}
        skus = {"input_file": "reports\\products.json"}
        validate_sku_snapshot_compatibility(mapping, skus)

    def test_59_cross_snapshot_join_stops_before_google_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping_path = root / "mapping.json"
            sku_path = root / "sku.json"
            mapping_payload = mapping_report(
                product_result(media_item(status="redacted"))
            )
            mapping_payload["inputs"] = {
                "products": "reports/products-a.json"
            }
            skus = sku_report(sku_item("CLM-ULTRA-VICA"))
            skus["input_file"] = "reports/products-b.json"
            mapping_path.write_text(
                json.dumps(mapping_payload),
                encoding="utf-8",
            )
            sku_path.write_text(json.dumps(skus), encoding="utf-8")
            factory = FakeFactory(metadata_response())
            with self.assertRaisesRegex(
                MediaSourceDiscoveryDryRunInputError,
                "sku_snapshot_mismatch",
            ):
                run_media_source_discovery_dry_run(
                    mapping_path,
                    SHEET,
                    FakeSettings(),
                    factory,
                    project_root=root,
                    sku_report_input_path=sku_path,
                )
        self.assertEqual(factory.calls, 0)

    def test_60_current_eight_source_ranges_join_exactly(self) -> None:
        current_ranges = (
            (479, 489, "I488", "MODEL-1"),
            (490, 500, "I499", "MODEL-2"),
            (501, 511, "I510", "MODEL-3"),
            (512, 522, "I521", "MODEL-4"),
            (523, 533, "I532", "MODEL-5"),
            (534, 544, "I543", "MODEL-6"),
            (545, 555, "I554", "MODEL-7"),
            (556, 565, "I565", "MODEL-8"),
        )
        read_results = tuple(
            read_batch(
                coordinate=coordinate,
                start_row=start_row,
                end_row=end_row,
                model=identity,
            ).results[0]
            for start_row, end_row, coordinate, identity in current_ranges
        )
        batch = SecureMediaReferenceReadBatch(read_results, 8, 1, 0)
        skus = sku_report(
            *(
                sku_item(
                    f"CLM-ULTRA-{identity}",
                    start_row=start_row,
                    end_row=end_row,
                    identity=identity,
                )
                for start_row, end_row, _, identity in current_ranges
            )
        )
        report = built_report(batch, skus=skus)

        self.assertEqual(report["summary"]["sku_joined"], 8)
        self.assertEqual(
            [item["product_source"] for item in report["results"]],
            [
                {"start_row": start_row, "end_row": end_row}
                for start_row, end_row, _, _ in current_ranges
            ],
        )


if __name__ == "__main__":
    unittest.main()
