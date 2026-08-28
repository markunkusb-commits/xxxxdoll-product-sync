from __future__ import annotations

import ast
import builtins
import copy
import inspect
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import image_asset_type_policy as policy
from sync_worker.folder_role_policy import FolderRole
from sync_worker.google_drive_folder_manifest import _manifest_item
from sync_worker.image_asset_type_policy import (
    POLICY_VERSION, AssetClass, ImageAssetTypePolicyError, ImageAssetTypeResult,
    classify_image_asset_type,
)


def classify(mime="image/jpeg", name="mock.jpg", **audit):
    return classify_image_asset_type(mime, name, **audit)


def decision(result):
    return (
        result.asset_class, result.normalized_mime_type, result.safe_extension,
        result.storefront_eligible, result.classification_source, result.status,
        result.warnings, result.blocking_issues, result.policy_version,
    )


class ImageAssetTypePolicyTests(unittest.TestCase):
    def setUp(self):
        self.denied = []
        for target in (
            "socket.socket.connect", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "sync_worker.google_api.OfficialGoogleClientFactory",
            "sync_worker.google_api.GoogleDriveMetadataGateway.list_folder_children",
            "sync_worker.google_api.ReadOnlyGoogleGateway.get_folder",
            "sync_worker.http_client.ReadOnlyHttpClient.request",
            "sync_worker.config.load_config", "sync_worker.config.load_google_config",
            "sync_worker.config.load_google_drive_metadata_config",
            "sync_worker.config.load_google_sheets_readonly_config",
        ):
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Metadata fixtures only; no external access"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def assert_class(self, mime, name, asset_class):
        result = classify(mime, name)
        self.assertEqual(result.asset_class, asset_class)
        self.assertEqual(result.blocking_issues, ())
        return result

    def test_01_jpeg_class(self):
        self.assert_class("image/jpeg", "mock.jpg", AssetClass.WEB_IMAGE)

    def test_02_png_class(self):
        self.assert_class("image/png", "mock.png", AssetClass.WEB_IMAGE)

    def test_03_webp_class(self):
        self.assert_class("image/webp", "mock.webp", AssetClass.WEB_IMAGE)

    def test_04_jpeg_storefront_true(self):
        self.assertTrue(classify().storefront_eligible)

    def test_05_png_storefront_true(self):
        self.assertTrue(classify("image/png", "mock.png").storefront_eligible)

    def test_06_webp_storefront_true(self):
        self.assertTrue(classify("image/webp", "mock.webp").storefront_eligible)

    def test_07_gif_class(self):
        self.assert_class("image/gif", "mock.gif", AssetClass.WEB_IMAGE)

    def test_08_gif_requires_platform_verification(self):
        result = classify("image/gif", "mock.gif")
        self.assertFalse(result.storefront_eligible)
        self.assertEqual(result.warnings, ("web_image_format_requires_platform_verification",))

    def test_09_avif_class(self):
        self.assert_class("image/avif", "mock.avif", AssetClass.WEB_IMAGE)

    def test_10_avif_requires_platform_verification(self):
        result = classify("image/avif", "mock.avif")
        self.assertFalse(result.storefront_eligible)
        self.assertEqual(result.warnings, ("web_image_format_requires_platform_verification",))

    def test_11_photoshop_mime(self):
        self.assertEqual(classify("image/vnd.adobe.photoshop", "mock.psd").normalized_mime_type, "image/vnd.adobe.photoshop")

    def test_12_psd_design_source(self):
        self.assert_class("image/vnd.adobe.photoshop", "mock.psd", AssetClass.DESIGN_SOURCE)

    def test_13_psd_not_storefront(self):
        self.assertFalse(classify("image/vnd.adobe.photoshop", "mock.psd").storefront_eligible)

    def test_14_mp4_video(self):
        self.assert_class("video/mp4", "mock.mp4", AssetClass.VIDEO)

    def test_15_webm_video(self):
        self.assert_class("video/webm", "mock.webm", AssetClass.VIDEO)

    def test_16_video_never_storefront(self):
        for mime in ("video/mp4", "video/webm", "video/quicktime", "video/x-mock-format"):
            with self.subTest(mime=mime):
                self.assertFalse(classify(mime, "mock-video").storefront_eligible)

    def test_17_pdf_other_media(self):
        self.assert_class("application/pdf", "mock.pdf", AssetClass.OTHER_MEDIA)

    def test_18_audio_other_media(self):
        for mime in ("audio/mpeg", "audio/wav", "audio/ogg", "audio/x-mock-format"):
            with self.subTest(mime=mime):
                self.assert_class(mime, "mock-audio", AssetClass.OTHER_MEDIA)

    def test_19_other_media_never_storefront(self):
        for mime in ("application/pdf", "audio/mpeg", "audio/mp4"):
            self.assertFalse(classify(mime, "mock-media").storefront_eligible)

    def test_20_missing_mime(self):
        result = self.assert_class(None, "mock", AssetClass.UNKNOWN)
        self.assertIsNone(result.normalized_mime_type)
        self.assertFalse(result.storefront_eligible)

    def test_21_malformed_mime(self):
        for mime in ("jpeg", "image/", "/jpeg", "image//jpeg", "image / jpeg", "image/*", "image/jpeg; charset=binary"):
            with self.subTest(mime=mime):
                result = self.assert_class(mime, "mock.jpg", AssetClass.UNKNOWN)
                self.assertIsNone(result.normalized_mime_type)
                self.assertEqual(result.classification_source, "unknown")

    def test_22_unknown_warning_not_blocker(self):
        result = classify("malformed", "mock")
        self.assertEqual(result.warnings, ("asset_mime_unknown",))
        self.assertEqual(result.blocking_issues, ())

    def test_23_uppercase_mime_normalizes(self):
        result = classify("IMAGE/JPEG", "mock.JPG")
        self.assertEqual(result.normalized_mime_type, "image/jpeg")
        self.assertTrue(result.storefront_eligible)

    def test_24_outer_whitespace_normalizes(self):
        result = classify(" \t\r\nimage/png \n", "mock.png")
        self.assertEqual(result.normalized_mime_type, "image/png")
        self.assertEqual(result.warnings, ())

    def test_25_jpg_extension(self):
        self.assertEqual(classify().safe_extension, ".jpg")

    def test_26_jpeg_extension(self):
        self.assertEqual(classify(name="mock.JPEG").safe_extension, ".jpeg")

    def test_27_png_extension(self):
        self.assertEqual(classify("image/png", "mock.PNG").safe_extension, ".png")

    def test_28_webp_extension(self):
        self.assertEqual(classify("image/webp", "mock.WEBP").safe_extension, ".webp")

    def test_29_psd_extension(self):
        self.assertEqual(classify("image/vnd.adobe.photoshop", "mock.PSD").safe_extension, ".psd")

    def test_30_mp4_extension(self):
        self.assertEqual(classify("video/mp4", "mock.MP4").safe_extension, ".mp4")

    def test_31_mime_takes_precedence(self):
        result = classify("application/pdf", "mock.jpg")
        self.assertEqual(result.asset_class, AssetClass.OTHER_MEDIA)
        self.assertEqual(result.classification_source, "mime")
        self.assertFalse(result.storefront_eligible)

    def test_32_jpeg_mime_psd_name(self):
        result = classify("image/jpeg", "mock.psd")
        self.assertEqual(result.asset_class, AssetClass.WEB_IMAGE)
        self.assertTrue(result.storefront_eligible)
        self.assertEqual(result.normalized_mime_type, "image/jpeg")
        self.assertIn("asset_extension_mime_mismatch", result.warnings)

    def test_33_psd_mime_jpg_name(self):
        result = classify("image/vnd.adobe.photoshop", "mock.jpg")
        self.assertEqual(result.asset_class, AssetClass.DESIGN_SOURCE)
        self.assertFalse(result.storefront_eligible)
        self.assertIn("asset_extension_mime_mismatch", result.warnings)

    def test_34_mismatch_warning_once(self):
        result = classify("image/jpeg", "mock.webp")
        self.assertEqual(result.warnings, ("asset_extension_mime_mismatch",))
        self.assertEqual(result.blocking_issues, ())

    def test_35_octet_stream_jpg_fallback(self):
        result = classify("application/octet-stream", "mock.jpg")
        self.assertEqual(result.asset_class, AssetClass.WEB_IMAGE)
        self.assertEqual(result.classification_source, "extension_fallback")
        self.assertEqual(result.status, "extension_fallback_candidate")
        self.assertEqual(result.normalized_mime_type, "application/octet-stream")
        self.assertEqual(result.warnings, ("mime_verification_required",))

    def test_36_octet_stream_psd_fallback(self):
        result = classify("application/octet-stream", "mock.psd")
        self.assertEqual(result.asset_class, AssetClass.DESIGN_SOURCE)
        self.assertEqual(result.classification_source, "extension_fallback")

    def test_37_fallbacks_never_storefront(self):
        for mime in (None, "", "application/octet-stream"):
            for extension in ("jpg", "jpeg", "png", "webp", "psd", "mp4"):
                with self.subTest(mime=mime, extension=extension):
                    result = classify(mime, "mock." + extension)
                    self.assertFalse(result.storefront_eligible)
                    self.assertIn("mime_verification_required", result.warnings)

    def test_38_no_fuzzy_mime_repair(self):
        for mime in ("image/jpg", "image/jepg", "image/x-photoshop", "application/x-photoshop", "image/x-png", "images/jpeg"):
            with self.subTest(mime=mime):
                result = classify(mime, "mock.jpg")
                self.assertEqual(result.asset_class, AssetClass.UNSUPPORTED)
                self.assertFalse(result.storefront_eligible)
                self.assertEqual(result.classification_source, "mime")

    def test_39_sku_audit_only(self):
        original = classify()
        result = classify(sku="MOCK-PSD-VIDEO")
        self.assertEqual(decision(original), decision(result))
        self.assertEqual(result.sku, "MOCK-PSD-VIDEO")

    def test_40_folder_role_audit_only(self):
        for role in FolderRole:
            with self.subTest(role=role):
                result = classify(folder_role=role)
                self.assertEqual(decision(classify()), decision(result))
                self.assertEqual(result.folder_role, role)

    def test_41_size_does_not_change_type(self):
        for size in (None, 0, 1, 10**12):
            result = classify(size_bytes=size)
            self.assertEqual(decision(result), decision(classify()))
            self.assertEqual(result.size_bytes, size)

    def test_42_dimensions_do_not_change_type(self):
        for width, height in ((None, None), (0, 0), (1, 1), (0, 4000), (100000, 100000)):
            result = classify(image_width=width, image_height=height)
            self.assertEqual(decision(result), decision(classify()))
            self.assertEqual((result.image_width, result.image_height), (width, height))

    def test_43_input_not_mutated(self):
        metadata = {"mime_type": " IMAGE/JPEG ", "safe_name": "mock.JPG", "size_bytes": 123,
                    "image_width": 0, "image_height": 0, "sku": "MOCK-001", "folder_role": "banner"}
        original = copy.deepcopy(metadata)
        classify_image_asset_type(**metadata)
        self.assertEqual(metadata, original)

    def test_44_deterministic(self):
        first = classify("image/jpeg", "mock.psd", sku="MOCK", folder_role="banner")
        second = classify("image/jpeg", "mock.psd", sku="MOCK", folder_role="banner")
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True), json.dumps(second.to_dict(), sort_keys=True))

    def test_45_policy_version(self):
        self.assertEqual(POLICY_VERSION, "xxxxdoll-image-asset-type-v1")
        self.assertEqual(classify().policy_version, POLICY_VERSION)
        self.assertFalse(next(item for item in fields(ImageAssetTypeResult) if item.name == "policy_version").init)

    def test_46_classification_sources(self):
        self.assertEqual(classify().classification_source, "mime")
        self.assertEqual(classify(None, "mock.jpg").classification_source, "extension_fallback")
        self.assertEqual(classify(None, "mock").classification_source, "unknown")

    def test_47_no_raw_id_input(self):
        for key in ("provider_file_id", "raw_file_id", "file_id"):
            with self.subTest(key=key), self.assertRaises(TypeError) as caught:
                classify_image_asset_type("image/jpeg", "mock.jpg", **{key: "MOCK_RAW_ID"})
            self.assertNotIn("MOCK_RAW_ID", str(caught.exception))

    def test_48_no_fingerprint_input(self):
        with self.assertRaises(TypeError):
            classify_image_asset_type("image/jpeg", "mock.jpg", file_id_fingerprint="MOCK_FINGERPRINT")

    def test_49_no_url_or_download_link_input(self):
        for key in ("url", "drive_url", "download_link"):
            with self.subTest(key=key), self.assertRaises(TypeError):
                classify_image_asset_type("image/jpeg", "mock.jpg", **{key: "https://example.invalid/mock.jpg"})

    def test_50_no_network(self):
        classify()
        for operation in self.denied[:3]:
            operation.assert_not_called()

    def test_51_no_drive_call(self):
        classify("image/vnd.adobe.photoshop", "mock.psd")
        for operation in self.denied[4:7]:
            operation.assert_not_called()

    def test_52_no_download(self):
        classify(None, "mock.jpg")
        self.denied[3].assert_not_called()

    def test_53_no_writes(self):
        with patch("builtins.open", side_effect=AssertionError("No file writes")) as opened, patch.object(Path, "write_text", side_effect=AssertionError("No reports")) as write:
            classify().to_dict()
        opened.assert_not_called()
        write.assert_not_called()
        self.denied[7].assert_not_called()

    def test_54_no_pillow_or_content_library(self):
        imports = {alias.name.split(".")[0] for node in ast.walk(ast.parse(inspect.getsource(policy)))
                   if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.split(".")[0] for node in ast.walk(ast.parse(inspect.getsource(policy)))
                       if isinstance(node, ast.ImportFrom) and node.module)
        self.assertTrue(imports.isdisjoint({"PIL", "Pillow", "magic", "mimetypes", "requests", "httpx", "googleapiclient", "google_api"}))

    def test_55_no_file_content_reads(self):
        with patch("builtins.open", side_effect=AssertionError("No content reads")) as opened, patch("io.open", side_effect=AssertionError("No content reads")) as io_open, patch.object(Path, "read_bytes", side_effect=AssertionError("No content reads")) as read:
            classify("image/jpeg", "mock.jpg", image_width=0, image_height=0).to_dict()
        opened.assert_not_called()
        io_open.assert_not_called()
        read.assert_not_called()

    def test_56_jpeg_metadata_candidate_only(self):
        self.assertEqual(classify().status, "metadata_web_image")

    def test_57_no_verified_image_claim(self):
        output = json.dumps(classify().to_dict())
        self.assertNotIn("verified", output)
        self.assertNotIn("content", output)
        self.assertNotIn("download_ready", output)

    def test_58_jpeg_in_banner_stays_type_eligible(self):
        result = classify(folder_role="banner")
        self.assertTrue(result.storefront_eligible)
        self.assertEqual(result.asset_class, AssetClass.WEB_IMAGE)

    def test_59_psd_discovery_candidate_remains_separate(self):
        item = _manifest_item({"name": "mock.psd", "mimeType": "image/vnd.adobe.photoshop", "size": "42"})
        self.assertTrue(item.image_candidate)
        self.assertEqual(item.item_kind, "image_candidate")
        result = classify_image_asset_type(item.mime_type, item.safe_name, size_bytes=item.size_bytes)
        self.assertEqual(result.asset_class, AssetClass.DESIGN_SOURCE)
        self.assertFalse(result.storefront_eligible)
        self.assertTrue(item.image_candidate)

    def test_60_postscript_is_design_source(self):
        result = self.assert_class("application/postscript", "mock.eps", AssetClass.DESIGN_SOURCE)
        self.assertFalse(result.storefront_eligible)
        self.assertEqual(result.warnings, ())

    def test_61_unknown_image_types_unsupported(self):
        for mime in ("image/tiff", "image/bmp", "image/svg+xml", "image/heic", "image/x-mock"):
            with self.subTest(mime=mime):
                result = self.assert_class(mime, "mock", AssetClass.UNSUPPORTED)
                self.assertFalse(result.storefront_eligible)

    def test_62_known_nonmedia_types_unsupported(self):
        for mime in ("application/zip", "text/plain", "application/vnd.google-apps.folder"):
            self.assert_class(mime, "mock", AssetClass.UNSUPPORTED)

    def test_63_unsupported_warning_not_blocker(self):
        result = classify("application/zip", "mock.zip")
        self.assertEqual(result.warnings, ("asset_type_unsupported",))
        self.assertEqual(result.blocking_issues, ())

    def test_64_empty_mime_without_fallback(self):
        for mime in ("", " \t\r\n"):
            result = classify(mime, "mock")
            self.assertEqual(result.asset_class, AssetClass.UNKNOWN)
            self.assertEqual(result.warnings, ("asset_mime_unknown",))

    def test_65_non_text_mime_nonblocking_unknown(self):
        for mime in (0, False, [], {}, b"image/jpeg"):
            with self.subTest(mime=mime):
                result = classify(mime, "mock.jpg")
                self.assertEqual(result.asset_class, AssetClass.UNKNOWN)
                self.assertEqual(result.warnings, ("asset_mime_unknown",))
                self.assertEqual(result.blocking_issues, ())

    def test_66_unicode_mime_lookalikes_not_repaired(self):
        for mime in ("ｉｍａｇｅ/jpeg", "image／jpeg", "image/ｊｐｅｇ"):
            self.assertEqual(classify(mime).asset_class, AssetClass.UNKNOWN)

    def test_67_no_extension_does_not_change_valid_mime(self):
        result = classify(name="mock")
        self.assertTrue(result.storefront_eligible)
        self.assertIsNone(result.safe_extension)
        self.assertEqual(result.warnings, ())

    def test_68_hidden_name_is_not_extension_fallback(self):
        self.assertIsNone(classify(None, ".jpg").safe_extension)
        self.assertEqual(classify(None, ".jpg").asset_class, AssetClass.UNKNOWN)

    def test_69_only_last_extension_used(self):
        result = classify(None, "mock.jpg.psd")
        self.assertEqual(result.safe_extension, ".psd")
        self.assertEqual(result.asset_class, AssetClass.DESIGN_SOURCE)

    def test_70_unknown_extension_audit_only(self):
        result = classify(name="mock.custom")
        self.assertEqual(result.safe_extension, ".custom")
        self.assertTrue(result.storefront_eligible)
        self.assertEqual(result.warnings, ())

    def test_71_missing_mime_fallback_retains_unknown_warning(self):
        result = classify(None, "mock.jpg")
        self.assertIsNone(result.normalized_mime_type)
        self.assertEqual(result.warnings, ("asset_mime_unknown", "mime_verification_required"))
        self.assertEqual(result.status, "extension_fallback_candidate")

    def test_72_octet_stream_mp4_fallback(self):
        result = classify("application/octet-stream", "mock.mp4")
        self.assertEqual(result.asset_class, AssetClass.VIDEO)
        self.assertFalse(result.storefront_eligible)
        self.assertEqual(result.classification_source, "extension_fallback")

    def test_73_fallback_allowlist_closed(self):
        for extension in ("gif", "avif", "pdf", "eps", "webm", "tiff", "bmp", "zip"):
            with self.subTest(extension=extension):
                result = classify(None, "mock." + extension)
                self.assertEqual(result.asset_class, AssetClass.UNKNOWN)
                self.assertEqual(result.classification_source, "unknown")

    def test_74_generic_mime_without_approved_extension_unsupported(self):
        result = classify("application/octet-stream", "mock.bin")
        self.assertEqual(result.asset_class, AssetClass.UNSUPPORTED)
        self.assertEqual(result.classification_source, "mime")
        self.assertFalse(result.storefront_eligible)

    def test_75_malformed_mime_does_not_enable_extension_fallback(self):
        result = classify("image /jpeg", "mock.jpg")
        self.assertEqual(result.asset_class, AssetClass.UNKNOWN)
        self.assertNotIn("mime_verification_required", result.warnings)

    def test_76_explicit_unsupported_mime_not_overridden(self):
        result = classify("application/zip", "mock.psd")
        self.assertEqual(result.asset_class, AssetClass.UNSUPPORTED)
        self.assertEqual(result.classification_source, "mime")
        self.assertIn("asset_extension_mime_mismatch", result.warnings)

    def test_77_matching_extension_no_mismatch(self):
        for mime, name in (("image/jpeg", "mock.jpeg"), ("image/png", "mock.png"), ("image/webp", "mock.webp"),
                           ("image/vnd.adobe.photoshop", "mock.psd"), ("video/mp4", "mock.mp4"), ("application/pdf", "mock.pdf")):
            self.assertNotIn("asset_extension_mime_mismatch", classify(mime, name).warnings)

    def test_78_multiple_extensions_supported_for_same_mime(self):
        for name in ("mock.jpg", "mock.jpeg"):
            self.assertEqual(classify(name=name).warnings, ())
        self.assertNotIn("asset_extension_mime_mismatch", classify("audio/mp4", "mock.mp4").warnings)

    def test_79_platform_and_mismatch_warnings_both_retained(self):
        result = classify("image/gif", "mock.jpg")
        self.assertEqual(result.warnings, ("web_image_format_requires_platform_verification", "asset_extension_mime_mismatch"))
        self.assertFalse(result.storefront_eligible)

    def test_80_psd_in_storefront_folder_stays_ineligible(self):
        result = classify("image/vnd.adobe.photoshop", "mock.psd", folder_role="storefront_photos")
        self.assertFalse(result.storefront_eligible)
        self.assertEqual(result.asset_class, AssetClass.DESIGN_SOURCE)

    def test_81_quality_metadata_cannot_promote_unsupported(self):
        result = classify("image/tiff", "mock.tiff", size_bytes=9000000, image_width=10000, image_height=10000)
        self.assertFalse(result.storefront_eligible)
        self.assertEqual(result.asset_class, AssetClass.UNSUPPORTED)

    def test_82_invalid_audit_numbers_rejected_without_echo(self):
        for field in ("size_bytes", "image_width", "image_height"):
            for value in (-1, True, 1.5, "MOCK_PRIVATE_VALUE"):
                with self.subTest(field=field, value=value), self.assertRaises(ImageAssetTypePolicyError) as caught:
                    classify(**{field: value})
                self.assertEqual(str(caught.exception), "invalid_" + field)

    def test_83_result_is_frozen(self):
        result = classify()
        with self.assertRaises(FrozenInstanceError):
            result.storefront_eligible = False
        with self.assertRaises(FrozenInstanceError):
            result.policy_version = "changed"

    def test_84_json_projection_independent_of_result(self):
        result = classify("image/jpeg", "mock.psd")
        projection = result.to_dict()
        projection["warnings"].append("changed")
        projection["sku"] = "changed"
        self.assertEqual(result.warnings, ("asset_extension_mime_mismatch",))
        self.assertIsNone(result.sku)
        self.assertEqual(json.loads(json.dumps(result.to_dict())), result.to_dict())

    def test_85_output_contains_only_safe_metadata(self):
        expected = {"asset_class", "policy_version", "normalized_mime_type", "safe_extension", "storefront_eligible",
                    "classification_source", "status", "safe_name", "size_bytes", "image_width", "image_height",
                    "sku", "folder_role", "warnings", "blocking_issues"}
        self.assertEqual(set(classify().to_dict()), expected)

    def test_86_url_in_name_rejected_without_echo(self):
        name = "https://drive.google.com/file/d/MOCK_ID/view?resourcekey=MOCK_KEY"
        with self.assertRaises(ImageAssetTypePolicyError) as caught:
            classify(name=name)
        self.assertEqual(str(caught.exception), "unsafe_safe_name")
        self.assertNotIn("MOCK_ID", str(caught.exception))

    def test_87_paths_rejected_without_opening(self):
        for name in (r"C:\mock\private.json", "../mock.jpg", r"\\mock\share\image.jpg", "data:image/png;base64,MOCK"):
            with self.subTest(name=name), patch("builtins.open", side_effect=AssertionError("No content reads")) as opened:
                with self.assertRaises(ImageAssetTypePolicyError):
                    classify(name=name)
                opened.assert_not_called()

    def test_88_secrets_rejected_from_audit_text(self):
        for field in ("safe_name", "sku", "folder_role"):
            for value in ("ck_" + "x" * 30, "cs_" + "y" * 30, "token=MOCK", "resource_key=MOCK", "Authorization: MOCK", "Cookie: MOCK", "mock-service@example.invalid"):
                metadata = {"mime_type": "image/jpeg", "safe_name": "mock.jpg", field: value}
                with self.subTest(field=field, value=value), self.assertRaises(ImageAssetTypePolicyError) as caught:
                    classify_image_asset_type(**metadata)
                self.assertNotIn(value, str(caught.exception))

    def test_89_unsafe_malformed_mime_not_retained(self):
        for mime in ("https://example.invalid/MOCK_PRIVATE", "private_key=MOCK_KEY", "Cookie: MOCK"):
            result = classify(mime, "mock")
            self.assertIsNone(result.normalized_mime_type)
            self.assertNotIn("MOCK", json.dumps(result.to_dict()))

    def test_90_credential_like_valid_mime_rejected(self):
        with self.assertRaisesRegex(ImageAssetTypePolicyError, "unsafe_mime_type"):
            classify("image/ck_" + "x" * 30, "mock")

    def test_91_safe_unicode_name_retained(self):
        result = classify(name="测试素材-示例.JPG")
        self.assertEqual(result.safe_name, "测试素材-示例.JPG")
        self.assertEqual(result.safe_extension, ".jpg")
        self.assertTrue(result.storefront_eligible)

    def test_92_no_clock_randomness_or_cli_dependencies(self):
        source = inspect.getsource(policy)
        imports = {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(imports.intersection({"cli", "google_api", "config", "folder_role_policy", "google_drive_folder_manifest"}))
        output = classify().to_dict()
        for name in ("timestamp", "uuid", "random", "gallery", "images", "selected"):
            self.assertNotIn(name, output)

    def test_93_no_pillow_import_during_classification(self):
        original_import = builtins.__import__
        def safe_import(name, *args, **kwargs):
            if name.split(".")[0] in {"PIL", "magic", "mimetypes"}:
                raise AssertionError("No file inspection libraries")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=safe_import):
            self.assertTrue(classify().storefront_eligible)

    def test_94_all_six_asset_classes_exposed(self):
        self.assertEqual({item.value for item in AssetClass}, {"web_image", "design_source", "video", "other_media", "unsupported", "unknown"})


if __name__ == "__main__":
    unittest.main()
