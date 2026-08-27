from __future__ import annotations

import copy
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import google_drive_folder_manifest_dry_run as root_dry_run
from sync_worker import google_drive_nested_folder_manifest_dry_run as dry_run
from sync_worker.config import ConfigError, GOOGLE_DRIVE_READONLY_SCOPE
from sync_worker.google_drive_folder_manifest import (
    DriveMetadataScopeUnavailable, FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE,
)
from sync_worker.google_drive_nested_folder_manifest import GoogleDriveNestedFolderManifestError
from sync_worker.sanitization import Redactor
from tests.test_google_drive_folder_manifest import FakeHttpError, drive_file
from tests.test_google_drive_folder_manifest_dry_run import (
    FakeCombinedFactory, local_reports, settings, sheet_response,
)
from tests.test_secure_media_reference_read import SHEET


COMMAND = "build-nested-drive-folder-manifests"
ITEM_FIELDS = {
    "safe_name", "mime_type", "size_bytes", "modified_time", "provider_content_checksum",
    "file_id_fingerprint", "item_kind", "image_candidate", "image_width", "image_height", "warnings",
}
RESULT_FIELDS = {
    "sku", "product_source", "root_folder_id_fingerprint", "nested_folder_id_fingerprint",
    "safe_folder_name", "depth", "status", "item_count", "image_candidate_count",
    "nested_folder_at_depth_limit_count", "shortcut_count", "google_workspace_file_count",
    "other_file_count", "duplicate_name_candidate_count", "duplicate_content_candidate_count",
    "pages_read", "items", "warnings", "blocking_issues",
}


def folder(name, identifier):
    return drive_file(name, file_id=identifier, mime_type=FOLDER_MIME_TYPE, md5=None)


class NestedFolderManifestDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mapping_path = self.root / "mapping.json"
        self.sku_path = self.root / "sku.json"
        self.connect = self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("Real network forbidden")))
        self.create_connection = self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("Real network forbidden")))
        self._fixture()

    def tearDown(self):
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def _fixture(self, root_count=1, nested_per_root=1, *, root_extras=False):
        self.mapping, self.skus, self.root_ids, self.coordinates = local_reports(root_count)
        self.urls = [f"https://drive.google.com/drive/folders/{identifier}" for identifier in self.root_ids]
        self.responses = {}
        self.nested_ids = []
        self.child_ids = []
        names = ("Photos-", "Factory Photos-", "Videos-", "Factory Videos-", "Banner-")
        for root_index, root_id in enumerate(self.root_ids):
            children = []
            for index in range(nested_per_root):
                identifier = f"NESTED_FIXTURE_PRIVATE_{root_index}_{index}"
                child_id = f"IMAGE_FIXTURE_PRIVATE_{root_index}_{index}"
                self.nested_ids.append(identifier)
                self.child_ids.append(child_id)
                children.append(folder(f"{names[(root_index * nested_per_root + index) % len(names)]}{index}", identifier))
                self.responses[(identifier, None)] = {"files": [drive_file("photo.webp", file_id=child_id, mime_type="image/webp")]}
            if root_extras and root_index < 5:
                shortcut = drive_file("shortcut", file_id=f"ROOT_SHORTCUT_PRIVATE_{root_index}", mime_type=SHORTCUT_MIME_TYPE, md5=None)
                shortcut["shortcutDetails"] = {"targetId": "ROOT_SHORTCUT_TARGET_PRIVATE"}
                children.append(shortcut)
            if root_extras and root_index < 4:
                children.append(drive_file("notes.txt", file_id=f"ROOT_OTHER_PRIVATE_{root_index}", mime_type="text/plain", md5=None))
            self.responses[(root_id, None)] = {"files": children}
        self.active_settings = settings()

    def _prepare(self):
        self.mapping_path.write_text(json.dumps(self.mapping), encoding="utf-8")
        self.sku_path.write_text(json.dumps(self.skus), encoding="utf-8")
        self.factory = FakeCombinedFactory(sheet_response(self.coordinates, self.urls), self.responses)

    def _run(self):
        self._prepare()
        return dry_run.run_nested_drive_folder_manifest_dry_run(
            self.mapping_path, SHEET, self.sku_path,
            self.active_settings, self.factory, project_root=self.root,
        )

    def _args(self):
        return [COMMAND, "--mapping", str(self.mapping_path), "--sheet", SHEET, "--sku-report", str(self.sku_path)]

    def _set_nested_files(self, files):
        self.responses[(self.nested_ids[0], None)] = {"files": files}

    def _listed_ids(self):
        return [call["q"].split("'", maxsplit=2)[1] for call in self.factory.drive.file_resource.list_calls]

    def test_01_cli_is_registered(self):
        args = cli.build_parser().parse_args(self._args())
        self.assertEqual(args.command, COMMAND)

    def test_02_mapping_is_required(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args([COMMAND, "--sheet", SHEET, "--sku-report", "sku.json"])

    def test_03_sheet_is_required(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args([COMMAND, "--mapping", "mapping.json", "--sku-report", "sku.json"])

    def test_04_sku_report_is_required(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args([COMMAND, "--mapping", "mapping.json", "--sheet", SHEET])

    def test_05_cli_paths_and_sheet_are_preserved(self):
        args = cli.build_parser().parse_args(self._args())
        self.assertEqual(args.mapping_input_path, self.mapping_path)
        self.assertEqual(args.sku_report_input_path, self.sku_path)
        self.assertEqual(args.sheet_title, SHEET)

    def test_06_cli_dispatches_to_nested_handler(self):
        with patch.object(cli, "_run_build_nested_drive_folder_manifests", return_value=0) as handler:
            self.assertEqual(cli.main(self._args()), 0)
        handler.assert_called_once()

    def test_07_cli_uses_metadata_config_and_one_shared_factory(self):
        self._prepare()
        logger = MagicMock()
        with (
            patch.object(cli, "load_google_drive_metadata_config", return_value=self.active_settings) as loader,
            patch.object(cli, "load_google_config", side_effect=AssertionError("Full config forbidden")) as full_loader,
            patch.object(cli, "google_redactor_for_settings", return_value=Redactor()),
            patch.object(cli, "OfficialGoogleClientFactory", return_value=self.factory) as factory,
            patch.object(cli, "PROJECT_ROOT", self.root),
            patch.object(cli, "_configure_logging", return_value=logger),
        ):
            self.assertEqual(cli.main(self._args()), 0)
        loader.assert_called_once()
        full_loader.assert_not_called()
        factory.assert_called_once()
        self.assertEqual(self.factory.calls, 1)

    def test_08_cli_config_failure_stops_before_factory(self):
        with (
            patch.object(cli, "load_google_drive_metadata_config", side_effect=ConfigError("drive_metadata_scope_unavailable")),
            patch.object(cli, "OfficialGoogleClientFactory") as factory,
            patch.object(cli, "_configure_logging", return_value=MagicMock()),
        ):
            self.assertEqual(cli.main(self._args()), 2)
        factory.assert_not_called()

    def test_09_root_pipeline_helper_is_reused(self):
        with patch.object(dry_run, "read_root_drive_manifest_batch", wraps=root_dry_run.read_root_drive_manifest_batch) as reader:
            self._run()
        reader.assert_called_once()

    def test_10_secure_reader_is_reused_for_one_exact_cell_batch(self):
        with patch.object(dry_run, "SecureMediaReferenceReader", wraps=dry_run.SecureMediaReferenceReader) as reader:
            self._run()
        reader.assert_called_once()
        self.assertEqual(len(self.factory.values.batch_get_calls), 1)
        self.assertEqual(self.factory.values.batch_get_calls[0]["ranges"], [f"'{SHEET}'!{self.coordinates[0]}"])

    def test_11_fresh_discovery_helper_is_reused(self):
        with patch.object(root_dry_run, "discover_from_secure_read_result", wraps=root_dry_run.discover_from_secure_read_result) as discovery:
            self._run()
        discovery.assert_called_once()

    def test_12_exact_source_range_sku_join_is_reused(self):
        with patch.object(root_dry_run, "join_verified_sku", wraps=root_dry_run.join_verified_sku) as join:
            report, _ = self._run()
        join.assert_called_once()
        self.assertEqual(report["results"][0]["sku"], "CLM-ULTRA-MODEL0")
        self.assertEqual(report["results"][0]["product_source"], {"start_row": 10, "end_row": 15})

    def test_13_secure_root_handle_is_reused(self):
        with patch.object(root_dry_run, "create_secure_google_drive_folder_handle", wraps=root_dry_run.create_secure_google_drive_folder_handle) as creator:
            self._run()
        creator.assert_called_once()

    def test_14_root_core_is_reused(self):
        with patch.object(root_dry_run, "build_drive_folder_manifests_with_gateway", wraps=root_dry_run.build_drive_folder_manifests_with_gateway) as builder:
            self._run()
        builder.assert_called_once()

    def test_15_nested_core_is_called_once_for_all_handles(self):
        self._fixture(nested_per_root=3)
        with patch.object(dry_run, "build_nested_drive_folder_manifests_with_gateway", wraps=dry_run.build_nested_drive_folder_manifests_with_gateway) as builder:
            self._run()
        builder.assert_called_once()
        self.assertEqual(len(builder.call_args.args[0]), 3)

    def test_16_nested_handle_receives_actual_root_domain_item(self):
        with patch.object(dry_run, "create_secure_google_drive_nested_folder_handle", wraps=dry_run.create_secure_google_drive_nested_folder_handle) as creator:
            self._run()
        root, item = creator.call_args.args
        self.assertTrue(any(candidate is item for candidate in root.items))
        self.assertEqual(item.provider_file_id, self.nested_ids[0])
        self.assertNotEqual(item.provider_file_id, item.file_id_fingerprint)

    def test_17_memory_handoff_repr_hides_all_provider_ids(self):
        captured = []
        original = root_dry_run.read_root_drive_manifest_batch
        def capture(*args, **kwargs):
            value = original(*args, **kwargs)
            captured.append(value)
            return value
        with patch.object(dry_run, "read_root_drive_manifest_batch", side_effect=capture):
            self._run()
        for value in (*self.root_ids, *self.nested_ids):
            self.assertNotIn(value, repr(captured))

    def test_18_only_mapping_and_sku_json_are_loaded(self):
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as loader:
            self._run()
        self.assertEqual([call.args[0] for call in loader.call_args_list], [self.mapping_path, self.sku_path])

    def test_19_serialized_root_report_is_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as loader:
            with self.assertRaisesRegex(dry_run.GoogleDriveNestedFolderManifestDryRunError, "serialized_root_manifest_not_supported"):
                dry_run.run_nested_drive_folder_manifest_dry_run(
                    self.root / root_dry_run.REPORT_FILENAME, SHEET, self.sku_path,
                    settings(), MagicMock(), project_root=self.root,
                )
        loader.assert_not_called()

    def test_20_output_filename_and_saved_json(self):
        report, path = self._run()
        self.assertEqual(path, self.root / "reports" / dry_run.REPORT_FILENAME)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_21_eight_roots_twenty_four_nested_folders_fixture(self):
        self._fixture(8, 3, root_extras=True)
        self.assertEqual(sum(len(self.responses[(identifier, None)]["files"]) for identifier in self.root_ids), 33)
        report, _ = self._run()
        for key, value in {
            "root_folders_processed": 8, "total_nested_folders": 24,
            "nested_folders_listed": 24, "image_candidates": 24,
            "root_pages_read": 8, "nested_pages_read": 24,
            "sheets_read_requests_performed": 1, "root_drive_read_requests_performed": 8,
            "nested_drive_read_requests_performed": 24, "network_requests_performed": 33,
            "download_requests_performed": 0, "write_requests_performed": 0,
        }.items():
            self.assertEqual(report["summary"][key], value, key)
        self.assertEqual(self._listed_ids()[:8], self.root_ids)
        self.assertEqual(set(self._listed_ids()[8:]), set(self.nested_ids))

    def test_22_all_folder_names_are_included(self):
        self._fixture(nested_per_root=5)
        report, _ = self._run()
        self.assertEqual({item["safe_folder_name"] for item in report["results"]}, {
            "Photos-0", "Factory Photos-1", "Videos-2", "Factory Videos-3", "Banner-4",
        })

    def test_23_root_shortcuts_are_not_followed(self):
        self._fixture(8, 3, root_extras=True)
        report, _ = self._run()
        self.assertEqual(len(self._listed_ids()), 32)
        self.assertNotIn("ROOT_SHORTCUT_TARGET_PRIVATE", str(self.factory.drive.file_resource.list_calls))
        self.assertIn("shortcut_not_followed", report["warnings"])

    def test_24_nested_parent_query_is_direct_children_only(self):
        self._run()
        call = self.factory.drive.file_resource.list_calls[1]
        self.assertEqual(call["q"], f"'{self.nested_ids[0]}' in parents and trashed = false")
        self.assertEqual(call["pageSize"], 100)

    def test_25_single_page_counters_are_separate(self):
        report, _ = self._run()
        summary = report["summary"]
        self.assertEqual((summary["root_pages_read"], summary["nested_pages_read"]), (1, 1))
        self.assertEqual(summary["network_requests_performed"], 3)

    def test_26_root_pagination_is_not_double_counted(self):
        self.responses[(self.root_ids[0], None)]["nextPageToken"] = "ROOT_NEXT"
        self.responses[(self.root_ids[0], "ROOT_NEXT")] = {"files": []}
        report, _ = self._run()
        self.assertEqual(report["summary"]["root_pages_read"], 2)
        self.assertEqual(report["summary"]["root_drive_read_requests_performed"], 2)
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 1)
        self.assertEqual(report["summary"]["network_requests_performed"], 4)

    def test_27_nested_pagination_is_counted(self):
        self.responses[(self.nested_ids[0], None)]["nextPageToken"] = "NESTED_NEXT"
        self.responses[(self.nested_ids[0], "NESTED_NEXT")] = {"files": [drive_file("second.jpg", file_id="SECOND_PRIVATE_IMAGE")]}
        report, _ = self._run()
        self.assertEqual(report["results"][0]["item_count"], 2)
        self.assertEqual(report["summary"]["nested_pages_read"], 2)
        self.assertEqual(report["summary"]["network_requests_performed"], 4)

    def test_28_all_result_depths_are_one(self):
        self._fixture(nested_per_root=3)
        report, _ = self._run()
        self.assertTrue(all(item["depth"] == 1 for item in report["results"]))

    def test_29_deeper_folder_has_depth_limit_warning(self):
        self._set_nested_files([folder("Photos-deeper", "DEPTH_TWO_PRIVATE_FOLDER")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["nested_folders_at_depth_limit"], 1)
        self.assertEqual(report["results"][0]["nested_folder_at_depth_limit_count"], 1)
        self.assertIn("max_traversal_depth_reached", report["results"][0]["items"][0]["warnings"])

    def test_30_depth_two_folder_is_never_listed(self):
        self._set_nested_files([folder("deeper", "DEPTH_TWO_PRIVATE_FOLDER")])
        self.responses[("DEPTH_TWO_PRIVATE_FOLDER", None)] = AssertionError("Must not traverse depth two")
        self._run()
        self.assertEqual(self._listed_ids(), [self.root_ids[0], self.nested_ids[0]])

    def test_31_image_candidate_is_metadata_only(self):
        report, _ = self._run()
        item = report["results"][0]["items"][0]
        self.assertTrue(item["image_candidate"])
        self.assertEqual(item["item_kind"], "image_candidate")
        self.assertNotIn("verified_image", item)

    def test_32_nested_shortcut_is_not_followed(self):
        shortcut = drive_file("shortcut", mime_type=SHORTCUT_MIME_TYPE)
        shortcut["shortcutDetails"] = {"targetId": "NESTED_SHORTCUT_TARGET_PRIVATE"}
        self._set_nested_files([shortcut])
        report, _ = self._run()
        self.assertEqual(report["summary"]["shortcuts"], 1)
        self.assertIn("shortcut_not_followed", report["results"][0]["warnings"])
        self.assertNotIn("NESTED_SHORTCUT_TARGET_PRIVATE", str(self.factory.drive.file_resource.list_calls))

    def test_33_workspace_file_is_classified(self):
        self._set_nested_files([drive_file("doc", mime_type="application/vnd.google-apps.document")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["google_workspace_files"], 1)

    def test_34_video_stays_other_file(self):
        self._set_nested_files([drive_file("movie.mp4", mime_type="video/mp4")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["other_files"], 1)
        self.assertEqual(report["summary"]["image_candidates"], 0)

    def test_35_duplicate_name_candidates_are_counted(self):
        self._set_nested_files([drive_file("same.jpg", file_id="PRIVATE_A"), drive_file("SAME.JPG", file_id="PRIVATE_B")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["duplicate_name_candidates"], 2)

    def test_36_duplicate_content_candidates_are_counted(self):
        self._set_nested_files([drive_file("a.jpg", file_id="PRIVATE_A"), drive_file("b.jpg", file_id="PRIVATE_B")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["duplicate_content_candidates"], 2)

    def test_37_duplicates_are_not_deleted(self):
        self._set_nested_files([drive_file(), drive_file()])
        report, _ = self._run()
        self.assertEqual(report["results"][0]["item_count"], 2)

    def test_38_shared_nested_folder_keeps_each_product_manifest(self):
        self._fixture(2, 1)
        self.responses[(self.root_ids[1], None)] = {"files": [folder("Shared", self.nested_ids[0])]}
        report, _ = self._run()
        self.assertEqual(len(report["results"]), 2)
        self.assertEqual(self._listed_ids().count(self.nested_ids[0]), 2)
        self.assertTrue(all("shared_nested_folder_candidate" in item["warnings"] for item in report["results"]))
        self.assertEqual(len({item["sku"] for item in report["results"]}), 2)

    def test_39_empty_nested_folder(self):
        self._set_nested_files([])
        report, _ = self._run()
        self.assertEqual(report["summary"]["empty_nested_folders"], 1)
        self.assertEqual(report["results"][0]["status"], "empty_folder")

    def test_40_nested_401(self):
        self.responses[(self.nested_ids[0], None)] = FakeHttpError(401)
        report, _ = self._run()
        self.assertEqual(report["summary"]["nested_folders_access_denied"], 1)
        self.assertEqual(report["status"], "partial")

    def test_41_nested_403(self):
        self.responses[(self.nested_ids[0], None)] = FakeHttpError(403)
        report, _ = self._run()
        self.assertEqual(report["results"][0]["status"], "access_denied")

    def test_42_nested_404(self):
        self.responses[(self.nested_ids[0], None)] = FakeHttpError(404)
        report, _ = self._run()
        self.assertEqual(report["summary"]["nested_folders_missing_or_inaccessible"], 1)

    def test_43_nested_429_retries(self):
        self.responses[(self.nested_ids[0], None)] = [FakeHttpError(429), {"files": []}]
        report, _ = self._run()
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 2)
        self.assertEqual(report["summary"]["nested_pages_read"], 1)

    def test_44_nested_5xx_retries(self):
        self.responses[(self.nested_ids[0], None)] = [FakeHttpError(503), {"files": []}]
        report, _ = self._run()
        self.assertEqual(report["summary"]["network_requests_performed"], 4)

    def test_45_nested_retry_limit(self):
        self.responses[(self.nested_ids[0], None)] = FakeHttpError(503)
        report, _ = self._run()
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 3)
        self.assertEqual(report["summary"]["nested_folders_read_failed"], 1)

    def test_46_hundred_nested_folders_allowed(self):
        self._fixture(nested_per_root=100)
        report, _ = self._run()
        self.assertEqual(report["summary"]["total_nested_folders"], 100)
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 100)

    def test_47_over_hundred_blocks_before_any_nested_read(self):
        self._fixture(nested_per_root=101)
        with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "nested_folder_batch_limit_exceeded"):
            self._run()
        self.assertEqual(self._listed_ids(), self.root_ids)
        self.assertFalse((self.root / "reports" / dry_run.REPORT_FILENAME).exists())

    def test_48_deterministic_ordering(self):
        self._fixture(2, 3)
        first, _ = self._run()
        self.mapping["results"].reverse()
        for identifier in self.root_ids:
            self.responses[(identifier, None)]["files"].reverse()
        second, _ = self._run()
        self.assertEqual(first, second)

    def test_49_raw_ids_absent_from_report_and_saved_json(self):
        report, path = self._run()
        for output in (json.dumps(report), path.read_text(encoding="utf-8")):
            for identifier in (*self.root_ids, *self.nested_ids, *self.child_ids):
                self.assertNotIn(identifier, output)
            self.assertNotIn("provider_file_id", output)

    def test_50_urls_and_resource_keys_are_absent(self):
        self.urls[0] += "?resourcekey=RESOURCE_KEY_PRIVATE_FIXTURE"
        report, _ = self._run()
        for forbidden in ("https://", "drive.google.com", "RESOURCE_KEY_PRIVATE_FIXTURE", "resource_key"):
            self.assertNotIn(forbidden, json.dumps(report))

    def test_51_result_and_item_allowlists(self):
        report, _ = self._run()
        self.assertEqual(set(report["results"][0]), RESULT_FIELDS)
        self.assertEqual(set(report["results"][0]["items"][0]), ITEM_FIELDS)

    def test_52_no_get_media(self):
        self._run()
        self.assertEqual(self.factory.drive.file_resource.get_media_calls, 0)

    def test_53_no_alt_media_or_download_fields(self):
        self._run()
        for call in self.factory.drive.file_resource.list_calls:
            self.assertNotIn("alt", call)
            for forbidden in ("webContentLink", "thumbnailLink", "shortcutDetails"):
                self.assertNotIn(forbidden, call["fields"])

    def test_54_no_export_or_download(self):
        report, _ = self._run()
        self.assertEqual(self.factory.drive.file_resource.export_calls, 0)
        self.assertEqual(report["summary"]["download_requests_performed"], 0)

    def test_55_zero_external_writes(self):
        report, _ = self._run()
        self.assertEqual(self.factory.values.write_calls, 0)
        self.assertEqual(report["summary"]["write_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_56_network_is_mock_only(self):
        self._run()
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def test_57_inputs_are_immutable(self):
        before = copy.deepcopy((self.mapping, self.skus, self.responses))
        self._run()
        self.assertEqual((self.mapping, self.skus, self.responses), before)

    def test_58_stale_sku_snapshot_is_rejected_before_factory(self):
        self.skus["input_file"] = "reports/other-mock-products.json"
        with self.assertRaises(ValueError):
            self._run()
        self.assertEqual(self.factory.calls, 0)

    def test_59_missing_sku_blocks_drive_requests(self):
        self.skus["results"] = []
        report, _ = self._run()
        self.assertEqual(self._listed_ids(), [])
        self.assertEqual(report["summary"]["root_sources_blocked"], 1)
        self.assertEqual(report["summary"]["network_requests_performed"], 1)

    def test_60_ambiguous_sku_blocks_drive_requests(self):
        duplicate = copy.deepcopy(self.skus["results"][0])
        duplicate["sku"] = "CLM-ULTRA-OTHER"
        self.skus["results"].append(duplicate)
        report, _ = self._run()
        self.assertEqual(self._listed_ids(), [])
        self.assertIn("sku_join_ambiguous", report["blocking_issues"])

    def test_61_non_metadata_drive_scope_is_rejected(self):
        self.active_settings = settings(drive_scope=GOOGLE_DRIVE_READONLY_SCOPE)
        with self.assertRaises(DriveMetadataScopeUnavailable):
            self._run()
        self.assertEqual(self.factory.calls, 0)

    def test_62_invalid_sheets_scope_is_rejected(self):
        self.active_settings = settings(sheets_scope="https://www.googleapis.com/auth/spreadsheets")
        with self.assertRaises(DriveMetadataScopeUnavailable):
            self._run()
        self.assertEqual(self.factory.calls, 0)

    def test_63_empty_mapping_never_creates_clients(self):
        self.mapping["results"] = []
        report, _ = self._run()
        self.assertEqual(self.factory.calls, 0)
        self.assertEqual(report["summary"]["network_requests_performed"], 0)
        self.assertEqual(report["results"], [])

    def test_64_root_access_denied_is_visible_without_nested_reads(self):
        self.responses[(self.root_ids[0], None)] = FakeHttpError(403)
        report, _ = self._run()
        self.assertEqual(report["summary"]["root_folders_processed"], 1)
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 0)
        self.assertEqual(report["root_issues"][0]["status"], "access_denied")
        self.assertEqual(report["status"], "partial")

    def test_65_incomplete_root_is_not_traversed(self):
        self.responses[(self.root_ids[0], None)] = {
            "files": [folder(f"Directory {i}", f"INCOMPLETE_PRIVATE_{i}") for i in range(1001)],
        }
        report, _ = self._run()
        self.assertEqual(report["root_issues"][0]["status"], "limit_exceeded")
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 0)

    def test_66_missing_nested_id_is_reported_not_guessed(self):
        self.responses[(self.root_ids[0], None)]["files"][0].pop("id")
        report, _ = self._run()
        self.assertEqual(report["summary"]["invalid_nested_folder_handles"], 1)
        self.assertEqual(report["summary"]["total_nested_folders"], 1)
        self.assertEqual(report["results"][0]["status"], "invalid_nested_folder_handle")
        self.assertEqual(self._listed_ids(), self.root_ids)

    def test_67_known_root_id_in_nested_metadata_fails_safe_report_scan(self):
        self._set_nested_files([drive_file(f"{self.root_ids[0]}.jpg", file_id="OTHER_PRIVATE_CHILD")])
        with self.assertRaisesRegex(root_dry_run.GoogleDriveFolderManifestDryRunError, "unsafe_drive_manifest_leak"):
            self._run()
        self.assertFalse((self.root / "reports" / dry_run.REPORT_FILENAME).exists())

    def test_68_no_image_selection_or_role_policy(self):
        report, _ = self._run()
        for name in ("folder_role", "main_image", "gallery", "images", "selected_image"):
            self.assertNotIn(f'"{name}"', json.dumps(report))

    def test_69_root_report_is_not_written(self):
        self._run()
        self.assertFalse((self.root / "reports" / root_dry_run.REPORT_FILENAME).exists())

    def test_70_cli_logs_only_safe_summary(self):
        self._prepare()
        logger = MagicMock()
        with (
            patch.object(cli, "load_google_drive_metadata_config", return_value=self.active_settings),
            patch.object(cli, "google_redactor_for_settings", return_value=Redactor()),
            patch.object(cli, "OfficialGoogleClientFactory", return_value=self.factory),
            patch.object(cli, "PROJECT_ROOT", self.root),
        ):
            self.assertEqual(cli._run_build_nested_drive_folder_manifests(logger, self.mapping_path, SHEET, self.sku_path), 0)
        payload = json.loads(logger.info.call_args.args[0])
        self.assertEqual(payload["summary"]["network_requests_performed"], 3)
        output = str(logger.mock_calls)
        for forbidden in (*self.root_ids, *self.nested_ids, *self.child_ids, "https://", "Authorization", "Cookie"):
            self.assertNotIn(forbidden, output)

    def test_71_report_builder_rejects_serialized_root_results(self):
        with self.assertRaisesRegex(dry_run.GoogleDriveNestedFolderManifestDryRunError, "fresh_root_manifest_read_required"):
            dry_run.build_nested_drive_folder_manifest_report(
                {"status": "ok", "results": []}, mapping_input_file="mapping.json",
                sheet_title=SHEET, sku_report_input_file="sku.json",
                sheets_read_requests_performed=0, gateway=MagicMock(),
            )

    def test_72_empty_root_has_zero_nested_reads(self):
        self.responses[(self.root_ids[0], None)] = {"files": []}
        report, _ = self._run()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["summary"]["root_folders_processed"], 1)
        self.assertEqual(report["summary"]["total_nested_folders"], 0)
        self.assertEqual(report["summary"]["network_requests_performed"], 2)

    def test_73_root_and_nested_retries_are_counted_separately(self):
        root_response = self.responses[(self.root_ids[0], None)]
        self.responses[(self.root_ids[0], None)] = [FakeHttpError(429), root_response]
        self.responses[(self.nested_ids[0], None)] = [FakeHttpError(503), {"files": []}]
        report, _ = self._run()
        summary = report["summary"]
        self.assertEqual(summary["root_drive_read_requests_performed"], 2)
        self.assertEqual(summary["nested_drive_read_requests_performed"], 2)
        self.assertEqual(summary["root_pages_read"], 1)
        self.assertEqual(summary["nested_pages_read"], 1)
        self.assertEqual(summary["network_requests_performed"], 5)


if __name__ == "__main__":
    unittest.main()
