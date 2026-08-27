from __future__ import annotations

import copy
import json
import socket
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import google_drive_depth2_folder_manifest as depth2_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker.google_api import DRIVE_FOLDER_MANIFEST_FIELDS
from sync_worker.google_drive_depth2_folder_manifest import (
    GoogleDriveDepth2FolderManifestError,
    MAX_DEPTH2_FOLDERS_PER_RUN,
    MAX_TRAVERSAL_DEPTH,
    SecureGoogleDriveDepth2FolderHandle,
    build_depth2_drive_folder_manifests_with_gateway,
    create_secure_google_drive_depth2_folder_handle,
)
from tests.test_google_drive_folder_manifest import FakeHttpError, MD5, drive_file
from tests.test_google_drive_nested_folder_manifest import (
    ROOT_ID, NESTED_ID, folder, mocked_gateway, nested_handles, root_manifest,
)


DEPTH2_ID = "MOCK_DEPTH2_PRIVATE_123456"
DEPTH3_ID = "MOCK_DEPTH3_PRIVATE_123456"
CHILD_ID = "MOCK_DEPTH2_CHILD_PRIVATE_123456"
SHORTCUT_TARGET_ID = "MOCK_DEPTH2_SHORTCUT_TARGET_PRIVATE"
FOLDER_NAMES = (
    "Banner-MOCK", "Eye Options (MOCK)", "Factory Photos - MOCK",
    "Factory Videos - MOCK", "Other Skin Tone Factory Photos",
    "Photos-MOCK", "Video-MOCK", "Promo assets - MOCK",
)
SUMMARY_FIELDS = {
    "total_depth2_folders", "depth2_folders_listed", "empty_depth2_folders",
    "depth2_folders_access_denied", "depth2_folders_missing_or_inaccessible",
    "depth2_folders_limit_exceeded", "depth2_folders_read_failed",
    "invalid_depth2_folder_handles", "total_depth2_items", "image_candidates",
    "nested_folders_at_depth_limit", "shortcuts", "google_workspace_files",
    "other_files", "duplicate_name_candidates", "duplicate_content_candidates",
    "pages_read", "drive_read_requests_performed", "download_requests_performed",
    "write_requests_performed",
}


def depth1_manifest(files=None, *, parent_id=NESTED_ID, parent_name="MOCK Parent", **root_args):
    root = root_manifest([folder(parent_name, parent_id)], **root_args)
    gateway, _ = mocked_gateway({
        (parent_id, None): {"files": files if files is not None else [folder("Photos-MOCK", DEPTH2_ID)]},
    })
    return nested_core.build_nested_drive_folder_manifests_with_gateway(
        nested_handles(root), gateway,
    ).manifests[0]


def depth2_handles(parent):
    return tuple(
        create_secure_google_drive_depth2_folder_handle(parent, item)
        for item in parent.items if item.item_kind == "nested_folder"
    )


def built(files=None, *, handles=None, responses=None, **limits):
    active_handles = depth2_handles(depth1_manifest()) if handles is None else handles
    gateway, drive = mocked_gateway(
        responses if responses is not None else {(DEPTH2_ID, None): {"files": files or []}}
    )
    result = build_depth2_drive_folder_manifests_with_gateway(active_handles, gateway, **limits)
    return result, gateway, drive


def serialized(result):
    return json.dumps(result.to_report_dict(), sort_keys=True)


def listed_ids(drive):
    return [call.kwargs["q"].split("'", maxsplit=2)[1] for call in drive.files.return_value.list.call_args_list]


class GoogleDriveDepth2FolderManifestTests(unittest.TestCase):
    def setUp(self):
        self.connect = self.enterContext(patch.object(
            socket.socket, "connect", side_effect=AssertionError("Real network forbidden"),
        ))
        self.create_connection = self.enterContext(patch.object(
            socket, "create_connection", side_effect=AssertionError("Real network forbidden"),
        ))
        self.factory = self.enterContext(patch(
            "sync_worker.google_api.OfficialGoogleClientFactory",
            side_effect=AssertionError("Core must not create credentials or clients"),
        ))

    def tearDown(self):
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()
        self.factory.assert_not_called()

    def test_01_depth_limit_item_promoted_from_actual_domain_object(self):
        parent = depth1_manifest()
        item = parent.items[0]
        self.assertIn("max_traversal_depth_reached", item.warnings)
        handle = create_secure_google_drive_depth2_folder_handle(parent, item)
        self.assertEqual(handle.raw_depth2_folder_id, item.provider_file_id)
        self.assertEqual(handle.raw_depth2_folder_id, DEPTH2_ID)
        self.assertNotEqual(handle.raw_depth2_folder_id, item.file_id_fingerprint)

    def test_02_raw_id_field_is_hidden_from_repr_and_allowlist(self):
        handle = depth2_handles(depth1_manifest())[0]
        self.assertFalse(SecureGoogleDriveDepth2FolderHandle.__dataclass_fields__["raw_depth2_folder_id"].repr)
        self.assertNotIn(DEPTH2_ID, repr(handle) + json.dumps(handle.to_safe_dict()))
        self.assertNotIn("raw_depth2_folder_id", handle.to_dict())

    def test_03_non_folder_items_cannot_create_handles(self):
        for mime in ("image/jpeg", "video/mp4", root_core.SHORTCUT_MIME_TYPE, "application/vnd.google-apps.document"):
            with self.subTest(mime=mime):
                parent = depth1_manifest([drive_file(mime_type=mime)])
                with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "invalid_depth2_folder_handle"):
                    create_secure_google_drive_depth2_folder_handle(parent, parent.items[0])

    def test_04_root_cannot_skip_depth_one(self):
        root = root_manifest()
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth1_manifest_domain_object_required"):
            create_secure_google_drive_depth2_folder_handle(root, root.items[0])

    def test_05_serialized_parent_is_not_provenance(self):
        parent = depth1_manifest()
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth1_manifest_domain_object_required"):
            create_secure_google_drive_depth2_folder_handle(parent.to_dict(), parent.items[0])

    def test_06_equal_item_copy_is_not_actual_source_item(self):
        parent = depth1_manifest()
        copied = replace(parent.items[0])
        self.assertEqual(copied, parent.items[0])
        with self.assertRaises(GoogleDriveDepth2FolderManifestError):
            create_secure_google_drive_depth2_folder_handle(parent, copied)

    def test_07_item_from_another_parent_is_rejected(self):
        parent = depth1_manifest()
        other = depth1_manifest(parent_id="OTHER_MOCK_PARENT")
        with self.assertRaises(GoogleDriveDepth2FolderManifestError):
            create_secure_google_drive_depth2_folder_handle(parent, other.items[0])

    def test_08_unmarked_folder_cannot_be_promoted(self):
        parent = depth1_manifest()
        item = replace(parent.items[0], warnings=("nested_folder_not_traversed",))
        parent = replace(parent, items=(item,))
        with self.assertRaises(GoogleDriveDepth2FolderManifestError):
            create_secure_google_drive_depth2_folder_handle(parent, item)

    def test_09_parent_depth_must_be_exactly_integer_one(self):
        parent = depth1_manifest()
        for depth in (0, 2, 3, True, "1", 1.0):
            with self.subTest(depth=depth), self.assertRaises(GoogleDriveDepth2FolderManifestError):
                create_secure_google_drive_depth2_folder_handle(replace(parent, depth=depth), parent.items[0])

    def test_10_incomplete_or_failed_parent_is_rejected(self):
        parent = depth1_manifest()
        for status in ("limit_exceeded", "read_failed", "access_denied", "empty_folder"):
            with self.subTest(status=status), self.assertRaises(GoogleDriveDepth2FolderManifestError):
                create_secure_google_drive_depth2_folder_handle(replace(parent, status=status), parent.items[0])

    def test_11_blocked_parent_is_rejected(self):
        parent = depth1_manifest()
        parent = replace(parent, blocking_issues=("mock_blocker",))
        with self.assertRaises(GoogleDriveDepth2FolderManifestError):
            create_secure_google_drive_depth2_folder_handle(parent, parent.items[0])

    def test_12_missing_id_never_falls_back_to_fingerprint(self):
        parent = depth1_manifest()
        item = replace(parent.items[0], provider_file_id=None)
        parent = replace(parent, items=(item,))
        with self.assertRaises(GoogleDriveDepth2FolderManifestError):
            create_secure_google_drive_depth2_folder_handle(parent, item)

    def test_13_mismatched_item_fingerprint_is_rejected(self):
        parent = depth1_manifest()
        item = replace(parent.items[0], file_id_fingerprint="f" * 64)
        parent = replace(parent, items=(item,))
        with self.assertRaises(GoogleDriveDepth2FolderManifestError):
            create_secure_google_drive_depth2_folder_handle(parent, item)

    def test_14_malformed_id_does_not_leak_through_exception(self):
        parent = depth1_manifest()
        item = replace(parent.items[0], provider_file_id="PRIVATE' or '1'='1")
        with self.assertRaises(GoogleDriveDepth2FolderManifestError) as caught:
            create_secure_google_drive_depth2_folder_handle(replace(parent, items=(item,)), item)
        self.assertEqual(str(caught.exception), "invalid_depth2_folder_handle")

    def test_15_parent_identity_and_source_are_retained(self):
        parent = depth1_manifest()
        handle = depth2_handles(parent)[0]
        self.assertEqual(handle.sku, parent.sku)
        self.assertEqual(handle.product_source, parent.product_source)
        self.assertEqual(handle.root_folder_id_fingerprint, parent.root_folder_id_fingerprint)
        self.assertEqual(handle.depth1_folder_id_fingerprint, parent.nested_folder_id_fingerprint)
        self.assertEqual(handle.depth2_folder_id_fingerprint, parent.items[0].file_id_fingerprint)
        self.assertEqual(handle.depth1_safe_folder_name, "MOCK Parent")
        self.assertEqual(handle.depth2_safe_folder_name, "Photos-MOCK")

    def test_16_handle_creation_does_not_read_files_or_create_clients(self):
        parent = depth1_manifest()
        with patch("builtins.open", side_effect=AssertionError("No file reads")), patch("io.open", side_effect=AssertionError("No file reads")):
            handle = create_secure_google_drive_depth2_folder_handle(parent, parent.items[0])
        self.assertEqual(handle.raw_depth2_folder_id, DEPTH2_ID)

    def test_17_depth_is_exactly_two(self):
        handles = depth2_handles(depth1_manifest())
        result, _, _ = built(handles=handles)
        self.assertEqual(MAX_TRAVERSAL_DEPTH, 2)
        self.assertEqual(handles[0].depth, 2)
        self.assertEqual(result.manifests[0].depth, 2)

    def test_18_invalid_depth_never_makes_requests(self):
        handle = depth2_handles(depth1_manifest())[0]
        for depth in (0, 1, 3, -1, True, "2", 2.0):
            with self.subTest(depth=depth):
                result, gateway, drive = built(handles=(replace(handle, depth=depth),))
                self.assertEqual(result.summary.invalid_depth2_folder_handles, 1)
                self.assertEqual(result.manifests[0].status, "invalid_depth2_folder_handle")
                self.assertEqual(gateway.counters.read_requests_performed, 0)
                drive.files.assert_not_called()

    def test_19_unsafe_public_identity_is_rejected_and_hidden(self):
        handle = depth2_handles(depth1_manifest())[0]
        for field, value in (
            ("depth1_safe_folder_name", "https://drive.google.com/private"),
            ("depth2_safe_folder_name", "Authorization: PRIVATE"),
            ("depth2_safe_folder_name", DEPTH2_ID),
            ("sku", "../PRIVATE"),
        ):
            with self.subTest(field=field, value=value):
                invalid = replace(handle, **{field: value})
                result, _, drive = built(handles=(invalid,))
                self.assertEqual(result.summary.invalid_depth2_folder_handles, 1)
                self.assertNotIn(value, serialized(result) + repr(invalid))
                drive.files.assert_not_called()

    def test_20_invalid_ancestor_fingerprint_is_rejected(self):
        handle = depth2_handles(depth1_manifest())[0]
        for field in ("root_folder_id_fingerprint", "depth1_folder_id_fingerprint"):
            with self.subTest(field=field):
                result, _, drive = built(handles=(replace(handle, **{field: "PRIVATE"}),))
                self.assertEqual(result.summary.invalid_depth2_folder_handles, 1)
                drive.files.assert_not_called()

    def test_21_eight_folder_fixture_includes_every_business_name(self):
        parent = depth1_manifest([folder(name, f"MOCK_BATCH_{i}") for i, name in enumerate(FOLDER_NAMES)])
        handles = depth2_handles(parent)
        result, _, drive = built(handles=handles, responses={
            (handle.raw_depth2_folder_id, None): {"files": [drive_file(file_id=f"MOCK_IMAGE_{i}")]}
            for i, handle in enumerate(handles)
        })
        self.assertEqual(result.summary.total_depth2_folders, 8)
        self.assertEqual(result.summary.depth2_folders_listed, 8)
        self.assertEqual(result.summary.image_candidates, 8)
        self.assertEqual(result.summary.drive_read_requests_performed, 8)
        self.assertEqual(set(listed_ids(drive)), {handle.raw_depth2_folder_id for handle in handles})
        self.assertEqual({manifest.depth2_safe_folder_name for manifest in result.manifests}, set(FOLDER_NAMES))

    def test_22_fifty_folders_allowed(self):
        handles = depth2_handles(depth1_manifest([folder(f"Mock {i}", f"MOCK_BATCH_{i:03d}") for i in range(50)]))
        result, _, drive = built(handles=handles)
        self.assertEqual(MAX_DEPTH2_FOLDERS_PER_RUN, 50)
        self.assertEqual(result.summary.total_depth2_folders, 50)
        self.assertEqual(drive.files.return_value.list.call_count, 50)

    def test_23_over_fifty_stops_before_any_listing(self):
        handles = depth2_handles(depth1_manifest([folder(f"Mock {i}", f"MOCK_BATCH_{i:03d}") for i in range(51)]))
        gateway, drive = mocked_gateway()
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth2_folder_batch_limit_exceeded"):
            build_depth2_drive_folder_manifests_with_gateway(handles, gateway)
        self.assertEqual(gateway.counters.read_requests_performed, 0)
        drive.files.assert_not_called()

    def test_24_limit_cannot_be_expanded_or_non_integer(self):
        handles = depth2_handles(depth1_manifest())
        gateway, drive = mocked_gateway()
        for limit in (51, 0, -1, True, "50", 50.0):
            with self.subTest(limit=limit), self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "invalid_depth2_folder_batch_limit"):
                build_depth2_drive_folder_manifests_with_gateway(handles, gateway, max_depth2_folders_per_run=limit)
        drive.files.assert_not_called()

    def test_25_tighter_limit_is_respected(self):
        handles = depth2_handles(depth1_manifest([folder("A", "MOCK_A"), folder("B", "MOCK_B")]))
        gateway, drive = mocked_gateway()
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth2_folder_batch_limit_exceeded"):
            build_depth2_drive_folder_manifests_with_gateway(handles, gateway, max_depth2_folders_per_run=1)
        drive.files.assert_not_called()

    def test_26_empty_batch_performs_no_reads(self):
        result, _, drive = built(handles=())
        self.assertEqual(result.summary.total_depth2_folders, 0)
        self.assertEqual(result.summary.drive_read_requests_performed, 0)
        drive.files.assert_not_called()

    def test_27_serialized_or_wrong_handle_types_block_whole_batch(self):
        valid = depth2_handles(depth1_manifest())[0]
        gateway, drive = mocked_gateway()
        for handles in ({"manifests": []}, "PRIVATE", [valid, valid.to_dict()], [depth1_manifest()]):
            with self.subTest(kind=type(handles).__name__), self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth2_folder_handles_required"):
                build_depth2_drive_folder_manifests_with_gateway(handles, gateway)
        drive.files.assert_not_called()

    def test_28_query_uses_only_raw_depth2_id_not_ancestor_or_fingerprint(self):
        result, _, drive = built()
        query = drive.files.return_value.list.call_args.kwargs["q"]
        self.assertEqual(query, f"'{DEPTH2_ID}' in parents and trashed = false")
        for value in (ROOT_ID, NESTED_ID, result.manifests[0].depth2_folder_id_fingerprint):
            self.assertNotIn(value, query)

    def test_29_only_allowlisted_direct_child_metadata_is_requested(self):
        _, _, drive = built()
        call = drive.files.return_value.list.call_args.kwargs
        self.assertEqual(call["fields"], DRIVE_FOLDER_MANIFEST_FIELDS)
        self.assertEqual(call["pageSize"], 100)
        self.assertTrue(call["supportsAllDrives"])
        for forbidden in ("shortcutDetails", "resourceKey", "webContentLink", "thumbnailLink", "webViewLink"):
            self.assertNotIn(forbidden, call["fields"])

    def test_30_listing_reuses_root_core_with_unchanged_defaults(self):
        handles = depth2_handles(depth1_manifest())
        with patch.object(root_core, "build_drive_folder_manifests_with_gateway", wraps=root_core.build_drive_folder_manifests_with_gateway) as builder:
            built(handles=handles)
        builder.assert_called_once()
        self.assertEqual(builder.call_args.args[0][0].raw_folder_id, DEPTH2_ID)
        self.assertEqual(builder.call_args.kwargs, {})

    def test_31_pagination_reuses_root_policy(self):
        result, _, drive = built(responses={
            (DEPTH2_ID, None): {"files": [drive_file("a.jpg")], "nextPageToken": "NEXT"},
            (DEPTH2_ID, "NEXT"): {"files": [drive_file("b.jpg", file_id=CHILD_ID)]},
        })
        self.assertEqual(result.summary.pages_read, 2)
        self.assertEqual(result.summary.total_depth2_items, 2)
        self.assertEqual(drive.files.return_value.list.call_args_list[1].kwargs["pageToken"], "NEXT")

    def test_32_thousand_item_limit_is_reused(self):
        result, _, _ = built([drive_file(f"{i}.jpg", file_id=f"MOCK_IMAGE_{i}") for i in range(1001)])
        self.assertEqual(result.summary.total_depth2_items, 1000)
        self.assertEqual(result.summary.depth2_folders_limit_exceeded, 1)

    def test_33_twenty_page_limit_is_reused(self):
        result, _, drive = built(responses={
            (DEPTH2_ID, None if i == 0 else f"PAGE{i}"): {"files": [], "nextPageToken": f"PAGE{i + 1}"}
            for i in range(21)
        })
        self.assertEqual(result.summary.pages_read, 20)
        self.assertEqual(result.summary.depth2_folders_limit_exceeded, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 20)

    def test_34_duplicate_page_token_stops_listing(self):
        result, _, drive = built(responses={
            (DEPTH2_ID, None): {"files": [], "nextPageToken": "SAME"},
            (DEPTH2_ID, "SAME"): {"files": [], "nextPageToken": "SAME"},
        })
        self.assertEqual(result.manifests[0].blocking_issues, ("duplicate_drive_page_token",))
        self.assertEqual(drive.files.return_value.list.call_count, 2)

    def test_35_mime_image_candidate_keeps_existing_status(self):
        result, _, _ = built([drive_file(image_metadata={"width": 320, "height": 480, "rotation": 90})])
        item = result.manifests[0].items[0]
        self.assertTrue(item.image_candidate)
        self.assertEqual(item.image_candidate_status, "drive_metadata_image_candidate")
        self.assertEqual((item.image_width, item.image_height, item.image_rotation), (320, 480, 90))

    def test_36_psd_candidate_policy_is_unchanged(self):
        result, _, _ = built([drive_file("source.psd", mime_type="image/vnd.adobe.photoshop")])
        self.assertEqual(result.summary.image_candidates, 1)
        self.assertEqual(result.manifests[0].items[0].image_candidate_status, "drive_metadata_image_candidate")

    def test_37_extension_cannot_override_mime_classification(self):
        result, _, _ = built([drive_file("pretend.jpg", mime_type="video/mp4")])
        self.assertEqual(result.summary.image_candidates, 0)
        self.assertEqual(result.summary.other_files, 1)

    def test_38_video_and_archive_stay_other_files(self):
        result, _, _ = built([
            drive_file("movie.mp4", mime_type="video/mp4"),
            drive_file("assets.zip", file_id="MOCK_ARCHIVE", mime_type="application/zip"),
        ])
        self.assertEqual(result.summary.other_files, 2)

    def test_39_workspace_file_is_classified(self):
        result, _, _ = built([drive_file("doc", mime_type="application/vnd.google-apps.document")])
        self.assertEqual(result.summary.google_workspace_files, 1)

    def test_40_child_folder_remains_inventory_at_depth_limit(self):
        result, _, _ = built([folder("Deeper-MOCK", DEPTH3_ID)])
        self.assertEqual(result.summary.nested_folders_at_depth_limit, 1)
        self.assertEqual(result.manifests[0].items[0].item_kind, "nested_folder")
        self.assertIn("max_traversal_depth_reached", result.manifests[0].warnings)
        self.assertIn("max_traversal_depth_reached", result.manifests[0].items[0].warnings)

    def test_41_depth_three_folder_is_never_listed(self):
        _, _, drive = built(responses={
            (DEPTH2_ID, None): {"files": [folder("Deeper-MOCK", DEPTH3_ID)]},
            (DEPTH3_ID, None): AssertionError("Depth three traversal forbidden"),
        })
        self.assertEqual(listed_ids(drive), [DEPTH2_ID])

    def test_42_depth2_result_cannot_authorize_another_promotion(self):
        result, _, drive = built([folder("Deeper-MOCK", DEPTH3_ID)])
        parent = result.manifests[0]
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "depth1_manifest_domain_object_required"):
            create_secure_google_drive_depth2_folder_handle(parent, parent.items[0])
        self.assertEqual(listed_ids(drive), [DEPTH2_ID])

    def test_43_shortcut_not_followed_or_target_requested(self):
        shortcut = drive_file("Shortcut", mime_type=root_core.SHORTCUT_MIME_TYPE)
        shortcut["shortcutDetails"] = {"targetId": SHORTCUT_TARGET_ID}
        result, _, drive = built([shortcut])
        self.assertEqual(result.summary.shortcuts, 1)
        self.assertIn("shortcut_not_followed", result.manifests[0].warnings)
        self.assertIn("shortcut_not_followed", result.manifests[0].items[0].warnings)
        self.assertNotIn(SHORTCUT_TARGET_ID, serialized(result) + str(drive.mock_calls))
        drive.files.return_value.get.assert_not_called()

    def test_44_duplicate_name_detection_is_reused(self):
        result, _, _ = built([drive_file("same.jpg", file_id="MOCK_A"), drive_file("SAME.JPG", file_id="MOCK_B")])
        self.assertEqual(result.summary.duplicate_name_candidates, 2)
        self.assertTrue(all("duplicate_name_candidate" in item.warnings for item in result.manifests[0].items))

    def test_45_duplicate_checksum_detection_is_reused(self):
        result, _, _ = built([drive_file("a.jpg", md5=MD5), drive_file("b.jpg", file_id="MOCK_B", md5=MD5.upper())])
        self.assertEqual(result.summary.duplicate_content_candidates, 2)
        self.assertTrue(all(item.md5_checksum == MD5 for item in result.manifests[0].items))

    def test_46_duplicate_candidates_are_not_removed(self):
        result, _, _ = built([drive_file(), drive_file()])
        self.assertEqual(result.summary.total_depth2_items, 2)

    def test_47_empty_folder(self):
        result, _, _ = built()
        self.assertEqual(result.manifests[0].status, "empty_folder")
        self.assertEqual(result.summary.empty_depth2_folders, 1)

    def test_48_401_is_access_denied_without_retry(self):
        result, _, drive = built(responses={(DEPTH2_ID, None): FakeHttpError(401)})
        self.assertEqual(result.summary.depth2_folders_access_denied, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_49_403_is_access_denied_without_retry(self):
        result, _, drive = built(responses={(DEPTH2_ID, None): FakeHttpError(403)})
        self.assertEqual(result.summary.depth2_folders_access_denied, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_50_404_is_missing_without_retry(self):
        result, _, drive = built(responses={(DEPTH2_ID, None): FakeHttpError(404)})
        self.assertEqual(result.summary.depth2_folders_missing_or_inaccessible, 1)
        self.assertEqual(drive.files.return_value.list.call_count, 1)

    def test_51_429_retries_safely(self):
        result, _, _ = built(responses={(DEPTH2_ID, None): [FakeHttpError(429), {"files": []}]})
        self.assertEqual(result.summary.drive_read_requests_performed, 2)
        self.assertEqual(result.summary.pages_read, 1)

    def test_52_5xx_retries_safely(self):
        result, _, _ = built(responses={(DEPTH2_ID, None): [FakeHttpError(503), {"files": []}]})
        self.assertEqual(result.summary.drive_read_requests_performed, 2)
        self.assertEqual(result.summary.empty_depth2_folders, 1)

    def test_53_retry_limit_is_three(self):
        result, _, drive = built(responses={(DEPTH2_ID, None): FakeHttpError(503)})
        self.assertEqual(result.summary.depth2_folders_read_failed, 1)
        self.assertEqual(result.summary.drive_read_requests_performed, 3)
        self.assertEqual(drive.files.return_value.list.call_count, 3)

    def test_54_transport_retries_are_reused(self):
        for error in (TimeoutError(), ConnectionResetError()):
            with self.subTest(error=type(error).__name__):
                result, _, _ = built(responses={(DEPTH2_ID, None): [error, {"files": []}]})
                self.assertEqual(result.summary.drive_read_requests_performed, 2)

    def test_55_failure_in_one_folder_does_not_hide_another(self):
        handles = depth2_handles(depth1_manifest([folder("A", "MOCK_A"), folder("B", "MOCK_B")]))
        result, _, _ = built(handles=handles, responses={
            ("MOCK_A", None): FakeHttpError(403), ("MOCK_B", None): {"files": [drive_file()]},
        })
        self.assertEqual(result.summary.depth2_folders_access_denied, 1)
        self.assertEqual(result.summary.depth2_folders_listed, 1)

    def test_56_deterministic_handle_and_item_ordering(self):
        handles = depth2_handles(depth1_manifest([folder("A", "MOCK_A"), folder("B", "MOCK_B")]))
        files = [drive_file("b.jpg", file_id="MOCK_IMAGE_B"), drive_file("a.jpg", file_id="MOCK_IMAGE_A")]
        first, _, drive_a = built(handles=handles, responses={(h.raw_depth2_folder_id, None): {"files": files} for h in handles})
        second, _, drive_b = built(handles=tuple(reversed(handles)), responses={
            (h.raw_depth2_folder_id, None): {"files": list(reversed(files))} for h in handles
        })
        self.assertEqual(serialized(first), serialized(second))
        self.assertEqual(listed_ids(drive_a), listed_ids(drive_b))

    def test_57_raw_ids_absent_from_report_and_repr(self):
        handles = depth2_handles(depth1_manifest())
        result, _, _ = built([drive_file(file_id=CHILD_ID), folder("Deeper", DEPTH3_ID)], handles=handles)
        output = serialized(result) + repr(result) + repr(handles)
        for identifier in (ROOT_ID, NESTED_ID, DEPTH2_ID, DEPTH3_ID, CHILD_ID):
            self.assertNotIn(identifier, output)
        self.assertNotIn("provider_file_id", serialized(result))
        self.assertNotIn("raw_depth2_folder_id", serialized(result))

    def test_58_urls_resource_keys_and_download_links_are_not_projected(self):
        item = drive_file()
        item.update({
            "webContentLink": "https://drive.google.com/private", "resourceKey": "PRIVATE_RESOURCE_KEY",
            "thumbnailLink": "https://example.invalid/private", "webViewLink": "https://drive.google.com/view",
        })
        result, _, _ = built([item])
        for forbidden in ("https://", "drive.google.com", "PRIVATE_RESOURCE_KEY", "webContentLink", "thumbnailLink", "resourceKey"):
            self.assertNotIn(forbidden, serialized(result))

    def test_59_provider_exception_details_are_never_exposed(self):
        detail = f"{DEPTH2_ID} https://drive.google.com/private Authorization: PRIVATE Cookie: PRIVATE token=PRIVATE"
        result, _, _ = built(responses={(DEPTH2_ID, None): RuntimeError(detail)})
        self.assertEqual(result.manifests[0].blocking_issues, ("drive_metadata_read_failed",))
        for forbidden in (DEPTH2_ID, "https://", "Authorization", "Cookie", "PRIVATE", "token"):
            self.assertNotIn(forbidden, serialized(result) + repr(result))

    def test_60_provider_identifiers_in_filename_use_existing_redaction(self):
        result, _, _ = built([drive_file(f"{DEPTH2_ID}-{CHILD_ID}.jpg", file_id=CHILD_ID)])
        self.assertNotIn(DEPTH2_ID, serialized(result))
        self.assertNotIn(CHILD_ID, serialized(result))
        self.assertIn("provider_identifier_redacted_from_name", result.manifests[0].items[0].warnings)

    def test_61_safe_filename_policy_is_reused(self):
        handles = depth2_handles(depth1_manifest())
        with patch.object(root_core, "_safe_name", wraps=root_core._safe_name) as sanitizer:
            result, _, _ = built([drive_file("../unsafe\nimage.jpg")], handles=handles)
        self.assertTrue(sanitizer.called)
        self.assertNotIn("..", result.manifests[0].items[0].safe_name)
        self.assertNotIn("\n", result.manifests[0].items[0].safe_name)

    def test_62_no_content_download_export_or_alt_media(self):
        _, _, drive = built([drive_file()])
        for method in ("get_media", "export", "export_media", "download"):
            getattr(drive.files.return_value, method).assert_not_called()
        self.assertNotIn("alt", drive.files.return_value.list.call_args.kwargs)

    def test_63_no_http_media_probe_or_real_network(self):
        built([drive_file()])
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def test_64_download_counts_are_fixed_zero(self):
        result, _, _ = built([drive_file()])
        self.assertEqual(result.download_requests_performed, 0)
        self.assertEqual(result.summary.download_requests_performed, 0)
        self.assertEqual(result.to_dict()["download_requests_performed"], 0)
        self.assertEqual(result.summary.to_dict()["download_requests_performed"], 0)

    def test_65_write_counts_are_fixed_zero_and_no_mutation_methods_called(self):
        result, _, drive = built([drive_file()])
        self.assertEqual(result.write_requests_performed, 0)
        self.assertEqual(result.summary.write_requests_performed, 0)
        self.assertEqual(result.to_dict()["write_requests_performed"], 0)
        self.assertEqual(result.summary.to_dict()["write_requests_performed"], 0)
        for method in ("create", "update", "delete", "copy"):
            getattr(drive.files.return_value, method).assert_not_called()
        drive.permissions.assert_not_called()

    def test_66_summary_has_all_required_fields(self):
        result, _, _ = built()
        self.assertEqual(set(result.summary.to_dict()), SUMMARY_FIELDS)

    def test_67_request_counts_exclude_existing_root_and_nested_reads(self):
        handles = depth2_handles(depth1_manifest())
        gateway, _ = mocked_gateway()
        gateway.counters.read_requests_performed = 32
        result = build_depth2_drive_folder_manifests_with_gateway(handles, gateway)
        self.assertEqual(result.summary.drive_read_requests_performed, 1)
        self.assertEqual(gateway.counters.read_requests_performed, 33)

    def test_68_core_never_reads_reports_env_or_credentials(self):
        handles = depth2_handles(depth1_manifest())
        with (
            patch("builtins.open", side_effect=AssertionError("No file access")),
            patch("io.open", side_effect=AssertionError("No file access")),
            patch("sync_worker.config.load_google_drive_metadata_config", side_effect=AssertionError("No env reads")),
        ):
            result, _, _ = built(handles=handles)
        self.assertEqual(result.summary.total_depth2_folders, 1)

    def test_69_input_domain_objects_and_payloads_are_unchanged(self):
        parent = depth1_manifest()
        handles = depth2_handles(parent)
        files = [drive_file(file_id=CHILD_ID), folder("Deeper", DEPTH3_ID)]
        before = copy.deepcopy((parent, handles, files))
        built(files, handles=handles)
        self.assertEqual((parent, handles, files), before)
        self.assertIn("max_traversal_depth_reached", parent.items[0].warnings)

    def test_70_no_folder_roles_or_image_selection(self):
        result, _, _ = built([drive_file("main.jpg"), drive_file("gallery.jpg"), drive_file("factory.jpg")])
        for field in ("folder_role", "gallery", "factory", "banner", "video", "eye_options", "promo", "main_image", "gallery_order", "selected", "verified_image"):
            self.assertNotIn(f'"{field}":', serialized(result))
        self.assertEqual(result.summary.image_candidates, 3)

    def test_71_existing_nested_core_still_stops_at_depth_one(self):
        self.assertEqual(nested_core.MAX_TRAVERSAL_DEPTH, 1)
        parent = depth1_manifest()
        self.assertEqual(parent.depth, 1)
        self.assertIn("max_traversal_depth_reached", parent.items[0].warnings)

    def test_72_core_has_no_cli_or_client_creation_entrypoint(self):
        for name in ("main", "build_parser", "OfficialGoogleClientFactory", "load_google_drive_metadata_config"):
            self.assertFalse(hasattr(depth2_core, name))

    def test_73_shared_folder_is_not_merged_across_products(self):
        first = depth2_handles(depth1_manifest(sku="CLM-CLASSIC-MOCKA", row=10))[0]
        second = depth2_handles(depth1_manifest(sku="CLM-CLASSIC-MOCKB", row=20))[0]
        result, _, drive = built([drive_file()], handles=(first, second))
        self.assertEqual(result.summary.total_depth2_folders, 2)
        self.assertEqual(result.summary.drive_read_requests_performed, 2)
        self.assertEqual(listed_ids(drive), [DEPTH2_ID, DEPTH2_ID])
        self.assertEqual(len({manifest.sku for manifest in result.manifests}), 2)

    def test_74_invalid_direct_handle_is_reported_without_guessing_or_reading(self):
        handle = depth2_handles(depth1_manifest())[0]
        invalid = replace(handle, depth2_folder_id_fingerprint="f" * 64)
        result, _, drive = built(handles=(invalid,))
        self.assertEqual(result.summary.invalid_depth2_folder_handles, 1)
        self.assertNotIn(DEPTH2_ID, serialized(result))
        drive.files.assert_not_called()

    def test_75_known_id_from_another_folder_cannot_leak_in_metadata(self):
        handles = depth2_handles(depth1_manifest([folder("A", "MOCK_FOLDER_ALPHA"), folder("B", "MOCK_FOLDER_BETA")]))
        with self.assertRaisesRegex(GoogleDriveDepth2FolderManifestError, "unsafe_depth2_manifest_output"):
            built(handles=handles, responses={
                ("MOCK_FOLDER_ALPHA", None): {"files": [drive_file("MOCK_FOLDER_BETA.jpg")]},
                ("MOCK_FOLDER_BETA", None): {"files": []},
            })

    def test_76_duplicates_remain_scoped_to_each_folder(self):
        handles = depth2_handles(depth1_manifest([folder("A", "MOCK_A"), folder("B", "MOCK_B")]))
        result, _, _ = built(handles=handles, responses={
            (handle.raw_depth2_folder_id, None): {"files": [drive_file("same.jpg")]} for handle in handles
        })
        self.assertEqual(result.summary.total_depth2_items, 2)
        self.assertEqual(result.summary.duplicate_name_candidates, 0)
        self.assertEqual(result.summary.duplicate_content_candidates, 0)


if __name__ == "__main__":
    unittest.main()
