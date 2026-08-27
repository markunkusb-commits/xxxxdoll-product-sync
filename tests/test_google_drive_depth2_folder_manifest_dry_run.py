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
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import google_drive_depth2_folder_manifest_dry_run as dry_run
from sync_worker import google_drive_folder_manifest_dry_run as root_dry_run
from sync_worker import google_drive_nested_folder_manifest_dry_run as nested_dry_run
from sync_worker.config import ConfigError, GOOGLE_DRIVE_READONLY_SCOPE
from sync_worker.google_drive_depth2_folder_manifest import GoogleDriveDepth2FolderManifestError
from sync_worker.google_drive_folder_manifest import (
    DriveMetadataScopeUnavailable, FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE,
)
from sync_worker.sanitization import Redactor
from tests.test_google_drive_depth2_folder_manifest import FOLDER_NAMES
from tests.test_google_drive_folder_manifest import FakeHttpError, MD5, drive_file
from tests.test_google_drive_folder_manifest_dry_run import (
    FakeCombinedFactory, local_reports, settings, sheet_response,
)
from tests.test_secure_media_reference_read import SHEET


COMMAND = "build-depth2-drive-folder-manifests"
ITEM_FIELDS = {
    "safe_name", "mime_type", "size_bytes", "modified_time", "provider_content_checksum",
    "file_id_fingerprint", "item_kind", "image_candidate", "image_candidate_status",
    "image_width", "image_height", "warnings",
}
RESULT_FIELDS = {
    "sku", "product_source", "root_folder_id_fingerprint", "depth1_folder_id_fingerprint",
    "depth2_folder_id_fingerprint", "depth1_safe_folder_name", "depth2_safe_folder_name",
    "depth", "status", "item_count", "image_candidate_count", "nested_folder_at_depth_limit_count",
    "shortcut_count", "google_workspace_file_count", "other_file_count",
    "duplicate_name_candidate_count", "duplicate_content_candidate_count", "pages_read",
    "items", "warnings", "blocking_issues",
}


def folder(name, identifier):
    return drive_file(name, file_id=identifier, mime_type=FOLDER_MIME_TYPE, md5=None)


class Depth2FolderManifestDryRunTests(unittest.TestCase):
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

    def _fixture(self, roots=1, nested_per_root=1, targets=1):
        self.mapping, self.skus, self.root_ids, self.coordinates = local_reports(roots)
        self.urls = [f"https://drive.google.com/drive/folders/{identifier}" for identifier in self.root_ids]
        self.responses = {}
        self.depth1_ids = []
        self.depth2_ids = [f"DEPTH2_PRIVATE_FIXTURE_{i:03d}" for i in range(targets)]
        self.child_ids = [f"DEPTH2_CHILD_PRIVATE_FIXTURE_{i:03d}" for i in range(targets)]
        for root_index, root_id in enumerate(self.root_ids):
            children = []
            for index in range(nested_per_root):
                identifier = f"DEPTH1_PRIVATE_FIXTURE_{root_index}_{index}"
                self.depth1_ids.append(identifier)
                children.append(folder(f"Mock Parent {root_index}-{index}", identifier))
                self.responses[(identifier, None)] = {"files": []}
            self.responses[(root_id, None)] = {"files": children}
        self.responses[(self.depth1_ids[0], None)] = {"files": [
            folder(FOLDER_NAMES[i % len(FOLDER_NAMES)], identifier)
            for i, identifier in enumerate(self.depth2_ids)
        ]}
        for identifier, child_id in zip(self.depth2_ids, self.child_ids):
            self.responses[(identifier, None)] = {"files": [drive_file("photo.jpg", file_id=child_id)]}
        self.active_settings = settings()

    def _prepare(self):
        self.mapping_path.write_text(json.dumps(self.mapping), encoding="utf-8")
        self.sku_path.write_text(json.dumps(self.skus), encoding="utf-8")
        self.factory = FakeCombinedFactory(sheet_response(self.coordinates, self.urls), self.responses)

    def _run(self):
        self._prepare()
        return dry_run.run_depth2_drive_folder_manifest_dry_run(
            self.mapping_path, SHEET, self.sku_path, self.active_settings, self.factory,
            project_root=self.root,
        )

    def _args(self):
        return [COMMAND, "--mapping", str(self.mapping_path), "--sheet", SHEET, "--sku-report", str(self.sku_path)]

    def _cli(self):
        self._prepare()
        logger = MagicMock()
        with (
            patch.object(cli, "load_google_drive_metadata_config", return_value=self.active_settings) as loader,
            patch.object(cli, "load_google_config", side_effect=AssertionError("Full Google config forbidden")) as full_loader,
            patch.object(cli, "google_redactor_for_settings", return_value=Redactor()),
            patch.object(cli, "OfficialGoogleClientFactory", return_value=self.factory),
            patch.object(cli, "PROJECT_ROOT", self.root),
            patch.object(cli, "_configure_logging", return_value=logger),
        ):
            code = cli.main(self._args())
        loader.assert_called_once()
        full_loader.assert_not_called()
        return code, logger

    def _set_files(self, files):
        self.responses[(self.depth2_ids[0], None)] = {"files": files}

    def _listed_ids(self):
        return [call["q"].split("'", maxsplit=2)[1] for call in self.factory.drive.file_resource.list_calls]

    def test_01_cli_registered(self):
        args = cli.build_parser().parse_args(self._args())
        self.assertEqual(args.command, COMMAND)
        self.assertEqual(args.mapping_input_path, self.mapping_path)
        self.assertEqual(args.sku_report_input_path, self.sku_path)

    def test_02_mapping_required(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args([COMMAND, "--sheet", SHEET, "--sku-report", "sku.json"])

    def test_03_sheet_required(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args([COMMAND, "--mapping", "mapping.json", "--sku-report", "sku.json"])

    def test_04_sku_report_required(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args([COMMAND, "--mapping", "mapping.json", "--sheet", SHEET])

    def test_05_cli_dispatch(self):
        with patch.object(cli, "_run_build_depth2_drive_folder_manifests", return_value=0) as handler:
            self.assertEqual(cli.main(self._args()), 0)
        handler.assert_called_once()

    def test_06_cli_uses_metadata_loader_and_shared_client(self):
        code, _ = self._cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.factory.calls, 1)

    def test_07_config_failure_prevents_client_creation(self):
        with (
            patch.object(cli, "load_google_drive_metadata_config", side_effect=ConfigError("drive_metadata_scope_unavailable")),
            patch.object(cli, "OfficialGoogleClientFactory") as factory,
            patch.object(cli, "_configure_logging", return_value=MagicMock()),
        ):
            self.assertEqual(cli.main(self._args()), 2)
        factory.assert_not_called()

    def test_08_root_pipeline_helper_reused(self):
        with patch.object(dry_run, "read_root_drive_manifest_batch", wraps=root_dry_run.read_root_drive_manifest_batch) as reader:
            self._run()
        reader.assert_called_once()

    def test_09_nested_pipeline_helper_reused(self):
        with patch.object(dry_run, "read_nested_drive_manifest_batch", wraps=nested_dry_run.read_nested_drive_manifest_batch) as reader:
            self._run()
        reader.assert_called_once()

    def test_10_secure_reader_uses_one_exact_cell_batch(self):
        with patch.object(dry_run, "SecureMediaReferenceReader", wraps=dry_run.SecureMediaReferenceReader) as reader:
            self._run()
        reader.assert_called_once()
        self.assertEqual(len(self.factory.values.batch_get_calls), 1)
        self.assertEqual(self.factory.values.batch_get_calls[0]["ranges"], [f"'{SHEET}'!{self.coordinates[0]}"])

    def test_11_root_core_and_fresh_discovery_are_reused(self):
        with (
            patch.object(root_dry_run, "build_drive_folder_manifests_with_gateway", wraps=root_dry_run.build_drive_folder_manifests_with_gateway) as builder,
            patch.object(root_dry_run, "discover_from_secure_read_result", wraps=root_dry_run.discover_from_secure_read_result) as discovery,
            patch.object(root_dry_run, "create_secure_google_drive_folder_handle", wraps=root_dry_run.create_secure_google_drive_folder_handle) as creator,
        ):
            self._run()
        builder.assert_called_once()
        discovery.assert_called_once()
        creator.assert_called_once()

    def test_12_nested_core_is_reused(self):
        with patch.object(nested_dry_run, "build_nested_drive_folder_manifests_with_gateway", wraps=nested_dry_run.build_nested_drive_folder_manifests_with_gateway) as builder:
            self._run()
        builder.assert_called_once()

    def test_13_depth2_core_called_once_for_all_targets(self):
        self._fixture(targets=8)
        with patch.object(dry_run, "build_depth2_drive_folder_manifests_with_gateway", wraps=dry_run.build_depth2_drive_folder_manifests_with_gateway) as builder:
            self._run()
        builder.assert_called_once()
        self.assertEqual(len(builder.call_args.args[0]), 8)

    def test_14_handle_receives_actual_depth1_domain_item(self):
        with patch.object(dry_run, "create_secure_google_drive_depth2_folder_handle", wraps=dry_run.create_secure_google_drive_depth2_folder_handle) as creator:
            self._run()
        parent, item = creator.call_args.args
        self.assertEqual(parent.depth, 1)
        self.assertTrue(any(candidate is item for candidate in parent.items))
        self.assertEqual(item.provider_file_id, self.depth2_ids[0])
        self.assertIn("max_traversal_depth_reached", item.warnings)

    def test_15_unmarked_folder_is_not_a_target(self):
        original = dry_run.read_nested_drive_manifest_batch
        def remove_marker(*args, **kwargs):
            read = original(*args, **kwargs)
            parent = read.core_batch.manifests[0]
            item = replace(parent.items[0], warnings=())
            return replace(read, core_batch=replace(read.core_batch, manifests=(replace(parent, items=(item,)),)))
        with patch.object(dry_run, "read_nested_drive_manifest_batch", side_effect=remove_marker):
            report, _ = self._run()
        self.assertEqual(report["summary"]["total_depth2_folders"], 0)
        self.assertEqual(self._listed_ids(), [*self.root_ids, *self.depth1_ids])

    def test_16_non_folder_items_are_not_depth2_targets(self):
        self.responses[(self.depth1_ids[0], None)]["files"].extend([
            drive_file("Photos.jpg", file_id="UPSTREAM_IMAGE"),
            drive_file("Videos", file_id="UPSTREAM_VIDEO", mime_type="video/mp4"),
        ])
        report, _ = self._run()
        self.assertEqual(report["summary"]["total_depth2_folders"], 1)
        self.assertEqual(self._listed_ids(), [*self.root_ids, *self.depth1_ids, *self.depth2_ids])

    def test_17_eight_names_are_not_filtered(self):
        self._fixture(targets=8)
        report, _ = self._run()
        self.assertEqual({item["depth2_safe_folder_name"] for item in report["results"]}, set(FOLDER_NAMES))

    def test_18_handoff_repr_hides_raw_ids(self):
        captured = []
        original = dry_run.read_nested_drive_manifest_batch
        def capture(*args, **kwargs):
            read = original(*args, **kwargs)
            captured.append(read)
            return read
        with patch.object(dry_run, "read_nested_drive_manifest_batch", side_effect=capture):
            self._run()
        for value in (*self.root_ids, *self.depth1_ids, *self.depth2_ids):
            self.assertNotIn(value, repr(captured))

    def test_19_only_mapping_and_sku_reports_are_loaded(self):
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as loader:
            self._run()
        self.assertEqual([call.args[0] for call in loader.call_args_list], [self.mapping_path, self.sku_path])

    def test_20_old_root_report_is_rejected_before_open(self):
        with patch.object(dry_run, "load_local_json_report") as loader:
            with self.assertRaisesRegex(dry_run.GoogleDriveDepth2FolderManifestDryRunError, "serialized_drive_manifest_not_supported"):
                dry_run.run_depth2_drive_folder_manifest_dry_run(
                    self.root / root_dry_run.REPORT_FILENAME, SHEET, self.sku_path,
                    settings(), MagicMock(), project_root=self.root,
                )
        loader.assert_not_called()

    def test_21_old_nested_report_is_rejected_in_either_input(self):
        old = self.root / nested_dry_run.REPORT_FILENAME.upper()
        with patch.object(dry_run, "load_local_json_report") as loader:
            for mapping, sku in ((old, self.sku_path), (self.mapping_path, old)):
                with self.subTest(mapping=mapping.name), self.assertRaises(dry_run.GoogleDriveDepth2FolderManifestDryRunError):
                    dry_run.run_depth2_drive_folder_manifest_dry_run(mapping, SHEET, sku, settings(), MagicMock(), project_root=self.root)
        loader.assert_not_called()

    def test_22_report_filename_and_saved_content(self):
        report, path = self._run()
        self.assertEqual(path, self.root / "reports" / dry_run.REPORT_FILENAME)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_23_eight_root_twentyfour_nested_eight_target_accounting(self):
        self._fixture(roots=8, nested_per_root=3, targets=8)
        report, _ = self._run()
        for key, value in {
            "root_folders_processed": 8, "depth1_folders_processed": 24,
            "total_depth2_folders": 8, "depth2_folders_listed": 8,
            "root_pages_read": 8, "nested_pages_read": 24, "depth2_pages_read": 8,
            "sheets_read_requests_performed": 1, "root_drive_read_requests_performed": 8,
            "nested_drive_read_requests_performed": 24, "depth2_drive_read_requests_performed": 8,
            "network_requests_performed": 41, "download_requests_performed": 0, "write_requests_performed": 0,
        }.items():
            self.assertEqual(report["summary"][key], value, key)
        self.assertEqual(self._listed_ids()[:8], self.root_ids)
        self.assertEqual(set(self._listed_ids()[8:32]), set(self.depth1_ids))
        self.assertEqual(set(self._listed_ids()[32:]), set(self.depth2_ids))

    def test_24_zero_targets_still_reports_upstream_reads(self):
        self._fixture(targets=0)
        report, _ = self._run()
        self.assertEqual(report["results"], [])
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 0)
        self.assertEqual(report["summary"]["network_requests_performed"], 3)

    def test_25_empty_mapping_creates_no_clients(self):
        self.mapping["results"] = []
        report, _ = self._run()
        self.assertEqual(self.factory.calls, 0)
        self.assertEqual(report["summary"]["network_requests_performed"], 0)

    def test_26_fifty_targets_allowed(self):
        self._fixture(targets=50)
        report, _ = self._run()
        self.assertEqual(report["summary"]["total_depth2_folders"], 50)
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 50)

    def test_27_over_fifty_blocks_before_depth2_requests(self):
        self._fixture(targets=51)
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth2_folder_batch_limit_exceeded"):
            self._run()
        self.assertEqual(self._listed_ids(), [*self.root_ids, *self.depth1_ids])
        self.assertFalse((self.root / "reports" / dry_run.REPORT_FILENAME).exists())

    def test_28_root_pagination_counted_only_as_root_reads(self):
        self.responses[(self.root_ids[0], None)]["nextPageToken"] = "ROOT_NEXT"
        self.responses[(self.root_ids[0], "ROOT_NEXT")] = {"files": []}
        report, _ = self._run()
        summary = report["summary"]
        self.assertEqual(summary["root_pages_read"], 2)
        self.assertEqual(summary["root_drive_read_requests_performed"], 2)
        self.assertEqual(summary["nested_drive_read_requests_performed"], 1)
        self.assertEqual(summary["depth2_drive_read_requests_performed"], 1)
        self.assertEqual(summary["network_requests_performed"], 5)

    def test_29_nested_pagination_counted_only_as_nested_reads(self):
        self.responses[(self.depth1_ids[0], None)]["nextPageToken"] = "NESTED_NEXT"
        self.responses[(self.depth1_ids[0], "NESTED_NEXT")] = {"files": []}
        report, _ = self._run()
        self.assertEqual(report["summary"]["nested_pages_read"], 2)
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 2)
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 1)
        self.assertEqual(report["summary"]["network_requests_performed"], 5)

    def test_30_depth2_pagination_counted_only_as_depth2_reads(self):
        self.responses[(self.depth2_ids[0], None)]["nextPageToken"] = "DEPTH2_NEXT"
        self.responses[(self.depth2_ids[0], "DEPTH2_NEXT")] = {"files": [drive_file("second.jpg", file_id="SECOND_DEPTH2_CHILD")]}
        report, _ = self._run()
        self.assertEqual(report["results"][0]["item_count"], 2)
        self.assertEqual(report["summary"]["depth2_pages_read"], 2)
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 2)
        self.assertEqual(report["summary"]["network_requests_performed"], 5)

    def test_31_retries_at_all_levels_are_separate(self):
        for identifier in (self.root_ids[0], self.depth1_ids[0], self.depth2_ids[0]):
            self.responses[(identifier, None)] = [FakeHttpError(503), self.responses[(identifier, None)]]
        report, _ = self._run()
        summary = report["summary"]
        for prefix in ("root", "nested", "depth2"):
            self.assertEqual(summary[f"{prefix}_drive_read_requests_performed"], 2)
            self.assertEqual(summary[f"{prefix}_pages_read"], 1)
        self.assertEqual(summary["network_requests_performed"], 7)

    def test_32_result_depth_exactly_two(self):
        report, _ = self._run()
        self.assertEqual(report["results"][0]["depth"], 2)

    def test_33_depth3_folder_retained_with_depth_limit_warning(self):
        self._set_files([folder("Deeper", "DEPTH3_PRIVATE_FIXTURE")])
        report, _ = self._run()
        item = report["results"][0]["items"][0]
        self.assertEqual(item["item_kind"], "nested_folder")
        self.assertIn("max_traversal_depth_reached", item["warnings"])
        self.assertEqual(report["summary"]["nested_folders_at_depth_limit"], 1)

    def test_34_depth3_never_traversed(self):
        self._set_files([folder("Deeper", "DEPTH3_PRIVATE_FIXTURE")])
        self.responses[("DEPTH3_PRIVATE_FIXTURE", None)] = AssertionError("Depth three forbidden")
        self._run()
        self.assertEqual(self._listed_ids(), [*self.root_ids, *self.depth1_ids, *self.depth2_ids])

    def test_35_depth2_shortcut_retained_not_followed(self):
        shortcut = drive_file("shortcut", mime_type=SHORTCUT_MIME_TYPE)
        shortcut["shortcutDetails"] = {"targetId": "DEPTH2_SHORTCUT_TARGET_PRIVATE"}
        self._set_files([shortcut])
        report, _ = self._run()
        self.assertEqual(report["summary"]["shortcuts"], 1)
        self.assertIn("shortcut_not_followed", report["results"][0]["warnings"])
        self.assertNotIn("DEPTH2_SHORTCUT_TARGET_PRIVATE", str(self.factory.drive.file_resource.list_calls))

    def test_36_upstream_shortcuts_are_not_followed(self):
        for identifier in (self.root_ids[0], self.depth1_ids[0]):
            shortcut = drive_file("shortcut", file_id="UPSTREAM_SHORTCUT_PRIVATE", mime_type=SHORTCUT_MIME_TYPE)
            shortcut["shortcutDetails"] = {"targetId": "UPSTREAM_TARGET_PRIVATE"}
            self.responses[(identifier, None)]["files"].append(shortcut)
        report, _ = self._run()
        self.assertEqual(self._listed_ids(), [*self.root_ids, *self.depth1_ids, *self.depth2_ids])
        self.assertIn("shortcut_not_followed", report["warnings"])

    def test_37_jpeg_candidate_status(self):
        report, _ = self._run()
        item = report["results"][0]["items"][0]
        self.assertTrue(item["image_candidate"])
        self.assertEqual(item["image_candidate_status"], "drive_metadata_image_candidate")

    def test_38_psd_candidate_behavior_unchanged(self):
        self._set_files([drive_file("source.psd", mime_type="image/vnd.adobe.photoshop")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["image_candidates"], 1)
        self.assertEqual(report["results"][0]["items"][0]["image_candidate_status"], "drive_metadata_image_candidate")

    def test_39_video_is_other_file(self):
        self._set_files([drive_file("movie.mp4", mime_type="video/mp4")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["other_files"], 1)
        self.assertEqual(report["summary"]["image_candidates"], 0)

    def test_40_workspace_file(self):
        self._set_files([drive_file("document", mime_type="application/vnd.google-apps.document")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["google_workspace_files"], 1)

    def test_41_image_dimensions_preserved(self):
        self._set_files([drive_file(image_metadata={"width": 640, "height": 480})])
        report, _ = self._run()
        item = report["results"][0]["items"][0]
        self.assertEqual((item["image_width"], item["image_height"]), (640, 480))

    def test_42_checksum_preserved(self):
        self._set_files([drive_file(md5=MD5.upper())])
        report, _ = self._run()
        self.assertEqual(report["results"][0]["items"][0]["provider_content_checksum"], MD5)

    def test_43_duplicate_name_candidates(self):
        self._set_files([drive_file("same.jpg", file_id="PRIVATE_A"), drive_file("SAME.JPG", file_id="PRIVATE_B")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["duplicate_name_candidates"], 2)
        self.assertEqual(report["results"][0]["item_count"], 2)

    def test_44_duplicate_content_candidates(self):
        self._set_files([drive_file("a.jpg", file_id="PRIVATE_A"), drive_file("b.jpg", file_id="PRIVATE_B")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["duplicate_content_candidates"], 2)

    def test_45_empty_depth2_folder(self):
        self._set_files([])
        report, _ = self._run()
        self.assertEqual(report["results"][0]["status"], "empty_folder")
        self.assertEqual(report["summary"]["empty_depth2_folders"], 1)

    def test_46_depth2_401(self):
        self.responses[(self.depth2_ids[0], None)] = FakeHttpError(401)
        report, _ = self._run()
        self.assertEqual(report["summary"]["depth2_folders_access_denied"], 1)
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 1)
        self.assertEqual(report["status"], "partial")

    def test_47_depth2_403(self):
        self.responses[(self.depth2_ids[0], None)] = FakeHttpError(403)
        report, _ = self._run()
        self.assertEqual(report["results"][0]["status"], "access_denied")

    def test_48_depth2_404(self):
        self.responses[(self.depth2_ids[0], None)] = FakeHttpError(404)
        report, _ = self._run()
        self.assertEqual(report["summary"]["depth2_folders_missing_or_inaccessible"], 1)

    def test_49_depth2_429_retry(self):
        self.responses[(self.depth2_ids[0], None)] = [FakeHttpError(429), {"files": []}]
        report, _ = self._run()
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 2)
        self.assertEqual(report["summary"]["depth2_pages_read"], 1)

    def test_50_depth2_5xx_retry(self):
        self.responses[(self.depth2_ids[0], None)] = [FakeHttpError(503), {"files": []}]
        report, _ = self._run()
        self.assertEqual(report["summary"]["network_requests_performed"], 5)

    def test_51_retry_bound_remains_three(self):
        self.responses[(self.depth2_ids[0], None)] = FakeHttpError(503)
        report, _ = self._run()
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 3)
        self.assertEqual(report["summary"]["depth2_folders_read_failed"], 1)

    def test_52_deterministic_result_order(self):
        self._fixture(roots=2, nested_per_root=2, targets=8)
        first, _ = self._run()
        self.mapping["results"].reverse()
        for response in self.responses.values():
            response["files"].reverse()
        second, _ = self._run()
        self.assertEqual(first, second)

    def test_53_raw_ids_absent_from_report_and_saved_json(self):
        report, path = self._run()
        for output in (json.dumps(report), path.read_text(encoding="utf-8")):
            for identifier in (*self.root_ids, *self.depth1_ids, *self.depth2_ids, *self.child_ids):
                self.assertNotIn(identifier, output)
            self.assertNotIn("provider_file_id", output)
            self.assertNotIn("raw_depth2_folder_id", output)

    def test_54_drive_urls_and_links_absent(self):
        item = drive_file()
        item.update({
            "webContentLink": "https://drive.google.com/private-download",
            "webViewLink": "https://drive.google.com/private-view",
            "thumbnailLink": "https://example.invalid/private-thumbnail",
        })
        self._set_files([item])
        report, _ = self._run()
        for forbidden in ("https://", "drive.google.com", "webContentLink", "webViewLink", "thumbnailLink"):
            self.assertNotIn(forbidden, json.dumps(report))

    def test_55_resource_keys_absent(self):
        self.urls[0] += "?resourcekey=ROOT_RESOURCE_KEY_PRIVATE"
        self.responses[(self.depth2_ids[0], None)]["files"][0]["resourceKey"] = "CHILD_RESOURCE_KEY_PRIVATE"
        report, _ = self._run()
        for forbidden in ("ROOT_RESOURCE_KEY_PRIVATE", "CHILD_RESOURCE_KEY_PRIVATE", "resource_key", "resourceKey"):
            self.assertNotIn(forbidden, json.dumps(report))

    def test_56_shortcut_target_absent_in_output(self):
        item = drive_file("shortcut", mime_type=SHORTCUT_MIME_TYPE)
        item["shortcutDetails"] = {"targetId": "SHORTCUT_TARGET_PRIVATE"}
        self._set_files([item])
        report, _ = self._run()
        self.assertNotIn("SHORTCUT_TARGET_PRIVATE", json.dumps(report))
        self.assertNotIn("shortcutDetails", json.dumps(report))

    def test_57_no_get_media(self):
        self._run()
        self.assertEqual(self.factory.drive.file_resource.get_media_calls, 0)

    def test_58_no_alt_media_or_content_fields(self):
        self._run()
        for call in self.factory.drive.file_resource.list_calls:
            self.assertNotIn("alt", call)
            for field in ("webContentLink", "webViewLink", "thumbnailLink", "shortcutDetails"):
                self.assertNotIn(field, call["fields"])

    def test_59_no_export(self):
        self._run()
        self.assertEqual(self.factory.drive.file_resource.export_calls, 0)

    def test_60_no_http_media_or_real_network(self):
        self._run()
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def test_61_download_requests_fixed_zero(self):
        report, _ = self._run()
        self.assertEqual(report["summary"]["download_requests_performed"], 0)

    def test_62_external_write_requests_fixed_zero(self):
        report, _ = self._run()
        self.assertEqual(self.factory.values.write_calls, 0)
        self.assertEqual(report["summary"]["write_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_63_current_sku_exact_source_range_join(self):
        with patch.object(root_dry_run, "join_verified_sku", wraps=root_dry_run.join_verified_sku) as join:
            report, _ = self._run()
        join.assert_called_once()
        self.assertEqual(report["results"][0]["sku"], "CLM-ULTRA-MODEL0")
        self.assertEqual(report["results"][0]["product_source"], {"start_row": 10, "end_row": 15})

    def test_64_missing_sku_blocks_drive_reads(self):
        self.skus["results"] = []
        report, _ = self._run()
        self.assertEqual(self._listed_ids(), [])
        self.assertEqual(report["summary"]["root_sources_blocked"], 1)
        self.assertEqual(report["summary"]["network_requests_performed"], 1)
        self.assertEqual(report["status"], "partial")

    def test_65_ambiguous_sku_blocks_drive_reads(self):
        duplicate = copy.deepcopy(self.skus["results"][0])
        duplicate["sku"] = "CLM-ULTRA-OTHER"
        self.skus["results"].append(duplicate)
        report, _ = self._run()
        self.assertEqual(self._listed_ids(), [])
        self.assertIn("sku_join_ambiguous", report["blocking_issues"])

    def test_66_stale_product_snapshot_blocks_before_factory(self):
        self.skus["input_file"] = "reports/other-mock-products.json"
        with self.assertRaises(ValueError):
            self._run()
        self.assertEqual(self.factory.calls, 0)

    def test_67_non_metadata_drive_scope_blocked_before_factory(self):
        self.active_settings = settings(drive_scope=GOOGLE_DRIVE_READONLY_SCOPE)
        with self.assertRaises(DriveMetadataScopeUnavailable):
            self._run()
        self.assertEqual(self.factory.calls, 0)

    def test_68_writable_sheets_scope_blocked_before_factory(self):
        self.active_settings = settings(sheets_scope="https://www.googleapis.com/auth/spreadsheets")
        with self.assertRaises(DriveMetadataScopeUnavailable):
            self._run()
        self.assertEqual(self.factory.calls, 0)

    def test_69_root_failure_is_visible_and_stops_descendants(self):
        self.responses[(self.root_ids[0], None)] = FakeHttpError(403)
        report, _ = self._run()
        self.assertEqual(report["root_issues"][0]["status"], "access_denied")
        self.assertEqual(report["summary"]["nested_drive_read_requests_performed"], 0)
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 0)
        self.assertEqual(report["status"], "partial")

    def test_70_incomplete_depth1_is_visible_and_not_traversed(self):
        self.responses[(self.depth1_ids[0], None)] = {"files": [
            folder(f"Mock {i}", f"INCOMPLETE_DEPTH2_{i:04d}") for i in range(1001)
        ]}
        report, _ = self._run()
        self.assertEqual(report["depth1_issues"][0]["status"], "limit_exceeded")
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 0)
        self.assertEqual(report["status"], "partial")

    def test_71_missing_memory_id_is_invalid_not_guessed(self):
        self.responses[(self.depth1_ids[0], None)]["files"][0].pop("id")
        report, _ = self._run()
        self.assertEqual(report["summary"]["invalid_depth2_folder_handles"], 1)
        self.assertEqual(report["summary"]["total_depth2_folders"], 1)
        self.assertEqual(report["results"][0]["status"], "invalid_depth2_folder_handle")
        self.assertEqual(report["summary"]["depth2_drive_read_requests_performed"], 0)

    def test_72_report_result_and_item_allowlists(self):
        report, _ = self._run()
        self.assertEqual(set(report["results"][0]), RESULT_FIELDS)
        self.assertEqual(set(report["results"][0]["items"][0]), ITEM_FIELDS)

    def test_73_safety_scan_blocks_known_root_id_leak_before_write(self):
        self._set_files([drive_file(f"{self.root_ids[0]}.jpg", file_id="OTHER_CHILD_PRIVATE")])
        with patch.object(dry_run, "SafeJsonReportWriter") as writer:
            with self.assertRaisesRegex(root_dry_run.GoogleDriveFolderManifestDryRunError, "unsafe_drive_manifest_leak"):
                self._run()
        writer.assert_not_called()
        self.assertFalse((self.root / "reports" / dry_run.REPORT_FILENAME).exists())

    def test_74_cli_logs_safe_summary_only(self):
        code, logger = self._cli()
        self.assertEqual(code, 0)
        event = json.loads(logger.info.call_args.args[0])
        self.assertEqual(event["event"], "depth2_drive_folder_manifest_dry_run_report_written")
        self.assertEqual(event["summary"]["network_requests_performed"], 4)
        for forbidden in (*self.root_ids, *self.depth1_ids, *self.depth2_ids, *self.child_ids, "https://", "Authorization", "Cookie"):
            self.assertNotIn(forbidden, str(logger.mock_calls))

    def test_75_provider_error_details_never_reach_cli_or_report(self):
        secret = f"{self.depth2_ids[0]} Authorization: PRIVATE Cookie: PRIVATE token=PRIVATE https://drive.google.com/private"
        self.responses[(self.depth2_ids[0], None)] = RuntimeError(secret)
        code, logger = self._cli()
        self.assertEqual(code, 1)
        output = str(logger.mock_calls) + (self.root / "reports" / dry_run.REPORT_FILENAME).read_text(encoding="utf-8")
        for forbidden in (self.depth2_ids[0], "Authorization", "Cookie", "PRIVATE", "https://"):
            self.assertNotIn(forbidden, output)

    def test_76_root_and_nested_reports_are_not_written(self):
        self._run()
        for name in (root_dry_run.REPORT_FILENAME, nested_dry_run.REPORT_FILENAME):
            self.assertFalse((self.root / "reports" / name).exists())

    def test_77_serialized_nested_report_is_not_a_domain_input(self):
        with self.assertRaisesRegex(dry_run.GoogleDriveDepth2FolderManifestDryRunError, "fresh_nested_manifest_read_required"):
            dry_run.build_depth2_drive_folder_manifest_report(
                {"results": []}, mapping_input_file="mapping.json", sheet_title=SHEET,
                sku_report_input_file="sku.json", sheets_read_requests_performed=0, gateway=MagicMock(),
            )

    def test_78_inputs_and_metadata_are_unchanged(self):
        before = copy.deepcopy((self.mapping, self.skus, self.responses))
        self._run()
        self.assertEqual((self.mapping, self.skus, self.responses), before)

    def test_79_unknown_folder_names_are_still_targets(self):
        self.responses[(self.depth1_ids[0], None)]["files"][0]["name"] = "Unclassified new supplier folder"
        report, _ = self._run()
        self.assertEqual(report["summary"]["total_depth2_folders"], 1)
        self.assertEqual(report["results"][0]["depth2_safe_folder_name"], "Unclassified new supplier folder")

    def test_80_no_folder_role_or_image_selection(self):
        self._set_files([drive_file("main.jpg"), drive_file("gallery.jpg", file_id="OTHER_IMAGE")])
        report, _ = self._run()
        self.assertEqual(report["summary"]["image_candidates"], 2)
        for name in ("folder_role", "main_image", "gallery_order", "selected_image", "storefront"):
            self.assertNotIn(f'"{name}"', json.dumps(report))

    def test_81_wrong_depth1_domain_depth_blocks_before_depth2_reads(self):
        original = dry_run.read_nested_drive_manifest_batch
        def invalid_depth(*args, **kwargs):
            read = original(*args, **kwargs)
            parent = replace(read.core_batch.manifests[0], depth=2)
            return replace(read, core_batch=replace(read.core_batch, manifests=(parent,)))
        with patch.object(dry_run, "read_nested_drive_manifest_batch", side_effect=invalid_depth):
            with self.assertRaisesRegex(dry_run.GoogleDriveDepth2FolderManifestDryRunError, "invalid_depth1_manifest_depth"):
                self._run()
        self.assertEqual(self._listed_ids(), [*self.root_ids, *self.depth1_ids])

    def test_82_invalid_output_depth_blocks_report_write(self):
        original = dry_run.build_depth2_drive_folder_manifests_with_gateway
        def invalid_depth(*args, **kwargs):
            batch = original(*args, **kwargs)
            return replace(batch, manifests=(replace(batch.manifests[0], depth=3),))
        with (
            patch.object(dry_run, "build_depth2_drive_folder_manifests_with_gateway", side_effect=invalid_depth),
            patch.object(dry_run, "SafeJsonReportWriter") as writer,
        ):
            with self.assertRaisesRegex(dry_run.GoogleDriveDepth2FolderManifestDryRunError, "invalid_depth2_manifest_depth"):
                self._run()
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
