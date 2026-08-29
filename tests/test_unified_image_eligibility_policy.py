from __future__ import annotations

import ast
import copy
import inspect
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest.mock import PropertyMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import folder_role_policy as folders, image_asset_type_policy as assets
from sync_worker import webp_output_policy as webp, unified_image_eligibility_policy as policy
from sync_worker.unified_image_eligibility_policy import (
    EligibilityReason, UnifiedImageEligibilityPolicyError, UnifiedImageEligibilityResult,
    evaluate_unified_image_eligibility,
)


def folder(name="Photos-Mock", **overrides):
    return replace(folders.classify_folder_role(name), **overrides)


def source(mime="image/jpeg", **overrides):
    return replace(webp.evaluate_webp_output_policy(assets.classify_image_asset_type(mime, "mock-asset")), **overrides)


def evaluate(name="Photos-Mock", mime="image/jpeg"):
    return evaluate_unified_image_eligibility(folder(name), source(mime))


class UnifiedImageEligibilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.denied = []
        for target in (
            "socket.socket.connect", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "sync_worker.google_api.OfficialGoogleClientFactory",
            "sync_worker.google_api.GoogleDriveMetadataGateway.list_folder_children",
            "sync_worker.http_client.ReadOnlyHttpClient.request",
            "sync_worker.config.load_config", "sync_worker.config.load_google_config",
            "sync_worker.config.load_google_drive_metadata_config",
            "sync_worker.config.load_google_sheets_readonly_config",
            "subprocess.run", "subprocess.Popen", "os.system",
        ):
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Pure mock policy; no I/O"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def assert_eligible(self, result):
        self.assertIs(result.unified_image_eligible, True)
        self.assertTrue(result.folder_gallery_eligible)
        self.assertTrue(result.source_asset_eligible)
        self.assertTrue(result.requires_webp_pipeline)
        self.assertIn(result.webp_action, (webp.WebPAction.CONVERT_TO_WEBP, webp.WebPAction.VALIDATE_EXISTING_WEBP))
        self.assertEqual((result.target_mime_type, result.target_extension), ("image/webp", ".webp"))
        self.assertEqual(result.blocking_issues, ())

    def assert_denied(self, result, reason=None):
        self.assertIs(result.unified_image_eligible, False)
        if reason is not None:
            self.assertEqual(result.eligibility_reason, reason)

    def test_01_storefront_jpeg_eligible(self):
        self.assert_eligible(evaluate())

    def test_02_storefront_png_eligible(self):
        self.assert_eligible(evaluate(mime="image/png"))

    def test_03_storefront_webp_eligible(self):
        self.assert_eligible(evaluate(mime="image/webp"))

    def test_04_factory_jpeg_eligible(self):
        self.assert_eligible(evaluate("Factory Photos-Mock"))

    def test_05_factory_png_eligible(self):
        self.assert_eligible(evaluate("Factory Photos-Mock", "image/png"))

    def test_06_factory_webp_eligible(self):
        self.assert_eligible(evaluate("Factory Photos-Mock", "image/webp"))

    def test_07_banner_jpeg_false_despite_convertible_source(self):
        result = evaluate("Banner-Mock")
        self.assert_denied(result)
        self.assertTrue(result.source_asset_eligible)
        self.assertTrue(result.requires_webp_pipeline)
        self.assertEqual(result.webp_action, webp.WebPAction.CONVERT_TO_WEBP)

    def test_08_banner_webp_false(self):
        self.assert_denied(evaluate("Banner-Mock", "image/webp"))

    def test_09_video_folder_false(self):
        self.assert_denied(evaluate("Factory Videos-Mock"))
        self.assert_denied(evaluate("Factory Videos-Mock", "video/mp4"))

    def test_10_eye_options_false(self):
        self.assert_denied(evaluate("Eye Options-Mock"))

    def test_11_promo_assets_false(self):
        self.assert_denied(evaluate("Promo Assets-Mock"))

    def test_12_other_skin_tone_false(self):
        self.assert_denied(evaluate("Other Skin Tone Factory Photos-Mock"))

    def test_13_unknown_role_false(self):
        self.assert_denied(evaluate("Mock Collection"))

    def test_14_photos_psd_false(self):
        self.assert_denied(evaluate(mime="image/vnd.adobe.photoshop"))

    def test_15_factory_photos_psd_false(self):
        self.assert_denied(evaluate("Factory Photos-Mock", "image/vnd.adobe.photoshop"))

    def test_16_gallery_role_video_false(self):
        for mime in ("video/quicktime", "video/mp4"):
            self.assert_denied(evaluate(mime=mime))

    def test_17_gallery_role_unsupported_false(self):
        self.assert_denied(evaluate(mime="application/octet-stream"))

    def test_18_missing_folder_role_false(self):
        self.assert_denied(evaluate_unified_image_eligibility(None, source()))

    def test_19_false_gallery_flag_blocks_allowlisted_role(self):
        result = evaluate_unified_image_eligibility(folder(gallery_eligible=False), source())
        self.assert_denied(result, EligibilityReason.FOLDER_ROLE_NOT_GALLERY_ELIGIBLE)

    def test_20_role_allowlist_cannot_be_bypassed_with_gallery_true(self):
        for role in folders.FolderRole:
            if role not in {folders.FolderRole.STOREFRONT_PHOTOS, folders.FolderRole.FACTORY_PHOTOS}:
                with self.subTest(role=role):
                    self.assert_denied(evaluate_unified_image_eligibility(folder(role=role, gallery_eligible=True), source()))

    def test_21_source_asset_ineligible_blocks(self):
        result = evaluate_unified_image_eligibility(folder(), source(source_asset_eligible=False))
        self.assert_denied(result, EligibilityReason.SOURCE_ASSET_NOT_WEBP_ELIGIBLE)

    def test_22_pipeline_false_blocks(self):
        result = evaluate_unified_image_eligibility(folder(), source(requires_webp_pipeline=False))
        self.assert_denied(result, EligibilityReason.WEBP_PIPELINE_NOT_REQUIRED)

    def test_23_not_allowed_action_blocks(self):
        result = evaluate_unified_image_eligibility(folder(), source(webp_action=webp.WebPAction.NOT_ALLOWED))
        self.assert_denied(result, EligibilityReason.INVALID_WEBP_ACTION)

    def test_24_convert_to_webp_accepted(self):
        result = evaluate()
        self.assertEqual(result.webp_action, webp.WebPAction.CONVERT_TO_WEBP)
        self.assert_eligible(result)

    def test_25_validate_existing_webp_accepted(self):
        result = evaluate(mime="image/webp")
        self.assertEqual(result.webp_action, webp.WebPAction.VALIDATE_EXISTING_WEBP)
        self.assert_eligible(result)

    def test_26_target_mime_must_be_webp(self):
        f, w = folder(), source()
        with patch.object(webp.WebPOutputPolicyResult, "target_mime_type", new_callable=PropertyMock, return_value="image/png"):
            result = evaluate_unified_image_eligibility(f, w)
        self.assert_denied(result, EligibilityReason.INVALID_WEBP_TARGET)

    def test_27_target_extension_must_be_webp(self):
        f, w = folder(), source()
        with patch.object(webp.WebPOutputPolicyResult, "target_extension", new_callable=PropertyMock, return_value=".jpg"):
            result = evaluate_unified_image_eligibility(f, w)
        self.assert_denied(result, EligibilityReason.INVALID_WEBP_TARGET)

    def test_28_folder_blocker(self):
        result = evaluate_unified_image_eligibility(folder(blocking_issues=("mock_folder_blocker",)), source())
        self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
        self.assertIn("mock_folder_blocker", result.blocking_issues)

    def test_29_webp_blocker(self):
        result = evaluate_unified_image_eligibility(folder(), source(blocking_issues=("mock_webp_blocker",)))
        self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
        self.assertIn("mock_webp_blocker", result.blocking_issues)

    def test_30_blockers_merge_safely_and_deterministically(self):
        result = evaluate_unified_image_eligibility(
            folder(blocking_issues=("mock_folder", "mock_shared")),
            source(blocking_issues=("mock_shared", "mock_webp")),
        )
        self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
        self.assertEqual(result.blocking_issues, ("mock_folder", "mock_shared", "mock_webp"))

    def test_31_storefront_reason(self):
        self.assertEqual(evaluate().eligibility_reason, "eligible_storefront_photo")

    def test_32_factory_reason(self):
        self.assertEqual(evaluate("Factory Photos-Mock").eligibility_reason, "eligible_factory_photo")

    def test_33_banner_reason(self):
        self.assertEqual(evaluate("Banner-Mock").eligibility_reason, "folder_role_not_gallery_eligible")

    def test_34_source_reason(self):
        self.assertEqual(evaluate(mime="image/vnd.adobe.photoshop").eligibility_reason, "source_asset_not_webp_eligible")

    def test_35_unknown_reason(self):
        self.assertEqual(evaluate("Mock Collection").eligibility_reason, "folder_role_unknown")

    def test_36_missing_role_reason(self):
        result = evaluate_unified_image_eligibility(None, source())
        self.assertEqual(result.eligibility_reason, "missing_folder_role")

    def test_37_storefront_and_factory_remain_distinct(self):
        first, second = evaluate(), evaluate("Factory Photos-Mock")
        self.assertEqual(first.folder_role, folders.FolderRole.STOREFRONT_PHOTOS)
        self.assertEqual(second.folder_role, folders.FolderRole.FACTORY_PHOTOS)
        self.assertNotEqual(first.to_dict()["folder_role"], second.to_dict()["folder_role"])

    def test_38_deeper_inventory_false(self):
        result = evaluate()
        self.assertFalse(result.requires_deeper_inventory)
        self.assertNotIn("folder_inventory_incomplete", result.warnings)

    def test_39_deeper_inventory_true_preserved(self):
        result = evaluate_unified_image_eligibility(folder(requires_deeper_inventory=True), source())
        self.assertTrue(result.requires_deeper_inventory)

    def test_40_deeper_inventory_does_not_block_direct_image(self):
        result = evaluate_unified_image_eligibility(folder("Factory Photos-Mock", requires_deeper_inventory=True), source())
        self.assert_eligible(result)

    def test_41_deeper_inventory_warning(self):
        result = evaluate_unified_image_eligibility(folder(requires_deeper_inventory=True), source())
        self.assertEqual(result.warnings, ("folder_inventory_incomplete",))

    def test_42_no_depth_three_trigger(self):
        result = evaluate_unified_image_eligibility(folder(depth=2, requires_deeper_inventory=True), source())
        self.assertNotIn("depth", result.to_dict())
        self.assertNotIn("jobs", result.to_dict())
        self.assertFalse(hasattr(policy, "traverse"))
        for operation in self.denied:
            operation.assert_not_called()

    def test_43_upload_false_does_not_block_and_is_not_inspected(self):
        f, w = folder(), source()
        self.assertIs(w.wordpress_upload_ready, False)
        with patch.object(webp.WebPOutputPolicyResult, "wordpress_upload_ready", new_callable=PropertyMock, side_effect=AssertionError("Not an eligibility input")):
            result = evaluate_unified_image_eligibility(f, w)
        self.assert_eligible(result)

    def test_44_no_upload_authority_fields_or_methods(self):
        result = evaluate()
        for name in ("upload_ready", "wordpress_upload_ready", "source_wordpress_upload_ready", "media_id", "mark_ready", "approve_upload", "upload"):
            self.assertNotIn(name, result.to_dict())
            self.assertFalse(hasattr(result, name))
            self.assertFalse(hasattr(policy, name))

    def test_45_folder_version_retained(self):
        f = folder()
        result = evaluate_unified_image_eligibility(f, source())
        self.assertEqual(result.folder_role_policy_version, f.policy_version)
        self.assertEqual(result.folder_role_policy_version, "xxxxdoll-folder-role-v1")

    def test_46_webp_version_retained(self):
        w = source()
        result = evaluate_unified_image_eligibility(folder(), w)
        self.assertEqual(result.webp_policy_version, w.policy_version)
        self.assertEqual(result.webp_policy_version, "xxxxdoll-webp-output-v1")

    def test_47_unified_version(self):
        self.assertEqual(policy.POLICY_VERSION, "xxxxdoll-unified-image-eligibility-v1")
        self.assertEqual(evaluate().policy_version, policy.POLICY_VERSION)

    def test_48_wrong_folder_domain_object_rejected(self):
        for value in ({}, folder().to_dict(), "Photos", object(), source()):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(UnifiedImageEligibilityPolicyError, "folder_role_result_required"):
                evaluate_unified_image_eligibility(value, source())

    def test_49_wrong_webp_domain_object_rejected(self):
        for value in (None, {}, source().to_dict(), "image/webp", object(), assets.classify_image_asset_type("image/jpeg", "mock")):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(UnifiedImageEligibilityPolicyError, "webp_output_policy_result_required"):
                evaluate_unified_image_eligibility(folder(), value)

    def test_50_no_name_based_folder_classification(self):
        f, w = folder(), source()
        with patch.object(folders, "classify_folder_role", side_effect=AssertionError("No reclassification")), patch.object(folders, "normalize_folder_name", side_effect=AssertionError("No normalization")):
            for name in ("normalized_folder_name", "matched_rule", "parent_safe_folder_name", "sku", "depth", "product_source"):
                with self.subTest(name=name), patch.object(folders.FolderRoleClassification, name, new_callable=PropertyMock, side_effect=AssertionError("Decision fields only")):
                    self.assert_eligible(evaluate_unified_image_eligibility(f, w))

    def test_51_no_mime_or_webp_reclassification(self):
        f, w = folder(), source()
        with patch.object(assets, "classify_image_asset_type", side_effect=AssertionError("No MIME rules")), patch.object(webp, "evaluate_webp_output_policy", side_effect=AssertionError("Consume existing decision")):
            self.assert_eligible(evaluate_unified_image_eligibility(f, w))
        tree = ast.parse(inspect.getsource(policy))
        literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertTrue(literals.isdisjoint({"image/jpeg", "image/png", "image/vnd.adobe.photoshop", "image/gif", "image/avif", "video/", "audio/", ".jpg", ".png", ".psd", ".mp4"}))

    def test_52_width_does_not_affect_eligibility(self):
        a = assets.classify_image_asset_type("image/jpeg", "mock", image_width=0)
        self.assert_eligible(evaluate_unified_image_eligibility(folder(), webp.evaluate_webp_output_policy(a)))
        with self.assertRaises(TypeError):
            evaluate_unified_image_eligibility(folder(), source(), width=0)

    def test_53_height_does_not_affect_eligibility(self):
        a = assets.classify_image_asset_type("image/jpeg", "mock", image_height=0)
        self.assert_eligible(evaluate_unified_image_eligibility(folder(), webp.evaluate_webp_output_policy(a)))
        with self.assertRaises(TypeError):
            evaluate_unified_image_eligibility(folder(), source(), height=0)

    def test_54_size_does_not_affect_eligibility(self):
        a = assets.classify_image_asset_type("image/jpeg", "mock", size_bytes=0)
        self.assert_eligible(evaluate_unified_image_eligibility(folder(), webp.evaluate_webp_output_policy(a)))
        with self.assertRaises(TypeError):
            evaluate_unified_image_eligibility(folder(), source(), size_bytes=0)

    def test_55_deterministic(self):
        f, w = folder(warnings=("mock_folder_warning",), requires_deeper_inventory=True), source(warnings=("mock_webp_warning",))
        first = evaluate_unified_image_eligibility(f, w)
        second = evaluate_unified_image_eligibility(f, w)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True), json.dumps(second.to_dict(), sort_keys=True))

    def test_56_frozen_result(self):
        result = evaluate()
        with self.assertRaises(FrozenInstanceError):
            result.unified_image_eligible = False
        with self.assertRaises(FrozenInstanceError):
            result.folder_role = folders.FolderRole.BANNER

    def test_57_no_raw_id_arguments(self):
        for key in ("raw_drive_id", "provider_file_id", "file_id", "media_id"):
            with self.subTest(key=key), self.assertRaises(TypeError):
                evaluate_unified_image_eligibility(folder(), source(), **{key: "MOCK_ID"})

    def test_58_no_url_arguments_or_echo(self):
        value = "https://drive.google.com/file/d/MOCK_ONLY/view"
        with self.assertRaises(UnifiedImageEligibilityPolicyError) as caught:
            evaluate_unified_image_eligibility(value, source())
        self.assertNotIn("MOCK_ONLY", str(caught.exception))
        for key in ("drive_url", "download_url", "wordpress_url"):
            with self.assertRaises(TypeError):
                evaluate_unified_image_eligibility(folder(), source(), **{key: value})

    def test_59_no_local_path_arguments(self):
        with self.assertRaises(UnifiedImageEligibilityPolicyError):
            evaluate_unified_image_eligibility(Path("mock.jpg"), source())
        with self.assertRaises(TypeError):
            evaluate_unified_image_eligibility(folder(), source(), local_path="mock.jpg")

    def test_60_network_zero(self):
        evaluate()
        for operation in self.denied[:3]:
            operation.assert_not_called()

    def test_61_download_zero(self):
        evaluate()
        self.denied[3].assert_not_called()

    def test_62_conversion_zero(self):
        evaluate()
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_63_upload_zero(self):
        evaluate()
        self.denied[6].assert_not_called()

    def test_64_no_file_reads_or_writes(self):
        f, w = folder(), source()
        with patch("builtins.open", side_effect=AssertionError("No files")), patch("io.open", side_effect=AssertionError("No files")), patch.object(Path, "read_bytes", side_effect=AssertionError("No bytes")), patch.object(Path, "write_bytes", side_effect=AssertionError("No media")), patch.object(Path, "write_text", side_effect=AssertionError("No reports")):
            evaluate_unified_image_eligibility(f, w).to_dict()

    def test_65_no_pillow_import(self):
        tree = ast.parse(inspect.getsource(policy))
        modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        modules.update(node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module)
        self.assertTrue(modules.isdisjoint({"PIL", "Pillow", "wand", "cv2", "subprocess", "os", "google_api", "http_client"}))

    def test_66_no_cwebp(self):
        evaluate(mime="image/png")
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_67_no_imagemagick(self):
        evaluate(mime="image/vnd.adobe.photoshop")
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_68_no_ffmpeg(self):
        evaluate(mime="video/mp4")
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_69_no_wordpress_client_or_credentials(self):
        evaluate()
        self.denied[6].assert_not_called()
        for key in ("client", "wordpress_client", "credentials", "wp_app_password"):
            with self.assertRaises(TypeError):
                evaluate_unified_image_eligibility(folder(), source(), **{key: "MOCK_ONLY"})

    def test_70_synthetic_storefront_reality_shape(self):
        for name in ("Photos-Mock Ultra", "Photos-Mock Pro", "Photos-Mock Classic"):
            self.assert_eligible(evaluate(name))

    def test_71_synthetic_factory_reality_shape(self):
        for name in ("Factory Photos-Mock Ultra", "Factory Photos-Mock Pro", "Factory Photos-Mock Classic"):
            self.assert_eligible(evaluate(name))

    def test_72_synthetic_banner_reality_shape(self):
        self.assert_denied(evaluate("Banner-Mock Ultra"), EligibilityReason.FOLDER_ROLE_NOT_GALLERY_ELIGIBLE)

    def test_73_synthetic_psd_reality_shape(self):
        for name in ("Photos-Mock", "Factory Photos-Mock"):
            result = evaluate(name, "image/vnd.adobe.photoshop")
            self.assert_denied(result)
            self.assertEqual(result.webp_action, webp.WebPAction.NOT_ALLOWED)

    def test_74_synthetic_video_reality_shape(self):
        for mime in ("video/quicktime", "video/mp4"):
            self.assert_denied(evaluate("Factory Videos-Mock", mime))

    def test_75_synthetic_classic_27_direct_images_with_deeper_folders(self):
        f = folders.classify_folder_role("Factory Photos-Mock Si70cm", has_depth_limit_children=True)
        results = [evaluate_unified_image_eligibility(f, source()) for _ in range(27)]
        self.assertEqual(len(results), 27)
        self.assertTrue(all(result.unified_image_eligible and result.requires_deeper_inventory for result in results))
        self.assertTrue(all("folder_inventory_incomplete" in result.warnings for result in results))

    def test_76_upstream_warnings_preserved(self):
        result = evaluate_unified_image_eligibility(folder(warnings=("mock_folder_warning",)), source(warnings=("mock_webp_warning",)))
        self.assertEqual(result.warnings, ("mock_folder_warning", "mock_webp_warning"))

    def test_77_safe_warning_not_automatic_block(self):
        result = evaluate_unified_image_eligibility(folder(warnings=("mock_review_warning",)), source(warnings=("asset_extension_mime_mismatch",)))
        self.assert_eligible(result)

    def test_78_unknown_warning_kept_or_added(self):
        first = evaluate("Mock Collection")
        second = evaluate_unified_image_eligibility(folder("Mock Collection", warnings=()), source())
        for result in (first, second):
            self.assert_denied(result, EligibilityReason.FOLDER_ROLE_UNKNOWN)
            self.assertEqual(result.warnings.count("folder_role_unknown"), 1)

    def test_79_invalid_target_does_not_leak_unsafe_text(self):
        f, w = folder(), source()
        for value in ("https://example.invalid/MOCK_ONLY", r"C:\mock\private.json", "image/png", None):
            with self.subTest(kind=type(value).__name__), patch.object(webp.WebPOutputPolicyResult, "target_mime_type", new_callable=PropertyMock, return_value=value):
                result = evaluate_unified_image_eligibility(f, w)
            self.assert_denied(result, EligibilityReason.INVALID_WEBP_TARGET)
            self.assertIn("invalid_webp_target", result.blocking_issues)
            self.assertNotIn("MOCK_ONLY", repr(result) + json.dumps(result.to_dict()))
            self.assertEqual(result.target_mime_type, "image/webp")

    def test_80_target_webp_invariant_for_all_decisions(self):
        for name in ("Photos-Mock", "Factory Photos-Mock", "Banner", "Unknown"):
            for mime in ("image/jpeg", "image/webp", "image/vnd.adobe.photoshop", "video/mp4", None):
                result = evaluate(name, mime)
                self.assertEqual(result.target_mime_type, "image/webp")
                self.assertEqual(result.target_extension, ".webp")

    def test_81_folder_flags_require_exact_booleans(self):
        for flag in ("gallery_eligible", "requires_deeper_inventory"):
            for value in (1, 0, "true", "false", None):
                with self.subTest(flag=flag, value=value):
                    result = evaluate_unified_image_eligibility(folder(**{flag: value}), source())
                    self.assert_denied(result, EligibilityReason.INVALID_POLICY_INPUT)
                    self.assertIn("invalid_policy_input", result.blocking_issues)

    def test_82_webp_flags_require_exact_booleans(self):
        for flag in ("source_asset_eligible", "requires_webp_pipeline"):
            for value in (1, 0, "true", "false", None):
                with self.subTest(flag=flag, value=value):
                    result = evaluate_unified_image_eligibility(folder(), source(**{flag: value}))
                    self.assert_denied(result, EligibilityReason.INVALID_POLICY_INPUT)

    def test_83_role_requires_formal_enum_without_raw_echo(self):
        for role in ("storefront_photos", "unknown", None, [], "https://example.invalid/MOCK_ONLY"):
            with self.subTest(kind=type(role).__name__):
                result = evaluate_unified_image_eligibility(folder(role=role), source())
                self.assert_denied(result, EligibilityReason.INVALID_POLICY_INPUT)
                self.assertIsNone(result.folder_role)
                self.assertNotIn("MOCK_ONLY", repr(result))

    def test_84_unrecognized_actions_never_coerced(self):
        for action in ("convert_to_webp", "upload", "https://example.invalid/MOCK_ONLY", None, []):
            with self.subTest(kind=type(action).__name__):
                result = evaluate_unified_image_eligibility(folder(), source(webp_action=action))
                self.assert_denied(result, EligibilityReason.INVALID_WEBP_ACTION)
                self.assertIsNone(result.webp_action)
                self.assertNotIn("MOCK_ONLY", repr(result))

    def test_85_unrecognized_folder_version_fail_closed(self):
        f, w = folder(), source()
        for version in ("xxxxdoll-folder-role-v2", "https://example.invalid/MOCK_ONLY", None):
            with patch.object(folders.FolderRoleClassification, "policy_version", new_callable=PropertyMock, return_value=version):
                result = evaluate_unified_image_eligibility(f, w)
            self.assert_denied(result, EligibilityReason.INVALID_POLICY_INPUT)
            self.assertIsNone(result.folder_role_policy_version)
            self.assertNotIn("MOCK_ONLY", repr(result))

    def test_86_unrecognized_webp_version_fail_closed(self):
        f, w = folder(), source()
        with patch.object(webp.WebPOutputPolicyResult, "policy_version", new_callable=PropertyMock, return_value="xxxxdoll-webp-output-v2"):
            result = evaluate_unified_image_eligibility(f, w)
        self.assert_denied(result, EligibilityReason.INVALID_POLICY_INPUT)
        self.assertIsNone(result.webp_policy_version)

    def test_87_missing_folder_preserves_only_safe_webp_decisions(self):
        result = evaluate_unified_image_eligibility(None, source())
        self.assertIsNone(result.folder_role)
        self.assertIsNone(result.folder_role_policy_version)
        self.assertFalse(result.folder_gallery_eligible)
        self.assertFalse(result.requires_deeper_inventory)
        self.assertTrue(result.source_asset_eligible)
        self.assertEqual(result.webp_policy_version, webp.POLICY_VERSION)

    def test_88_warning_merge_deduplicates_preserving_order(self):
        f = folder(requires_deeper_inventory=True, warnings=("mock_a", "mock_shared", "folder_inventory_incomplete"))
        w = source(warnings=("mock_shared", "mock_b"))
        result = evaluate_unified_image_eligibility(f, w)
        self.assertEqual(result.warnings, ("mock_a", "mock_shared", "folder_inventory_incomplete", "mock_b"))

    def test_89_invalid_issue_shapes_fail_closed(self):
        for value in (None, [], "mock", (None,), (123,)):
            for field in ("warnings", "blocking_issues"):
                for which in ("folder", "webp"):
                    with self.subTest(kind=type(value).__name__, field=field, which=which):
                        f = folder(**{field: value}) if which == "folder" else folder()
                        w = source(**{field: value}) if which == "webp" else source()
                        self.assert_denied(evaluate_unified_image_eligibility(f, w), EligibilityReason.INVALID_POLICY_INPUT)

    def test_90_unsafe_folder_warning_redacted_and_blocked(self):
        text = "https://drive.google.com/file/d/MOCK_ID?resourcekey=MOCK_KEY"
        result = evaluate_unified_image_eligibility(folder(warnings=("mock_safe", text)), source())
        self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
        self.assertIn("mock_safe", result.warnings)
        self.assertIn("unsafe_folder_warning_redacted", result.warnings)
        self.assertIn("unsafe_upstream_audit", result.blocking_issues)
        self.assertNotIn("MOCK_ID", json.dumps(result.to_dict()) + repr(result))

    def test_91_unsafe_webp_warning_redacted_and_blocked(self):
        for text in ("ck_" + "x" * 30, "token=MOCK_SECRET", "client_email=mock@example.invalid", "private_key_mock_value"):
            with self.subTest(text=text):
                result = evaluate_unified_image_eligibility(folder(), source(warnings=(text,)))
                self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
                self.assertIn("unsafe_webp_warning_redacted", result.warnings)
                self.assertNotIn(text, repr(result) + json.dumps(result.to_dict()))

    def test_92_unsafe_folder_blocker_redacted(self):
        text = r"C:\mock\credentials.json"
        result = evaluate_unified_image_eligibility(folder(blocking_issues=(text,)), source())
        self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
        self.assertIn("unsafe_folder_blocker_redacted", result.blocking_issues)
        self.assertNotIn(text, repr(result))

    def test_93_unsafe_webp_blocker_redacted(self):
        for text in ("Authorization: MOCK_SECRET", "Cookie: MOCK_SECRET", "cs_" + "x" * 30):
            with self.subTest(text=text):
                result = evaluate_unified_image_eligibility(folder(), source(blocking_issues=("mock_safe_blocker", text)))
                self.assert_denied(result, EligibilityReason.UPSTREAM_BLOCKED)
                self.assertIn("mock_safe_blocker", result.blocking_issues)
                self.assertIn("unsafe_webp_blocker_redacted", result.blocking_issues)
                self.assertNotIn(text, repr(result) + json.dumps(result.to_dict()))

    def test_94_inputs_not_mutated(self):
        f, w = folder(warnings=("mock_warning",)), source(blocking_issues=("mock_blocker",))
        original = copy.deepcopy((f, w))
        evaluate_unified_image_eligibility(f, w)
        self.assertEqual((f, w), original)

    def test_95_projection_independent_and_json_safe(self):
        result = evaluate()
        projected = result.to_dict()
        self.assertEqual(json.loads(json.dumps(projected)), projected)
        projected["unified_image_eligible"] = False
        projected["warnings"].append("mock_changed")
        self.assertTrue(result.unified_image_eligible)
        self.assertEqual(result.warnings, ())
        self.assertEqual(set(result.to_dict()), {
            "policy_version", "folder_role", "folder_role_policy_version", "webp_policy_version",
            "folder_gallery_eligible", "source_asset_eligible", "requires_webp_pipeline", "webp_action",
            "target_mime_type", "target_extension", "unified_image_eligible", "eligibility_reason",
            "requires_deeper_inventory", "warnings", "blocking_issues",
        })

    def test_96_fixed_version_not_constructor_argument(self):
        self.assertFalse(next(item for item in fields(UnifiedImageEligibilityResult) if item.name == "policy_version").init)
        with self.assertRaises(ValueError):
            replace(evaluate(), policy_version="mock_other")

    def test_97_unused_mime_and_source_audit_not_read_or_copied(self):
        f, w = folder(), source()
        for name in ("source_mime_type", "source_asset_class", "reason"):
            with patch.object(webp.WebPOutputPolicyResult, name, new_callable=PropertyMock, side_effect=AssertionError("Use decision, not raw audit")):
                result = evaluate_unified_image_eligibility(f, w)
            self.assert_eligible(result)
            self.assertNotIn(name, result.to_dict())

    def test_98_no_selection_or_ranking_interface(self):
        parameters = tuple(inspect.signature(evaluate_unified_image_eligibility).parameters)
        self.assertEqual(parameters, ("folder_role", "webp_result"))
        for name in ("select_main_image", "sort_images", "deduplicate", "max_images", "rank", "download"):
            self.assertFalse(hasattr(policy, name))
            self.assertNotIn(name, evaluate().to_dict())
        tree = ast.parse(inspect.getsource(policy))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        self.assertTrue(imports.isdisjoint({"datetime", "time", "uuid", "random", "cli", "config"}))

    def test_99_environment_does_not_override_decisions(self):
        with patch.dict("os.environ", {"UNIFIED_IMAGE_ELIGIBLE": "true", "WORDPRESS_UPLOAD_READY": "true", "WEBP_TARGET_MIME": "image/png"}):
            result = evaluate("Banner")
        self.assert_denied(result)
        self.assertEqual(result.target_mime_type, "image/webp")
        self.assertFalse(hasattr(result, "wordpress_upload_ready"))

    def test_100_target_contract_cannot_be_replaced(self):
        result = evaluate()
        with self.assertRaises(TypeError):
            replace(result, target_mime_type="image/jpeg")
        with self.assertRaises(AttributeError):
            object.__setattr__(result, "target_extension", ".jpg")
        self.assertEqual(result.target_extension, ".webp")


if __name__ == "__main__":
    unittest.main()
