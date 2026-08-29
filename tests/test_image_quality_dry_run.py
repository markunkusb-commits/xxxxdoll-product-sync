from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import (
    cli,
    folder_role_policy as folder_core,
    image_asset_type_policy as asset_core,
    image_quality_dry_run as dry_run,
    image_quality_policy as quality_core,
    unified_image_eligibility_policy as unified_core,
    webp_output_policy as webp_core,
)
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.image_quality_dry_run import ImageQualityDryRunInputError


def context(
    *, sku="MOCK-001", start=10, end=20, kind="nested",
    folder="Photos Mock", parent=None, name="mock.jpg",
):
    if kind == "root":
        folder, parent, source = None, None, None
    else:
        source = {"start_row": start, "end_row": end}
        if kind == "depth2" and parent is None:
            parent = "Mock Parent"
    return {
        "sku": sku, "product_source": source, "source_manifest_kind": kind,
        "depth": {"root": 0, "nested": 1, "depth2": 2}[kind],
        "safe_folder_name": folder, "parent_safe_folder_name": parent,
        "safe_name": name,
    }


def unified_result(
    *, folder_name="Photos Mock", deeper=False, kind="nested",
    parent=None, mime="image/jpeg", **context_overrides,
):
    folder_name = context_overrides.pop("folder", folder_name)
    ctx = context(kind=kind, folder=folder_name, parent=parent, **context_overrides)
    source = webp_core.evaluate_webp_output_policy(
        asset_core.classify_image_asset_type(mime, ctx["safe_name"], sku=ctx["sku"])
    )
    if kind == "root":
        role = None
    else:
        role = folder_core.classify_folder_role(
            folder_name, parent_safe_folder_name=ctx["parent_safe_folder_name"],
            depth=ctx["depth"], sku=ctx["sku"],
            product_source=ProductSourceRange(
                ctx["product_source"]["start_row"], ctx["product_source"]["end_row"]
            ),
            has_depth_limit_children=deeper,
        )
    result = unified_core.evaluate_unified_image_eligibility(role, source)
    data = result.to_dict()
    version = data.pop("policy_version")
    return {
        **ctx, "source_asset_class": source.source_asset_class.value,
        "source_mime_type": source.source_mime_type,
        "join_status": "missing" if kind == "root" else "joined",
        **data, "unified_policy_version": version,
    }


def unified_report(*results, **overrides):
    summary = {
        "total_assets": len(results), "root_assets": sum(x["depth"] == 0 for x in results),
        "depth1_assets": sum(x["depth"] == 1 for x in results),
        "depth2_assets": sum(x["depth"] == 2 for x in results),
        "folder_role_joined": sum(x["join_status"] == "joined" for x in results),
        "folder_role_missing": sum(x["join_status"] == "missing" for x in results),
        "folder_role_ambiguous": sum(x["join_status"] == "ambiguous" for x in results),
        "unified_image_eligible": sum(x["unified_image_eligible"] for x in results),
        "unified_image_ineligible": sum(not x["unified_image_eligible"] for x in results),
        "eligible_storefront_photos": sum(x["unified_image_eligible"] and x["folder_role"] == "storefront_photos" for x in results),
        "eligible_factory_photos": sum(x["unified_image_eligible"] and x["folder_role"] == "factory_photos" for x in results),
        **{key: 0 for key in (
            "ineligible_banner", "ineligible_video_folder", "ineligible_eye_options",
            "ineligible_promo_assets", "ineligible_other_skin_tone", "ineligible_unknown_role",
            "ineligible_missing_role", "ineligible_source_asset",
            "ineligible_invalid_webp_contract", "assets_with_warnings", "blocking_assets",
        )},
        "requires_deeper_inventory_assets": sum(x["requires_deeper_inventory"] for x in results),
        **dict.fromkeys(dry_run.REQUEST_COUNTERS, 0),
    }
    return {
        "status": "ok", "policy_version": unified_core.POLICY_VERSION,
        "folder_role_policy_version": folder_core.POLICY_VERSION,
        "webp_policy_version": webp_core.POLICY_VERSION,
        "summary": summary, "results": list(results),
        **dict.fromkeys(dry_run.REQUEST_COUNTERS, 0), **overrides,
    }


def asset_result(
    *, width=1600, height=2000, size=1_000_000, mime="image/jpeg", **context_overrides,
):
    ctx = context(**context_overrides)
    result = asset_core.classify_image_asset_type(
        mime, ctx["safe_name"],
        size_bytes=size if type(size) is int and size >= 0 else None,
        image_width=width if type(width) is int and width >= 0 else None,
        image_height=height if type(height) is int and height >= 0 else None,
        sku=ctx["sku"],
    ).to_dict()
    result.pop("sku")
    result.pop("folder_role")
    result["image_width"] = width
    result["image_height"] = height
    result["size_bytes"] = size
    return {**ctx, **result, "mime_type": mime}


def asset_report(*results, **overrides):
    summary = {
        "total_manifest_items_seen": len(results), "classified_assets": len(results),
        "skipped_nested_folders": 0, "skipped_shortcuts": 0,
        **{asset.value: sum(x["asset_class"] == asset.value for x in results) for asset in asset_core.AssetClass},
        "storefront_eligible_assets": sum(x["storefront_eligible"] for x in results),
        "storefront_ineligible_assets": sum(not x["storefront_eligible"] for x in results),
        "mime_classified": sum(x["classification_source"] == "mime" for x in results),
        "extension_fallback": sum(x["classification_source"] == "extension_fallback" for x in results),
        "mime_extension_mismatch": sum("asset_extension_mime_mismatch" in x["warnings"] for x in results),
        "assets_with_warnings": sum(bool(x["warnings"]) for x in results),
        "blocking_assets": sum(bool(x["blocking_issues"]) for x in results),
        "root_assets": sum(x["depth"] == 0 for x in results),
        "depth1_assets": sum(x["depth"] == 1 for x in results),
        "depth2_assets": sum(x["depth"] == 2 for x in results),
        **dict.fromkeys(dry_run._ASSET_COUNTERS, 0),
    }
    return {
        "status": "ok", "policy_version": asset_core.POLICY_VERSION,
        "summary": summary, "results": list(results),
        **dict.fromkeys(dry_run._ASSET_COUNTERS, 0), **overrides,
    }


def build(unified=None, assets=None):
    return dry_run.build_image_quality_dry_run_report(
        unified_report(unified_result()) if unified is None else unified,
        asset_report(asset_result()) if assets is None else assets,
    )


def one(unified=None, asset=None):
    return build(
        unified_report(unified_result() if unified is None else unified),
        asset_report(asset_result() if asset is None else asset),
    )["results"][0]


def reality_fixture():
    unified_rows, asset_rows = [], []
    for index in range(117):
        kwargs = dict(sku="MOCK-STOREFRONT", start=10, end=20, folder="Photos Mock", name=f"photo-{index:03}.jpg")
        unified_rows.append(unified_result(**kwargs))
        asset_rows.append(asset_result(width=3024, height=4032, size=2_000_000, **kwargs))
    for index in range(60):
        kwargs = dict(sku="MOCK-FACTORY", start=30, end=40, folder="Factory Photos Mock", name=f"factory-{index:03}.jpg")
        unified_rows.append(unified_result(folder_name="Factory Photos Mock", **{k: v for k, v in kwargs.items() if k != "folder"}))
        asset_rows.append(asset_result(width=4608, height=3072, size=3_000_000, **kwargs))
    for index in range(27):
        kwargs = dict(sku="MOCK-DEEP", start=50, end=60, kind="depth2", folder="Factory Photos Deep", name=f"deep-{index:03}.jpg")
        unified_rows.append(unified_result(folder_name="Factory Photos Deep", deeper=True, **{k: v for k, v in kwargs.items() if k != "folder"}))
        asset_rows.append(asset_result(width=1848, height=2464, size=900_000, **kwargs))
    for index in range(44):
        kwargs = dict(sku="MOCK-SKIP", start=70, end=80, folder="Banner Mock", name=f"skip-{index:03}.jpg")
        unified_rows.append(unified_result(folder_name="Banner Mock", **{k: v for k, v in kwargs.items() if k != "folder"}))
        asset_rows.append(asset_result(width=800, height=1200, size=1, **kwargs))
    return unified_report(*unified_rows), asset_report(*asset_rows)


class ImageQualityDryRunTests(unittest.TestCase):
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
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Offline JSON only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No config"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def files(self, unified=None, assets=None):
        paths = self.project / "unified.json", self.project / "assets.json"
        paths[0].write_text(json.dumps(unified_report(unified_result()) if unified is None else unified), encoding="utf-8")
        paths[1].write_text(json.dumps(asset_report(asset_result()) if assets is None else assets), encoding="utf-8")
        return paths

    def run_cli(self, paths):
        with patch.object(cli, "PROJECT_ROOT", self.project):
            return cli.main(["evaluate-image-quality", "--unified-report", str(paths[0]), "--asset-report", str(paths[1])])

    def test_001_cli_registration(self):
        args = cli.build_parser().parse_args(["evaluate-image-quality", "--unified-report", "u.json", "--asset-report", "a.json"])
        self.assertEqual(args.command, "evaluate-image-quality")
        self.assertEqual(args.unified_report_path, Path("u.json"))
        self.assertEqual(args.asset_report_path, Path("a.json"))

    def test_002_unified_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["evaluate-image-quality", "--asset-report", "a.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_003_asset_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["evaluate-image-quality", "--unified-report", "u.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_004_cli_success(self):
        self.assertEqual(self.run_cli(self.files()), 0)
        self.assertTrue(self.output.is_file())

    def test_005_cli_blocked_returns_one(self):
        self.assertEqual(self.run_cli(self.files(assets=asset_report())), 1)

    def test_006_cli_input_error_returns_two(self):
        source = unified_report(unified_result())
        source["status"] = "partial"
        self.assertEqual(self.run_cli(self.files(source)), 2)

    def test_007_bad_unified_status(self):
        source = unified_report(unified_result())
        source["status"] = "blocked"
        with self.assertRaisesRegex(ImageQualityDryRunInputError, "unified_report_status_not_ok"):
            build(source)

    def test_008_bad_asset_status(self):
        source = asset_report(asset_result())
        source["status"] = "partial"
        with self.assertRaisesRegex(ImageQualityDryRunInputError, "asset_report_status_not_ok"):
            build(assets=source)

    def test_009_unified_version_mismatch(self):
        source = unified_report(unified_result())
        source["policy_version"] = "mock-v2"
        with self.assertRaisesRegex(ImageQualityDryRunInputError, "unified_policy_version_mismatch"):
            build(source)

    def test_010_asset_version_mismatch(self):
        source = asset_report(asset_result())
        source["policy_version"] = "mock-v2"
        with self.assertRaisesRegex(ImageQualityDryRunInputError, "asset_policy_version_mismatch"):
            build(assets=source)

    def test_011_exact_join_success(self):
        self.assertEqual(one()["join_status"], "joined")

    def test_012_sku_component(self):
        self.assertEqual(one(asset=asset_result(sku="OTHER"))["join_status"], "missing")

    def test_013_source_start_component(self):
        self.assertEqual(one(asset=asset_result(start=11))["join_status"], "missing")

    def test_014_source_end_component(self):
        self.assertEqual(one(asset=asset_result(end=21))["join_status"], "missing")

    def test_015_manifest_kind_component(self):
        self.assertEqual(one(asset=asset_result(kind="depth2"))["join_status"], "missing")

    def test_016_depth_component_is_strictly_validated(self):
        source = asset_result()
        source["depth"] = 2
        with self.assertRaisesRegex(ImageQualityDryRunInputError, "invalid_asset_hierarchy"):
            build(assets=asset_report(source))

    def test_017_folder_component(self):
        self.assertEqual(one(asset=asset_result(folder="Different"))["join_status"], "missing")

    def test_018_parent_component(self):
        u = unified_result(folder_name="Photos Detail", kind="depth2", parent="Parent A")
        a = asset_result(kind="depth2", folder="Photos Detail", parent="Parent B")
        self.assertEqual(one(u, a)["join_status"], "missing")

    def test_019_safe_name_component(self):
        self.assertEqual(one(asset=asset_result(name="other.jpg"))["join_status"], "missing")

    def test_020_no_fuzzy_join(self):
        self.assertEqual(one(asset=asset_result(folder="Photos Mck"))["join_status"], "missing")

    def test_021_no_case_insensitive_join(self):
        self.assertEqual(one(asset=asset_result(name="MOCK.JPG"))["join_status"], "missing")

    def test_022_no_substring_join(self):
        self.assertEqual(one(asset=asset_result(folder="Photos"))["join_status"], "missing")

    def test_023_missing_join_fail_closed(self):
        result = build(assets=asset_report())["results"][0]
        self.assertFalse(result["quality_eligible"])
        self.assertEqual(result["quality_reason"], "quality_metadata_join_missing")

    def test_024_ambiguous_join_fail_closed(self):
        duplicate = asset_result()
        result = build(assets=asset_report(duplicate, copy.deepcopy(duplicate)))["results"][0]
        self.assertEqual(result["join_status"], "ambiguous")
        self.assertEqual(result["quality_reason"], "quality_metadata_join_ambiguous")

    def test_025_no_first_match_on_ambiguous(self):
        first, second = asset_result(width=1600), asset_result(width=4000)
        result = build(assets=asset_report(first, second))["results"][0]
        self.assertIsNone(result["image_width"])

    def test_026_upstream_eligible_processed(self):
        self.assertEqual(build()["summary"]["quality_evaluated"], 1)

    def test_027_upstream_ineligible_skipped(self):
        source = unified_result(folder_name="Banner Mock")
        report = build(unified_report(source), asset_report(asset_result(folder="Banner Mock")))
        self.assertEqual(report["results"], [])
        self.assertEqual(report["summary"]["skipped_upstream_ineligible"], 1)

    def test_028_core_called_only_for_joined_eligible(self):
        u = unified_report(unified_result(), unified_result(folder_name="Banner Mock", name="skip.jpg"))
        a = asset_report(asset_result())
        with patch.object(quality_core, "evaluate_image_quality", wraps=quality_core.evaluate_image_quality) as evaluate:
            build(u, a)
        self.assertEqual(evaluate.call_count, 1)

    def test_029_1600x2000_pass(self):
        self.assertTrue(one(asset=asset_result(width=1600, height=2000))["quality_eligible"])

    def test_030_1848x2464_pass(self):
        self.assertTrue(one(asset=asset_result(width=1848, height=2464))["quality_eligible"])

    def test_031_3024x4032_pass(self):
        self.assertTrue(one(asset=asset_result(width=3024, height=4032))["quality_eligible"])

    def test_032_4160x6240_pass(self):
        self.assertTrue(one(asset=asset_result(width=4160, height=6240))["quality_eligible"])

    def test_033_1599_short_edge_fail(self):
        result = one(asset=asset_result(width=1599, height=3000))
        self.assertEqual(result["quality_reason"], "short_edge_below_minimum")

    def test_034_1600_square_mp_fail(self):
        result = one(asset=asset_result(width=1600, height=1600))
        self.assertEqual(result["quality_reason"], "megapixels_below_minimum")

    def test_035_missing_width(self):
        self.assertEqual(one(asset=asset_result(width=None))["quality_reason"], "quality_metadata_missing")

    def test_036_missing_height(self):
        self.assertEqual(one(asset=asset_result(height=None))["quality_reason"], "quality_metadata_missing")

    def test_037_missing_size(self):
        self.assertEqual(one(asset=asset_result(size=None))["quality_reason"], "quality_metadata_missing")

    def test_038_invalid_width(self):
        self.assertEqual(one(asset=asset_result(width="bad"))["quality_reason"], "quality_metadata_invalid")

    def test_039_invalid_height(self):
        self.assertEqual(one(asset=asset_result(height=-1))["quality_reason"], "quality_metadata_invalid")

    def test_040_invalid_size(self):
        self.assertEqual(one(asset=asset_result(size=False))["quality_reason"], "quality_metadata_invalid")

    def test_041_quality_core_reused_with_domain_object(self):
        with patch.object(quality_core, "evaluate_image_quality", wraps=quality_core.evaluate_image_quality) as evaluate:
            build()
        self.assertIs(type(evaluate.call_args.args[0]), unified_core.UnifiedImageEligibilityResult)

    def test_042_threshold_rules_not_copied(self):
        source = inspect.getsource(dry_run.build_image_quality_dry_run_report)
        self.assertNotIn("1600", source)
        self.assertNotIn("3.0", source)

    def test_043_short_edge_output(self):
        self.assertEqual(one(asset=asset_result(width=1848, height=2464))["short_edge"], 1848)

    def test_044_long_edge_output(self):
        self.assertEqual(one(asset=asset_result(width=1848, height=2464))["long_edge"], 2464)

    def test_045_mp_output(self):
        self.assertEqual(one(asset=asset_result(width=1848, height=2464))["megapixels"], 4.553472)

    def test_046_orientation_output(self):
        self.assertEqual(one(asset=asset_result(width=1848, height=2464))["orientation"], "portrait")

    def test_047_portrait_count(self):
        self.assertEqual(build(assets=asset_report(asset_result(width=1600, height=2000)))["summary"]["portrait"], 1)

    def test_048_landscape_count(self):
        self.assertEqual(build(assets=asset_report(asset_result(width=2000, height=1600)))["summary"]["landscape"], 1)

    def test_049_square_count(self):
        self.assertEqual(build(assets=asset_report(asset_result(width=1800, height=1800)))["summary"]["square"], 1)

    def test_050_size_does_not_affect_pass(self):
        self.assertTrue(one(asset=asset_result(size=1))["quality_eligible"])
        self.assertTrue(one(asset=asset_result(size=900_000_000))["quality_eligible"])

    def test_051_storefront_pass_count(self):
        self.assertEqual(build()["summary"]["storefront_quality_pass"], 1)

    def test_052_factory_pass_count(self):
        u = unified_result(folder_name="Factory Photos Mock")
        a = asset_result(folder="Factory Photos Mock")
        self.assertEqual(build(unified_report(u), asset_report(a))["summary"]["factory_quality_pass"], 1)

    def test_053_deeper_direct_images_pass(self):
        report = build(*reality_fixture())
        deep = [x for x in report["results"] if x["requires_deeper_inventory"]]
        self.assertEqual(len(deep), 27)
        self.assertTrue(all(x["quality_eligible"] for x in deep))

    def test_054_deeper_warning_preserved(self):
        report = build(*reality_fixture())
        deep = [x for x in report["results"] if x["requires_deeper_inventory"]]
        self.assertTrue(all("folder_inventory_incomplete" in x["warnings"] for x in deep))

    def test_055_deeper_does_not_trigger_traversal(self):
        build(*reality_fixture())
        self.assertFalse(hasattr(dry_run, "traverse"))
        self.assertFalse(hasattr(dry_run, "list_children"))

    def test_056_quality_fail_record_retained(self):
        report = build(assets=asset_report(asset_result(width=900, height=1200)))
        self.assertEqual(len(report["results"]), 1)
        self.assertFalse(report["results"][0]["quality_eligible"])

    def test_057_deterministic_order(self):
        u = unified_report(unified_result(name="b.jpg"), unified_result(name="a.jpg"))
        a = asset_report(asset_result(name="a.jpg"), asset_result(name="b.jpg"))
        self.assertEqual([x["safe_name"] for x in build(u, a)["results"]], ["b.jpg", "a.jpg"])

    def test_058_input_reports_immutable(self):
        u, a = reality_fixture()
        before = copy.deepcopy((u, a))
        build(u, a)
        self.assertEqual((u, a), before)

    def test_059_reality_total_248(self):
        self.assertEqual(build(*reality_fixture())["summary"]["total_unified_assets"], 248)

    def test_060_reality_204_joined(self):
        summary = build(*reality_fixture())["summary"]
        self.assertEqual(summary["quality_metadata_joined"], 204)
        self.assertEqual(summary["quality_metadata_missing"], 0)
        self.assertEqual(summary["quality_metadata_ambiguous"], 0)

    def test_061_reality_204_pass(self):
        self.assertEqual(build(*reality_fixture())["summary"]["quality_pass"], 204)

    def test_062_reality_zero_fail(self):
        self.assertEqual(build(*reality_fixture())["summary"]["quality_fail"], 0)

    def test_063_reality_44_skipped(self):
        self.assertEqual(build(*reality_fixture())["summary"]["skipped_upstream_ineligible"], 44)

    def test_064_summary_evaluated(self):
        self.assertEqual(build(*reality_fixture())["summary"]["quality_evaluated"], 204)

    def test_065_summary_missing_join(self):
        self.assertEqual(build(assets=asset_report())["summary"]["quality_metadata_missing"], 1)

    def test_066_summary_ambiguous_join(self):
        item = asset_result()
        self.assertEqual(build(assets=asset_report(item, copy.deepcopy(item)))["summary"]["quality_metadata_ambiguous"], 1)

    def test_067_summary_short_edge_failure(self):
        self.assertEqual(build(assets=asset_report(asset_result(width=1599, height=3000)))["summary"]["fail_short_edge"], 1)

    def test_068_summary_mp_failure(self):
        self.assertEqual(build(assets=asset_report(asset_result(width=1600, height=1600)))["summary"]["fail_megapixels"], 1)

    def test_069_summary_metadata_missing_failure(self):
        self.assertEqual(build(assets=asset_report(asset_result(width=None)))["summary"]["fail_metadata_missing"], 1)

    def test_070_summary_metadata_invalid_failure(self):
        self.assertEqual(build(assets=asset_report(asset_result(width="bad")))["summary"]["fail_metadata_invalid"], 1)

    def test_071_no_selection(self):
        text = json.dumps(build())
        self.assertNotIn("main_image", text)
        self.assertNotIn("selected", text)

    def test_072_no_top_n(self):
        self.assertNotIn("top_n", inspect.getsource(dry_run).casefold())

    def test_073_no_ranking(self):
        self.assertFalse(hasattr(dry_run, "rank"))
        self.assertFalse(hasattr(dry_run, "score"))

    def test_074_no_dedupe(self):
        source = inspect.getsource(dry_run.build_image_quality_dry_run_report)
        self.assertNotIn("set(", source)

    def test_075_no_raw_id(self):
        text = json.dumps(build())
        self.assertNotIn("provider_file_id", text)
        self.assertNotIn("fingerprint", text)

    def test_076_no_drive_url(self):
        self.assertNotIn("drive.google.com", json.dumps(build()))

    def test_077_no_local_path(self):
        self.assertNotIn("local_path", json.dumps(build()))

    def test_078_no_wordpress_url(self):
        text = json.dumps(build()).casefold()
        self.assertNotIn("wordpress_url", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)

    def test_079_all_zero_counters(self):
        report = build(*reality_fixture())
        for key in dry_run.REQUEST_COUNTERS:
            self.assertEqual(report[key], 0)
            self.assertEqual(report["summary"][key], 0)

    def test_080_no_pillow_imagemagick_cwebp_ffmpeg(self):
        source = inspect.getsource(dry_run)
        for value in ("PIL", "ImageMagick", "cwebp", "ffmpeg"):
            self.assertNotIn(value, source)

    def test_081_no_media_open(self):
        tree = ast.parse(inspect.getsource(dry_run))
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertTrue(calls.isdisjoint({"open", "read_bytes", "write_bytes"}))

    def test_082_only_two_local_json_reads(self):
        paths = self.files()
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as load:
            dry_run.run_image_quality_dry_run(*paths, project_root=self.project)
        self.assertEqual(load.call_count, 2)

    def test_083_forbidden_other_report_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(ImageQualityDryRunInputError):
            dry_run.run_image_quality_dry_run(Path("folder-role-dry-run.json"), Path("asset.json"), project_root=self.project)
        load.assert_not_called()

    def test_084_env_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(ImageQualityDryRunInputError):
            dry_run.run_image_quality_dry_run(Path(".env"), Path("asset.json"), project_root=self.project)
        load.assert_not_called()

    def test_085_url_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(ImageQualityDryRunInputError):
            dry_run.run_image_quality_dry_run(Path("https://example.invalid/u.json"), Path("asset.json"), project_root=self.project)
        load.assert_not_called()

    def test_086_credentials_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as load, self.assertRaises(ImageQualityDryRunInputError):
            dry_run.run_image_quality_dry_run(Path("credentials.json"), Path("asset.json"), project_root=self.project)
        load.assert_not_called()

    def test_087_same_input_rejected(self):
        path = self.files()[0]
        with self.assertRaisesRegex(ImageQualityDryRunInputError, "input_report_collision"):
            dry_run.run_image_quality_dry_run(path, path, project_root=self.project)

    def test_088_output_field_contract(self):
        self.assertEqual(set(one()), {
            "sku", "product_source", "source_manifest_kind", "depth", "safe_folder_name",
            "parent_safe_folder_name", "safe_name", "folder_role", "unified_image_eligible",
            "requires_deeper_inventory", "image_width", "image_height", "short_edge",
            "long_edge", "pixel_count", "megapixels", "size_bytes", "orientation",
            "quality_eligible", "quality_reason", "min_short_edge_px", "min_megapixels",
            "quality_policy_version", "warnings", "blocking_issues", "join_status",
        })

    def test_089_summary_field_contract(self):
        self.assertEqual(set(build()["summary"]), {
            "total_unified_assets", "upstream_eligible_candidates", "skipped_upstream_ineligible",
            "quality_metadata_joined", "quality_metadata_missing", "quality_metadata_ambiguous",
            "quality_evaluated", "quality_pass", "quality_fail", "fail_short_edge",
            "fail_megapixels", "fail_metadata_missing", "fail_metadata_invalid",
            "fail_upstream", "fail_invalid_policy_input", "portrait", "landscape", "square",
            "storefront_quality_pass", "factory_quality_pass",
            "requires_deeper_inventory_quality_pass", "assets_with_warnings",
            "blocking_assets", *dry_run.REQUEST_COUNTERS,
        })

    def test_090_report_deterministic(self):
        u, a = reality_fixture()
        self.assertEqual(build(copy.deepcopy(u), copy.deepcopy(a)), build(u, a))

    def test_091_no_asset_type_reclassification(self):
        u, a = unified_report(unified_result()), asset_report(asset_result())
        with patch.object(asset_core, "classify_image_asset_type", side_effect=AssertionError("No asset reclassification")) as classify:
            build(u, a)
        classify.assert_not_called()

    def test_092_no_folder_role_reclassification(self):
        u, a = unified_report(unified_result()), asset_report(asset_result())
        with patch.object(folder_core, "classify_folder_role", side_effect=AssertionError("No folder reclassification")) as classify:
            build(u, a)
        classify.assert_not_called()

    def test_093_no_webp_replanning(self):
        u, a = unified_report(unified_result()), asset_report(asset_result())
        with patch.object(webp_core, "evaluate_webp_output_policy", side_effect=AssertionError("No WebP replanning")) as evaluate:
            build(u, a)
        evaluate.assert_not_called()

    def test_094_no_unified_reevaluation(self):
        u, a = unified_report(unified_result()), asset_report(asset_result())
        with patch.object(unified_core, "evaluate_unified_image_eligibility", side_effect=AssertionError("No Unified reevaluation")) as evaluate:
            build(u, a)
        evaluate.assert_not_called()

    def test_095_both_reports_validated_before_quality_core(self):
        source = asset_report(asset_result(), asset_result(name="bad.jpg"))
        source["results"][1]["download_url"] = "MOCK_ONLY"
        with patch.object(quality_core, "evaluate_image_quality") as evaluate, self.assertRaises(ImageQualityDryRunInputError):
            build(assets=source)
        evaluate.assert_not_called()

    def test_096_reality_calls_quality_core_204_times(self):
        u, a = reality_fixture()
        with patch.object(quality_core, "evaluate_image_quality", wraps=quality_core.evaluate_image_quality) as evaluate:
            build(u, a)
        self.assertEqual(evaluate.call_count, 204)

    def test_097_summary_role_split_reality(self):
        summary = build(*reality_fixture())["summary"]
        self.assertEqual((summary["storefront_quality_pass"], summary["factory_quality_pass"]), (117, 87))

    def test_098_deeper_pass_summary_reality(self):
        self.assertEqual(build(*reality_fixture())["summary"]["requires_deeper_inventory_quality_pass"], 27)

    def test_099_join_failure_blocks_report(self):
        report = build(assets=asset_report())
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["summary"]["blocking_assets"], 1)

    def test_100_quality_threshold_failure_is_normal_report(self):
        report = build(assets=asset_report(asset_result(width=900, height=1200)))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["summary"]["quality_fail"], 1)

    def test_101_upstream_and_asset_warnings_merge(self):
        u = unified_result()
        u["warnings"] = ["mock_unified_warning"]
        a = asset_result()
        a["warnings"] = ["mock_asset_warning"]
        result = one(u, a)
        self.assertEqual(set(result["warnings"]), {"mock_unified_warning", "mock_asset_warning"})

    def test_102_warning_summary(self):
        u = unified_result()
        u["warnings"] = ["mock_warning"]
        self.assertEqual(build(unified_report(u), asset_report(asset_result()))["summary"]["assets_with_warnings"], 1)

    def test_103_fail_upstream_stays_zero_due_to_skip(self):
        report = build(unified_report(unified_result(folder_name="Banner Mock")), asset_report())
        self.assertEqual(report["summary"]["fail_upstream"], 0)

    def test_104_no_input_paths_in_report(self):
        report = build()
        forbidden = {"input_file", "input_path", "unified_report_path", "asset_report_path"}
        self.assertTrue(set(report).isdisjoint(forbidden))
        self.assertTrue(all(set(item).isdisjoint(forbidden) for item in report["results"]))

    def test_105_policy_versions_in_report(self):
        report = build()
        self.assertEqual(report["policy_version"], quality_core.POLICY_VERSION)
        self.assertEqual(report["source_unified_policy_version"], unified_core.POLICY_VERSION)
        self.assertEqual(report["source_asset_policy_version"], asset_core.POLICY_VERSION)


def _make_unknown_field_test(target, field):
    def test(self):
        if target == "unified":
            report = unified_report(unified_result())
            report["results"][0][field] = "MOCK_ONLY"
            with self.assertRaises(ImageQualityDryRunInputError):
                build(report)
        else:
            report = asset_report(asset_result())
            report["results"][0][field] = "MOCK_ONLY"
            with self.assertRaises(ImageQualityDryRunInputError):
                build(assets=report)
    return test


for _target, _fields in {
    "unified": ("provider_file_id", "download_url", "local_path", "image_width", "media_id"),
    "asset": ("provider_file_id", "download_url", "local_path", "folder_role", "wordpress_url"),
}.items():
    for _index, _field in enumerate(_fields, 1):
        setattr(ImageQualityDryRunTests, f"test_schema_{_target}_{_index:02}_{_field}", _make_unknown_field_test(_target, _field))
