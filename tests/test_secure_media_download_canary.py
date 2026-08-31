from __future__ import annotations

import hashlib
import inspect
import json
import socket
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import image_selection_policy as selection_core
from sync_worker import secure_media_download as download_core
from sync_worker import secure_media_download_canary as canary_core
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker.config import (
    GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    GoogleSettings,
)
from sync_worker.google_api import (
    GoogleDriveContentDownloadError,
    GoogleDriveContentDownloadReceipt,
)
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
)


JPEG = b"\xff\xd8\xff" + b"canary-jpeg-content"
PNG = b"\x89PNG\r\n\x1a\n" + b"canary-png-content"


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in (
        "load_config", "load_google_config", "load_google_drive_metadata_config",
        "load_google_sheets_readonly_config",
    ):
        monkeypatch.setattr(cli, name, denied)


def make_handle(
    *,
    sku="MOCK-001",
    position=0,
    data=JPEG,
    mime="image/jpeg",
    raw_id=None,
    name=None,
):
    raw_id = raw_id or f"opaque_{sku}_{position}"
    name = name or f"supplier-{position}.jpg"
    source = ProductSourceRange(10, 20)
    primary = position == 0
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
        image_role=(
            selection_core.ImageSelectionRole.PRIMARY
            if primary else selection_core.ImageSelectionRole.GALLERY
        ),
        selection_reason=(
            selection_core.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY
            if primary else selection_core.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY
        ),
    )
    item = root_core.DriveManifestItem(
        safe_name=name,
        mime_type=mime,
        size_bytes=len(data),
        modified_time="2026-01-01T00:00:00Z",
        md5_checksum=hashlib.md5(data, usedforsecurity=False).hexdigest(),
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


def preparation(handles, *, status="ok", overrides=None):
    handles = tuple(handles)
    summary = {
        "selected_items": len(handles),
        "handles_prepared": len(handles),
        "handles_blocked": 0,
        "nested_handles": len(handles),
        "depth2_handles": 0,
        "primary_handles": sum(item.image_role.value == "primary" for item in handles),
        "gallery_handles": sum(item.image_role.value == "gallery" for item in handles),
        "sheets_read_requests_performed": 1,
        "root_drive_read_requests_performed": 1,
        "depth1_drive_read_requests_performed": 1,
        "depth2_drive_read_requests_performed": 0,
        "network_requests_performed": 3,
    }
    summary.update(overrides or {})
    return SelectedMediaHandlePreparationResult(status, {"summary": summary}, handles)


def metadata_settings():
    return GoogleSettings(
        drive_scope=GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
        sheets_scope=GOOGLE_SHEETS_READONLY_SCOPE,
    )


class Factory:
    def __init__(self, drive=None, error=None):
        self.drive = object() if drive is None else drive
        self.error = error
        self.content_settings = []
        self.metadata_calls = []

    def create_drive_content_readonly(self, settings):
        self.content_settings.append(settings)
        if self.error:
            raise self.error
        return self.drive

    def create_drive_metadata_clients(self, settings):
        self.metadata_calls.append(settings)
        raise AssertionError("not used by execute-only test")


class FakeGateway:
    def __init__(self, data_by_id, *, errors=None, chunksize=5):
        self.data_by_id = data_by_id
        self.errors = {key: list(value) for key, value in (errors or {}).items()}
        self.chunksize = chunksize
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append((provider_file_id, chunk_size))
        pending = self.errors.get(provider_file_id, [])
        if pending:
            error = pending.pop(0)
            if error is not None:
                raise error
        data = self.data_by_id[provider_file_id]
        for offset in range(0, len(data), self.chunksize):
            sink.write(data[offset : offset + self.chunksize])
        return GoogleDriveContentDownloadReceipt(1, len(data))


def execute(tmp_path, handles=None, *, target_sku="MOCK-001", position=0, gateway_data=None, factory=None):
    handles = (make_handle(),) if handles is None else tuple(handles)
    selected = next((item for item in handles if item.sku == target_sku and item.selection_position == position), handles[0])
    raw_id = handle_core._provider_file_id_for_download(selected)
    gateway = FakeGateway({raw_id: JPEG} if gateway_data is None else gateway_data)
    factory = Factory() if factory is None else factory
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation(handles), metadata_settings(), factory,
            sku=target_sku, position=position, workspace_parent=tmp_path,
        )
    return report, gateway, factory


def test_001_policy_version():
    assert canary_core.POLICY_VERSION == "xxxxdoll-secure-media-download-canary-v1"


def test_002_cli_registered():
    assert "download-selected-media-canary" in cli.build_parser().format_help()


@pytest.mark.parametrize("missing", [
    "--selection-report", "--baseline-snapshot", "--mapping", "--sheet",
    "--sku-report", "--sku", "--position",
])
def test_003_all_cli_arguments_required(missing):
    values = {
        "--selection-report": "selection.json",
        "--baseline-snapshot": "baseline.json",
        "--mapping": "mapping.json",
        "--sheet": "Mock Sheet",
        "--sku-report": "sku.json",
        "--sku": "MOCK-001",
        "--position": "0",
    }
    argv = ["download-selected-media-canary"]
    for flag, value in values.items():
        if flag != missing:
            argv.extend((flag, value))
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.mark.parametrize("value", ["-1", "1.5", "x", "", "true"])
def test_010_position_validation(value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([
            "download-selected-media-canary",
            "--selection-report", "a.json", "--baseline-snapshot", "b.json",
            "--mapping", "c.json", "--sheet", "Mock", "--sku-report", "d.json",
            "--sku", "MOCK-001", "--position", value,
        ])


def test_015_successful_jpeg_canary(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["status"] == "ok" and report["canary"]["source_verified"] is True


@pytest.mark.parametrize("field", [
    "sku", "selection_position", "image_role", "folder_role", "safe_name",
    "file_id_fingerprint", "source_mime_type", "expected_size_bytes",
    "actual_size_bytes", "expected_md5_checksum", "actual_md5_checksum",
    "source_verified",
])
def test_016_canary_schema(tmp_path, field):
    report, _, _ = execute(tmp_path)
    assert field in report["canary"]


@pytest.mark.parametrize("field", [
    "status", "policy_version", "canary", "preparation_summary",
    "download_summary", "cleanup_completed", "source_files_remaining",
    "network_requests_performed", "download_requests_performed",
    "media_read_requests_performed", "conversion_requests_performed",
    "wordpress_upload_requests_performed", "external_write_requests_performed",
    "write_requests_performed", "warnings", "blocking_issues",
])
def test_028_report_schema(tmp_path, field):
    report, _, _ = execute(tmp_path)
    assert field in report


def test_044_md5_size_signature_pass(tmp_path):
    report, _, _ = execute(tmp_path)
    summary = report["download_summary"]
    assert summary["checksum_verified"] == 1
    assert summary["size_verified"] == 1
    assert summary["signature_verified"] == 1


def test_045_cleanup_after_success(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["cleanup_completed"] is True
    assert report["source_files_remaining"] == 0
    assert not tuple(tmp_path.glob("xxxxdoll-secure-media-*/*"))


def test_046_download_summary_preserves_precleanup_authority(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["download_summary"]["authoritative_artifacts"] == 1
    assert report["cleanup_completed"] is True


def test_047_exact_sku_and_position(tmp_path):
    first = make_handle(sku="MOCK-001", position=0, raw_id="file_a")
    target = make_handle(sku="MOCK-002", position=1, raw_id="file_b")
    raw_id = handle_core._provider_file_id_for_download(target)
    gateway = FakeGateway({raw_id: JPEG})
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation((first, target)), metadata_settings(), Factory(),
            sku="MOCK-002", position=1, workspace_parent=tmp_path,
        )
    assert report["status"] == "ok" and report["canary"]["sku"] == "MOCK-002"
    assert gateway.calls[0][0] == raw_id


def test_048_missing_canary_blocks_before_content_client(tmp_path):
    factory = Factory()
    report = canary_core.execute_secure_media_download_canary(
        preparation((make_handle(),)), metadata_settings(), factory,
        sku="MOCK-999", position=0, workspace_parent=tmp_path,
    )
    assert report["blocking_issues"] == ["canary_handle_not_found"]
    assert factory.content_settings == []


def test_049_ambiguous_canary_blocks(tmp_path):
    value = make_handle()
    factory = Factory()
    report = canary_core.execute_secure_media_download_canary(
        preparation((value, value)), metadata_settings(), factory,
        sku=value.sku, position=0, workspace_parent=tmp_path,
    )
    assert report["blocking_issues"] == ["canary_handle_ambiguous"]
    assert factory.content_settings == []


@pytest.mark.parametrize("status,overrides", [
    ("blocked", {"handles_blocked": 1}),
    ("ok", {"handles_prepared": 0}),
    ("ok", {"selected_items": 2}),
])
def test_050_incomplete_preparation_blocks_download(tmp_path, status, overrides):
    value = make_handle(); factory = Factory()
    report = canary_core.execute_secure_media_download_canary(
        preparation((value,), status=status, overrides=overrides),
        metadata_settings(), factory, sku=value.sku, position=0,
        workspace_parent=tmp_path,
    )
    assert "canary_preparation_not_authoritative" in report["blocking_issues"]
    assert factory.content_settings == []


@pytest.mark.parametrize("drive_scope,sheets_scope", [
    (GOOGLE_DRIVE_CONTENT_READONLY_SCOPE, GOOGLE_SHEETS_READONLY_SCOPE),
    (GOOGLE_DRIVE_METADATA_READONLY_SCOPE, ""),
    ("https://www.googleapis.com/auth/drive", GOOGLE_SHEETS_READONLY_SCOPE),
])
def test_053_preparation_scope_is_exact(tmp_path, drive_scope, sheets_scope):
    value = make_handle(); factory = Factory()
    report = canary_core.execute_secure_media_download_canary(
        preparation((value,)), GoogleSettings(drive_scope=drive_scope, sheets_scope=sheets_scope),
        factory, sku=value.sku, position=0, workspace_parent=tmp_path,
    )
    assert report["blocking_issues"] == ["canary_preparation_scope_mismatch"]
    assert factory.content_settings == []


def test_056_content_settings_are_isolated(tmp_path):
    report, _, factory = execute(tmp_path)
    assert report["status"] == "ok"
    assert factory.content_settings[0].drive_scope == GOOGLE_DRIVE_CONTENT_READONLY_SCOPE
    assert factory.content_settings[0].sheets_scope == ""


def test_057_metadata_settings_are_not_mutated(tmp_path):
    value = make_handle(); configured = metadata_settings(); factory = Factory()
    raw_id = handle_core._provider_file_id_for_download(value)
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        canary_core.execute_secure_media_download_canary(
            preparation((value,)), configured, factory,
            sku=value.sku, position=0, workspace_parent=tmp_path,
        )
    assert configured.drive_scope == GOOGLE_DRIVE_METADATA_READONLY_SCOPE


def test_058_only_one_handle_passed_to_download_core(tmp_path):
    handles = (make_handle(position=0, raw_id="file_0"), make_handle(position=1, raw_id="file_1"))
    raw_id = handle_core._provider_file_id_for_download(handles[0])
    gateway = FakeGateway({raw_id: JPEG})
    original = download_core.download_secure_media
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        with patch.object(download_core, "download_secure_media", wraps=original) as mocked:
            report = canary_core.execute_secure_media_download_canary(
                preparation(handles), metadata_settings(), Factory(),
                sku="MOCK-001", position=0, workspace_parent=tmp_path,
            )
    assert report["status"] == "ok"
    assert mocked.call_args.args[0] == (handles[0],)


def test_059_download_core_reused(tmp_path):
    value = make_handle(); raw_id = handle_core._provider_file_id_for_download(value)
    original = download_core.download_secure_media
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        with patch.object(download_core, "download_secure_media", wraps=original) as mocked:
            canary_core.execute_secure_media_download_canary(
                preparation((value,)), metadata_settings(), Factory(),
                sku=value.sku, position=0, workspace_parent=tmp_path,
            )
    mocked.assert_called_once()


def test_060_gallery_canary_supported(tmp_path):
    value = make_handle(position=1)
    raw_id = handle_core._provider_file_id_for_download(value)
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        report = canary_core.execute_secure_media_download_canary(
            preparation((value,)), metadata_settings(), Factory(),
            sku=value.sku, position=1, workspace_parent=tmp_path,
        )
    assert report["status"] == "ok" and report["canary"]["image_role"] == "gallery"


def test_061_primary_audit_preserved(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["canary"]["selection_position"] == 0
    assert report["canary"]["image_role"] == "primary"


@pytest.mark.parametrize("code", ["drive_download_forbidden", "drive_download_not_found"])
def test_062_provider_failure_cleanup_and_no_fallback(tmp_path, code):
    first = make_handle(raw_id="file_first")
    fallback = make_handle(position=1, raw_id="file_second")
    error = GoogleDriveContentDownloadError(code, transient=False, requests_performed=1)
    gateway = FakeGateway({"file_first": JPEG}, errors={"file_first": [error]})
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation((first, fallback)), metadata_settings(), Factory(),
            sku=first.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["status"] == "blocked" and report["cleanup_completed"] is True
    assert len(gateway.calls) == 1 and gateway.calls[0][0] == "file_first"


def test_064_transient_retry_inherited(tmp_path):
    value = make_handle(); raw_id = handle_core._provider_file_id_for_download(value)
    transient = GoogleDriveContentDownloadError(
        "drive_download_transient_error", transient=True, requests_performed=1,
    )
    gateway = FakeGateway({raw_id: JPEG}, errors={raw_id: [transient, None]})
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation((value,)), metadata_settings(), Factory(),
            sku=value.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["status"] == "ok" and len(gateway.calls) == 2


@pytest.mark.parametrize("kind,data", [
    ("checksum", JPEG + b"changed"),
    ("signature", b"bad-signature-content"),
])
def test_065_integrity_failure_inherited(tmp_path, kind, data):
    value = make_handle(data=data if kind == "signature" else JPEG)
    raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway({raw_id: data})
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation((value,)), metadata_settings(), Factory(),
            sku=value.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["status"] == "blocked" and report["source_files_remaining"] == 0
    assert len(gateway.calls) == 1


def test_067_content_client_failure_safe(tmp_path):
    value = make_handle()
    report = canary_core.execute_secure_media_download_canary(
        preparation((value,)), metadata_settings(),
        Factory(error=RuntimeError("secret credential provider path")),
        sku=value.sku, position=0, workspace_parent=tmp_path,
    )
    assert report["status"] == "failed"
    assert report["blocking_issues"] == ["canary_content_client_creation_failed"]
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("needle", [
    "provider_file_id", "raw_file_id", "raw_folder_id", "provider_resource_id",
    "resource_key", "drive.google.com", "download_url", "local_source_path",
    "temp_directory", str(PROJECT_ROOT), "authorization", "cookie",
    "access_token", "refresh_token", "client_secret", "credentials",
])
def test_068_report_forbidden(tmp_path, needle):
    report, _, _ = execute(tmp_path)
    assert needle.casefold() not in json.dumps(report, sort_keys=True).casefold()


@pytest.mark.parametrize("counter", [
    "media_read_requests_performed", "conversion_requests_performed",
    "wordpress_upload_requests_performed", "external_write_requests_performed",
    "write_requests_performed",
])
def test_084_forbidden_activity_zero(tmp_path, counter):
    report, _, _ = execute(tmp_path)
    assert report[counter] == 0


@pytest.mark.parametrize("needle", [
    "from PIL", "import PIL", "ImageMagick", "cwebp", "ffmpeg", "wp-json", "WooCommerce",
    "wordpress_client", "run_selected_media_handle_preparation",
    "selected-media-handle-preparation.json",
    "google-drive-folder-manifest-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
])
def test_089_source_has_no_forbidden_workflow(needle):
    assert needle.casefold() not in inspect.getsource(canary_core).casefold()


def test_101_same_process_preparation_core_reused(tmp_path):
    value = make_handle()
    prepared = preparation((value,))
    report = canary_core._blocked_report(value.sku, 0, "mock")
    with patch.object(canary_core, "prepare_selected_media_handles", return_value=prepared) as prep:
        with patch.object(canary_core, "execute_secure_media_download_canary", return_value=report) as execute_mock:
            written, output = canary_core.run_secure_media_download_canary(
                Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
                "Mock Sheet", Path("sku.json"), value.sku, 0,
                metadata_settings(), Factory(), project_root=tmp_path,
            )
    prep.assert_called_once()
    execute_mock.assert_called_once()
    assert output == tmp_path / "reports" / canary_core.REPORT_FILENAME
    assert written == report


def test_102_old_reports_are_not_modified(tmp_path):
    reports = tmp_path / "reports"; reports.mkdir()
    old_names = (
        "selected-media-baseline-snapshot.json", "image-selection-dry-run.json",
        "selected-media-handle-preparation.json", "google-drive-folder-manifest-dry-run.json",
        "google-drive-nested-folder-manifest-dry-run.json",
        "google-drive-depth2-folder-manifest-dry-run.json",
    )
    for name in old_names:
        (reports / name).write_text("frozen", encoding="utf-8")
    before = {name: (reports / name).read_bytes() for name in old_names}
    value = make_handle(); prepared = preparation((value,))
    mocked = canary_core._blocked_report(value.sku, 0, "mock")
    with patch.object(canary_core, "prepare_selected_media_handles", return_value=prepared):
        with patch.object(canary_core, "execute_secure_media_download_canary", return_value=mocked):
            canary_core.run_secure_media_download_canary(
                Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
                value.sku, 0, metadata_settings(), Factory(), project_root=tmp_path,
            )
    assert before == {name: (reports / name).read_bytes() for name in old_names}


def test_103_report_deterministic(tmp_path):
    (tmp_path / "one").mkdir()
    first, _, _ = execute(tmp_path / "one")
    (tmp_path / "two").mkdir()
    second, _, _ = execute(tmp_path / "two")
    assert first == second


def test_104_mock_96_preparation_then_one_download(tmp_path):
    handles = []
    for index in range(96):
        sku_index, position = divmod(index, 12)
        handles.append(make_handle(
            sku=f"MOCK-{sku_index + 1:03d}", position=position,
            raw_id=f"file_{index:03d}", name=f"photo-{position}.jpg",
        ))
    target = handles[0]; raw_id = handle_core._provider_file_id_for_download(target)
    gateway = FakeGateway({raw_id: JPEG})
    factory = Factory()
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation(handles), metadata_settings(), factory,
            sku=target.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["preparation_summary"]["selected_items"] == 96
    assert report["download_summary"]["handles_received"] == 1
    assert report["download_summary"]["downloads_verified"] == 1
    assert len(gateway.calls) == 1


def test_105_no_artifact_retained_by_report(tmp_path):
    report, _, _ = execute(tmp_path)
    assert "artifact" not in report and report["source_files_remaining"] == 0


def test_106_short_blocked_payload_projects_safe_actual_audit(tmp_path):
    expected = b"\xff\xd8\xff" + b"expected" * 100
    short_payload = b"\xff\xd8\xff" + b"x" * 151
    assert len(short_payload) == 154
    value = make_handle(data=expected)
    raw_id = handle_core._provider_file_id_for_download(value)
    gateway = FakeGateway({raw_id: short_payload}, chunksize=64)
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_secure_media_download_canary(
            preparation((value,)), metadata_settings(), Factory(),
            sku=value.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["status"] == "blocked"
    assert report["download_summary"]["bytes_downloaded"] == 154
    assert report["download_summary"]["downloads_verified"] == 0
    assert report["download_summary"]["authoritative_artifacts"] == 0
    assert report["canary"]["actual_size_bytes"] == 154
    assert report["canary"]["actual_md5_checksum"] == hashlib.md5(
        short_payload, usedforsecurity=False,
    ).hexdigest()
    assert report["canary"]["source_verified"] is False
    assert report["cleanup_completed"] is True
    assert report["source_files_remaining"] == 0
    assert not tuple(tmp_path.iterdir())


def test_107_failed_audit_projection_rejects_unsafe_values():
    canary = canary_core._empty_canary("MOCK-001", 0)
    canary_core._project_safe_download_audit(canary, {
        "results": [{
            "sku": "MOCK-001",
            "selection_position": 0,
            "actual_size_bytes": -1,
            "actual_md5_checksum": "secret-or-response-body",
            "source_verified": "true",
        }],
    })
    assert canary["actual_size_bytes"] is None
    assert canary["actual_md5_checksum"] is None
    assert canary["source_verified"] is False
