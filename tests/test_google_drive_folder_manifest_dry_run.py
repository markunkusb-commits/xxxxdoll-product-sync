from __future__ import annotations

import copy
import io
import json
import socket
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
import sync_worker.google_drive_folder_manifest_dry_run as manifest_dry_run  # noqa: E402
from sync_worker.config import (  # noqa: E402
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_DRIVE_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
)
from sync_worker.google_api import (  # noqa: E402
    GoogleClients,
    GoogleDriveMetadataGateway,
)
from sync_worker.google_drive_folder_manifest import (  # noqa: E402
    FOLDER_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    DriveMetadataScopeUnavailable,
    GoogleDriveFolderManifestError,
    build_drive_folder_manifests_with_gateway,
    create_secure_google_drive_folder_handle,
    fingerprint_drive_id,
)
from sync_worker.google_drive_folder_manifest_dry_run import (  # noqa: E402
    GoogleDriveFolderManifestDryRunError,
    REPORT_FILENAME,
    build_drive_folder_manifest_report,
    run_drive_folder_manifest_dry_run,
    validate_drive_manifest_scopes,
)
from sync_worker.image_mapping import (  # noqa: E402
    REDACTED_REFERENCE,
    ProductSourceRange,
    SupplierMediaSourceReference,
)
from sync_worker.media_source_discovery import discover_media_source  # noqa: E402
from sync_worker.media_source_discovery_dry_run import (  # noqa: E402
    discover_from_secure_read_result,
    join_verified_sku,
    restore_verified_sku_entries,
    run_media_source_discovery_dry_run,
)
from sync_worker.secure_media_reference_read import (  # noqa: E402
    SecureMediaReferenceReader,
    validate_mapping_report,
)
from tests.test_google_drive_folder_manifest import (  # noqa: E402
    FakeHttpError,
    MD5,
    drive_file,
)
from tests.test_media_source_discovery_dry_run import (  # noqa: E402
    read_batch,
    sku_item,
    sku_report,
)
from tests.test_secure_media_reference_read import (  # noqa: E402
    FakeFactory,
    FakeSettings,
    FakeSheets,
    FakeValues,
    SHEET,
    mapping_report,
    media_item,
    metadata_response,
    product_result,
    value_range,
)


FOLDER_ID = "DRY_RUN_FOLDER_PRIVATE_123"
FILE_ID = "DRY_RUN_FILE_PRIVATE_456"
BUSINESS_RESULT_FIELDS = {
    "sku", "product_source", "folder_id_fingerprint", "status", "item_count",
    "image_candidate_count", "nested_folder_count", "shortcut_count",
    "google_workspace_file_count", "other_file_count",
    "duplicate_name_candidate_count", "duplicate_content_candidate_count",
    "pages_read", "items", "warnings", "blocking_issues",
}


class FakeDriveRequest:
    def __init__(self, value: object) -> None:
        self.value = value

    def execute(self) -> object:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeDriveFiles:
    def __init__(self, responses: dict[tuple[str, object], object]) -> None:
        self.responses = responses
        self.list_calls: list[dict[str, object]] = []
        self.get_media_calls = 0
        self.export_calls = 0

    def list(self, **kwargs: object) -> FakeDriveRequest:
        self.list_calls.append(dict(kwargs))
        query = str(kwargs["q"])
        folder_id = query.split("'", maxsplit=2)[1]
        key = (folder_id, kwargs.get("pageToken"))
        value = self.responses.get(key, {"files": []})
        if isinstance(value, list):
            if not value:
                raise AssertionError("unexpected extra Drive request")
            value = value.pop(0)
        return FakeDriveRequest(value)

    def get_media(self, **kwargs: object) -> None:
        self.get_media_calls += 1
        raise AssertionError("Drive content download forbidden")

    def export(self, **kwargs: object) -> None:
        self.export_calls += 1
        raise AssertionError("Drive export forbidden")


class FakeDrive:
    def __init__(self, responses: dict[tuple[str, object], object]) -> None:
        self.file_resource = FakeDriveFiles(responses)

    def files(self) -> FakeDriveFiles:
        return self.file_resource


class FakeCombinedFactory:
    def __init__(
        self,
        sheet_response: object,
        drive_responses: dict[tuple[str, object], object] | None = None,
    ) -> None:
        self.values = FakeValues(sheet_response)
        self.drive = FakeDrive(drive_responses or {})
        self.calls = 0

    def create_drive_metadata_clients(self, settings: object) -> GoogleClients:
        self.calls += 1
        return GoogleClients(drive=self.drive, sheets=FakeSheets(self.values))


def settings(
    *,
    drive_scope: str = GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    sheets_scope: str = GOOGLE_SHEETS_READONLY_SCOPE,
) -> SimpleNamespace:
    return SimpleNamespace(
        drive_scope=drive_scope,
        sheets_scope=sheets_scope,
        clm_spreadsheet_id="mock-spreadsheet-id",
    )


def local_reports(
    count: int = 1,
    *,
    url_for_index=None,
) -> tuple[dict[str, object], dict[str, object], list[str], list[str]]:
    products: list[dict[str, object]] = []
    skus: list[dict[str, object]] = []
    folder_ids: list[str] = []
    coordinates: list[str] = []
    for index in range(count):
        start = 10 + index * 10
        end = start + 5
        coordinate = f"I{start + 1}"
        model = f"MODEL{index}"
        folder_id = f"DRY_RUN_FOLDER_PRIVATE_{index:03d}"
        raw_url = (
            url_for_index(index, folder_id)
            if url_for_index is not None
            else f"https://drive.google.com/drive/folders/{folder_id}"
        )
        products.append(
            product_result(
                media_item(
                    coordinate=coordinate,
                    marker=f"B{start}",
                    raw=raw_url,
                    status="redacted",
                ),
                start_row=start,
                end_row=end,
                model=model,
            )
        )
        skus.append(
            sku_item(
                f"CLM-ULTRA-{model}",
                start_row=start,
                end_row=end,
                identity=model,
            )
        )
        folder_ids.append(folder_id)
        coordinates.append(coordinate)
    mapping = mapping_report(*products)
    mapping["inputs"] = {"products": "reports/mock-products.json"}
    sku_payload = sku_report(*skus)
    sku_payload["input_file"] = "reports/mock-products.json"
    return mapping, sku_payload, folder_ids, coordinates


def sheet_response(
    coordinates: list[str], urls: list[str]
) -> dict[str, object]:
    return metadata_response(
        *(
            value_range(coordinate, raw)
            for coordinate, raw in zip(coordinates, urls, strict=True)
        )
    )


class GoogleDriveFolderManifestDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "reports").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(
        self,
        *,
        mapping: dict[str, object] | None = None,
        sku_payload: dict[str, object] | None = None,
        urls: list[str] | None = None,
        drive_responses: dict[tuple[str, object], object] | None = None,
        active_settings: object | None = None,
    ):
        default_mapping, default_skus, folder_ids, coordinates = local_reports()
        active_mapping = mapping or default_mapping
        active_skus = sku_payload or default_skus
        selected = validate_mapping_report(active_mapping)
        active_coordinates = [item.reference_coordinate for item in selected]
        active_urls = urls or [
            f"https://drive.google.com/drive/folders/{folder_ids[index]}"
            for index in range(len(active_coordinates))
        ]
        mapping_path = self.root / "mapping.json"
        sku_path = self.root / "sku.json"
        mapping_path.write_text(json.dumps(active_mapping), encoding="utf-8")
        sku_path.write_text(json.dumps(active_skus), encoding="utf-8")
        factory = FakeCombinedFactory(
            sheet_response(active_coordinates, active_urls),
            drive_responses,
        )
        report, report_path = run_drive_folder_manifest_dry_run(
            mapping_path,
            SHEET,
            sku_path,
            active_settings or settings(),
            factory,
            project_root=self.root,
        )
        return report, report_path, factory

    def test_01_cli_registered(self) -> None:
        args = build_parser().parse_args(
            [
                "build-drive-folder-manifests",
                "--mapping",
                "mapping.json",
                "--sheet",
                SHEET,
                "--sku-report",
                "sku.json",
            ]
        )
        self.assertEqual(args.command, "build-drive-folder-manifests")

    def test_02_mapping_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["build-drive-folder-manifests", "--sheet", SHEET, "--sku-report", "s.json"]
            )

    def test_03_sheet_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["build-drive-folder-manifests", "--mapping", "m.json", "--sku-report", "s.json"]
            )

    def test_04_sku_report_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["build-drive-folder-manifests", "--mapping", "m.json", "--sheet", SHEET]
            )

    def test_05_cli_argument_paths(self) -> None:
        args = build_parser().parse_args(
            ["build-drive-folder-manifests", "--mapping", "m.json", "--sheet", SHEET, "--sku-report", "s.json"]
        )
        self.assertEqual(args.mapping_input_path, Path("m.json"))
        self.assertEqual(args.sku_report_input_path, Path("s.json"))

    def test_06_cli_dispatches_without_running_real_command(self) -> None:
        with patch("sync_worker.cli._run_build_drive_folder_manifests", return_value=0) as runner:
            result = main(["build-drive-folder-manifests", "--mapping", "m.json", "--sheet", SHEET, "--sku-report", "s.json"])
        self.assertEqual(result, 0)
        runner.assert_called_once()

    def test_07_exact_mapping_gate_reused(self) -> None:
        mapping, skus, _, _ = local_reports()
        product = mapping["results"][0]
        product["media_sources"].append(media_item(coordinate="I99", match_status="unmatched_media_source"))
        report, _, factory = self._run(mapping=mapping, sku_payload=skus)
        self.assertEqual(report["summary"]["total_folders"], 1)
        self.assertEqual(len(factory.values.batch_get_calls[0]["ranges"]), 1)

    def test_08_unmatched_mapping_never_creates_client(self) -> None:
        mapping, skus, _, _ = local_reports()
        mapping["results"][0]["media_sources"][0]["match_status"] = "unmatched_media_source"
        mapping["results"][0]["media_sources"][0]["ambiguous"] = False
        report, _, factory = self._run(mapping=mapping, sku_payload=skus, urls=[])
        self.assertEqual(factory.calls, 0)
        self.assertEqual(report["results"], [])

    def test_09_sku_exact_range_join_reused(self) -> None:
        mapping, skus, _, _ = local_reports()
        mapped = validate_mapping_report(mapping)[0]
        from sync_worker.media_source_discovery_dry_run import restore_verified_sku_entries
        result, warnings = join_verified_sku(mapped, restore_verified_sku_entries(skus))
        self.assertEqual(result.sku, "CLM-ULTRA-MODEL0")
        self.assertEqual(warnings, ())

    def test_10_sku_not_found_blocks_drive(self) -> None:
        mapping, skus, _, _ = local_reports()
        skus["results"] = []
        report, _, factory = self._run(mapping=mapping, sku_payload=skus)
        self.assertEqual(report["summary"]["sku_join_not_found"], 1)
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_11_sku_ambiguous_blocks_drive(self) -> None:
        mapping, skus, _, _ = local_reports()
        skus["results"].append(sku_item("CLM-ULTRA-OTHER", start_row=10, end_row=15, identity="MODEL0"))
        report, _, factory = self._run(mapping=mapping, sku_payload=skus)
        self.assertEqual(report["summary"]["sku_join_ambiguous"], 1)
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_12_secure_reader_uses_one_batch_get(self) -> None:
        _, _, factory = self._run()
        self.assertEqual(len(factory.values.batch_get_calls), 1)

    def test_13_media_discovery_core_is_reused(self) -> None:
        with patch("sync_worker.media_source_discovery_dry_run.discover_media_source", wraps=discover_media_source) as discovery:
            self._run()
        discovery.assert_called_once()

    def test_14_secure_handle_core_is_reused(self) -> None:
        with patch("sync_worker.google_drive_folder_manifest_dry_run.create_secure_google_drive_folder_handle", wraps=create_secure_google_drive_folder_handle) as creator:
            self._run()
        creator.assert_called_once()

    def test_15_manifest_core_is_reused(self) -> None:
        with patch("sync_worker.google_drive_folder_manifest_dry_run.build_drive_folder_manifests_with_gateway", wraps=build_drive_folder_manifests_with_gateway) as builder:
            self._run()
        builder.assert_called_once()

    def test_16_google_drive_folder_is_accepted(self) -> None:
        report, _, _ = self._run()
        self.assertEqual(report["summary"]["total_folders"], 1)

    def test_17_non_drive_source_is_blocked(self) -> None:
        report, _, factory = self._run(urls=["https://example.test/photo.jpg"])
        self.assertEqual(report["results"][0]["status"], "not_google_drive_folder")
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_18_drive_file_is_not_accepted_as_folder(self) -> None:
        report, _, factory = self._run(urls=["https://drive.google.com/file/d/DRY_RUN_FILE_PRIVATE_789/view"])
        self.assertEqual(report["results"][0]["status"], "not_google_drive_folder")
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_19_exact_metadata_scope_is_accepted(self) -> None:
        validate_drive_manifest_scopes(settings())

    def test_20_drive_readonly_scope_is_blocked(self) -> None:
        with self.assertRaises(DriveMetadataScopeUnavailable):
            validate_drive_manifest_scopes(settings(drive_scope=GOOGLE_DRIVE_READONLY_SCOPE))

    def test_21_full_drive_scope_is_blocked_before_factory(self) -> None:
        with self.assertRaises(DriveMetadataScopeUnavailable):
            self._run(active_settings=settings(drive_scope="https://www.googleapis.com/auth/drive"))

    def test_22_wrong_sheets_scope_is_blocked_before_factory(self) -> None:
        with self.assertRaises(DriveMetadataScopeUnavailable):
            self._run(active_settings=settings(sheets_scope="https://www.googleapis.com/auth/spreadsheets"))

    def test_23_one_folder_uses_files_list_only(self) -> None:
        _, _, factory = self._run()
        self.assertEqual(len(factory.drive.file_resource.list_calls), 1)
        self.assertEqual(factory.drive.file_resource.get_media_calls, 0)

    def test_24_eight_folders_are_supported(self) -> None:
        mapping, skus, folder_ids, coords = local_reports(8)
        urls = [f"https://drive.google.com/drive/folders/{item}" for item in folder_ids]
        report, _, factory = self._run(mapping=mapping, sku_payload=skus, urls=urls)
        self.assertEqual(report["summary"]["total_folders"], 8)
        self.assertEqual(len(factory.drive.file_resource.list_calls), 8)

    def test_25_one_page_count(self) -> None:
        report, _, _ = self._run()
        self.assertEqual(report["summary"]["pages_read"], 1)

    def test_26_pagination_is_reused(self) -> None:
        responses = {
            ("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [], "nextPageToken": "NEXT"},
            ("DRY_RUN_FOLDER_PRIVATE_000", "NEXT"): {"files": [drive_file(file_id=FILE_ID)]},
        }
        report, _, factory = self._run(drive_responses=responses)
        self.assertEqual(report["summary"]["pages_read"], 2)
        self.assertEqual(len(factory.drive.file_resource.list_calls), 2)

    def test_27_page_size_is_100(self) -> None:
        _, _, factory = self._run()
        self.assertEqual(factory.drive.file_resource.list_calls[0]["pageSize"], 100)

    def test_28_image_candidate_uses_mime_type(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("no-extension", file_id=FILE_ID, mime_type="image/webp")]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertTrue(report["results"][0]["items"][0]["image_candidate"])

    def test_29_extension_does_not_override_mime_type(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("photo.jpg", file_id=FILE_ID, mime_type="application/pdf")]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertFalse(report["results"][0]["items"][0]["image_candidate"])

    def test_30_nested_folder_is_classified_not_traversed(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("nested", file_id=FILE_ID, mime_type=FOLDER_MIME_TYPE)]}}
        report, _, factory = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["nested_folder_count"], 1)
        self.assertEqual(len(factory.drive.file_resource.list_calls), 1)

    def test_31_shortcut_is_not_followed(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("shortcut", file_id=FILE_ID, mime_type=SHORTCUT_MIME_TYPE)]}}
        report, _, factory = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["shortcut_count"], 1)
        self.assertEqual(len(factory.drive.file_resource.list_calls), 1)

    def test_32_workspace_file_is_classified(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("doc", file_id=FILE_ID, mime_type="application/vnd.google-apps.document")]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["google_workspace_file_count"], 1)

    def test_33_other_file_is_classified(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("notes.txt", file_id=FILE_ID, mime_type="text/plain")]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["other_file_count"], 1)

    def test_34_image_dimensions_are_projected(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file(file_id=FILE_ID, image_metadata={"width": 640, "height": 480})]}}
        report, _, _ = self._run(drive_responses=responses)
        item = report["results"][0]["items"][0]
        self.assertEqual((item["image_width"], item["image_height"]), (640, 480))

    def test_35_missing_dimensions_remain_null(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file(file_id=FILE_ID)]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertIsNone(report["results"][0]["items"][0]["image_width"])

    def test_36_md5_is_renamed_provider_content_checksum(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file(file_id=FILE_ID)]}}
        report, _, _ = self._run(drive_responses=responses)
        item = report["results"][0]["items"][0]
        self.assertEqual(item["provider_content_checksum"], MD5)
        self.assertNotIn("md5_checksum", item)

    def test_37_duplicate_name_candidates_are_preserved(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("same.jpg", file_id="FILE_A", md5=None), drive_file("SAME.JPG", file_id="FILE_B", md5=None)]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["duplicate_name_candidate_count"], 2)

    def test_38_duplicate_content_candidates_are_preserved(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("a.jpg", file_id="FILE_A"), drive_file("b.jpg", file_id="FILE_B")]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["duplicate_content_candidate_count"], 2)

    def test_39_empty_folder_is_not_error(self) -> None:
        report, _, _ = self._run()
        result = report["results"][0]
        self.assertEqual(result["status"], "empty_folder")
        self.assertIn("empty_media_folder", result["warnings"])

    def test_40_401_is_isolated(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): FakeHttpError(401)}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["status"], "access_denied")

    def test_41_403_isolated_from_other_folder(self) -> None:
        mapping, skus, folder_ids, _ = local_reports(2)
        urls = [f"https://drive.google.com/drive/folders/{item}" for item in folder_ids]
        responses = {(folder_ids[0], None): FakeHttpError(403), (folder_ids[1], None): {"files": []}}
        report, _, _ = self._run(mapping=mapping, sku_payload=skus, urls=urls, drive_responses=responses)
        self.assertEqual([item["status"] for item in report["results"]], ["access_denied", "empty_folder"])

    def test_42_404_is_safe_status(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): FakeHttpError(404)}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["results"][0]["status"], "missing_or_inaccessible")

    def test_43_429_is_retried(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): [FakeHttpError(429), {"files": []}]}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["summary"]["drive_read_requests_performed"], 2)

    def test_44_5xx_is_retried(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): [FakeHttpError(503), {"files": []}]}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["summary"]["drive_read_requests_performed"], 2)

    def test_45_raw_folder_id_not_serialized(self) -> None:
        report, _, _ = self._run()
        self.assertNotIn("DRY_RUN_FOLDER_PRIVATE_000", json.dumps(report))

    def test_46_raw_file_id_not_serialized(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file(file_id=FILE_ID)]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertNotIn(FILE_ID, json.dumps(report))
        self.assertEqual(report["results"][0]["items"][0]["file_id_fingerprint"], fingerprint_drive_id(FILE_ID))

    def test_47_drive_url_not_serialized(self) -> None:
        report, _, _ = self._run()
        self.assertNotIn("drive.google.com", json.dumps(report))

    def test_48_unsafe_report_fails_closed(self) -> None:
        from sync_worker.google_drive_folder_manifest_dry_run import _assert_report_safe
        with self.assertRaisesRegex(GoogleDriveFolderManifestDryRunError, "unsafe_drive_manifest_leak"):
            _assert_report_safe({"bad": "https://drive.google.com/drive/folders/SECRET"})

    def test_49_safe_filename_filters_control_and_path_chars(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file("../bad\nname.jpg", file_id=FILE_ID)]}}
        report, _, _ = self._run(drive_responses=responses)
        name = report["results"][0]["items"][0]["safe_name"]
        self.assertNotIn("\n", name)
        self.assertNotIn("..", name)

    def test_50_summary_folder_counts(self) -> None:
        report, _, _ = self._run()
        self.assertEqual(report["summary"]["empty_folders"], 1)

    def test_51_summary_item_and_image_counts(self) -> None:
        responses = {("DRY_RUN_FOLDER_PRIVATE_000", None): {"files": [drive_file(file_id=FILE_ID)]}}
        report, _, _ = self._run(drive_responses=responses)
        self.assertEqual(report["summary"]["total_items"], 1)
        self.assertEqual(report["summary"]["image_candidates"], 1)

    def test_52_request_summary_is_separated(self) -> None:
        report, _, _ = self._run()
        summary = report["summary"]
        self.assertEqual(summary["sheets_read_requests_performed"], 1)
        self.assertEqual(summary["drive_read_requests_performed"], 1)
        self.assertEqual(summary["network_requests_performed"], 2)

    def test_53_download_requests_are_zero(self) -> None:
        report, _, factory = self._run()
        self.assertEqual(report["summary"]["download_requests_performed"], 0)
        self.assertEqual(factory.drive.file_resource.get_media_calls, 0)

    def test_54_write_requests_are_zero(self) -> None:
        report, _, factory = self._run()
        self.assertEqual(report["summary"]["write_requests_performed"], 0)
        self.assertEqual(factory.values.write_calls, 0)

    def test_55_no_alt_media_requested(self) -> None:
        _, _, factory = self._run()
        self.assertNotIn("alt", factory.drive.file_resource.list_calls[0])

    def test_56_no_export_or_shortcut_follow(self) -> None:
        _, _, factory = self._run()
        self.assertEqual(factory.drive.file_resource.export_calls, 0)

    def test_57_deterministic_result_order(self) -> None:
        mapping, skus, folder_ids, _ = local_reports(2)
        mapping["results"].reverse()
        urls = [f"https://drive.google.com/drive/folders/{folder_ids[1]}", f"https://drive.google.com/drive/folders/{folder_ids[0]}"]
        report, _, _ = self._run(mapping=mapping, sku_payload=skus, urls=urls)
        starts = [item["product_source"]["start_row"] for item in report["results"]]
        self.assertEqual(starts, sorted(starts))

    def test_58_inputs_are_immutable(self) -> None:
        mapping, skus, _, _ = local_reports()
        before_mapping = copy.deepcopy(mapping)
        before_skus = copy.deepcopy(skus)
        self._run(mapping=mapping, sku_payload=skus)
        self.assertEqual(mapping, before_mapping)
        self.assertEqual(skus, before_skus)

    def test_59_output_filename(self) -> None:
        _, report_path, _ = self._run()
        self.assertEqual(report_path.name, REPORT_FILENAME)
        self.assertTrue(report_path.is_file())

    def test_60_mock_run_does_not_open_socket(self) -> None:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            report, _, _ = self._run()
        self.assertEqual(report["status"], "ok")

    def test_61_secure_read_result_converts_to_supplier_reference(self) -> None:
        result = read_batch().results[0]
        source = result.to_supplier_reference()
        self.assertIsInstance(source, SupplierMediaSourceReference)
        self.assertEqual(source.raw_reference, result.raw_reference)

    def test_62_fresh_raw_url_classifies_as_google_drive_folder(self) -> None:
        discovery = discover_from_secure_read_result(read_batch().results[0], None)
        self.assertEqual(discovery.discovery_status, "classified")
        self.assertEqual(discovery.provider, "google_drive")
        self.assertEqual(discovery.resource_kind, "folder")

    def test_63_mapping_redaction_does_not_replace_fresh_reference(self) -> None:
        batch = read_batch(mapping_status="redacted")
        discovery = discover_from_secure_read_result(batch.results[0], None)
        self.assertEqual(discovery.provider, "google_drive")
        self.assertEqual(discovery.resource_kind, "folder")

    def test_64_redacted_projection_is_not_treated_as_fresh_url(self) -> None:
        redacted_batch = read_batch(
            raw=REDACTED_REFERENCE,
            mapping_status="redacted",
        )
        redacted = discover_from_secure_read_result(
            redacted_batch.results[0], None
        )
        fresh = discover_from_secure_read_result(read_batch().results[0], None)
        self.assertEqual(redacted.discovery_status, "redacted_reference")
        self.assertEqual(fresh.discovery_status, "classified")

    def test_65_report_projection_cannot_enter_fresh_handoff(self) -> None:
        projection = read_batch().results[0].to_supplier_reference().to_report_dict()
        with self.assertRaisesRegex(
            TypeError, "SecureMediaReferenceReadResult"
        ):
            discover_from_secure_read_result(projection, None)  # type: ignore[arg-type]

    def test_66_provider_resource_id_stays_memory_only(self) -> None:
        discovery = discover_from_secure_read_result(read_batch().results[0], None)
        self.assertIsNotNone(discovery.provider_resource_id)
        self.assertNotIn(discovery.provider_resource_id, repr(discovery))
        self.assertNotIn(discovery.provider_resource_id, json.dumps(discovery.to_dict()))

    def test_67_fresh_discovery_creates_secure_folder_handle(self) -> None:
        payload = sku_report(sku_item("CLM-ULTRA-VICA"))
        sku_result = restore_verified_sku_entries(payload)[0].result
        batch = read_batch()
        discovery = discover_from_secure_read_result(batch.results[0], sku_result)
        handle = create_secure_google_drive_folder_handle(
            discovery,
            ProductSourceRange(10, 20),
        )
        self.assertEqual(handle.sku, "CLM-ULTRA-VICA")
        self.assertEqual(
            handle.folder_id_fingerprint,
            discovery.resource_id_fingerprint,
        )

    def test_68_manifest_uses_shared_fresh_handoff_helper(self) -> None:
        with patch(
            "sync_worker.google_drive_folder_manifest_dry_run.discover_from_secure_read_result",
            wraps=discover_from_secure_read_result,
        ) as handoff:
            report, _, _ = self._run()
        handoff.assert_called_once()
        self.assertEqual(report["summary"]["total_folders"], 1)

    def test_69_mocked_fresh_discovery_flows_directly_into_handle(self) -> None:
        payload = sku_report(sku_item("CLM-ULTRA-MODEL0", end_row=15, identity="MODEL0"))
        sku_result = restore_verified_sku_entries(payload)[0].result
        fresh_discovery = discover_from_secure_read_result(
            read_batch(model="MODEL0").results[0], sku_result
        )
        with (
            patch.object(
                manifest_dry_run,
                "discover_from_secure_read_result",
                return_value=fresh_discovery,
            ) as handoff,
            patch.object(
                manifest_dry_run,
                "create_secure_google_drive_folder_handle",
                wraps=create_secure_google_drive_folder_handle,
            ) as creator,
        ):
            report, _, factory = self._run()
        handoff.assert_called_once()
        creator.assert_called_once_with(
            fresh_discovery,
            ProductSourceRange(10, 15),
        )
        self.assertEqual(len(factory.drive.file_resource.list_calls), 1)
        self.assertEqual(report["summary"]["total_folders"], 1)

    def test_70_manifest_module_has_no_direct_discovery_entrypoint(self) -> None:
        self.assertFalse(hasattr(manifest_dry_run, "discover_media_source"))

    def test_71_helper_none_has_no_projection_fallback(self) -> None:
        with patch.object(
            manifest_dry_run,
            "discover_from_secure_read_result",
            return_value=None,
        ) as handoff:
            report, _, factory = self._run()
        handoff.assert_called_once()
        self.assertEqual(report["summary"]["total_folders"], 0)
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_72_helper_drive_file_does_not_create_folder_handle(self) -> None:
        fresh = discover_from_secure_read_result(read_batch().results[0], None)
        drive_file_discovery = replace(fresh, resource_kind="file")
        with (
            patch.object(
                manifest_dry_run,
                "discover_from_secure_read_result",
                return_value=drive_file_discovery,
            ),
            patch.object(
                manifest_dry_run,
                "create_secure_google_drive_folder_handle",
                wraps=create_secure_google_drive_folder_handle,
            ) as creator,
        ):
            report, _, factory = self._run()
        creator.assert_not_called()
        self.assertEqual(report["results"][0]["status"], "not_google_drive_folder")
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_73_eight_sources_each_use_shared_handoff_once(self) -> None:
        mapping, skus, folder_ids, _ = local_reports(8)
        urls = [
            f"https://drive.google.com/drive/folders/{folder_id}"
            for folder_id in folder_ids
        ]
        with patch.object(
            manifest_dry_run,
            "discover_from_secure_read_result",
            wraps=discover_from_secure_read_result,
        ) as handoff:
            report, _, factory = self._run(
                mapping=mapping,
                sku_payload=skus,
                urls=urls,
            )
        self.assertEqual(handoff.call_count, 8)
        self.assertEqual(report["summary"]["total_folders"], 8)
        self.assertGreater(report["summary"]["drive_read_requests_performed"], 0)
        self.assertEqual(len(factory.drive.file_resource.list_calls), 8)

    def test_74_mocked_provider_id_remains_memory_only_after_listing(self) -> None:
        payload = sku_report(sku_item("CLM-ULTRA-MODEL0", end_row=15, identity="MODEL0"))
        sku_result = restore_verified_sku_entries(payload)[0].result
        fresh = discover_from_secure_read_result(
            read_batch(model="MODEL0").results[0], sku_result
        )
        raw_folder_id = fresh.provider_resource_id
        with patch.object(
            manifest_dry_run,
            "discover_from_secure_read_result",
            return_value=fresh,
        ):
            report, _, factory = self._run()
        self.assertIn(raw_folder_id, factory.drive.file_resource.list_calls[0]["q"])
        self.assertNotIn(raw_folder_id, json.dumps(report))
        self.assertNotIn("https://drive.google.com", json.dumps(report))

    def test_75_success_report_only_contains_business_fields(self) -> None:
        report, _, _ = self._run()
        self.assertEqual(set(report["results"][0]), BUSINESS_RESULT_FIELDS)

    def test_76_blocked_report_only_contains_business_fields(self) -> None:
        report, _, _ = self._run(urls=["https://example.test/photo.jpg"])
        self.assertEqual(set(report["results"][0]), BUSINESS_RESULT_FIELDS)

    def test_77_written_report_has_no_diagnostic_projection(self) -> None:
        report, path, _ = self._run()
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {**report, "write_requests_performed": 0})
        self.assertEqual(set(saved), {"status", "inputs", "summary", "results", "write_requests_performed"})
        self.assertEqual(set(saved["results"][0]), BUSINESS_RESULT_FIELDS)
        self.assertNotIn("diagnostic", json.dumps(saved))

    def test_78_cell_shape_diagnostic_command_is_removed(self) -> None:
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["diagnose-media-cell-shapes"])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("diagnose-media-cell-shapes", build_parser().format_help())

    def test_79_parity_diagnostic_command_is_removed(self) -> None:
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["diagnose-media-reference-parity"])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("diagnose-media-reference-parity", build_parser().format_help())

    def test_80_manifest_never_serializes_raw_provider_values(self) -> None:
        report, _, _ = self._run()
        serialized = json.dumps(report)
        self.assertNotIn("DRY_RUN_FOLDER_PRIVATE_000", serialized)
        self.assertNotIn("https://drive.google.com", serialized)
        self.assertNotIn('"provider_resource_id":', serialized)

    def test_81_handle_error_uses_approved_safe_code(self) -> None:
        with patch.object(
            manifest_dry_run,
            "create_secure_google_drive_folder_handle",
            side_effect=GoogleDriveFolderManifestError(
                "invalid_google_drive_folder_id"
            ),
        ):
            report, _, factory = self._run()
        result = report["results"][0]
        self.assertNotIn("diagnostic", result)
        self.assertEqual(
            result["status"],
            "invalid_google_drive_folder_id",
        )
        self.assertIn("invalid_google_drive_folder_id", result["blocking_issues"])
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_82_unapproved_handle_exception_is_generic_and_redacted(self) -> None:
        secret = "https://drive.google.com/drive/folders/NEVER_REPORT_THIS_ID"
        with patch.object(
            manifest_dry_run,
            "create_secure_google_drive_folder_handle",
            side_effect=RuntimeError(secret),
        ):
            report, _, _ = self._run()
        serialized = json.dumps(report)
        self.assertEqual(
            report["results"][0]["status"],
            "folder_handle_creation_failed",
        )
        self.assertIn("folder_handle_creation_failed", report["results"][0]["blocking_issues"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("NEVER_REPORT_THIS_ID", serialized)

    def test_83_resource_key_and_probe_are_absent(self) -> None:
        resource_key = "RESOURCE_KEY_PRIVATE_FIXTURE"
        url = (
            "https://drive.google.com/drive/folders/"
            f"DRY_RUN_FOLDER_PRIVATE_000?resourcekey={resource_key}"
        )
        report, _, _ = self._run(urls=[url])
        self.assertEqual(report["summary"]["total_folders"], 1)
        self.assertNotIn("resource_key_present", json.dumps(report))
        self.assertNotIn(resource_key, json.dumps(report))

    def test_84_discovery_and_manifest_share_fingerprint_and_sku(self) -> None:
        batch = read_batch(mapping_status="redacted")
        read_result = batch.results[0]
        payload = sku_report(sku_item("CLM-ULTRA-VICA"))
        sku_result = restore_verified_sku_entries(payload)[0].result
        direct = discover_from_secure_read_result(read_result, sku_result)
        report = build_drive_folder_manifest_report(
            (read_result,),
            payload,
            mapping_input_file="mock-mapping.json",
            sheet_title=SHEET,
            sku_report_input_file="mock-sku.json",
            sheets_read_requests_performed=1,
            gateway=GoogleDriveMetadataGateway(FakeDrive({})),
        )
        result = report["results"][0]
        self.assertEqual(result["folder_id_fingerprint"], direct.resource_id_fingerprint)
        self.assertEqual(result["sku"], sku_result.sku)
        self.assertEqual(result["status"], "empty_folder")

    def test_85_redacted_reference_still_blocks_drive(self) -> None:
        report, _, factory = self._run(urls=[REDACTED_REFERENCE])
        self.assertNotIn("diagnostic", report["results"][0])
        self.assertEqual(
            report["results"][0]["status"], "media_reference_link_missing"
        )
        self.assertEqual(factory.drive.file_resource.list_calls, [])

    def test_86_smart_chip_probes_are_not_projected(self) -> None:
        result = replace(
            read_batch(mapping_status="redacted").results[0],
            smart_chip_present=True,
            smart_chip_rich_link_count=2,
            smart_chip_unique_uri=True,
        )
        payload = sku_report(sku_item("CLM-ULTRA-VICA"))
        report = build_drive_folder_manifest_report(
            (result,),
            payload,
            mapping_input_file="mock-mapping.json",
            sheet_title=SHEET,
            sku_report_input_file="mock-sku.json",
            sheets_read_requests_performed=1,
            gateway=GoogleDriveMetadataGateway(FakeDrive({})),
        )
        self.assertEqual(report["summary"]["total_folders"], 1)
        self.assertEqual(set(report["results"][0]), BUSINESS_RESULT_FIELDS)
        serialized = json.dumps(report)
        self.assertNotIn("smart_chip", serialized)
        self.assertNotIn("FOLDER_ID_PRIVATE", serialized)
        self.assertNotIn("https://drive.google.com", serialized)

    def test_87_cell_link_and_formula_probes_are_not_projected(self) -> None:
        result = replace(
            read_batch(mapping_status="redacted").results[0],
            cell_level_link_present=True,
            formula_present=True,
            formula_function="HYPERLINK",
            formula_is_hyperlink=True,
        )
        payload = sku_report(sku_item("CLM-ULTRA-VICA"))
        report = build_drive_folder_manifest_report(
            (result,),
            payload,
            mapping_input_file="mock-mapping.json",
            sheet_title=SHEET,
            sku_report_input_file="mock-sku.json",
            sheets_read_requests_performed=1,
            gateway=GoogleDriveMetadataGateway(FakeDrive({})),
        )
        self.assertEqual(report["summary"]["total_folders"], 1)
        self.assertEqual(set(report["results"][0]), BUSINESS_RESULT_FIELDS)
        serialized = json.dumps(report)
        self.assertNotIn("formula", serialized)
        self.assertNotIn("cell_level_link", serialized)
        self.assertNotIn("formulaValue", serialized)
        self.assertNotIn("https://drive.google.com", serialized)


    def test_88_current_row_fixture_preserves_eight_folder_milestone(self) -> None:
        # Synthetic cells/IDs only; no supplier snapshot is opened.
        ranges = (
            (479, 489, 488), (490, 500, 499), (501, 511, 510),
            (512, 522, 521), (523, 533, 532), (534, 544, 543),
            (545, 555, 554), (556, 565, 565),
        )
        products = []
        skus = []
        folder_ids = []
        coordinates = []
        responses = {}
        for index, (start, end, reference_row) in enumerate(ranges):
            model = f"FIXTURE{index}"
            coordinate = f"I{reference_row}"
            folder_id = f"MOCK_MILESTONE_FOLDER_{index:03d}"
            coordinates.append(coordinate)
            folder_ids.append(folder_id)
            products.append(product_result(
                media_item(coordinate=coordinate, marker=f"B{reference_row}", status="redacted"),
                start_row=start, end_row=end, model=model,
            ))
            skus.append(sku_item(
                f"CLM-ULTRA-{model}", start_row=start, end_row=end, identity=model,
            ))
            files = [drive_file(
                f"nested-{number}", file_id=f"MOCK_NESTED_{index}_{number}",
                mime_type=FOLDER_MIME_TYPE, md5=None,
            ) for number in range(3)]
            if index < 5:
                files.append(drive_file(
                    "shortcut", file_id=f"MOCK_SHORTCUT_{index}",
                    mime_type=SHORTCUT_MIME_TYPE, md5=None,
                ))
            if index < 4:
                files.append(drive_file(
                    "notes.txt", file_id=f"MOCK_OTHER_{index}",
                    mime_type="text/plain", md5=None,
                ))
            responses[(folder_id, None)] = {"files": files}
        mapping = mapping_report(*products)
        mapping["inputs"] = {"products": "reports/mock-current-products.json"}
        sku_payload = sku_report(*skus)
        sku_payload["input_file"] = "reports/mock-current-products.json"
        urls = [f"https://drive.google.com/drive/folders/{item}" for item in folder_ids]
        discovery_factory = FakeFactory(sheet_response(coordinates, urls))

        with (
            patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")) as connect,
            patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")) as create_connection,
            patch.object(manifest_dry_run, "discover_from_secure_read_result", wraps=discover_from_secure_read_result) as handoff,
            patch.object(manifest_dry_run, "create_secure_google_drive_folder_handle", wraps=create_secure_google_drive_folder_handle) as handle_creator,
        ):
            report, path, factory = self._run(
                mapping=mapping, sku_payload=sku_payload, urls=urls,
                drive_responses=responses,
            )
            discovery, _ = run_media_source_discovery_dry_run(
                self.root / "mapping.json", SHEET, FakeSettings(), discovery_factory,
                project_root=self.root, sku_report_input_path=self.root / "sku.json",
            )
        connect.assert_not_called()
        create_connection.assert_not_called()
        self.assertEqual(handoff.call_count, 8)
        self.assertEqual(handle_creator.call_count, 8)
        self.assertEqual(discovery_factory.full_create_calls, 0)
        self.assertFalse(discovery_factory.drive_client_created)
        for name, expected in {
            "sku_joined": 8, "google_drive_sources": 8,
            "folder_candidates": 8, "blocked_sources": 0,
        }.items():
            self.assertEqual(discovery["summary"][name], expected)
        for name, expected in {
            "total_folders": 8, "folders_listed": 8,
            "folders_access_denied": 0, "folders_missing_or_inaccessible": 0,
            "folders_read_failed": 0, "total_items": 33,
            "nested_folders": 24, "shortcuts": 5, "other_files": 4,
            "image_candidates": 0, "sheets_read_requests_performed": 1,
            "drive_read_requests_performed": 8, "network_requests_performed": 9,
            "download_requests_performed": 0, "write_requests_performed": 0,
        }.items():
            self.assertEqual(report["summary"][name], expected, name)
        self.assertEqual(factory.calls, 1)
        for sheets_calls in (factory.values.batch_get_calls, discovery_factory.values.batch_get_calls):
            self.assertEqual(len(sheets_calls), 1)
            self.assertEqual(sheets_calls[0]["ranges"], [f"'{SHEET}'!{cell}" for cell in coordinates])
        drive_calls = factory.drive.file_resource.list_calls
        self.assertEqual(len(drive_calls), 8)
        self.assertEqual(
            [call["q"] for call in drive_calls],
            [f"'{folder_id}' in parents and trashed = false" for folder_id in folder_ids],
        )
        self.assertTrue(all("alt" not in call for call in drive_calls))
        self.assertEqual(factory.drive.file_resource.get_media_calls, 0)
        self.assertEqual(factory.drive.file_resource.export_calls, 0)
        self.assertEqual(factory.values.write_calls, 0)
        self.assertEqual(discovery_factory.values.write_calls, 0)
        self.assertEqual(len(report["results"]), 8)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {**report, "write_requests_performed": 0})
        serialized = json.dumps(report)
        for index, result in enumerate(report["results"]):
            self.assertEqual(set(result), BUSINESS_RESULT_FIELDS)
            self.assertEqual(result["sku"], f"CLM-ULTRA-FIXTURE{index}")
            self.assertEqual(result["product_source"], {"start_row": ranges[index][0], "end_row": ranges[index][1]})
            self.assertEqual(result["folder_id_fingerprint"], fingerprint_drive_id(folder_ids[index]))
        for forbidden in (*folder_ids, "MOCK_NESTED_", "MOCK_SHORTCUT_", "MOCK_OTHER_", "https://", "diagnostic"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
