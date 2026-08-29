from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import (
    cli,
    folder_role_policy as folder_core,
    image_asset_type_policy as asset_core,
    unified_image_eligibility_dry_run as dry_run,
    unified_image_eligibility_policy as unified_core,
    webp_output_policy as webp_core,
)
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.unified_image_eligibility_dry_run import (
    UnifiedImageEligibilityDryRunInputError,
)


def folder_result(
    folder="Photos Mock",
    *,
    sku="MOCK-001",
    start=10,
    end=20,
    kind="nested",
    parent=None,
    deeper=False,
    **overrides,
):
    depth = {"nested": 1, "depth2": 2}[kind]
    if kind == "depth2" and parent is None:
        parent = "Mock Parent"
    result = folder_core.classify_folder_role(
        folder,
        parent_safe_folder_name=parent,
        depth=depth,
        sku=sku,
        product_source=ProductSourceRange(start, end),
        has_depth_limit_children=deeper,
    ).to_dict()
    result.update(safe_folder_name=folder, source_manifest_kind=kind)
    result.update(overrides)
    return result


def folder_report(*results, **overrides):
    summary = {
        "total_folders": len(results), "depth1_folders": sum(x["depth"] == 1 for x in results),
        "depth2_folders": sum(x["depth"] == 2 for x in results),
        **{role.value: sum(x["role"] == role.value for x in results) for role in folder_core.FolderRole},
        "gallery_eligible_folders": sum(x["gallery_eligible"] for x in results),
        "requires_deeper_inventory_folders": sum(x["requires_deeper_inventory"] for x in results),
        "folders_with_warnings": sum(bool(x["warnings"]) for x in results),
        "blocking_folders": sum(bool(x["blocking_issues"]) for x in results),
        "network_requests_performed": 0, "download_requests_performed": 0,
        "write_requests_performed": 0,
    }
    return {
        "status": "ok", "policy_version": folder_core.POLICY_VERSION,
        "summary": summary, "results": list(results),
        "network_requests_performed": 0, "download_requests_performed": 0,
        "write_requests_performed": 0, **overrides,
    }


def webp_result(
    *,
    sku="MOCK-001",
    start=10,
    end=20,
    kind="nested",
    folder="Photos Mock",
    parent=None,
    name="mock.jpg",
    asset_class=asset_core.AssetClass.WEB_IMAGE,
    mime="image/jpeg",
    eligible=True,
    action=webp_core.WebPAction.CONVERT_TO_WEBP,
    reason=None,
    warnings=(),
    blockers=(),
    **overrides,
):
    depth = {"root": 0, "nested": 1, "depth2": 2}[kind]
    if kind == "root":
        folder, parent, source = None, None, None
    else:
        if kind == "depth2" and parent is None:
            parent = "Mock Parent"
        source = {"start_row": start, "end_row": end}
    result = webp_core.WebPOutputPolicyResult(
        source_asset_class=asset_class,
        source_mime_type=mime,
        source_asset_eligible=eligible,
        requires_webp_pipeline=eligible,
        webp_action=action,
        reason=reason,
        warnings=tuple(warnings),
        blocking_issues=tuple(blockers),
    ).to_dict()
    result.update({
        "sku": sku, "product_source": source, "source_manifest_kind": kind,
        "depth": depth, "safe_folder_name": folder,
        "parent_safe_folder_name": parent, "safe_name": name,
    })
    result.update(overrides)
    return result


def webp_report(*results, **overrides):
    summary = {
        "total_assets": len(results),
        "source_asset_eligible": sum(x["source_asset_eligible"] for x in results),
        "source_asset_ineligible": sum(not x["source_asset_eligible"] for x in results),
        "requires_webp_pipeline": sum(x["requires_webp_pipeline"] for x in results),
        **{action.value: sum(x["webp_action"] == action.value for x in results) for action in webp_core.WebPAction},
        "wordpress_upload_ready": sum(x["wordpress_upload_ready"] for x in results),
        "jpeg_sources": sum(x["source_mime_type"] == "image/jpeg" for x in results),
        "png_sources": sum(x["source_mime_type"] == "image/png" for x in results),
        "webp_sources": sum(x["source_mime_type"] == "image/webp" for x in results),
        "design_sources": sum(x["source_asset_class"] == "design_source" for x in results),
        "video_sources": sum(x["source_asset_class"] == "video" for x in results),
        "unsupported_sources": sum(x["source_asset_class"] == "unsupported" for x in results),
        "unknown_sources": sum(x["source_asset_class"] == "unknown" for x in results),
        "other_media_sources": sum(x["source_asset_class"] == "other_media" for x in results),
        "assets_with_warnings": sum(bool(x["warnings"]) for x in results),
        "blocking_assets": sum(bool(x["blocking_issues"]) for x in results),
        **dict.fromkeys(dry_run.REQUEST_COUNTERS, 0),
    }
    return {
        "status": "ok", "policy_version": webp_core.POLICY_VERSION,
        "source_policy_version": asset_core.POLICY_VERSION,
        "summary": summary, "results": list(results),
        **dict.fromkeys(dry_run.REQUEST_COUNTERS, 0), **overrides,
    }


def build(folders=None, assets=None):
    return dry_run.build_unified_image_eligibility_dry_run_report(
        folder_report(folder_result()) if folders is None else folders,
        webp_report(webp_result()) if assets is None else assets,
    )


def one(folder=None, asset=None):
    report = build(
        folder_report(folder_result() if folder is None else folder),
        webp_report(webp_result() if asset is None else asset),
    )
    return report["results"][0]


def inventory_fixture():
    folders = folder_report(
        folder_result("Photos Mock", sku="MOCK-STOREFRONT", start=10, end=20),
        folder_result("Factory Photos Mock", sku="MOCK-FACTORY", start=30, end=40),
        folder_result("Factory Photos Deep", sku="MOCK-DEEP", start=50, end=60, kind="depth2", deeper=True),
        folder_result("Banner Mock", sku="MOCK-BANNER", start=70, end=80),
        folder_result("Videos Mock", sku="MOCK-VIDEO", start=90, end=100),
    )
    assets = []
    assets.extend(webp_result(sku="MOCK-STOREFRONT", start=10, end=20, name=f"photo-{i:03}.jpg") for i in range(102))
    assets.extend(webp_result(sku="MOCK-FACTORY", start=30, end=40, folder="Factory Photos Mock", name=f"factory-{i:03}.jpg") for i in range(75))
    assets.extend(webp_result(sku="MOCK-DEEP", start=50, end=60, kind="depth2", folder="Factory Photos Deep", name=f"deep-{i:03}.jpg") for i in range(27))
    assets.extend(webp_result(sku="MOCK-BANNER", start=70, end=80, folder="Banner Mock", name=f"banner-{i}.jpg") for i in range(2))
    assets.extend(webp_result(
        sku="MOCK-VIDEO", start=90, end=100, folder="Videos Mock", name=f"video-{i:02}.mp4",
        asset_class=asset_core.AssetClass.VIDEO, mime="video/mp4", eligible=False,
        action=webp_core.WebPAction.NOT_ALLOWED, reason="video_not_storefront_asset",
    ) for i in range(38))
    assets.extend([
        webp_result(kind="root", name="root-a.psd", asset_class=asset_core.AssetClass.DESIGN_SOURCE,
                    mime="image/vnd.adobe.photoshop", eligible=False, action=webp_core.WebPAction.NOT_ALLOWED,
                    reason="design_source_not_storefront_asset"),
        webp_result(kind="root", name="root-b.psd", asset_class=asset_core.AssetClass.DESIGN_SOURCE,
                    mime="image/vnd.adobe.photoshop", eligible=False, action=webp_core.WebPAction.NOT_ALLOWED,
                    reason="design_source_not_storefront_asset"),
        webp_result(kind="root", name="root-video.mov", asset_class=asset_core.AssetClass.VIDEO,
                    mime="video/quicktime", eligible=False, action=webp_core.WebPAction.NOT_ALLOWED,
                    reason="video_not_storefront_asset"),
        webp_result(kind="root", name="root.bin", asset_class=asset_core.AssetClass.UNSUPPORTED,
                    mime="application/octet-stream", eligible=False, action=webp_core.WebPAction.NOT_ALLOWED,
                    reason="unsupported_asset_not_allowed"),
    ])
    return folders, webp_report(*assets)


class UnifiedImageEligibilityDryRunTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(self.enterContext(TemporaryDirectory()))
        self.output = self.project / "reports" / dry_run.REPORT_FILENAME
        self.denied = []
        for target in (
            "socket.socket.connect", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "sync_worker.cli.OfficialGoogleClientFactory",
            "sync_worker.google_api.OfficialGoogleClientFactory",
            "sync_worker.http_client.ReadOnlyHttpClient.request", "subprocess.run",
            "subprocess.Popen", "os.system",
        ):
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Offline fixtures only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No config reads"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def files(self, folders=None, assets=None):
        paths = self.project / "roles.json", self.project / "webp.json"
        paths[0].write_text(json.dumps(folder_report(folder_result()) if folders is None else folders), encoding="utf-8")
        paths[1].write_text(json.dumps(webp_report(webp_result()) if assets is None else assets), encoding="utf-8")
        return paths

    def run_cli(self, paths):
        with patch.object(cli, "PROJECT_ROOT", self.project):
            return cli.main([
                "evaluate-image-eligibility", "--folder-role-report", str(paths[0]),
                "--webp-report", str(paths[1]),
            ])

    def test_001_cli_registered(self):
        args = cli.build_parser().parse_args([
            "evaluate-image-eligibility", "--folder-role-report", "roles.json",
            "--webp-report", "webp.json",
        ])
        self.assertEqual(args.command, "evaluate-image-eligibility")
        self.assertEqual(args.folder_role_report_path, Path("roles.json"))
        self.assertEqual(args.webp_report_path, Path("webp.json"))

    def test_002_folder_argument_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["evaluate-image-eligibility", "--webp-report", "webp.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_003_webp_argument_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["evaluate-image-eligibility", "--folder-role-report", "roles.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_004_cli_success_writes_fixed_report(self):
        code = self.run_cli(self.files())
        self.assertEqual(code, 0)
        self.assertTrue(self.output.is_file())

    def test_005_cli_blocked_returns_one(self):
        code = self.run_cli(self.files(folder_report(), webp_report(webp_result())))
        self.assertEqual(code, 1)

    def test_006_cli_input_error_returns_two(self):
        source = folder_report(folder_result())
        source["status"] = "partial"
        self.assertEqual(self.run_cli(self.files(source, webp_report(webp_result()))), 2)

    def test_007_calls_unified_core_once_per_asset(self):
        assets = webp_report(webp_result(name="a.jpg"), webp_result(name="b.jpg"))
        with patch.object(unified_core, "evaluate_unified_image_eligibility", wraps=unified_core.evaluate_unified_image_eligibility) as evaluate:
            build(assets=assets)
        self.assertEqual(evaluate.call_count, 2)

    def test_008_restores_folder_domain_object(self):
        with patch.object(unified_core, "evaluate_unified_image_eligibility", wraps=unified_core.evaluate_unified_image_eligibility) as evaluate:
            build()
        self.assertIs(type(evaluate.call_args.args[0]), folder_core.FolderRoleClassification)

    def test_009_restores_webp_domain_object(self):
        with patch.object(unified_core, "evaluate_unified_image_eligibility", wraps=unified_core.evaluate_unified_image_eligibility) as evaluate:
            build()
        self.assertIs(type(evaluate.call_args.args[1]), webp_core.WebPOutputPolicyResult)

    def test_010_does_not_call_folder_classifier(self):
        folders = folder_report(folder_result())
        assets = webp_report(webp_result())
        with patch.object(folder_core, "classify_folder_role", side_effect=AssertionError("No reclassification")) as classify:
            build(folders, assets)
        classify.assert_not_called()

    def test_011_does_not_call_webp_planner(self):
        with patch.object(webp_core, "evaluate_webp_output_policy", side_effect=AssertionError("No replanning")) as evaluate:
            build()
        evaluate.assert_not_called()

    def test_012_exact_join(self):
        result = one()
        self.assertEqual(result["join_status"], "joined")
        self.assertEqual(result["folder_role"], "storefront_photos")

    def test_013_case_sensitive_join(self):
        result = one(asset=webp_result(folder="photos mock"))
        self.assertEqual((result["join_status"], result["eligibility_reason"]), ("missing", "missing_folder_role_join"))

    def test_014_no_substring_join(self):
        result = one(asset=webp_result(folder="Photos"))
        self.assertEqual(result["join_status"], "missing")

    def test_015_no_sku_only_join(self):
        result = one(asset=webp_result(folder="Different Folder"))
        self.assertEqual(result["join_status"], "missing")

    def test_016_no_depth_only_join(self):
        result = one(asset=webp_result(sku="DIFFERENT"))
        self.assertEqual(result["join_status"], "missing")

    def test_017_source_start_row_exact(self):
        self.assertEqual(one(asset=webp_result(start=11))["join_status"], "missing")

    def test_018_source_end_row_exact(self):
        self.assertEqual(one(asset=webp_result(end=21))["join_status"], "missing")

    def test_019_manifest_kind_exact(self):
        asset = webp_result(kind="depth2", parent="Mock Parent")
        self.assertEqual(one(asset=asset)["join_status"], "missing")

    def test_020_parent_exact(self):
        role = folder_result(kind="depth2", parent="Parent A")
        asset = webp_result(kind="depth2", parent="Parent B")
        self.assertEqual(one(role, asset)["join_status"], "missing")

    def test_021_duplicate_join_is_ambiguous(self):
        roles = folder_report(folder_result(), folder_result())
        result = build(roles, webp_report(webp_result()))["results"][0]
        self.assertEqual(result["join_status"], "ambiguous")

    def test_022_ambiguous_never_selects_first(self):
        roles = folder_report(folder_result(), folder_result("Banner Mock"))
        roles["results"][1].update(safe_folder_name="Photos Mock", normalized_folder_name="photos mock")
        result = build(roles, webp_report(webp_result()))["results"][0]
        self.assertIsNone(result["folder_role"])

    def test_023_missing_join_blocks(self):
        result = build(folder_report(), webp_report(webp_result()))["results"][0]
        self.assertIn("missing_folder_role_join", result["blocking_issues"])

    def test_024_ambiguous_join_blocks(self):
        result = build(folder_report(folder_result(), folder_result()), webp_report(webp_result()))["results"][0]
        self.assertIn("ambiguous_folder_role_join", result["blocking_issues"])

    def test_025_root_is_expected_missing(self):
        result = build(folder_report(), webp_report(webp_result(kind="root")))["results"][0]
        self.assertEqual((result["join_status"], result["eligibility_reason"]), ("missing", "missing_folder_role"))

    def test_026_root_has_no_join_blocker(self):
        result = build(folder_report(), webp_report(webp_result(kind="root")))["results"][0]
        self.assertNotIn("missing_folder_role_join", result["blocking_issues"])

    def test_027_storefront_jpeg_eligible(self):
        self.assertTrue(one()["unified_image_eligible"])

    def test_028_factory_jpeg_eligible(self):
        role = folder_result("Factory Photos Mock")
        asset = webp_result(folder="Factory Photos Mock")
        self.assertTrue(one(role, asset)["unified_image_eligible"])

    def test_029_banner_jpeg_ineligible(self):
        role = folder_result("Banner Mock")
        asset = webp_result(folder="Banner Mock")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_030_video_role_ineligible(self):
        role = folder_result("Videos Mock")
        asset = webp_result(folder="Videos Mock")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_031_eye_role_ineligible(self):
        role = folder_result("Eye Options Mock")
        asset = webp_result(folder="Eye Options Mock")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_032_promo_role_ineligible(self):
        role = folder_result("Promo Assets Mock")
        asset = webp_result(folder="Promo Assets Mock")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_033_skin_role_ineligible(self):
        role = folder_result("Other Skin Tone Mock")
        asset = webp_result(folder="Other Skin Tone Mock")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_034_unknown_role_ineligible(self):
        role = folder_result("Mock Collection")
        asset = webp_result(folder="Mock Collection")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_035_source_ineligible_blocks_gallery(self):
        asset = webp_result(asset_class=asset_core.AssetClass.VIDEO, mime="video/mp4", eligible=False,
                            action=webp_core.WebPAction.NOT_ALLOWED, reason="video_not_storefront_asset")
        self.assertFalse(one(asset=asset)["unified_image_eligible"])

    def test_036_png_eligible(self):
        asset = webp_result(mime="image/png")
        self.assertTrue(one(asset=asset)["unified_image_eligible"])

    def test_037_webp_validate_eligible(self):
        asset = webp_result(mime="image/webp", action=webp_core.WebPAction.VALIDATE_EXISTING_WEBP)
        self.assertTrue(one(asset=asset)["unified_image_eligible"])

    def test_038_fixed_target(self):
        result = one()
        self.assertEqual((result["target_mime_type"], result["target_extension"]), ("image/webp", ".webp"))

    def test_039_invalid_target_fail_closed(self):
        result = one(asset=webp_result(target_mime_type="image/png"))
        self.assertFalse(result["unified_image_eligible"])
        self.assertEqual(result["eligibility_reason"], "invalid_webp_target")

    def test_040_upload_ready_contract_fail_closed(self):
        result = one(asset=webp_result(wordpress_upload_ready=True))
        self.assertFalse(result["unified_image_eligible"])
        self.assertIn("wordpress_upload_ready_contract_violation", result["blocking_issues"])

    def test_041_invalid_action_fail_closed(self):
        result = one(asset=webp_result(webp_action="upload_now"))
        self.assertFalse(result["unified_image_eligible"])
        self.assertEqual(result["eligibility_reason"], "invalid_webp_action")

    def test_042_pipeline_mismatch_fail_closed(self):
        result = one(asset=webp_result(requires_webp_pipeline=False))
        self.assertFalse(result["unified_image_eligible"])
        self.assertIn("invalid_webp_pipeline_contract", result["blocking_issues"])

    def test_043_deeper_factory_remains_eligible(self):
        role = folder_result("Factory Photos Deep", kind="depth2", deeper=True)
        asset = webp_result(kind="depth2", folder="Factory Photos Deep")
        self.assertTrue(one(role, asset)["unified_image_eligible"])

    def test_044_deeper_adds_warning(self):
        role = folder_result("Factory Photos Deep", kind="depth2", deeper=True)
        asset = webp_result(kind="depth2", folder="Factory Photos Deep")
        self.assertIn("folder_inventory_incomplete", one(role, asset)["warnings"])

    def test_045_upstream_warnings_merged(self):
        role = folder_result(warnings=["folder_audit_warning"])
        asset = webp_result(warnings=("webp_audit_warning",))
        self.assertEqual(set(one(role, asset)["warnings"]), {"folder_audit_warning", "webp_audit_warning"})

    def test_046_upstream_blockers_merged(self):
        role = folder_result(blocking_issues=["folder_audit_blocked"])
        asset = webp_result(blockers=("webp_audit_blocked",))
        result = one(role, asset)
        self.assertTrue({"folder_audit_blocked", "webp_audit_blocked"}.issubset(result["blocking_issues"]))

    def test_047_no_duplicate_issue_codes(self):
        role = folder_result(warnings=["shared_warning"])
        asset = webp_result(warnings=("shared_warning",))
        self.assertEqual(one(role, asset)["warnings"].count("shared_warning"), 1)

    def test_048_policy_versions_projected(self):
        result = one()
        self.assertEqual(result["unified_policy_version"], unified_core.POLICY_VERSION)
        self.assertEqual(result["folder_role_policy_version"], folder_core.POLICY_VERSION)
        self.assertEqual(result["webp_policy_version"], webp_core.POLICY_VERSION)

    def test_049_top_policy_versions(self):
        report = build()
        self.assertEqual(report["policy_version"], unified_core.POLICY_VERSION)
        self.assertEqual(report["folder_role_policy_version"], folder_core.POLICY_VERSION)
        self.assertEqual(report["webp_policy_version"], webp_core.POLICY_VERSION)

    def test_050_preserves_webp_order_and_duplicates(self):
        assets = webp_report(webp_result(name="b.jpg"), webp_result(name="a.jpg"), webp_result(name="b.jpg"))
        names = [x["safe_name"] for x in build(assets=assets)["results"]]
        self.assertEqual(names, ["b.jpg", "a.jpg", "b.jpg"])

    def test_051_summary_total(self):
        folders, assets = inventory_fixture()
        self.assertEqual(build(folders, assets)["summary"]["total_assets"], 248)

    def test_052_summary_depth_counts(self):
        summary = build(*inventory_fixture())["summary"]
        self.assertEqual((summary["root_assets"], summary["depth1_assets"], summary["depth2_assets"]), (4, 217, 27))

    def test_053_summary_source_reality_counts(self):
        report = build(*inventory_fixture())
        self.assertEqual(sum(x["source_mime_type"] == "image/jpeg" for x in report["results"]), 206)
        self.assertEqual(sum(x["source_asset_class"] == "design_source" for x in report["results"]), 2)
        self.assertEqual(sum(x["source_asset_class"] == "video" for x in report["results"]), 39)
        self.assertEqual(sum(x["source_asset_class"] == "unsupported" for x in report["results"]), 1)

    def test_054_summary_unified_counts(self):
        summary = build(*inventory_fixture())["summary"]
        self.assertEqual((summary["unified_image_eligible"], summary["unified_image_ineligible"]), (204, 44))

    def test_055_summary_banner_exclusion(self):
        summary = build(*inventory_fixture())["summary"]
        self.assertEqual(summary["ineligible_banner"], 2)

    def test_056_summary_role_eligible_split(self):
        summary = build(*inventory_fixture())["summary"]
        self.assertEqual((summary["eligible_storefront_photos"], summary["eligible_factory_photos"]), (102, 102))

    def test_057_summary_source_ineligible(self):
        self.assertEqual(build(*inventory_fixture())["summary"]["ineligible_source_asset"], 42)

    def test_058_summary_root_missing(self):
        summary = build(*inventory_fixture())["summary"]
        self.assertEqual((summary["folder_role_missing"], summary["ineligible_missing_role"]), (4, 4))

    def test_059_summary_joined(self):
        self.assertEqual(build(*inventory_fixture())["summary"]["folder_role_joined"], 244)

    def test_060_summary_deeper(self):
        report = build(*inventory_fixture())
        self.assertEqual(report["summary"]["requires_deeper_inventory_assets"], 27)
        self.assertEqual(sum("folder_inventory_incomplete" in x["warnings"] for x in report["results"]), 27)

    def test_061_synthetic_report_has_no_blockers(self):
        report = build(*inventory_fixture())
        self.assertEqual(report["summary"]["blocking_assets"], 0)
        self.assertEqual(report["status"], "ok")

    def test_062_dynamic_counts_not_literals_in_source(self):
        tree = ast.parse(inspect.getsource(dry_run))
        ints = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and type(node.value) is int}
        self.assertTrue({204, 206, 248, 44}.isdisjoint(ints))

    def test_063_summary_all_zero_request_counters(self):
        report = build(*inventory_fixture())
        for counter in dry_run.REQUEST_COUNTERS:
            self.assertEqual(report["summary"][counter], 0)

    def test_064_top_all_zero_request_counters(self):
        report = build(*inventory_fixture())
        for counter in dry_run.REQUEST_COUNTERS:
            self.assertEqual(report[counter], 0)

    def test_065_no_upload_ready_field_in_results(self):
        self.assertNotIn("wordpress_upload_ready", one())

    def test_066_no_ids_or_urls_in_report(self):
        text = json.dumps(build(*inventory_fixture()), ensure_ascii=False)
        for token in ("https://", "drive.google.com", "provider_file_id", "fingerprint", "download_url"):
            self.assertNotIn(token, text)

    def test_067_no_input_paths_in_report(self):
        report = build()
        self.assertNotIn("input", json.dumps(report))

    def test_068_no_selection_fields(self):
        text = json.dumps(build())
        for token in ("primary_image", "gallery_order", "selected_image", "hero_image"):
            self.assertNotIn(token, text)

    def test_069_report_result_field_contract(self):
        self.assertEqual(set(one()), {
            "sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name",
            "parent_safe_folder_name", "safe_name", "source_asset_class", "source_mime_type",
            "join_status", "folder_role", "folder_role_policy_version", "folder_gallery_eligible",
            "requires_deeper_inventory", "source_asset_eligible", "requires_webp_pipeline",
            "webp_action", "target_mime_type", "target_extension", "unified_image_eligible",
            "eligibility_reason", "unified_policy_version", "webp_policy_version", "warnings",
            "blocking_issues",
        })

    def test_070_summary_field_contract(self):
        expected = {
            "total_assets", "root_assets", "depth1_assets", "depth2_assets",
            "folder_role_joined", "folder_role_missing", "folder_role_ambiguous",
            "unified_image_eligible", "unified_image_ineligible", "eligible_storefront_photos",
            "eligible_factory_photos", "ineligible_banner", "ineligible_video_folder",
            "ineligible_eye_options", "ineligible_promo_assets", "ineligible_other_skin_tone",
            "ineligible_unknown_role", "ineligible_missing_role", "ineligible_source_asset",
            "ineligible_invalid_webp_contract", "requires_deeper_inventory_assets",
            "assets_with_warnings", "blocking_assets", *dry_run.REQUEST_COUNTERS,
        }
        self.assertEqual(set(build()["summary"]), expected)

    def test_071_invalid_contract_summary(self):
        report = build(assets=webp_report(webp_result(target_extension=".png")))
        self.assertEqual(report["summary"]["ineligible_invalid_webp_contract"], 1)

    def test_072_missing_summary(self):
        report = build(folder_report(), webp_report(webp_result()))
        self.assertEqual(report["summary"]["folder_role_missing"], 1)

    def test_073_ambiguous_summary(self):
        report = build(folder_report(folder_result(), folder_result()), webp_report(webp_result()))
        self.assertEqual(report["summary"]["folder_role_ambiguous"], 1)

    def test_074_only_two_local_json_reads(self):
        paths = self.files()
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as load:
            dry_run.run_unified_image_eligibility_dry_run(*paths, project_root=self.project)
        self.assertEqual(load.call_count, 2)

    def test_075_writes_only_fixed_report(self):
        dry_run.run_unified_image_eligibility_dry_run(*self.files(), project_root=self.project)
        self.assertEqual([p.relative_to(self.project).as_posix() for p in self.project.rglob("*.json") if p.parent.name == "reports"],
                         ["reports/unified-image-eligibility-dry-run.json"])

    def test_076_output_collision_rejected(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("{}", encoding="utf-8")
        with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(self.output, self.project / "webp.json", project_root=self.project)

    def test_077_same_input_rejected(self):
        path = self.files()[0]
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "input_report_collision"):
            dry_run.run_unified_image_eligibility_dry_run(path, path, project_root=self.project)

    def test_078_url_path_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(Path("https://example.invalid/role.json"), Path("webp.json"), project_root=self.project)
        load.assert_not_called()

    def test_079_env_path_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(Path(".env.json"), Path("webp.json"), project_root=self.project)
        load.assert_not_called()

    def test_080_credentials_path_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(Path("credentials.json"), Path("webp.json"), project_root=self.project)
        load.assert_not_called()

    def test_081_drive_manifest_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(Path("google-drive-folder-manifest-dry-run.json"), Path("webp.json"), project_root=self.project)
        load.assert_not_called()

    def test_082_asset_type_report_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(Path("roles.json"), Path("image-asset-type-dry-run.json"), project_root=self.project)
        load.assert_not_called()

    def test_083_folder_status_must_be_ok(self):
        source = folder_report(folder_result())
        source["status"] = "partial"
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "folder_role_report_status_not_ok"):
            build(source)

    def test_084_webp_status_must_be_ok(self):
        source = webp_report(webp_result())
        source["status"] = "blocked"
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "webp_report_status_not_ok"):
            build(assets=source)

    def test_085_folder_version_exact(self):
        source = folder_report(folder_result())
        source["policy_version"] = "folder-v2"
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "folder_role_policy_version_mismatch"):
            build(source)

    def test_086_webp_version_exact(self):
        source = webp_report(webp_result())
        source["policy_version"] = "webp-v2"
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "webp_policy_version_mismatch"):
            build(assets=source)

    def test_087_source_version_exact(self):
        source = webp_report(webp_result())
        source["source_policy_version"] = "asset-v2"
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "asset_type_policy_version_mismatch"):
            build(assets=source)

    def test_088_folder_counter_nonzero_rejected(self):
        source = folder_report(folder_result())
        source["network_requests_performed"] = 1
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "input_report_not_offline"):
            build(source)

    def test_089_webp_counter_nonzero_rejected(self):
        source = webp_report(webp_result())
        source["conversion_requests_performed"] = 1
        with self.assertRaisesRegex(UnifiedImageEligibilityDryRunInputError, "input_report_not_offline"):
            build(assets=source)

    def test_090_validates_both_inputs_before_core(self):
        source = webp_report(webp_result(), webp_result(name="bad.jpg"))
        source["results"][1]["safe_name"] = "https://example.invalid/bad.jpg"
        with patch.object(unified_core, "evaluate_unified_image_eligibility") as evaluate, self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            build(assets=source)
        evaluate.assert_not_called()

    def test_091_deterministic_output(self):
        folders, assets = inventory_fixture()
        self.assertEqual(build(copy.deepcopy(folders), copy.deepcopy(assets)), build(folders, assets))

    def test_092_no_file_bytes_or_media_tools(self):
        source = inspect.getsource(dry_run)
        for forbidden in ("PIL", "ImageMagick", "cwebp", "ffmpeg", "read_bytes", "write_bytes"):
            self.assertNotIn(forbidden, source)

    def test_093_no_http_or_api_imports(self):
        tree = ast.parse(inspect.getsource(dry_run))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertTrue(imported.isdisjoint({"requests", "httplib2", "googleapiclient", "urllib"}))

    def test_094_no_fuzzy_join_library(self):
        source = inspect.getsource(dry_run)
        self.assertNotIn("difflib", source)
        self.assertNotIn("fuzzy", source.casefold())

    def test_095_no_sort_or_dedupe_assets(self):
        source = inspect.getsource(dry_run.build_unified_image_eligibility_dry_run_report)
        self.assertNotIn("sort(", source)
        self.assertNotIn("sorted(", source)

    def test_096_report_secret_redacted_or_rejected(self):
        source = webp_report(webp_result())
        source["results"][0]["safe_name"] = "WP_APP_PASSWORD=mock-secret"
        with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            build(assets=source)

    def test_097_no_local_report_path_field(self):
        self.assertFalse(any("path" in key for key in build()))

    def test_098_cli_logs_no_input_paths(self):
        paths = self.files()
        with patch.object(cli, "PROJECT_ROOT", self.project), self.assertLogs("sync_worker", level="INFO") as logs:
            cli.main(["evaluate-image-eligibility", "--folder-role-report", str(paths[0]), "--webp-report", str(paths[1])])
        joined = "\n".join(logs.output)
        self.assertNotIn(str(paths[0]), joined)
        self.assertNotIn(str(paths[1]), joined)

    def test_099_read_error_does_not_overwrite_existing_report(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("sentinel", encoding="utf-8")
        with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            dry_run.run_unified_image_eligibility_dry_run(self.project / "missing-role.json", self.project / "missing-webp.json", project_root=self.project)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel")

    def test_100_report_is_json_serializable(self):
        self.assertEqual(json.loads(json.dumps(build())), build())

    def test_101_depth2_exact_join(self):
        role = folder_result("Photos Detail", kind="depth2", parent="Photos Parent")
        asset = webp_result(kind="depth2", folder="Photos Detail", parent="Photos Parent")
        self.assertEqual(one(role, asset)["join_status"], "joined")

    def test_102_root_role_not_guessed_from_name_or_mime(self):
        asset = webp_result(kind="root", name="Photos Mock.jpg", mime="image/jpeg")
        result = build(folder_report(folder_result()), webp_report(asset))["results"][0]
        self.assertIsNone(result["folder_role"])
        self.assertFalse(result["unified_image_eligible"])

    def test_103_banner_convertible_action_still_false(self):
        role = folder_result("Banner Mock")
        asset = webp_result(folder="Banner Mock", action=webp_core.WebPAction.CONVERT_TO_WEBP)
        result = one(role, asset)
        self.assertTrue(result["source_asset_eligible"])
        self.assertEqual(result["webp_action"], "convert_to_webp")
        self.assertFalse(result["unified_image_eligible"])

    def test_104_photos_psd_false(self):
        asset = webp_result(asset_class=asset_core.AssetClass.DESIGN_SOURCE,
                            mime="image/vnd.adobe.photoshop", eligible=False,
                            action=webp_core.WebPAction.NOT_ALLOWED,
                            reason="design_source_not_storefront_asset")
        self.assertFalse(one(asset=asset)["unified_image_eligible"])

    def test_105_banner_psd_false(self):
        role = folder_result("Banner Mock")
        asset = webp_result(folder="Banner Mock", asset_class=asset_core.AssetClass.DESIGN_SOURCE,
                            mime="image/vnd.adobe.photoshop", eligible=False,
                            action=webp_core.WebPAction.NOT_ALLOWED,
                            reason="design_source_not_storefront_asset")
        self.assertFalse(one(role, asset)["unified_image_eligible"])

    def test_106_video_source_false_even_in_photos(self):
        asset = webp_result(asset_class=asset_core.AssetClass.VIDEO, mime="video/mp4",
                            eligible=False, action=webp_core.WebPAction.NOT_ALLOWED,
                            reason="video_not_storefront_asset")
        self.assertFalse(one(asset=asset)["unified_image_eligible"])

    def test_107_unsupported_source_false(self):
        asset = webp_result(asset_class=asset_core.AssetClass.UNSUPPORTED,
                            mime="application/octet-stream", eligible=False,
                            action=webp_core.WebPAction.NOT_ALLOWED,
                            reason="unsupported_asset_not_allowed")
        self.assertFalse(one(asset=asset)["unified_image_eligible"])

    def test_108_convert_action_accepted(self):
        self.assertTrue(one(asset=webp_result(action=webp_core.WebPAction.CONVERT_TO_WEBP))["unified_image_eligible"])

    def test_109_validate_action_accepted(self):
        asset = webp_result(mime="image/webp", action=webp_core.WebPAction.VALIDATE_EXISTING_WEBP)
        self.assertTrue(one(asset=asset)["unified_image_eligible"])

    def test_110_not_allowed_rejected(self):
        asset = webp_result(eligible=False, action=webp_core.WebPAction.NOT_ALLOWED,
                            reason="webp_source_mime_not_supported")
        self.assertFalse(one(asset=asset)["unified_image_eligible"])

    def test_111_inputs_are_immutable(self):
        folders, assets = inventory_fixture()
        before = copy.deepcopy((folders, assets))
        build(folders, assets)
        self.assertEqual((folders, assets), before)

    def test_112_width_cannot_enter_contract(self):
        source = webp_report(webp_result())
        source["results"][0]["image_width"] = 4000
        with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            build(assets=source)

    def test_113_height_cannot_enter_contract(self):
        source = webp_report(webp_result())
        source["results"][0]["image_height"] = 4000
        with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            build(assets=source)

    def test_114_size_cannot_enter_contract(self):
        source = webp_report(webp_result())
        source["results"][0]["size_bytes"] = 999999
        with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
            build(assets=source)

    def test_115_folder_role_join_mismatch_fail_closed(self):
        folders = folder_report(folder_result())
        assets = webp_report(webp_result())
        with patch.object(dry_run, "_joined_context_matches", return_value=False):
            result = build(folders, assets)["results"][0]
        self.assertFalse(result["unified_image_eligible"])
        self.assertEqual(result["eligibility_reason"], "folder_role_join_mismatch")
        self.assertIn("folder_role_join_mismatch", result["blocking_issues"])

    def test_116_all_four_root_assets_false(self):
        report = build(*inventory_fixture())
        roots = [item for item in report["results"] if item["depth"] == 0]
        self.assertEqual(len(roots), 4)
        self.assertTrue(all(not item["unified_image_eligible"] for item in roots))

    def test_117_all_39_video_assets_false(self):
        report = build(*inventory_fixture())
        videos = [item for item in report["results"] if item["source_asset_class"] == "video"]
        self.assertEqual(len(videos), 39)
        self.assertTrue(all(not item["unified_image_eligible"] for item in videos))

    def test_118_both_psd_assets_false(self):
        report = build(*inventory_fixture())
        design = [item for item in report["results"] if item["source_asset_class"] == "design_source"]
        self.assertEqual(len(design), 2)
        self.assertTrue(all(not item["unified_image_eligible"] for item in design))

    def test_119_single_unsupported_asset_false(self):
        report = build(*inventory_fixture())
        unsupported = [item for item in report["results"] if item["source_asset_class"] == "unsupported"]
        self.assertEqual(len(unsupported), 1)
        self.assertFalse(unsupported[0]["unified_image_eligible"])

    def test_120_summary_role_specific_counts(self):
        report = build(*inventory_fixture())
        self.assertEqual(report["summary"]["ineligible_banner"], 2)
        self.assertEqual(report["summary"]["ineligible_video_folder"], 38)


def _make_unknown_field_test(target: str, field: str):
    def test(self):
        if target == "folder_top":
            source = folder_report(folder_result())
            source[field] = "MOCK_ONLY"
            with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
                build(source)
        elif target == "webp_top":
            source = webp_report(webp_result())
            source[field] = "MOCK_ONLY"
            with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
                build(assets=source)
        elif target == "folder_record":
            source = folder_report(folder_result())
            source["results"][0][field] = "MOCK_ONLY"
            with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
                build(source)
        else:
            source = webp_report(webp_result())
            source["results"][0][field] = "MOCK_ONLY"
            with self.assertRaises(UnifiedImageEligibilityDryRunInputError):
                build(assets=source)
    return test


# Each generated method is an independently discovered unittest.  The matrix
# rejects safe-looking as well as obviously sensitive unapproved fields, so a
# future adapter cannot silently widen either upstream report contract.
_UNKNOWN_FIELDS = {
    "folder_top": ("input_file", "provider_id", "fingerprint", "url", "download_link"),
    "webp_top": ("input_file", "provider_id", "fingerprint", "url", "media_path"),
    "folder_record": ("provider_file_id", "fingerprint", "file_name", "mime_type", "image_count"),
    "webp_record": ("provider_file_id", "fingerprint", "download_url", "local_path", "image_bytes"),
}
for _target, _fields in _UNKNOWN_FIELDS.items():
    for _index, _field in enumerate(_fields, 1):
        setattr(
            UnifiedImageEligibilityDryRunTests,
            f"test_schema_{_target}_{_index:02}_{_field}",
            _make_unknown_field_test(_target, _field),
        )
