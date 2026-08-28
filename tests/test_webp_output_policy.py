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

from sync_worker import image_asset_type_policy as asset_policy, webp_output_policy as policy
from sync_worker.image_asset_type_policy import AssetClass, ImageAssetTypeResult
from sync_worker.webp_output_policy import (
    POLICY_VERSION, WebPAction, WebPOutputPolicyError, WebPOutputPolicyResult,
    evaluate_webp_output_policy,
)


def source(mime="image/jpeg", name="mock-source", **overrides):
    return replace(asset_policy.classify_image_asset_type(mime, name), **overrides)


def evaluate(mime="image/jpeg", name="mock-source", **overrides):
    return evaluate_webp_output_policy(source(mime, name, **overrides))


class WebPOutputPolicyTests(unittest.TestCase):
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
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Pure mock policy; no I/O or conversion"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def assert_denied(self, result):
        self.assertFalse(result.source_asset_eligible)
        self.assertFalse(result.requires_webp_pipeline)
        self.assertEqual(result.webp_action, WebPAction.NOT_ALLOWED)
        self.assertIs(result.wordpress_upload_ready, False)

    def test_01_jpeg_eligible_source(self):
        self.assertTrue(evaluate().source_asset_eligible)

    def test_02_jpeg_requires_pipeline(self):
        self.assertTrue(evaluate().requires_webp_pipeline)

    def test_03_jpeg_convert_action(self):
        self.assertEqual(evaluate().webp_action, WebPAction.CONVERT_TO_WEBP)

    def test_04_jpeg_target_webp(self):
        result = evaluate()
        self.assertEqual((result.target_mime_type, result.target_extension), ("image/webp", ".webp"))

    def test_05_jpeg_upload_not_ready(self):
        self.assertIs(evaluate().wordpress_upload_ready, False)

    def test_06_png_eligible_source(self):
        self.assertTrue(evaluate("image/png").source_asset_eligible)

    def test_07_png_conversion_required(self):
        result = evaluate("image/png")
        self.assertTrue(result.requires_webp_pipeline)
        self.assertEqual(result.webp_action, WebPAction.CONVERT_TO_WEBP)

    def test_08_png_upload_not_ready(self):
        self.assertIs(evaluate("image/png").wordpress_upload_ready, False)

    def test_09_existing_webp_eligible_source(self):
        self.assertTrue(evaluate("image/webp").source_asset_eligible)

    def test_10_existing_webp_validation_action(self):
        self.assertEqual(evaluate("image/webp").webp_action, WebPAction.VALIDATE_EXISTING_WEBP)

    def test_11_existing_webp_requires_pipeline(self):
        self.assertTrue(evaluate("image/webp").requires_webp_pipeline)

    def test_12_existing_webp_upload_not_ready(self):
        self.assertIs(evaluate("image/webp").wordpress_upload_ready, False)

    def test_13_psd_not_allowed(self):
        result = evaluate("image/vnd.adobe.photoshop")
        self.assert_denied(result)
        self.assertEqual(result.reason, "design_source_not_storefront_asset")

    def test_14_psd_no_pipeline(self):
        self.assertFalse(evaluate("image/vnd.adobe.photoshop").requires_webp_pipeline)

    def test_15_psd_no_upload(self):
        self.assertIs(evaluate("image/vnd.adobe.photoshop").wordpress_upload_ready, False)

    def test_16_video_not_allowed(self):
        self.assert_denied(evaluate("video/mp4"))

    def test_17_mp4_upload_not_ready(self):
        self.assertIs(evaluate("video/mp4").wordpress_upload_ready, False)

    def test_18_quicktime_upload_not_ready(self):
        self.assert_denied(evaluate("video/quicktime"))

    def test_19_unsupported_not_allowed(self):
        self.assert_denied(evaluate("application/zip"))

    def test_20_unknown_not_allowed(self):
        self.assert_denied(evaluate(None))

    def test_21_other_media_not_allowed(self):
        for mime in ("application/pdf", "audio/mpeg", "audio/mp4"):
            with self.subTest(mime=mime):
                self.assert_denied(evaluate(mime))

    def test_22_gif_respects_upstream_ineligible(self):
        result = evaluate("image/gif")
        self.assert_denied(result)
        self.assertEqual(result.reason, "upstream_storefront_ineligible")
        self.assertIn("web_image_format_requires_platform_verification", result.warnings)

    def test_23_avif_respects_upstream_ineligible(self):
        self.assert_denied(evaluate("image/avif"))

    def test_24_extension_fallback_not_eligible(self):
        upstream = source("application/octet-stream", "mock.jpg")
        self.assertEqual(upstream.classification_source, "extension_fallback")
        self.assert_denied(evaluate_webp_output_policy(upstream))

    def test_25_extension_fallback_cannot_convert(self):
        self.assertEqual(evaluate(None, "mock.png").webp_action, WebPAction.NOT_ALLOWED)

    def test_26_upstream_blocker_prevents_pipeline(self):
        result = evaluate(blocking_issues=("mock_asset_blocked",))
        self.assert_denied(result)
        self.assertEqual(result.reason, "upstream_asset_blocked")

    def test_27_safe_blocker_audit_preserved(self):
        result = evaluate(blocking_issues=("mock_first_blocker", "mock_second_blocker"))
        self.assertEqual(result.blocking_issues, ("mock_first_blocker", "mock_second_blocker"))

    def test_28_storefront_eligible_not_upload_authority(self):
        upstream = source()
        result = evaluate_webp_output_policy(upstream)
        self.assertTrue(upstream.storefront_eligible)
        self.assertTrue(result.source_asset_eligible)
        self.assertFalse(result.wordpress_upload_ready)

    def test_29_jpeg_direct_upload_not_exposed(self):
        result = evaluate()
        self.assertFalse(result.to_dict()["wordpress_upload_ready"])
        self.assertNotIn("upload", result.webp_action.value)

    def test_30_png_direct_upload_not_exposed(self):
        self.assertFalse(evaluate("image/png").to_dict()["wordpress_upload_ready"])

    def test_31_source_webp_cannot_bypass_validation(self):
        result = evaluate("image/webp", "mock.webp")
        self.assertTrue(result.requires_webp_pipeline)
        self.assertEqual(result.webp_action.value, "validate_existing_webp")
        self.assertFalse(result.to_dict()["wordpress_upload_ready"])

    def test_32_target_mime_always_webp(self):
        for mime in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/vnd.adobe.photoshop", "video/mp4", "application/pdf", None):
            with self.subTest(mime=mime):
                self.assertEqual(evaluate(mime).target_mime_type, "image/webp")

    def test_33_target_extension_always_webp(self):
        for mime in ("image/jpeg", "image/png", "image/webp", "application/octet-stream", "application/zip", None):
            self.assertEqual(evaluate(mime).target_extension, ".webp")

    def test_34_no_jpg_output_configuration(self):
        with self.assertRaises(TypeError):
            evaluate_webp_output_policy(source(), target_extension=".jpg")

    def test_35_no_png_output_configuration(self):
        with self.assertRaises(TypeError):
            evaluate_webp_output_policy(source(), target_mime_type="image/png")

    def test_36_asset_class_audit_preserved(self):
        for mime in ("image/jpeg", "image/vnd.adobe.photoshop", "video/mp4", "application/pdf", "text/plain", None):
            upstream = source(mime)
            self.assertEqual(evaluate_webp_output_policy(upstream).source_asset_class, upstream.asset_class)

    def test_37_normalized_mime_audit_preserved(self):
        upstream = source(" IMAGE/JPEG ")
        self.assertEqual(evaluate_webp_output_policy(upstream).source_mime_type, "image/jpeg")

    def test_38_deterministic(self):
        upstream = source(blocking_issues=("mock_blocker",), warnings=("mock_warning",))
        first, second = evaluate_webp_output_policy(upstream), evaluate_webp_output_policy(upstream)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True), json.dumps(second.to_dict(), sort_keys=True))

    def test_39_immutable_result(self):
        result = evaluate()
        with self.assertRaises(FrozenInstanceError):
            result.webp_action = WebPAction.NOT_ALLOWED
        with self.assertRaises(FrozenInstanceError):
            result.source_asset_eligible = False

    def test_40_policy_version(self):
        self.assertEqual(POLICY_VERSION, "xxxxdoll-webp-output-v1")
        self.assertEqual(evaluate().policy_version, POLICY_VERSION)
        self.assertFalse(next(item for item in fields(WebPOutputPolicyResult) if item.name == "policy_version").init)

    def test_41_no_filename_classification(self):
        upstream = source()
        with patch.object(ImageAssetTypeResult, "safe_name", new_callable=PropertyMock, side_effect=AssertionError("No name inspection")):
            result = evaluate_webp_output_policy(upstream)
        self.assertTrue(result.source_asset_eligible)

    def test_42_no_extension_classification(self):
        upstream = source()
        with patch.object(ImageAssetTypeResult, "safe_extension", new_callable=PropertyMock, side_effect=AssertionError("No extension inspection")):
            result = evaluate_webp_output_policy(upstream)
        self.assertEqual(result.webp_action, WebPAction.CONVERT_TO_WEBP)

    def test_43_no_reclassification_or_copied_mime_rules(self):
        upstream = source()
        with patch.object(asset_policy, "classify_image_asset_type", side_effect=AssertionError("No second classification")) as classify:
            evaluate_webp_output_policy(upstream)
        classify.assert_not_called()
        literals = {node.value for node in ast.walk(ast.parse(inspect.getsource(policy)))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertTrue(literals.isdisjoint({"image/vnd.adobe.photoshop", "image/gif", "image/avif", "video/", "audio/", ".jpg", ".png", ".psd", ".mp4"}))

    def test_44_consumes_existing_result_directly(self):
        upstream = source()
        self.assertIsInstance(upstream, ImageAssetTypeResult)
        self.assertIsInstance(evaluate_webp_output_policy(upstream), WebPOutputPolicyResult)
        self.assertEqual(tuple(inspect.signature(evaluate_webp_output_policy).parameters), ("asset",))

    def test_45_malformed_consumed_fields_fail_closed(self):
        cases = ({"asset_class": "web_image"}, {"asset_class": None}, {"storefront_eligible": "true"},
                 {"storefront_eligible": 1}, {"classification_source": "untrusted"},
                 {"classification_source": []}, {"normalized_mime_type": 12},
                 {"warnings": []}, {"blocking_issues": "mock_blocker"})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(WebPOutputPolicyError):
                evaluate_webp_output_policy(source(**values))

    def test_46_wrong_object_type_rejected(self):
        for value in (None, {}, source().to_dict(), "mock.jpg", Path("mock.jpg"), object()):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(WebPOutputPolicyError, "image_asset_type_result_required"):
                evaluate_webp_output_policy(value)

    def test_47_no_drive_id_argument(self):
        for name in ("raw_drive_id", "provider_file_id", "file_id"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                evaluate_webp_output_policy(source(), **{name: "MOCK_ID"})

    def test_48_no_url_argument(self):
        for name in ("drive_url", "download_url", "url"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                evaluate_webp_output_policy(source(), **{name: "https://example.invalid/mock"})

    def test_49_no_local_path_argument(self):
        with self.assertRaises(TypeError):
            evaluate_webp_output_policy(source(), local_file_path=Path("mock.jpg"))

    def test_50_no_wordpress_credentials(self):
        for name in ("credentials", "wp_app_password", "authorization"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                evaluate_webp_output_policy(source(), **{name: "MOCK_CREDENTIAL"})

    def test_51_no_media_id(self):
        with self.assertRaises(TypeError):
            evaluate_webp_output_policy(source(), media_id=123)

    def test_52_no_pillow_imports(self):
        parsed = ast.parse(inspect.getsource(policy))
        imports = {alias.name.split(".")[0] for node in ast.walk(parsed) if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.split(".")[0] for node in ast.walk(parsed) if isinstance(node, ast.ImportFrom) and node.module)
        self.assertTrue(imports.isdisjoint({"PIL", "Pillow", "wand", "magic", "cv2", "subprocess", "os"}))

    def test_53_no_imagemagick_execution(self):
        evaluate("image/png")
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_54_no_cwebp_execution(self):
        evaluate()
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_55_no_ffmpeg_execution(self):
        evaluate("video/mp4")
        for operation in self.denied[-3:]:
            operation.assert_not_called()

    def test_56_no_file_open(self):
        upstream = source()
        with patch("builtins.open", side_effect=AssertionError("No media open")) as opened, patch("io.open", side_effect=AssertionError("No media open")) as io_open:
            evaluate_webp_output_policy(upstream)
        opened.assert_not_called()
        io_open.assert_not_called()

    def test_57_no_bytes_read(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No bytes read")) as read:
            evaluate()
        read.assert_not_called()

    def test_58_no_file_write(self):
        with patch.object(Path, "write_bytes", side_effect=AssertionError("No artifact writes")) as write, patch.object(Path, "write_text", side_effect=AssertionError("No report writes")) as text:
            evaluate().to_dict()
        write.assert_not_called()
        text.assert_not_called()

    def test_59_no_network(self):
        evaluate()
        for operation in self.denied[:3]:
            operation.assert_not_called()

    def test_60_no_download(self):
        evaluate()
        self.denied[3].assert_not_called()

    def test_61_no_upload(self):
        evaluate()
        self.denied[6].assert_not_called()

    def test_62_mock_206_jpegs_require_conversion_not_upload(self):
        results = [evaluate(name=f"mock-{index:03}.jpg") for index in range(206)]
        self.assertEqual(len(results), 206)
        self.assertTrue(all(result.source_asset_eligible and result.requires_webp_pipeline for result in results))
        self.assertTrue(all(result.webp_action == WebPAction.CONVERT_TO_WEBP for result in results))
        self.assertFalse(any(result.wordpress_upload_ready for result in results))

    def test_63_mock_two_psds_not_allowed(self):
        for index in range(2):
            self.assert_denied(evaluate("image/vnd.adobe.photoshop", f"mock-{index}.psd"))

    def test_64_mock_39_videos_not_allowed(self):
        results = [evaluate("video/mp4", f"mock-{index}.mp4") for index in range(39)]
        for result in results:
            self.assert_denied(result)

    def test_65_extension_fallback_fixture_closed(self):
        for mime in (None, "application/octet-stream"):
            for extension in ("jpg", "jpeg", "png", "webp", "psd", "mp4"):
                with self.subTest(mime=mime, extension=extension):
                    self.assert_denied(evaluate(mime, "mock." + extension))

    def test_66_upstream_mismatch_warning_preserved(self):
        result = evaluate("image/jpeg", "mock.psd")
        self.assertTrue(result.source_asset_eligible)
        self.assertIn("asset_extension_mime_mismatch", result.warnings)
        self.assertEqual(result.webp_action, WebPAction.CONVERT_TO_WEBP)

    def test_67_folder_role_not_consumed(self):
        upstream = source(folder_role="banner")
        with patch.object(ImageAssetTypeResult, "folder_role", new_callable=PropertyMock, side_effect=AssertionError("No role join")):
            result = evaluate_webp_output_policy(upstream)
        self.assertTrue(result.source_asset_eligible)

    def test_68_dimensions_not_consumed(self):
        upstream = source(size_bytes=0, image_width=0, image_height=0)
        with patch.object(ImageAssetTypeResult, "image_width", new_callable=PropertyMock, side_effect=AssertionError("No quality policy")):
            result = evaluate_webp_output_policy(upstream)
        self.assertTrue(result.source_asset_eligible)

    def test_69_every_upstream_class_upload_false(self):
        for asset_class in AssetClass:
            result = evaluate(asset_class=asset_class)
            self.assertIs(result.wordpress_upload_ready, False)
            self.assertIs(result.to_dict()["wordpress_upload_ready"], False)

    def test_70_gate_not_constructor_parameter(self):
        parameters = inspect.signature(WebPOutputPolicyResult).parameters
        self.assertNotIn("wordpress_upload_ready", parameters)
        self.assertNotIn("target_mime_type", parameters)
        self.assertNotIn("target_extension", parameters)

    def test_71_cannot_construct_ready_result(self):
        with self.assertRaises(TypeError):
            WebPOutputPolicyResult(AssetClass.WEB_IMAGE, "image/jpeg", True, True,
                                   WebPAction.CONVERT_TO_WEBP, wordpress_upload_ready=True)

    def test_72_dataclass_replace_cannot_mark_ready(self):
        with self.assertRaises(TypeError):
            replace(evaluate(), wordpress_upload_ready=True)

    def test_73_upload_ready_property_cannot_be_assigned(self):
        result = evaluate()
        with self.assertRaises((FrozenInstanceError, TypeError, AttributeError)):
            result.wordpress_upload_ready = True
        self.assertIs(result.wordpress_upload_ready, False)

    def test_74_object_setattr_cannot_override_gate(self):
        result = evaluate()
        with self.assertRaises(AttributeError):
            object.__setattr__(result, "wordpress_upload_ready", True)
        self.assertIs(result.wordpress_upload_ready, False)

    def test_75_target_mime_cannot_be_replaced(self):
        with self.assertRaises(TypeError):
            replace(evaluate(), target_mime_type="image/jpeg")

    def test_76_target_extension_cannot_be_overridden(self):
        result = evaluate()
        with self.assertRaises(AttributeError):
            object.__setattr__(result, "target_extension", ".png")
        self.assertEqual(result.target_extension, ".webp")

    def test_77_no_upload_bypass_methods(self):
        result = evaluate()
        for name in ("mark_ready", "approve_upload", "upload", "mark_verified"):
            self.assertFalse(hasattr(result, name))
            self.assertFalse(hasattr(policy, name))

    def test_78_mutating_projection_cannot_promote_result(self):
        result = evaluate()
        data = result.to_dict()
        data["wordpress_upload_ready"] = True
        data["target_mime_type"] = "image/png"
        data["warnings"].append("mock_changed")
        self.assertFalse(result.to_dict()["wordpress_upload_ready"])
        self.assertEqual(result.target_mime_type, "image/webp")
        self.assertEqual(result.warnings, ())

    def test_79_exact_action_enum(self):
        self.assertEqual({action.value for action in WebPAction}, {"convert_to_webp", "validate_existing_webp", "not_allowed"})

    def test_80_upstream_ineligible_jpeg_cannot_be_promoted(self):
        self.assert_denied(evaluate(storefront_eligible=False))

    def test_81_forged_fallback_eligible_flag_cannot_promote(self):
        self.assert_denied(evaluate(classification_source="extension_fallback", storefront_eligible=True))

    def test_82_unknown_classification_source_not_allowed(self):
        self.assert_denied(evaluate(classification_source="unknown"))

    def test_83_design_class_not_overridden_by_jpeg_mime(self):
        self.assert_denied(evaluate(asset_class=AssetClass.DESIGN_SOURCE, storefront_eligible=True))

    def test_84_forged_web_image_gif_true_still_denied(self):
        self.assert_denied(evaluate("image/gif", storefront_eligible=True))

    def test_85_no_action_for_unapproved_mime(self):
        for mime in ("image/avif", "image/bmp", "image/x-png", "image/jpg"):
            with self.subTest(mime=mime):
                self.assert_denied(evaluate(normalized_mime_type=mime))

    def test_86_no_mime_normalization_or_repair_in_webp_layer(self):
        for mime in (None, "", "IMAGE/JPEG", " image/jpeg ", "image / jpeg"):
            with self.subTest(mime=mime):
                result = evaluate(normalized_mime_type=mime)
                self.assert_denied(result)
                self.assertEqual(result.source_mime_type, mime)

    def test_87_blocker_stops_existing_webp(self):
        self.assert_denied(evaluate("image/webp", blocking_issues=("mock_blocker",)))

    def test_88_safe_warning_does_not_block_conversion(self):
        result = evaluate(warnings=("mock_safe_warning",))
        self.assertTrue(result.source_asset_eligible)
        self.assertEqual(result.warnings, ("mock_safe_warning",))

    def test_89_unsafe_blocker_redacted_and_blocks(self):
        text = "https://drive.google.com/file/d/MOCK_ID?resourcekey=MOCK_KEY"
        result = evaluate(blocking_issues=("mock_safe_blocker", text))
        self.assert_denied(result)
        self.assertIn("mock_safe_blocker", result.blocking_issues)
        self.assertIn("unsafe_upstream_blocker_redacted", result.blocking_issues)
        output = json.dumps(result.to_dict()) + repr(result)
        self.assertNotIn("MOCK_ID", output)
        self.assertNotIn("MOCK_KEY", output)
        self.assertNotIn("https://", output)

    def test_90_unsafe_warning_redacted_and_fails_closed(self):
        for text in ("ck_" + "x" * 30, "Authorization: MOCK_SECRET", "Cookie: MOCK_SECRET", r"C:\mock\private.json", "private_key=MOCK_SECRET", "mock-service@example.invalid"):
            with self.subTest(text=text):
                result = evaluate(warnings=(text,))
                self.assert_denied(result)
                self.assertIn("unsafe_upstream_warning_redacted", result.warnings)
                self.assertIn("unsafe_upstream_audit", result.blocking_issues)
                self.assertNotIn(text, json.dumps(result.to_dict()) + repr(result))

    def test_91_unsafe_mime_audit_rejected_without_echo(self):
        for mime in ("https://example.invalid/MOCK_SECRET", r"C:\mock\private.json", "/mock/private.json", "private_key=MOCK_SECRET", "image/ck_" + "x" * 30):
            with self.subTest(mime=mime), self.assertRaisesRegex(WebPOutputPolicyError, "unsafe_upstream_mime_type") as caught:
                evaluate(normalized_mime_type=mime)
            self.assertNotIn(mime, str(caught.exception))

    def test_92_input_result_immutable_and_unchanged(self):
        upstream = source(warnings=("mock_warning",))
        original = copy.deepcopy(upstream)
        evaluate_webp_output_policy(upstream)
        self.assertEqual(upstream, original)

    def test_93_json_safe_projection(self):
        result = evaluate(blocking_issues=("mock_blocker",))
        self.assertEqual(json.loads(json.dumps(result.to_dict())), result.to_dict())
        self.assertEqual(set(result.to_dict()), {"policy_version", "source_asset_class", "source_mime_type",
                         "source_asset_eligible", "requires_webp_pipeline", "webp_action", "target_mime_type",
                         "target_extension", "wordpress_upload_ready", "reason", "warnings", "blocking_issues"})

    def test_94_no_clock_randomness_or_cli_dependencies(self):
        tree = ast.parse(inspect.getsource(policy))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(imports.intersection({"datetime", "time", "uuid", "random", "cli", "config", "google_api", "folder_role_policy"}))
        for key in ("timestamp", "uuid", "random"):
            self.assertNotIn(key, evaluate().to_dict())

    def test_95_only_source_decision_fields_consumed(self):
        upstream = source(sku="MOCK-SKU", folder_role="banner", size_bytes=0)
        with patch.object(ImageAssetTypeResult, "status", new_callable=PropertyMock, side_effect=AssertionError("No status reclassification")), patch.object(ImageAssetTypeResult, "sku", new_callable=PropertyMock, side_effect=AssertionError("No SKU decision")):
            self.assertTrue(evaluate_webp_output_policy(upstream).source_asset_eligible)

    def test_96_no_png_encoding_quality_choices(self):
        output = evaluate("image/png").to_dict()
        for field in ("quality", "alpha", "alpha_flatten", "background", "resize", "optimize", "thumbnail"):
            self.assertNotIn(field, output)

    def test_97_postscript_design_source_not_allowed(self):
        result = evaluate("application/postscript")
        self.assert_denied(result)
        self.assertEqual(result.reason, "design_source_not_storefront_asset")

    def test_98_safe_blockers_deduplicated_deterministically(self):
        result = evaluate(blocking_issues=("mock_a", "mock_b", "mock_a"))
        self.assertEqual(result.blocking_issues, ("mock_a", "mock_b"))
        self.assert_denied(result)

    def test_99_no_state_that_can_be_mistaken_for_verified_artifact(self):
        result = evaluate("image/webp")
        self.assertFalse(hasattr(policy, "VerifiedWebPArtifact"))
        for field in ("artifact", "artifact_path", "verified", "media_id", "download_ready"):
            self.assertNotIn(field, result.to_dict())

    def test_100_target_and_gate_ignore_environment_configuration(self):
        with patch.dict("os.environ", {"WEBP_TARGET_MIME": "image/png", "WORDPRESS_UPLOAD_READY": "true"}):
            result = evaluate()
        self.assertEqual(result.target_mime_type, "image/webp")
        self.assertIs(result.wordpress_upload_ready, False)


if __name__ == "__main__":
    unittest.main()
