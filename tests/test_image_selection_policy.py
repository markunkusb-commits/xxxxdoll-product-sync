from __future__ import annotations

import ast
import copy
import inspect
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import (
    folder_role_policy as folders,
    image_asset_type_policy as assets,
    image_quality_policy as quality,
    image_selection_policy as policy,
    unified_image_eligibility_policy as unified,
    webp_output_policy as webp,
)
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.image_selection_policy import (
    ImageSelectionBatchResult,
    ImageSelectionCandidate,
    ImageSelectionItem,
    ImageSelectionPolicyError,
    ImageSelectionReason,
    ImageSelectionRole,
    natural_safe_name_key,
    select_images,
    select_images_for_sku,
)


def quality_result(width=1600, height=2000, size=1_000_000, *, deeper=False):
    folder = folders.classify_folder_role("Photos Mock", has_depth_limit_children=deeper)
    source = webp.evaluate_webp_output_policy(
        assets.classify_image_asset_type("image/jpeg", "mock.jpg")
    )
    eligible = unified.evaluate_unified_image_eligibility(folder, source)
    return quality.evaluate_image_quality(
        eligible, image_width=width, image_height=height, size_bytes=size,
    )


def candidate(
    name="photo-1.jpg",
    *, sku="MOCK-001", role=folders.FolderRole.STOREFRONT_PHOTOS,
    width=1600, height=2000, size=1_000_000, kind="nested",
    folder=None, parent=None, start=10, end=20, deeper=False,
    quality_override=None,
):
    if folder is None:
        folder = "Factory Photos Mock" if role is folders.FolderRole.FACTORY_PHOTOS else "Photos Mock"
    if kind == "root":
        folder, parent, source = None, None, None
    else:
        source = ProductSourceRange(start, end)
        if kind == "depth2" and parent is None:
            parent = "Mock Parent"
    return ImageSelectionCandidate(
        sku=sku, folder_role=role, safe_name=name,
        source_manifest_kind=kind, depth={"root": 0, "nested": 1, "depth2": 2}[kind],
        safe_folder_name=folder, parent_safe_folder_name=parent,
        quality_result=quality_result(width, height, size, deeper=deeper) if quality_override is None else quality_override,
        product_source=source, requires_deeper_inventory=deeper,
    )


def candidates(count, role=folders.FolderRole.STOREFRONT_PHOTOS, *, sku="MOCK-001", prefix="photo", start_at=1, **kwargs):
    return [candidate(f"{prefix}-{index}.jpg", sku=sku, role=role, **kwargs) for index in range(start_at, start_at + count)]


def batch(storefront=1, factory=0, *, sku="MOCK-001"):
    values = [
        *candidates(storefront, sku=sku, prefix="store"),
        *candidates(factory, folders.FolderRole.FACTORY_PHOTOS, sku=sku, prefix="factory"),
    ]
    return select_images_for_sku(sku, values)


def selected(result):
    return [item for item in result.items if item.selected]


REALITY_COUNTS = (
    ("CLM-CLASSIC-SI70CM-AR", 14, 27),
    ("CLM-PRO-FD160CM-MERU", 10, 16),
    ("CLM-PRO-FD177-ALIKA", 15, 5),
    ("CLM-PRO-FD177-ZARA", 21, 0),
    ("CLM-ULTRA-SIQ157CM-MIKO", 12, 7),
    ("CLM-ULTRA-SIR161-VICA", 16, 18),
    ("CLM-ULTRA-SIT163-HARRIET", 18, 7),
    ("CLM-ULTRA-SIW160CM-IMANI", 11, 7),
)


def reality_candidates():
    values = []
    for sku, storefront, factory in REALITY_COUNTS:
        values.extend(candidates(storefront, sku=sku, prefix="store"))
        values.extend(candidates(factory, folders.FolderRole.FACTORY_PHOTOS, sku=sku, prefix="factory"))
    return values


class ImageSelectionPolicyTests(unittest.TestCase):
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
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Pure policy only"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def test_001_max_12(self):
        self.assertEqual(batch(20).selected_count, 12)

    def test_002_exactly_12(self):
        self.assertEqual(batch(12).selected_count, 12)

    def test_003_less_than_12(self):
        self.assertEqual(batch(5).selected_count, 5)

    def test_004_storefront_priority(self):
        result = batch(12, 20)
        self.assertEqual(result.selected_storefront, 12)
        self.assertEqual(result.selected_factory, 0)

    def test_005_factory_fill(self):
        result = batch(10, 16)
        self.assertEqual((result.selected_storefront, result.selected_factory), (10, 2))

    def test_006_fourteen_storefront_only(self):
        result = batch(14, 27)
        self.assertEqual((result.selected_storefront, result.selected_factory), (12, 0))

    def test_007_ten_storefront_two_factory(self):
        result = batch(10, 16)
        self.assertEqual((result.selected_storefront, result.selected_factory), (10, 2))

    def test_008_eleven_storefront_one_factory(self):
        result = batch(11, 7)
        self.assertEqual((result.selected_storefront, result.selected_factory), (11, 1))

    def test_009_twenty_one_storefront_selects_twelve(self):
        self.assertEqual(batch(21).selected_storefront, 12)

    def test_010_primary_storefront(self):
        primary = selected(batch(2, 2))[0]
        self.assertEqual(primary.folder_role, folders.FolderRole.STOREFRONT_PHOTOS)
        self.assertEqual(primary.image_role, ImageSelectionRole.PRIMARY)

    def test_011_one_primary_only(self):
        self.assertEqual(batch(20, 20).primary_count, 1)

    def test_012_primary_position_zero(self):
        self.assertEqual(selected(batch(3))[0].selection_position, 0)

    def test_013_gallery_positions_start_one(self):
        self.assertEqual([x.selection_position for x in selected(batch(4))], [0, 1, 2, 3])

    def test_014_factory_fallback_primary(self):
        primary = selected(batch(0, 5))[0]
        self.assertEqual(primary.selection_reason, ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK)
        self.assertEqual(primary.image_role, ImageSelectionRole.PRIMARY)

    def test_015_factory_fallback_warning(self):
        result = batch(0, 5)
        self.assertIn("primary_from_factory_fallback", result.warnings)
        self.assertIn("primary_from_factory_fallback", selected(result)[0].warnings)

    def test_016_zero_candidate(self):
        result = select_images_for_sku("MOCK-EMPTY", [])
        self.assertEqual((result.selected_count, result.primary_count, result.gallery_count), (0, 0, 0))

    def test_017_no_quality_warning(self):
        self.assertIn("no_quality_images_available", select_images_for_sku("MOCK-EMPTY", []).warnings)

    def test_018_natural_one_before_two(self):
        values = [candidate("photo-2.jpg"), candidate("photo-1.jpg")]
        self.assertEqual([x.safe_name for x in selected(select_images_for_sku("MOCK-001", values))], ["photo-1.jpg", "photo-2.jpg"])

    def test_019_natural_two_before_ten(self):
        values = [candidate("photo-10.jpg"), candidate("photo-2.jpg")]
        self.assertEqual([x.safe_name for x in selected(select_images_for_sku("MOCK-001", values))], ["photo-2.jpg", "photo-10.jpg"])

    def test_020_natural_nine_before_ten(self):
        self.assertLess(natural_safe_name_key("photo-9.jpg"), natural_safe_name_key("photo-10.jpg"))

    def test_021_unicode_nfkc_numeric(self):
        self.assertEqual(natural_safe_name_key("photo-２.jpg"), natural_safe_name_key("photo-2.jpg"))

    def test_022_casefold_key(self):
        self.assertEqual(natural_safe_name_key("PHOTO-2.JPG"), natural_safe_name_key("photo-2.jpg"))

    def test_023_provider_order_ignored(self):
        order = (6, 5, 7, 4, 10, 9, 3, 8, 2, 1)
        values = [candidate(f"photo-{number}.jpg") for number in order]
        self.assertEqual([x.safe_name for x in selected(select_images_for_sku("MOCK-001", values))], [f"photo-{n}.jpg" for n in range(1, 11)])

    def test_024_reverse_input_same_selection_order(self):
        values = candidates(20)
        forward = [x.safe_name for x in selected(select_images_for_sku("MOCK-001", values))]
        reverse = [x.safe_name for x in selected(select_images_for_sku("MOCK-001", list(reversed(values))))]
        self.assertEqual(forward, reverse)

    def test_025_permuted_input_same_result_order(self):
        values = candidates(6)
        permuted = [values[i] for i in (3, 0, 5, 2, 1, 4)]
        first = [x.safe_name for x in selected(select_images_for_sku("MOCK-001", values))]
        second = [x.safe_name for x in selected(select_images_for_sku("MOCK-001", permuted))]
        self.assertEqual(first, second)

    def test_026_filename_semantics_not_interpreted(self):
        values = [candidate("z-main.jpg"), candidate("a-ordinary.jpg")]
        self.assertEqual(selected(select_images_for_sku("MOCK-001", values))[0].safe_name, "a-ordinary.jpg")

    def test_027_main_word_no_priority(self):
        self.assertEqual(selected(select_images_for_sku("MOCK-001", [candidate("main-9.jpg"), candidate("image-1.jpg")]))[0].safe_name, "image-1.jpg")

    def test_028_best_word_no_priority(self):
        self.assertEqual(selected(select_images_for_sku("MOCK-001", [candidate("best-9.jpg"), candidate("alpha-1.jpg")]))[0].safe_name, "alpha-1.jpg")

    def test_029_front_word_no_priority(self):
        self.assertEqual(selected(select_images_for_sku("MOCK-001", [candidate("front-9.jpg"), candidate("alpha-1.jpg")]))[0].safe_name, "alpha-1.jpg")

    def test_030_quality_pass_required(self):
        result = select_images_for_sku("MOCK-001", [candidate()])
        self.assertEqual(result.selected_count, 1)

    def test_031_quality_fail_not_selected(self):
        item = select_images_for_sku("MOCK-001", [candidate(width=800, height=1200)]).items[0]
        self.assertFalse(item.selected)
        self.assertEqual(item.selection_reason, ImageSelectionReason.NOT_SELECTED_QUALITY_INELIGIBLE)

    def test_032_no_mp_ranking(self):
        values = [candidate("photo-1.jpg", width=1600, height=2000), candidate("photo-2.jpg", width=4160, height=6240)]
        self.assertEqual(selected(select_images_for_sku("MOCK-001", values))[0].safe_name, "photo-1.jpg")

    def test_033_no_short_edge_ranking(self):
        values = [candidate("photo-1.jpg", width=1600, height=2000), candidate("photo-2.jpg", width=4000, height=5000)]
        self.assertEqual(selected(select_images_for_sku("MOCK-001", values))[0].safe_name, "photo-1.jpg")

    def test_034_no_size_ranking(self):
        values = [candidate("photo-1.jpg", size=1), candidate("photo-2.jpg", size=900_000_000)]
        self.assertEqual(selected(select_images_for_sku("MOCK-001", values))[0].safe_name, "photo-1.jpg")

    def test_035_portrait_allowed(self):
        self.assertEqual(select_images_for_sku("MOCK-001", [candidate(width=1600, height=2000)]).selected_count, 1)

    def test_036_landscape_allowed(self):
        self.assertEqual(select_images_for_sku("MOCK-001", [candidate(width=2000, height=1600)]).selected_count, 1)

    def test_037_square_allowed(self):
        self.assertEqual(select_images_for_sku("MOCK-001", [candidate(width=1800, height=1800)]).selected_count, 1)

    def test_038_no_orientation_quota(self):
        values = [candidate(f"photo-{i}.jpg", width=2000, height=1600) for i in range(1, 13)]
        self.assertEqual(select_images_for_sku("MOCK-001", values).selected_count, 12)

    def test_039_deeper_does_not_block(self):
        result = select_images_for_sku("MOCK-001", [candidate(deeper=True)])
        self.assertEqual(result.selected_count, 1)
        self.assertTrue(selected(result)[0].requires_deeper_inventory)

    def test_040_no_traversal(self):
        select_images_for_sku("MOCK-001", [candidate(deeper=True)])
        self.assertFalse(hasattr(policy, "traverse"))
        self.assertFalse(hasattr(policy, "list_children"))

    def test_041_selected_count_never_over_twelve(self):
        for store, factory in ((0, 50), (5, 50), (12, 50), (50, 50)):
            with self.subTest(store=store, factory=factory):
                self.assertLessEqual(batch(store, factory).selected_count, 12)

    def test_042_primary_count_never_over_one(self):
        self.assertLessEqual(batch(50, 50).primary_count, 1)

    def test_043_nonempty_selection_has_primary(self):
        for store, factory in ((1, 0), (0, 1), (5, 2)):
            self.assertEqual(batch(store, factory).primary_count, 1)

    def test_044_positions_contiguous(self):
        result = batch(10, 16)
        self.assertEqual([x.selection_position for x in selected(result)], list(range(12)))

    def test_045_not_selected_position_null(self):
        result = batch(14)
        self.assertTrue(all(x.selection_position is None for x in result.items if not x.selected))

    def test_046_storefront_selected_count(self):
        self.assertEqual(batch(10, 16).selected_storefront, 10)

    def test_047_factory_selected_count(self):
        self.assertEqual(batch(10, 16).selected_factory, 2)

    def test_048_storefront_selection_reasons(self):
        reasons = [x.selection_reason for x in selected(batch(3))]
        self.assertEqual(reasons, [ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY] + [ImageSelectionReason.SELECTED_STOREFRONT_GALLERY] * 2)

    def test_049_limit_reason(self):
        self.assertTrue(all(x.selection_reason is ImageSelectionReason.NOT_SELECTED_IMAGE_LIMIT for x in batch(14).items if not x.selected))

    def test_050_factory_fill_reason(self):
        factory_items = [x for x in selected(batch(10, 2)) if x.folder_role is folders.FolderRole.FACTORY_PHOTOS]
        self.assertTrue(all(x.selection_reason is ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL for x in factory_items))

    def test_051_batch_deterministic(self):
        values = [*candidates(10), *candidates(10, folders.FolderRole.FACTORY_PHOTOS, prefix="factory")]
        self.assertEqual(select_images_for_sku("MOCK-001", values), select_images_for_sku("MOCK-001", copy.deepcopy(values)))

    def test_052_immutable_results(self):
        with self.assertRaises(FrozenInstanceError):
            batch(1).selected_count = 0
        with self.assertRaises(FrozenInstanceError):
            batch(1).items[0].selected = False

    def test_053_duplicate_safe_name_retained(self):
        result = select_images_for_sku("MOCK-001", [candidate("same.jpg"), candidate("same.jpg")])
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.selected_count, 2)

    def test_054_duplicate_warning(self):
        result = select_images_for_sku("MOCK-001", [candidate("same.jpg"), candidate("same.jpg")])
        self.assertIn("duplicate_selection_name", result.warnings)
        self.assertTrue(all("duplicate_selection_name" in item.warnings for item in result.items))

    def test_055_hierarchy_tie_break(self):
        values = [candidate("same.jpg", folder="Photos B"), candidate("same.jpg", folder="Photos A")]
        result = select_images_for_sku("MOCK-001", values)
        self.assertEqual([x.safe_folder_name for x in selected(result)], ["Photos A", "Photos B"])

    def test_056_no_dedupe(self):
        result = select_images_for_sku("MOCK-001", [candidate("same.jpg") for _ in range(15)])
        self.assertEqual(result.total_candidates, 15)
        self.assertEqual(len(result.items), 15)

    def test_057_five_store_two_factory_selects_seven(self):
        result = batch(5, 2)
        self.assertEqual((result.selected_count, result.selected_storefront, result.selected_factory), (7, 5, 2))

    def test_058_factory_only_selects_five(self):
        result = batch(0, 5)
        self.assertEqual((result.selected_count, result.selected_factory), (5, 5))

    def test_059_no_invented_images(self):
        result = batch(5, 2)
        self.assertEqual(len(result.items), 7)
        self.assertEqual(result.selected_count, result.total_candidates)

    def test_060_classic_shape(self):
        self.assertEqual((batch(14, 27).selected_storefront, batch(14, 27).selected_factory), (12, 0))

    def test_061_meru_shape(self):
        self.assertEqual((batch(10, 16).selected_storefront, batch(10, 16).selected_factory), (10, 2))

    def test_062_alika_shape(self):
        self.assertEqual((batch(15, 5).selected_storefront, batch(15, 5).selected_factory), (12, 0))

    def test_063_zara_shape(self):
        self.assertEqual((batch(21, 0).selected_storefront, batch(21, 0).selected_factory), (12, 0))

    def test_064_miko_shape(self):
        self.assertEqual((batch(12, 7).selected_storefront, batch(12, 7).selected_factory), (12, 0))

    def test_065_vica_shape(self):
        self.assertEqual((batch(16, 18).selected_storefront, batch(16, 18).selected_factory), (12, 0))

    def test_066_harriet_shape(self):
        self.assertEqual((batch(18, 7).selected_storefront, batch(18, 7).selected_factory), (12, 0))

    def test_067_imani_shape(self):
        self.assertEqual((batch(11, 7).selected_storefront, batch(11, 7).selected_factory), (11, 1))

    def test_068_reality_aggregate_selected(self):
        self.assertEqual(sum(x.selected_count for x in select_images(reality_candidates())), 96)

    def test_069_reality_aggregate_storefront(self):
        self.assertEqual(sum(x.selected_storefront for x in select_images(reality_candidates())), 93)

    def test_070_reality_aggregate_factory(self):
        self.assertEqual(sum(x.selected_factory for x in select_images(reality_candidates())), 3)

    def test_071_reality_aggregate_primary(self):
        self.assertEqual(sum(x.primary_count for x in select_images(reality_candidates())), 8)

    def test_072_reality_aggregate_gallery(self):
        self.assertEqual(sum(x.gallery_count for x in select_images(reality_candidates())), 88)

    def test_073_aggregate_not_hardcoded_in_policy(self):
        integers = {node.value for node in ast.walk(ast.parse(inspect.getsource(policy))) if isinstance(node, ast.Constant) and type(node.value) is int}
        self.assertTrue({88, 93, 96}.isdisjoint(integers))

    def test_074_policy_version(self):
        self.assertEqual(policy.POLICY_VERSION, "xxxxdoll-image-selection-v1")
        self.assertEqual(batch(1).policy_version, policy.POLICY_VERSION)

    def test_075_safe_sku_audit(self):
        self.assertEqual(batch(1, sku="CLM-MOCK-001").sku, "CLM-MOCK-001")

    def test_076_source_context_retained(self):
        item = batch(1).items[0]
        self.assertEqual(item.source_manifest_kind, "nested")
        self.assertEqual(item.depth, 1)
        self.assertEqual(item.product_source, ProductSourceRange(10, 20))

    def test_077_no_raw_ids(self):
        text = json.dumps(batch(1).to_dict())
        self.assertNotIn("provider_file_id", text)
        self.assertNotIn("fingerprint", text)

    def test_078_no_drive_urls(self):
        self.assertNotIn("drive.google.com", json.dumps(batch(1).to_dict()))

    def test_079_no_paths(self):
        names = {field.name for field in fields(ImageSelectionCandidate)}
        self.assertTrue(names.isdisjoint({"path", "local_path", "file_path"}))

    def test_080_no_credentials(self):
        for value in ("WP_APP_PASSWORD=mock", "private_key=mock", "https://example.invalid/x"):
            with self.subTest(value=value), self.assertRaises(ImageSelectionPolicyError):
                select_images_for_sku(value, [])

    def test_081_no_wordpress_url(self):
        self.assertNotIn("wordpress_url", json.dumps(batch(1).to_dict()).casefold())

    def test_082_no_upload_authority(self):
        item = batch(1).items[0]
        self.assertFalse(hasattr(item, "wordpress_upload_ready"))
        self.assertFalse(hasattr(item, "upload_ready"))

    def test_083_no_network_imports(self):
        imported = {alias.name for node in ast.walk(ast.parse(inspect.getsource(policy))) if isinstance(node, ast.Import) for alias in node.names}
        self.assertTrue(imported.isdisjoint({"socket", "requests", "urllib", "httplib2", "googleapiclient"}))

    def test_084_no_download(self):
        self.assertFalse(hasattr(policy, "download"))

    def test_085_no_conversion(self):
        self.assertFalse(hasattr(policy, "convert"))

    def test_086_no_upload(self):
        self.assertFalse(hasattr(policy, "upload"))

    def test_087_no_write(self):
        self.assertFalse(hasattr(policy, "write"))

    def test_088_no_pillow(self):
        self.assertNotIn("PIL", inspect.getsource(policy))

    def test_089_no_imagemagick(self):
        self.assertNotIn("ImageMagick", inspect.getsource(policy))

    def test_090_no_cwebp(self):
        self.assertNotIn("cwebp", inspect.getsource(policy))

    def test_091_no_ffmpeg(self):
        self.assertNotIn("ffmpeg", inspect.getsource(policy))

    def test_092_no_media_open(self):
        calls = {node.func.id for node in ast.walk(ast.parse(inspect.getsource(policy))) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertTrue(calls.isdisjoint({"open", "read_bytes", "write_bytes", "read_text"}))

    def test_093_input_sequence_unmodified(self):
        values = [*candidates(5), *candidates(3, folders.FolderRole.FACTORY_PHOTOS, prefix="factory")]
        before = copy.deepcopy(values)
        select_images_for_sku("MOCK-001", values)
        self.assertEqual(values, before)

    def test_094_candidate_immutable(self):
        value = candidate()
        with self.assertRaises(FrozenInstanceError):
            value.safe_name = "changed.jpg"

    def test_095_batch_grouping(self):
        values = [candidate(sku="SKU-2"), candidate(sku="SKU-1")]
        self.assertEqual([x.sku for x in select_images(values)], ["SKU-1", "SKU-2"])

    def test_096_invalid_role_candidate(self):
        value = candidate(role=folders.FolderRole.BANNER)
        item = select_images_for_sku("MOCK-001", [value]).items[0]
        self.assertEqual(item.selection_reason, ImageSelectionReason.INVALID_SELECTION_CANDIDATE)
        self.assertIn("invalid_selection_candidate", item.blocking_issues)

    def test_097_invalid_quality_contract_candidate(self):
        invalid_quality = replace(quality_result(), quality_eligible=False)
        item = select_images_for_sku("MOCK-001", [candidate(quality_override=invalid_quality)]).items[0]
        self.assertEqual(item.selection_reason, ImageSelectionReason.INVALID_SELECTION_CANDIDATE)

    def test_098_quality_fail_reason(self):
        item = select_images_for_sku("MOCK-001", [candidate(width=1600, height=1600)]).items[0]
        self.assertEqual(item.selection_reason, ImageSelectionReason.NOT_SELECTED_QUALITY_INELIGIBLE)

    def test_099_result_item_structure(self):
        self.assertEqual(set(batch(1).items[0].to_dict()), {
            "sku", "folder_role", "safe_name", "source_manifest_kind", "depth",
            "safe_folder_name", "parent_safe_folder_name", "product_source",
            "requires_deeper_inventory", "quality_eligible", "selected",
            "selection_position", "image_role", "selection_reason", "policy_version",
            "warnings", "blocking_issues",
        })

    def test_100_batch_structure(self):
        self.assertEqual(set(batch(1).to_dict()), {
            "sku", "total_candidates", "quality_candidates", "storefront_candidates",
            "factory_candidates", "selected_count", "selected_storefront",
            "selected_factory", "primary_count", "gallery_count", "items",
            "warnings", "blocking_issues", "policy_version",
        })

    def test_101_factory_fallback_gallery_reasons(self):
        result = batch(0, 3)
        self.assertEqual([x.selection_reason for x in selected(result)], [
            ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK,
            ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL,
            ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL,
        ])

    def test_102_quality_candidates_count(self):
        values = [candidate(), candidate("bad.jpg", width=800, height=1200)]
        self.assertEqual(select_images_for_sku("MOCK-001", values).quality_candidates, 1)

    def test_103_storefront_candidates_count(self):
        self.assertEqual(batch(5, 2).storefront_candidates, 5)

    def test_104_factory_candidates_count(self):
        self.assertEqual(batch(5, 2).factory_candidates, 2)

    def test_105_total_candidates_count(self):
        self.assertEqual(batch(5, 2).total_candidates, 7)

    def test_106_gallery_count(self):
        self.assertEqual(batch(10, 2).gallery_count, 11)

    def test_107_duplicate_over_limit_retained(self):
        result = select_images_for_sku("MOCK-001", [candidate("same.jpg") for _ in range(20)])
        self.assertEqual((result.selected_count, len(result.items)), (12, 20))

    def test_108_fullwidth_numeric_selection(self):
        values = [candidate("photo-１０.jpg"), candidate("photo-２.jpg")]
        self.assertEqual([x.safe_name for x in selected(select_images_for_sku("MOCK-001", values))], ["photo-２.jpg", "photo-１０.jpg"])

    def test_109_source_row_tie_break(self):
        values = [candidate("same.jpg", start=30, end=40), candidate("same.jpg", start=10, end=20)]
        result = select_images_for_sku("MOCK-001", values)
        self.assertEqual([x.product_source.start_row for x in selected(result)], [10, 30])

    def test_110_completely_identical_stable_input(self):
        first, second = candidate("same.jpg"), candidate("same.jpg")
        result = select_images_for_sku("MOCK-001", [first, second])
        self.assertEqual(result.items[0].to_dict(), result.items[1].to_dict() | {"selection_position": 0, "image_role": "primary", "selection_reason": "selected_storefront_primary"})


def _make_bad_sequence_test(value):
    def test(self):
        with self.assertRaises(ImageSelectionPolicyError):
            select_images(value)
    return test


for _index, _value in enumerate((None, "mock", b"mock", bytearray(b"mock"), {}, set()), 1):
    setattr(ImageSelectionPolicyTests, f"test_sequence_{_index:02}_{type(_value).__name__}", _make_bad_sequence_test(_value))
