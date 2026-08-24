from __future__ import annotations

import copy
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.category_mapping import CATEGORY_REGISTRY_VERSION  # noqa: E402
from sync_worker.woo_category_binding import (  # noqa: E402
    STAGING_BINDING_PROFILE_VERSION,
    STAGING_ENVIRONMENT,
    STAGING_EXPECTED_HOST,
    ApprovedWooCategoryBinding,
    DiscoveryRecordConflictError,
    InvalidProfileWooCategoryIdError,
    ProfileBindingConflictError,
    WooCategoryBindingProfile,
    WooCategoryBindingProfileError,
    staging_category_binding_profile,
    verify_woo_category_bindings,
)
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    StdlibWooCategoryTransport,
    WooCategoryRecord,
    WooCategorySource,
)


def record(category_id: int, name: str, *, parent: int = 0) -> WooCategoryRecord:
    return WooCategoryRecord(
        id=category_id,
        name=name,
        slug=name.casefold().replace(" ", "-"),
        parent=parent,
        count=1,
        description=None,
        display=None,
        parent_name=None,
        category_path=name,
        source=WooCategorySource(),
        warnings=(),
    )


def discovery_records() -> tuple[WooCategoryRecord, ...]:
    return (
        record(1412, "DOLLS"),
        record(1431, "Realistic sex dolls", parent=1412),
        record(1432, "Silicone sex dolls", parent=1412),
        record(1488, "MD DOLLS", parent=1432),
        record(1434, "Torso dolls", parent=1412),
        record(1437, "Uncategorized"),
    )


def verified(records=None, *, environment: str = STAGING_ENVIRONMENT, host: str = STAGING_EXPECTED_HOST):
    return verify_woo_category_bindings(
        staging_category_binding_profile(),
        environment=environment,
        host=host,
        discovery_records=discovery_records() if records is None else records,
    )


def result_for(verification, internal_category_key: str):
    return next(
        result
        for result in verification.results
        if result.internal_category_key == internal_category_key
    )


class WooCategoryBindingTests(unittest.TestCase):
    def test_01_clm_pro_binds_to_1431(self) -> None:
        result = result_for(verified(), "clm-pro")
        self.assertEqual(result.status, "bound_verified")
        self.assertEqual(result.woo_category_id, 1431)

    def test_02_clm_ultra_binds_to_1432(self) -> None:
        result = result_for(verified(), "clm-ultra")
        self.assertEqual(result.status, "bound_verified")
        self.assertEqual(result.woo_category_id, 1432)

    def test_03_classic_is_unbound(self) -> None:
        result = result_for(verified(), "clm-classic")
        self.assertEqual(result.status, "unbound_category")
        self.assertIsNone(result.woo_category_id)

    def test_04_ulw_is_unbound(self) -> None:
        result = result_for(verified(), "clm-ulw")
        self.assertEqual(result.status, "unbound_category")
        self.assertIsNone(result.woo_category_id)

    def test_05_staging_profile_version(self) -> None:
        self.assertEqual(
            staging_category_binding_profile().profile_version,
            "xxxxdoll-staging-category-bind-v1",
        )
        self.assertEqual(
            staging_category_binding_profile().profile_version,
            STAGING_BINDING_PROFILE_VERSION,
        )

    def test_06_environment_is_staging(self) -> None:
        self.assertEqual(staging_category_binding_profile().environment, "staging")

    def test_07_expected_staging_host_is_exact(self) -> None:
        self.assertEqual(
            staging_category_binding_profile().expected_host,
            "staging-1d07-owenau512-iqjhz.wpcomstaging.com",
        )

    def test_08_correct_staging_host_verifies(self) -> None:
        verification = verified()
        self.assertEqual(verification.status, "verified")
        self.assertEqual(verification.blocking_issues, ())

    def test_09_production_host_blocks_staging_profile(self) -> None:
        verification = verified(host="xxxxdoll.com")
        self.assertEqual(verification.status, "blocked")
        self.assertEqual(
            verification.blocking_issues,
            ("category_binding_environment_mismatch",),
        )
        self.assertEqual(verification.results, ())

    def test_10_positive_woo_id_is_accepted(self) -> None:
        profile = WooCategoryBindingProfile(
            "test-profile",
            "staging",
            STAGING_EXPECTED_HOST,
            (ApprovedWooCategoryBinding("clm-pro", 1, "One"),),
        )
        self.assertEqual(profile.bindings[0].woo_category_id, 1)

    def test_11_zero_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidProfileWooCategoryIdError):
            WooCategoryBindingProfile(
                "test-profile",
                "staging",
                STAGING_EXPECTED_HOST,
                (ApprovedWooCategoryBinding("clm-pro", 0, "Zero"),),
            )

    def test_12_negative_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidProfileWooCategoryIdError):
            WooCategoryBindingProfile(
                "test-profile",
                "staging",
                STAGING_EXPECTED_HOST,
                (ApprovedWooCategoryBinding("clm-pro", -1, "Negative"),),
            )

    def test_13_discovery_contains_1431(self) -> None:
        result = result_for(verified(), "clm-pro")
        self.assertEqual(result.discovered_name, "Realistic sex dolls")

    def test_14_discovery_contains_1432(self) -> None:
        result = result_for(verified(), "clm-ultra")
        self.assertEqual(result.discovered_name, "Silicone sex dolls")

    def test_15_1431_name_is_verified(self) -> None:
        result = result_for(verified(), "clm-pro")
        self.assertEqual(result.expected_name, result.discovered_name)

    def test_16_1432_name_is_verified(self) -> None:
        result = result_for(verified(), "clm-ultra")
        self.assertEqual(result.expected_name, result.discovered_name)

    def test_17_missing_target_id_is_blocked(self) -> None:
        records = tuple(item for item in discovery_records() if item.id != 1431)
        verification = verified(records)
        result = result_for(verification, "clm-pro")
        self.assertEqual(result.status, "binding_target_missing")
        self.assertEqual(result.blocking_issues, ("binding_target_missing",))

    def test_18_changed_target_name_is_blocked(self) -> None:
        records = tuple(
            record(item.id, "Renamed category", parent=item.parent or 0)
            if item.id == 1432
            else item
            for item in discovery_records()
        )
        result = result_for(verified(records), "clm-ultra")
        self.assertEqual(result.status, "binding_target_changed")
        self.assertEqual(result.blocking_issues, ("binding_target_changed",))

    def test_19_md_dolls_1488_is_not_bound(self) -> None:
        records = tuple(item for item in discovery_records() if item.id != 1432)
        result = result_for(verified(records), "clm-ultra")
        self.assertEqual(result.status, "binding_target_missing")
        self.assertNotEqual(result.woo_category_id, 1488)

    def test_20_uncategorized_1437_is_not_fallback(self) -> None:
        records = (record(1437, "Uncategorized"),)
        verification = verified(records)
        self.assertTrue(
            all(result.woo_category_id != 1437 for result in verification.results)
        )

    def test_21_classic_does_not_auto_bind(self) -> None:
        records = (*discovery_records(), record(2001, "CLM Classic"))
        result = result_for(verified(records), "clm-classic")
        self.assertEqual(result.status, "unbound_category")

    def test_22_ulw_does_not_auto_bind(self) -> None:
        records = (*discovery_records(), record(2002, "CLM ULW"))
        result = result_for(verified(records), "clm-ulw")
        self.assertEqual(result.status, "unbound_category")

    def test_23_no_fuzzy_category_binding(self) -> None:
        records = (
            record(9001, "Realistic sex dolls"),
            record(9002, "Silicone sex dolls"),
        )
        verification = verified(records)
        self.assertEqual(result_for(verification, "clm-pro").status, "binding_target_missing")
        self.assertEqual(result_for(verification, "clm-ultra").status, "binding_target_missing")

    def test_24_profile_has_no_category_creation_operation(self) -> None:
        profile = staging_category_binding_profile()
        for method in ("create", "create_category", "post", "put", "delete"):
            self.assertFalse(hasattr(profile, method))

    def test_25_verification_is_deterministic(self) -> None:
        self.assertEqual(verified(), verified())

    def test_26_registry_and_profile_versions_are_audited(self) -> None:
        verification = verified()
        self.assertEqual(verification.registry_version, CATEGORY_REGISTRY_VERSION)
        self.assertEqual(
            verification.profile_version,
            STAGING_BINDING_PROFILE_VERSION,
        )

    def test_27_verification_never_calls_woo_transport(self) -> None:
        with patch.object(
            StdlibWooCategoryTransport,
            "get_categories",
            side_effect=AssertionError("Woo API forbidden"),
        ):
            verification = verified()
        self.assertEqual(verification.status, "verified")

    def test_28_verification_opens_no_network_socket(self) -> None:
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network forbidden"),
        ), patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("network forbidden"),
        ):
            verification = verified()
        self.assertEqual(verification.network_requests_performed, 0)

    def test_29_verification_performs_no_external_write(self) -> None:
        with patch.object(
            Path,
            "write_text",
            side_effect=AssertionError("external write forbidden"),
        ):
            verification = verified()
        self.assertEqual(verification.write_requests_performed, 0)

    def test_30_same_internal_category_conflict_is_rejected(self) -> None:
        with self.assertRaises(ProfileBindingConflictError):
            WooCategoryBindingProfile(
                "test-profile",
                "staging",
                STAGING_EXPECTED_HOST,
                (
                    ApprovedWooCategoryBinding("clm-pro", 1431, "First"),
                    ApprovedWooCategoryBinding("clm-pro", 9999, "Second"),
                ),
            )

    def test_31_identical_duplicate_binding_is_deterministically_deduplicated(self) -> None:
        binding = ApprovedWooCategoryBinding("clm-pro", 1431, "Realistic sex dolls")
        profile = WooCategoryBindingProfile(
            "test-profile",
            "staging",
            STAGING_EXPECTED_HOST,
            (binding, binding),
        )
        self.assertEqual(profile.bindings, (binding,))

    def test_32_different_internal_categories_may_share_id_with_warning(self) -> None:
        profile = WooCategoryBindingProfile(
            "test-profile",
            "staging",
            STAGING_EXPECTED_HOST,
            (
                ApprovedWooCategoryBinding("clm-pro", 1431, "Shared"),
                ApprovedWooCategoryBinding("clm-ultra", 1431, "Shared"),
            ),
        )
        self.assertIn("shared_woo_category_id_requires_review", profile.warnings)

    def test_33_different_staging_host_is_blocked(self) -> None:
        verification = verified(host="other.wpcomstaging.com")
        self.assertEqual(
            verification.blocking_issues,
            ("category_binding_environment_mismatch",),
        )

    def test_34_exact_https_url_host_is_accepted(self) -> None:
        verification = verified(host=f"https://{STAGING_EXPECTED_HOST}/")
        self.assertEqual(verification.status, "verified")

    def test_35_production_environment_is_blocked_on_staging_host(self) -> None:
        verification = verified(environment="production")
        self.assertEqual(
            verification.blocking_issues,
            ("category_binding_environment_mismatch",),
        )

    def test_36_environment_mismatch_stops_before_discovery_validation(self) -> None:
        verification = verify_woo_category_bindings(
            staging_category_binding_profile(),
            environment="production",
            host="xxxxdoll.com",
            discovery_records=object(),  # type: ignore[arg-type]
        )
        self.assertEqual(verification.status, "blocked")

    def test_37_duplicate_discovery_id_is_rejected(self) -> None:
        duplicate = (*discovery_records(), record(1431, "Realistic sex dolls"))
        with self.assertRaises(DiscoveryRecordConflictError):
            verified(duplicate)

    def test_38_non_sequence_discovery_input_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            verify_woo_category_bindings(
                staging_category_binding_profile(),
                environment="staging",
                host=STAGING_EXPECTED_HOST,
                discovery_records=object(),  # type: ignore[arg-type]
            )

    def test_39_non_record_discovery_member_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            verify_woo_category_bindings(
                staging_category_binding_profile(),
                environment="staging",
                host=STAGING_EXPECTED_HOST,
                discovery_records=[object()],  # type: ignore[list-item]
            )

    def test_40_result_order_follows_internal_registry_order(self) -> None:
        self.assertEqual(
            [result.internal_category_key for result in verified().results],
            ["clm-classic", "clm-pro", "clm-ulw", "clm-ultra"],
        )

    def test_41_summary_counts_two_bound_and_two_unbound(self) -> None:
        summary = verified().summary
        self.assertEqual(summary.total_internal_categories, 4)
        self.assertEqual(summary.bound_verified, 2)
        self.assertEqual(summary.unbound_categories, 2)
        self.assertEqual(summary.blocking_bindings, 0)

    def test_42_missing_and_changed_targets_are_summarized(self) -> None:
        records = (
            record(1432, "Renamed Silicone"),
            record(1488, "MD DOLLS"),
        )
        summary = verified(records).summary
        self.assertEqual(summary.missing_targets, 1)
        self.assertEqual(summary.changed_targets, 1)
        self.assertEqual(summary.blocking_bindings, 2)

    def test_43_to_dict_is_detached_audit_data(self) -> None:
        verification = verified()
        payload = verification.to_dict()
        payload["profile_version"] = "changed"
        self.assertEqual(
            verification.profile_version,
            STAGING_BINDING_PROFILE_VERSION,
        )

    def test_44_boolean_woo_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidProfileWooCategoryIdError):
            WooCategoryBindingProfile(
                "test-profile",
                "staging",
                STAGING_EXPECTED_HOST,
                (ApprovedWooCategoryBinding("clm-pro", True, "Invalid"),),  # type: ignore[arg-type]
            )

    def test_45_unknown_internal_category_is_rejected(self) -> None:
        with self.assertRaises(WooCategoryBindingProfileError):
            WooCategoryBindingProfile(
                "test-profile",
                "staging",
                STAGING_EXPECTED_HOST,
                (ApprovedWooCategoryBinding("other", 1431, "Other"),),
            )

    def test_46_target_name_match_is_exact(self) -> None:
        records = tuple(
            record(item.id, "realistic sex dolls", parent=item.parent or 0)
            if item.id == 1431
            else item
            for item in discovery_records()
        )
        self.assertEqual(
            result_for(verified(records), "clm-pro").status,
            "binding_target_changed",
        )

    def test_47_profile_and_discovery_inputs_are_not_mutated(self) -> None:
        profile = staging_category_binding_profile()
        records = discovery_records()
        profile_before = copy.deepcopy(profile)
        records_before = copy.deepcopy(records)
        verify_woo_category_bindings(
            profile,
            environment="staging",
            host=STAGING_EXPECTED_HOST,
            discovery_records=records,
        )
        self.assertEqual(profile, profile_before)
        self.assertEqual(records, records_before)


if __name__ == "__main__":
    unittest.main()
