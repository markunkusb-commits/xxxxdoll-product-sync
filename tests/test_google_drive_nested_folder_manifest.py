from __future__ import annotations

import copy
import json
import socket
import sys
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import google_drive_folder_manifest as root_core
from sync_worker.google_api import DRIVE_FOLDER_MANIFEST_FIELDS, GoogleDriveMetadataGateway
from sync_worker.google_drive_nested_folder_manifest import (
    GoogleDriveNestedFolderManifestError,
    MAX_NESTED_FOLDERS_PER_RUN,
    MAX_TRAVERSAL_DEPTH,
    SecureGoogleDriveNestedFolderHandle,
    build_nested_drive_folder_manifests_with_gateway,
    create_secure_google_drive_nested_folder_handle,
)
from sync_worker.image_mapping import ProductSourceRange
from tests.test_google_drive_folder_manifest import (
    FakeGateway, FakeHttpError, MD5, drive_file, handle,
)


ROOT_ID = "MOCK_ROOT_PRIVATE_123456"
NESTED_ID = "MOCK_NESTED_PRIVATE_123456"
CHILD_ID = "MOCK_CHILD_PRIVATE_123456"
DEPTH_TWO_ID = "MOCK_DEPTH_TWO_PRIVATE_123456"
SHORTCUT_TARGET_ID = "MOCK_SHORTCUT_TARGET_PRIVATE_123456"


def folder(name="Photos-FIXTURE", folder_id=NESTED_ID):
    return drive_file(name, file_id=folder_id, mime_type=root_core.FOLDER_MIME_TYPE, md5=None)


def root_manifest(files=None, *, root_id=ROOT_ID, sku="CLM-ULTRA-FIXTURE", row=10):
    root_handle = replace(handle(folder_id=root_id, start_row=row, end_row=row + 5), sku=sku)
    gateway = FakeGateway({None: {"files": files if files is not None else [folder()]}})
    return root_core.build_drive_folder_manifests_with_gateway((root_handle,), gateway).manifests[0]


def nested_handles(root):
    return tuple(
        create_secure_google_drive_nested_folder_handle(root, item)
        for item in root.items if item.item_kind == "nested_folder"
    )


def mocked_gateway(responses=None):
    # The actual metadata gateway is used, but every Google service/request is fake.
    active = {
        key: deque(value) if isinstance(value, list) else value
        for key, value in (responses or {}).items()
    }
    drive = MagicMock(name="mock_drive_service")

    def list_request(**kwargs):
        folder_id = kwargs["q"].split("'", maxsplit=2)[1]
        value = active.get((folder_id, kwargs.get("pageToken")), {"files": []})
        if isinstance(value, deque):
            if not value:
                raise AssertionError("Unexpected extra mock listing")
            value = value.popleft()
        request = MagicMock(name="mock_metadata_request")
        if isinstance(value, BaseException):
            request.execute.side_effect = value
        else:
            request.execute.return_value = value
        return request

    drive.files.return_value.list.side_effect = list_request
    return GoogleDriveMetadataGateway(drive), drive


def built(files=None, *, handles=None, responses=None, **limits):
    active_handles = nested_handles(root_manifest()) if handles is None else handles
    gateway, drive = mocked_gateway(
        responses if responses is not None else {(NESTED_ID, None): {"files": files or []}}
    )
    result = build_nested_drive_folder_manifests_with_gateway(active_handles, gateway, **limits)
    return result, gateway, drive


def serialized(result):
    return json.dumps(result.to_report_dict(), sort_keys=True)


class GoogleDriveNestedFolderManifestTests(unittest.TestCase):
    def setUp(self):
        self.connect = self.enterContext(patch.object(
            socket.socket, "connect", side_effect=AssertionError("Real network forbidden"),
        ))
        self.create_connection = self.enterContext(patch.object(
            socket, "create_connection", side_effect=AssertionError("Real network forbidden"),
        ))
        self.factory = self.enterContext(patch(
            "sync_worker.google_api.OfficialGoogleClientFactory.create_drive_metadata",
            side_effect=AssertionError("Core must not create credentials or clients"),
        ))

    def tearDown(self):
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()
        self.factory.assert_not_called()

    def test_01_root_item_retains_memory_only_child_id(self):
        self.assertEqual(root_manifest().items[0].provider_file_id, NESTED_ID)

    def test_02_raw_child_id_is_hidden_from_repr(self):
        root = root_manifest()
        self.assertNotIn(NESTED_ID, repr(root.items[0]))
        self.assertNotIn(NESTED_ID, repr(root))

    def test_03_raw_child_id_is_not_serialized(self):
        root = root_manifest()
        self.assertNotIn("provider_file_id", root.items[0].to_dict())
        self.assertNotIn(NESTED_ID, json.dumps(root.to_dict()))

    def test_04_handle_is_created_from_root_domain_item(self):
        root = root_manifest()
        result = create_secure_google_drive_nested_folder_handle(root, root.items[0])
        self.assertEqual(result.raw_nested_folder_id, NESTED_ID)
        self.assertEqual(result.root_folder_id_fingerprint, root.folder_id_fingerprint)
        self.assertEqual(result.sku, root.sku)
        self.assertEqual(result.product_source, root.product_source)

    def test_05_only_nested_folder_items_are_accepted(self):
        root = root_manifest([folder(), drive_file(file_id=CHILD_ID)])
        self.assertEqual(len(nested_handles(root)), 1)

    def test_06_shortcut_cannot_be_a_nested_handle(self):
        root = root_manifest([drive_file(mime_type=root_core.SHORTCUT_MIME_TYPE)])
        with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "invalid_nested_folder_handle"):
            create_secure_google_drive_nested_folder_handle(root, root.items[0])

    def test_07_image_cannot_be_a_nested_handle(self):
        root = root_manifest([drive_file()])
        with self.assertRaises(GoogleDriveNestedFolderManifestError):
            create_secure_google_drive_nested_folder_handle(root, root.items[0])

    def test_08_other_file_cannot_be_a_nested_handle(self):
        root = root_manifest([drive_file(mime_type="video/mp4")])
        with self.assertRaises(GoogleDriveNestedFolderManifestError):
            create_secure_google_drive_nested_folder_handle(root, root.items[0])

    def test_09_depth_is_exactly_one(self):
        handle_value = nested_handles(root_manifest())[0]
        result, _, _ = built(handles=(handle_value,))
        self.assertEqual(handle_value.depth, 1)
        self.assertEqual(result.manifests[0].depth, 1)
        self.assertEqual(MAX_TRAVERSAL_DEPTH, 1)

    def test_10_invalid_depth_is_blocked_without_listing(self):
        for depth in (0, 2, -1, True, "1"):
            with self.subTest(depth=depth):
                invalid = replace(nested_handles(root_manifest())[0], depth=depth)
                result, gateway, drive = built(handles=(invalid,))
                self.assertEqual(result.manifests[0].status, "invalid_nested_folder_handle")
                self.assertEqual(result.summary.invalid_nested_folder_handles, 1)
                self.assertEqual(gateway.counters.read_requests_performed, 0)
                drive.files.assert_not_called()

    def test_11_one_nested_folder_listing(self):
        result, _, drive = built([drive_file(file_id=CHILD_ID)])
        self.assertEqual(result.summary.nested_folders_listed, 1)
        self.assertEqual(result.summary.total_nested_items, 1)
        drive.files.return_value.list.assert_called_once()

    def test_12_multiple_nested_folders_are_independent(self):
        root = root_manifest([folder("Photos", "MOCK_PHOTOS"), folder("Videos", "MOCK_VIDEOS")])
        result, _, drive = built(handles=nested_handles(root))
        self.assertEqual(result.summary.total_nested_folders, 2)
        self.assertEqual(drive.files.return_value.list.call_count, 2)

    def test_13_twenty_four_folder_fixture_lists_all_names(self):
        names = ("Photos-", "Factory Photos-", "Banner-", "Videos-", "Factory Videos-")
        handles = []
        for product in range(8):
            files = [folder(
                f"{names[(product * 3 + index) % len(names)]}{product}-{index}",
                f"MOCK_NESTED_{product}_{index}",
            ) for index in range(3)]
            handles.extend(nested_handles(root_manifest(
                files, root_id=f"MOCK_ROOT_{product}", sku=f"CLM-ULTRA-FIXTURE{product}", row=product * 10 + 1,
            )))
        responses = {(item.raw_nested_folder_id, None): {"files": [drive_file()]} for item in handles}
        result, _, drive = built(handles=handles, responses=responses)
        self.assertEqual(result.summary.total_nested_folders, 24)
        self.assertEqual(result.summary.nested_folders_listed, 24)
        self.assertEqual(result.summary.image_candidates, 24)
        self.assertEqual(result.summary.drive_read_requests_performed, 24)
        self.assertEqual(drive.files.return_value.list.call_count, 24)
        for name in names:
            self.assertTrue(any(item.safe_folder_name.startswith(name) for item in result.manifests))

    def test_14_root_listing_core_is_reused(self):
        handles = nested_handles(root_manifest())
        with patch.object(root_core, "build_drive_folder_manifests_with_gateway", wraps=root_core.build_drive_folder_manifests_with_gateway) as builder:
            built(handles=handles)
        builder.assert_called_once()
        self.assertEqual(builder.call_args.args[0][0].raw_folder_id, NESTED_ID)
        self.assertEqual(builder.call_args.kwargs, {})

    def test_15_parent_query_uses_raw_nested_id_only_in_request(self):
        _, _, drive = built()
        call = drive.files.return_value.list.call_args.kwargs
        self.assertEqual(call["q"], f"'{NESTED_ID}' in parents and trashed = false")
        self.assertNotIn(ROOT_ID, call["q"])

    def test_16_only_non_trashed_direct_children_are_requested(self):
        _, _, drive = built()
        call = drive.files.return_value.list.call_args.kwargs
        self.assertIn("and trashed = false", call["q"])
        self.assertEqual(call["fields"], DRIVE_FOLDER_MANIFEST_FIELDS)
        self.assertEqual(call["pageSize"], 100)

    def test_17_pagination_uses_root_policy(self):
        result, _, drive = built(responses={
            (NESTED_ID, None): {"files": [drive_file("a.jpg", file_id="MOCK_IMAGE_A")], "nextPageToken": "NEXT"},
            (NESTED_ID, "NEXT"): {"files": [drive_file("b.jpg", file_id="MOCK_IMAGE_B")]},
        })
        self.assertEqual(result.summary.pages_read, 2)
        self.assertEqual(result.summary.total_nested_items, 2)
        self.assertEqual(drive.files.return_value.list.call_args_list[1].kwargs["pageToken"], "NEXT")

    def test_18_duplicate_page_token_stops_listing(self):
        result, _, drive = built(responses={
            (NESTED_ID, None): {"files": [], "nextPageToken": "SAME"},
            (NESTED_ID, "SAME"): {"files": [], "nextPageToken": "SAME"},
        })
        self.assertEqual(result.manifests[0].status, "limit_exceeded")
        self.assertIn("duplicate_drive_page_token", result.manifests[0].blocking_issues)
        self.assertEqual(drive.files.return_value.list.call_count, 2)

    def test_19_max_pages_remains_twenty(self):
        responses = {
            (NESTED_ID, None if index == 0 else f"PAGE{index}"):
                {"files": [], "nextPageToken": f"PAGE{index + 1}"}
            for index in range(21)
        }
        result, _, drive = built(responses=responses)
        self.assertEqual(result.summary.pages_read, 20)
        self.assertEqual(result.summary.nested_folders_limit_exceeded, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 20)

    def test_20_max_items_remains_one_thousand(self):
        result, _, _ = built([drive_file(f"{i}.jpg", file_id=f"MOCK_IMAGE_{i}") for i in range(1001)])
        self.assertEqual(result.summary.total_nested_items, 1000)
        self.assertEqual(result.manifests[0].status, "limit_exceeded")

    def test_21_image_candidate_is_metadata_only(self):
        result, _, _ = built([drive_file(file_id=CHILD_ID, mime_type="image/webp")])
        item = result.manifests[0].items[0]
        self.assertEqual(item.item_kind, "image_candidate")
        self.assertEqual(item.image_candidate_status, "drive_metadata_image_candidate")
        self.assertTrue(item.image_candidate)

    def test_22_deeper_folder_stays_classified_as_nested_folder(self):
        result, _, _ = built([folder("deeper", DEPTH_TWO_ID)])
        self.assertEqual(result.manifests[0].items[0].item_kind, "nested_folder")
        self.assertEqual(result.summary.nested_folders_at_depth_limit, 1)

    def test_23_depth_limit_warning_is_on_item_and_manifest(self):
        result, _, _ = built([folder("deeper", DEPTH_TWO_ID)])
        self.assertIn("max_traversal_depth_reached", result.manifests[0].warnings)
        self.assertIn("max_traversal_depth_reached", result.manifests[0].items[0].warnings)

    def test_24_depth_two_folder_is_not_listed(self):
        _, _, drive = built([folder("Photos-deeper", DEPTH_TWO_ID)])
        self.assertEqual(drive.files.return_value.list.call_count, 1)
        self.assertNotIn(DEPTH_TWO_ID, str(drive.files.return_value.list.call_args_list))

    def test_25_shortcut_remains_shortcut(self):
        result, _, _ = built([drive_file(mime_type=root_core.SHORTCUT_MIME_TYPE)])
        self.assertEqual(result.manifests[0].items[0].item_kind, "shortcut")
        self.assertEqual(result.summary.shortcuts, 1)

    def test_26_shortcut_is_not_followed(self):
        shortcut = drive_file(mime_type=root_core.SHORTCUT_MIME_TYPE)
        shortcut["shortcutDetails"] = {"targetId": SHORTCUT_TARGET_ID}
        result, _, drive = built([shortcut])
        self.assertIn("shortcut_not_followed", result.manifests[0].warnings)
        self.assertIn("shortcut_not_followed", result.manifests[0].items[0].warnings)
        self.assertEqual(drive.files.return_value.list.call_count, 1)
        drive.files.return_value.get.assert_not_called()

    def test_27_workspace_file_classification_is_reused(self):
        result, _, _ = built([drive_file(mime_type="application/vnd.google-apps.document")])
        self.assertEqual(result.summary.google_workspace_files, 1)

    def test_28_video_remains_other_file(self):
        result, _, _ = built([drive_file("film.mp4", mime_type="video/mp4")])
        self.assertEqual(result.summary.other_files, 1)
        self.assertEqual(result.summary.image_candidates, 0)

    def test_29_image_dimensions_are_preserved(self):
        result, _, _ = built([drive_file(image_metadata={"width": 640, "height": 480, "rotation": 90})])
        item = result.manifests[0].items[0]
        self.assertEqual((item.image_width, item.image_height, item.image_rotation), (640, 480, 90))

    def test_30_md5_is_preserved(self):
        result, _, _ = built([drive_file(md5=MD5.upper())])
        self.assertEqual(result.manifests[0].items[0].md5_checksum, MD5)

    def test_31_duplicate_names_are_marked(self):
        result, _, _ = built([drive_file("same.jpg", file_id="MOCK_A"), drive_file("SAME.JPG", file_id="MOCK_B")])
        self.assertEqual(result.summary.duplicate_name_candidates, 2)
        self.assertTrue(all("duplicate_name_candidate" in item.warnings for item in result.manifests[0].items))

    def test_32_duplicate_content_is_marked(self):
        result, _, _ = built([drive_file("a.jpg", file_id="MOCK_A"), drive_file("b.jpg", file_id="MOCK_B")])
        self.assertEqual(result.summary.duplicate_content_candidates, 2)

    def test_33_duplicates_are_not_removed(self):
        result, _, _ = built([drive_file(), drive_file()])
        self.assertEqual(len(result.manifests[0].items), 2)

    def test_34_empty_nested_folder(self):
        result, _, _ = built()
        self.assertEqual(result.manifests[0].status, "empty_folder")
        self.assertEqual(result.summary.empty_nested_folders, 1)

    def test_35_401_is_access_denied_without_retry(self):
        result, _, drive = built(responses={(NESTED_ID, None): FakeHttpError(401)})
        self.assertEqual(result.summary.nested_folders_access_denied, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_36_403_is_access_denied_without_retry(self):
        result, _, drive = built(responses={(NESTED_ID, None): FakeHttpError(403)})
        self.assertEqual(result.manifests[0].status, "access_denied")
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_37_404_is_missing_without_retry(self):
        result, _, drive = built(responses={(NESTED_ID, None): FakeHttpError(404)})
        self.assertEqual(result.summary.nested_folders_missing_or_inaccessible, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_38_429_retries_using_root_policy(self):
        result, _, _ = built(responses={(NESTED_ID, None): [FakeHttpError(429), {"files": []}]})
        self.assertEqual(result.summary.drive_read_requests_performed, 2)
        self.assertEqual(result.summary.empty_nested_folders, 1)

    def test_39_5xx_retries_using_root_policy(self):
        result, _, _ = built(responses={(NESTED_ID, None): [FakeHttpError(503), {"files": []}]})
        self.assertEqual(result.summary.drive_read_requests_performed, 2)

    def test_40_retry_limit_remains_three_attempts(self):
        result, _, drive = built(responses={(NESTED_ID, None): FakeHttpError(503)})
        self.assertEqual(drive.files.return_value.list.call_count, 3)
        self.assertEqual(result.summary.nested_folders_read_failed, 1)
        self.assertIn("drive_metadata_temporarily_unavailable", result.manifests[0].blocking_issues)

    def test_41_folder_order_is_independent_of_input_order(self):
        handles = nested_handles(root_manifest([folder("z", "MOCK_Z"), folder("A", "MOCK_A"), folder("a", "MOCK_A2")]))
        first = built(handles=handles)[0]
        second = built(handles=tuple(reversed(handles)))[0]
        self.assertEqual(first.to_dict(), second.to_dict())
        keys = [(item.sku, item.safe_folder_name.casefold(), item.nested_folder_id_fingerprint) for item in first.manifests]
        self.assertEqual(keys, sorted(keys))

    def test_42_child_order_is_independent_of_drive_response(self):
        files = [drive_file("z.jpg", file_id="MOCK_Z"), drive_file("A.jpg", file_id="MOCK_A")]
        self.assertEqual(built(files)[0].to_dict(), built(list(reversed(files)))[0].to_dict())

    def test_43_shared_nested_folder_is_flagged_not_merged(self):
        first = nested_handles(root_manifest())[0]
        second = nested_handles(root_manifest(root_id="MOCK_OTHER_ROOT", sku="CLM-PRO-OTHER", row=30))[0]
        result, _, drive = built(handles=(first, second))
        self.assertEqual(len(result.manifests), 2)
        self.assertEqual(drive.files.return_value.list.call_count, 2)
        self.assertTrue(all("shared_nested_folder_candidate" in item.warnings for item in result.manifests))

    def test_44_one_hundred_nested_folders_are_allowed(self):
        handles = nested_handles(root_manifest([folder(f"Folder {i}", f"MOCK_NESTED_{i}") for i in range(100)]))
        result, _, drive = built(handles=handles)
        self.assertEqual(result.summary.total_nested_folders, 100)
        self.assertEqual(drive.files.return_value.list.call_count, 100)

    def test_45_batch_limit_is_checked_before_any_listing(self):
        handles = nested_handles(root_manifest([folder(f"Folder {i}", f"MOCK_NESTED_{i}") for i in range(101)]))
        gateway, drive = mocked_gateway()
        with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "nested_folder_batch_limit_exceeded"):
            build_nested_drive_folder_manifests_with_gateway(handles, gateway)
        self.assertEqual(gateway.counters.read_requests_performed, 0)
        drive.files.assert_not_called()

    def test_46_raw_root_id_is_not_in_nested_report(self):
        result, _, _ = built()
        self.assertNotIn(ROOT_ID, serialized(result))
        self.assertIn(root_core.fingerprint_drive_id(ROOT_ID), serialized(result))

    def test_47_raw_nested_id_is_not_in_report_or_repr(self):
        handles = nested_handles(root_manifest())
        result, _, _ = built(handles=handles)
        for output in (serialized(result), repr(result), repr(handles), json.dumps(handles[0].to_dict())):
            self.assertNotIn(NESTED_ID, output)

    def test_48_raw_child_file_id_is_not_in_report_or_repr(self):
        result, _, _ = built([drive_file(file_id=CHILD_ID)])
        self.assertNotIn(CHILD_ID, serialized(result))
        self.assertNotIn(CHILD_ID, repr(result))
        self.assertNotIn("provider_file_id", serialized(result))

    def test_49_shortcut_target_id_is_neither_requested_nor_serialized(self):
        item = drive_file(mime_type=root_core.SHORTCUT_MIME_TYPE)
        item["shortcutDetails"] = {"targetId": SHORTCUT_TARGET_ID}
        result, _, drive = built([item])
        self.assertNotIn(SHORTCUT_TARGET_ID, serialized(result))
        self.assertNotIn("shortcutDetails", str(drive.files.return_value.list.call_args.kwargs))

    def test_50_no_get_media_calls(self):
        _, _, drive = built([drive_file()])
        drive.files.return_value.get_media.assert_not_called()

    def test_51_no_alt_media_parameter(self):
        _, _, drive = built([drive_file()])
        self.assertNotIn("alt", drive.files.return_value.list.call_args.kwargs)

    def test_52_no_export_calls(self):
        _, _, drive = built([drive_file()])
        drive.files.return_value.export.assert_not_called()
        drive.files.return_value.export_media.assert_not_called()

    def test_53_no_download_links_are_requested(self):
        _, _, drive = built()
        fields = drive.files.return_value.list.call_args.kwargs["fields"]
        for forbidden in ("webContentLink", "webViewLink", "thumbnailLink"):
            self.assertNotIn(forbidden, fields)

    def test_54_nested_manifest_cannot_be_promoted_as_root(self):
        result, _, drive = built([folder("deeper", DEPTH_TWO_ID)])
        with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "root_manifest_domain_object_required"):
            create_secure_google_drive_nested_folder_handle(result.manifests[0], result.manifests[0].items[0])
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_55_no_http_probe(self):
        built([drive_file()])
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def test_56_download_counts_are_fixed_zero(self):
        result, _, _ = built([drive_file()])
        self.assertEqual(result.download_requests_performed, 0)
        self.assertEqual(result.summary.download_requests_performed, 0)
        self.assertEqual(result.to_dict()["download_requests_performed"], 0)

    def test_57_write_counts_are_fixed_zero(self):
        result, _, drive = built([drive_file()])
        self.assertEqual(result.write_requests_performed, 0)
        self.assertEqual(result.summary.write_requests_performed, 0)
        for method in ("create", "update", "delete", "copy"):
            getattr(drive.files.return_value, method).assert_not_called()
        drive.permissions.assert_not_called()

    def test_58_input_domain_objects_and_metadata_are_immutable(self):
        root = root_manifest()
        handles = nested_handles(root)
        files = [drive_file(file_id=CHILD_ID), folder("deeper", DEPTH_TWO_ID)]
        before = copy.deepcopy((root, handles, files))
        built(files, handles=handles)
        self.assertEqual((root, handles, files), before)
        self.assertEqual(root.items[0].provider_file_id, NESTED_ID)

    def test_59_empty_batch_makes_no_requests(self):
        result, gateway, drive = built(handles=())
        self.assertEqual(result.summary.total_nested_folders, 0)
        self.assertEqual(result.summary.drive_read_requests_performed, 0)
        drive.files.assert_not_called()

    def test_60_core_does_not_read_files_environment_or_credentials(self):
        handles = nested_handles(root_manifest())
        with (
            patch("builtins.open", side_effect=AssertionError("File reads forbidden")),
            patch("io.open", side_effect=AssertionError("File reads forbidden")),
            patch("sync_worker.config.load_google_drive_metadata_config", side_effect=AssertionError("Config reads forbidden")),
        ):
            result, _, _ = built(handles=handles)
        self.assertEqual(result.summary.total_nested_folders, 1)

    def test_61_timeout_retry_is_reused(self):
        result, _, _ = built(responses={(NESTED_ID, None): [TimeoutError(), {"files": []}]})
        self.assertEqual(result.summary.drive_read_requests_performed, 2)

    def test_62_connection_reset_retry_is_reused(self):
        result, _, _ = built(responses={(NESTED_ID, None): [ConnectionResetError(), {"files": []}]})
        self.assertEqual(result.summary.drive_read_requests_performed, 2)

    def test_63_one_failed_folder_does_not_block_another(self):
        handles = nested_handles(root_manifest([folder("a", "MOCK_A"), folder("b", "MOCK_B")]))
        result, _, _ = built(handles=handles, responses={
            ("MOCK_A", None): FakeHttpError(403), ("MOCK_B", None): {"files": [drive_file()]},
        })
        self.assertEqual(result.summary.nested_folders_access_denied, 1)
        self.assertEqual(result.summary.nested_folders_listed, 1)

    def test_64_duplicates_are_not_detected_across_folders(self):
        handles = nested_handles(root_manifest([folder("a", "MOCK_A"), folder("b", "MOCK_B")]))
        result, _, _ = built(handles=handles, responses={
            (item.raw_nested_folder_id, None): {"files": [drive_file("same.jpg")]}
            for item in handles
        })
        self.assertEqual(result.summary.total_nested_items, 2)
        self.assertEqual(result.summary.duplicate_name_candidates, 0)
        self.assertEqual(result.summary.duplicate_content_candidates, 0)

    def test_65_serialized_root_json_is_rejected(self):
        report = root_manifest().to_dict()
        gateway, drive = mocked_gateway()
        for value in (report, [report], json.dumps(report)):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "nested_folder_handles_required"):
                    build_nested_drive_folder_manifests_with_gateway(value, gateway)
        drive.files.assert_not_called()

    def test_66_missing_memory_id_never_falls_back_to_fingerprint(self):
        root = root_manifest()
        item = replace(root.items[0], provider_file_id=None)
        root = replace(root, items=(item,))
        with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "invalid_nested_folder_handle"):
            create_secure_google_drive_nested_folder_handle(root, item)

    def test_67_item_must_belong_to_root_manifest(self):
        root = root_manifest()
        different_item = root_manifest([folder("Other", "MOCK_OTHER")]).items[0]
        with self.assertRaises(GoogleDriveNestedFolderManifestError):
            create_secure_google_drive_nested_folder_handle(root, different_item)

    def test_68_workspace_file_cannot_be_a_handle(self):
        root = root_manifest([drive_file(mime_type="application/vnd.google-apps.spreadsheet")])
        with self.assertRaises(GoogleDriveNestedFolderManifestError):
            create_secure_google_drive_nested_folder_handle(root, root.items[0])

    def test_69_failed_or_incomplete_root_is_not_promoted(self):
        root = root_manifest()
        for status in ("limit_exceeded", "access_denied", "read_failed"):
            with self.subTest(status=status), self.assertRaises(GoogleDriveNestedFolderManifestError):
                create_secure_google_drive_nested_folder_handle(replace(root, status=status), root.items[0])

    def test_70_fingerprint_mismatch_is_blocked_before_request(self):
        invalid = replace(nested_handles(root_manifest())[0], nested_folder_id_fingerprint="f" * 64)
        result, gateway, _ = built(handles=(invalid,))
        self.assertEqual(result.manifests[0].status, "invalid_nested_folder_handle")
        self.assertEqual(gateway.counters.read_requests_performed, 0)

    def test_71_malformed_raw_id_is_blocked_without_leaking(self):
        invalid = replace(nested_handles(root_manifest())[0], raw_nested_folder_id="SECRET' or '1'='1")
        result, _, _ = built(handles=(invalid,))
        self.assertEqual(result.manifests[0].status, "invalid_nested_folder_handle")
        self.assertNotIn("SECRET", repr(invalid) + serialized(result))

    def test_72_errors_never_include_provider_id_or_credentials(self):
        secret = f"{ROOT_ID} {NESTED_ID} Authorization: private Cookie: private token=private"
        result, _, _ = built(responses={(NESTED_ID, None): RuntimeError(secret)})
        output = serialized(result) + repr(result)
        for forbidden in (ROOT_ID, NESTED_ID, "Authorization", "Cookie", "private"):
            self.assertNotIn(forbidden, output)
        self.assertEqual(result.manifests[0].blocking_issues, ("drive_metadata_read_failed",))

    def test_73_resource_keys_and_urls_in_response_are_not_projected(self):
        item = drive_file()
        item.update({"webContentLink": "https://drive.google.com/private", "resourceKey": "MOCK_RESOURCE_KEY"})
        result, _, _ = built([item])
        for value in ("https://", "drive.google.com", "MOCK_RESOURCE_KEY", "webContentLink"):
            self.assertNotIn(value, serialized(result))

    def test_74_identifier_in_name_uses_root_redaction(self):
        result, _, _ = built([drive_file(f"{NESTED_ID}-{CHILD_ID}.jpg", file_id=CHILD_ID)])
        self.assertNotIn(NESTED_ID, serialized(result))
        self.assertNotIn(CHILD_ID, serialized(result))
        self.assertIn("provider_identifier_redacted_from_name", result.manifests[0].items[0].warnings)

    def test_75_safe_filename_policy_is_reused(self):
        handles = nested_handles(root_manifest())
        with patch.object(root_core, "_safe_name", wraps=root_core._safe_name) as safe_name:
            result, _, _ = built([drive_file("../unsafe\nfile.jpg")], handles=handles)
        self.assertTrue(safe_name.called)
        self.assertNotIn("..", result.manifests[0].items[0].safe_name)
        self.assertNotIn("\n", result.manifests[0].items[0].safe_name)

    def test_76_summary_contains_all_required_counts(self):
        result, _, _ = built()
        self.assertEqual(set(result.summary.to_dict()), {
            "total_nested_folders", "nested_folders_listed", "empty_nested_folders",
            "nested_folders_access_denied", "nested_folders_missing_or_inaccessible",
            "nested_folders_limit_exceeded", "nested_folders_read_failed", "invalid_nested_folder_handles",
            "total_nested_items", "image_candidates", "nested_folders_at_depth_limit",
            "shortcuts", "google_workspace_files", "other_files", "duplicate_name_candidates",
            "duplicate_content_candidates", "pages_read", "drive_read_requests_performed",
            "download_requests_performed", "write_requests_performed",
        })

    def test_77_request_count_excludes_prior_root_reads(self):
        handles = nested_handles(root_manifest())
        gateway, _ = mocked_gateway()
        gateway.counters.read_requests_performed = 8
        result = build_nested_drive_folder_manifests_with_gateway(handles, gateway)
        self.assertEqual(result.summary.drive_read_requests_performed, 1)
        self.assertEqual(gateway.counters.read_requests_performed, 9)

    def test_78_batch_limit_cannot_be_expanded_or_boolean(self):
        self.assertEqual(MAX_NESTED_FOLDERS_PER_RUN, 100)
        for value in (101, 0, -1, True, "100"):
            with self.subTest(value=value), self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "invalid_nested_folder_batch_limit"):
                built(max_nested_folders_per_run=value)

    def test_79_tighter_batch_limit_fails_closed(self):
        handles = nested_handles(root_manifest([folder("a", "MOCK_A"), folder("b", "MOCK_B")]))
        gateway, drive = mocked_gateway()
        with self.assertRaisesRegex(GoogleDriveNestedFolderManifestError, "nested_folder_batch_limit_exceeded"):
            build_nested_drive_folder_manifests_with_gateway(handles, gateway, max_nested_folders_per_run=1)
        drive.files.assert_not_called()

    def test_80_no_folder_role_or_image_selection_policy(self):
        result, _, _ = built([drive_file("main.jpg"), drive_file("gallery.jpg")])
        output = serialized(result)
        for key in ("folder_role", "main_image", "gallery_order", "verified_image", "images"):
            self.assertNotIn(f'"{key}"', output)
        self.assertEqual(result.summary.image_candidates, 2)

    def test_81_no_nested_cli_is_registered(self):
        from sync_worker.cli import build_parser
        self.assertNotIn("build-nested-drive-folder-manifests", build_parser().format_help())

    def test_82_provider_id_field_is_explicitly_repr_false(self):
        self.assertFalse(root_core.DriveManifestItem.__dataclass_fields__["provider_file_id"].repr)
        self.assertFalse(SecureGoogleDriveNestedFolderHandle.__dataclass_fields__["raw_nested_folder_id"].repr)


if __name__ == "__main__":
    unittest.main()
