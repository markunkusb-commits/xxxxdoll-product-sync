from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import random
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
    image_quality_policy as quality_core,
    image_selection_dry_run as dry_run,
    image_selection_policy as selection_core,
    unified_image_eligibility_policy as unified_core,
)
from sync_worker.image_selection_dry_run import ImageSelectionDryRunInputError


def quality_item(
    name="photo-1.jpg", *, sku="MOCK-001", role="storefront_photos",
    eligible=True, width=1600, height=2000, size=1_000_000,
    kind="nested", folder=None, parent=None, start=10, end=20,
    deeper=False, warnings=None,
):
    if not eligible and width >= quality_core.MIN_SHORT_EDGE_PX:
        width = 1000
    if folder is None:
        folder = "Factory Photos Mock" if role == "factory_photos" else "Photos Mock"
    if kind == "root":
        folder, parent, source = None, None, None
    else:
        source = {"start_row": start, "end_row": end}
        if kind == "depth2" and parent is None:
            parent = "Parent Mock"
    short, long = sorted((width, height))
    pixels = width * height
    megapixels = pixels / 1_000_000
    orientation = "square" if width == height else "portrait" if width < height else "landscape"
    reason = "quality_pass" if eligible else "short_edge_below_minimum"
    return {
        "sku": sku, "product_source": source, "source_manifest_kind": kind,
        "depth": {"root": 0, "nested": 1, "depth2": 2}[kind],
        "safe_folder_name": folder, "parent_safe_folder_name": parent,
        "safe_name": name, "folder_role": role,
        "unified_image_eligible": True, "requires_deeper_inventory": deeper,
        "image_width": width, "image_height": height, "short_edge": short,
        "long_edge": long, "pixel_count": pixels, "megapixels": megapixels,
        "size_bytes": size, "orientation": orientation,
        "quality_eligible": eligible, "quality_reason": reason,
        "min_short_edge_px": quality_core.MIN_SHORT_EDGE_PX,
        "min_megapixels": quality_core.MIN_MEGAPIXELS,
        "quality_policy_version": quality_core.POLICY_VERSION,
        "warnings": list(warnings or ()), "blocking_issues": [], "join_status": "joined",
    }


def quality_report(*items, **overrides):
    passed = [item for item in items if item["quality_eligible"]]
    reason = lambda code: sum(item["quality_reason"] == code for item in items)
    summary = {
        "total_unified_assets": len(items), "upstream_eligible_candidates": len(items),
        "skipped_upstream_ineligible": 0, "quality_metadata_joined": len(items),
        "quality_metadata_missing": 0, "quality_metadata_ambiguous": 0,
        "quality_evaluated": len(items), "quality_pass": len(passed),
        "quality_fail": len(items) - len(passed),
        "fail_short_edge": reason("short_edge_below_minimum"),
        "fail_megapixels": reason("megapixels_below_minimum"),
        "fail_metadata_missing": reason("quality_metadata_missing"),
        "fail_metadata_invalid": reason("quality_metadata_invalid"),
        "fail_upstream": reason("upstream_image_ineligible"),
        "fail_invalid_policy_input": reason("invalid_policy_input"),
        "portrait": sum(item["orientation"] == "portrait" for item in items),
        "landscape": sum(item["orientation"] == "landscape" for item in items),
        "square": sum(item["orientation"] == "square" for item in items),
        "storefront_quality_pass": sum(item["quality_eligible"] and item["folder_role"] == "storefront_photos" for item in items),
        "factory_quality_pass": sum(item["quality_eligible"] and item["folder_role"] == "factory_photos" for item in items),
        "requires_deeper_inventory_quality_pass": sum(item["quality_eligible"] and item["requires_deeper_inventory"] for item in items),
        "assets_with_warnings": sum(bool(item["warnings"]) for item in items),
        "blocking_assets": sum(bool(item["blocking_issues"]) for item in items),
        **dict.fromkeys(dry_run.REQUEST_COUNTERS, 0),
    }
    return {
        "status": "ok", "policy_version": quality_core.POLICY_VERSION,
        "source_unified_policy_version": unified_core.POLICY_VERSION,
        "source_asset_policy_version": asset_core.POLICY_VERSION,
        "summary": summary, "results": list(items),
        **dict.fromkeys(dry_run.REQUEST_COUNTERS, 0), **overrides,
    }


def candidates(storefront, factory, *, sku="MOCK-001", eligible=True, start=10):
    values = [
        quality_item(f"photo-{index}.jpg", sku=sku, eligible=eligible, start=start, end=start + 10)
        for index in range(1, storefront + 1)
    ]
    values.extend(
        quality_item(
            f"factory-{index}.jpg", sku=sku, role="factory_photos",
            eligible=eligible, start=start, end=start + 10,
        )
        for index in range(1, factory + 1)
    )
    return values


REALITY_SHAPES = (
    ("CLM-CLASSIC-SI70CM-AR", 14, 27, 12, 0),
    ("CLM-PRO-FD160CM-MERU", 10, 16, 10, 2),
    ("CLM-PRO-FD177-ALIKA", 15, 5, 12, 0),
    ("CLM-PRO-FD177-ZARA", 21, 0, 12, 0),
    ("CLM-ULTRA-SIQ157CM-MIKO", 12, 7, 12, 0),
    ("CLM-ULTRA-SIR161-VICA", 16, 18, 12, 0),
    ("CLM-ULTRA-SIT163-HARRIET", 18, 7, 12, 0),
    ("CLM-ULTRA-SIW160CM-IMANI", 11, 7, 11, 1),
)


def reality_report():
    items = []
    for offset, (sku, storefront, factory, _, _) in enumerate(REALITY_SHAPES):
        items.extend(candidates(storefront, factory, sku=sku, start=10 + offset * 20))
    return quality_report(*items)


def build(*items):
    return dry_run.build_image_selection_dry_run_report(quality_report(*items))


def one(*items):
    return build(*items)["results"][0]


class ImageSelectionDryRunTests(unittest.TestCase):
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
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Offline Quality JSON only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No config"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def file(self, report=None, name="quality.json"):
        path = self.project / name
        path.write_text(json.dumps(quality_report(quality_item()) if report is None else report), encoding="utf-8")
        return path

    def run_cli(self, report=None):
        path = self.file(report)
        with patch.object(cli, "PROJECT_ROOT", self.project):
            return cli.main(["select-product-images", "--quality-report", str(path)])

    def test_001_cli_registration(self):
        args = cli.build_parser().parse_args(["select-product-images", "--quality-report", "q.json"])
        self.assertEqual((args.command, args.quality_report_path), ("select-product-images", Path("q.json")))

    def test_002_quality_report_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["select-product-images"])
        self.assertEqual(caught.exception.code, 2)

    def test_003_invalid_report_status(self):
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "quality_report_status_not_ok"):
            dry_run.build_image_selection_dry_run_report(quality_report(quality_item(), status="blocked"))

    def test_004_policy_version_mismatch(self):
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "quality_policy_version_mismatch"):
            dry_run.build_image_selection_dry_run_report(quality_report(quality_item(), policy_version="wrong"))

    def test_005_only_quality_pass_selected(self):
        result = one(quality_item("pass.jpg"), quality_item("fail.jpg", eligible=False))
        self.assertEqual([item["safe_name"] for item in result["items"] if item["selected"]], ["pass.jpg"])

    def test_006_core_reuse(self):
        with patch.object(selection_core, "select_images", wraps=selection_core.select_images) as mocked:
            build(quality_item())
        mocked.assert_called_once()

    def test_007_no_duplicate_selection_logic(self):
        source = inspect.getsource(dry_run.build_image_selection_dry_run_report)
        self.assertNotIn("sorted(", source)
        self.assertNotIn("[:12]", source)

    def test_008_group_by_sku(self):
        report = build(quality_item(sku="SKU-B"), quality_item(sku="SKU-A"))
        self.assertEqual([result["sku"] for result in report["results"]], ["SKU-A", "SKU-B"])

    def test_009_no_cross_sku_selection(self):
        report = build(*candidates(8, 8, sku="SKU-A"), *candidates(8, 8, sku="SKU-B", start=30))
        self.assertEqual([result["selected_count"] for result in report["results"]], [12, 12])

    def test_010_max_twelve_output(self):
        self.assertEqual(one(*candidates(20, 20))["selected_count"], selection_core.MAX_IMAGES_PER_SKU)

    def test_011_primary_contract(self):
        result = one(*candidates(5, 2))
        self.assertEqual(result["primary_count"], 1)

    def test_012_gallery_contract(self):
        result = one(*candidates(5, 2))
        self.assertEqual(result["gallery_count"], 6)

    def test_013_contiguous_positions(self):
        selected = [item for item in one(*candidates(10, 16))["items"] if item["selected"]]
        self.assertEqual([item["selection_position"] for item in selected], list(range(12)))

    def test_014_storefront_priority(self):
        result = one(*candidates(14, 27))
        self.assertEqual((result["selected_storefront"], result["selected_factory"]), (12, 0))

    def test_015_factory_fill(self):
        result = one(*candidates(10, 16))
        self.assertEqual((result["selected_storefront"], result["selected_factory"]), (10, 2))

    def test_016_classic_shape(self):
        self.assertEqual((one(*candidates(14, 27))["selected_storefront"], one(*candidates(14, 27))["selected_factory"]), (12, 0))

    def test_017_meru_shape(self):
        result = one(*candidates(10, 16)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (10, 2))

    def test_018_alika_shape(self):
        result = one(*candidates(15, 5)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (12, 0))

    def test_019_zara_shape(self):
        result = one(*candidates(21, 0)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (12, 0))

    def test_020_miko_shape(self):
        result = one(*candidates(12, 7)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (12, 0))

    def test_021_vica_shape(self):
        result = one(*candidates(16, 18)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (12, 0))

    def test_022_harriet_shape(self):
        result = one(*candidates(18, 7)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (12, 0))

    def test_023_imani_shape(self):
        result = one(*candidates(11, 7)); self.assertEqual((result["selected_storefront"], result["selected_factory"]), (11, 1))

    def test_024_aggregate_selected(self):
        self.assertEqual(dry_run.build_image_selection_dry_run_report(reality_report())["summary"]["selected_total"], 96)

    def test_025_aggregate_storefront(self):
        self.assertEqual(dry_run.build_image_selection_dry_run_report(reality_report())["summary"]["selected_storefront"], 93)

    def test_026_aggregate_factory(self):
        self.assertEqual(dry_run.build_image_selection_dry_run_report(reality_report())["summary"]["selected_factory"], 3)

    def test_027_aggregate_primary(self):
        self.assertEqual(dry_run.build_image_selection_dry_run_report(reality_report())["summary"]["primary_total"], 8)

    def test_028_aggregate_gallery(self):
        self.assertEqual(dry_run.build_image_selection_dry_run_report(reality_report())["summary"]["gallery_total"], 88)

    def test_029_aggregate_not_selected(self):
        self.assertEqual(dry_run.build_image_selection_dry_run_report(reality_report())["summary"]["not_selected_total"], 108)

    def test_030_quantities_not_hardcoded(self):
        tree = ast.parse(inspect.getsource(dry_run))
        values = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and type(node.value) is int}
        self.assertTrue(values.isdisjoint({96, 93, 88, 108, 204, 117, 87}))

    def test_031_natural_sort_one_to_ten(self):
        result = one(*(quality_item(f"photo-{n}.jpg") for n in (10, 2, 1, 9)))
        self.assertEqual([x["safe_name"] for x in result["items"] if x["selected"]], ["photo-1.jpg", "photo-2.jpg", "photo-9.jpg", "photo-10.jpg"])

    def test_032_provider_order_ignored(self):
        result = one(*(quality_item(f"photo-{n}.jpg") for n in (6, 5, 7, 4, 10, 9, 3, 8, 2, 1)))
        self.assertEqual([x["safe_name"] for x in result["items"]], [f"photo-{n}.jpg" for n in range(1, 11)])

    def test_033_reverse_input_deterministic(self):
        items = candidates(14, 5)
        self.assertEqual(build(*items), build(*reversed(items)))

    def test_034_shuffle_input_deterministic(self):
        items = candidates(14, 5); shuffled = list(items); random.Random(7).shuffle(shuffled)
        self.assertEqual(build(*items), build(*shuffled))

    def test_035_megapixels_ignored_for_ranking(self):
        result = one(quality_item("photo-2.jpg", width=4000, height=5000), quality_item("photo-1.jpg"))
        self.assertEqual(result["items"][0]["safe_name"], "photo-1.jpg")

    def test_036_short_edge_ignored_for_ranking(self):
        result = one(quality_item("photo-2.jpg", width=3000, height=4000), quality_item("photo-1.jpg", width=1600, height=2000))
        self.assertEqual(result["items"][0]["safe_name"], "photo-1.jpg")

    def test_037_size_ignored_for_ranking(self):
        result = one(quality_item("photo-2.jpg", size=99_000_000), quality_item("photo-1.jpg", size=1))
        self.assertEqual(result["items"][0]["safe_name"], "photo-1.jpg")

    def test_038_orientation_ignored(self):
        result = one(quality_item("photo-2.jpg", width=2000, height=1600), quality_item("photo-1.jpg", width=1600, height=2000))
        self.assertEqual(result["items"][0]["safe_name"], "photo-1.jpg")

    def test_039_filename_semantics_ignored(self):
        result = one(quality_item("z-main.jpg"), quality_item("a-ordinary.jpg"))
        self.assertEqual(result["items"][0]["safe_name"], "a-ordinary.jpg")

    def test_040_selected_role_invariant(self):
        selected = [x for x in one(*candidates(2, 2))["items"] if x["selected"]]
        self.assertTrue(all(x["folder_role"] in {"storefront_photos", "factory_photos"} for x in selected))

    def test_041_selected_quality_invariant(self):
        selected = [x for x in one(quality_item(), quality_item("bad.jpg", eligible=False))["items"] if x["selected"]]
        self.assertTrue(all(x["quality_eligible"] and x["quality_reason"] == "quality_pass" for x in selected))

    def test_042_max_limit_invariant(self):
        self.assertLessEqual(one(*candidates(50, 50))["selected_count"], selection_core.MAX_IMAGES_PER_SKU)

    def test_043_primary_exactly_one(self):
        for amount in range(1, 15):
            self.assertEqual(one(*candidates(amount, 0))["primary_count"], 1)

    def test_044_factory_fallback_future(self):
        result = one(*candidates(0, 5))
        self.assertEqual((result["selected_count"], result["primary_count"]), (5, 1))
        self.assertIn("primary_from_factory_fallback", result["warnings"])

    def test_045_no_quality_future(self):
        result = one(*candidates(3, 2, eligible=False))
        self.assertEqual((result["selected_count"], result["primary_count"]), (0, 0))
        self.assertIn("no_quality_images_available", result["warnings"])

    def test_046_below_limit_sku(self):
        summary = build(*candidates(5, 2))["summary"]
        self.assertEqual((summary["skus_at_limit"], summary["skus_below_limit"]), (0, 1))

    def test_047_duplicate_names_retained(self):
        result = one(quality_item("same.jpg"), quality_item("same.jpg"))
        self.assertEqual((result["total_candidates"], result["selected_count"]), (2, 2))

    def test_048_hierarchy_tie_break(self):
        values = [
            quality_item("same.jpg", kind="depth2", folder="B", parent="P", start=20),
            quality_item("same.jpg", kind="depth2", folder="A", parent="P", start=10),
        ]
        result = one(*values)
        self.assertEqual([x["safe_folder_name"] for x in result["items"]], ["A", "B"])

    def test_049_deeper_warnings_retained(self):
        item = quality_item("deep.jpg", role="factory_photos", kind="depth2", deeper=True, warnings=["deeper_inventory_required"])
        output = one(item)["items"][0]
        self.assertTrue(output["requires_deeper_inventory"])
        self.assertIn("deeper_inventory_required", output["warnings"])

    def test_050_no_traversal(self):
        self.assertEqual(build(quality_item(deeper=True))["network_requests_performed"], 0)

    def test_051_report_order_deterministic(self):
        items = [quality_item(sku="SKU-10"), quality_item(sku="SKU-2")]
        self.assertEqual([x["sku"] for x in build(*items)["results"]], ["SKU-2", "SKU-10"])

    def test_052_summary_total(self):
        self.assertEqual(build(*candidates(5, 2))["summary"]["total_quality_items"], 7)

    def test_053_summary_sku(self):
        self.assertEqual(build(quality_item(sku="A"), quality_item(sku="B"))["summary"]["total_skus"], 2)

    def test_054_summary_selected(self):
        self.assertEqual(build(*candidates(5, 2))["summary"]["selected_total"], 7)

    def test_055_summary_not_selected(self):
        self.assertEqual(build(*candidates(14, 5))["summary"]["not_selected_total"], 7)

    def test_056_summary_role_counts(self):
        summary = build(*candidates(10, 16))["summary"]
        self.assertEqual((summary["selected_storefront"], summary["selected_factory"]), (10, 2))

    def test_057_summary_primary_gallery(self):
        summary = build(*candidates(5, 2))["summary"]
        self.assertEqual((summary["primary_total"], summary["gallery_total"]), (1, 6))

    def test_058_factory_fill_summary(self):
        self.assertEqual(build(*candidates(10, 16))["summary"]["factory_fill_skus"], 1)

    def test_059_warnings_summary(self):
        self.assertEqual(build(quality_item(warnings=["mock_warning_code"]))["summary"]["assets_with_warnings"], 1)

    def test_060_blockers_summary(self):
        self.assertEqual(build(quality_item())["summary"]["blocking_assets"], 0)

    def test_061_selected_no_upload_authority(self):
        item = one(quality_item())["items"][0]
        self.assertNotIn("wordpress_upload_ready", item)

    def test_062_raw_ids_absent(self):
        text = json.dumps(build(quality_item())).casefold()
        self.assertNotIn("provider_file_id", text); self.assertNotIn('"id"', text)

    def test_063_urls_absent(self):
        with self.assertRaises(ImageSelectionDryRunInputError):
            build(quality_item("https://example.invalid/image.jpg"))

    def test_064_local_paths_absent(self):
        with self.assertRaises(ImageSelectionDryRunInputError):
            build(quality_item("C:\\private\\image.jpg"))

    def test_065_credentials_absent(self):
        with self.assertRaises(ImageSelectionDryRunInputError):
            build(quality_item("WP_APP_PASSWORD=mock"))

    def test_066_no_drive_import(self):
        imports = {node.names[0].name for node in ast.walk(ast.parse(inspect.getsource(dry_run))) if isinstance(node, ast.Import)}
        self.assertNotIn("googleapiclient", imports)

    def test_067_no_http_import(self):
        tree = ast.parse(inspect.getsource(dry_run))
        imported = {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        self.assertTrue(imported.isdisjoint({"requests", "httplib2", "urllib", "http"}))

    def test_068_network_zero(self):
        self.assertEqual(build(quality_item())["network_requests_performed"], 0)

    def test_069_download_zero(self):
        self.assertEqual(build(quality_item())["download_requests_performed"], 0)

    def test_070_conversion_zero(self):
        self.assertEqual(build(quality_item())["conversion_requests_performed"], 0)

    def test_071_upload_zero(self):
        self.assertEqual(build(quality_item())["wordpress_upload_requests_performed"], 0)

    def test_072_external_writes_zero(self):
        self.assertEqual(build(quality_item())["write_requests_performed"], 0)

    def test_073_no_pillow(self):
        self.assertNotIn("PIL", inspect.getsource(dry_run))

    def test_074_no_imagemagick(self):
        self.assertNotIn("ImageMagick", inspect.getsource(dry_run))

    def test_075_no_cwebp(self):
        self.assertNotIn("cwebp", inspect.getsource(dry_run))

    def test_076_no_ffmpeg(self):
        self.assertNotIn("ffmpeg", inspect.getsource(dry_run))

    def test_077_no_media_open(self):
        tree = ast.parse(inspect.getsource(dry_run))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"]
        self.assertEqual(calls, [])

    def test_078_input_immutable(self):
        source = quality_report(*candidates(4, 4)); before = copy.deepcopy(source)
        dry_run.build_image_selection_dry_run_report(source)
        self.assertEqual(source, before)

    def test_079_selection_core_version(self):
        self.assertEqual(build(quality_item())["policy_version"], selection_core.POLICY_VERSION)

    def test_080_quality_source_version(self):
        self.assertEqual(build(quality_item())["source_quality_policy_version"], quality_core.POLICY_VERSION)

    def test_081_safe_report_schema(self):
        self.assertEqual(set(build(quality_item())), {"status", "policy_version", "source_quality_policy_version", "summary", "results", *dry_run.REQUEST_COUNTERS})

    def test_082_malformed_item_fail_closed(self):
        item = quality_item(); item.pop("safe_name")
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.build_image_selection_dry_run_report(quality_report(item))

    def test_083_duplicate_sku_batches_prohibited(self):
        original = selection_core.select_images
        def duplicate(values):
            batches = original(values)
            return (batches[0], batches[0])
        with patch.object(selection_core, "select_images", side_effect=duplicate), self.assertRaisesRegex(ImageSelectionDryRunInputError, "duplicate_sku_batches"):
            build(quality_item())

    def test_084_no_silent_record_dropping(self):
        report = build(*candidates(14, 27))
        self.assertEqual(sum(len(batch["items"]) for batch in report["results"]), 41)

    def test_085_position_contract_blocks(self):
        original = selection_core.select_images
        def corrupt(values):
            batches = original(values); batch = batches[0]
            item = replace(batch.items[0], selection_position=5)
            return (replace(batch, items=(item, *batch.items[1:])),)
        with patch.object(selection_core, "select_images", side_effect=corrupt):
            report = build(*candidates(2, 0))
        self.assertEqual(report["status"], "blocked")
        self.assertIn("selection_position_contract_violation", report["results"][0]["blocking_issues"])

    def test_086_count_contract_blocks(self):
        original = selection_core.select_images
        def corrupt(values):
            batch = original(values)[0]
            return (replace(batch, selected_count=99),)
        with patch.object(selection_core, "select_images", side_effect=corrupt):
            report = build(quality_item())
        self.assertIn("selection_count_contract_violation", report["results"][0]["blocking_issues"])

    def test_087_cli_success_and_report(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertTrue(self.output.is_file())

    def test_088_cli_report_is_sanitized(self):
        self.assertEqual(self.run_cli(), 0)
        text = self.output.read_text(encoding="utf-8")
        for forbidden in ("Authorization", "Cookie", "WP_APP_PASSWORD", "https://", "provider_file_id"):
            self.assertNotIn(forbidden, text)

    def test_089_invalid_cli_does_not_overwrite(self):
        self.output.parent.mkdir(parents=True); self.output.write_text("old", encoding="utf-8")
        self.assertEqual(self.run_cli(quality_report(quality_item(), status="blocked")), 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "old")

    def test_090_summary_counters_zero(self):
        summary = build(quality_item())["summary"]
        self.assertTrue(all(summary[key] == 0 for key in dry_run.REQUEST_COUNTERS))

    def test_091_top_counters_zero(self):
        report = build(quality_item())
        self.assertTrue(all(report[key] == 0 for key in dry_run.REQUEST_COUNTERS))

    def test_092_factory_fallback_summary(self):
        self.assertEqual(build(*candidates(0, 5))["summary"]["factory_primary_fallback_skus"], 1)

    def test_093_no_quality_summary(self):
        self.assertEqual(build(*candidates(2, 2, eligible=False))["summary"]["no_quality_image_skus"], 1)

    def test_094_selected_deeper_summary(self):
        self.assertEqual(build(quality_item(deeper=True))["summary"]["selected_with_deeper_inventory"], 1)

    def test_095_unselected_position_null(self):
        items = one(*candidates(14, 0))["items"]
        self.assertTrue(all(item["selection_position"] is None for item in items if not item["selected"]))

    def test_096_selection_reasons_retained(self):
        reasons = {item["selection_reason"] for item in one(*candidates(10, 3))["items"] if item["selected"]}
        self.assertEqual(reasons, {"selected_storefront_primary", "selected_storefront_gallery", "selected_factory_gallery_fill"})

    def test_097_quality_metrics_retained_for_audit(self):
        item = one(quality_item(width=2000, height=3000, size=12345))["items"][0]
        self.assertEqual((item["image_width"], item["image_height"], item["size_bytes"]), (2000, 3000, 12345))

    def test_098_selection_policy_version_per_item(self):
        self.assertEqual(one(quality_item())["items"][0]["selection_policy_version"], selection_core.POLICY_VERSION)

    def test_099_quality_policy_version_per_item(self):
        self.assertEqual(one(quality_item())["items"][0]["quality_policy_version"], quality_core.POLICY_VERSION)

    def test_100_output_filename(self):
        self.assertEqual(dry_run.REPORT_FILENAME, "image-selection-dry-run.json")

    def test_101_only_one_input_report_read(self):
        path = self.file()
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as mocked:
            dry_run.run_image_selection_dry_run(path, project_root=self.project)
        mocked.assert_called_once_with(path.resolve())

    def test_102_forbid_env_input(self):
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "forbidden_input_report_path"):
            dry_run.run_image_selection_dry_run(Path(".env"), project_root=self.project)

    def test_103_forbid_credentials_input(self):
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "forbidden_input_report_path"):
            dry_run.run_image_selection_dry_run(Path("credentials.json"), project_root=self.project)

    def test_104_forbid_url_input(self):
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.run_image_selection_dry_run(Path("https://example.invalid/q.json"), project_root=self.project)

    def test_105_forbid_unified_report_input(self):
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "forbidden_input_report_path"):
            dry_run.run_image_selection_dry_run(Path("unified-image-eligibility-dry-run.json"), project_root=self.project)

    def test_106_input_top_counter_nonzero(self):
        report = quality_report(quality_item()); report["network_requests_performed"] = 1
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "quality_report_not_offline"):
            dry_run.build_image_selection_dry_run_report(report)

    def test_107_input_summary_counter_nonzero(self):
        report = quality_report(quality_item()); report["summary"]["download_requests_performed"] = 1
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "quality_report_not_offline"):
            dry_run.build_image_selection_dry_run_report(report)

    def test_108_quality_summary_mismatch(self):
        report = quality_report(quality_item()); report["summary"]["quality_pass"] = 0
        with self.assertRaisesRegex(ImageSelectionDryRunInputError, "quality_summary_mismatch"):
            dry_run.build_image_selection_dry_run_report(report)

    def test_109_source_unified_version_mismatch(self):
        report = quality_report(quality_item()); report["source_unified_policy_version"] = "wrong"
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.build_image_selection_dry_run_report(report)

    def test_110_source_asset_version_mismatch(self):
        report = quality_report(quality_item()); report["source_asset_policy_version"] = "wrong"
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.build_image_selection_dry_run_report(report)

    def test_111_duplicate_warning_retained(self):
        result = one(quality_item("same.jpg"), quality_item("same.jpg"))
        self.assertIn("duplicate_selection_name", result["warnings"])

    def test_112_empty_report(self):
        report = dry_run.build_image_selection_dry_run_report(quality_report())
        self.assertEqual((report["status"], report["summary"]["total_skus"]), ("ok", 0))

    def test_113_invalid_selected_role_blocks(self):
        original = selection_core.select_images
        def corrupt(values):
            batch = original(values)[0]
            item = replace(batch.items[0], folder_role=folder_core.FolderRole.BANNER)
            return (replace(batch, items=(item, *batch.items[1:])),)
        with patch.object(selection_core, "select_images", side_effect=corrupt):
            report = build(quality_item())
        self.assertEqual(report["status"], "blocked")
        self.assertIn("invalid_selected_folder_role", report["results"][0]["blocking_issues"])

    def test_114_selected_quality_ineligible_blocks(self):
        original = selection_core.select_images
        def corrupt(values):
            batch = original(values)[0]
            item = replace(batch.items[0], quality_eligible=False)
            return (replace(batch, items=(item,)),)
        with patch.object(selection_core, "select_images", side_effect=corrupt):
            report = build(quality_item())
        self.assertEqual(report["status"], "blocked")
        self.assertIn("selected_quality_ineligible", report["results"][0]["blocking_issues"])

    def test_115_nonfinite_metric_rejected(self):
        item = quality_item(); item["megapixels"] = float("nan")
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.build_image_selection_dry_run_report(quality_report(item))

    def test_116_oversized_dimension_rejected(self):
        item = quality_item(width=quality_core.MAX_SAFE_DIMENSION_PX + 1, height=2000)
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.build_image_selection_dry_run_report(quality_report(item))


def _make_missing_item_field_test(field):
    def test(self):
        item = quality_item(); item.pop(field)
        report = quality_report(quality_item())
        report["results"] = [item]
        with self.assertRaises(ImageSelectionDryRunInputError):
            dry_run.build_image_selection_dry_run_report(report)
    return test


for _index, _field in enumerate(sorted(dry_run._RESULT_FIELDS), 1):
    setattr(ImageSelectionDryRunTests, f"test_schema_missing_{_index:02}_{_field}", _make_missing_item_field_test(_field))
