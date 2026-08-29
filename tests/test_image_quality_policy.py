from __future__ import annotations

import ast
import inspect
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from itertools import cycle, islice
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import (
    folder_role_policy as folders,
    image_asset_type_policy as assets,
    image_quality_policy as policy,
    unified_image_eligibility_policy as unified,
    webp_output_policy as webp,
)
from sync_worker.image_quality_policy import (
    ImageOrientation,
    ImageQualityPolicyError,
    ImageQualityReason,
    ImageQualityPolicyResult,
    evaluate_image_quality,
)


def upstream(folder_name="Photos Mock", mime="image/jpeg", **overrides):
    folder = folders.classify_folder_role(folder_name)
    source = webp.evaluate_webp_output_policy(
        assets.classify_image_asset_type(mime, "mock-asset")
    )
    return replace(unified.evaluate_unified_image_eligibility(folder, source), **overrides)


def evaluate(width=1600, height=2000, size=1_000_000, source=None):
    return evaluate_image_quality(
        upstream() if source is None else source,
        image_width=width,
        image_height=height,
        size_bytes=size,
    )


class ImageQualityPolicyTests(unittest.TestCase):
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

    def assert_pass(self, result):
        self.assertIs(result.quality_eligible, True)
        self.assertEqual(result.quality_reason, ImageQualityReason.QUALITY_PASS)
        self.assertEqual(result.blocking_issues, ())

    def assert_fail(self, result, reason):
        self.assertIs(result.quality_eligible, False)
        self.assertEqual(result.quality_reason, reason)

    def test_001_1600x2000_pass(self):
        self.assert_pass(evaluate(1600, 2000))

    def test_002_1848x2464_pass(self):
        self.assert_pass(evaluate(1848, 2464))

    def test_003_3024x4032_pass(self):
        self.assert_pass(evaluate(3024, 4032))

    def test_004_4160x6240_pass(self):
        self.assert_pass(evaluate(4160, 6240))

    def test_005_1599x3000_fails_short_edge(self):
        self.assert_fail(evaluate(1599, 3000), ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM)

    def test_006_1200x3000_fails_short_edge(self):
        self.assert_fail(evaluate(1200, 3000), ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM)

    def test_007_800x1200_fails_short_edge(self):
        self.assert_fail(evaluate(800, 1200), ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM)

    def test_008_1600x1600_fails_megapixels(self):
        self.assert_fail(evaluate(1600, 1600), ImageQualityReason.MEGAPIXELS_BELOW_MINIMUM)

    def test_009_1600x2000_passes_megapixels(self):
        self.assertGreaterEqual(evaluate(1600, 2000).megapixels, policy.MIN_MEGAPIXELS)

    def test_010_exact_1600_edge_can_pass(self):
        result = evaluate(1600, 2000)
        self.assertEqual(result.short_edge, 1600)
        self.assert_pass(result)

    def test_011_exact_3mp_boundary_passes(self):
        result = evaluate(1600, 1875)
        self.assertEqual(result.megapixels, 3.0)
        self.assert_pass(result)

    def test_012_width_missing(self):
        self.assert_fail(evaluate(None, 2000), ImageQualityReason.QUALITY_METADATA_MISSING)

    def test_013_height_missing(self):
        self.assert_fail(evaluate(2000, None), ImageQualityReason.QUALITY_METADATA_MISSING)

    def test_014_size_missing(self):
        self.assert_fail(evaluate(2000, 2000, None), ImageQualityReason.QUALITY_METADATA_MISSING)

    def test_015_width_zero(self):
        self.assert_fail(evaluate(0, 2000), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_016_height_zero(self):
        self.assert_fail(evaluate(2000, 0), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_017_size_zero(self):
        self.assert_fail(evaluate(2000, 2000, 0), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_018_width_negative(self):
        self.assert_fail(evaluate(-1, 2000), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_019_height_negative(self):
        self.assert_fail(evaluate(2000, -1), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_020_size_negative(self):
        self.assert_fail(evaluate(2000, 2000, -1), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_021_wrong_width_type(self):
        self.assert_fail(evaluate("2000", 2000), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_022_wrong_height_type(self):
        self.assert_fail(evaluate(2000, 2.0), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_023_wrong_size_type(self):
        self.assert_fail(evaluate(2000, 2000, True), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_024_short_edge_portrait(self):
        self.assertEqual(evaluate(1848, 2464).short_edge, 1848)

    def test_025_short_edge_landscape(self):
        self.assertEqual(evaluate(2464, 1848).short_edge, 1848)

    def test_026_long_edge(self):
        self.assertEqual(evaluate(1848, 2464).long_edge, 2464)

    def test_027_pixel_count(self):
        self.assertEqual(evaluate(1848, 2464).pixel_count, 1848 * 2464)

    def test_028_megapixels(self):
        self.assertEqual(evaluate(1848, 2464).megapixels, 4.553472)

    def test_029_portrait(self):
        self.assertEqual(evaluate(1848, 2464).orientation, ImageOrientation.PORTRAIT)

    def test_030_landscape(self):
        self.assertEqual(evaluate(2464, 1848).orientation, ImageOrientation.LANDSCAPE)

    def test_031_square(self):
        self.assertEqual(evaluate(2000, 2000).orientation, ImageOrientation.SQUARE)

    def test_032_orientation_does_not_affect_eligibility(self):
        for dimensions in ((1600, 2000), (2000, 1600), (1800, 1800)):
            with self.subTest(dimensions=dimensions):
                self.assert_pass(evaluate(*dimensions))

    def test_033_source_size_not_a_quality_score(self):
        self.assertEqual(evaluate(size=1).quality_eligible, evaluate(size=900_000_000).quality_eligible)

    def test_034_small_positive_source_size_can_pass(self):
        self.assert_pass(evaluate(size=1))

    def test_035_large_source_size_can_pass(self):
        self.assert_pass(evaluate(size=policy.MAX_SAFE_SIZE_BYTES))

    def test_036_no_max_resolution_rejection(self):
        self.assert_pass(evaluate(policy.MAX_SAFE_DIMENSION_PX, policy.MAX_SAFE_DIMENSION_PX))

    def test_037_storefront_role_uses_same_floor(self):
        self.assert_pass(evaluate(source=upstream("Photos Mock")))

    def test_038_factory_role_uses_same_floor(self):
        self.assert_pass(evaluate(source=upstream("Factory Photos Mock")))

    def test_039_deeper_inventory_does_not_affect_quality(self):
        source = upstream(
            "Factory Photos Mock",
            requires_deeper_inventory=True,
            warnings=("folder_inventory_incomplete",),
        )
        result = evaluate(source=source)
        self.assert_pass(result)
        self.assertIn("folder_inventory_incomplete", result.warnings)

    def test_040_upstream_false_blocks(self):
        source = upstream("Banner Mock")
        self.assert_fail(evaluate(source=source), ImageQualityReason.UPSTREAM_IMAGE_INELIGIBLE)

    def test_041_upstream_blocker_preserved(self):
        source = replace(upstream("Banner Mock"), blocking_issues=("mock_upstream_blocker",))
        result = evaluate(source=source)
        self.assertIn("mock_upstream_blocker", result.blocking_issues)
        self.assert_fail(result, ImageQualityReason.UPSTREAM_IMAGE_INELIGIBLE)

    def test_042_invalid_upstream_object_rejected(self):
        for value in (None, {}, object(), "mock"):
            with self.subTest(value=type(value).__name__), self.assertRaisesRegex(
                ImageQualityPolicyError, "unified_image_eligibility_result_required"
            ):
                evaluate_image_quality(value, image_width=1600, image_height=2000, size_bytes=1)

    def test_043_reason_precedence_short_edge_before_mp(self):
        result = evaluate(800, 1000)
        self.assertLess(result.megapixels, policy.MIN_MEGAPIXELS)
        self.assertEqual(result.quality_reason, ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM)

    def test_044_quality_pass_reason(self):
        self.assertEqual(evaluate().quality_reason.value, "quality_pass")

    def test_045_short_edge_reason(self):
        self.assertEqual(evaluate(1599, 3000).quality_reason.value, "short_edge_below_minimum")

    def test_046_megapixel_reason(self):
        self.assertEqual(evaluate(1600, 1600).quality_reason.value, "megapixels_below_minimum")

    def test_047_policy_version(self):
        self.assertEqual(evaluate().policy_version, "xxxxdoll-image-quality-v1")

    def test_048_thresholds_exposed_for_audit(self):
        result = evaluate()
        self.assertEqual(result.min_short_edge_px, 1600)
        self.assertEqual(result.min_megapixels, 3.0)

    def test_049_deterministic(self):
        self.assertEqual(evaluate(), evaluate())
        self.assertEqual(evaluate().to_dict(), evaluate().to_dict())

    def test_050_result_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            evaluate().quality_eligible = False

    def test_051_input_immutable(self):
        source = upstream()
        before = source.to_dict()
        evaluate(source=source)
        self.assertEqual(source.to_dict(), before)

    def test_052_no_filename_input(self):
        parameters = inspect.signature(evaluate_image_quality).parameters
        self.assertTrue(set(parameters).isdisjoint({"safe_name", "filename", "file_name", "name"}))

    def test_053_no_mime_input(self):
        parameters = inspect.signature(evaluate_image_quality).parameters
        self.assertTrue(set(parameters).isdisjoint({"mime", "mime_type", "source_mime_type"}))

    def test_054_no_folder_role_reclassification(self):
        source = upstream()
        with patch.object(folders, "classify_folder_role", side_effect=AssertionError("No role classification")) as classify:
            evaluate(source=source)
        classify.assert_not_called()

    def test_055_no_selection_fields(self):
        names = {field.name for field in fields(ImageQualityPolicyResult)}
        self.assertTrue(names.isdisjoint({"selected", "main_image", "gallery_order", "rank", "priority"}))

    def test_056_no_top_n_or_ordering_logic(self):
        source = inspect.getsource(policy)
        self.assertNotIn("top_n", source.casefold())
        self.assertNotIn("sorted(", source)
        self.assertNotIn("sort(", source)

    def test_057_no_raw_id_interface(self):
        names = set(inspect.signature(evaluate_image_quality).parameters)
        self.assertTrue(names.isdisjoint({"id", "drive_id", "provider_file_id", "media_id", "fingerprint"}))

    def test_058_no_url_interface(self):
        names = set(inspect.signature(evaluate_image_quality).parameters)
        self.assertTrue(names.isdisjoint({"url", "drive_url", "download_url", "wordpress_url"}))

    def test_059_no_local_path_interface(self):
        names = set(inspect.signature(evaluate_image_quality).parameters)
        self.assertTrue(names.isdisjoint({"path", "local_path", "file_path"}))

    def test_060_no_pillow(self):
        self.assertNotIn("PIL", inspect.getsource(policy))

    def test_061_no_cwebp(self):
        self.assertNotIn("cwebp", inspect.getsource(policy).casefold())

    def test_062_no_imagemagick(self):
        self.assertNotIn("imagemagick", inspect.getsource(policy).casefold())

    def test_063_no_ffmpeg(self):
        self.assertNotIn("ffmpeg", inspect.getsource(policy).casefold())

    def test_064_no_open_or_media_read(self):
        tree = ast.parse(inspect.getsource(policy))
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertTrue(calls.isdisjoint({"open", "read", "read_bytes", "read_text"}))

    def test_065_no_network_imports(self):
        tree = ast.parse(inspect.getsource(policy))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertTrue(imported.isdisjoint({"socket", "requests", "urllib", "httplib2", "googleapiclient"}))

    def test_066_no_download_operation(self):
        self.assertFalse(hasattr(policy, "download"))

    def test_067_no_conversion_operation(self):
        self.assertFalse(hasattr(policy, "convert"))

    def test_068_no_upload_operation(self):
        self.assertFalse(hasattr(policy, "upload"))

    def test_069_no_write_operation(self):
        self.assertFalse(hasattr(policy, "write"))

    def test_070_classic_minimum_fixture_passes(self):
        self.assert_pass(evaluate(1848, 2464))

    def test_071_realistic_3024x4032_passes(self):
        self.assert_pass(evaluate(3024, 4032))

    def test_072_realistic_landscape_4608x3072_passes(self):
        result = evaluate(4608, 3072)
        self.assertEqual(result.orientation, ImageOrientation.LANDSCAPE)
        self.assert_pass(result)

    def test_073_204_shape_fixture_all_passes_dynamically(self):
        base = ((1848, 2464), (3024, 4032), (4160, 6240), (4608, 3072))
        shapes = tuple(islice(cycle(base), 204))
        results = [evaluate(width, height) for width, height in shapes]
        self.assertEqual(sum(item.quality_eligible for item in results), len(shapes))

    def test_074_upstream_version_mismatch_fails_closed(self):
        source = upstream()
        object.__setattr__(source, "policy_version", "mock-other-version")
        result = evaluate(source=source)
        self.assert_fail(result, ImageQualityReason.INVALID_POLICY_INPUT)

    def test_075_non_boolean_upstream_eligibility_fails_closed(self):
        source = replace(upstream(), unified_image_eligible=1)
        result = evaluate(source=source)
        self.assert_fail(result, ImageQualityReason.INVALID_POLICY_INPUT)

    def test_076_eligible_with_blocker_is_invalid_input(self):
        source = replace(upstream(), blocking_issues=("mock_blocker",))
        result = evaluate(source=source)
        self.assert_fail(result, ImageQualityReason.INVALID_POLICY_INPUT)
        self.assertIn("mock_blocker", result.blocking_issues)

    def test_077_upstream_warning_preserved(self):
        source = replace(upstream(), warnings=("mock_upstream_warning",))
        self.assertEqual(evaluate(source=source).warnings, ("mock_upstream_warning",))

    def test_078_unsafe_upstream_warning_redacted_and_blocked(self):
        source = replace(upstream(), warnings=("Authorization: mock",))
        result = evaluate(source=source)
        self.assert_fail(result, ImageQualityReason.INVALID_POLICY_INPUT)
        self.assertNotIn("Authorization", json.dumps(result.to_dict()))

    def test_079_unsafe_upstream_blocker_redacted_and_blocked(self):
        source = replace(upstream("Banner Mock"), blocking_issues=("private_key=mock",))
        result = evaluate(source=source)
        self.assert_fail(result, ImageQualityReason.INVALID_POLICY_INPUT)
        self.assertNotIn("private_key", json.dumps(result.to_dict()))
        self.assertIn("unsafe_upstream_audit", result.blocking_issues)

    def test_080_git_safe_result_structure(self):
        self.assertEqual(set(evaluate().to_dict()), {
            "policy_version", "quality_eligible", "quality_reason", "image_width",
            "image_height", "short_edge", "long_edge", "pixel_count", "megapixels",
            "size_bytes", "orientation", "min_short_edge_px", "min_megapixels",
            "warnings", "blocking_issues",
        })

    def test_081_missing_metadata_has_no_derived_metrics(self):
        result = evaluate(None, 2000)
        self.assertIsNone(result.short_edge)
        self.assertIsNone(result.pixel_count)
        self.assertIsNone(result.orientation)

    def test_082_invalid_metadata_has_no_derived_metrics(self):
        result = evaluate(0, 2000)
        self.assertIsNone(result.short_edge)
        self.assertIsNone(result.megapixels)

    def test_083_dimension_over_safety_ceiling_invalid(self):
        result = evaluate(policy.MAX_SAFE_DIMENSION_PX + 1, 2000)
        self.assert_fail(result, ImageQualityReason.QUALITY_METADATA_INVALID)
        self.assertIsNone(result.image_width)

    def test_084_size_over_safety_ceiling_invalid(self):
        result = evaluate(2000, 2000, policy.MAX_SAFE_SIZE_BYTES + 1)
        self.assert_fail(result, ImageQualityReason.QUALITY_METADATA_INVALID)
        self.assertIsNone(result.size_bytes)

    def test_085_extreme_integer_safe_without_multiplication(self):
        result = evaluate(10 ** 1000, 10 ** 1000, 1)
        self.assert_fail(result, ImageQualityReason.QUALITY_METADATA_INVALID)
        self.assertIsNone(result.pixel_count)

    def test_086_booleans_are_not_dimensions(self):
        self.assert_fail(evaluate(True, 2000), ImageQualityReason.QUALITY_METADATA_INVALID)
        self.assert_fail(evaluate(2000, False), ImageQualityReason.QUALITY_METADATA_INVALID)

    def test_087_invalid_metadata_adds_blocker(self):
        self.assertIn("quality_metadata_invalid", evaluate(0, 2000).blocking_issues)

    def test_088_missing_metadata_adds_blocker(self):
        self.assertIn("quality_metadata_missing", evaluate(None, 2000).blocking_issues)

    def test_089_quality_threshold_failure_is_not_system_blocker(self):
        result = evaluate(1599, 3000)
        self.assertEqual(result.blocking_issues, ())

    def test_090_no_upload_authority(self):
        result = evaluate()
        self.assertFalse(hasattr(result, "wordpress_upload_ready"))
        self.assertFalse(hasattr(result, "upload_ready"))


def _make_wrong_type_test(field_name, value, index):
    def test(self):
        values = {"width": 2000, "height": 2000, "size": 100}
        values[field_name] = value
        result = evaluate(values["width"], values["height"], values["size"])
        self.assert_fail(result, ImageQualityReason.QUALITY_METADATA_INVALID)
    test.__name__ = f"test_type_matrix_{index:03}_{field_name}_{type(value).__name__}"
    return test


# Independently discovered cases guard Python's bool-as-int edge and prevent
# coercion of strings/floats/containers into metadata.
_case = 100
for _field in ("width", "height", "size"):
    for _value in (True, False, "2000", 2000.0, [], {}):
        _case += 1
        setattr(ImageQualityPolicyTests, f"test_{_case:03}_{_field}_wrong_type", _make_wrong_type_test(_field, _value, _case))
