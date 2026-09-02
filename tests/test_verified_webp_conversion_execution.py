from __future__ import annotations

import hashlib
import inspect
import io
import json
import pickle
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import image_selection_policy as selection_core
from sync_worker import secure_media_download as download_core
from sync_worker import secure_media_download_execution as download_execution
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker import verified_webp_conversion as conversion_core
from sync_worker import verified_webp_conversion_execution as execution_core
from sync_worker.config import (
    GOOGLE_DRIVE_CONTENT_READONLY_SCOPE,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    GoogleSettings,
)
from sync_worker.google_api import GoogleDriveContentDownloadReceipt
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
)


def image_bytes(image_format="JPEG", *, size=(8, 6)):
    image = Image.new("RGB", size, (29, 71, 113))
    output = io.BytesIO()
    image.save(output, format=image_format)
    image.close()
    return output.getvalue()


JPEG = image_bytes()


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in (
        "load_config",
        "load_google_config",
        "load_google_drive_metadata_config",
        "load_google_sheets_readonly_config",
    ):
        monkeypatch.setattr(cli, name, denied)


def make_handle(*, sku="MOCK-001", position=0, data=JPEG, raw_id=None):
    raw_id = raw_id or f"opaque_{sku}_{position}"
    safe_name = f"supplier-{position}.jpg"
    sku_number = int(sku.rsplit("-", 1)[-1])
    source = ProductSourceRange(sku_number * 10, sku_number * 10 + 5)
    primary = position == 0
    selection = selection_core.ImageSelectionItem(
        sku=sku,
        folder_role=folder_core.FolderRole.STOREFRONT_PHOTOS,
        safe_name=safe_name,
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
            if primary
            else selection_core.ImageSelectionRole.GALLERY
        ),
        selection_reason=(
            selection_core.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY
            if primary
            else selection_core.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY
        ),
    )
    item = root_core.DriveManifestItem(
        safe_name=safe_name,
        mime_type="image/jpeg",
        size_bytes=len(data),
        modified_time="2026-01-01T00:00:00Z",
        md5_checksum=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        file_id_fingerprint=root_core.fingerprint_drive_id(raw_id),
        item_kind="image_candidate",
        image_candidate=True,
        image_candidate_status="drive_metadata_image_candidate",
        image_width=8,
        image_height=6,
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


def make_handles(count):
    return tuple(
        make_handle(
            sku=f"MOCK-{index // 12 + 1:03d}",
            position=index % 12,
            raw_id=f"opaque_file_{index:03d}",
        )
        for index in range(count)
    )


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
        "root_drive_read_requests_performed": 8,
        "depth1_drive_read_requests_performed": 8,
        "depth2_drive_read_requests_performed": 0,
        "network_requests_performed": 17,
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
        self.drive = object()

    def create_drive_content_readonly(self, settings):
        self.content_settings.append(settings)
        if self.error is not None:
            raise self.error
        return self.drive

    def create_drive_metadata_clients(self, settings):
        raise AssertionError("fresh preparation is mocked")


class FakeGateway:
    def __init__(self, content, *, failure_id=None, exception=None):
        self.content = content
        self.failure_id = failure_id
        self.exception = exception
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append((provider_file_id, chunk_size))
        if provider_file_id == self.failure_id and self.exception is not None:
            raise self.exception
        data = self.content[provider_file_id]
        for offset in range(0, len(data), 64):
            sink.write(data[offset : offset + 64])
        return GoogleDriveContentDownloadReceipt(1, len(data))


def gateway_for(handles, *, corrupt_index=None, exception_index=None, exception=None):
    content = {}
    failure_id = None
    for index, handle in enumerate(handles):
        raw_id = handle_core._provider_file_id_for_download(handle)
        content[raw_id] = JPEG[:-1] + b"x" if index == corrupt_index else JPEG
        if index == exception_index:
            failure_id = raw_id
    return FakeGateway(content, failure_id=failure_id, exception=exception)


def required_capacity(handles, *, delta=0):
    total = sum(item.size_bytes for item in handles)
    required = (
        total
        + download_execution.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
        + len(handles) * conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES
    )
    return lambda path: SimpleNamespace(free=required + delta)


def execute_case(
    tmp_path,
    handles=None,
    *,
    gateway=None,
    capacity_delta=0,
    download_progress_callback=None,
    conversion_progress_callback=None,
):
    handles = make_handles(2) if handles is None else tuple(handles)
    gateway = gateway_for(handles) if gateway is None else gateway
    factory = Factory()
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_webp_conversion_batch(
            preparation(handles),
            metadata_settings(),
            factory,
            workspace_parent=tmp_path,
            disk_usage_reader=required_capacity(handles, delta=capacity_delta),
            download_progress_callback=download_progress_callback,
            conversion_progress_callback=conversion_progress_callback,
        )
        report = execution_core.finalize_webp_conversion_execution(batch)
    return report, gateway, factory


def valid_argv():
    return [
        "convert-selected-media-batch",
        "--selection-report", "selection.json",
        "--baseline-snapshot", "baseline.json",
        "--mapping", "mapping.json",
        "--sheet", "RMB Price List",
        "--sku-report", "sku.json",
    ]


def test_policy_version():
    assert execution_core.POLICY_VERSION == "xxxxdoll-verified-webp-conversion-execution-v1"


def test_report_filename():
    assert execution_core.REPORT_FILENAME == "verified-webp-conversion-execution.json"


def test_cli_registered():
    assert "convert-selected-media-batch" in cli.build_parser().format_help()
    assert cli.build_parser().parse_args(valid_argv()).command == "convert-selected-media-batch"


@pytest.mark.parametrize(
    "missing",
    ["--selection-report", "--baseline-snapshot", "--mapping", "--sheet", "--sku-report"],
)
def test_five_cli_arguments_are_required(missing):
    argv = valid_argv()
    index = argv.index(missing)
    del argv[index : index + 2]
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize("attribute", ["sku", "position"])
def test_batch_cli_has_no_canary_selector(attribute):
    assert not hasattr(cli.build_parser().parse_args(valid_argv()), attribute)


@pytest.mark.parametrize("status,expected", [("ok", 0), ("blocked", 1), ("failed", 2)])
def test_cli_exit_codes(status, expected, monkeypatch):
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", metadata_settings)
    report = {
        "status": status,
        "selected_items": 2,
        "downloads_verified": 2,
        "conversion_verified": 2,
        "source_cleanup_completed": True,
        "source_files_remaining": 0,
        "webp_cleanup_completed": True,
        "webp_files_remaining": 0,
    }
    monkeypatch.setattr(
        cli,
        "run_verified_webp_conversion_execution",
        lambda *args, **kwargs: (report, Path("mock.json")),
    )
    assert cli.main(valid_argv()) == expected


def test_cli_dispatches_all_inputs_and_callbacks(monkeypatch):
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", metadata_settings)
    captured = {}

    def run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ({
            "status": "ok", "selected_items": 0, "downloads_verified": 0,
            "conversion_verified": 0, "source_cleanup_completed": True,
            "source_files_remaining": 0, "webp_cleanup_completed": True,
            "webp_files_remaining": 0,
        }, Path("mock.json"))

    monkeypatch.setattr(cli, "run_verified_webp_conversion_execution", run)
    assert cli.main(valid_argv()) == 0
    assert captured["args"][:5] == (
        Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
        "RMB Price List", Path("sku.json"),
    )
    assert callable(captured["kwargs"]["download_progress_callback"])
    assert callable(captured["kwargs"]["conversion_progress_callback"])


def test_combined_capacity_formula(tmp_path):
    handles = make_handles(3)
    preflight = execution_core.preflight_webp_conversion_workspace(
        handles,
        workspace_parent=tmp_path,
        disk_usage_reader=required_capacity(handles),
    )
    assert preflight.expected_total_source_bytes == 3 * len(JPEG)
    assert preflight.maximum_webp_output_bytes == (
        3 * conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES
    )
    assert preflight.required_capacity_bytes == (
        3 * len(JPEG)
        + 3 * conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES
        + download_execution.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
    )


def test_combined_capacity_exact_value_passes(tmp_path):
    handles = make_handles(1)
    assert execution_core.preflight_webp_conversion_workspace(
        handles,
        workspace_parent=tmp_path,
        disk_usage_reader=required_capacity(handles),
    ).maximum_webp_output_bytes == conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES


def test_combined_capacity_one_byte_short_blocks_before_download(tmp_path):
    handles = make_handles(2)
    factory = Factory()
    with patch.object(download_core, "download_secure_media") as download:
        with pytest.raises(execution_core.VerifiedWebPConversionExecutionError) as caught:
            execution_core.execute_prepared_webp_conversion_batch(
                preparation(handles), metadata_settings(), factory,
                workspace_parent=tmp_path,
                disk_usage_reader=required_capacity(handles, delta=-1),
            )
    assert caught.value.code == "insufficient_webp_conversion_workspace_capacity"
    download.assert_not_called()
    assert factory.content_settings == []


@pytest.mark.parametrize(
    "reader",
    [
        lambda path: (_ for _ in ()).throw(OSError("unsafe path")),
        lambda path: SimpleNamespace(free=None),
        lambda path: SimpleNamespace(free=-1),
        lambda path: SimpleNamespace(free=True),
    ],
)
def test_capacity_probe_failure_is_safe(tmp_path, reader):
    with pytest.raises(execution_core.VerifiedWebPConversionExecutionError) as caught:
        execution_core.preflight_webp_conversion_workspace(
            make_handles(1), workspace_parent=tmp_path, disk_usage_reader=reader,
        )
    assert "capacity" in caught.value.code
    assert str(tmp_path) not in str(caught.value)


def test_full_download_core_and_conversion_core_each_called_once(tmp_path):
    handles = make_handles(4)
    gateway = gateway_for(handles)
    original_download = download_core.download_secure_media
    original_conversion = conversion_core.convert_verified_media_to_webp
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        with patch.object(download_core, "download_secure_media", wraps=original_download) as downloaded:
            with patch.object(conversion_core, "convert_verified_media_to_webp", wraps=original_conversion) as converted:
                batch = execution_core.execute_prepared_webp_conversion_batch(
                    preparation(handles), metadata_settings(), Factory(),
                    workspace_parent=tmp_path,
                    disk_usage_reader=required_capacity(handles),
                )
                assert downloaded.call_count == 1
                assert downloaded.call_args.args[0] == handles
                assert converted.call_count == 1
                assert converted.call_args.args[0] == batch.download_artifacts
                batch.cleanup()


def test_transient_execution_batch_retains_both_authorities(tmp_path):
    handles = make_handles(3)
    gateway = gateway_for(handles)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_webp_conversion_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
        )
    assert len(batch.download_artifacts) == 3
    assert len(batch.webp_artifacts) == 3
    batch.cleanup()
    assert batch.download_artifacts == ()
    assert batch.webp_artifacts == ()


@pytest.mark.parametrize("serializer", [pickle.dumps, lambda value: value.__reduce__()])
def test_execution_authority_is_not_serializable(tmp_path, serializer):
    handles = make_handles(1)
    gateway = gateway_for(handles)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_webp_conversion_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
        )
    with pytest.raises(TypeError):
        serializer(batch)
    batch.cleanup()


def test_success_cleanup_order_webp_then_source(tmp_path):
    order = []
    handles = make_handles(2)
    original_webp = conversion_core.VerifiedWebPConversionBatchResult.cleanup
    original_source = download_core.SecureMediaDownloadBatchResult.cleanup

    def webp_cleanup(self):
        order.append("webp")
        return original_webp(self)

    def source_cleanup(self):
        order.append("source")
        return original_source(self)

    with patch.object(conversion_core.VerifiedWebPConversionBatchResult, "cleanup", webp_cleanup):
        with patch.object(download_core.SecureMediaDownloadBatchResult, "cleanup", source_cleanup):
            report, _, _ = execute_case(tmp_path, handles)
    assert order[-2:] == ["webp", "source"]
    assert report["webp_cleanup_completed"] is True
    assert report["source_cleanup_completed"] is True


def test_double_cleanup_is_idempotent(tmp_path):
    handles = make_handles(2)
    gateway = gateway_for(handles)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_webp_conversion_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
        )
    batch.cleanup()
    first_download = batch.download_batch.download_result.to_safe_report_dict()
    first_webp = batch.conversion_result.to_safe_report_dict()
    batch.cleanup()
    assert batch.download_batch.download_result.to_safe_report_dict() == first_download
    assert batch.conversion_result.to_safe_report_dict() == first_webp
    assert not tuple(tmp_path.iterdir())


def test_96_jpeg_full_success(tmp_path):
    handles = make_handles(96)
    report, gateway, _ = execute_case(tmp_path, handles)
    assert report["status"] == "ok"
    assert report["selected_items"] == 96
    assert report["preparation_summary"]["primary_handles"] == 8
    assert report["preparation_summary"]["gallery_handles"] == 88
    assert report["downloads_verified"] == 96
    assert report["downloads_failed"] == 0
    assert report["conversion_attempted"] == 96
    assert report["conversion_verified"] == 96
    assert report["conversion_failed"] == 0
    assert report["converted_from_jpeg"] == 96
    assert report["decode_verified"] == 96
    assert report["dimension_verified"] == 96
    assert report["webp_signature_verified"] == 96
    assert report["webp_decode_verified"] == 96
    assert report["verified_webp_artifacts_before_cleanup"] == 96
    assert report["source_files_remaining"] == 0
    assert report["webp_files_remaining"] == 0
    assert report["retained_download_artifacts"] == 0
    assert report["retained_webp_artifacts"] == 0
    assert len(gateway.calls) == 96
    assert not tuple(tmp_path.iterdir())


def test_total_bytes_and_compression_ratio_are_audit_only(tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(3))
    assert report["source_total_bytes"] == 3 * len(JPEG)
    assert report["output_total_bytes"] > 0
    assert report["compression_ratio"] == round(
        report["output_total_bytes"] / report["source_total_bytes"], 8
    )


def test_dimensions_preserved_and_sha_valid_for_every_item(tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(12))
    for item in report["results"]:
        assert (item["source_width"], item["source_height"]) == (8, 6)
        assert (item["output_width"], item["output_height"]) == (8, 6)
        assert item["output_mime_type"] == "image/webp"
        assert item["output_extension"] == ".webp"
        assert conversion_core._valid_sha256(item["output_sha256"])
        assert item["webp_verified"] is True


@pytest.mark.parametrize("failure_index", [0, 95])
def test_download_first_and_final_failure_block_conversion(tmp_path, failure_index):
    handles = make_handles(96)
    gateway = gateway_for(handles, corrupt_index=failure_index)
    with patch.object(conversion_core, "convert_verified_media_to_webp") as converted:
        report, _, _ = execute_case(tmp_path, handles, gateway=gateway)
    converted.assert_not_called()
    assert report["status"] == "blocked"
    assert report["downloads_failed"] == 1
    assert report["conversion_attempted"] == 0
    assert report["verified_webp_artifacts_before_cleanup"] == 0
    assert report["source_files_remaining"] == 0
    assert report["webp_files_remaining"] == 0
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("failure_index", [0, 47, 95])
def test_conversion_first_middle_final_failure_all_or_nothing(tmp_path, failure_index):
    handles = make_handles(96)
    original = conversion_core._encode_webp
    counter = {"value": 0}

    def fail_at(image, target):
        current = counter["value"]
        counter["value"] += 1
        if current == failure_index:
            raise conversion_core._ConversionBlocked("webp_output_write_failed")
        return original(image, target)

    with patch.object(conversion_core, "_encode_webp", side_effect=fail_at):
        report, _, _ = execute_case(tmp_path, handles)
    assert report["status"] == "blocked"
    assert report["conversion_failed"] == 1
    assert report["verified_webp_artifacts_before_cleanup"] == 0
    assert report["retained_webp_artifacts"] == 0
    assert report["source_files_remaining"] == 0
    assert report["webp_files_remaining"] == 0
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(7)])
def test_interruption_during_download_cleans_and_rethrows(tmp_path, exception):
    handles = make_handles(2)
    gateway = gateway_for(handles, exception_index=0, exception=exception)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        with pytest.raises(type(exception)):
            execution_core.execute_prepared_webp_conversion_batch(
                preparation(handles), metadata_settings(), Factory(),
                workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
            )
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(9)])
def test_interruption_during_conversion_cleans_both_and_rethrows(tmp_path, exception):
    handles = make_handles(3)
    gateway = gateway_for(handles)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        with patch.object(conversion_core, "_encode_webp", side_effect=exception):
            with pytest.raises(type(exception)):
                execution_core.execute_prepared_webp_conversion_batch(
                    preparation(handles), metadata_settings(), Factory(),
                    workspace_parent=tmp_path,
                    disk_usage_reader=required_capacity(handles),
                )
    assert not tuple(tmp_path.iterdir())


def test_custom_baseexception_during_conversion_cleans_both(tmp_path):
    class StopNow(BaseException):
        pass

    handles = make_handles(2)
    gateway = gateway_for(handles)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        with patch.object(conversion_core, "_encode_webp", side_effect=StopNow()):
            with pytest.raises(StopNow):
                execution_core.execute_prepared_webp_conversion_batch(
                    preparation(handles), metadata_settings(), Factory(),
                    workspace_parent=tmp_path,
                    disk_usage_reader=required_capacity(handles),
                )
    assert not tuple(tmp_path.iterdir())


def test_report_projection_interruption_cleans_both(tmp_path):
    handles = make_handles(2)
    gateway = gateway_for(handles)
    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        batch = execution_core.execute_prepared_webp_conversion_batch(
            preparation(handles), metadata_settings(), Factory(),
            workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
        )
    with patch.object(execution_core, "_safe_results", side_effect=KeyboardInterrupt()):
        with pytest.raises(KeyboardInterrupt):
            execution_core.finalize_webp_conversion_execution(batch)
    assert batch.download_artifacts == ()
    assert batch.webp_artifacts == ()
    assert not tuple(tmp_path.iterdir())


def test_conversion_progress_has_only_safe_fields(tmp_path):
    events = []
    report, _, _ = execute_case(
        tmp_path,
        make_handles(3),
        conversion_progress_callback=lambda event: events.append(dict(event)),
    )
    assert report["status"] == "ok"
    assert len(events) == 6
    assert {event["status"] for event in events} == {
        "conversion_started", "conversion_verified",
    }
    assert all(set(event) == {
        "current_index", "total_items", "sku", "selection_position",
        "stage", "status",
    } for event in events)
    assert all(event["stage"] == "conversion" for event in events)


def test_download_progress_is_staged_and_safe(tmp_path):
    events = []
    report, _, _ = execute_case(
        tmp_path,
        make_handles(2),
        download_progress_callback=lambda event: events.append(dict(event)),
    )
    assert report["status"] == "ok"
    assert len(events) == 4
    assert {event["status"] for event in events} == {
        "download_started", "download_verified",
    }
    assert all(set(event) == {
        "current_index", "total_items", "sku", "selection_position",
        "stage", "status",
    } for event in events)
    assert all(event["stage"] == "download" for event in events)


def test_conversion_blocked_progress_is_emitted(tmp_path):
    events = []

    def fail(image, target):
        raise conversion_core._ConversionBlocked("webp_output_write_failed")

    with patch.object(conversion_core, "_encode_webp", side_effect=fail):
        report, _, _ = execute_case(
            tmp_path,
            make_handles(2),
            conversion_progress_callback=lambda event: events.append(dict(event)),
        )
    assert report["status"] == "blocked"
    assert [event["status"] for event in events] == [
        "conversion_started", "conversion_blocked",
    ]


@pytest.mark.parametrize(
    "forbidden",
    ["provider_file_id", "raw_file_id", "path", "safe_name", "md5", "sha", "url"],
)
def test_conversion_progress_does_not_leak_authority(tmp_path, forbidden):
    events = []
    execute_case(
        tmp_path,
        make_handles(1),
        conversion_progress_callback=lambda event: events.append(dict(event)),
    )
    assert forbidden not in json.dumps(events).casefold()


@pytest.mark.parametrize("exception", [RuntimeError("callback failed"), KeyboardInterrupt()])
def test_conversion_progress_failure_cleans_both(tmp_path, exception):
    handles = make_handles(2)
    gateway = gateway_for(handles)

    def fail(event):
        if event["status"] == "conversion_started":
            raise exception

    with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
        with pytest.raises((conversion_core.VerifiedWebPConversionError, KeyboardInterrupt)):
            execution_core.execute_prepared_webp_conversion_batch(
                preparation(handles), metadata_settings(), Factory(),
                workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
                conversion_progress_callback=fail,
            )
    assert not tuple(tmp_path.iterdir())


def test_run_reuses_fresh_preparation_and_writes_only_execution_report(tmp_path):
    handles = make_handles(2)
    prepared = preparation(handles)
    gateway = gateway_for(handles)
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=prepared) as prep:
        with patch.object(download_execution, "GoogleDriveContentGateway", return_value=gateway):
            report, output = execution_core.run_verified_webp_conversion_execution(
                Path("selection"), Path("baseline"), Path("mapping"), "Mock Sheet",
                Path("sku"), metadata_settings(), Factory(), project_root=tmp_path,
                workspace_parent=tmp_path, disk_usage_reader=required_capacity(handles),
            )
    prep.assert_called_once()
    assert report["status"] == "ok"
    assert output == tmp_path / "reports" / execution_core.REPORT_FILENAME
    assert tuple(path.name for path in output.parent.iterdir()) == (
        execution_core.REPORT_FILENAME,
    )


def test_run_does_not_write_report_on_baseexception(tmp_path):
    handles = make_handles(1)
    with patch.object(execution_core, "prepare_selected_media_handles", return_value=preparation(handles)):
        with patch.object(
            execution_core,
            "execute_prepared_webp_conversion_batch",
            side_effect=KeyboardInterrupt(),
        ):
            with pytest.raises(KeyboardInterrupt):
                execution_core.run_verified_webp_conversion_execution(
                    Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
                    metadata_settings(), Factory(), project_root=tmp_path,
                )
    assert not (tmp_path / "reports" / execution_core.REPORT_FILENAME).exists()


@pytest.mark.parametrize(
    "field_name",
    [
        "status", "policy_version", "selected_items", "capacity_preflight",
        "preparation_summary", "download_summary", "conversion_summary",
        "source_total_bytes", "output_total_bytes", "compression_ratio",
        "downloads_verified", "downloads_failed", "checksum_verified",
        "checksum_mismatch", "source_size_verified", "source_size_mismatch",
        "source_signature_verified", "source_signature_mismatch",
        "conversion_attempted", "conversion_verified", "conversion_failed",
        "converted_from_jpeg", "converted_from_png", "validated_existing_webp",
        "decode_verified", "decode_failed", "dimension_verified",
        "dimension_mismatch", "webp_signature_verified",
        "webp_signature_mismatch", "webp_decode_verified", "webp_decode_failed",
        "verified_webp_artifacts_before_cleanup", "source_cleanup_completed",
        "source_files_remaining", "webp_cleanup_completed", "webp_files_remaining",
        "retained_download_artifacts", "retained_webp_artifacts",
        "network_requests_performed", "download_requests_performed",
        "conversion_requests_performed", "wordpress_upload_requests_performed",
        "external_write_requests_performed", "write_requests_performed",
        "warnings", "blocking_issues", "results",
    ],
)
def test_report_schema(field_name, tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(1))
    assert field_name in report


@pytest.mark.parametrize(
    "field_name",
    [
        "sku", "selection_position", "image_role", "folder_role", "safe_name",
        "source_mime_type", "source_size_bytes", "source_md5_checksum",
        "source_width", "source_height", "conversion_action",
        "encoder_profile_version", "output_mime_type", "output_extension",
        "output_size_bytes", "output_sha256", "output_width", "output_height",
        "compression_ratio", "webp_verified", "conversion_status", "warnings",
        "blocking_issues",
    ],
)
def test_item_audit_schema(field_name, tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(1))
    assert field_name in report["results"][0]


@pytest.mark.parametrize(
    "needle",
    [
        "provider_file_id", "raw_file_id", "provider_resource_id", "resource_key",
        "drive.google.com", "download_url", "local_source_path", "local_webp_path",
        "temp_directory", "authorization", "cookie", "access_token",
        "refresh_token", "client_secret", "private_key", "client_email",
        "wp_app_password", "opaque_file_000", str(PROJECT_ROOT), "credentials",
    ],
)
def test_report_contains_no_authority_path_or_secret(needle, tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(1))
    assert needle.casefold() not in json.dumps(report, sort_keys=True).casefold()


@pytest.mark.parametrize(
    "counter",
    [
        "wordpress_upload_requests_performed", "external_write_requests_performed",
        "write_requests_performed",
    ],
)
def test_forbidden_write_counters_are_zero(counter, tmp_path):
    report, _, _ = execute_case(tmp_path, make_handles(2))
    assert report[counter] == 0


@pytest.mark.parametrize(
    "forbidden",
    [
        "secure-media-download-execution.json",
        "verified-webp-conversion-canary.json",
        "json.load(", "requests.post", "wp-json", "woocommerce",
    ],
)
def test_execution_source_has_no_report_authority_restore_or_upload(forbidden):
    assert forbidden.casefold() not in inspect.getsource(execution_core).casefold()


def test_production_code_does_not_hardcode_reality_count_or_ratio():
    source = inspect.getsource(execution_core)
    assert "== 96" not in source
    assert "7.57" not in source


def test_report_deterministic_for_same_synthetic_input(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first, _, _ = execute_case(one, make_handles(2))
    second, _, _ = execute_case(two, make_handles(2))
    assert first == second


def test_readme_documents_batch_command_and_cleanup_order():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Verified WebP Conversion Execution V1", 1)[1]
    section = section.split("### Secure Media Download Execution V1", 1)[0]
    assert "convert-selected-media-batch" in section
    assert "insufficient_webp_conversion_workspace_capacity" in section
    assert "先 cleanup WebP workspace，再 cleanup source workspace" in section
    assert "WordPress" in section
