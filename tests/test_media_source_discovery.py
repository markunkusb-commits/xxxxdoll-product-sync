from __future__ import annotations

import copy
import hashlib
import json
import socket
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.google_api import OfficialGoogleClientFactory  # noqa: E402
from sync_worker.http_client import ReadOnlyHttpClient  # noqa: E402
from sync_worker.image_mapping import (  # noqa: E402
    REDACTED_REFERENCE,
    MediaSourceMappingResult,
    ProductIdentitySnapshot,
    ProductMediaMappingResult,
    ProductSourceRange,
    create_supplier_media_source_reference,
)
from sync_worker.media_source_discovery import (  # noqa: E402
    discover_media_source,
    discover_media_sources,
    summarize_media_source_discovery,
)
from sync_worker.sku_policy import (  # noqa: E402
    SKU_POLICY_VERSION,
    SkuAudit,
    SkuGenerationResult,
)
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    StdlibWooCategoryTransport,
)


def source(
    raw_reference: str | None,
    *,
    coordinate: str = "I16",
):
    row = int("".join(character for character in coordinate if character.isdigit()))
    return create_supplier_media_source_reference(
        source_coordinate=coordinate,
        source_row=row,
        marker_coordinate=f"B{max(row - 1, 1)}",
        marker_text="Photo download link",
        raw_reference=raw_reference,
    )


def mapping_result(
    *,
    safe_reference: str | None,
    reference_status: str = "available",
) -> MediaSourceMappingResult:
    fingerprint = (
        hashlib.sha256(safe_reference.encode("utf-8")).hexdigest()
        if safe_reference
        else None
    )
    return MediaSourceMappingResult(
        match_status="exact_source_range_match",
        match_method="source_range",
        marker_coordinate="B15",
        marker_text="Photo download link",
        reference_coordinate="I16",
        reference_status=reference_status,  # type: ignore[arg-type]
        safe_reference=safe_reference,
        reference_fingerprint=fingerprint,
        media_source_kind="unknown",
        candidate_product_identities=("ultra:VICA:10-20",),
        download_ready=False,
        warnings=(),
    )


def sku_result(sku: str = "CLM-ULTRA-SIR161-VICA") -> SkuGenerationResult:
    return SkuGenerationResult(
        status="ok",
        sku=sku,
        series="ultra",
        raw_identity="SiR161cm-Vica",
        normalized_identity="SIR161-VICA",
        policy_version=SKU_POLICY_VERSION,
        warnings=(),
        blocking_issues=(),
        conflicting_product_identities=(),
        audit=SkuAudit(
            policy_version=SKU_POLICY_VERSION,
            identity_source="model",
            series_namespace="ULTRA",
        ),
    )


class MediaSourceDiscoveryTests(unittest.TestCase):
    def test_01_google_drive_folder(self) -> None:
        result = discover_media_source(
            source("https://drive.google.com/drive/folders/FOLDER_ID_123")
        )
        self.assertEqual(result.provider, "google_drive")
        self.assertEqual(result.resource_kind, "folder")

    def test_02_google_drive_file(self) -> None:
        result = discover_media_source(
            source("https://drive.google.com/file/d/FILE_ID_123/view")
        )
        self.assertEqual(result.provider, "google_drive")
        self.assertEqual(result.resource_kind, "file")

    def test_03_google_drive_open_id_has_unknown_kind(self) -> None:
        result = discover_media_source(
            source("https://drive.google.com/open?id=OPEN_ID_123")
        )
        self.assertEqual(result.provider, "google_drive")
        self.assertEqual(result.resource_kind, "unknown")

    def test_04_docs_google_is_workspace_resource(self) -> None:
        result = discover_media_source(
            source("https://docs.google.com/spreadsheets/d/SHEET_ID_123/edit")
        )
        self.assertEqual(result.provider, "google_drive")
        self.assertEqual(result.resource_kind, "workspace_resource")

    def test_05_dropbox_folder(self) -> None:
        result = discover_media_source(
            source("https://www.dropbox.com/scl/fo/SHARE_ID_123/folder")
        )
        self.assertEqual(result.provider, "dropbox")
        self.assertEqual(result.resource_kind, "folder")

    def test_06_dropbox_file(self) -> None:
        result = discover_media_source(
            source("https://dropbox.com/scl/fi/SHARE_ID_456/photo.webp")
        )
        self.assertEqual(result.provider, "dropbox")
        self.assertEqual(result.resource_kind, "file")

    def test_07_dropbox_unknown_kind(self) -> None:
        result = discover_media_source(source("https://dropbox.com/share/abc"))
        self.assertEqual(result.provider, "dropbox")
        self.assertEqual(result.resource_kind, "unknown")

    def test_08_onedrive_provider(self) -> None:
        for url in (
            "https://1drv.ms/u/s!opaque",
            "https://onedrive.live.com/?id=opaque",
        ):
            with self.subTest(url=url):
                result = discover_media_source(source(url))
                self.assertEqual(result.provider, "onedrive")
                self.assertEqual(result.resource_kind, "unknown")

    def test_09_sharepoint_provider(self) -> None:
        result = discover_media_source(
            source("https://tenant.sharepoint.com/sites/media/opaque")
        )
        self.assertEqual(result.provider, "sharepoint")
        self.assertEqual(result.resource_kind, "unknown")

    def test_10_direct_https_resource(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/media/resource")
        )
        self.assertEqual(result.provider, "direct_web")
        self.assertEqual(result.resource_kind, "unknown")

    def test_11_jpg_is_direct_image_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.jpg"))
        self.assertEqual(result.resource_kind, "direct_image_candidate")

    def test_12_png_is_direct_image_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.PNG"))
        self.assertEqual(result.resource_kind, "direct_image_candidate")

    def test_13_webp_is_direct_image_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.webp"))
        self.assertEqual(result.resource_kind, "direct_image_candidate")

    def test_14_avif_is_direct_image_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.avif"))
        self.assertEqual(result.resource_kind, "direct_image_candidate")

    def test_15_zip_is_archive_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.zip"))
        self.assertEqual(result.resource_kind, "archive_candidate")

    def test_16_rar_is_archive_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.rar"))
        self.assertEqual(result.resource_kind, "archive_candidate")

    def test_17_7z_is_archive_candidate(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.7z"))
        self.assertEqual(result.resource_kind, "archive_candidate")

    def test_18_extension_classification_is_candidate_only(self) -> None:
        result = discover_media_source(source("https://cdn.example.test/a.jpeg"))
        self.assertIn("resource_not_verified", result.warnings)
        self.assertIs(result.download_ready, False)

    def test_19_verified_image_status_is_never_emitted(self) -> None:
        serialized = json.dumps(
            discover_media_source(
                source("https://cdn.example.test/a.gif")
            ).to_dict()
        )
        self.assertNotIn("verified_image", serialized)

    def test_20_https_is_accepted(self) -> None:
        result = discover_media_source(source("https://example.test/media"))
        self.assertEqual(result.discovery_status, "classified")
        self.assertEqual(result.scheme, "https")

    def test_21_http_is_insecure_blocker(self) -> None:
        result = discover_media_source(source("http://example.test/media"))
        self.assertEqual(result.discovery_status, "insecure_scheme")
        self.assertIn("insecure_media_source", result.blocking_issues)

    def test_22_ftp_is_blocked(self) -> None:
        result = discover_media_source(source("ftp://example.test/media"))
        self.assertEqual(result.discovery_status, "unsupported_scheme")

    def test_23_file_scheme_is_blocked(self) -> None:
        result = discover_media_source(source("file:///private/media.jpg"))
        self.assertEqual(result.discovery_status, "unsupported_scheme")

    def test_24_javascript_scheme_is_blocked(self) -> None:
        result = discover_media_source(source("javascript:alert(1)"))
        self.assertEqual(result.discovery_status, "unsupported_scheme")

    def test_25_data_scheme_is_blocked(self) -> None:
        result = discover_media_source(source("data:image/png;base64,AAAA"))
        self.assertEqual(result.discovery_status, "unsupported_scheme")

    def test_26_embedded_username_is_blocked(self) -> None:
        result = discover_media_source(
            source("https://supplier-user@example.test/media")
        )
        self.assertEqual(result.discovery_status, "embedded_credentials")
        self.assertNotIn("supplier-user", json.dumps(result.to_dict()))

    def test_27_embedded_password_is_blocked(self) -> None:
        result = discover_media_source(
            source("https://supplier-user:private-pass@example.test/media")
        )
        serialized = json.dumps(result.to_dict())
        self.assertEqual(result.discovery_status, "embedded_credentials")
        self.assertNotIn("private-pass", serialized)

    def test_28_query_is_removed_from_safe_output(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/a.webp?download=1")
        )
        self.assertNotIn("?", json.dumps(result.to_dict()))

    def test_29_fragment_is_removed_from_safe_output(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/a.webp#supplier-private")
        )
        self.assertNotIn("supplier-private", json.dumps(result.to_dict()))

    def test_30_token_is_not_leaked(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/a.webp?token=private-token")
        )
        serialized = json.dumps(result.to_dict())
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("token=", serialized)

    def test_31_auth_is_not_leaked(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/a.webp?auth=private-auth")
        )
        self.assertNotIn("private-auth", json.dumps(result.to_dict()))

    def test_32_signature_is_not_leaked(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/a.webp?signature=private-signature")
        )
        self.assertNotIn("private-signature", json.dumps(result.to_dict()))

    def test_33_resource_id_fingerprint_is_sha256(self) -> None:
        resource_id = "FOLDER_ID_123"
        result = discover_media_source(
            source(f"https://drive.google.com/drive/folders/{resource_id}")
        )
        self.assertEqual(
            result.resource_id_fingerprint,
            hashlib.sha256(resource_id.encode("utf-8")).hexdigest(),
        )

    def test_34_raw_resource_id_is_not_in_report(self) -> None:
        resource_id = "DROPBOX_SHARE_ID_PRIVATE"
        result = discover_media_source(
            source(f"https://dropbox.com/scl/fo/{resource_id}/folder")
        )
        self.assertNotIn(resource_id, json.dumps(result.to_dict()))

    def test_35_existing_reference_fingerprint_is_reused(self) -> None:
        item = source("https://drive.google.com/file/d/FILE_ID/view")
        result = discover_media_source(item)
        self.assertEqual(result.reference_fingerprint, item.reference_fingerprint)

    def test_36_fingerprints_are_deterministic(self) -> None:
        item = source("https://drive.google.com/file/d/FILE_ID/view")
        first = discover_media_source(item)
        second = discover_media_source(item)
        self.assertEqual(first.reference_fingerprint, second.reference_fingerprint)
        self.assertEqual(
            first.resource_id_fingerprint, second.resource_id_fingerprint
        )

    def test_37_redacted_reference_status(self) -> None:
        result = discover_media_source(source(REDACTED_REFERENCE))
        self.assertEqual(result.discovery_status, "redacted_reference")

    def test_38_redacted_provider_is_unknown(self) -> None:
        result = discover_media_source(source(REDACTED_REFERENCE))
        self.assertEqual(result.provider, "unknown")
        self.assertEqual(result.resource_kind, "unknown")

    def test_39_redacted_reference_is_not_download_ready(self) -> None:
        result = discover_media_source(source(REDACTED_REFERENCE))
        self.assertIs(result.download_ready, False)
        self.assertFalse(result.blocking_issues)

    def test_40_missing_reference(self) -> None:
        result = discover_media_source(source(None))
        self.assertEqual(result.discovery_status, "missing_reference")
        self.assertIn("missing_reference", result.blocking_issues)

    def test_41_malformed_url(self) -> None:
        result = discover_media_source(source("not a valid URL"))
        self.assertEqual(result.discovery_status, "invalid_reference")

    def test_42_provider_api_hint(self) -> None:
        google = discover_media_source(
            source("https://drive.google.com/drive/folders/ID")
        )
        direct = discover_media_source(source("https://example.test/a.jpg"))
        self.assertTrue(google.requires_provider_api)
        self.assertFalse(direct.requires_provider_api)

    def test_43_http_probe_hint(self) -> None:
        direct = discover_media_source(source("https://example.test/a.jpg"))
        dropbox = discover_media_source(
            source("https://dropbox.com/scl/fi/ID/a.jpg")
        )
        self.assertTrue(direct.requires_http_probe)
        self.assertFalse(dropbox.requires_http_probe)

    def test_44_verified_sku_is_retained_for_audit(self) -> None:
        sku = sku_result()
        result = discover_media_source(
            source("https://example.test/a.jpg"), sku_result=sku
        )
        self.assertEqual(result.sku, sku.sku)

    def test_45_sku_does_not_affect_classification(self) -> None:
        item = source("https://dropbox.com/scl/fo/ID/folder")
        first = discover_media_source(item, sku_result=sku_result("CLM-ULTRA-A"))
        second = discover_media_source(item, sku_result=sku_result("CLM-PRO-B"))
        self.assertEqual(first.provider, second.provider)
        self.assertEqual(first.resource_kind, second.resource_kind)
        self.assertEqual(first.reference_fingerprint, second.reference_fingerprint)

    def test_46_inputs_are_immutable(self) -> None:
        item = source("https://example.test/a.jpg")
        sku = sku_result()
        before_item = copy.deepcopy(item)
        before_sku = copy.deepcopy(sku)
        discover_media_source(item, sku_result=sku)
        self.assertEqual(item, before_item)
        self.assertEqual(sku, before_sku)

    def test_47_batch_summary_total(self) -> None:
        batch = discover_media_sources(
            [
                source("https://example.test/a.jpg", coordinate="I10"),
                source(REDACTED_REFERENCE, coordinate="I20"),
            ]
        )
        self.assertEqual(batch.summary.total_sources, 2)

    def test_48_provider_summary(self) -> None:
        batch = discover_media_sources(
            [
                source("https://drive.google.com/drive/folders/ID", coordinate="I10"),
                source("https://dropbox.com/scl/fi/ID/a", coordinate="I20"),
                source("https://1drv.ms/u/s!x", coordinate="I30"),
                source("https://tenant.sharepoint.com/a", coordinate="I40"),
                source("https://example.test/a", coordinate="I50"),
            ]
        )
        summary = batch.summary
        self.assertEqual(summary.google_drive_sources, 1)
        self.assertEqual(summary.dropbox_sources, 1)
        self.assertEqual(summary.onedrive_sources, 1)
        self.assertEqual(summary.sharepoint_sources, 1)
        self.assertEqual(summary.direct_web_sources, 1)

    def test_49_resource_kind_summary(self) -> None:
        batch = discover_media_sources(
            [
                source("https://drive.google.com/drive/folders/ID", coordinate="I10"),
                source("https://dropbox.com/scl/fi/ID/a", coordinate="I20"),
                source("https://example.test/a.jpg", coordinate="I30"),
                source("https://example.test/a.zip", coordinate="I40"),
            ]
        )
        self.assertEqual(batch.summary.folder_candidates, 1)
        self.assertEqual(batch.summary.file_candidates, 1)
        self.assertEqual(batch.summary.direct_image_candidates, 1)
        self.assertEqual(batch.summary.archive_candidates, 1)

    def test_50_blocked_summary(self) -> None:
        batch = discover_media_sources(
            [
                source("http://example.test/a", coordinate="I10"),
                source("ftp://example.test/a", coordinate="I20"),
                source("https://user:pass@example.test/a", coordinate="I30"),
            ]
        )
        self.assertEqual(batch.summary.insecure_sources, 1)
        self.assertEqual(batch.summary.unsupported_scheme_sources, 1)
        self.assertEqual(batch.summary.credential_blocked_sources, 1)

    def test_51_batch_order_is_deterministic(self) -> None:
        first_source = source("https://example.test/a.jpg", coordinate="I10")
        second_source = source("https://example.test/b.jpg", coordinate="I20")
        first = discover_media_sources([second_source, first_source]).to_report_dict()
        second = discover_media_sources([first_source, second_source]).to_report_dict()
        self.assertEqual(first, second)

    def test_52_no_http_calls(self) -> None:
        with patch.object(
            ReadOnlyHttpClient,
            "get",
            side_effect=AssertionError("HTTP forbidden"),
        ):
            result = discover_media_source(source("https://example.test/a.jpg"))
        self.assertEqual(result.discovery_status, "classified")

    def test_53_no_google_api(self) -> None:
        with patch.object(
            OfficialGoogleClientFactory,
            "create",
            side_effect=AssertionError("Google API forbidden"),
        ):
            result = discover_media_source(
                source("https://drive.google.com/drive/folders/ID")
            )
        self.assertEqual(result.provider, "google_drive")

    def test_54_no_dropbox_or_onedrive_api_import(self) -> None:
        with patch("builtins.__import__", wraps=__import__) as importer:
            discover_media_source(source("https://dropbox.com/scl/fo/ID/a"))
            discover_media_source(source("https://1drv.ms/u/s!x"))
        imported_names = [call.args[0] for call in importer.call_args_list]
        self.assertFalse(any(name.startswith("dropbox") for name in imported_names))
        self.assertFalse(any(name.startswith("onedrive") for name in imported_names))

    def test_55_no_wordpress_api(self) -> None:
        with patch.object(
            ReadOnlyHttpClient,
            "head",
            side_effect=AssertionError("WordPress API forbidden"),
        ):
            result = discover_media_source(source("https://example.test/a.jpg"))
        self.assertEqual(result.discovery_status, "classified")

    def test_56_no_woo_api(self) -> None:
        with patch.object(
            StdlibWooCategoryTransport,
            "get_categories",
            side_effect=AssertionError("Woo API forbidden"),
        ):
            result = discover_media_source(source("https://example.test/a.jpg"))
        self.assertEqual(result.discovery_status, "classified")

    def test_57_network_requests_are_zero(self) -> None:
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network forbidden"),
        ), patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("network forbidden"),
        ):
            batch = discover_media_sources([source("https://example.test/a.jpg")])
        self.assertEqual(batch.network_requests_performed, 0)

    def test_58_external_writes_are_zero(self) -> None:
        with patch.object(
            Path,
            "write_text",
            side_effect=AssertionError("write forbidden"),
        ):
            batch = discover_media_sources([source("https://example.test/a.jpg")])
        self.assertEqual(batch.write_requests_performed, 0)

    def test_59_mapping_result_input_is_supported_and_immutable(self) -> None:
        item = mapping_result(safe_reference="https://example.test/a.webp")
        parent = ProductMediaMappingResult(
            status="mapped",
            product_identity=ProductIdentitySnapshot(
                series="ultra",
                model="VICA",
                raw_model="VICA",
                raw_series_title="CLM Ultra",
            ),
            series="ultra",
            product_source=ProductSourceRange(10, 20),
            media_sources=(item,),
            warnings=(),
            blocking_issues=(),
        )
        before = copy.deepcopy(parent)
        result = discover_media_source(parent.media_sources[0])
        self.assertEqual(result.resource_kind, "direct_image_candidate")
        self.assertEqual(parent, before)

    def test_60_no_image_download(self) -> None:
        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=AssertionError("download forbidden"),
        ):
            result = discover_media_source(source("https://example.test/a.jpg"))
        self.assertIs(result.download_ready, False)

    def test_61_malformed_port_is_rejected(self) -> None:
        result = discover_media_source(source("https://example.test:bad/a.jpg"))
        self.assertEqual(result.discovery_status, "invalid_reference")

    def test_62_report_contains_no_full_url(self) -> None:
        result = discover_media_source(
            source("https://cdn.example.test/private/a.jpg?token=hidden")
        )
        self.assertNotIn("://", json.dumps(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
