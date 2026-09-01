from __future__ import annotations

import hashlib
import inspect
import json
import pickle
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_depth2_folder_manifest as depth2_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import image_selection_policy as selection_core
from sync_worker import secure_media_download as download_core
from sync_worker import secure_media_download_execution as execution_core
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


JPEG = b"\xff\xd8\xff" + b"batch-jpeg-content"
PNG = b"\x89PNG\r\n\x1a\n" + b"batch-png-content"


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
    depth2=False,
    expected_size=None,
):
    raw_id = raw_id or f"opaque_{sku}_{position}"
    name = f"supplier-{position}.jpg" if mime == "image/jpeg" else f"supplier-{position}.png"
    source = ProductSourceRange(int(sku.rsplit("-", 1)[-1]) * 10, int(sku.rsplit("-", 1)[-1]) * 10 + 5)
    primary = position == 0
    image_role = (
        selection_core.ImageSelectionRole.PRIMARY
        if primary else selection_core.ImageSelectionRole.GALLERY
    )
    folder_role = (
        folder_core.FolderRole.FACTORY_PHOTOS
        if depth2 else folder_core.FolderRole.STOREFRONT_PHOTOS
    )
    selection = selection_core.ImageSelectionItem(
        sku=sku,
        folder_role=folder_role,
        safe_name=name,
        source_manifest_kind="depth2" if depth2 else "nested",
        depth=2 if depth2 else 1,
        safe_folder_name="Factory Deep" if depth2 else "Storefront Photos",
        parent_safe_folder_name="Factory Photos" if depth2 else None,
        product_source=source,
        requires_deeper_inventory=False,
        quality_eligible=True,
        selected=True,
        selection_position=position,
        image_role=image_role,
        selection_reason=(
            selection_core.ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK
            if depth2 and primary
            else selection_core.ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL
            if depth2
            else selection_core.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY
            if primary
            else selection_core.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY
        ),
    )
    item = root_core.DriveManifestItem(
        safe_name=name,
        mime_type=mime,
        size_bytes=len(data) if expected_size is None else expected_size,
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
    if depth2:
        manifest = depth2_core.GoogleDriveDepth2FolderManifest(
            sku=sku,
            product_source=source,
            root_folder_id_fingerprint=root_core.fingerprint_drive_id("root_" + sku),
            depth1_folder_id_fingerprint=root_core.fingerprint_drive_id("depth1_" + sku),
            depth2_folder_id_fingerprint=root_core.fingerprint_drive_id("depth2_" + sku),
            depth1_safe_folder_name="Factory Photos",
            depth2_safe_folder_name="Factory Deep",
            depth=2,
            status="listed",
            items=(item,),
            pages_read=1,
        )
    else:
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


def make_handles(count, *, depth2_count=0):
    handles = []
    for index in range(count):
        sku_index, position = divmod(index, 12)
        handles.append(make_handle(
            sku=f"MOCK-{sku_index + 1:03d}",
            position=position,
            raw_id=f"opaque_file_{index:03d}",
            depth2=index >= count - depth2_count,
        ))
    return tuple(handles)


def preparation(handles, *, status="ok", overrides=None):
    handles = tuple(handles)
    summary = {
        "selected_items": len(handles),
        "handles_prepared": len(handles),
        "handles_blocked": 0,
        "nested_handles": sum(item.source_manifest_kind == "nested" for item in handles),
        "depth2_handles": sum(item.source_manifest_kind == "depth2" for item in handles),
        "primary_handles": sum(item.image_role.value == "primary" for item in handles),
        "gallery_handles": sum(item.image_role.value == "gallery" for item in handles),
        "sheets_read_requests_performed": 1,
        "root_drive_read_requests_performed": 8,
        "depth1_drive_read_requests_performed": 10,
        "depth2_drive_read_requests_performed": 1,
        "network_requests_performed": 20,
    }
    summary.update(overrides or {})
    return SelectedMediaHandlePreparationResult(status, {"summary": summary}, handles)


def metadata_settings():
    return GoogleSettings(
        drive_scope=GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
        sheets_scope=GOOGLE_SHEETS_READONLY_SCOPE,
    )


class Factory:
    def __init__(self, *, error=None):
        self.error = error
        self.content_settings = []
        self.metadata_calls = []
        self.drive = object()

    def create_drive_content_readonly(self, settings):
        self.content_settings.append(settings)
        if self.error is not None:
            raise self.error
        return self.drive

    def create_drive_metadata_clients(self, settings):
        self.metadata_calls.append(settings)
        raise AssertionError("not used by execute-only tests")


class FakeGateway:
    def __init__(self, data_by_id, *, errors=None, stream_chunk_size=7):
        self.data_by_id = data_by_id
        self.errors = {key: list(value) for key, value in (errors or {}).items()}
        self.stream_chunk_size = stream_chunk_size
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append((provider_file_id, chunk_size))
        pending = self.errors.get(provider_file_id, [])
        if pending:
            error = pending.pop(0)
            if error is not None:
                raise error
        data = self.data_by_id[provider_file_id]
        requests = 0
        for offset in range(0, len(data), self.stream_chunk_size):
            sink.write(data[offset:offset + self.stream_chunk_size])
            requests += 1
        return GoogleDriveContentDownloadReceipt(requests, len(data))


def gateway_for(handles, *, replacements=None, errors=None, stream_chunk_size=7):
    replacements = replacements or {}
    data_by_id = {}
    for handle in handles:
        raw_id = handle_core._provider_file_id_for_download(handle)
        data_by_id[raw_id] = replacements.get((handle.sku, handle.selection_position), JPEG)
    return FakeGateway(
        data_by_id,
        errors=errors,
        stream_chunk_size=stream_chunk_size,
    )


def capacity_for(handles, delta=0):
    total = sum(item.size_bytes for item in handles)
    free = total + execution_core.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES + delta
    return lambda path: SimpleNamespace(free=free)


def execute_case(
    tmp_path,
    handles=None,
    *,
    gateway=None,
    factory=None,
    capacity_delta=0,
    progress_callback=None,
):
    handles = make_handles(2) if handles is None else tuple(handles)
    gateway = gateway_for(handles) if gateway is None else gateway
    factory = Factory() if factory is None else factory
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), factory,
            workspace_parent=tmp_path,
            disk_usage_reader=capacity_for(handles, capacity_delta),
            progress_callback=progress_callback,
        )
        report = execution_core.finalize_media_download_execution(batch)
    return report, gateway, factory


def test_001_policy_version():
    assert execution_core.POLICY_VERSION == "xxxxdoll-secure-media-download-execution-v1"


def test_002_report_filename():
    assert execution_core.REPORT_FILENAME == "secure-media-download-execution.json"


def test_003_reserve_is_512_mib():
    assert execution_core.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES == 512 * 1024 * 1024


def test_004_batch_ceiling_derived_from_core():
    assert execution_core.MAX_BATCH_SOURCE_BYTES == (
        download_core.MAX_HANDLES_PER_BATCH * download_core.MAX_SOURCE_FILE_BYTES
    )


def test_005_cli_registered():
    assert "download-selected-media-batch" in cli.build_parser().format_help()


@pytest.mark.parametrize("missing", [
    "--selection-report", "--baseline-snapshot", "--mapping", "--sheet", "--sku-report",
])
def test_006_cli_arguments_required(missing):
    values = {
        "--selection-report": "selection.json",
        "--baseline-snapshot": "baseline.json",
        "--mapping": "mapping.json",
        "--sheet": "Mock Sheet",
        "--sku-report": "sku.json",
    }
    argv = ["download-selected-media-batch"]
    for flag, value in values.items():
        if flag != missing:
            argv.extend((flag, value))
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize("forbidden", ["--sku", "--position"])
def test_011_cli_has_no_canary_selector(forbidden):
    subparser = cli.build_parser().parse_args([
        "download-selected-media-batch",
        "--selection-report", "selection.json",
        "--baseline-snapshot", "baseline.json",
        "--mapping", "mapping.json",
        "--sheet", "Mock Sheet",
        "--sku-report", "sku.json",
    ])
    assert not hasattr(subparser, forbidden.removeprefix("--").replace("-", "_"))


def test_013_cli_parses_all_local_inputs():
    args = cli.build_parser().parse_args([
        "download-selected-media-batch",
        "--selection-report", "selection.json",
        "--baseline-snapshot", "baseline.json",
        "--mapping", "mapping.json",
        "--sheet", "Mock Sheet",
        "--sku-report", "sku.json",
    ])
    assert args.command == "download-selected-media-batch"
    assert args.selection_report_path == Path("selection.json")
    assert args.sheet_title == "Mock Sheet"


@pytest.mark.parametrize("status,expected_exit", [("ok", 0), ("blocked", 1), ("failed", 2)])
def test_014_cli_exit_status(status, expected_exit, monkeypatch):
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", lambda: metadata_settings())
    report = {
        "status": status, "selected_items": 2, "downloads_verified": 2,
        "cleanup_completed": True, "source_files_remaining": 0,
        "download_requests_performed": 2,
    }
    monkeypatch.setattr(
        cli, "run_secure_media_download_execution",
        lambda *args, **kwargs: (report, Path("report.json")),
    )
    assert cli.main([
        "download-selected-media-batch",
        "--selection-report", "selection.json",
        "--baseline-snapshot", "baseline.json",
        "--mapping", "mapping.json",
        "--sheet", "Mock Sheet",
        "--sku-report", "sku.json",
    ]) == expected_exit


def test_017_same_process_preparation_core_reused(tmp_path):
    handles = make_handles(2)
    prepared = preparation(handles)
    mocked_report = execution_core._blocked_report("mock")
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared) as prep:
        with patch.object(
            execution_core, "execute_prepared_media_download_batch",
            side_effect=execution_core.SecureMediaDownloadExecutionError("mock"),
        ):
            report, output = execution_core.run_secure_media_download_execution(
                Path("selection"), Path("baseline"), Path("mapping"), "Mock Sheet",
                Path("sku"), metadata_settings(), Factory(), project_root=tmp_path,
            )
    prep.assert_called_once()
    assert report == mocked_report or report["blocking_issues"] == ["mock"]
    assert output == tmp_path / "reports" / execution_core.REPORT_FILENAME


def test_018_no_preparation_json_authority_restore():
    source = inspect.getsource(execution_core)
    assert "selected-media-handle-preparation.json" not in source
    assert "run_selected_media_handle_preparation" not in source


def test_019_internal_execution_writes_no_report(tmp_path):
    handles = make_handles(1)
    gateway = gateway_for(handles)
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert not tuple(tmp_path.rglob("*.json"))
    batch.cleanup()


def test_020_internal_batch_retains_capability_until_owner_cleanup(tmp_path):
    handles = make_handles(2)
    gateway = gateway_for(handles)
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert len(batch.download_result.artifacts) == 2
    batch.cleanup()
    assert batch.download_result.artifacts == ()
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("protocol", [None, 0, 4, 5])
def test_021_internal_batch_not_serializable(tmp_path, protocol):
    handles = make_handles(1)
    gateway = gateway_for(handles)
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    with pytest.raises(TypeError, match="not_serializable"):
        pickle.dumps(batch) if protocol is None else pickle.dumps(batch, protocol=protocol)
    batch.cleanup()


def test_025_internal_repr_has_no_authority(tmp_path):
    handles = make_handles(1)
    gateway = gateway_for(handles)
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    rendered = repr(batch)
    assert "opaque_file" not in rendered and str(tmp_path) not in rendered
    batch.cleanup()


def test_026_content_scope_isolated(tmp_path):
    report, _, factory = execute_case(tmp_path)
    assert report["status"] == "ok"
    assert factory.content_settings[0].drive_scope == GOOGLE_DRIVE_CONTENT_READONLY_SCOPE
    assert factory.content_settings[0].sheets_scope == ""


def test_027_metadata_settings_not_mutated(tmp_path):
    handles = make_handles(1)
    settings = metadata_settings()
    gateway = gateway_for(handles)
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_media_download_batch(
            preparation(handles), settings, Factory(),
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert settings.drive_scope == GOOGLE_DRIVE_METADATA_READONLY_SCOPE
    assert settings.sheets_scope == GOOGLE_SHEETS_READONLY_SCOPE
    batch.cleanup()


@pytest.mark.parametrize("drive_scope,sheets_scope", [
    (GOOGLE_DRIVE_CONTENT_READONLY_SCOPE, GOOGLE_SHEETS_READONLY_SCOPE),
    (GOOGLE_DRIVE_METADATA_READONLY_SCOPE, ""),
    ("https://www.googleapis.com/auth/drive", GOOGLE_SHEETS_READONLY_SCOPE),
])
def test_028_invalid_preparation_scope_blocks_before_content(tmp_path, drive_scope, sheets_scope):
    handles = make_handles(1)
    factory = Factory()
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.execute_prepared_media_download_batch(
            preparation(handles), GoogleSettings(drive_scope=drive_scope, sheets_scope=sheets_scope),
            factory, workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert caught.value.code == "execution_preparation_scope_mismatch"
    assert factory.content_settings == []


def test_031_full_tuple_passed_to_download_core(tmp_path):
    handles = make_handles(5)
    gateway = gateway_for(handles)
    original = download_core.download_secure_media
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        with patch.object(download_core, "download_secure_media", wraps=original) as mocked:
            batch = execution_core.execute_prepared_media_download_batch(
                preparation(handles), metadata_settings(), Factory(),
                workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
            )
    assert mocked.call_args.args[0] == handles
    batch.cleanup()


@pytest.mark.parametrize("count", [1, 2, 3, 7, 12, 13, 24, 48, 72, 96])
def test_032_dynamic_batch_size(tmp_path, count):
    handles = make_handles(count)
    report, gateway, _ = execute_case(tmp_path, handles)
    assert report["selected_items"] == count
    assert report["downloads_verified"] == count
    assert len(gateway.calls) == count
    assert report["verified_artifacts_before_cleanup"] == count


def test_042_canonical_order_preserved_in_results(tmp_path):
    handles = make_handles(24)
    report, _, _ = execute_case(tmp_path, handles)
    expected = [(item.sku, item.selection_position) for item in handles]
    actual = [(item["sku"], item["selection_position"]) for item in report["results"]]
    assert actual == expected


def test_043_noncanonical_order_rejected_before_content(tmp_path):
    handles = tuple(reversed(make_handles(2)))
    factory = Factory()
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), factory,
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert caught.value.code == "download_handles_not_canonical_order"
    assert factory.content_settings == []


def test_044_duplicate_identity_rejected_before_content(tmp_path):
    value = make_handles(1)[0]
    handles = (value, value)
    factory = Factory()
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), factory,
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert caught.value.code == "download_handles_not_canonical_order"
    assert factory.content_settings == []


def test_045_preflight_sufficient_space(tmp_path):
    handles = make_handles(2)
    result = execution_core.preflight_download_workspace(
        handles, workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles, 1),
    )
    assert result.expected_total_source_bytes == sum(item.size_bytes for item in handles)


def test_046_preflight_exact_capacity(tmp_path):
    handles = make_handles(2)
    result = execution_core.preflight_download_workspace(
        handles, workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
    )
    assert result.required_capacity_bytes == (
        result.expected_total_source_bytes
        + execution_core.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
    )


def test_047_preflight_one_byte_short(tmp_path):
    handles = make_handles(2)
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.preflight_download_workspace(
            handles, workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles, -1),
        )
    assert caught.value.code == "insufficient_download_workspace_capacity"
    assert caught.value.expected_total_source_bytes == sum(item.size_bytes for item in handles)


@pytest.mark.parametrize("value,code", [
    (None, "download_preflight_size_missing"),
    (0, "download_preflight_size_invalid"),
    (-1, "download_preflight_size_invalid"),
    (True, "download_preflight_size_invalid"),
    ("20", "download_preflight_size_invalid"),
    (download_core.MAX_SOURCE_FILE_BYTES + 1, "download_preflight_file_too_large"),
])
def test_048_invalid_size_preflight_blocks(tmp_path, value, code):
    handle = make_handles(1)[0]
    object.__setattr__(handle, "_size_bytes", value)
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.preflight_download_workspace(
            (handle,), workspace_parent=tmp_path,
            disk_usage_reader=lambda path: SimpleNamespace(free=10**12),
        )
    assert caught.value.code == code


@pytest.mark.parametrize("reader", [
    lambda path: (_ for _ in ()).throw(OSError("unsafe path")),
    lambda path: SimpleNamespace(free=None),
    lambda path: SimpleNamespace(free=-1),
    lambda path: SimpleNamespace(free=True),
])
def test_054_capacity_probe_failure_safe(tmp_path, reader):
    handles = make_handles(1)
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.preflight_download_workspace(
            handles, workspace_parent=tmp_path, disk_usage_reader=reader,
        )
    assert caught.value.code == "download_workspace_capacity_unavailable"
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("mutation", ["insufficient", "missing", "negative", "too_large"])
def test_058_preflight_failure_creates_no_content_client_or_request(tmp_path, mutation):
    handles = list(make_handles(1))
    delta = 0
    if mutation == "insufficient":
        delta = -1
    elif mutation == "missing":
        object.__setattr__(handles[0], "_size_bytes", None)
    elif mutation == "negative":
        object.__setattr__(handles[0], "_size_bytes", -1)
    else:
        object.__setattr__(handles[0], "_size_bytes", download_core.MAX_SOURCE_FILE_BYTES + 1)
    factory = Factory()
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError):
        execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), factory,
            workspace_parent=tmp_path,
            disk_usage_reader=(
                capacity_for(handles, delta)
                if mutation == "insufficient"
                else lambda path: SimpleNamespace(free=10**12)
            ),
        )
    assert factory.content_settings == []


def test_062_handle_count_limit_blocks_before_capacity(tmp_path):
    value = make_handles(1)[0]
    handles = (value,) * (download_core.MAX_HANDLES_PER_BATCH + 1)
    factory = Factory()
    reader = Mock(side_effect=AssertionError("capacity must not run"))
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(), factory,
            workspace_parent=tmp_path, disk_usage_reader=reader,
        )
    assert caught.value.code == "download_batch_handle_limit_exceeded"
    reader.assert_not_called()
    assert factory.content_settings == []


def test_063_current_shape_96_success(tmp_path):
    handles = make_handles(96, depth2_count=12)
    report, gateway, _ = execute_case(tmp_path, handles)
    assert report["status"] == "ok"
    assert report["preparation_summary"]["nested_handles"] == 84
    assert report["preparation_summary"]["depth2_handles"] == 12
    assert report["preparation_summary"]["primary_handles"] == 8
    assert report["preparation_summary"]["gallery_handles"] == 88
    assert report["downloads_verified"] == 96
    assert report["downloads_failed"] == 0
    assert report["checksum_verified"] == 96
    assert report["size_verified"] == 96
    assert report["signature_verified"] == 96
    assert report["verified_artifacts_before_cleanup"] == 96
    assert report["source_files_remaining"] == 0
    assert report["retained_authoritative_artifacts"] == 0
    assert len(gateway.calls) == 96


@pytest.mark.parametrize("failure_index", [0, 47, 95])
def test_064_first_middle_last_failure_all_or_nothing(tmp_path, failure_index):
    handles = make_handles(96, depth2_count=12)
    target = handles[failure_index]
    replacements = {(target.sku, target.selection_position): JPEG[:-1] + b"x"}
    gateway = gateway_for(handles, replacements=replacements)
    report, _, _ = execute_case(tmp_path, handles, gateway=gateway)
    assert report["status"] == "blocked"
    assert report["downloads_failed"] == 1
    assert report["verified_artifacts_before_cleanup"] == 0
    assert report["retained_authoritative_artifacts"] == 0
    assert report["cleanup_completed"] is True
    assert report["source_files_remaining"] == 0
    assert not tuple(tmp_path.iterdir())


def test_067_final_failure_does_not_expose_95_artifacts(tmp_path):
    handles = make_handles(96, depth2_count=12)
    target = handles[-1]
    gateway = gateway_for(
        handles,
        replacements={(target.sku, target.selection_position): JPEG[:-1] + b"x"},
    )
    report, _, _ = execute_case(tmp_path, handles, gateway=gateway)
    assert report["download_summary"]["downloads_verified"] == 95
    assert report["download_summary"]["authoritative_artifacts"] == 0
    assert report["verified_artifacts_before_cleanup"] == 0


def test_068_success_cleanup(tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(5))
    assert report["cleanup_completed"] is True
    assert report["source_files_remaining"] == 0
    assert report["retained_authoritative_artifacts"] == 0
    assert report["download_summary"]["source_files_cleaned"] == 5
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("code", [
    "drive_download_forbidden", "drive_download_not_found", "drive_download_transient_error",
])
def test_069_transport_failure_cleanup(tmp_path, code):
    handles = make_handles(3)
    raw_id = handle_core._provider_file_id_for_download(handles[0])
    error = GoogleDriveContentDownloadError(
        code, transient=code == "drive_download_transient_error", requests_performed=1,
    )
    repetitions = [error, error, error] if error.transient else [error]
    gateway = gateway_for(handles, errors={raw_id: repetitions})
    report, _, _ = execute_case(tmp_path, handles, gateway=gateway)
    assert report["status"] == "blocked"
    assert report["verified_artifacts_before_cleanup"] == 0
    assert report["cleanup_completed"] is True
    assert report["source_files_remaining"] == 0
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["checksum", "size", "signature"])
def test_072_integrity_gates_inherited(tmp_path, kind):
    if kind == "size":
        handle = make_handle(expected_size=len(JPEG) + 1)
        gateway = gateway_for((handle,))
    elif kind == "signature":
        bad = b"not-a-jpeg-signature"
        handle = make_handle(data=bad)
        gateway = gateway_for((handle,), replacements={(handle.sku, 0): bad})
    else:
        handle = make_handle()
        gateway = gateway_for((handle,), replacements={(handle.sku, 0): JPEG[:-1] + b"x"})
    report, _, _ = execute_case(tmp_path, (handle,), gateway=gateway)
    assert report["status"] == "blocked"
    assert report[f"{kind}_mismatch"] == 1
    assert report["verified_artifacts_before_cleanup"] == 0
    assert report["cleanup_completed"] is True


def test_075_expected_and_actual_total_bytes(tmp_path):
    handles = make_handles(5)
    report, _, _ = execute_case(tmp_path, handles)
    expected = sum(item.size_bytes for item in handles)
    assert report["expected_total_source_bytes"] == expected
    assert report["actual_total_source_bytes"] == expected


def test_076_request_counters_are_dynamic(tmp_path):
    handles = make_handles(3)
    gateway = gateway_for(handles, stream_chunk_size=4)
    report, _, _ = execute_case(tmp_path, handles, gateway=gateway)
    expected_requests = sum((item.size_bytes + 3) // 4 for item in handles)
    assert report["download_requests_performed"] == expected_requests
    assert report["network_requests_performed"] == 20 + expected_requests


@pytest.mark.parametrize("field_name", [
    "status", "policy_version", "preparation_summary", "download_summary",
    "expected_total_source_bytes", "actual_total_source_bytes", "selected_items",
    "downloads_verified", "downloads_failed", "checksum_verified", "checksum_mismatch",
    "size_verified", "size_mismatch", "signature_verified", "signature_mismatch",
    "verified_artifacts_before_cleanup", "cleanup_completed", "source_files_remaining",
    "retained_authoritative_artifacts", "network_requests_performed",
    "download_requests_performed", "media_read_requests_performed",
    "conversion_requests_performed", "wordpress_upload_requests_performed",
    "external_write_requests_performed", "write_requests_performed", "warnings",
    "blocking_issues", "results",
])
def test_077_report_schema(tmp_path, field_name):
    report, _, _ = execute_case(tmp_path)
    assert field_name in report


@pytest.mark.parametrize("field_name", [
    "sku", "selection_position", "image_role", "folder_role", "safe_name",
    "file_id_fingerprint", "source_mime_type", "expected_size_bytes",
    "actual_size_bytes", "expected_md5_checksum", "actual_md5_checksum",
    "source_verified", "download_status", "warnings", "blocking_issues",
])
def test_106_result_schema(tmp_path, field_name):
    report, _, _ = execute_case(tmp_path)
    assert field_name in report["results"][0]


@pytest.mark.parametrize("needle", [
    "provider_file_id", "raw_file_id", "raw_folder_id", "provider_resource_id",
    "resource_key", "drive.google.com", "download_url", "local_source_path",
    "temp_directory", str(PROJECT_ROOT), "authorization", "cookie", "access_token",
    "refresh_token", "client_secret", "credentials", "private_key", "client_email",
    "wp_app_password", "opaque_file_000",
])
def test_121_report_forbidden_values(tmp_path, needle):
    report, _, _ = execute_case(tmp_path)
    assert needle.casefold() not in json.dumps(report, sort_keys=True).casefold()


@pytest.mark.parametrize("counter", [
    "media_read_requests_performed", "conversion_requests_performed",
    "wordpress_upload_requests_performed", "external_write_requests_performed",
    "write_requests_performed",
])
def test_141_external_counters_zero(tmp_path, counter):
    report, _, _ = execute_case(tmp_path)
    assert report[counter] == 0


@pytest.mark.parametrize("needle", [
    "from PIL", "import PIL", "ImageMagick", "cwebp", "ffmpeg", ".webp",
    "wp-json", "WooCommerce", "wordpress_client", "media_upload",
])
def test_146_no_conversion_or_wordpress_workflow(needle):
    assert needle.casefold() not in inspect.getsource(execution_core).casefold()


@pytest.mark.parametrize("status,blocked,prepared,selected", [
    ("blocked", 1, 0, 1),
    ("ok", 1, 1, 1),
    ("ok", 0, 0, 1),
    ("ok", 0, 1, 2),
])
def test_156_incomplete_preparation_blocks_content(tmp_path, status, blocked, prepared, selected):
    handles = make_handles(1)
    factory = Factory()
    value = preparation(
        handles, status=status,
        overrides={
            "handles_blocked": blocked,
            "handles_prepared": prepared,
            "selected_items": selected,
        },
    )
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.execute_prepared_media_download_batch(
            value, metadata_settings(), factory,
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert caught.value.code == "execution_preparation_not_authoritative"
    assert factory.content_settings == []


def test_160_content_client_failure_is_safe(tmp_path):
    handles = make_handles(1)
    with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
        execution_core.execute_prepared_media_download_batch(
            preparation(handles), metadata_settings(),
            Factory(error=RuntimeError("unsafe credential and path")),
            workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
        )
    assert caught.value.code == "execution_content_client_creation_failed"
    assert "credential" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_161_preflight_blocked_report_has_zero_content_requests(tmp_path):
    handles = make_handles(1)
    prepared = preparation(handles)
    factory = Factory()
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared):
        report, _ = execution_core.run_secure_media_download_execution(
            Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
            metadata_settings(), factory, project_root=tmp_path,
            workspace_parent=tmp_path,
            disk_usage_reader=capacity_for(handles, -1),
        )
    assert report["status"] == "blocked"
    assert report["blocking_issues"] == ["insufficient_download_workspace_capacity"]
    assert report["download_requests_performed"] == 0
    assert factory.content_settings == []


def test_162_only_execution_report_written(tmp_path):
    handles = make_handles(1)
    prepared = preparation(handles)
    gateway = gateway_for(handles)
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared):
        with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
            report, output = execution_core.run_secure_media_download_execution(
                Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
                metadata_settings(), Factory(), project_root=tmp_path,
                workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
            )
    assert report["status"] == "ok"
    assert output == tmp_path / "reports" / execution_core.REPORT_FILENAME
    assert tuple(path.name for path in (tmp_path / "reports").iterdir()) == (
        execution_core.REPORT_FILENAME,
    )


def test_163_old_reports_unchanged(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    protected = (
        "selected-media-baseline-snapshot.json", "image-selection-dry-run.json",
        "selected-media-handle-preparation.json", "secure-media-download-canary.json",
        "google-drive-folder-manifest-dry-run.json",
        "google-drive-nested-folder-manifest-dry-run.json",
        "google-drive-depth2-folder-manifest-dry-run.json",
    )
    for name in protected:
        (reports / name).write_text("frozen", encoding="utf-8")
    before = {name: (reports / name).read_bytes() for name in protected}
    prepared = preparation(make_handles(1))
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared):
        report, _ = execution_core.run_secure_media_download_execution(
            Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
            metadata_settings(), Factory(), project_root=tmp_path,
            workspace_parent=tmp_path,
            disk_usage_reader=capacity_for(prepared.handles, -1),
        )
    assert report["status"] == "blocked"
    assert before == {name: (reports / name).read_bytes() for name in protected}


def test_164_report_deterministic(tmp_path):
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    first, _, _ = execute_case(first_dir)
    second, _, _ = execute_case(second_dir)
    assert first == second


def test_165_no_artifact_authority_in_report(tmp_path):
    report, _, _ = execute_case(tmp_path)
    assert "artifacts" not in report
    assert report["retained_authoritative_artifacts"] == 0
    assert all("local_source_path" not in item for item in report["results"])


def test_166_execution_passes_one_callback_into_full_batch_core(tmp_path):
    handles = make_handles(5)
    gateway = gateway_for(handles)
    callback = Mock()
    original = download_core.download_secure_media
    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        with patch.object(download_core, "download_secure_media", wraps=original) as mocked:
            batch = execution_core.execute_prepared_media_download_batch(
                preparation(handles), metadata_settings(), Factory(),
                workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
                progress_callback=callback,
            )
    assert mocked.call_count == 1
    assert mocked.call_args.args[0] == handles
    assert mocked.call_args.kwargs["progress_callback"] is callback
    batch.cleanup()


def test_167_execution_progress_does_not_enter_report_authority(tmp_path):
    handles = make_handles(3)
    events = []
    report, _, _ = execute_case(
        tmp_path, handles, progress_callback=events.append,
    )
    assert len(events) == 6
    serialized = json.dumps(report, sort_keys=True)
    assert "download_started" not in serialized
    assert "progress" not in serialized.casefold()
    assert report["downloads_verified"] == 3


def test_168_execution_progress_callback_failure_cleans_and_is_fixed(tmp_path):
    handles = make_handles(3)
    gateway = gateway_for(handles)

    def callback(event):
        if event["current_index"] == 2 and event["status"] == "download_verified":
            raise RuntimeError("unsafe callback details")

    with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
        with pytest.raises(execution_core.SecureMediaDownloadExecutionError) as caught:
            execution_core.execute_prepared_media_download_batch(
                preparation(handles), metadata_settings(), Factory(),
                workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
                progress_callback=callback,
            )
    assert caught.value.code == "execution_download_core_failed"
    assert "unsafe" not in str(caught.value)
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("interrupt_index", [0, 3, 7])
def test_169_execution_keyboard_interrupt_cleanup_and_no_report(tmp_path, interrupt_index):
    handles = make_handles(8)
    target = handles[interrupt_index]
    raw_id = handle_core._provider_file_id_for_download(target)
    gateway = gateway_for(handles, errors={raw_id: [KeyboardInterrupt()]})
    prepared = preparation(handles)
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared):
        with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
            with pytest.raises(KeyboardInterrupt):
                execution_core.run_secure_media_download_execution(
                    Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
                    metadata_settings(), Factory(), project_root=tmp_path,
                    workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
                )
    assert not (tmp_path / "reports" / execution_core.REPORT_FILENAME).exists()
    assert not tuple(tmp_path.iterdir())


def test_172_execution_system_exit_cleanup_and_no_report(tmp_path):
    handles = make_handles(4)
    target = handles[2]
    raw_id = handle_core._provider_file_id_for_download(target)
    gateway = gateway_for(handles, errors={raw_id: [SystemExit(11)]})
    prepared = preparation(handles)
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared):
        with patch.object(execution_core, "GoogleDriveContentGateway", return_value=gateway):
            with pytest.raises(SystemExit) as caught:
                execution_core.run_secure_media_download_execution(
                    Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
                    metadata_settings(), Factory(), project_root=tmp_path,
                    workspace_parent=tmp_path, disk_usage_reader=capacity_for(handles),
                )
    assert caught.value.code == 11
    assert not (tmp_path / "reports" / execution_core.REPORT_FILENAME).exists()
    assert not tuple(tmp_path.iterdir())


def test_173_cli_progress_logs_only_safe_projection(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", metadata_settings)

    def fake_run(*args, **kwargs):
        kwargs["progress_callback"]({
            "current_index": 1,
            "total_items": 96,
            "sku": "MOCK-001",
            "selection_position": 0,
            "status": "download_started",
            "provider_file_id": "raw-secret-id",
            "local_source_path": "C:/secret/source.jpg",
        })
        return ({
            "status": "ok", "selected_items": 1, "downloads_verified": 1,
            "cleanup_completed": True, "source_files_remaining": 0,
            "download_requests_performed": 1,
        }, Path("safe-report.json"))

    monkeypatch.setattr(cli, "run_secure_media_download_execution", fake_run)
    assert cli._run_secure_media_download_execution(
        logger, Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
    ) == 0
    logs = " ".join(call.args[0] for call in logger.info.call_args_list)
    assert "secure_media_download_progress" in logs
    assert "raw-secret-id" not in logs
    assert "source.jpg" not in logs


def test_174_cli_does_not_swallow_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", metadata_settings)
    monkeypatch.setattr(
        cli,
        "run_secure_media_download_execution",
        Mock(side_effect=KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        cli._run_secure_media_download_execution(
            Mock(), Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
        )


def test_175_mock_96_success_with_progress_unchanged(tmp_path):
    handles = make_handles(96, depth2_count=12)
    events = []
    report, gateway, _ = execute_case(
        tmp_path, handles, progress_callback=events.append,
    )
    assert report["status"] == "ok"
    assert report["downloads_verified"] == 96
    assert report["verified_artifacts_before_cleanup"] == 96
    assert report["retained_authoritative_artifacts"] == 0
    assert len(gateway.calls) == 96
    assert len(events) == 192
    assert events[0]["current_index"] == 1
    assert events[-1]["current_index"] == 96
