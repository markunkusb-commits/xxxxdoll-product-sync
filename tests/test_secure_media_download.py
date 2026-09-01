from __future__ import annotations

import hashlib
import inspect
import json
import pickle
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import image_selection_policy as selection_core
from sync_worker import secure_media_download as download_core
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker.config import (
    ConfigError,
    GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GoogleSettings,
    load_google_drive_content_config,
)
from sync_worker.google_api import (
    GoogleDriveContentDownloadError,
    GoogleDriveContentDownloadReceipt,
    GoogleDriveContentGateway,
    GoogleDriveContentSinkError,
    OfficialGoogleClientFactory,
    _safe_content_download_error,
)
from sync_worker.image_mapping import ProductSourceRange


JPEG = b"\xff\xd8\xff" + b"jpeg-mock-content"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-mock-content"
WEBP = b"RIFF\x10\x00\x00\x00WEBP" + b"webp-mock-content"
REALITY_SOURCE_SIZE = 12_458_951
REALITY_SOURCE_MD5 = "5c53847fc04463312e389b98e8184026"


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def make_handle(
    data=JPEG,
    *,
    mime="image/jpeg",
    sku="MOCK-001",
    position=0,
    raw_id="opaque_file_001",
    name="supplier-photo.jpg",
    expected_data=None,
    expected_size=None,
):
    source = ProductSourceRange(10, 20)
    image_role = (
        selection_core.ImageSelectionRole.PRIMARY
        if position == 0
        else selection_core.ImageSelectionRole.GALLERY
    )
    reason = (
        selection_core.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY
        if position == 0
        else selection_core.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY
    )
    selection = selection_core.ImageSelectionItem(
        sku=sku,
        folder_role=folder_core.FolderRole.STOREFRONT_PHOTOS,
        safe_name=name,
        source_manifest_kind="nested",
        depth=1,
        safe_folder_name="Storefront Photos",
        parent_safe_folder_name=None,
        product_source=source,
        requires_deeper_inventory=False,
        quality_eligible=True,
        selected=True,
        selection_position=position,
        image_role=image_role,
        selection_reason=reason,
    )
    expected = data if expected_data is None else expected_data
    size = len(expected) if expected_size is None else expected_size
    item = root_core.DriveManifestItem(
        safe_name=name,
        mime_type=mime,
        size_bytes=size,
        modified_time="2026-01-01T00:00:00Z",
        md5_checksum=hashlib.md5(expected, usedforsecurity=False).hexdigest(),
        file_id_fingerprint=root_core.fingerprint_drive_id(raw_id),
        item_kind="image_candidate",
        image_candidate=True,
        image_candidate_status="drive_metadata_image_candidate",
        image_width=2000,
        image_height=3000,
        image_rotation=0,
        warnings=(),
        provider_file_id=raw_id,
    )
    manifest = nested_core.GoogleDriveNestedFolderManifest(
        sku=sku,
        product_source=source,
        root_folder_id_fingerprint=root_core.fingerprint_drive_id("root_" + sku),
        nested_folder_id_fingerprint=root_core.fingerprint_drive_id("nested_" + sku),
        safe_folder_name="Storefront Photos",
        depth=1,
        status="listed",
        items=(item,),
        pages_read=1,
    )
    baseline = handle_core.create_selected_media_baseline_identity(selection, manifest)
    return handle_core.create_secure_selected_media_handle(selection, baseline, manifest)


class FakeGateway:
    def __init__(self, data_by_id=None, *, chunksize=5, failures=None, receipt=None):
        self.data_by_id = data_by_id or {"opaque_file_001": JPEG}
        self.chunksize = chunksize
        self.failures = {key: list(value) for key, value in (failures or {}).items()}
        self.receipt = receipt
        self.calls = []
        self.write_sizes = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append((provider_file_id, chunk_size))
        pending = self.failures.get(provider_file_id, [])
        if pending:
            error = pending.pop(0)
            if error is not None:
                raise error
        data = self.data_by_id[provider_file_id]
        for offset in range(0, len(data), self.chunksize):
            chunk = data[offset : offset + self.chunksize]
            self.write_sizes.append(len(chunk))
            sink.write(chunk)
        if self.receipt is not None:
            return self.receipt
        return GoogleDriveContentDownloadReceipt(1, len(data))


def run_one(tmp_path, *, data=JPEG, mime="image/jpeg", gateway_data=None, **kwargs):
    handle = make_handle(data, mime=mime, **kwargs)
    raw_id = handle_core._provider_file_id_for_download(handle)
    gateway = FakeGateway({raw_id: data if gateway_data is None else gateway_data})
    result = download_core.download_secure_media(handle, gateway, workspace_parent=tmp_path)
    return result, gateway


def safe(result):
    return result.to_safe_report_dict()


def test_001_policy_version():
    assert download_core.POLICY_VERSION == "xxxxdoll-secure-media-download-v1"


def test_002_exact_content_scope_constant():
    assert GOOGLE_DRIVE_CONTENT_READONLY_SCOPE == "https://www.googleapis.com/auth/drive.readonly"


def test_003_metadata_scope_unchanged():
    assert GOOGLE_DRIVE_METADATA_READONLY_SCOPE == "https://www.googleapis.com/auth/drive.metadata.readonly"


def test_004_valid_single_handle(tmp_path):
    result, _ = run_one(tmp_path)
    assert result.status == "ok" and len(result.artifacts) == 1
    result.cleanup()


def test_005_valid_tuple(tmp_path):
    value = make_handle()
    result = download_core.download_secure_media((value,), FakeGateway(), workspace_parent=tmp_path)
    assert len(result.artifacts) == 1
    result.cleanup()


@pytest.mark.parametrize("value", [{}, {"safe_name": "x.jpg"}, "opaque_file_001", [make_handle()], 1, None])
def test_006_non_capability_input_rejected(value, tmp_path):
    with pytest.raises(download_core.SecureMediaDownloadError, match="secure_selected_media_handles_required"):
        download_core.download_secure_media(value, FakeGateway(), workspace_parent=tmp_path)


def test_012_forged_handle_rejected(tmp_path):
    forged = object.__new__(handle_core.SecureSelectedMediaHandle)
    with pytest.raises(download_core.SecureMediaDownloadError):
        download_core.download_secure_media(forged, FakeGateway(), workspace_parent=tmp_path)


def test_013_empty_tuple_rejected(tmp_path):
    with pytest.raises(download_core.SecureMediaDownloadError, match="secure_selected_media_handles_required"):
        download_core.download_secure_media((), FakeGateway(), workspace_parent=tmp_path)


def settings(tmp_path, scope):
    credential = tmp_path / "fake.json"
    credential.write_text("{}", encoding="utf-8")
    return GoogleSettings(
        service_account_file=str(credential),
        drive_scope=scope,
        google_proxy_mode="none",
    )


def test_013_content_scope_validation_accepts_exact(tmp_path):
    settings(tmp_path, GOOGLE_DRIVE_CONTENT_READONLY_SCOPE).validate_drive_content_readonly()


@pytest.mark.parametrize("scope", [
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "",
])
def test_014_content_scope_validation_rejects_everything_else(tmp_path, scope):
    with pytest.raises(ConfigError, match="drive_content_readonly_scope_unavailable"):
        settings(tmp_path, scope).validate_drive_content_readonly()


def test_018_content_factory_uses_only_content_scope(tmp_path):
    configured = settings(tmp_path, GOOGLE_DRIVE_CONTENT_READONLY_SCOPE)
    transport = object()
    drive = object()
    package = ModuleType("googleapiclient")
    discovery = ModuleType("googleapiclient.discovery")
    build = Mock(return_value=drive)
    discovery.build = build
    package.discovery = discovery
    with patch.dict(sys.modules, {"googleapiclient": package, "googleapiclient.discovery": discovery}):
        with patch.object(OfficialGoogleClientFactory, "_create_authorized_http", return_value=transport) as auth:
            result = OfficialGoogleClientFactory().create_drive_content_readonly(configured)
    assert result is drive
    assert auth.call_args.args[1] == (GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,)
    build.assert_called_once_with("drive", "v3", http=transport, cache_discovery=False)


def test_019_content_loader_does_not_require_sheets_or_folder_ids(tmp_path):
    credential = tmp_path / "fake.json"
    credential.write_text("{}", encoding="utf-8")
    loaded = load_google_drive_content_config({
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(credential),
        "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
        "GOOGLE_PROXY_MODE": "none",
        "GOOGLE_PROXY_RDNS": "true",
    })
    assert loaded.drive_scope == GOOGLE_DRIVE_CONTENT_READONLY_SCOPE
    assert loaded.sheets_scope == "" and loaded.clm_drive_folder_id == ""


def test_020_metadata_validator_rejects_content_scope(tmp_path):
    with pytest.raises(ConfigError, match="drive_metadata_scope_unavailable"):
        settings(tmp_path, GOOGLE_DRIVE_CONTENT_READONLY_SCOPE).validate_drive_metadata()


@pytest.mark.parametrize("mime,data,extension", [
    ("image/jpeg", JPEG, ".jpg"),
    ("image/png", PNG, ".png"),
    ("image/webp", WEBP, ".webp"),
])
def test_019_allowed_mime_and_signature(tmp_path, mime, data, extension):
    result, _ = run_one(tmp_path, data=data, mime=mime)
    assert result.artifacts[0].source_extension == extension
    assert safe(result)["summary"]["signature_verified"] == 1
    result.cleanup()


@pytest.mark.parametrize("mime", ["image/gif", "image/tiff", "application/octet-stream", "text/plain"])
def test_022_unsupported_mime_blocked_before_gateway(tmp_path, mime):
    value = make_handle(JPEG, mime=mime)
    gateway = FakeGateway()
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert result.status == "blocked" and result.artifacts == () and gateway.calls == []
    assert safe(result)["results"][0]["blocking_issues"] == ["download_source_mime_not_allowed"]


@pytest.mark.parametrize("mime,expected,wrong", [
    ("image/jpeg", JPEG, PNG),
    ("image/png", PNG, JPEG),
    ("image/webp", WEBP, JPEG),
])
def test_026_wrong_signature_blocked(tmp_path, mime, expected, wrong):
    handle = make_handle(wrong, mime=mime)
    raw_id = handle_core._provider_file_id_for_download(handle)
    result = download_core.download_secure_media(handle, FakeGateway({raw_id: wrong}), workspace_parent=tmp_path)
    assert result.status == "blocked" and result.artifacts == ()
    assert safe(result)["summary"]["signature_mismatch"] == 1


def test_029_exact_md5_pass(tmp_path):
    result, _ = run_one(tmp_path)
    assert safe(result)["summary"]["checksum_verified"] == 1
    result.cleanup()


def test_030_md5_mismatch_is_not_retried(tmp_path):
    value = make_handle(JPEG)
    raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway({raw_id: JPEG + b"changed"})
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert safe(result)["summary"]["checksum_mismatch"] == 1
    assert len(gateway.calls) == 1 and result.artifacts == ()


def test_031_exact_size_pass(tmp_path):
    result, _ = run_one(tmp_path)
    assert safe(result)["summary"]["size_verified"] == 1
    result.cleanup()


def test_032_size_mismatch_blocked(tmp_path):
    value = make_handle(JPEG, expected_size=len(JPEG) + 1)
    raw_id = handle_core._provider_file_id_for_download(value)
    result = download_core.download_secure_media(value, FakeGateway({raw_id: JPEG}), workspace_parent=tmp_path)
    assert safe(result)["summary"]["size_mismatch"] == 1 and result.artifacts == ()


def test_033_streams_bounded_chunks(tmp_path):
    data = b"\xff\xd8\xff" + b"x" * (download_core.DOWNLOAD_CHUNK_SIZE * 2)
    value = make_handle(data)
    raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway({raw_id: data}, chunksize=64 * 1024)
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert max(gateway.write_sizes) <= download_core.DOWNLOAD_CHUNK_SIZE
    assert len(gateway.write_sizes) > 1
    result.cleanup()


def test_034_whole_buffer_gateway_is_rejected(tmp_path):
    data = b"\xff\xd8\xff" + b"x" * download_core.DOWNLOAD_CHUNK_SIZE
    value = make_handle(data)
    raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway({raw_id: data}, chunksize=len(data))
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert "download_chunk_limit_exceeded" in safe(result)["results"][0]["blocking_issues"]


def test_035_large_mock_remains_chunked(tmp_path):
    data = b"\xff\xd8\xff" + b"z" * (2 * 1024 * 1024)
    value = make_handle(data)
    raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway({raw_id: data}, chunksize=download_core.DOWNLOAD_CHUNK_SIZE)
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert result.status == "ok" and len(gateway.write_sizes) == 9
    result.cleanup()


def test_036_private_id_helper_is_used(tmp_path):
    value = make_handle()
    original = handle_core._provider_file_id_for_download
    with patch.object(handle_core, "_provider_file_id_for_download", wraps=original) as helper:
        result = download_core.download_secure_media(value, FakeGateway(), workspace_parent=tmp_path)
    helper.assert_called_once_with(value)
    result.cleanup()


def test_037_temp_filename_ignores_supplier_name(tmp_path):
    result, _ = run_one(tmp_path, name="Imani (supplier name) 2.jpg")
    path = download_core._local_source_path_for_conversion(result.artifacts[0])
    assert path.name == "source-000-000.source.jpg"
    assert "Imani" not in path.name
    result.cleanup()


def test_038_workspace_is_dedicated_subdirectory(tmp_path):
    result, _ = run_one(tmp_path)
    path = download_core._local_source_path_for_conversion(result.artifacts[0])
    assert path.parent.parent == tmp_path and path.parent.name.startswith("xxxxdoll-secure-media-")
    result.cleanup()


@pytest.mark.parametrize("forbidden", [PROJECT_ROOT, PROJECT_ROOT / "reports", PROJECT_ROOT / "src", PROJECT_ROOT / "tests"])
def test_039_repo_workspace_rejected(forbidden):
    with pytest.raises(download_core.SecureMediaDownloadError, match="download_workspace_parent_invalid"):
        download_core.download_secure_media(make_handle(), FakeGateway(), workspace_parent=forbidden)


def test_043_private_path_helper_revalidates(tmp_path):
    result, _ = run_one(tmp_path)
    artifact = result.artifacts[0]
    path = download_core._local_source_path_for_conversion(artifact)
    path.write_bytes(PNG)
    with pytest.raises(download_core.SecureMediaDownloadError, match="downloaded_artifact_local_content_changed"):
        download_core._local_source_path_for_conversion(artifact)
    result.cleanup()


def test_044_forged_artifact_rejected():
    forged = object.__new__(download_core.VerifiedDownloadedMediaArtifact)
    with pytest.raises(download_core.SecureMediaDownloadError):
        download_core._local_source_path_for_conversion(forged)


def test_045_artifact_has_no_public_path(tmp_path):
    result, _ = run_one(tmp_path)
    artifact = result.artifacts[0]
    assert not hasattr(artifact, "local_source_path") and not hasattr(artifact, "get_path")
    result.cleanup()


def test_046_success_keeps_file_until_cleanup(tmp_path):
    result, _ = run_one(tmp_path)
    path = download_core._local_source_path_for_conversion(result.artifacts[0])
    assert path.exists()
    result.cleanup()


def test_047_cleanup_removes_file_and_authority(tmp_path):
    result, _ = run_one(tmp_path)
    artifact = result.artifacts[0]
    path = download_core._local_source_path_for_conversion(artifact)
    result.cleanup()
    assert not path.exists() and result.artifacts == ()
    with pytest.raises(download_core.SecureMediaDownloadError):
        download_core._local_source_path_for_conversion(artifact)


def test_048_cleanup_idempotent(tmp_path):
    result, _ = run_one(tmp_path)
    result.cleanup(); first = safe(result)
    result.cleanup(); second = safe(result)
    assert first == second and first["summary"]["source_files_cleaned"] == 1


def make_batch(count=96):
    handles = []
    data_by_id = {}
    for index in range(count):
        sku_index, position = divmod(index, 12)
        sku = f"MOCK-{sku_index + 1:03d}"
        raw_id = f"opaque_file_{index:03d}"
        data = b"\xff\xd8\xff" + f"mock-{index:03d}".encode()
        value = make_handle(data, sku=sku, position=position, raw_id=raw_id, name=f"photo-{position}.jpg")
        handles.append(value); data_by_id[raw_id] = data
    return tuple(handles), data_by_id


def test_049_mock_96_all_verified(tmp_path):
    handles, data = make_batch()
    result = download_core.download_secure_media(handles, FakeGateway(data), workspace_parent=tmp_path)
    report = safe(result)
    assert len(result.artifacts) == 96
    assert (report["summary"]["handles_received"], report["summary"]["downloads_verified"], report["summary"]["authoritative_artifacts"]) == (96, 96, 96)
    result.cleanup()


def test_050_mock_96_roles_and_order_preserved(tmp_path):
    handles, data = make_batch()
    result = download_core.download_secure_media(handles, FakeGateway(data), workspace_parent=tmp_path)
    keys = [(item.sku, item.selection_position) for item in result.artifacts]
    assert keys == [(item.sku, item.selection_position) for item in handles]
    assert sum(item.image_role == "primary" for item in result.artifacts) == 8
    assert sum(item.image_role == "gallery" for item in result.artifacts) == 88
    result.cleanup()


def test_051_one_failure_clears_all_authority_and_files(tmp_path):
    handles, data = make_batch()
    last_id = handle_core._provider_file_id_for_download(handles[-1])
    data[last_id] += b"changed"
    result = download_core.download_secure_media(handles, FakeGateway(data), workspace_parent=tmp_path)
    report = safe(result)
    assert result.artifacts == () and result.status == "blocked"
    assert report["summary"]["downloads_verified"] == 95
    assert report["summary"]["source_files_cleaned"] == 96
    assert not tuple(tmp_path.glob("xxxxdoll-secure-media-*/*"))


def test_052_noncanonical_order_rejected(tmp_path):
    handles, data = make_batch(2)
    with pytest.raises(download_core.SecureMediaDownloadError, match="download_handles_not_canonical_order"):
        download_core.download_secure_media(tuple(reversed(handles)), FakeGateway(data), workspace_parent=tmp_path)


def test_053_duplicate_identity_rejected(tmp_path):
    value = make_handle()
    with pytest.raises(download_core.SecureMediaDownloadError, match="duplicate_download_handle_identity"):
        download_core.download_secure_media((value, value), FakeGateway(), workspace_parent=tmp_path)


def error(code, transient, requests=1):
    return GoogleDriveContentDownloadError(code, transient=transient, requests_performed=requests)


@pytest.mark.parametrize("code", ["drive_download_forbidden", "drive_download_not_found"])
def test_054_nontransient_provider_errors_not_retried(tmp_path, code):
    value = make_handle(); raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway(failures={raw_id: [error(code, False)]})
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert len(gateway.calls) == 1 and code in safe(result)["results"][0]["blocking_issues"]


def test_056_transient_error_retries_then_succeeds(tmp_path):
    value = make_handle(); raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway(failures={raw_id: [error("drive_download_transient_error", True), None]})
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert result.status == "ok" and len(gateway.calls) == 2
    result.cleanup()


def test_057_transient_error_max_three_attempts(tmp_path):
    value = make_handle(); raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway(failures={raw_id: [error("drive_download_transient_error", True)] * 3})
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert result.status == "blocked" and len(gateway.calls) == 3
    assert safe(result)["summary"]["download_requests_performed"] == 3


def test_058_provider_exception_is_sanitized(tmp_path):
    value = make_handle(); raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway(failures={raw_id: [RuntimeError("secret raw provider response opaque_file_001")]})
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    serialized = json.dumps(safe(result))
    assert "opaque_file_001" not in serialized and "secret raw provider" not in serialized


def test_059_batch_limit(tmp_path):
    value = make_handle()
    with pytest.raises(download_core.SecureMediaDownloadError, match="batch_limit"):
        download_core.download_secure_media((value,) * 201, FakeGateway(), workspace_parent=tmp_path)


def test_060_expected_file_size_limit_blocks_before_gateway(tmp_path):
    value = make_handle(JPEG, expected_size=download_core.MAX_SOURCE_FILE_BYTES + 1)
    gateway = FakeGateway()
    result = download_core.download_secure_media(value, gateway, workspace_parent=tmp_path)
    assert gateway.calls == [] and "download_source_file_too_large" in safe(result)["results"][0]["blocking_issues"]


def test_061_byte_and_request_counts(tmp_path):
    result, gateway = run_one(tmp_path)
    summary = safe(result)["summary"]
    assert summary["bytes_downloaded"] == len(JPEG)
    assert summary["download_requests_performed"] == 1
    assert len(gateway.calls) == 1
    result.cleanup()


@pytest.mark.parametrize("field", [
    "policy_version", "sku", "selection_position", "image_role", "folder_role",
    "safe_name", "source_mime_type", "source_extension", "expected_size_bytes",
    "actual_size_bytes", "expected_md5_checksum", "actual_md5_checksum",
    "file_id_fingerprint", "expected_image_width", "expected_image_height",
    "source_verified", "warnings", "blocking_issues",
])
def test_062_artifact_safe_fields(tmp_path, field):
    result, _ = run_one(tmp_path)
    assert field in result.artifacts[0].to_safe_dict()
    result.cleanup()


@pytest.mark.parametrize("field", [
    "handles_received", "downloads_attempted", "downloads_verified", "downloads_failed",
    "checksum_verified", "checksum_mismatch", "size_verified", "size_mismatch",
    "signature_verified", "signature_mismatch", "source_files_created",
    "source_files_cleaned", "authoritative_artifacts", "download_requests_performed",
    "bytes_downloaded", "conversion_requests_performed",
    "wordpress_upload_requests_performed", "external_write_requests_performed",
])
def test_080_summary_fields(tmp_path, field):
    result, _ = run_one(tmp_path)
    assert field in safe(result)["summary"]
    result.cleanup()


@pytest.mark.parametrize("needle", [
    "provider_file_id", "raw_file_id", "raw_folder_id", "provider_resource_id",
    "resource_key", "drive.google.com", "download_url", "local_source_path",
    "temp_directory", "authorization", "cookie", "access_token", "refresh_token",
    "client_secret", "credentials", "opaque_file_001", str(PROJECT_ROOT).casefold(),
])
def test_098_safe_projection_forbidden(tmp_path, needle):
    result, _ = run_one(tmp_path)
    serialized = json.dumps(safe(result), sort_keys=True).casefold()
    assert needle.casefold() not in serialized
    result.cleanup()


@pytest.mark.parametrize("needle", ["opaque_file_001", str(PROJECT_ROOT), str(Path.cwd()), "local_source_path"])
def test_115_repr_forbidden(tmp_path, needle):
    result, _ = run_one(tmp_path)
    assert needle.casefold() not in repr(result).casefold()
    assert needle.casefold() not in repr(result.artifacts[0]).casefold()
    result.cleanup()


@pytest.mark.parametrize("attribute", [
    "_sku", "_selection_position", "_image_role", "_folder_role", "_safe_name",
    "_source_mime_type", "_source_extension", "_expected_size_bytes",
    "_actual_size_bytes", "_actual_md5_checksum",
])
def test_119_artifact_immutable(tmp_path, attribute):
    result, _ = run_one(tmp_path)
    with pytest.raises(AttributeError):
        setattr(result.artifacts[0], attribute, "changed")
    result.cleanup()


def test_129_artifact_pickle_blocked(tmp_path):
    result, _ = run_one(tmp_path)
    with pytest.raises(TypeError):
        pickle.dumps(result.artifacts[0])
    result.cleanup()


@pytest.mark.parametrize("name", [
    "from PIL", "ImageMagick", "cwebp", "ffmpeg", "wordpress_client", "wp-json",
    "SafeJsonReportWriter", "argparse", "subcommands", "requests.get",
])
def test_130_core_has_no_conversion_upload_report_or_cli(name):
    assert name.casefold() not in inspect.getsource(download_core).casefold()


@pytest.mark.parametrize("counter", [
    "conversion_requests_performed",
    "wordpress_upload_requests_performed",
    "external_write_requests_performed",
])
def test_140_external_counters_zero(tmp_path, counter):
    result, _ = run_one(tmp_path)
    assert safe(result)["summary"][counter] == 0 and safe(result)[counter] == 0
    result.cleanup()


@pytest.mark.parametrize("status,code,transient", [
    (403, "drive_download_forbidden", False),
    (404, "drive_download_not_found", False),
    (408, "drive_download_transient_error", True),
    (429, "drive_download_transient_error", True),
    (500, "drive_download_transient_error", True),
    (503, "drive_download_transient_error", True),
])
def test_143_provider_status_classification(status, code, transient):
    provider_error = RuntimeError("unsafe response")
    provider_error.resp = SimpleNamespace(status=status)
    result = _safe_content_download_error(provider_error, 1)
    assert result.code == code and result.transient is transient and str(result) == code


def test_149_gateway_uses_get_media_request_in_bounded_chunks():
    get_calls = []
    get_media_calls = []
    media_request = object()

    class Files:
        def get(self, **kwargs):
            get_calls.append(kwargs)
            raise AssertionError("files.get must not be used for media download")

        def get_media(self, **kwargs):
            get_media_calls.append(kwargs)
            return media_request

    class Drive:
        def files(self):
            return Files()

    class Sink:
        bytes_written = 0

        def write(self, value):
            self.bytes_written += len(value)
            return len(value)

    class Downloader:
        def __init__(self, sink, request, chunksize):
            assert chunksize == download_core.DOWNLOAD_CHUNK_SIZE
            assert request is media_request
            self.sink = sink

        def next_chunk(self, num_retries):
            assert num_retries == 0
            self.sink.write(JPEG)
            return None, True

    package = ModuleType("googleapiclient")
    http = ModuleType("googleapiclient.http")
    http.MediaIoBaseDownload = Downloader
    package.http = http
    with patch.dict(sys.modules, {"googleapiclient": package, "googleapiclient.http": http}):
        receipt = GoogleDriveContentGateway(Drive()).download_file(
            "opaque_file_001", Sink(), chunk_size=download_core.DOWNLOAD_CHUNK_SIZE,
        )
    assert get_calls == []
    assert get_media_calls == [{"fileId": "opaque_file_001", "supportsAllDrives": True}]
    assert "alt" not in get_media_calls[0]
    assert receipt == GoogleDriveContentDownloadReceipt(1, len(JPEG))


def test_150_gateway_request_counter_tracks_each_next_chunk():
    media_request = object()

    class Files:
        def get_media(self, **kwargs):
            return media_request

    class Drive:
        def files(self):
            return Files()

    class Sink:
        bytes_written = 0

        def write(self, value):
            self.bytes_written += len(value)
            return len(value)

    class Downloader:
        def __init__(self, sink, request, chunksize):
            assert request is media_request
            assert chunksize == 256 * 1024
            self.sink = sink
            self.calls = 0

        def next_chunk(self, num_retries):
            assert num_retries == 0
            self.calls += 1
            self.sink.write(b"chunk")
            return None, self.calls == 3

    package = ModuleType("googleapiclient")
    http = ModuleType("googleapiclient.http")
    http.MediaIoBaseDownload = Downloader
    package.http = http
    gateway = GoogleDriveContentGateway(Drive())
    with patch.dict(sys.modules, {"googleapiclient": package, "googleapiclient.http": http}):
        receipt = gateway.download_file(
            "opaque_file_001", Sink(), chunk_size=download_core.DOWNLOAD_CHUNK_SIZE,
        )
    assert receipt == GoogleDriveContentDownloadReceipt(3, 15)
    assert gateway.counters.read_requests_performed == 3


@pytest.mark.parametrize("status,code", [
    (403, "drive_download_forbidden"),
    (404, "drive_download_not_found"),
])
def test_151_get_media_provider_status_remains_safe(status, code):
    class Files:
        def get_media(self, **kwargs):
            return object()

    class Drive:
        def files(self):
            return Files()

    class Sink:
        bytes_written = 0

    class Downloader:
        def __init__(self, sink, request, chunksize):
            pass

        def next_chunk(self, num_retries):
            error = RuntimeError("unsafe provider body")
            error.resp = SimpleNamespace(status=status)
            raise error

    package = ModuleType("googleapiclient")
    http = ModuleType("googleapiclient.http")
    http.MediaIoBaseDownload = Downloader
    package.http = http
    gateway = GoogleDriveContentGateway(Drive())
    with patch.dict(sys.modules, {"googleapiclient": package, "googleapiclient.http": http}):
        with pytest.raises(GoogleDriveContentDownloadError) as caught:
            gateway.download_file(
                "opaque_file_001", Sink(), chunk_size=download_core.DOWNLOAD_CHUNK_SIZE,
            )
    assert caught.value.code == code
    assert caught.value.transient is False
    assert caught.value.requests_performed == 1


def test_153_reality_shape_mock_stream_is_multichunk_and_verified(tmp_path):
    assert REALITY_SOURCE_SIZE == 12_458_951
    assert REALITY_SOURCE_MD5 == "5c53847fc04463312e389b98e8184026"
    pattern = b"mock-canary-media-block"
    body_size = REALITY_SOURCE_SIZE - 3
    mock_media = b"\xff\xd8\xff" + (
        pattern * ((body_size + len(pattern) - 1) // len(pattern))
    )[:body_size]
    assert len(mock_media) == REALITY_SOURCE_SIZE
    assert hashlib.md5(mock_media, usedforsecurity=False).hexdigest() != REALITY_SOURCE_MD5
    handle = make_handle(mock_media)
    raw_id = handle_core._provider_file_id_for_download(handle)
    gateway = FakeGateway(
        {raw_id: mock_media}, chunksize=download_core.DOWNLOAD_CHUNK_SIZE,
    )
    result = download_core.download_secure_media(
        (handle,), gateway, workspace_parent=tmp_path,
    )
    report = safe(result)
    assert len(gateway.write_sizes) > 1
    assert sum(gateway.write_sizes) == REALITY_SOURCE_SIZE
    assert report["status"] == "ok"
    assert report["summary"]["downloads_verified"] == 1
    assert report["summary"]["authoritative_artifacts"] == 1
    result.cleanup()


def test_154_short_unexpected_payload_is_rejected_and_cleaned(tmp_path):
    expected = b"\xff\xd8\xff" + b"expected" * 100
    short_payload = b"\xff\xd8\xff" + b"x" * 151
    assert len(short_payload) == 154
    handle = make_handle(expected)
    raw_id = handle_core._provider_file_id_for_download(handle)
    gateway = FakeGateway({raw_id: short_payload}, chunksize=64)
    result = download_core.download_secure_media(
        (handle,), gateway, workspace_parent=tmp_path,
    )
    report = safe(result)
    assert report["status"] == "blocked"
    assert report["summary"]["checksum_mismatch"] == 1
    assert report["summary"]["downloads_verified"] == 0
    assert report["summary"]["authoritative_artifacts"] == 0
    assert report["summary"]["source_files_cleaned"] == 1
    assert not tuple(tmp_path.iterdir())


def test_150_gateway_exposes_no_write_methods():
    names = {name.casefold() for name in dir(GoogleDriveContentGateway)}
    assert not names.intersection({"upload", "update", "delete", "copy", "move", "create", "permissions"})


def test_151_no_cli_registration():
    from sync_worker import cli

    assert "secure-media-download" not in cli.build_parser().format_help()


def test_152_no_json_report_created(tmp_path):
    before = set(tmp_path.rglob("*.json"))
    result, _ = run_one(tmp_path)
    after = set(tmp_path.rglob("*.json"))
    assert after == before
    result.cleanup()


def interrupt_batch_handles(count):
    return tuple(
        make_handle(
            position=index,
            raw_id=f"interrupt_file_{index:03d}",
            name=f"interrupt-{index:03d}.jpg",
        )
        for index in range(count)
    )


def interrupt_gateway(handles, interrupt_index, error):
    data = {}
    failures = {}
    for index, handle in enumerate(handles):
        raw_id = handle_core._provider_file_id_for_download(handle)
        data[raw_id] = JPEG
        if index == interrupt_index:
            failures[raw_id] = [error]
    return FakeGateway(data, failures=failures)


@pytest.mark.parametrize("count,interrupt_index", [(1, 0), (6, 3), (96, 95)])
def test_155_keyboard_interrupt_cleans_entire_batch_and_reraises(
    tmp_path, count, interrupt_index,
):
    handles = interrupt_batch_handles(count)
    gateway = interrupt_gateway(handles, interrupt_index, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        download_core.download_secure_media(
            handles, gateway, workspace_parent=tmp_path,
        )
    assert not tuple(tmp_path.iterdir())
    assert not tuple(tmp_path.rglob("source-*"))


@pytest.mark.parametrize("count,interrupt_index,exit_code", [(1, 0, 3), (8, 4, 9)])
def test_158_system_exit_cleans_entire_batch_and_reraises(
    tmp_path, count, interrupt_index, exit_code,
):
    handles = interrupt_batch_handles(count)
    gateway = interrupt_gateway(handles, interrupt_index, SystemExit(exit_code))
    with pytest.raises(SystemExit) as caught:
        download_core.download_secure_media(
            handles, gateway, workspace_parent=tmp_path,
        )
    assert caught.value.code == exit_code
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("stage", ["write", "signature", "artifact"])
def test_160_base_exception_guard_covers_entire_workspace_lifecycle(tmp_path, stage):
    handle = make_handle()
    raw_id = handle_core._provider_file_id_for_download(handle)
    gateway = FakeGateway({raw_id: JPEG})
    if stage == "write":
        target = patch.object(
            download_core._BoundedHashingSink,
            "write",
            side_effect=KeyboardInterrupt(),
        )
    elif stage == "signature":
        target = patch.object(
            download_core,
            "_signature_matches",
            side_effect=KeyboardInterrupt(),
        )
    else:
        target = patch.object(
            download_core,
            "VerifiedDownloadedMediaArtifact",
            side_effect=KeyboardInterrupt(),
        )
    with target:
        with pytest.raises(KeyboardInterrupt):
            download_core.download_secure_media(
                (handle,), gateway, workspace_parent=tmp_path,
            )
    assert not tuple(tmp_path.iterdir())


def test_163_progress_emits_started_and_verified_in_canonical_order(tmp_path):
    handles = interrupt_batch_handles(3)
    gateway = interrupt_gateway(handles, -1, KeyboardInterrupt())
    events = []
    result = download_core.download_secure_media(
        handles,
        gateway,
        workspace_parent=tmp_path,
        progress_callback=events.append,
    )
    assert [event["status"] for event in events] == [
        "download_started", "download_verified",
        "download_started", "download_verified",
        "download_started", "download_verified",
    ]
    verified = events[1::2]
    assert [event["current_index"] for event in verified] == [1, 2, 3]
    assert all(event["total_items"] == 3 for event in events)
    assert [(event["sku"], event["selection_position"]) for event in verified] == [
        (handle.sku, handle.selection_position) for handle in handles
    ]
    result.cleanup()


def test_164_progress_emits_blocked_and_stops_download_progress(tmp_path):
    handles = interrupt_batch_handles(3)
    target = handles[1]
    raw_id = handle_core._provider_file_id_for_download(target)
    gateway = interrupt_gateway(handles, -1, KeyboardInterrupt())
    gateway.data_by_id[raw_id] = JPEG[:-1] + b"x"
    events = []
    result = download_core.download_secure_media(
        handles,
        gateway,
        workspace_parent=tmp_path,
        progress_callback=events.append,
    )
    assert result.status == "blocked"
    assert [event["status"] for event in events] == [
        "download_started", "download_verified",
        "download_started", "download_blocked",
    ]
    assert len(gateway.calls) == 2
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("forbidden", [
    "provider_file_id", "file_id_fingerprint", "drive.google.com", "download_url",
    "safe_name", "local_source_path", "temp", "md5", "credentials",
])
def test_165_progress_event_contains_only_safe_fields(tmp_path, forbidden):
    handle = make_handle()
    raw_id = handle_core._provider_file_id_for_download(handle)
    events = []
    result = download_core.download_secure_media(
        (handle,), FakeGateway({raw_id: JPEG}), workspace_parent=tmp_path,
        progress_callback=events.append,
    )
    assert set(events[0]) == {
        "current_index", "total_items", "sku", "selection_position", "status",
    }
    assert forbidden.casefold() not in json.dumps(events).casefold()
    result.cleanup()


@pytest.mark.parametrize("failure_status", ["download_started", "download_verified"])
def test_174_progress_callback_exception_cleans_and_becomes_fixed_error(
    tmp_path, failure_status,
):
    handle = make_handle()
    raw_id = handle_core._provider_file_id_for_download(handle)

    def callback(event):
        if event["status"] == failure_status:
            raise RuntimeError("unsafe callback detail and path")

    with pytest.raises(
        download_core.SecureMediaDownloadError,
        match="^download_progress_callback_failed$",
    ):
        download_core.download_secure_media(
            (handle,), FakeGateway({raw_id: JPEG}), workspace_parent=tmp_path,
            progress_callback=callback,
        )
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("signal", [KeyboardInterrupt(), SystemExit(7)])
def test_176_progress_callback_base_exception_is_original_and_cleanup(tmp_path, signal):
    handle = make_handle()
    raw_id = handle_core._provider_file_id_for_download(handle)

    def callback(event):
        if event["status"] == "download_verified":
            raise signal

    with pytest.raises(type(signal)) as caught:
        download_core.download_secure_media(
            (handle,), FakeGateway({raw_id: JPEG}), workspace_parent=tmp_path,
            progress_callback=callback,
        )
    assert caught.value is signal
    assert not tuple(tmp_path.iterdir())


def test_178_invalid_progress_callback_fails_before_workspace(tmp_path):
    handle = make_handle()
    with pytest.raises(
        download_core.SecureMediaDownloadError,
        match="^invalid_download_progress_callback$",
    ):
        download_core.download_secure_media(
            (handle,), FakeGateway(), workspace_parent=tmp_path,
            progress_callback="not-callable",
        )
    assert not tuple(tmp_path.iterdir())
