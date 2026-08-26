from __future__ import annotations

import copy
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.config import (  # noqa: E402
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_DRIVE_READONLY_SCOPE,
    GoogleSettings,
    load_google_drive_metadata_config,
)
from sync_worker.google_api import (  # noqa: E402
    DRIVE_FOLDER_MANIFEST_FIELDS,
    GoogleDriveMetadataGateway,
    GoogleOperationBlocked,
    OfficialGoogleClientFactory,
)
from sync_worker.google_drive_folder_manifest import (  # noqa: E402
    FOLDER_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    DriveMetadataScopeUnavailable,
    SecureGoogleDriveFolderHandle,
    build_drive_folder_manifests,
    build_drive_folder_manifests_with_gateway,
    create_secure_google_drive_folder_handle,
    fingerprint_drive_id,
    is_valid_drive_id,
)
from sync_worker.image_mapping import (  # noqa: E402
    ProductSourceRange,
    SupplierMediaSourceReference,
)
from sync_worker.media_source_discovery import (  # noqa: E402
    MediaSourceDiscoveryResult,
    discover_media_source,
)


FOLDER_ID = "FOLDER_ID_PRIVATE_123"
FILE_ID = "FILE_ID_PRIVATE_456"
SKU = "CLM-ULTRA-VICA"
MD5 = "0123456789abcdef0123456789abcdef"


def discovery(
    *,
    folder_id: str = FOLDER_ID,
    resource_key: str | None = None,
    provider: str = "google_drive",
    resource_kind: str = "folder",
) -> MediaSourceDiscoveryResult:
    return MediaSourceDiscoveryResult(
        discovery_status="classified",
        provider=provider,  # type: ignore[arg-type]
        resource_kind=resource_kind,  # type: ignore[arg-type]
        scheme="https",
        safe_host="drive.google.com",
        safe_path_hint="/drive/folders/[RESOURCE_ID]",
        reference_coordinate="I16",
        reference_fingerprint="a" * 64,
        resource_id_fingerprint=fingerprint_drive_id(folder_id),
        requires_provider_api=True,
        requires_http_probe=False,
        download_ready=False,
        sku=SKU,
        warnings=(),
        blocking_issues=(),
        provider_resource_id=folder_id,
        resource_key=resource_key,
    )


def handle(
    *,
    folder_id: str = FOLDER_ID,
    fingerprint: str | None = None,
    start_row: int = 478,
    end_row: int = 488,
) -> SecureGoogleDriveFolderHandle:
    return SecureGoogleDriveFolderHandle(
        provider="google_drive",
        resource_kind="folder",
        raw_folder_id=folder_id,
        folder_id_fingerprint=fingerprint or fingerprint_drive_id(folder_id),
        sku=SKU,
        product_source=ProductSourceRange(start_row, end_row),
    )


def drive_file(
    name: str = "photo.jpg",
    *,
    file_id: str = FILE_ID,
    mime_type: str = "image/jpeg",
    size: object = "123",
    modified_time: object = "2026-01-02T03:04:05.000Z",
    md5: object = MD5,
    image_metadata: object = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "size": size,
        "modifiedTime": modified_time,
        "md5Checksum": md5,
    }
    if image_metadata is not None:
        payload["imageMediaMetadata"] = image_metadata
    return payload


class FakeHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str = "safe mock failure") -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeGateway:
    def __init__(self, responses: dict[object, object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.counters = SimpleNamespace(read_requests_performed=0)

    def list_folder_children(
        self,
        folder_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> object:
        self.counters.read_requests_performed += 1
        self.calls.append(
            {
                "folder_id": folder_id,
                "page_token": page_token,
                "page_size": page_size,
            }
        )
        response = self.responses.get(page_token)
        if isinstance(response, list):
            if not response:
                raise AssertionError("unexpected extra Drive page request")
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeFactory:
    def __init__(self, drive_client: object = None) -> None:
        self.drive_client = drive_client or MagicMock(name="drive_client")
        self.calls = 0

    def create_drive_metadata(self, settings: object) -> object:
        self.calls += 1
        return self.drive_client


def built(
    files: list[object] | None = None,
    *,
    response: object | None = None,
    manifest_handle: SecureGoogleDriveFolderHandle | None = None,
    **limits: int,
):
    gateway = FakeGateway(
        {None: response if response is not None else {"files": files or []}}
    )
    result = build_drive_folder_manifests_with_gateway(
        (manifest_handle or handle(),),
        gateway,
        **limits,
    )
    return result, gateway


class GoogleDriveFolderManifestTests(unittest.TestCase):
    def test_01_secure_folder_handle_from_discovery(self) -> None:
        result = create_secure_google_drive_folder_handle(
            discovery(), ProductSourceRange(478, 488)
        )
        self.assertEqual(result.provider, "google_drive")
        self.assertEqual(result.resource_kind, "folder")
        self.assertEqual(result.sku, SKU)

    def test_02_raw_folder_id_is_hidden_from_repr(self) -> None:
        self.assertNotIn(FOLDER_ID, repr(handle()))

    def test_03_raw_folder_id_is_not_serialized(self) -> None:
        serialized = json.dumps(handle().to_safe_dict())
        self.assertNotIn(FOLDER_ID, serialized)
        self.assertIn(fingerprint_drive_id(FOLDER_ID), serialized)

    def test_04_folder_id_validation_rejects_injection(self) -> None:
        for value in ("bad id", "bad'id", "bad\\id", "bad\nid", "x) or (1=1"):
            with self.subTest(value=value):
                self.assertFalse(is_valid_drive_id(value))
        self.assertTrue(is_valid_drive_id("Abc_123-xyz"))

    def test_05_metadata_scope_is_accepted_and_factory_reused(self) -> None:
        settings = SimpleNamespace(
            drive_scope=GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        )
        factory = FakeFactory()
        result = build_drive_folder_manifests((), settings, factory)
        self.assertEqual(factory.calls, 1)
        self.assertEqual(result.summary.total_folders, 0)

    def test_06_drive_readonly_scope_is_rejected_before_factory(self) -> None:
        settings = SimpleNamespace(drive_scope=GOOGLE_DRIVE_READONLY_SCOPE)
        factory = FakeFactory()
        with self.assertRaisesRegex(
            DriveMetadataScopeUnavailable,
            "drive_metadata_scope_unavailable",
        ):
            build_drive_folder_manifests((), settings, factory)
        self.assertEqual(factory.calls, 0)

    def test_07_full_drive_scope_is_rejected_before_factory(self) -> None:
        settings = SimpleNamespace(
            drive_scope="https://www.googleapis.com/auth/drive"
        )
        factory = FakeFactory()
        with self.assertRaises(DriveMetadataScopeUnavailable):
            build_drive_folder_manifests((), settings, factory)
        self.assertEqual(factory.calls, 0)

    def test_08_gateway_calls_only_files_list(self) -> None:
        drive = MagicMock()
        request = drive.files.return_value.list.return_value
        request.execute.return_value = {"files": []}
        GoogleDriveMetadataGateway(drive).list_folder_children(FOLDER_ID)
        drive.files.return_value.list.assert_called_once()
        drive.files.return_value.get.assert_not_called()

    def test_09_gateway_query_uses_exact_parent(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        GoogleDriveMetadataGateway(drive).list_folder_children(FOLDER_ID)
        query = drive.files.return_value.list.call_args.kwargs["q"]
        self.assertIn(f"'{FOLDER_ID}' in parents", query)

    def test_10_gateway_query_excludes_trashed(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        GoogleDriveMetadataGateway(drive).list_folder_children(FOLDER_ID)
        query = drive.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("trashed = false", query)

    def test_11_gateway_uses_metadata_fields_allowlist(self) -> None:
        self.assertIn(
            "files(id,name,mimeType,size,modifiedTime,md5Checksum",
            DRIVE_FOLDER_MANIFEST_FIELDS,
        )
        self.assertIn(
            "imageMediaMetadata(width,height,rotation)",
            DRIVE_FOLDER_MANIFEST_FIELDS,
        )

    def test_12_fields_exclude_web_content_link(self) -> None:
        self.assertNotIn("webContentLink", DRIVE_FOLDER_MANIFEST_FIELDS)
        self.assertNotIn("webViewLink", DRIVE_FOLDER_MANIFEST_FIELDS)

    def test_13_fields_exclude_thumbnail_link(self) -> None:
        self.assertNotIn("thumbnailLink", DRIVE_FOLDER_MANIFEST_FIELDS)
        self.assertNotIn("permissions", DRIVE_FOLDER_MANIFEST_FIELDS)

    def test_14_one_page_manifest(self) -> None:
        result, gateway = built([drive_file()])
        self.assertEqual(result.manifests[0].status, "listed")
        self.assertEqual(result.summary.pages_read, 1)
        self.assertEqual(len(gateway.calls), 1)

    def test_15_multiple_pages_are_read(self) -> None:
        gateway = FakeGateway(
            {
                None: {"files": [drive_file("a.jpg")], "nextPageToken": "p2"},
                "p2": {"files": [drive_file("b.jpg", file_id="B_ID")]},
            }
        )
        result = build_drive_folder_manifests_with_gateway((handle(),), gateway)
        self.assertEqual(result.summary.pages_read, 2)
        self.assertEqual(result.summary.total_items, 2)

    def test_16_page_token_is_passed_to_next_request(self) -> None:
        gateway = FakeGateway(
            {
                None: {"files": [], "nextPageToken": "NEXT"},
                "NEXT": {"files": []},
            }
        )
        build_drive_folder_manifests_with_gateway((handle(),), gateway)
        self.assertEqual(gateway.calls[1]["page_token"], "NEXT")

    def test_17_duplicate_page_token_is_blocked(self) -> None:
        gateway = FakeGateway(
            {
                None: {"files": [], "nextPageToken": "SAME"},
                "SAME": {"files": [], "nextPageToken": "SAME"},
            }
        )
        result = build_drive_folder_manifests_with_gateway((handle(),), gateway)
        manifest = result.manifests[0]
        self.assertEqual(manifest.status, "limit_exceeded")
        self.assertIn("duplicate_drive_page_token", manifest.blocking_issues)

    def test_18_max_pages_is_fail_closed(self) -> None:
        result, gateway = built(
            response={"files": [], "nextPageToken": "MORE"},
            max_pages=1,
        )
        self.assertEqual(result.manifests[0].status, "limit_exceeded")
        self.assertEqual(len(gateway.calls), 1)

    def test_19_max_items_is_fail_closed(self) -> None:
        result, _ = built(
            [drive_file("a.jpg"), drive_file("b.jpg", file_id="B_ID")],
            max_items_per_folder=1,
        )
        self.assertEqual(result.manifests[0].status, "limit_exceeded")
        self.assertEqual(len(result.manifests[0].items), 1)

    def test_20_jpeg_is_image_candidate(self) -> None:
        result, _ = built([drive_file(mime_type="image/jpeg")])
        self.assertTrue(result.manifests[0].items[0].image_candidate)

    def test_21_png_is_image_candidate(self) -> None:
        result, _ = built([drive_file(mime_type="image/png")])
        self.assertEqual(result.summary.image_candidates, 1)

    def test_22_webp_is_image_candidate(self) -> None:
        result, _ = built([drive_file(mime_type="image/webp")])
        self.assertEqual(result.manifests[0].items[0].item_kind, "image_candidate")

    def test_23_avif_is_image_candidate(self) -> None:
        result, _ = built([drive_file(mime_type="image/avif")])
        self.assertEqual(
            result.manifests[0].items[0].image_candidate_status,
            "drive_metadata_image_candidate",
        )

    def test_24_extension_alone_does_not_make_image_candidate(self) -> None:
        result, _ = built([drive_file("photo.jpg", mime_type="text/plain")])
        self.assertFalse(result.manifests[0].items[0].image_candidate)

    def test_25_image_width_is_preserved(self) -> None:
        result, _ = built([drive_file(image_metadata={"width": 1200})])
        self.assertEqual(result.manifests[0].items[0].image_width, 1200)

    def test_26_image_height_is_preserved(self) -> None:
        result, _ = built([drive_file(image_metadata={"height": 800})])
        self.assertEqual(result.manifests[0].items[0].image_height, 800)

    def test_27_missing_image_metadata_remains_null(self) -> None:
        item = built([drive_file()])[0].manifests[0].items[0]
        self.assertIsNone(item.image_width)
        self.assertIsNone(item.image_height)

    def test_28_nested_folder_is_recorded(self) -> None:
        result, _ = built([drive_file("nested", mime_type=FOLDER_MIME_TYPE)])
        item = result.manifests[0].items[0]
        self.assertEqual(item.item_kind, "nested_folder")
        self.assertIn("nested_folder_present", result.manifests[0].warnings)

    def test_29_nested_folder_is_not_followed(self) -> None:
        result, gateway = built(
            [drive_file("nested", mime_type=FOLDER_MIME_TYPE)]
        )
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(result.summary.nested_folders, 1)

    def test_30_shortcut_is_recorded(self) -> None:
        result, _ = built([drive_file("link", mime_type=SHORTCUT_MIME_TYPE)])
        self.assertEqual(result.manifests[0].items[0].item_kind, "shortcut")

    def test_31_shortcut_is_not_followed(self) -> None:
        result, gateway = built(
            [drive_file("link", mime_type=SHORTCUT_MIME_TYPE)]
        )
        self.assertEqual(len(gateway.calls), 1)
        self.assertIn("shortcut_not_followed", result.manifests[0].warnings)

    def test_32_google_workspace_file_is_classified(self) -> None:
        result, _ = built(
            [drive_file("doc", mime_type="application/vnd.google-apps.document")]
        )
        self.assertEqual(
            result.manifests[0].items[0].item_kind,
            "google_workspace_file",
        )

    def test_33_other_file_is_classified(self) -> None:
        result, _ = built([drive_file("notes.txt", mime_type="text/plain")])
        self.assertEqual(result.manifests[0].items[0].item_kind, "other_file")

    def test_34_path_traversal_characters_are_sanitized(self) -> None:
        result, _ = built([drive_file("../unsafe\\name.jpg")])
        name = result.manifests[0].items[0].safe_name
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_35_newline_is_sanitized(self) -> None:
        result, _ = built([drive_file("photo\nprivate.jpg")])
        self.assertNotIn("\n", result.manifests[0].items[0].safe_name)

    def test_36_control_characters_are_sanitized(self) -> None:
        result, _ = built([drive_file("photo\x00\x07.jpg")])
        name = result.manifests[0].items[0].safe_name
        self.assertNotIn("\x00", name)
        self.assertNotIn("\x07", name)

    def test_37_file_id_is_fingerprinted(self) -> None:
        result, _ = built([drive_file()])
        self.assertEqual(
            result.manifests[0].items[0].file_id_fingerprint,
            fingerprint_drive_id(FILE_ID),
        )

    def test_38_raw_file_id_is_hidden_from_report(self) -> None:
        result, _ = built([drive_file()])
        self.assertNotIn(FILE_ID, json.dumps(result.to_report_dict()))

    def test_39_file_fingerprint_is_deterministic(self) -> None:
        self.assertEqual(fingerprint_drive_id(FILE_ID), fingerprint_drive_id(FILE_ID))
        self.assertNotEqual(
            fingerprint_drive_id(FILE_ID),
            fingerprint_drive_id("OTHER"),
        )

    def test_40_md5_is_retained_when_provided(self) -> None:
        result, _ = built([drive_file(md5=MD5.upper())])
        self.assertEqual(result.manifests[0].items[0].md5_checksum, MD5)

    def test_41_missing_md5_is_allowed(self) -> None:
        result, _ = built([drive_file(md5=None)])
        self.assertIsNone(result.manifests[0].items[0].md5_checksum)

    def test_42_duplicate_name_candidates_are_marked(self) -> None:
        result, _ = built(
            [drive_file("Same.jpg"), drive_file("same.jpg", file_id="B_ID")]
        )
        self.assertEqual(result.summary.duplicate_name_candidates, 2)

    def test_43_duplicate_md5_candidates_are_marked(self) -> None:
        result, _ = built(
            [drive_file("a.jpg"), drive_file("b.jpg", file_id="B_ID")]
        )
        self.assertEqual(result.summary.duplicate_content_candidates, 2)

    def test_44_duplicate_candidates_are_not_auto_deduplicated(self) -> None:
        result, _ = built(
            [drive_file("same.jpg"), drive_file("same.jpg", file_id="B_ID")]
        )
        self.assertEqual(len(result.manifests[0].items), 2)

    def test_45_items_are_sorted_deterministically(self) -> None:
        result, _ = built(
            [drive_file("z.jpg"), drive_file("A.jpg", file_id="A_ID")]
        )
        self.assertEqual(
            [item.safe_name for item in result.manifests[0].items],
            ["A.jpg", "z.jpg"],
        )

    def test_46_api_response_order_is_irrelevant(self) -> None:
        first = built(
            [drive_file("b.jpg"), drive_file("a.jpg", file_id="A_ID")]
        )[0]
        second = built(
            [drive_file("a.jpg", file_id="A_ID"), drive_file("b.jpg")]
        )[0]
        self.assertEqual(first, second)

    def test_47_empty_folder_status(self) -> None:
        result, _ = built([])
        self.assertEqual(result.manifests[0].status, "empty_folder")
        self.assertEqual(result.summary.empty_folders, 1)

    def test_48_401_returns_safe_authentication_error(self) -> None:
        result, gateway = built(response=FakeHttpError(401, FOLDER_ID))
        manifest = result.manifests[0]
        self.assertEqual(manifest.status, "access_denied")
        self.assertIn("drive_authentication_failed", manifest.blocking_issues)
        self.assertEqual(len(gateway.calls), 1)

    def test_49_403_returns_safe_access_error(self) -> None:
        result, _ = built(response=FakeHttpError(403, FOLDER_ID))
        self.assertIn(
            "drive_folder_access_denied",
            result.manifests[0].blocking_issues,
        )

    def test_50_404_returns_missing_or_inaccessible(self) -> None:
        result, _ = built(response=FakeHttpError(404, FOLDER_ID))
        self.assertEqual(result.manifests[0].status, "missing_or_inaccessible")

    def test_51_429_is_retried_then_succeeds(self) -> None:
        gateway = FakeGateway(
            {None: [FakeHttpError(429), {"files": [drive_file()]}]}
        )
        result = build_drive_folder_manifests_with_gateway((handle(),), gateway)
        self.assertEqual(result.manifests[0].status, "listed")
        self.assertEqual(len(gateway.calls), 2)

    def test_52_temporary_5xx_is_retried(self) -> None:
        gateway = FakeGateway(
            {None: [FakeHttpError(503), {"files": []}]}
        )
        result = build_drive_folder_manifests_with_gateway((handle(),), gateway)
        self.assertEqual(result.manifests[0].status, "empty_folder")
        self.assertEqual(len(gateway.calls), 2)

    def test_53_retry_limit_is_three_attempts(self) -> None:
        gateway = FakeGateway(
            {None: [FakeHttpError(500), FakeHttpError(502), FakeHttpError(503)]}
        )
        result = build_drive_folder_manifests_with_gateway((handle(),), gateway)
        self.assertEqual(result.manifests[0].status, "read_failed")
        self.assertEqual(len(gateway.calls), 3)

    def test_54_raw_id_is_absent_from_safe_error(self) -> None:
        result, _ = built(response=FakeHttpError(403, f"folder={FOLDER_ID}"))
        self.assertNotIn(FOLDER_ID, json.dumps(result.to_report_dict()))

    def test_55_credentials_are_absent_from_report(self) -> None:
        result, _ = built([drive_file("token=private-secret")])
        serialized = json.dumps(result.to_report_dict()).lower()
        self.assertNotIn("private-secret", serialized)
        self.assertNotIn("authorization", serialized)

    def test_56_gateway_never_sets_alt_media(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        GoogleDriveMetadataGateway(drive).list_folder_children(FOLDER_ID)
        kwargs = drive.files.return_value.list.call_args.kwargs
        self.assertNotIn("alt", kwargs)
        self.assertNotIn("media", json.dumps(kwargs))

    def test_57_gateway_has_no_download_methods(self) -> None:
        gateway = GoogleDriveMetadataGateway(MagicMock())
        for name in ("download", "get_media", "export", "export_media"):
            self.assertFalse(hasattr(gateway, name))

    def test_58_nested_folder_does_not_trigger_recursive_listing(self) -> None:
        _, gateway = built(
            [drive_file("nested", mime_type=FOLDER_MIME_TYPE)]
        )
        self.assertEqual(
            [call["folder_id"] for call in gateway.calls],
            [FOLDER_ID],
        )

    def test_59_manifest_performs_no_http_probe(self) -> None:
        with patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("HTTP probe forbidden"),
        ):
            result, _ = built([drive_file()])
        self.assertEqual(result.summary.image_candidates, 1)

    def test_60_development_network_is_mock_only(self) -> None:
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network forbidden"),
        ):
            result, gateway = built([drive_file()])
        self.assertEqual(result.summary.drive_read_requests_performed, 1)
        self.assertIsInstance(gateway, FakeGateway)

    def test_61_write_and_download_counts_are_zero(self) -> None:
        result, _ = built([drive_file()])
        self.assertEqual(result.write_requests_performed, 0)
        self.assertEqual(result.download_requests_performed, 0)
        self.assertEqual(result.summary.write_requests_performed, 0)

    def test_62_inputs_are_immutable(self) -> None:
        source = discovery()
        source_before = copy.deepcopy(source)
        secure_handle = create_secure_google_drive_folder_handle(
            source, ProductSourceRange(478, 488)
        )
        handle_before = copy.deepcopy(secure_handle)
        response = {"files": [drive_file()]}
        response_before = copy.deepcopy(response)
        built(response=response, manifest_handle=secure_handle)
        self.assertEqual(source, source_before)
        self.assertEqual(secure_handle, handle_before)
        self.assertEqual(response, response_before)

    def test_63_manifest_is_stable_across_runs(self) -> None:
        first = built([drive_file("b.jpg"), drive_file("a.jpg", file_id="A")])[0]
        second = built([drive_file("b.jpg"), drive_file("a.jpg", file_id="A")])[0]
        self.assertEqual(first, second)

    def test_64_resource_key_is_memory_only(self) -> None:
        secure_handle = create_secure_google_drive_folder_handle(
            discovery(resource_key="RESOURCE_KEY_PRIVATE"),
            ProductSourceRange(478, 488),
        )
        self.assertNotIn("RESOURCE_KEY_PRIVATE", repr(secure_handle))
        self.assertNotIn(
            "RESOURCE_KEY_PRIVATE",
            json.dumps(secure_handle.to_safe_dict()),
        )

    def test_65_summary_contains_required_fields(self) -> None:
        summary = built([drive_file()])[0].summary.to_dict()
        for field in (
            "total_folders",
            "folders_listed",
            "empty_folders",
            "folders_access_denied",
            "folders_missing_or_inaccessible",
            "folders_limit_exceeded",
            "total_items",
            "image_candidates",
            "nested_folders",
            "shortcuts",
            "google_workspace_files",
            "other_files",
            "duplicate_name_candidates",
            "duplicate_content_candidates",
            "pages_read",
            "drive_read_requests_performed",
            "write_requests_performed",
        ):
            self.assertIn(field, summary)

    def test_66_gateway_exposes_no_write_operations(self) -> None:
        gateway = GoogleDriveMetadataGateway(MagicMock())
        for name in ("create", "update", "delete", "copy", "move", "upload"):
            self.assertFalse(hasattr(gateway, name))

    def test_67_official_factory_builds_drive_with_one_metadata_scope(self) -> None:
        settings = MagicMock(spec=GoogleSettings)
        settings.drive_scope = GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        settings.validate_drive_metadata = MagicMock()
        authorized_http = object()
        drive_client = object()
        build = MagicMock(return_value=drive_client)
        googleapiclient = ModuleType("googleapiclient")
        googleapiclient.__path__ = []  # type: ignore[attr-defined]
        discovery_module = ModuleType("googleapiclient.discovery")
        discovery_module.build = build  # type: ignore[attr-defined]
        googleapiclient.discovery = discovery_module  # type: ignore[attr-defined]
        factory = OfficialGoogleClientFactory()
        with patch.object(
            factory,
            "_create_authorized_http",
            return_value=authorized_http,
        ) as authorize, patch.dict(
            sys.modules,
            {
                "googleapiclient": googleapiclient,
                "googleapiclient.discovery": discovery_module,
            },
        ):
            created = factory.create_drive_metadata(settings)
        authorize.assert_called_once()
        self.assertEqual(
            authorize.call_args.args[1],
            (GOOGLE_DRIVE_METADATA_READONLY_SCOPE,),
        )
        build.assert_called_once_with(
            "drive",
            "v3",
            http=authorized_http,
            cache_discovery=False,
        )
        self.assertIs(created, drive_client)

    def test_68_metadata_config_loader_reuses_proxy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_path = Path(directory) / "mock.json"
            credential_path.write_text("{}", encoding="utf-8")
            settings = load_google_drive_metadata_config(
                {
                    "GOOGLE_SERVICE_ACCOUNT_FILE": str(credential_path),
                    "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
                    "GOOGLE_PROXY_MODE": "socks5",
                    "GOOGLE_PROXY_HOST": "127.0.0.1",
                    "GOOGLE_PROXY_PORT": "26001",
                    "GOOGLE_PROXY_RDNS": "true",
                }
            )
        self.assertEqual(settings.google_proxy_mode, "socks5")
        self.assertEqual(settings.google_proxy_port, 26001)

    def test_69_no_main_image_selection_is_emitted(self) -> None:
        report = built(
            [
                drive_file("main.jpg"),
                drive_file("largest.jpg", file_id="LARGE", size="999999"),
            ]
        )[0].to_report_dict()
        serialized = json.dumps(report)
        self.assertNotIn('"main_image"', serialized)
        self.assertNotIn('"gallery_position"', serialized)

    def test_70_gateway_rejects_folder_id_before_files_list(self) -> None:
        drive = MagicMock()
        with self.assertRaisesRegex(
            GoogleOperationBlocked,
            "folder identifier is invalid",
        ):
            GoogleDriveMetadataGateway(drive).list_folder_children("bad'id")
        drive.files.assert_not_called()

    def test_71_provider_ids_in_file_names_are_redacted(self) -> None:
        result, _ = built(
            [
                drive_file(
                    f"photo-{FILE_ID}-{FOLDER_ID}.jpg",
                    file_id=FILE_ID,
                )
            ]
        )
        serialized = json.dumps(result.to_report_dict())
        self.assertNotIn(FILE_ID, serialized)
        self.assertNotIn(FOLDER_ID, serialized)

    def test_72_invalid_handle_stops_before_client_creation(self) -> None:
        unsafe_handle = handle(folder_id="bad'id")
        settings = SimpleNamespace(
            drive_scope=GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        )
        factory = FakeFactory()
        result = build_drive_folder_manifests(
            (unsafe_handle,),
            settings,
            factory,
        )
        self.assertEqual(factory.calls, 0)
        self.assertEqual(result.manifests[0].status, "invalid_folder_handle")

    def test_73_discovery_retains_resource_key_in_memory(self) -> None:
        resource_key = "RESOURCE_KEY_PRIVATE"
        source = SupplierMediaSourceReference(
            source_coordinate="I16",
            source_row=16,
            marker_coordinate="B15",
            marker_text="Photo download link",
            raw_reference=(
                f"https://drive.google.com/drive/folders/{FOLDER_ID}"
                f"?resourcekey={resource_key}"
            ),
            safe_reference="[REDACTED]",
            reference_status="available",
            reference_fingerprint="a" * 64,
            product_source_candidate=ProductSourceRange(478, 488),
        )
        result = discover_media_source(source)
        self.assertEqual(result.provider_resource_id, FOLDER_ID)
        self.assertEqual(result.resource_key, resource_key)

    def test_74_discovery_raw_handle_fields_are_not_reported(self) -> None:
        result = discovery(resource_key="RESOURCE_KEY_PRIVATE")
        serialized = json.dumps(result.to_dict())
        self.assertNotIn(FOLDER_ID, serialized)
        self.assertNotIn("RESOURCE_KEY_PRIVATE", serialized)
        self.assertNotIn(FOLDER_ID, repr(result))


if __name__ == "__main__":
    unittest.main()
