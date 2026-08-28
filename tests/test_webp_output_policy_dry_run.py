from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import runpy
import stat
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli, image_asset_type_policy as asset_policy
from sync_worker import webp_output_policy as core, webp_output_policy_dry_run as dry_run
from sync_worker.webp_output_policy_dry_run import WebPOutputPolicyDryRunInputError


def asset(mime="image/jpeg", name="mock-asset", *, kind="root", **overrides):
    result = asset_policy.classify_image_asset_type(mime, name, sku="MOCK-001").to_dict()
    result.pop("folder_role")
    result.update(
        mime_type=mime, source_manifest_kind=kind,
        depth={"root": 0, "nested": 1, "depth2": 2}[kind],
        safe_folder_name=None if kind == "root" else "Mock Folder",
        parent_safe_folder_name="Mock Parent" if kind == "depth2" else None,
        product_source={"start_row": 10, "end_row": 20},
    )
    result.update(overrides)
    return result


def asset_report(*assets, **overrides):
    return {
        "status": "ok", "policy_version": asset_policy.POLICY_VERSION,
        "summary": {"classified_assets": len(assets)}, "results": list(assets),
        "network_requests_performed": 0, "download_requests_performed": 0,
        "write_requests_performed": 0, **overrides,
    }


def build(report=None):
    return dry_run.build_webp_output_policy_dry_run_report(asset_report(asset()) if report is None else report)


def one(mime="image/jpeg", **overrides):
    return build(asset_report(asset(mime, **overrides)))["results"][0]


def mixed_report():
    return asset_report(
        asset(), asset("image/png"), asset("image/webp"), asset("image/vnd.adobe.photoshop"),
        asset("video/quicktime"), asset("video/mp4"), asset("application/octet-stream"),
        asset(None), asset("application/pdf"), asset("image/gif"), asset("image/avif"),
        asset(None, "mock-fallback.jpg"),
    )


def inventory_fixture():
    # Synthetic count/shape regression only; no real supplier names or files.
    return asset_report(
        *(asset(name=f"mock-{index:03}.jpg", kind="nested") for index in range(206)),
        *(asset("image/vnd.adobe.photoshop", f"mock-{index}.psd", kind="depth2") for index in range(2)),
        *(asset("video/quicktime", f"mock-{index:02}.mov") for index in range(31)),
        *(asset("video/mp4", f"mock-{index:02}.mp4") for index in range(8)),
        asset("application/octet-stream", "mock.bin"),
    )


class WebPOutputPolicyDryRunTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(self.enterContext(TemporaryDirectory()))
        self.output = self.project / "reports" / dry_run.REPORT_FILENAME
        self.denied = []
        for target in (
            "socket.socket.connect", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "sync_worker.cli.OfficialGoogleClientFactory",
            "sync_worker.google_api.OfficialGoogleClientFactory",
            "sync_worker.google_api.GoogleDriveMetadataGateway.list_folder_children",
            "sync_worker.http_client.ReadOnlyHttpClient.request",
            "sync_worker.folder_role_policy.classify_folder_role",
            "sync_worker.cli.run_folder_role_dry_run", "sync_worker.cli.run_image_asset_type_dry_run",
            "subprocess.run", "subprocess.Popen", "os.system",
        ):
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Offline JSON planning only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No configuration reads"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def file(self, report=None):
        path = self.project / "mock-assets.json"
        path.write_text(json.dumps(mixed_report() if report is None else report, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, path):
        with patch.object(cli, "PROJECT_ROOT", self.project):
            return cli.main(["plan-webp-output", "--asset-report", str(path)])

    def corrupt_core(self, **changes):
        original = core.WebPOutputPolicyResult.to_dict

        def projection(result):
            data = original(result)
            data.update(changes)
            return data

        return patch.object(core.WebPOutputPolicyResult, "to_dict", projection)

    def assert_denied(self, result):
        self.assertIs(result["source_asset_eligible"], False)
        self.assertIs(result["requires_webp_pipeline"], False)
        self.assertEqual(result["webp_action"], "not_allowed")
        self.assertIs(result["wordpress_upload_ready"], False)

    def assert_zero(self, key):
        result = build(mixed_report())
        self.assertEqual(result[key], 0)
        self.assertEqual(result["summary"][key], 0)

    def test_01_cli_registration(self):
        args = cli.build_parser().parse_args(["plan-webp-output", "--asset-report", "mock.json"])
        self.assertEqual(args.command, "plan-webp-output")
        self.assertEqual(args.asset_report_path, Path("mock.json"))

    def test_02_asset_report_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["plan-webp-output"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_invalid_report_status(self):
        for status in (None, "partial", "blocked", "failed", True, "OK"):
            with self.subTest(status=status), self.assertRaisesRegex(WebPOutputPolicyDryRunInputError, "asset_report_status_not_ok"):
                build(asset_report(asset(), status=status))

    def test_04_unsafe_report_field(self):
        for field in ("download_url", "provider_file_id", "media_id", "Authorization", "unused"):
            with self.subTest(field=field), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build(asset_report(asset(), **{field: "MOCK_ONLY"}))

    def test_05_url_input_rejected(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(WebPOutputPolicyDryRunInputError):
            dry_run.run_webp_output_policy_dry_run(Path("https://example.invalid/mock.json"), project_root=self.project)
        load.assert_not_called()

    def test_06_core_reused_with_restored_result_per_asset(self):
        source = mixed_report()
        with patch.object(core, "evaluate_webp_output_policy", wraps=core.evaluate_webp_output_policy) as evaluate:
            build(source)
        self.assertEqual(evaluate.call_count, len(source["results"]))
        for call, upstream in zip(evaluate.call_args_list, source["results"]):
            restored = call.args[0]
            self.assertIs(type(restored), asset_policy.ImageAssetTypeResult)
            self.assertEqual(restored.asset_class.value, upstream["asset_class"])
            self.assertEqual(restored.normalized_mime_type, upstream["normalized_mime_type"])
            self.assertEqual(restored.storefront_eligible, upstream["storefront_eligible"])

    def test_07_no_copied_mime_classification(self):
        source = mixed_report()
        with patch.object(asset_policy, "classify_image_asset_type", side_effect=AssertionError("No second classification")) as classify:
            build(source)
        classify.assert_not_called()
        tree = ast.parse(inspect.getsource(dry_run))
        literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertTrue(literals.isdisjoint({"image/vnd.adobe.photoshop", "video/quicktime", "video/mp4", "image/gif", "image/avif", ".jpg", ".png", ".psd", ".mp4"}))

    def test_08_jpeg_source_eligible(self):
        self.assertTrue(one()["source_asset_eligible"])

    def test_09_jpeg_convert_action(self):
        self.assertEqual(one()["webp_action"], "convert_to_webp")

    def test_10_jpeg_pipeline_required(self):
        self.assertTrue(one()["requires_webp_pipeline"])

    def test_11_jpeg_upload_false(self):
        self.assertIs(one()["wordpress_upload_ready"], False)

    def test_12_png_eligible(self):
        self.assertTrue(one("image/png")["source_asset_eligible"])

    def test_13_png_convert_action(self):
        self.assertEqual(one("image/png")["webp_action"], "convert_to_webp")

    def test_14_existing_webp_eligible(self):
        self.assertTrue(one("image/webp")["source_asset_eligible"])

    def test_15_existing_webp_validation_and_pipeline(self):
        result = one("image/webp")
        self.assertEqual(result["webp_action"], "validate_existing_webp")
        self.assertTrue(result["requires_webp_pipeline"])

    def test_16_existing_webp_upload_false(self):
        self.assertIs(one("image/webp")["wordpress_upload_ready"], False)

    def test_17_psd_not_allowed(self):
        result = one("image/vnd.adobe.photoshop")
        self.assert_denied(result)
        self.assertEqual(result["reason"], "design_source_not_storefront_asset")

    def test_18_video_not_allowed(self):
        for mime in ("video/quicktime", "video/mp4"):
            self.assert_denied(one(mime))

    def test_19_unsupported_not_allowed(self):
        self.assert_denied(one("application/octet-stream"))

    def test_20_unknown_not_allowed(self):
        self.assert_denied(one(None))

    def test_21_other_media_not_allowed(self):
        for mime in ("application/pdf", "audio/mpeg"):
            self.assert_denied(one(mime))

    def test_22_gif_upstream_ineligible(self):
        self.assert_denied(one("image/gif"))

    def test_23_avif_upstream_ineligible(self):
        self.assert_denied(one("image/avif"))

    def test_24_extension_fallback_not_allowed(self):
        result = one(None, name="mock.jpg")
        self.assert_denied(result)
        self.assertEqual(result["reason"], "mime_classification_required")

    def test_25_upstream_blocker_preserved(self):
        result = one(blocking_issues=["mock_source_blocker"])
        self.assert_denied(result)
        self.assertEqual(result["blocking_issues"], ["mock_source_blocker"])

    def test_26_target_mime_webp(self):
        self.assertTrue(all(row["target_mime_type"] == "image/webp" for row in build(mixed_report())["results"]))

    def test_27_target_extension_webp(self):
        self.assertTrue(all(row["target_extension"] == ".webp" for row in build(mixed_report())["results"]))

    def test_28_eligible_count(self):
        self.assertEqual(build(mixed_report())["summary"]["source_asset_eligible"], 3)

    def test_29_ineligible_count(self):
        self.assertEqual(build(mixed_report())["summary"]["source_asset_ineligible"], 9)

    def test_30_pipeline_count(self):
        self.assertEqual(build(mixed_report())["summary"]["requires_webp_pipeline"], 3)

    def test_31_convert_count(self):
        self.assertEqual(build(mixed_report())["summary"]["convert_to_webp"], 2)

    def test_32_validate_count(self):
        self.assertEqual(build(mixed_report())["summary"]["validate_existing_webp"], 1)

    def test_33_not_allowed_count(self):
        self.assertEqual(build(mixed_report())["summary"]["not_allowed"], 9)

    def test_34_upload_ready_always_zero(self):
        result = build(mixed_report())
        self.assertEqual(result["summary"]["wordpress_upload_ready"], 0)
        self.assertTrue(all(row["wordpress_upload_ready"] is False for row in result["results"]))

    def test_35_any_true_core_upload_flag_blocks_not_silently_repaired(self):
        with self.corrupt_core(wordpress_upload_ready=True):
            report = build()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["summary"]["blocking_assets"], 1)
        self.assertIn("wordpress_upload_ready_contract_violation", report["results"][0]["blocking_issues"])
        self.assert_denied(report["results"][0])
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            build(asset_report(asset(wordpress_upload_ready=True)))

    def test_36_synthetic_206_jpegs(self):
        rows = build(inventory_fixture())["results"][:206]
        self.assertEqual(len(rows), 206)
        self.assertTrue(all(row["source_asset_eligible"] and row["requires_webp_pipeline"] for row in rows))
        self.assertTrue(all(row["webp_action"] == "convert_to_webp" and not row["wordpress_upload_ready"] for row in rows))

    def test_37_synthetic_two_psds(self):
        rows = build(inventory_fixture())["results"][206:208]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["source_asset_class"], "design_source")
            self.assert_denied(row)

    def test_38_synthetic_39_videos(self):
        rows = build(inventory_fixture())["results"][208:247]
        self.assertEqual(len(rows), 39)
        self.assertEqual(sum(row["source_mime_type"] == "video/quicktime" for row in rows), 31)
        self.assertEqual(sum(row["source_mime_type"] == "video/mp4" for row in rows), 8)
        for row in rows:
            self.assert_denied(row)

    def test_39_synthetic_one_unsupported(self):
        row = build(inventory_fixture())["results"][-1]
        self.assertEqual(row["source_asset_class"], "unsupported")
        self.assertEqual(row["source_mime_type"], "application/octet-stream")
        self.assert_denied(row)

    def test_40_248_fixture_dynamic_summary(self):
        report = build(inventory_fixture())
        expected = {"total_assets": 248, "source_asset_eligible": 206, "source_asset_ineligible": 42,
                    "requires_webp_pipeline": 206, "convert_to_webp": 206, "validate_existing_webp": 0,
                    "not_allowed": 42, "wordpress_upload_ready": 0, "jpeg_sources": 206, "png_sources": 0,
                    "webp_sources": 0, "design_sources": 2, "video_sources": 39, "unsupported_sources": 1,
                    "unknown_sources": 0, "other_media_sources": 0}
        for key, value in expected.items():
            self.assertEqual(report["summary"][key], value)
        self.assertEqual(report["status"], "ok")

    def test_41_banner_name_does_not_add_folder_role_policy(self):
        result = one(kind="nested", safe_folder_name="Banner")
        self.assertTrue(result["source_asset_eligible"])
        self.assertNotIn("folder_role", result)
        for operation in self.denied:
            operation.assert_not_called()

    def test_42_hierarchy_audit_retained(self):
        upstream = asset(kind="depth2", sku="MOCK-XYZ", safe_folder_name="Mock Leaf", parent_safe_folder_name="Mock Parent")
        row = build(asset_report(upstream))["results"][0]
        for field in ("sku", "source_manifest_kind", "depth", "safe_folder_name", "parent_safe_folder_name", "safe_name", "product_source"):
            self.assertEqual(row[field], upstream[field])
        self.assertIsNone(one()["safe_folder_name"])

    def test_43_deterministic_upstream_order(self):
        source = asset_report(asset(name="mock-z.jpg"), asset(name="mock-a.jpg"), asset(name="mock-b.jpg"))
        first, second = build(source), build(copy.deepcopy(source))
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual([row["safe_name"] for row in first["results"]], ["mock-z.jpg", "mock-a.jpg", "mock-b.jpg"])

    def test_44_warnings_summary_counts_assets_not_messages(self):
        source = asset_report(asset(warnings=["mock_a", "mock_b"]), asset(), asset(warnings=["mock_c"]))
        result = build(source)
        self.assertEqual(result["summary"]["assets_with_warnings"], 2)
        self.assertEqual(result["results"][0]["warnings"], ["mock_a", "mock_b"])

    def test_45_blockers_summary(self):
        result = build(asset_report(asset(blocking_issues=["mock_a", "mock_b"]), asset()))
        self.assertEqual(result["summary"]["blocking_assets"], 1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["summary"]["source_asset_eligible"], 1)

    def test_46_raw_id_fields_rejected(self):
        for field in ("id", "raw_drive_id", "provider_file_id", "providerFileId", "media_id", "folder_id", "file_id_fingerprint"):
            with self.subTest(field=field), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build(asset_report(asset(**{field: "MOCK_RAW_ID"})))

    def test_47_drive_url_rejected_without_echo(self):
        value = "https://drive.google.com/file/d/MOCK_ONLY/view?resourcekey=MOCK_KEY"
        with self.assertRaises(WebPOutputPolicyDryRunInputError) as caught:
            one(safe_name=value)
        self.assertNotIn("MOCK_ONLY", str(caught.exception))
        self.assertNotIn("https://", json.dumps(build()))

    def test_48_local_paths_rejected_in_audit_names(self):
        for value in (r"C:\mock\image.jpg", "/mock/image.jpg", "../mock.jpg", "mock/image.jpg", r"\\mock\share\image.jpg"):
            for field in ("safe_name", "safe_folder_name", "parent_safe_folder_name", "sku"):
                with self.subTest(value=value, field=field), self.assertRaises(WebPOutputPolicyDryRunInputError):
                    one(kind="depth2", **{field: value})

    def test_49_wordpress_url_rejected(self):
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            one(safe_name="https://example.invalid/wp-json/wp/v2/media")
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            one(wordpress_url="https://example.invalid")

    def test_50_credentials_and_sensitive_text_rejected(self):
        for field in ("Authorization", "Cookie", "private_key", "client_email", "token", "secret", "credentials"):
            with self.subTest(field=field), self.assertRaises(WebPOutputPolicyDryRunInputError):
                one(**{field: "MOCK_ONLY"})
        for value in ("ck_" + "m" * 30, "token=MOCK_ONLY", "Cookie: MOCK_ONLY", "mock@example.invalid", "secret"):
            with self.subTest(value=value), self.assertRaises(WebPOutputPolicyDryRunInputError):
                one(safe_name=value)

    def test_51_network_zero(self):
        self.assert_zero("network_requests_performed")

    def test_52_download_zero(self):
        self.assert_zero("download_requests_performed")

    def test_53_conversion_zero(self):
        self.assert_zero("conversion_requests_performed")

    def test_54_wordpress_upload_zero(self):
        self.assert_zero("wordpress_upload_requests_performed")

    def test_55_external_write_zero(self):
        self.assert_zero("write_requests_performed")

    def test_56_no_conversion_imports(self):
        tree = ast.parse(inspect.getsource(dry_run))
        modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        modules.update(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        self.assertFalse(modules.intersection({"PIL", "Pillow", "wand", "cv2", "subprocess", "google_api", "http_client", "folder_role_policy"}))

    def test_57_no_imagemagick_execution(self):
        build(mixed_report())
        for operation in self.denied[11:14]:
            operation.assert_not_called()

    def test_58_no_cwebp_execution(self):
        one("image/png")
        for operation in self.denied[11:14]:
            operation.assert_not_called()

    def test_59_no_ffmpeg_execution(self):
        one("video/mp4")
        for operation in self.denied[11:14]:
            operation.assert_not_called()

    def test_60_only_one_local_asset_report_opened(self):
        path = self.file()
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as load:
            dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
        load.assert_called_once_with(path)
        source = mixed_report()
        with patch("builtins.open", side_effect=AssertionError("No files in builder")), patch("io.open", side_effect=AssertionError("No files in builder")):
            build(source)

    def test_61_no_media_bytes_read(self):
        path = self.file()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No media bytes")) as read:
            dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
        read.assert_not_called()

    def test_62_only_json_audit_written_no_media_files(self):
        path = self.file()
        original = Path.write_text
        writes = []

        def write(target, *args, **kwargs):
            writes.append(target)
            return original(target, *args, **kwargs)

        with patch.object(Path, "write_bytes", side_effect=AssertionError("No media output")), patch.object(Path, "write_text", write):
            dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
        self.assertEqual(writes, [self.output.with_name(self.output.name + ".tmp")])
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_63_cli_end_to_end_with_local_fixture(self):
        with self.assertLogs("sync_worker", level="INFO") as logs:
            self.assertEqual(self.run_cli(self.file()), 0)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["summary"]["total_assets"], 12)
        self.assertIn("webp_output_policy_dry_run_report_written", " ".join(logs.output))
        self.assertNotIn(str(self.project), " ".join(logs.output))

    def test_64_python_module_entry_with_mock_fixture(self):
        path = self.file()
        with patch.object(cli, "PROJECT_ROOT", self.project), patch.object(sys, "argv", ["sync_worker", "plan-webp-output", "--asset-report", str(path)]):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("sync_worker", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)

    def test_65_saved_report_round_trip(self):
        from sync_worker.image_asset_type_dry_run import build_image_asset_type_dry_run_report
        # Exercise the actual upstream schema, still with metadata-only mocks.
        empty = {"status": "ok", "results": []}
        root = {"status": "ok", "results": [{
            "sku": "MOCK-001", "product_source": {"start_row": 1, "end_row": 3},
            "items": [{"item_kind": "image_candidate", "safe_name": "mock.jpg", "mime_type": "image/jpeg"}],
        }]}
        upstream = build_image_asset_type_dry_run_report(root, empty, empty)
        report, path = dry_run.run_webp_output_policy_dry_run(self.file(upstream), project_root=self.project)
        self.assertEqual(path, self.output)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
        self.assertEqual(report["policy_version"], core.POLICY_VERSION)
        self.assertEqual(report["source_policy_version"], asset_policy.POLICY_VERSION)
        self.assertEqual(report["summary"]["total_assets"], 1)
        self.assertIsNone(report["results"][0]["safe_folder_name"])

    def test_66_wrong_report_types_rejected(self):
        for source in ({"status": "ok", "results": [{"items": []}]},
                       {"status": "ok", "policy_version": "xxxxdoll-folder-role-v1", "summary": {}, "results": []},
                       {"type": "service_account", "private_key": "MOCK_ONLY"}):
            with self.subTest(source=source), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build(source)

    def test_67_root_version_required_and_exact(self):
        for version in (None, "xxxxdoll-image-asset-type-v2", core.POLICY_VERSION):
            with self.subTest(version=version), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build(asset_report(asset(), policy_version=version))
        source = asset_report(asset())
        source.pop("policy_version")
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            build(source)

    def test_68_asset_version_required_and_exact(self):
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            one(policy_version="unknown-policy")

    def test_69_malformed_report_shapes(self):
        for source in ([], "mock", {"status": "ok"}, asset_report(results=None), asset_report(results={}), asset_report(results=[None])):
            with self.subTest(source=source), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build(source)

    def test_70_all_input_validated_before_core(self):
        source = asset_report(asset(), asset(depth="bad"))
        with patch.object(core, "evaluate_webp_output_policy") as evaluate, self.assertRaises(WebPOutputPolicyDryRunInputError):
            build(source)
        evaluate.assert_not_called()

    def test_71_boolean_not_truthy_coerced(self):
        for value in ("true", "false", 1, 0, None):
            with self.subTest(value=value), self.assertRaises(WebPOutputPolicyDryRunInputError):
                one(storefront_eligible=value)

    def test_72_invalid_asset_class_source_and_status(self):
        for changes in ({"asset_class": "image"}, {"asset_class": []}, {"classification_source": "extension"},
                        {"classification_source": []}, {"status": "ok"}, {"status": []}, {"warnings": "mock"}):
            with self.subTest(changes=changes), self.assertRaises(WebPOutputPolicyDryRunInputError):
                one(**changes)

    def test_73_depth_and_kind_must_agree(self):
        for changes in ({"depth": True}, {"depth": 1}, {"depth": -1}, {"source_manifest_kind": "depth3"}, {"source_manifest_kind": []}):
            with self.subTest(changes=changes), self.assertRaises(WebPOutputPolicyDryRunInputError):
                one(**changes)
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            one(kind="depth2", parent_safe_folder_name=None)

    def test_74_product_source_rows_validated(self):
        for source in ([], {"start_row": 0, "end_row": 1}, {"start_row": 5, "end_row": 4},
                       {"start_row": True, "end_row": 10}, {"start_row": 1, "end_row": 2, "file_id": "MOCK"}):
            with self.subTest(source=source), self.assertRaises(WebPOutputPolicyDryRunInputError):
                one(product_source=source)

    def test_75_audit_numbers_validated_not_used_for_eligibility(self):
        for field in ("size_bytes", "image_width", "image_height"):
            self.assertTrue(one(**{field: 0})["source_asset_eligible"])
            for value in (-1, "1", True, 1.5):
                with self.subTest(field=field, value=value), self.assertRaises(WebPOutputPolicyDryRunInputError):
                    one(**{field: value})

    def test_76_counts_ignore_upstream_summary_totals(self):
        source = asset_report(asset(), summary={"classified_assets": 99999, "storefront_eligible_assets": 88888})
        result = build(source)
        self.assertEqual(result["summary"]["total_assets"], 1)
        self.assertEqual(result["summary"]["source_asset_eligible"], 1)
        for key in ("network_requests_performed", "download_requests_performed", "write_requests_performed"):
            with self.subTest(key=key), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build(asset_report(asset(), **{key: 1}))

    def test_77_empty_report_zero_counts(self):
        report = build(asset_report())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["results"], [])
        self.assertTrue(all(value == 0 for value in report["summary"].values()))

    def test_78_no_deduplication(self):
        upstream = asset()
        report = build(asset_report(upstream, copy.deepcopy(upstream)))
        self.assertEqual(report["summary"]["total_assets"], 2)
        self.assertEqual(report["results"][0], report["results"][1])

    def test_79_input_object_and_file_unchanged(self):
        source = mixed_report()
        original = copy.deepcopy(source)
        build(source)
        self.assertEqual(source, original)
        path = self.file(source)
        content = path.read_text(encoding="utf-8")
        dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_80_filename_or_raw_mime_not_reclassified(self):
        row = one(safe_name="mock.psd", safe_extension=".psd", mime_type="image/vnd.adobe.photoshop")
        self.assertEqual(row["source_asset_class"], "web_image")
        self.assertEqual(row["source_mime_type"], "image/jpeg")
        self.assertEqual(row["webp_action"], "convert_to_webp")
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            one(normalized_mime_type="IMAGE/JPEG")

    def test_81_core_decision_not_recomputed(self):
        sentinel = core.WebPOutputPolicyResult(
            asset_policy.AssetClass.WEB_IMAGE, "image/jpeg", False, False,
            core.WebPAction.NOT_ALLOWED, reason="mock_core_decision",
        )
        with patch.object(core, "evaluate_webp_output_policy", return_value=sentinel):
            row = one()
        self.assert_denied(row)
        self.assertEqual(row["reason"], "mock_core_decision")

    def test_82_invalid_target_mime_blocker(self):
        with self.corrupt_core(target_mime_type="image/png"):
            report = build()
        self.assertEqual(report["status"], "blocked")
        row = report["results"][0]
        self.assertIn("invalid_webp_target_contract", row["blocking_issues"])
        self.assert_denied(row)
        self.assertEqual(row["target_mime_type"], "image/webp")

    def test_83_invalid_target_extension_blocker(self):
        with self.corrupt_core(target_extension=".jpg"):
            report = build()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("invalid_webp_target_contract", report["results"][0]["blocking_issues"])
        self.assertEqual(report["results"][0]["target_extension"], ".webp")

    def test_84_invalid_core_boolean_rejected(self):
        for field in ("source_asset_eligible", "requires_webp_pipeline", "wordpress_upload_ready"):
            with self.subTest(field=field), self.corrupt_core(**{field: "false"}), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build()

    def test_85_invalid_core_shape_or_audit_rejected(self):
        for changes in ({"policy_version": "wrong"}, {"source_asset_class": "video"}, {"source_mime_type": "image/png"},
                        {"webp_action": "upload"}, {"media_id": "MOCK_ONLY"}, {"reason": "https://example.invalid/MOCK"}):
            with self.subTest(changes=changes), self.corrupt_core(**changes), self.assertRaises(WebPOutputPolicyDryRunInputError):
                build()
        with patch.object(core, "evaluate_webp_output_policy", return_value={}), self.assertRaises(WebPOutputPolicyDryRunInputError):
            build()

    def test_86_non_json_paths_rejected_before_load(self):
        for name in (".env", "mock.jpg", "mock.webp", "mock.psd", "mock.mp4", "mock.txt"):
            with self.subTest(name=name), patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(WebPOutputPolicyDryRunInputError):
                dry_run.run_webp_output_policy_dry_run(self.project / name, project_root=self.project)
            load.assert_not_called()

    def test_87_known_credential_paths_rejected_before_load(self):
        for name in ("credentials.json", "service-account.json", "google-service-account.json", "client_secret.json", "token.json", ".env.json"):
            with self.subTest(name=name), patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(WebPOutputPolicyDryRunInputError):
                dry_run.run_webp_output_policy_dry_run(self.project / name, project_root=self.project)
            load.assert_not_called()

    def test_88_known_other_workflow_paths_rejected_before_load(self):
        for name in ("google-drive-folder-manifest-dry-run.json", "google-drive-nested-folder-manifest-dry-run.json",
                     "google-drive-depth2-folder-manifest-dry-run.json", "folder-role-dry-run.json"):
            with self.subTest(name=name), patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(WebPOutputPolicyDryRunInputError):
                dry_run.run_webp_output_policy_dry_run(self.project / name, project_root=self.project)
            load.assert_not_called()

    def test_89_remote_filesystem_rejected_before_stat(self):
        for name in ("https://example.invalid/mock.json", "file://mock/report.json", r"\\mock\share\report.json"):
            with self.subTest(name=name), patch.object(Path, "lstat", side_effect=AssertionError("No remote filesystem")) as access:
                with self.assertRaises(WebPOutputPolicyDryRunInputError):
                    dry_run.run_webp_output_policy_dry_run(Path(name), project_root=self.project)
            access.assert_not_called()

    def test_90_input_symlink_and_parent_junction_blocked(self):
        path = self.file()
        original = Path.lstat
        for linked, info in ((path, SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)),
                             (self.project, SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))):
            def lstat(target, **kwargs):
                return info if target == linked else original(target, **kwargs)
            with patch.object(Path, "lstat", lstat), patch.object(dry_run, "load_local_json_report") as load:
                with self.assertRaises(WebPOutputPolicyDryRunInputError):
                    dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
                load.assert_not_called()

    def test_91_output_and_temporary_links_blocked(self):
        path = self.file()
        self.output.parent.mkdir()
        original = Path.lstat
        for linked in (self.output, self.output.with_name(self.output.name + ".tmp")):
            def lstat(target, **kwargs):
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0) if target == linked else original(target, **kwargs)
            with patch.object(Path, "lstat", lstat), patch.object(dry_run, "load_local_json_report") as load:
                with self.assertRaises(WebPOutputPolicyDryRunInputError):
                    dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
                load.assert_not_called()

    def test_92_output_input_collision_blocked(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaisesRegex(WebPOutputPolicyDryRunInputError, "asset_report_output_collision"):
            dry_run.run_webp_output_policy_dry_run(self.output, project_root=self.project)
        load.assert_not_called()

    def test_93_invalid_json_and_missing_file_safe_errors(self):
        path = self.file()
        for content in ("{", "null", "[]"):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(WebPOutputPolicyDryRunInputError) as caught:
                dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
            self.assertNotIn(str(path), str(caught.exception))
        with self.assertRaises(WebPOutputPolicyDryRunInputError):
            dry_run.run_webp_output_policy_dry_run(self.project / "missing.json", project_root=self.project)
        self.assertFalse(self.output.exists())

    def test_94_cli_safety_error_does_not_leak_input(self):
        path = self.file(asset_report(asset(safe_name="https://example.invalid/MOCK_SECRET")))
        with self.assertLogs("sync_worker", level="ERROR") as logs:
            self.assertEqual(self.run_cli(path), 2)
        output = " ".join(logs.output)
        self.assertIn("webp_output_policy_dry_run_aborted", output)
        self.assertNotIn("MOCK_SECRET", output)
        self.assertNotIn("https://", output)
        self.assertNotIn(str(path), output)

    def test_95_unexpected_exception_not_logged_raw(self):
        path = self.file()
        with patch.object(cli, "run_webp_output_policy_dry_run", side_effect=RuntimeError("Authorization: MOCK_SECRET C:\\mock\\private.json")):
            with self.assertLogs("sync_worker", level="ERROR") as logs:
                self.assertEqual(self.run_cli(path), 2)
        output = " ".join(logs.output)
        self.assertNotIn("MOCK_SECRET", output)
        self.assertNotIn("Authorization", output)
        self.assertNotIn("private.json", output)

    def test_96_write_error_safe_without_path(self):
        path = self.file()
        with patch.object(dry_run.SafeJsonReportWriter, "write", side_effect=PermissionError("MOCK_SECRET C:\\mock\\private.json")):
            with self.assertRaisesRegex(WebPOutputPolicyDryRunInputError, "webp_plan_report_write_failed") as caught:
                dry_run.run_webp_output_policy_dry_run(path, project_root=self.project)
        self.assertNotIn("MOCK_SECRET", str(caught.exception))

    def test_97_invalid_input_does_not_overwrite_stale_report(self):
        self.output.parent.mkdir()
        self.output.write_text('{"mock_stale": true}', encoding="utf-8")
        path = self.file(asset_report(asset(), status="partial"))
        with self.assertLogs("sync_worker", level="ERROR"):
            self.assertEqual(self.run_cli(path), 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), '{"mock_stale": true}')

    def test_98_blocked_cli_returns_one_and_persists_closed_audit(self):
        path = self.file(asset_report(asset()))
        with self.corrupt_core(wordpress_upload_ready=True):
            self.assertEqual(self.run_cli(path), 1)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["summary"]["wordpress_upload_ready"], 0)
        self.assert_denied(report["results"][0])

        with self.corrupt_core(requires_webp_pipeline=False):
            self.assertEqual(self.run_cli(path), 1)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertIn("invalid_webp_pipeline_contract", report["results"][0]["blocking_issues"])
        self.assert_denied(report["results"][0])

    def test_99_environment_cannot_change_targets_or_create_clients(self):
        path = self.file()
        with patch.dict("os.environ", {"HTTP_PROXY": "https://example.invalid", "WORDPRESS_UPLOAD_READY": "true", "WEBP_TARGET_MIME": "image/png"}):
            self.assertEqual(self.run_cli(path), 0)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertTrue(all(row["target_mime_type"] == "image/webp" and row["wordpress_upload_ready"] is False for row in report["results"]))

    def test_100_metadata_only_projection_and_json_serialization(self):
        report = build()
        expected = {"sku", "source_manifest_kind", "depth", "safe_folder_name", "parent_safe_folder_name", "safe_name",
                    "product_source", "source_asset_class", "source_mime_type", "source_asset_eligible", "requires_webp_pipeline",
                    "webp_action", "target_mime_type", "target_extension", "wordpress_upload_ready", "policy_version",
                    "warnings", "blocking_issues", "reason"}
        self.assertEqual(set(report["results"][0]), expected)
        self.assertEqual(json.loads(json.dumps(report)), report)
        for forbidden in ("timestamp", "input_file", "input_path", "folder_role", "file_id", "download_url", "media_id"):
            self.assertNotIn(forbidden, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
