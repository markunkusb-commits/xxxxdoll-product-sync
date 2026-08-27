from __future__ import annotations

import copy
import inspect
import json
import socket
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import folder_role_policy as policy
from sync_worker.folder_role_policy import (
    POLICY_VERSION, ROLE_PRIORITY, FolderRole, FolderRolePolicyError,
    classify_folder_role, normalize_folder_name,
)
from sync_worker.image_mapping import ProductSourceRange


class FolderRolePolicyTests(unittest.TestCase):
    def setUp(self):
        self.connect = self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("Real network forbidden")))
        self.create_connection = self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("Real network forbidden")))
        self.factory = self.enterContext(patch("sync_worker.google_api.OfficialGoogleClientFactory", side_effect=AssertionError("No Google clients")))
        self.drive_list = self.enterContext(patch("sync_worker.google_api.GoogleDriveMetadataGateway.list_folder_children", side_effect=AssertionError("No Drive reads")))
        self.http_open = self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("No media downloads")))

    def tearDown(self):
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()
        self.factory.assert_not_called()
        self.drive_list.assert_not_called()
        self.http_open.assert_not_called()

    def assertRole(self, name, role):
        result = classify_folder_role(name)
        self.assertEqual(result.role, role)
        self.assertEqual(result.blocking_issues, ())
        return result

    def test_01_storefront_photos(self):
        self.assertRole("Photos-Mock Model", FolderRole.STOREFRONT_PHOTOS)

    def test_02_singular_photo(self):
        self.assertRole("Photo Mock Model", FolderRole.STOREFRONT_PHOTOS)

    def test_03_photos_casefold(self):
        self.assertRole("pHoToS-MOCK", FolderRole.STOREFRONT_PHOTOS)

    def test_04_whitespace_normalization(self):
        result = self.assertRole(" \t Photos  \r\n Mock   Model \n", FolderRole.STOREFRONT_PHOTOS)
        self.assertEqual(result.normalized_folder_name, "photos mock model")

    def test_05_factory_photos(self):
        self.assertRole("Factory Photos-Mock", FolderRole.FACTORY_PHOTOS)

    def test_06_lowercase_factory_photos(self):
        self.assertRole("factory photos-mock", FolderRole.FACTORY_PHOTOS)

    def test_07_factory_photo_singular(self):
        self.assertRole("Factory Photo Mock", FolderRole.FACTORY_PHOTOS)

    def test_08_other_skin_tone_factory_photos(self):
        self.assertRole("Other Skin Tone Factory Photos", FolderRole.OTHER_SKIN_TONE)

    def test_09_skin_tone_priority_over_factory(self):
        result = self.assertRole("Factory Photos - Other Skin Tone", FolderRole.OTHER_SKIN_TONE)
        self.assertEqual(result.matched_rule, "other_skin_tone_phrase")

    def test_10_banner(self):
        self.assertRole("Banner", FolderRole.BANNER)

    def test_11_banner_prefix(self):
        self.assertRole("Banner-Mock", FolderRole.BANNER)

    def test_12_video(self):
        self.assertRole("Video-Mock", FolderRole.VIDEO)

    def test_13_videos(self):
        self.assertRole("Videos-Mock", FolderRole.VIDEO)

    def test_14_factory_videos(self):
        self.assertRole("Factory Videos - Mock", FolderRole.VIDEO)

    def test_15_factory_video(self):
        self.assertRole("Factory Video-Mock", FolderRole.VIDEO)

    def test_16_eye_options(self):
        self.assertRole("Eye Options (Mock_A)", FolderRole.EYE_OPTIONS)

    def test_17_eye_option(self):
        self.assertRole("Eye Option-Mock", FolderRole.EYE_OPTIONS)

    def test_18_promo_assets(self):
        self.assertRole("Promo assets-Mock", FolderRole.PROMO_ASSETS)

    def test_19_emoji_promo_assets(self):
        result = self.assertRole("🎯 🎯 Promo assets - Mock", FolderRole.PROMO_ASSETS)
        self.assertEqual(result.normalized_folder_name, "🎯 🎯 promo assets mock")

    def test_20_punctuation_promo_assets(self):
        self.assertRole("!!!...Promo assets-Mock", FolderRole.PROMO_ASSETS)

    def test_21_unknown_name(self):
        self.assertRole("Mock Product Collection", FolderRole.UNKNOWN)

    def test_22_no_fuzzy_matching(self):
        for name in ("Phots-Mock", "Fotory Photos-Mock", "Eye Optons", "Prmo assets", "Vidoes-Mock"):
            with self.subTest(name=name):
                self.assertRole(name, FolderRole.UNKNOWN)

    def test_23_photography_is_not_photos(self):
        self.assertRole("Photography-Mock", FolderRole.UNKNOWN)

    def test_24_promotion_is_not_promo_assets(self):
        for name in ("Promotion-Mock", "Promotion assets", "Promotional asset", "Promo"):
            with self.subTest(name=name):
                self.assertRole(name, FolderRole.UNKNOWN)

    def test_25_classification_is_deterministic(self):
        kwargs = {"parent_safe_folder_name": "Mock Parent", "depth": 2, "sku": "CLM-CLASSIC-MOCK", "has_depth_limit_children": True}
        first = classify_folder_role("Factory Photos-Mock", **kwargs)
        second = classify_folder_role("Factory Photos-Mock", **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True), json.dumps(second.to_dict(), sort_keys=True))

    def test_26_normalization_is_deterministic_and_idempotent(self):
        normalized = normalize_folder_name("  ＰＨＯＴＯＳ__－ Mock \t Model  ")
        self.assertEqual(normalized, "photos mock model")
        self.assertEqual(normalize_folder_name(normalized), normalized)

    def test_27_fixed_role_priority(self):
        names = (
            ("Other Skin Tone", FolderRole.OTHER_SKIN_TONE),
            ("Eye Options", FolderRole.EYE_OPTIONS),
            ("Promo assets", FolderRole.PROMO_ASSETS),
            ("Banner", FolderRole.BANNER),
            ("Videos", FolderRole.VIDEO),
            ("Factory Photos", FolderRole.FACTORY_PHOTOS),
            ("Photos-Mock", FolderRole.STOREFRONT_PHOTOS),
        )
        self.assertEqual(ROLE_PRIORITY, tuple(role for _, role in names) + (FolderRole.UNKNOWN,))
        for index, (name, role) in enumerate(names[:-1]):
            for lower_name, _ in names[index + 1:]:
                with self.subTest(higher=name, lower=lower_name):
                    self.assertRole(f"{lower_name} / {name}", role)

    def test_28_safe_parent_is_audit_metadata(self):
        result = classify_folder_role("Photos-Mock", parent_safe_folder_name="Factory Photos Parent")
        self.assertEqual(result.parent_safe_folder_name, "Factory Photos Parent")
        self.assertEqual(result.role, FolderRole.STOREFRONT_PHOTOS)

    def test_29_depth_is_retained(self):
        result = classify_folder_role("Eye Options", depth=2)
        self.assertEqual(result.depth, 2)
        self.assertEqual(result.to_dict()["depth"], 2)

    def test_30_sku_is_retained_for_audit(self):
        result = classify_folder_role("Photos-Mock", sku="CLM-CLASSIC-MOCK")
        self.assertEqual(result.sku, "CLM-CLASSIC-MOCK")
        self.assertEqual(result.to_dict()["sku"], "CLM-CLASSIC-MOCK")

    def test_31_sku_does_not_determine_role(self):
        for sku in ("PHOTOS-MOCK", "FACTORY-PHOTOS", "PROMO-ASSETS", "CLM-ULTRA-MOCK"):
            with self.subTest(sku=sku):
                self.assertEqual(classify_folder_role("Unclassified", sku=sku).role, FolderRole.UNKNOWN)

    def test_32_image_count_is_not_an_input(self):
        self.assertNotIn("image_count", inspect.signature(classify_folder_role).parameters)
        for count in (0, 165, 10000):
            with self.subTest(count=count), self.assertRaises(TypeError):
                classify_folder_role("Unclassified", image_count=count)
        self.assertRole("Unclassified", FolderRole.UNKNOWN)

    def test_33_mime_and_modified_time_do_not_determine_role(self):
        for kwargs in ({"mime_type": "image/jpeg"}, {"mime_count": 99}, {"modified_time": "2026-01-01"}):
            with self.subTest(field=next(iter(kwargs))), self.assertRaises(TypeError):
                classify_folder_role("Unclassified", **kwargs)

    def test_34_storefront_gallery_eligible(self):
        result = classify_folder_role("Photos-Mock")
        self.assertTrue(result.gallery_eligible)
        self.assertTrue(result.storefront_gallery_eligible)

    def test_35_factory_gallery_eligible_but_role_distinct(self):
        result = classify_folder_role("Factory Photos-Mock")
        self.assertTrue(result.gallery_eligible)
        self.assertNotEqual(result.role, FolderRole.STOREFRONT_PHOTOS)

    def test_36_banner_not_gallery_eligible(self):
        self.assertFalse(classify_folder_role("Banner-Mock").gallery_eligible)

    def test_37_video_not_gallery_eligible(self):
        self.assertFalse(classify_folder_role("Factory Videos-Mock").gallery_eligible)

    def test_38_eye_not_gallery_eligible(self):
        self.assertFalse(classify_folder_role("Eye Options").gallery_eligible)

    def test_39_promo_not_gallery_eligible(self):
        self.assertFalse(classify_folder_role("Promo assets").gallery_eligible)

    def test_40_skin_tone_not_gallery_eligible(self):
        self.assertFalse(classify_folder_role("Other Skin Tone Factory Photos").gallery_eligible)

    def test_41_unknown_not_gallery_eligible(self):
        self.assertFalse(classify_folder_role("Unclassified").gallery_eligible)

    def test_42_storefront_deeper_inventory_uses_explicit_flag(self):
        self.assertTrue(classify_folder_role("Photos-Mock", has_depth_limit_children=True).requires_deeper_inventory)
        self.assertFalse(classify_folder_role("Photos-Mock").requires_deeper_inventory)

    def test_43_eye_deeper_inventory_uses_explicit_flag(self):
        self.assertTrue(classify_folder_role("Eye Options", has_depth_limit_children=True).requires_deeper_inventory)
        self.assertFalse(classify_folder_role("Eye Options").requires_deeper_inventory)

    def test_44_promo_deeper_inventory_uses_explicit_flag(self):
        self.assertTrue(classify_folder_role("Promo assets", has_depth_limit_children=True).requires_deeper_inventory)
        self.assertFalse(classify_folder_role("Promo assets").requires_deeper_inventory)

    def test_45_skin_tone_deeper_inventory_uses_explicit_flag(self):
        self.assertTrue(classify_folder_role("Other Skin Tone", has_depth_limit_children=True).requires_deeper_inventory)
        self.assertFalse(classify_folder_role("Other Skin Tone").requires_deeper_inventory)

    def test_46_no_network(self):
        classify_folder_role("Photos-Mock", has_depth_limit_children=True)
        self.connect.assert_not_called()
        self.create_connection.assert_not_called()

    def test_47_no_drive_clients_or_listing(self):
        classify_folder_role("Eye Options", depth=2, has_depth_limit_children=True)
        self.factory.assert_not_called()
        self.drive_list.assert_not_called()

    def test_48_no_download(self):
        classify_folder_role("Factory Photos-Mock", has_depth_limit_children=True)
        self.http_open.assert_not_called()

    def test_49_no_file_reads_or_writes(self):
        with (
            patch("builtins.open", side_effect=AssertionError("No files")) as builtin_open,
            patch("io.open", side_effect=AssertionError("No files")) as io_open,
            patch("os.open", side_effect=AssertionError("No files")) as os_open,
            patch("pathlib.Path.write_text", side_effect=AssertionError("No writes")) as write,
            patch("sync_worker.config.load_google_drive_metadata_config", side_effect=AssertionError("No env reads")) as config,
        ):
            classify_folder_role("Photos-Mock", depth=2).to_dict()
        for mocked in (builtin_open, io_open, os_open, write, config):
            mocked.assert_not_called()

    def test_50_input_immutable(self):
        kwargs = {
            "safe_folder_name": "Factory Photos-Mock", "depth": 2,
            "parent_safe_folder_name": "Mock Parent", "sku": "CLM-CLASSIC-MOCK",
            "product_source": ProductSourceRange(10, 20), "has_depth_limit_children": True,
        }
        before = copy.deepcopy(kwargs)
        classify_folder_role(**kwargs)
        self.assertEqual(kwargs, before)

    def test_51_fixed_policy_version(self):
        result = classify_folder_role("Photos-Mock")
        self.assertEqual(POLICY_VERSION, "xxxxdoll-folder-role-v1")
        self.assertEqual(result.policy_version, POLICY_VERSION)
        self.assertEqual(result.to_dict()["policy_version"], POLICY_VERSION)

    def test_52_unknown_warning_without_blocker(self):
        result = classify_folder_role("Unclassified")
        self.assertEqual(result.warnings, ("folder_role_unknown",))
        self.assertEqual(result.blocking_issues, ())
        self.assertIsNone(result.matched_rule)

    def test_53_no_raw_id_input_dependency(self):
        for field in ("raw_folder_id", "provider_file_id", "raw_depth2_folder_id", "file_id"):
            with self.subTest(field=field), self.assertRaises(TypeError):
                classify_folder_role("Photos-Mock", **{field: "MOCK_PRIVATE_ID"})
        self.assertNotIn("MOCK_PRIVATE_ID", json.dumps(classify_folder_role("Photos-Mock").to_dict()))

    def test_54_no_fingerprint_input_dependency(self):
        for field in ("fingerprint", "folder_id_fingerprint", "file_id_fingerprint"):
            with self.subTest(field=field), self.assertRaises(TypeError):
                classify_folder_role("Unclassified", **{field: "a" * 64})

    def test_55_all_eight_roles_are_explicit(self):
        self.assertEqual({role.value for role in FolderRole}, {
            "storefront_photos", "factory_photos", "banner", "video",
            "eye_options", "promo_assets", "other_skin_tone", "unknown",
        })

    def test_56_underscores_are_separators(self):
        self.assertRole("Factory__Photos_Mock", FolderRole.FACTORY_PHOTOS)
        self.assertEqual(normalize_folder_name("Eye_Options"), "eye options")

    def test_57_hyphens_are_separators(self):
        self.assertRole("Other-Skin--Tone-Factory-Photos", FolderRole.OTHER_SKIN_TONE)

    def test_58_unicode_nfkc(self):
        result = self.assertRole("ＦＡＣＴＯＲＹ　ＰＨＯＴＯＳ－Ｍｏｃｋ", FolderRole.FACTORY_PHOTOS)
        self.assertEqual(result.normalized_folder_name, "factory photos mock")

    def test_59_unicode_casefold_applies_to_normalized_audit_name(self):
        self.assertEqual(normalize_folder_name("Photos-Straße"), "photos strasse")

    def test_60_word_boundaries_prevent_substring_guesses(self):
        for name in ("Photostudio", "Bannerland", "Videography", "Videoscope", "Eyebrow Options", "Eye Optionset", "Promo assetsExtra", "Factory Photoshoot"):
            with self.subTest(name=name):
                self.assertRole(name, FolderRole.UNKNOWN)

    def test_61_storefront_requires_photo_prefix(self):
        self.assertRole("Archive Photos Mock", FolderRole.UNKNOWN)
        self.assertRole("Mock Photo Archive", FolderRole.UNKNOWN)
        self.assertRole("Photos", FolderRole.STOREFRONT_PHOTOS)
        self.assertRole("Photo", FolderRole.STOREFRONT_PHOTOS)

    def test_62_promotional_assets_alias(self):
        self.assertRole("Promotional assets-Mock", FolderRole.PROMO_ASSETS)

    def test_63_promo_asset_singular(self):
        self.assertRole("Promo asset-Mock", FolderRole.PROMO_ASSETS)

    def test_64_no_parent_role_inheritance(self):
        result = classify_folder_role("Yellow", parent_safe_folder_name="Photos-Mock")
        self.assertEqual(result.role, FolderRole.UNKNOWN)
        self.assertFalse(result.gallery_eligible)

    def test_65_depth_does_not_determine_role_or_request_inventory(self):
        for depth in (0, 1, 2, 3):
            with self.subTest(depth=depth):
                result = classify_folder_role("Unclassified", depth=depth)
                self.assertEqual(result.role, FolderRole.UNKNOWN)
                self.assertEqual(result.depth, depth)
                self.assertFalse(result.requires_deeper_inventory)
        self.drive_list.assert_not_called()

    def test_66_source_range_is_audit_only(self):
        for source in (ProductSourceRange(10, 20), ProductSourceRange(500, 550)):
            with self.subTest(source=source):
                result = classify_folder_role("Unclassified", product_source=source)
                self.assertEqual(result.role, FolderRole.UNKNOWN)
                self.assertEqual(result.product_source, source)
                self.assertEqual(result.to_dict()["product_source"], source.to_dict())

    def test_67_depth_limit_flag_requires_real_boolean(self):
        for value in (0, 1, "true", "false", None, [], 1.0):
            with self.subTest(value=value), self.assertRaisesRegex(FolderRolePolicyError, "invalid_depth_limit_children_flag"):
                classify_folder_role("Photos-Mock", has_depth_limit_children=value)

    def test_68_invalid_depth_rejected_without_echoing_value(self):
        for value in (-1, True, "PRIVATE", 2.0):
            with self.subTest(value=value), self.assertRaises(FolderRolePolicyError) as caught:
                classify_folder_role("Photos-Mock", depth=value)
            self.assertEqual(str(caught.exception), "invalid_depth")

    def test_69_invalid_source_rejected(self):
        for source in ({"start_row": 1, "end_row": 2}, ProductSourceRange(0, 2), ProductSourceRange(2, 1), ProductSourceRange(True, 2)):
            with self.subTest(source=source), self.assertRaisesRegex(FolderRolePolicyError, "invalid_product_source"):
                classify_folder_role("Photos-Mock", product_source=source)

    def test_70_urls_are_rejected_in_all_text_inputs(self):
        for field in ("safe_folder_name", "parent_safe_folder_name", "sku"):
            for url in ("https://drive.google.com/drive/folders/MOCK_PRIVATE", "drive.google.com/private", "ｈｔｔｐｓ：／／example.invalid/private"):
                kwargs = {"safe_folder_name": "Photos-Mock", field: url}
                with self.subTest(field=field), self.assertRaises(FolderRolePolicyError) as caught:
                    classify_folder_role(**kwargs)
                self.assertNotIn(url, str(caught.exception))
                self.assertEqual(str(caught.exception), f"unsafe_{field}")

    def test_71_credentials_are_rejected_before_normalization_output(self):
        for value in (
            "Photos ck_" + "X" * 30, "Authorization: MOCK_PRIVATE", "Cookie: MOCK_PRIVATE",
            "private_key=MOCK_PRIVATE", "client_email=MOCK_PRIVATE", "access_token=MOCK_PRIVATE",
            "resourceKey=MOCK_PRIVATE", "owner@example.invalid",
        ):
            with self.subTest(kind=value.split()[0]), self.assertRaises(FolderRolePolicyError) as caught:
                classify_folder_role(value)
            self.assertEqual(str(caught.exception), "unsafe_safe_folder_name")
            self.assertNotIn("MOCK_PRIVATE", repr(caught.exception))

    def test_72_manifest_like_object_never_read_or_stringified(self):
        class ForbiddenManifest:
            @property
            def provider_file_id(self):
                raise AssertionError("Raw provider ID must not be read")

            def __str__(self):
                raise AssertionError("Manifest must not be stringified")

        with self.assertRaisesRegex(FolderRolePolicyError, "invalid_safe_folder_name"):
            classify_folder_role(ForbiddenManifest())

    def test_73_empty_safe_name_is_nonblocking_unknown(self):
        for value in ("", "   \n\t", "___---"):
            with self.subTest(value=value):
                result = self.assertRole(value, FolderRole.UNKNOWN)
                self.assertEqual(result.warnings, ("folder_role_unknown",))
                self.assertEqual(result.normalized_folder_name, "")

    def test_74_output_is_allowlisted_audit_metadata(self):
        result = classify_folder_role("Photos-Mock", product_source=ProductSourceRange(1, 2))
        self.assertEqual(set(result.to_dict()), {
            "role", "policy_version", "normalized_folder_name", "matched_rule", "depth",
            "parent_safe_folder_name", "sku", "product_source", "gallery_eligible",
            "requires_deeper_inventory", "warnings", "blocking_issues",
        })
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["role"], "storefront_photos")

    def test_75_no_cli_or_traversal_entrypoint(self):
        for name in ("main", "build_parser", "GoogleDriveMetadataGateway", "OfficialGoogleClientFactory"):
            self.assertFalse(hasattr(policy, name))

    def test_76_business_symbols_are_not_stripped(self):
        result = self.assertRole("Photos-Mock (A+B)/3D", FolderRole.STOREFRONT_PHOTOS)
        self.assertEqual(result.normalized_folder_name, "photos mock (a+b)/3d")
        self.assertRole("Photos–Mock", FolderRole.UNKNOWN)  # en dash is not an approved '-' separator

    def test_77_matched_rules_are_auditable_and_stable(self):
        for name, rule in (
            ("Photos-Mock", "storefront_photos_prefix"), ("Factory Photo", "factory_photos_phrase"),
            ("Video", "video_word"), ("Banner", "banner_word"), ("Eye Option", "eye_options_phrase"),
            ("🎯 Promo asset", "promo_assets_phrase"), ("Other Skin Tone", "other_skin_tone_phrase"),
        ):
            with self.subTest(name=name):
                result = classify_folder_role(name)
                self.assertEqual(result.matched_rule, rule)
                self.assertEqual(result.warnings, ())

    def test_78_deeper_flag_never_changes_role_or_eligibility(self):
        for name in ("Photos", "Factory Photos", "Banner", "Video", "Eye Options", "Promo assets", "Other Skin Tone", "Unclassified"):
            with self.subTest(name=name):
                shallow = classify_folder_role(name)
                deeper = classify_folder_role(name, has_depth_limit_children=True)
                self.assertEqual(shallow.role, deeper.role)
                self.assertEqual(shallow.gallery_eligible, deeper.gallery_eligible)
                self.assertFalse(shallow.requires_deeper_inventory)
                self.assertTrue(deeper.requires_deeper_inventory)

    def test_79_factory_deeper_flag_does_not_merge_photo_roles(self):
        result = classify_folder_role("Factory Photos", has_depth_limit_children=True)
        self.assertEqual(result.role, FolderRole.FACTORY_PHOTOS)
        self.assertTrue(result.requires_deeper_inventory)
        self.assertTrue(result.gallery_eligible)
        self.drive_list.assert_not_called()

    def test_80_result_is_immutable(self):
        result = classify_folder_role("Photos-Mock")
        with self.assertRaises(FrozenInstanceError):
            result.role = FolderRole.UNKNOWN
        with self.assertRaises(FrozenInstanceError):
            result.policy_version = "unapproved"


if __name__ == "__main__":
    unittest.main()
