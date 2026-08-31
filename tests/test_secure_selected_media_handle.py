from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pickle
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import (
    folder_role_policy as folder_core,
    google_drive_depth2_folder_manifest as depth2_core,
    google_drive_folder_manifest as root_core,
    google_drive_nested_folder_manifest as nested_core,
    image_selection_policy as selection_core,
    secure_selected_media_handle as secure_core,
)
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.secure_selected_media_handle import (
    SecureSelectedMediaHandleError,
    create_secure_selected_media_handle,
)


RAW_ID = "opaque_drive_file_123"
SOURCE = ProductSourceRange(10, 20)


def drive_item(
    name="photo-1.jpg", *, raw_id=RAW_ID, fingerprint="auto",
    kind="image_candidate", image_candidate=True, mime="image/jpeg",
    size=1_000_000, width=2000, height=3000, warnings=(),
):
    if fingerprint == "auto":
        fingerprint = root_core.fingerprint_drive_id(raw_id) if isinstance(raw_id, str) else None
    return root_core.DriveManifestItem(
        safe_name=name, mime_type=mime, size_bytes=size,
        modified_time="2026-01-01T00:00:00Z", md5_checksum="a" * 32,
        file_id_fingerprint=fingerprint, item_kind=kind,
        image_candidate=image_candidate,
        image_candidate_status="drive_metadata_image_candidate" if image_candidate else None,
        image_width=width, image_height=height, image_rotation=0,
        warnings=tuple(warnings), provider_file_id=raw_id,
    )


def nested_manifest(
    *items, sku="MOCK-001", source=SOURCE, folder="Photos Mock",
    status="listed", warnings=(), blockers=(), depth=1,
):
    return nested_core.GoogleDriveNestedFolderManifest(
        sku=sku, product_source=source,
        root_folder_id_fingerprint=root_core.fingerprint_drive_id("root-folder-1"),
        nested_folder_id_fingerprint=root_core.fingerprint_drive_id("nested-folder-1"),
        safe_folder_name=folder, depth=depth, status=status,
        items=tuple(items), pages_read=1, warnings=tuple(warnings),
        blocking_issues=tuple(blockers),
    )


def depth2_manifest(
    *items, sku="MOCK-001", source=SOURCE, parent="Photos Mock",
    folder="Factory Photos Deep", status="listed", warnings=(), blockers=(), depth=2,
):
    return depth2_core.GoogleDriveDepth2FolderManifest(
        sku=sku, product_source=source,
        root_folder_id_fingerprint=root_core.fingerprint_drive_id("root-folder-1"),
        depth1_folder_id_fingerprint=root_core.fingerprint_drive_id("nested-folder-1"),
        depth2_folder_id_fingerprint=root_core.fingerprint_drive_id("depth2-folder-1"),
        depth1_safe_folder_name=parent, depth2_safe_folder_name=folder,
        depth=depth, status=status, items=tuple(items), pages_read=1,
        warnings=tuple(warnings), blocking_issues=tuple(blockers),
    )


def root_manifest(*items):
    return root_core.GoogleDriveFolderManifest(
        sku="MOCK-001", product_source=SOURCE,
        folder_id_fingerprint=root_core.fingerprint_drive_id("root-folder-1"),
        status="listed", items=tuple(items), pages_read=1,
    )


def selection_item(
    name="photo-1.jpg", *, sku="MOCK-001", source=SOURCE,
    kind="nested", folder="Photos Mock", parent=None,
    role=folder_core.FolderRole.STOREFRONT_PHOTOS,
    image_role=selection_core.ImageSelectionRole.PRIMARY, position=0,
    selected=True, quality=True, warnings=(), blockers=(), deeper=False,
):
    depth = {"root": 0, "nested": 1, "depth2": 2}[kind]
    reason = {
        (folder_core.FolderRole.STOREFRONT_PHOTOS, selection_core.ImageSelectionRole.PRIMARY): selection_core.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY,
        (folder_core.FolderRole.STOREFRONT_PHOTOS, selection_core.ImageSelectionRole.GALLERY): selection_core.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY,
        (folder_core.FolderRole.FACTORY_PHOTOS, selection_core.ImageSelectionRole.PRIMARY): selection_core.ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK,
        (folder_core.FolderRole.FACTORY_PHOTOS, selection_core.ImageSelectionRole.GALLERY): selection_core.ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL,
    }.get((role, image_role), selection_core.ImageSelectionReason.NOT_SELECTED_IMAGE_LIMIT)
    if not selected:
        image_role = selection_core.ImageSelectionRole.NOT_SELECTED
        position = None
        reason = selection_core.ImageSelectionReason.NOT_SELECTED_IMAGE_LIMIT
    return selection_core.ImageSelectionItem(
        sku=sku, folder_role=role, safe_name=name,
        source_manifest_kind=kind, depth=depth,
        safe_folder_name=folder, parent_safe_folder_name=parent,
        product_source=source, requires_deeper_inventory=deeper,
        quality_eligible=quality, selected=selected,
        selection_position=position, image_role=image_role,
        selection_reason=reason, warnings=tuple(warnings),
        blocking_issues=tuple(blockers),
    )


def depth2_selection(name="photo-1.jpg", **kwargs):
    return selection_item(
        name, kind="depth2", folder="Factory Photos Deep", parent="Photos Mock",
        role=folder_core.FolderRole.FACTORY_PHOTOS, **kwargs,
    )


def baseline_manifest_for(selection, source_item):
    fingerprint = source_item.file_id_fingerprint
    if type(fingerprint) is not str or root_core._SHA256_PATTERN.fullmatch(fingerprint) is None:
        fingerprint = root_core.fingerprint_drive_id(RAW_ID)
    checksum = source_item.md5_checksum
    if type(checksum) is not str or root_core._MD5_PATTERN.fullmatch(checksum) is None:
        checksum = "a" * 32
    historical_item = replace(
        source_item, safe_name=selection.safe_name,
        file_id_fingerprint=fingerprint, md5_checksum=checksum,
        item_kind="image_candidate", image_candidate=True,
        image_candidate_status="drive_metadata_image_candidate",
        warnings=(), provider_file_id=None,
    )
    if selection.source_manifest_kind == "depth2":
        return depth2_manifest(
            historical_item, sku=selection.sku, source=selection.product_source,
            parent=selection.parent_safe_folder_name,
            folder=selection.safe_folder_name,
        )
    return nested_manifest(
        historical_item, sku=selection.sku, source=selection.product_source,
        folder=selection.safe_folder_name,
    )


def baseline_identity(selection=None, source_item=None, manifest=None):
    selection = selection_item() if selection is None else selection
    source_item = drive_item(selection.safe_name) if source_item is None else source_item
    manifest = baseline_manifest_for(selection, source_item) if manifest is None else manifest
    return secure_core.create_selected_media_baseline_identity(selection, manifest)


def handle(item=None, selection=None, manifest=None, baseline=None, baseline_manifest=None):
    item = drive_item() if item is None else item
    selection = selection_item() if selection is None else selection
    if manifest is None:
        manifest = depth2_manifest(item) if selection.source_manifest_kind == "depth2" else nested_manifest(item)
    if baseline is None:
        baseline = baseline_identity(selection, item, baseline_manifest)
    return create_secure_selected_media_handle(selection, baseline, manifest)


class SecureSelectedMediaHandleTests(unittest.TestCase):
    def setUp(self):
        self.denied = []
        for target in (
            "socket.socket.connect", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "sync_worker.google_api.OfficialGoogleClientFactory",
            "sync_worker.google_api.GoogleDriveMetadataGateway.list_folder_children",
            "sync_worker.http_client.ReadOnlyHttpClient.request", "subprocess.run",
            "subprocess.Popen", "os.system",
        ):
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Pure memory only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No config"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def test_001_valid_nested(self):
        self.assertIsInstance(handle(), secure_core.SecureSelectedMediaHandle)

    def test_002_valid_depth2(self):
        item = drive_item(); result = handle(item, depth2_selection(), depth2_manifest(item))
        self.assertEqual((result.source_manifest_kind, result.depth), ("depth2", 2))

    def test_003_root_manifest_rejected(self):
        selection = selection_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "source_manifest_domain_object_required"):
            create_secure_selected_media_handle(selection, baseline_identity(selection), root_manifest(drive_item()))

    def test_004_not_selected_rejected(self):
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selection_item_not_selected"):
            handle(selection=selection_item(selected=False))

    def test_005_null_position_rejected(self):
        value = replace(selection_item(), selection_position=None)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "invalid_selection_item"):
            handle(selection=value)

    def test_006_not_selected_role_rejected(self):
        value = replace(selection_item(), image_role=selection_core.ImageSelectionRole.NOT_SELECTED)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(selection=value)

    def test_007_primary_accepted(self):
        self.assertEqual(handle().image_role, selection_core.ImageSelectionRole.PRIMARY)

    def test_008_gallery_accepted(self):
        value = selection_item(image_role=selection_core.ImageSelectionRole.GALLERY, position=1)
        self.assertEqual(handle(selection=value).selection_position, 1)

    def test_009_selection_version_mismatch(self):
        value = selection_item(); object.__setattr__(value, "policy_version", "wrong")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(selection=value)

    def test_010_selection_blockers_rejected(self):
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selection_item_blocked"):
            handle(selection=selection_item(blockers=("mock_blocker",)))

    def test_011_manifest_status_rejected(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item, status="read_failed"))

    def test_012_manifest_blocker_rejected(self):
        item = drive_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "source_manifest_blocked"):
            handle(item, manifest=nested_manifest(item, blockers=("mock_blocker",)))

    def test_013_sku_mismatch(self):
        item = drive_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_provenance_mismatch"):
            handle(item, manifest=nested_manifest(item, sku="MOCK-002"))

    def test_014_source_start_mismatch(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item, source=ProductSourceRange(11, 20)))

    def test_015_source_end_mismatch(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item, source=ProductSourceRange(10, 21)))

    def test_016_manifest_kind_mismatch(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, selection_item(kind="depth2", folder="Photos Mock", parent="Parent"), nested_manifest(item))

    def test_017_depth_mismatch(self):
        item = drive_item(); manifest = nested_manifest(item, depth=2)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=manifest)

    def test_018_folder_mismatch(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item, folder="Photos Other"))

    def test_019_parent_folder_mismatch(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, depth2_selection(), depth2_manifest(item, parent="Other"))

    def test_020_case_sensitive(self):
        item = drive_item("Photo-1.jpg")
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_source_missing"):
            handle(item, selection_item("photo-1.jpg"), nested_manifest(item))

    def test_021_no_fuzzy(self):
        item = drive_item("photo 1.jpg")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, selection_item("photo-1.jpg"), nested_manifest(item))

    def test_022_no_substring(self):
        item = drive_item("hero-photo-1.jpg")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, selection_item("photo-1.jpg"), nested_manifest(item))

    def test_023_source_missing(self):
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_source_missing"):
            handle(manifest=nested_manifest(drive_item("other.jpg")))

    def test_024_source_ambiguous(self):
        first, second = drive_item(), drive_item(raw_id="opaque_drive_file_456")
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_source_ambiguous"):
            handle(first, manifest=nested_manifest(first, second))

    def test_025_duplicate_safe_name_blocks(self):
        first, second = drive_item(), drive_item(raw_id="opaque_drive_file_456")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(first, manifest=nested_manifest(first, second))

    def test_026_no_first_match(self):
        first, second = drive_item(), drive_item(raw_id="opaque_drive_file_456")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(first, manifest=nested_manifest(first, second))

    def test_027_image_candidate_required(self):
        item = drive_item(kind="other_file", image_candidate=False)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_source_not_image_candidate"):
            handle(item, manifest=nested_manifest(item))

    def test_028_nested_folder_item_rejected(self):
        item = drive_item(kind="nested_folder", image_candidate=False, mime=root_core.FOLDER_MIME_TYPE, size=None, width=None, height=None)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_029_shortcut_rejected(self):
        item = drive_item(kind="shortcut", image_candidate=False, mime=root_core.SHORTCUT_MIME_TYPE)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_030_other_file_rejected(self):
        item = drive_item(kind="other_file", image_candidate=False, mime="video/mp4")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_031_provider_id_missing(self):
        item = drive_item(raw_id=None, fingerprint=None)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "fresh_selected_media_fingerprint_missing"):
            handle(item, manifest=nested_manifest(item))

    def test_032_provider_id_invalid(self):
        item = drive_item(raw_id="bad id", fingerprint="0" * 64)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "invalid_provider_file_identity"):
            handle(item, manifest=nested_manifest(item))

    def test_033_fingerprint_missing(self):
        item = drive_item(fingerprint=None)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_034_fingerprint_mismatch(self):
        item = drive_item(fingerprint="0" * 64)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_035_fingerprint_correct(self):
        self.assertEqual(handle().file_id_fingerprint, root_core.fingerprint_drive_id(RAW_ID))

    def test_036_fingerprint_not_reversed(self):
        source = inspect.getsource(secure_core.create_secure_selected_media_handle)
        self.assertNotIn("decode", source); self.assertNotIn("lookup", source)

    def test_037_safe_dict_no_raw_id(self):
        self.assertNotIn(RAW_ID, json.dumps(handle().to_safe_dict(), sort_keys=True))

    def test_038_repr_no_raw_id(self):
        self.assertNotIn(RAW_ID, repr(handle()))

    def test_039_str_no_raw_id(self):
        self.assertNotIn(RAW_ID, str(handle()))

    def test_040_no_dict(self):
        with self.assertRaises(TypeError):
            vars(handle())

    def test_041_asdict_cannot_expose(self):
        with self.assertRaises(TypeError):
            dataclasses.asdict(handle())

    def test_042_direct_json_fails(self):
        with self.assertRaises(TypeError):
            json.dumps(handle())

    def test_043_safe_dict_json_succeeds(self):
        self.assertIsInstance(json.dumps(handle().to_safe_dict()), str)

    def test_044_pickle_rejected(self):
        with self.assertRaisesRegex(TypeError, "secure_media_handle_not_serializable"):
            pickle.dumps(handle())

    def test_045_direct_constructor_guard(self):
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "secure_media_handle_factory_required"):
            secure_core.SecureSelectedMediaHandle()

    def test_046_forged_capability_rejected(self):
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core.SecureSelectedMediaHandle(object(), selection_item=selection_item(), source_item=drive_item())

    def test_047_factory_capability_accepted(self):
        item = drive_item()
        selection = selection_item()
        manifest = nested_manifest(item)
        value = secure_core.SecureSelectedMediaHandle(
            secure_core._HANDLE_CAPABILITY, selection_item=selection,
            baseline_identity=baseline_identity(selection, item),
            source_manifest=manifest, source_item=item,
        )
        self.assertEqual(secure_core._provider_file_id_for_download(value), RAW_ID)

    def test_048_private_provider_helper(self):
        self.assertEqual(secure_core._provider_file_id_for_download(handle()), RAW_ID)

    def test_049_helper_rejects_non_handle(self):
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download({})

    def test_050_helper_rejects_forged_handle(self):
        forged = object.__new__(secure_core.SecureSelectedMediaHandle)
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(forged)

    def test_051_raw_id_not_in_warnings(self):
        item = drive_item(warnings=(RAW_ID,))
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_052_raw_id_not_in_exception(self):
        item = drive_item(fingerprint="0" * 64)
        try:
            handle(item, manifest=nested_manifest(item))
        except SecureSelectedMediaHandleError as error:
            self.assertNotIn(RAW_ID, str(error))
        else:
            self.fail("expected failure")

    def test_053_safe_audit_fingerprint(self):
        self.assertEqual(handle().to_safe_dict()["file_id_fingerprint"], root_core.fingerprint_drive_id(RAW_ID))

    def test_054_safe_audit_sku(self):
        self.assertEqual(handle().to_safe_dict()["sku"], "MOCK-001")

    def test_055_safe_audit_hierarchy(self):
        data = handle().to_safe_dict()
        self.assertEqual((data["source_manifest_kind"], data["depth"], data["safe_folder_name"]), ("nested", 1, "Photos Mock"))

    def test_056_position_retained(self):
        value = selection_item(image_role=selection_core.ImageSelectionRole.GALLERY, position=7)
        self.assertEqual(handle(selection=value).selection_position, 7)

    def test_057_primary_retained(self):
        self.assertEqual(handle().to_safe_dict()["image_role"], "primary")

    def test_058_gallery_retained(self):
        value = selection_item(image_role=selection_core.ImageSelectionRole.GALLERY, position=1)
        self.assertEqual(handle(selection=value).to_safe_dict()["image_role"], "gallery")

    def test_059_folder_role_retained(self):
        self.assertEqual(handle().to_safe_dict()["folder_role"], "storefront_photos")

    def test_060_mime_retained(self):
        self.assertEqual(handle().source_mime_type, "image/jpeg")

    def test_061_dimensions_retained(self):
        self.assertEqual((handle().image_width, handle().image_height), (2000, 3000))

    def test_062_size_retained(self):
        self.assertEqual(handle().size_bytes, 1_000_000)

    def test_063_no_selection_reranking(self):
        selection = selection_item(image_role=selection_core.ImageSelectionRole.GALLERY, position=9)
        self.assertEqual(handle(selection=selection).selection_position, 9)

    def test_064_no_filename_correction(self):
        name = "SiW160 AmaraCinnamon) 1.jpg"; item = drive_item(name)
        self.assertEqual(handle(item, selection_item(name), nested_manifest(item)).safe_name, name)

    def test_065_imani_fuzzy_correction_forbidden(self):
        item = drive_item("SiW160 AmaraCinnamon) 1.jpg")
        selection = selection_item("SiW160 Amara(Cinnamon) 2.jpg")
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_source_missing"):
            handle(item, selection, nested_manifest(item))

    def test_066_no_download(self):
        self.assertFalse(hasattr(secure_core, "download"))

    def test_067_no_media_bytes(self):
        self.assertNotIn("bytes", handle().to_safe_dict())

    def test_068_no_network(self):
        imported = {node.names[0].name for node in ast.walk(ast.parse(inspect.getsource(secure_core))) if isinstance(node, ast.Import)}
        self.assertTrue(imported.isdisjoint({"socket", "requests", "urllib", "http"}))

    def test_069_no_drive_api(self):
        self.assertNotIn("googleapiclient", inspect.getsource(secure_core))

    def test_070_no_http(self):
        self.assertNotIn("httplib2", inspect.getsource(secure_core))

    def test_071_no_conversion(self):
        self.assertFalse(hasattr(secure_core, "convert"))

    def test_072_no_upload(self):
        self.assertFalse(hasattr(secure_core, "upload"))

    def test_073_no_write(self):
        tree = ast.parse(inspect.getsource(secure_core))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"write", "write_text", "write_bytes"} for node in ast.walk(tree)))

    def test_074_no_pillow(self):
        self.assertNotIn("PIL", inspect.getsource(secure_core))

    def test_075_no_imagemagick(self):
        self.assertNotIn("ImageMagick", inspect.getsource(secure_core))

    def test_076_no_cwebp(self):
        self.assertNotIn("cwebp", inspect.getsource(secure_core))

    def test_077_no_ffmpeg(self):
        self.assertNotIn("ffmpeg", inspect.getsource(secure_core))

    def test_078_nested_domain_required(self):
        selection = selection_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            create_secure_selected_media_handle(selection, baseline_identity(selection), depth2_manifest(drive_item()))

    def test_079_depth2_domain_required(self):
        selection = depth2_selection()
        with self.assertRaises(SecureSelectedMediaHandleError):
            create_secure_selected_media_handle(selection, baseline_identity(selection), nested_manifest(drive_item()))

    def test_080_dict_manifest_rejected(self):
        selection = selection_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "source_manifest_domain_object_required"):
            create_secure_selected_media_handle(selection, baseline_identity(selection), nested_manifest(drive_item()).to_dict())

    def test_081_json_manifest_rejected(self):
        selection = selection_item()
        value = json.dumps(nested_manifest(drive_item()).to_dict())
        with self.assertRaises(SecureSelectedMediaHandleError):
            create_secure_selected_media_handle(selection, baseline_identity(selection), value)

    def test_082_file_id_string_rejected(self):
        selection = selection_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            create_secure_selected_media_handle(selection, baseline_identity(selection), RAW_ID)

    def test_083_filename_string_rejected(self):
        selection = selection_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            create_secure_selected_media_handle(selection, baseline_identity(selection), "photo-1.jpg")

    def test_084_source_item_immutable(self):
        item = drive_item()
        with self.assertRaises(FrozenInstanceError):
            item.safe_name = "changed.jpg"

    def test_085_stable_safe_representation(self):
        value = handle()
        self.assertEqual(value.to_safe_dict(), value.to_safe_dict())
        self.assertEqual(repr(value), repr(value))

    def test_086_policy_version(self):
        self.assertEqual(handle().policy_version, "xxxxdoll-secure-selected-media-handle-v1")

    def test_087_ninety_six_shape(self):
        results = []
        for index in range(96):
            raw_id, name = f"opaque_id_{index}", f"photo-{index}.jpg"
            item = drive_item(name, raw_id=raw_id)
            results.append(handle(item, selection_item(name), nested_manifest(item)))
        self.assertEqual(len(results), 96)

    def test_088_one_ambiguous_prevents_handle(self):
        first, second = drive_item(), drive_item(raw_id="opaque_456")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(first, manifest=nested_manifest(first, second))

    def test_089_one_missing_prevents_handle(self):
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(manifest=nested_manifest())

    def test_090_no_report_writer(self):
        self.assertNotIn("SafeJsonReportWriter", inspect.getsource(secure_core))

    def test_091_no_cli(self):
        self.assertFalse(hasattr(secure_core, "main"))

    def test_092_selection_domain_exact_type(self):
        selection = selection_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "invalid_selection_item"):
            create_secure_selected_media_handle(selection.to_dict(), baseline_identity(selection), nested_manifest(drive_item()))

    def test_093_selection_json_rejected(self):
        selection = selection_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            create_secure_selected_media_handle(json.dumps(selection.to_dict()), baseline_identity(selection), nested_manifest(drive_item()))

    def test_094_root_domain_not_supported(self):
        self.assertNotIn(root_core.GoogleDriveFolderManifest, (nested_core.GoogleDriveNestedFolderManifest, depth2_core.GoogleDriveDepth2FolderManifest))

    def test_095_nested_manifest_exact_type(self):
        self.assertIs(type(nested_manifest(drive_item())), nested_core.GoogleDriveNestedFolderManifest)

    def test_096_depth2_manifest_exact_type(self):
        self.assertIs(type(depth2_manifest(drive_item())), depth2_core.GoogleDriveDepth2FolderManifest)

    def test_097_factory_is_pure_function(self):
        self.assertEqual(handle().to_safe_dict(), handle().to_safe_dict())

    def test_098_no_open_call(self):
        tree = ast.parse(inspect.getsource(secure_core))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" for node in ast.walk(tree)))

    def test_099_production_count_not_hardcoded(self):
        tree = ast.parse(inspect.getsource(secure_core))
        self.assertNotIn(96, {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)})

    def test_100_no_report_or_env_access(self):
        source = inspect.getsource(secure_core)
        self.assertNotIn("load_local_json_report", source); self.assertNotIn("load_config", source)

    def test_101_product_source_retained(self):
        self.assertEqual(handle().product_source, SOURCE)

    def test_102_selection_warning_retained(self):
        value = selection_item(warnings=("selection_warning",))
        self.assertIn("selection_warning", handle(selection=value).warnings)

    def test_103_manifest_warning_retained(self):
        item = drive_item(); value = handle(item, manifest=nested_manifest(item, warnings=("manifest_warning",)))
        self.assertIn("manifest_warning", value.warnings)

    def test_104_item_warning_retained(self):
        item = drive_item(warnings=("item_warning",)); value = handle(item, manifest=nested_manifest(item))
        self.assertIn("item_warning", value.warnings)

    def test_105_capability_not_public(self):
        self.assertFalse(hasattr(handle(), "capability"))

    def test_106_no_public_provider_getter(self):
        self.assertFalse(hasattr(handle(), "get_provider_file_id"))

    def test_107_no_to_dict_with_id(self):
        self.assertFalse(hasattr(handle(), "to_dict_with_id"))

    def test_108_no_export_handle(self):
        self.assertFalse(hasattr(handle(), "export_handle"))

    def test_109_no_download_ready(self):
        self.assertNotIn("download_ready", handle().to_safe_dict())

    def test_110_no_upload_ready(self):
        self.assertNotIn("wordpress_upload_ready", handle().to_safe_dict())

    def test_111_handle_not_dataclass(self):
        self.assertFalse(dataclasses.is_dataclass(handle()))

    def test_112_handle_immutable(self):
        with self.assertRaisesRegex(AttributeError, "secure_media_handle_is_immutable"):
            handle().sku = "MOCK-002"

    def test_113_source_item_reference_not_public(self):
        self.assertFalse(hasattr(handle(), "source_item"))

    def test_114_mime_not_reclassified(self):
        item = drive_item(mime="image/png")
        self.assertEqual(handle(item, manifest=nested_manifest(item)).source_mime_type, "image/png")

    def test_115_null_size_retained(self):
        item = drive_item(size=None)
        self.assertIsNone(handle(item, manifest=nested_manifest(item)).size_bytes)

    def test_116_null_dimensions_retained(self):
        item = drive_item(width=None, height=None)
        value = handle(item, manifest=nested_manifest(item))
        self.assertEqual((value.image_width, value.image_height), (None, None))

    def test_117_nested_folder_case_exact(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item, folder="photos mock"))

    def test_118_depth2_parent_case_exact(self):
        item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, depth2_selection(), depth2_manifest(item, parent="photos mock"))

    def test_119_safe_name_case_exact(self):
        item = drive_item("PHOTO-1.JPG")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, selection_item("photo-1.jpg"), nested_manifest(item))

    def test_120_manifest_items_must_be_tuple(self):
        item = drive_item(); manifest = nested_manifest(item)
        object.__setattr__(manifest, "items", [item])
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=manifest)

    def test_121_selection_blockers_must_be_tuple(self):
        value = selection_item(); object.__setattr__(value, "blocking_issues", [])
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(selection=value)

    def test_122_invalid_warning_code_rejected(self):
        item = drive_item(warnings=("unsafe warning",))
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_123_provider_id_cannot_be_public_name(self):
        item = drive_item(RAW_ID)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, selection_item(RAW_ID), nested_manifest(item))

    def test_124_fingerprint_shape(self):
        self.assertRegex(handle().file_id_fingerprint, r"^[a-f0-9]{64}$")

    def test_125_false_image_candidate_rejected(self):
        item = drive_item(image_candidate=False)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_126_wrong_kind_with_true_flag_rejected(self):
        item = drive_item(kind="other_file", image_candidate=True)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, manifest=nested_manifest(item))

    def test_127_safe_dict_allowlist(self):
        self.assertEqual(set(handle().to_safe_dict()), {
            "policy_version", "sku", "product_source", "source_manifest_kind",
            "depth", "safe_folder_name", "parent_safe_folder_name", "safe_name",
            "file_id_fingerprint", "md5_checksum", "folder_role", "selection_position", "image_role",
            "source_mime_type", "size_bytes", "image_width", "image_height", "warnings",
        })

    def test_128_safe_dict_pickle_allowed(self):
        self.assertEqual(pickle.loads(pickle.dumps(handle().to_safe_dict())), handle().to_safe_dict())

    def test_129_gallery_zero_position_rejected(self):
        value = selection_item(image_role=selection_core.ImageSelectionRole.GALLERY, position=0)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(selection=value)

    def test_130_primary_nonzero_position_rejected(self):
        value = selection_item(position=1)
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(selection=value)

    def test_131_helper_rejects_tampered_public_audit(self):
        value = handle()
        object.__setattr__(value, "_sku", "MOCK-999")
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(value)

    def test_132_helper_rejects_tampered_source_identity(self):
        value = handle()
        source_item = object.__getattribute__(value, "_SecureSelectedMediaHandle__source_item")
        object.__setattr__(source_item, "provider_file_id", "opaque_changed_999")
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(value)

    def test_133_capability_cannot_bind_unowned_source_item(self):
        owned, unowned = drive_item(), drive_item(raw_id="opaque_unowned_999")
        selection = selection_item()
        value = secure_core.SecureSelectedMediaHandle(
            secure_core._HANDLE_CAPABILITY, selection_item=selection,
            baseline_identity=baseline_identity(selection, owned),
            source_manifest=nested_manifest(owned), source_item=unowned,
        )
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(value)

    def test_134_changed_file_fingerprint_blocks(self):
        selection = selection_item()
        old = drive_item(raw_id="old_file_identity_1")
        baseline = baseline_identity(selection, old)
        fresh = drive_item(raw_id="new_file_identity_2")
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_file_identity_changed"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_135_same_fingerprint_changed_md5_blocks(self):
        selection = selection_item(); old = drive_item()
        baseline = baseline_identity(selection, old)
        fresh = replace(old, md5_checksum="b" * 32)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_content_changed"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_136_same_fingerprint_same_md5_passes(self):
        selection = selection_item(); fresh = drive_item()
        baseline = baseline_identity(selection, replace(fresh, provider_file_id=None))
        self.assertIsInstance(
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh)),
            secure_core.SecureSelectedMediaHandle,
        )

    def test_137_baseline_md5_missing_blocks(self):
        selection = selection_item(); item = drive_item(); historical = replace(item, md5_checksum=None, provider_file_id=None)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_checksum_missing"):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(historical))

    def test_138_fresh_md5_missing_blocks(self):
        selection = selection_item(); expected = drive_item(); baseline = baseline_identity(selection, expected)
        fresh = replace(expected, md5_checksum=None)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "fresh_selected_media_checksum_missing"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_139_baseline_fingerprint_missing_blocks(self):
        selection = selection_item(); historical = drive_item(fingerprint=None)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_fingerprint_missing"):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(historical))

    def test_140_fresh_fingerprint_missing_blocks(self):
        selection = selection_item(); baseline = baseline_identity(selection, drive_item())
        fresh = drive_item(fingerprint=None)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "fresh_selected_media_fingerprint_missing"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_141_mime_drift_blocks(self):
        selection = selection_item(); old = drive_item(); baseline = baseline_identity(selection, old)
        fresh = replace(old, mime_type="image/png")
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_metadata_changed"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_142_size_drift_blocks(self):
        selection = selection_item(); old = drive_item(); baseline = baseline_identity(selection, old)
        fresh = replace(old, size_bytes=old.size_bytes + 1)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_metadata_changed"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_143_width_drift_blocks(self):
        selection = selection_item(); old = drive_item(); baseline = baseline_identity(selection, old)
        fresh = replace(old, image_width=old.image_width + 1)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_metadata_changed"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_144_height_drift_blocks(self):
        selection = selection_item(); old = drive_item(); baseline = baseline_identity(selection, old)
        fresh = replace(old, image_height=old.image_height + 1)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_metadata_changed"):
            create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))

    def test_145_modified_time_drift_allowed(self):
        selection = selection_item(); old = drive_item(); baseline = baseline_identity(selection, old)
        fresh = replace(old, modified_time="2026-12-31T23:59:59Z")
        result = create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))
        self.assertEqual(result.md5_checksum, old.md5_checksum)

    def test_146_baseline_safe_dict_no_raw_id(self):
        selection = selection_item(); historical = drive_item()
        baseline = baseline_identity(selection, historical)
        self.assertNotIn(RAW_ID, json.dumps(baseline.to_safe_dict(), sort_keys=True))

    def test_147_baseline_repr_no_raw_id(self):
        selection = selection_item(); historical = drive_item()
        baseline = baseline_identity(selection, historical)
        self.assertNotIn(RAW_ID, repr(baseline))

    def test_148_baseline_has_no_provider_authority(self):
        baseline = baseline_identity()
        self.assertFalse(hasattr(baseline, "provider_file_id"))
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(baseline)

    def test_149_fresh_item_only_raw_id_authority(self):
        selection = selection_item(); historical = replace(drive_item(), provider_file_id=None)
        baseline = baseline_identity(selection, historical)
        fresh = drive_item()
        result = create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))
        self.assertEqual(secure_core._provider_file_id_for_download(result), RAW_ID)

    def test_150_handle_requires_baseline_argument(self):
        with self.assertRaises(TypeError):
            create_secure_selected_media_handle(selection_item(), nested_manifest(drive_item()))

    def test_151_fresh_item_cannot_self_authorize(self):
        selection = selection_item(); fresh = drive_item(); manifest = nested_manifest(fresh)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_identity_required"):
            create_secure_selected_media_handle(selection, fresh, manifest)

    def test_152_helper_rechecks_baseline_fingerprint(self):
        result = handle()
        baseline = object.__getattribute__(result, "_SecureSelectedMediaHandle__baseline_identity")
        object.__setattr__(baseline, "_file_id_fingerprint", "0" * 64)
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(result)

    def test_153_helper_rechecks_baseline_md5(self):
        result = handle()
        baseline = object.__getattribute__(result, "_SecureSelectedMediaHandle__baseline_identity")
        object.__setattr__(baseline, "_md5_checksum", "b" * 32)
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core._provider_file_id_for_download(result)

    def test_154_ninety_six_baseline_fresh_handles(self):
        results = []
        for index in range(96):
            selection = selection_item(f"photo-{index}.jpg")
            fresh = drive_item(selection.safe_name, raw_id=f"fresh_identity_{index}")
            baseline = baseline_identity(selection, fresh)
            results.append(create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh)))
        self.assertEqual(len(results), 96)

    def test_155_one_changed_fingerprint_among_ninety_six_blocks(self):
        blocked = 0
        for index in range(96):
            selection = selection_item(f"photo-{index}.jpg")
            old = drive_item(selection.safe_name, raw_id=f"old_identity_{index}")
            baseline = baseline_identity(selection, old)
            fresh = old if index != 47 else drive_item(selection.safe_name, raw_id="replacement_identity_47")
            try:
                create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))
            except SecureSelectedMediaHandleError as error:
                self.assertEqual(str(error), "selected_media_file_identity_changed")
                blocked += 1
        self.assertEqual(blocked, 1)

    def test_156_one_changed_md5_among_ninety_six_blocks(self):
        blocked = 0
        for index in range(96):
            selection = selection_item(f"photo-{index}.jpg")
            old = drive_item(selection.safe_name, raw_id=f"stable_identity_{index}")
            baseline = baseline_identity(selection, old)
            fresh = old if index != 63 else replace(old, md5_checksum="c" * 32)
            try:
                create_secure_selected_media_handle(selection, baseline, nested_manifest(fresh))
            except SecureSelectedMediaHandleError as error:
                self.assertEqual(str(error), "selected_media_content_changed")
                blocked += 1
        self.assertEqual(blocked, 1)

    def test_157_baseline_missing(self):
        selection = selection_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_missing"):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(drive_item("other.jpg")))

    def test_158_baseline_ambiguous(self):
        selection = selection_item(); first = drive_item(); second = drive_item(raw_id="other_identity_2")
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_ambiguous"):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(first, second))

    def test_159_baseline_image_required(self):
        selection = selection_item(); item = drive_item(kind="other_file", image_candidate=False)
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_not_image_candidate"):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(item))

    def test_160_baseline_direct_constructor_guard(self):
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_factory_required"):
            secure_core.SelectedMediaBaselineIdentity()

    def test_161_baseline_safe_schema(self):
        self.assertEqual(set(baseline_identity().to_safe_dict()), {
            "policy_version", "sku", "product_source", "source_manifest_kind",
            "depth", "safe_folder_name", "parent_safe_folder_name", "safe_name",
            "file_id_fingerprint", "md5_checksum", "source_mime_type",
            "size_bytes", "image_width", "image_height",
        })

    def test_162_handle_safe_dict_md5(self):
        self.assertEqual(handle().to_safe_dict()["md5_checksum"], "a" * 32)

    def test_163_baseline_selection_sku_exact(self):
        selection = selection_item(); item = drive_item()
        with self.assertRaisesRegex(SecureSelectedMediaHandleError, "selected_media_baseline_provenance_mismatch"):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(item, sku="MOCK-002"))

    def test_164_baseline_hierarchy_exact(self):
        selection = selection_item(); item = drive_item()
        with self.assertRaises(SecureSelectedMediaHandleError):
            secure_core.create_selected_media_baseline_identity(selection, nested_manifest(item, folder="Photos Other"))

    def test_165_baseline_can_use_rehydrated_item_without_raw_id(self):
        selection = selection_item(); historical = replace(drive_item(), provider_file_id=None)
        baseline = secure_core.create_selected_media_baseline_identity(selection, nested_manifest(historical))
        self.assertEqual(baseline.file_id_fingerprint, root_core.fingerprint_drive_id(RAW_ID))


def _make_manifest_name_mismatch_test(index):
    def test(self):
        item = drive_item(f"photo-{index}.jpg", raw_id=f"opaque_dynamic_{index}")
        with self.assertRaises(SecureSelectedMediaHandleError):
            handle(item, selection_item(f"other-{index}.jpg"), nested_manifest(item))
    return test


for _index in range(1, 11):
    setattr(SecureSelectedMediaHandleTests, f"test_dynamic_exact_name_{_index:02}", _make_manifest_name_mismatch_test(_index))
