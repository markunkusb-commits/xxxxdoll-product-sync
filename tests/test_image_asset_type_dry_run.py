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
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli, image_asset_type_dry_run as dry_run, image_asset_type_policy as policy
from sync_worker.image_asset_type_dry_run import ImageAssetTypeDryRunInputError


def item(mime="image/jpeg", name="mock.jpg", kind="image_candidate", **overrides):
    return {
        "safe_name": name, "mime_type": mime, "item_kind": kind,
        "size_bytes": 100, "image_width": 0, "image_height": 0,
        "file_id_fingerprint": "sha256:" + "a" * 64,
        "image_candidate": kind == "image_candidate", "warnings": [], **overrides,
    }


def folder(kind, *items, name=None, sku="MOCK-001", **overrides):
    result = {
        "sku": sku, "product_source": {"start_row": 10, "end_row": 20},
        "status": "listed", "items": list(items), "item_count": len(items),
        "pages_read": 1, "warnings": [], "blocking_issues": [],
    }
    if kind == "root":
        result["folder_id_fingerprint"] = "sha256:" + "b" * 64
        if name is not None:
            result["safe_folder_name"] = name
    elif kind == "nested":
        result.update({"depth": 1, "safe_folder_name": name or "Mock Nested",
                       "root_folder_id_fingerprint": "sha256:" + "b" * 64,
                       "nested_folder_id_fingerprint": "sha256:" + "c" * 64})
    else:
        result.update({"depth": 2, "depth2_safe_folder_name": name or "Mock Depth2",
                       "depth1_safe_folder_name": "Mock Parent",
                       "root_folder_id_fingerprint": "sha256:" + "b" * 64,
                       "depth1_folder_id_fingerprint": "sha256:" + "c" * 64,
                       "depth2_folder_id_fingerprint": "sha256:" + "d" * 64})
    result.update(overrides)
    return result


def manifest(*folders):
    return {"status": "ok", "results": list(folders), "write_requests_performed": 0,
            "summary": {"network_requests_performed": 17, "total_items": 9999}}


def build(root=None, nested=None, depth2=None):
    return dry_run.build_image_asset_type_dry_run_report(
        manifest(folder("root", item())) if root is None else root,
        manifest() if nested is None else nested,
        manifest() if depth2 is None else depth2,
    )


def one_file(file_item=None, *, kind="root", **context):
    reports = [manifest(), manifest(), manifest()]
    reports[("root", "nested", "depth2").index(kind)] = manifest(folder(kind, file_item or item(), **context))
    return build(*reports)["results"][0]


def mixed_reports():
    return (
        manifest(folder("root", item(name="mock-root.jpg"),
                        item("video/mp4", "mock.mp4", "other_file"),
                        item(name="folder.jpg", kind="nested_folder"),
                        item(name="shortcut.psd", kind="shortcut"))),
        manifest(folder("nested", item("image/png", "mock.png"),
                        item("image/vnd.adobe.photoshop", "mock.psd"),
                        item("application/pdf", "mock.pdf", "other_file"),
                        item("application/vnd.google-apps.document", "mock-doc", "google_workspace_file"),
                        item(None, "mock-unknown", "other_file"),
                        item(name="shortcut.jpg", kind="shortcut"))),
        manifest(folder("depth2", item("image/webp", "mock.webp"),
                        item("application/octet-stream", "mock-fallback.jpg", "other_file"),
                        item("image/gif", "mock.gif"), item("image/jpeg", "mock-mismatch.psd"),
                        item(name="folder.psd", kind="nested_folder"))),
    )


def inventory_fixture():
    # Synthetic files only: reproduce the supplied counts, never real reports.
    return (
        manifest(folder("root", item("video/mp4", "mock-root.mp4", "other_file"))),
        manifest(folder("nested", *(item(name=f"mock-depth1-{n:03}.jpg") for n in range(164)),
                        item("image/vnd.adobe.photoshop", "mock-depth1.psd"))),
        manifest(folder("depth2", *(item(name=f"mock-depth2-{n:03}.jpg") for n in range(42)),
                        item("image/vnd.adobe.photoshop", "mock-depth2.psd"))),
    )


class ImageAssetTypeDryRunTests(unittest.TestCase):
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
            "sync_worker.cli.run_folder_role_dry_run",
        ):
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Offline asset metadata only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No configuration reads"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def files(self, reports=None):
        reports = mixed_reports() if reports is None else reports
        paths = tuple(self.project / name for name in ("root.json", "nested.json", "depth2.json"))
        for path, report in zip(paths, reports):
            path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return paths

    def arguments(self, paths):
        return ["classify-image-asset-types", "--root-manifest", str(paths[0]),
                "--nested-manifest", str(paths[1]), "--depth2-manifest", str(paths[2])]

    def run_cli(self, paths):
        with patch.object(cli, "PROJECT_ROOT", self.project):
            return cli.main(self.arguments(paths))

    def required(self, flag):
        args = self.arguments(("root.json", "nested.json", "depth2.json"))
        index = args.index(flag)
        del args[index:index + 2]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(args)
        self.assertEqual(caught.exception.code, 2)

    def invalid_status(self, index):
        for status in (None, "partial", "error", "OK", True):
            reports = list(mixed_reports())
            reports[index]["status"] = status
            with self.subTest(status=status), self.assertRaises(ImageAssetTypeDryRunInputError):
                build(*reports)

    def test_01_cli_registered(self):
        args = cli.build_parser().parse_args(self.arguments(("root.json", "nested.json", "depth2.json")))
        self.assertEqual(args.command, "classify-image-asset-types")
        self.assertEqual((args.root_manifest_path, args.nested_manifest_path, args.depth2_manifest_path),
                         (Path("root.json"), Path("nested.json"), Path("depth2.json")))

    def test_02_root_required(self):
        self.required("--root-manifest")

    def test_03_nested_required(self):
        self.required("--nested-manifest")

    def test_04_depth2_required(self):
        self.required("--depth2-manifest")

    def test_05_root_status_ok_required(self):
        self.invalid_status(0)

    def test_06_nested_status_ok_required(self):
        self.invalid_status(1)

    def test_07_depth2_status_ok_required(self):
        self.invalid_status(2)

    def test_08_root_file_classification(self):
        result = one_file()
        self.assertEqual(result["depth"], 0)
        self.assertEqual(result["asset_class"], "web_image")

    def test_09_depth1_file_classification(self):
        result = one_file(kind="nested")
        self.assertEqual(result["depth"], 1)
        self.assertEqual(result["asset_class"], "web_image")

    def test_10_depth2_file_classification(self):
        result = one_file(kind="depth2")
        self.assertEqual(result["depth"], 2)
        self.assertEqual(result["asset_class"], "web_image")

    def test_11_image_candidates_processed(self):
        self.assertEqual(one_file(item(image_candidate=False))["asset_class"], "web_image")

    def test_12_other_files_processed(self):
        self.assertEqual(one_file(item("video/mp4", "mock.mp4", "other_file"))["asset_class"], "video")

    def test_13_workspace_files_processed(self):
        result = one_file(item("application/vnd.google-apps.document", "mock-doc", "google_workspace_file"))
        self.assertEqual(result["asset_class"], "unsupported")
        self.assertFalse(result["storefront_eligible"])

    def test_14_nested_folder_skipped_without_policy_call(self):
        with patch.object(policy, "classify_image_asset_type") as classify:
            report = build(manifest(folder("root", item(name="mock.jpg", kind="nested_folder"))))
        classify.assert_not_called()
        self.assertEqual(report["results"], [])
        self.assertEqual(report["summary"]["skipped_nested_folders"], 1)

    def test_15_shortcut_skipped_without_extension_inference(self):
        with patch.object(policy, "classify_image_asset_type") as classify:
            report = build(manifest(folder("root", item(name="mock.jpg", kind="shortcut"), item(name="mock.psd", kind="shortcut"))))
        classify.assert_not_called()
        self.assertEqual(report["results"], [])
        self.assertEqual(report["summary"]["skipped_shortcuts"], 2)

    def test_16_jpeg(self):
        result = one_file()
        self.assertEqual((result["asset_class"], result["classification_source"], result["storefront_eligible"]), ("web_image", "mime", True))

    def test_17_png(self):
        self.assertEqual(one_file(item("image/png", "mock.png"))["asset_class"], "web_image")

    def test_18_webp(self):
        self.assertEqual(one_file(item("image/webp", "mock.webp"))["asset_class"], "web_image")

    def test_19_psd(self):
        result = one_file(item("image/vnd.adobe.photoshop", "mock.psd"))
        self.assertEqual((result["asset_class"], result["classification_source"]), ("design_source", "mime"))

    def test_20_mp4(self):
        self.assertEqual(one_file(item("video/mp4", "mock.mp4", "other_file"))["asset_class"], "video")

    def test_21_pdf(self):
        self.assertEqual(one_file(item("application/pdf", "mock.pdf", "other_file"))["asset_class"], "other_media")

    def test_22_unknown(self):
        result = one_file(item(None, "mock-unknown", "other_file"))
        self.assertEqual(result["asset_class"], "unknown")
        self.assertIn("asset_mime_unknown", result["warnings"])
        self.assertEqual(result["blocking_issues"], [])

    def test_23_core_reused_for_each_file(self):
        with patch.object(policy, "classify_image_asset_type", wraps=policy.classify_image_asset_type) as classify:
            build(*mixed_reports())
        self.assertEqual(classify.call_count, 11)
        self.assertEqual(classify.call_args_list[0].args, ("image/jpeg", "mock-root.jpg"))
        self.assertEqual(classify.call_args_list[0].kwargs, {"size_bytes": 100, "image_width": 0, "image_height": 0, "sku": "MOCK-001"})

    def test_24_no_copied_mime_rules(self):
        source = inspect.getsource(dry_run)
        constants = {node.value for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertTrue(constants.isdisjoint({"image/jpeg", "image/png", "image/webp", "image/vnd.adobe.photoshop", "video/", "audio/", ".psd", ".jpg"}))
        for private_rule in ("_STOREFRONT_MIMES", "_EXTENSION_MIMES", "_EXTENSION_FALLBACK_CLASSES"):
            self.assertNotIn(private_rule, source)

    def test_25_mime_storefront_eligibility(self):
        for mime, name in (("image/jpeg", "mock.jpg"), ("image/png", "mock.png"), ("image/webp", "mock.webp")):
            self.assertTrue(one_file(item(mime, name))["storefront_eligible"])

    def test_26_psd_candidate_not_approved(self):
        result = one_file(item("image/vnd.adobe.photoshop", "mock.psd", image_candidate=True))
        self.assertFalse(result["storefront_eligible"])

    def test_27_video_not_approved(self):
        for mime in ("video/mp4", "video/webm", "video/quicktime"):
            self.assertFalse(one_file(item(mime, "mock-video", "other_file"))["storefront_eligible"])

    def test_28_extension_audit(self):
        result = one_file(item(name="mock.JPG"))
        self.assertEqual(result["safe_extension"], ".jpg")
        self.assertEqual(result["safe_name"], "mock.JPG")

    def test_29_mismatch_warning_mime_still_wins(self):
        result = one_file(item("image/jpeg", "mock.psd"))
        self.assertEqual(result["asset_class"], "web_image")
        self.assertTrue(result["storefront_eligible"])
        self.assertIn("asset_extension_mime_mismatch", result["warnings"])

    def test_30_generic_mime_fallback(self):
        result = one_file(item("application/octet-stream", "mock.jpg", "other_file"))
        self.assertEqual(result["classification_source"], "extension_fallback")
        self.assertEqual(result["status"], "extension_fallback_candidate")
        self.assertFalse(result["storefront_eligible"])
        self.assertIn("mime_verification_required", result["warnings"])

    def test_31_hierarchy_context(self):
        result = one_file(kind="depth2", name="Mock Child", depth1_safe_folder_name="Mock Parent", sku="MOCK-A")
        self.assertEqual(result["safe_folder_name"], "Mock Child")
        self.assertEqual(result["parent_safe_folder_name"], "Mock Parent")
        self.assertEqual(result["sku"], "MOCK-A")
        self.assertEqual(result["product_source"], {"start_row": 10, "end_row": 20})

    def test_32_depth_retained(self):
        for depth, kind in enumerate(("root", "nested", "depth2")):
            self.assertEqual(one_file(kind=kind)["depth"], depth)

    def test_33_source_manifest_kind_retained(self):
        for kind in ("root", "nested", "depth2"):
            self.assertEqual(one_file(kind=kind)["source_manifest_kind"], kind)

    def test_34_no_folder_role_dependency_or_output(self):
        source = inspect.getsource(dry_run)
        self.assertNotIn("folder_role", source)
        result = one_file(item(folder_role="video", gallery_eligible=False), kind="nested", folder_role="banner")
        self.assertNotIn("folder_role", result)
        self.assertNotIn("gallery_eligible", result)
        self.assertTrue(result["storefront_eligible"])

    def test_35_banner_jpeg_type_eligible(self):
        for name in ("Banner-Mock", "Photos-Mock", "Factory Photos-Mock", "Video-Mock"):
            self.assertTrue(one_file(kind="nested", name=name)["storefront_eligible"])

    def test_36_deterministic_sorting(self):
        reports = mixed_reports()
        first = build(*reports)
        for report in reports:
            report["results"].reverse()
            for result in report["results"]:
                result["items"].reverse()
        second = build(*reports)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        keys = [(r["sku"], r["depth"], r["safe_folder_name"] or "", r["safe_name"], r["normalized_mime_type"] or "") for r in first["results"]]
        self.assertEqual(keys, sorted(keys))

    def test_37_summary_total_seen(self):
        self.assertEqual(build(*mixed_reports())["summary"]["total_manifest_items_seen"], 15)

    def test_38_summary_classified(self):
        self.assertEqual(build(*mixed_reports())["summary"]["classified_assets"], 11)

    def test_39_summary_skipped_folders(self):
        self.assertEqual(build(*mixed_reports())["summary"]["skipped_nested_folders"], 2)

    def test_40_summary_skipped_shortcuts(self):
        self.assertEqual(build(*mixed_reports())["summary"]["skipped_shortcuts"], 2)

    def test_41_asset_class_counts(self):
        summary = build(*mixed_reports())["summary"]
        for key, count in {"web_image": 6, "design_source": 1, "video": 1, "other_media": 1, "unsupported": 1, "unknown": 1}.items():
            self.assertEqual(summary[key], count)

    def test_42_eligibility_counts(self):
        summary = build(*mixed_reports())["summary"]
        self.assertEqual(summary["storefront_eligible_assets"], 4)
        self.assertEqual(summary["storefront_ineligible_assets"], 7)

    def test_43_warning_counts(self):
        self.assertEqual(build(*mixed_reports())["summary"]["assets_with_warnings"], 5)

    def test_44_root_count(self):
        self.assertEqual(build(*mixed_reports())["summary"]["root_assets"], 2)

    def test_45_depth1_count(self):
        self.assertEqual(build(*mixed_reports())["summary"]["depth1_assets"], 5)

    def test_46_depth2_count(self):
        self.assertEqual(build(*mixed_reports())["summary"]["depth2_assets"], 4)

    def test_47_raw_ids_rejected_and_fingerprints_absent(self):
        for field in ("id", "file_id", "raw_folder_id", "provider_file_id", "providerFileId"):
            with self.subTest(field=field), self.assertRaises(ImageAssetTypeDryRunInputError):
                one_file(item(**{field: "MOCK_RAW_ID"}))
        output = json.dumps(build())
        self.assertNotIn("fingerprint", output)
        self.assertNotIn("provider_file_id", output)

    def test_48_url_rejected_without_leak(self):
        value = "https://drive.google.com/file/d/MOCK_ONLY/view?resourcekey=MOCK_KEY"
        with self.assertRaises(ImageAssetTypeDryRunInputError) as caught:
            one_file(item(name=value))
        self.assertNotIn("MOCK_ONLY", str(caught.exception))
        self.assertNotIn("https://", json.dumps(build()))

    def test_49_network_zero(self):
        report = build(*mixed_reports())
        self.assertEqual(report["network_requests_performed"], 0)
        self.assertEqual(report["summary"]["network_requests_performed"], 0)

    def test_50_downloads_zero(self):
        report = build(*mixed_reports())
        self.assertEqual(report["download_requests_performed"], 0)
        self.assertEqual(report["summary"]["download_requests_performed"], 0)

    def test_51_writes_zero(self):
        report = build(*mixed_reports())
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(report["summary"]["write_requests_performed"], 0)

    def test_52_input_immutable(self):
        reports = mixed_reports()
        original = copy.deepcopy(reports)
        build(*reports)
        self.assertEqual(reports, original)

    def test_53_synthetic_206_jpegs(self):
        report = build(*inventory_fixture())
        jpegs = [r for r in report["results"] if r["normalized_mime_type"] == "image/jpeg"]
        self.assertEqual(len(jpegs), 206)
        self.assertTrue(all(r["asset_class"] == "web_image" and r["classification_source"] == "mime" and r["storefront_eligible"] for r in jpegs))

    def test_54_synthetic_two_psds(self):
        report = build(*inventory_fixture())
        psds = [r for r in report["results"] if r["normalized_mime_type"] == "image/vnd.adobe.photoshop"]
        self.assertEqual(len(psds), 2)
        self.assertTrue(all(r["asset_class"] == "design_source" and r["classification_source"] == "mime" and not r["storefront_eligible"] for r in psds))
        self.assertEqual(report["summary"]["classified_assets"], 209)

    def test_55_only_three_local_reports_loaded(self):
        paths = self.files()
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as load:
            dry_run.run_image_asset_type_dry_run(*paths, project_root=self.project)
        self.assertEqual([call.args for call in load.call_args_list], [(path,) for path in paths])

    def test_56_report_saved_round_trip(self):
        report, path = dry_run.run_image_asset_type_dry_run(*self.files(), project_root=self.project)
        self.assertEqual(path, self.output)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_57_cli_end_to_end_with_fixtures(self):
        with self.assertLogs("sync_worker", level="INFO") as logs:
            self.assertEqual(self.run_cli(self.files()), 0)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["summary"]["classified_assets"], 11)
        self.assertIn("image_asset_type_dry_run_report_written", " ".join(logs.output))

    def test_58_cli_does_not_read_configuration_or_create_clients(self):
        self.assertEqual(self.run_cli(self.files()), 0)
        for operation in self.denied:
            operation.assert_not_called()

    def test_59_failed_input_does_not_overwrite_stale_output(self):
        self.output.parent.mkdir()
        self.output.write_text('{"mock_stale": true}', encoding="utf-8")
        reports = mixed_reports()
        reports[2]["status"] = "partial"
        with self.assertLogs("sync_worker", level="ERROR"):
            self.assertEqual(self.run_cli(self.files(reports)), 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), '{"mock_stale": true}')

    def test_60_all_statuses_checked_before_core(self):
        reports = mixed_reports()
        reports[2]["status"] = "partial"
        with patch.object(policy, "classify_image_asset_type") as classify, self.assertRaises(ImageAssetTypeDryRunInputError):
            build(*reports)
        classify.assert_not_called()

    def test_61_actual_root_schema_without_name_or_depth(self):
        from sync_worker.google_drive_folder_manifest import GoogleDriveFolderManifest, _manifest_item
        from sync_worker.google_drive_folder_manifest_dry_run import _manifest_report
        from sync_worker.image_mapping import ProductSourceRange
        core = GoogleDriveFolderManifest("MOCK", ProductSourceRange(1, 10), "a" * 64,
                                         "listed", (_manifest_item({"name": "mock.mp4", "mimeType": "video/mp4"}),), 1)
        safe = _manifest_report(core)
        self.assertNotIn("safe_folder_name", safe)
        self.assertNotIn("depth", safe)
        result = build(manifest(safe))["results"][0]
        self.assertIsNone(result["safe_folder_name"])
        self.assertIsNone(result["parent_safe_folder_name"])
        self.assertEqual(result["depth"], 0)
        self.assertEqual(result["asset_class"], "video")

    def test_62_explicit_root_name_preserved_without_lookup(self):
        self.assertEqual(one_file(name="Mock Root")["safe_folder_name"], "Mock Root")

    def test_63_wrong_depth_rejected(self):
        for kind, expected in (("root", 0), ("nested", 1), ("depth2", 2)):
            for value in (True, None, 3, str(expected), float(expected)):
                with self.subTest(kind=kind, value=value), self.assertRaises(ImageAssetTypeDryRunInputError):
                    one_file(kind=kind, depth=value)

    def test_64_missing_nested_depth_not_invented(self):
        record = folder("nested", item())
        record.pop("depth")
        with self.assertRaises(ImageAssetTypeDryRunInputError):
            build(manifest(), manifest(record))

    def test_65_unknown_item_kind_blocked(self):
        for kind in (None, [], "unknown", "image_file", "file"):
            with self.subTest(kind=kind), self.assertRaises(ImageAssetTypeDryRunInputError):
                one_file(item(kind=kind))

    def test_66_no_fingerprint_classification_or_dedup(self):
        report = manifest(folder("root", item(file_id_fingerprint="video"), item(file_id_fingerprint="design_source")))
        result = build(report)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(row["asset_class"] == "web_image" for row in result["results"]))
        self.assertNotIn("fingerprint", json.dumps(result))

    def test_67_size_dimensions_audit_only(self):
        result = one_file(item(size_bytes=0, image_width=0, image_height=0))
        self.assertTrue(result["storefront_eligible"])
        self.assertEqual((result["size_bytes"], result["image_width"], result["image_height"]), (0, 0, 0))

    def test_68_mime_fallback_and_mismatch_summaries(self):
        summary = build(*mixed_reports())["summary"]
        self.assertEqual((summary["mime_classified"], summary["extension_fallback"], summary["mime_extension_mismatch"]), (9, 1, 1))

    def test_69_upstream_warnings_deduplicated_not_dropped(self):
        result = one_file(item(warnings=["mock_warning", "mock_warning"]), warnings=["mock_folder_warning"])
        self.assertEqual(result["warnings"], ["mock_folder_warning", "mock_warning"])

    def test_70_blocking_assets_summary_and_partial(self):
        report = build(manifest(folder("root", item(blocking_issues=["mock_blocker"]))))
        self.assertEqual(report["summary"]["blocking_assets"], 1)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["results"][0]["blocking_issues"], ["mock_blocker"])

    def test_71_no_file_or_content_io_in_builder(self):
        reports = mixed_reports()
        with patch("builtins.open", side_effect=AssertionError("No content reads")) as opened, patch("io.open", side_effect=AssertionError("No content reads")) as io_open:
            build(*reports)
        opened.assert_not_called()
        io_open.assert_not_called()
        source = inspect.getsource(dry_run)
        self.assertNotIn("PIL", source)
        self.assertNotIn("get_media", source)

    def test_72_input_files_not_modified(self):
        paths = self.files()
        original = [path.read_bytes() for path in paths]
        dry_run.run_image_asset_type_dry_run(*paths, project_root=self.project)
        self.assertEqual(original, [path.read_bytes() for path in paths])

    def test_73_bad_json_and_missing_file_safe_error(self):
        paths = self.files()
        for content in ("{", "[]", "null"):
            paths[0].write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ImageAssetTypeDryRunInputError):
                dry_run.run_image_asset_type_dry_run(*paths, project_root=self.project)
        with self.assertRaises(ImageAssetTypeDryRunInputError):
            dry_run.run_image_asset_type_dry_run(self.project / "missing.json", paths[1], paths[2], project_root=self.project)
        self.assertFalse(self.output.exists())

    def test_74_non_json_including_env_rejected_before_load(self):
        with patch.object(dry_run, "load_local_json_report") as load:
            for name in (".env", "mock.txt", "mock.jpg"):
                with self.subTest(name=name), self.assertRaises(ImageAssetTypeDryRunInputError):
                    dry_run.run_image_asset_type_dry_run(self.project / name, self.project / "n.json", self.project / "d.json", project_root=self.project)
        load.assert_not_called()

    def test_75_remote_paths_rejected_before_filesystem(self):
        for path in ("https://example.invalid/mock.json", "file://mock/report.json", r"\\mock\share\report.json"):
            with self.subTest(path=path), patch.object(Path, "lstat", side_effect=AssertionError("No remote filesystem access")) as access:
                with self.assertRaises(ImageAssetTypeDryRunInputError):
                    dry_run.run_image_asset_type_dry_run(Path(path), Path("n.json"), Path("d.json"), project_root=self.project)
                access.assert_not_called()

    def test_76_symlink_and_parent_junction_rejected(self):
        paths = self.files()
        original = Path.lstat
        for linked, info in ((paths[0], SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)),
                             (self.project, SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))):
            def lstat(path, **kwargs):
                return info if path == linked else original(path, **kwargs)
            with patch.object(Path, "lstat", lstat), patch.object(dry_run, "load_local_json_report") as load:
                with self.assertRaises(ImageAssetTypeDryRunInputError):
                    dry_run.run_image_asset_type_dry_run(*paths, project_root=self.project)
                load.assert_not_called()

    def test_77_output_cannot_be_input(self):
        paths = self.files()
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaisesRegex(ImageAssetTypeDryRunInputError, "manifest_output_collision"):
            dry_run.run_image_asset_type_dry_run(self.output, paths[1], paths[2], project_root=self.project)
        load.assert_not_called()

    def test_78_credentials_and_links_rejected_in_unused_fields(self):
        for key in ("private_key", "client_email", "privateKey", "credentials", "resourceKey", "Authorization", "Cookie", "download_url"):
            with self.subTest(key=key), self.assertRaises(ImageAssetTypeDryRunInputError):
                one_file(item(**{key: "MOCK_ONLY"}))

    def test_79_secret_strings_rejected_from_hierarchy(self):
        for value in ("ck_" + "m" * 30, "token=MOCK_SECRET", "mock-service@example.invalid"):
            with self.subTest(value=value), self.assertRaises(ImageAssetTypeDryRunInputError) as caught:
                one_file(kind="depth2", depth1_safe_folder_name=value)
            self.assertNotIn(value, str(caught.exception))

    def test_80_cli_failure_log_no_sensitive_values(self):
        reports = (manifest(folder("root", item(name="https://example.invalid/MOCK_SECRET.jpg"))), manifest(), manifest())
        with self.assertLogs("sync_worker", level="ERROR") as logs:
            self.assertEqual(self.run_cli(self.files(reports)), 2)
        output = " ".join(logs.output)
        self.assertIn("image_asset_type_dry_run_aborted", output)
        self.assertNotIn("MOCK_SECRET", output)
        self.assertNotIn("https://", output)

    def test_81_core_decisions_not_recomputed(self):
        sentinel = replace(policy.classify_image_asset_type("image/jpeg", "mock.jpg"),
                           asset_class=policy.AssetClass.OTHER_MEDIA, storefront_eligible=False)
        with patch.object(policy, "classify_image_asset_type", return_value=sentinel):
            result = one_file()
        self.assertEqual(result["asset_class"], "other_media")
        self.assertFalse(result["storefront_eligible"])

    def test_82_policy_version_retained(self):
        report = build(*mixed_reports())
        self.assertEqual(report["policy_version"], "xxxxdoll-image-asset-type-v1")
        self.assertTrue(all(result["policy_version"] == policy.POLICY_VERSION for result in report["results"]))

    def test_83_empty_manifests_zero_summary(self):
        report = build(manifest(), manifest(), manifest())
        self.assertEqual(report["results"], [])
        self.assertEqual(report["status"], "ok")
        self.assertTrue(all(value == 0 for value in report["summary"].values()))

    def test_84_only_skipped_items_no_assets(self):
        report = build(manifest(folder("root", item(kind="shortcut"), item(kind="nested_folder"))))
        self.assertEqual(report["summary"]["classified_assets"], 0)
        self.assertEqual(report["summary"]["total_manifest_items_seen"], 2)

    def test_85_python_module_entry_with_mock_files(self):
        paths = self.files()
        with patch.object(cli, "PROJECT_ROOT", self.project), patch.object(sys, "argv", ["sync_worker", *self.arguments(paths)]):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("sync_worker", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertTrue(self.output.exists())

    def test_86_no_duplicate_removal(self):
        file = item(warnings=["duplicate_content_candidate"])
        report = build(manifest(folder("root", file, copy.deepcopy(file))))
        self.assertEqual(report["summary"]["classified_assets"], 2)
        self.assertEqual(report["summary"]["assets_with_warnings"], 2)

    def test_87_tied_sort_keys_deterministic(self):
        first, second = item(size_bytes=10), item(size_bytes=20)
        before = build(manifest(folder("root", first, second)))
        after = build(manifest(folder("root", second, first)))
        self.assertEqual(before, after)

    def test_88_metadata_only_projection(self):
        result = one_file()
        self.assertEqual(result["status"], "metadata_web_image")
        expected = {"sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name", "parent_safe_folder_name",
                    "safe_name", "mime_type", "normalized_mime_type", "safe_extension", "asset_class", "classification_source",
                    "storefront_eligible", "policy_version", "size_bytes", "image_width", "image_height", "warnings", "blocking_issues", "status"}
        self.assertEqual(set(result), expected)

    def test_89_malformed_shapes_fail_closed(self):
        for value in (None, {}, "mock", ["mock"]):
            with self.subTest(value=value), self.assertRaises(ImageAssetTypeDryRunInputError):
                build(manifest(folder("root", items=value)))

    def test_90_partial_cli_report_return_code(self):
        reports = (manifest(folder("root", item(blocking_issues=["mock_blocker"]))), manifest(), manifest())
        self.assertEqual(self.run_cli(self.files(reports)), 1)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["status"], "partial")


if __name__ == "__main__":
    unittest.main()
