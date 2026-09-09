from __future__ import annotations

import hashlib
import inspect
import io
import json
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
from sync_worker import verified_webp_conversion_execution as conversion_execution
from sync_worker import wordpress_media_upload_execution as execution_core
from sync_worker import wordpress_media_upload_gate as gate_core
from sync_worker import wordpress_media_upload_transport as transport_core
from sync_worker.config import (
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    GoogleSettings,
    Settings,
)
from sync_worker.google_api import GoogleDriveContentDownloadReceipt
from sync_worker.image_mapping import ProductSourceRange
from sync_worker.report import SafeWriteAuditJsonReportWriter
from sync_worker.selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
)


def image_bytes() -> bytes:
    image = Image.new("RGB", (8, 6), (37, 73, 109))
    output = io.BytesIO()
    image.save(output, format="JPEG")
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


def wp_settings(**overrides) -> Settings:
    values = {
        "wp_base_url": "https://staging-unit-test.wpcomstaging.com",
        "wp_username": "mock-wp-user",
        "wp_app_password": "mock application password",
        "wc_consumer_key": "ck_mock_never_use_12345678901234567890",
        "wc_consumer_secret": "cs_mock_never_use_12345678901234567890",
        "sync_environment": "staging",
        "dry_run": True,
        "default_product_status": "draft",
        "allow_delete": False,
    }
    values.update(overrides)
    return Settings(**values)


def google_settings() -> GoogleSettings:
    return GoogleSettings(
        drive_scope=GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
        sheets_scope=GOOGLE_SHEETS_READONLY_SCOPE,
    )


def make_handle(*, sku: str, position: int, raw_id: str):
    source = ProductSourceRange(10, 20)
    primary = position == 0
    selection = selection_core.ImageSelectionItem(
        sku=sku,
        folder_role=folder_core.FolderRole.STOREFRONT_PHOTOS,
        safe_name=f"supplier-{position}.jpg",
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
        safe_name=f"supplier-{position}.jpg",
        mime_type="image/jpeg",
        size_bytes=len(JPEG),
        modified_time="2026-01-01T00:00:00Z",
        md5_checksum=hashlib.md5(JPEG, usedforsecurity=False).hexdigest(),
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


def make_handles(count: int):
    return tuple(
        make_handle(
            sku=f"MOCK-{index // 12 + 1:03d}",
            position=index % 12,
            raw_id=f"opaque_file_{index:03d}",
        )
        for index in range(count)
    )


def preparation(handles, *, overrides=None):
    handles = tuple(handles)
    summary = {
        "selected_items": len(handles),
        "handles_prepared": len(handles),
        "handles_blocked": 0,
        "nested_handles": len(handles),
        "depth2_handles": 0,
        "primary_handles": sum(x.image_role.value == "primary" for x in handles),
        "gallery_handles": sum(x.image_role.value == "gallery" for x in handles),
        "sheets_read_requests_performed": 1,
        "root_drive_read_requests_performed": 8,
        "depth1_drive_read_requests_performed": 8,
        "depth2_drive_read_requests_performed": 0,
        "network_requests_performed": 17,
    }
    summary.update(overrides or {})
    return SelectedMediaHandlePreparationResult("ok", {"summary": summary}, handles)


class Factory:
    def __init__(self):
        self.drive = object()
        self.content_settings = []

    def create_drive_content_readonly(self, settings):
        self.content_settings.append(settings)
        return self.drive

    def create_drive_metadata_clients(self, settings):
        raise AssertionError("fresh preparation is injected in unit tests")


class Gateway:
    def __init__(self, handles):
        self.content = {
            handle_core._provider_file_id_for_download(handle): JPEG
            for handle in handles
        }
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append(provider_file_id)
        data = self.content[provider_file_id]
        sink.write(data)
        return GoogleDriveContentDownloadReceipt(1, len(data))


def response(status: int, value: object):
    return transport_core.WordPressMediaHttpResponse(
        status, json.dumps(value).encode("utf-8")
    )


def wp_record(slug: str, media_id: int):
    return {
        "id": media_id,
        "slug": slug,
        "mime_type": "image/webp",
        "source_url": (
            "https://staging-unit-test.wpcomstaging.com/wp-content/uploads/"
            f"2026/09/{slug}.webp"
        ),
        "media_details": {"file": f"2026/09/{slug}.webp"},
    }


class WordPressTransport:
    """Deterministic local transport script; never opens a socket."""

    def __init__(self, modes):
        self.modes = tuple(modes)
        self.slug_modes = {}
        self.lookup_numbers = {}
        self.lookup_calls = []
        self.upload_calls = []

    def lookup_media(self, *, slug, authorization):
        self.lookup_calls.append((slug, authorization))
        if slug not in self.slug_modes:
            self.slug_modes[slug] = self.modes[len(self.slug_modes)]
        mode = self.slug_modes[slug]
        call = self.lookup_numbers.get(slug, 0) + 1
        self.lookup_numbers[slug] = call
        if mode == "reused" or (mode == "reconciled" and call == 2):
            return response(200, [wp_record(slug, 1000 + len(self.slug_modes))])
        return response(200, [])

    def upload_media(self, *, slug, upload_filename, body, authorization):
        self.upload_calls.append((slug, upload_filename, body, authorization))
        mode = self.slug_modes[slug]
        if mode == "reconciled":
            raise TimeoutError("synthetic uncertain POST")
        if mode == "timeout":
            raise TimeoutError("synthetic unresolved POST")
        if mode.startswith("fail"):
            return response(int(mode.removeprefix("fail")), {})
        return response(201, wp_record(slug, 2000 + len(self.upload_calls)))


def required_capacity(handles, delta=0):
    required = (
        sum(handle.size_bytes for handle in handles)
        + download_execution.DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES
        + len(handles) * conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES
    )
    return lambda path: SimpleNamespace(free=required + delta)


def execute(tmp_path, modes, *, handles=None, expected=None, settings=None,
            progress_callback=None, capacity_delta=0, gateway=None,
            transport=None, disk_usage_reader=None):
    handles = make_handles(len(modes)) if handles is None else tuple(handles)
    expected = len(handles) if expected is None else expected
    gateway = Gateway(handles) if gateway is None else gateway
    factory = Factory()
    transport = WordPressTransport(modes) if transport is None else transport
    disk_usage_reader = (
        required_capacity(handles, capacity_delta)
        if disk_usage_reader is None
        else disk_usage_reader
    )
    with patch.object(
        download_execution, "GoogleDriveContentGateway", return_value=gateway
    ):
        report = execution_core.execute_wordpress_media_upload_execution(
            preparation(handles),
            expected,
            google_settings(),
            factory,
            wp_settings() if settings is None else settings,
            transport,
            execution_core.EXACT_CONFIRMATION_TOKEN,
            workspace_parent=tmp_path,
            disk_usage_reader=disk_usage_reader,
            progress_callback=progress_callback,
        )
    return report, gateway, factory, transport


def valid_argv():
    return [
        "upload-selected-media-batch",
        "--selection-report", "selection.json",
        "--baseline-snapshot", "baseline.json",
        "--mapping", "mapping.json",
        "--sheet", "RMB Price List",
        "--sku-report", "sku.json",
        "--expected-selected-items", "96",
        "--confirm-staging-media-batch-upload",
        execution_core.EXACT_CONFIRMATION_TOKEN,
    ]


def safe_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_policy_version_and_report_filename():
    assert execution_core.POLICY_VERSION == "xxxxdoll-wordpress-media-upload-execution-v1"
    assert execution_core.REPORT_FILENAME == "wordpress-media-upload-execution.json"


def test_cli_is_registered():
    arguments = cli.build_parser().parse_args(valid_argv())
    assert arguments.command == "upload-selected-media-batch"


@pytest.mark.parametrize(
    "flag",
    [
        "--selection-report", "--baseline-snapshot", "--mapping", "--sheet",
        "--sku-report", "--expected-selected-items",
        "--confirm-staging-media-batch-upload",
    ],
)
def test_all_seven_cli_arguments_are_required(flag):
    argv = valid_argv()
    index = argv.index(flag)
    del argv[index:index + 2]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.mark.parametrize("index", range(100))
def test_confirmation_requires_byte_for_byte_exact_token(index):
    with pytest.raises(
        execution_core.WordPressMediaUploadExecutionError,
        match="staging_media_batch_upload_confirmation_required",
    ):
        execution_core.validate_staging_media_batch_upload_confirmation(
            execution_core.EXACT_CONFIRMATION_TOKEN + str(index)
        )


_INVALID_EXPECTED = (
    None, True, False, 0, -1, -10, "1", "96", 1.0, 96.0, [], {}, (), object(),
) + tuple(
    gate_core.MAX_ARTIFACTS_PER_BATCH + offset for offset in range(1, 31)
)


@pytest.mark.parametrize("value", _INVALID_EXPECTED)
def test_invalid_expected_selected_items_are_rejected(value):
    with pytest.raises(
        execution_core.WordPressMediaUploadExecutionError,
        match="wordpress_media_batch_expected_selected_items_invalid",
    ):
        execution_core.validate_expected_selected_items(value)


@pytest.mark.parametrize("value", [1, 2, 95, 96, gate_core.MAX_ARTIFACTS_PER_BATCH])
def test_valid_expected_selected_items_are_preserved(value):
    assert execution_core.validate_expected_selected_items(value) == value


def test_wrong_cli_confirmation_blocks_before_config_or_client():
    argv = valid_argv()
    argv[-1] = "wrong"
    assert cli.main(argv) == 2


def test_invalid_cli_expected_blocks_before_config_or_client():
    argv = valid_argv()
    argv[argv.index("--expected-selected-items") + 1] = "0"
    assert cli.main(argv) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"wp_base_url": "http://staging-unit-test.wpcomstaging.com"},
        {"wp_base_url": "https://xxxxdoll.com"},
        {"wp_base_url": "https://www.xxxxdoll.com"},
        {"wp_base_url": "https://example.com"},
        {"sync_environment": "production"},
        {"sync_environment": "development"},
        {"dry_run": False},
        {"default_product_status": "publish"},
        {"allow_delete": True},
    ],
)
def test_unsafe_settings_block_before_content_or_wordpress(tmp_path, overrides):
    handles = make_handles(1)
    transport = WordPressTransport(("created",))
    factory = Factory()
    with pytest.raises(
        execution_core.WordPressMediaUploadExecutionError,
        match="wordpress_media_execution_staging_safety_failed",
    ):
        execution_core.execute_wordpress_media_upload_execution(
            preparation(handles), 1, google_settings(), factory,
            wp_settings(**overrides), transport,
            execution_core.EXACT_CONFIRMATION_TOKEN,
            workspace_parent=tmp_path,
            disk_usage_reader=required_capacity(handles),
        )
    assert factory.content_settings == []
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_selected_count_change_blocks_before_content_or_wordpress(tmp_path):
    handles = make_handles(2)
    report, gateway, factory, transport = execute(
        tmp_path, ("created", "created"), handles=handles, expected=3
    )
    assert report["status"] == "blocked"
    assert report["blocking_issues"] == [
        "wordpress_media_batch_selected_item_count_changed"
    ]
    assert gateway.calls == []
    assert factory.content_settings == []
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_capacity_one_byte_short_blocks_before_download_or_wordpress(tmp_path):
    report, gateway, _, transport = execute(
        tmp_path, ("created",), capacity_delta=-1
    )
    assert report["status"] == "blocked"
    assert report["blocking_issues"] == [
        "insufficient_wordpress_media_upload_workspace_capacity"
    ]
    assert gateway.calls == []
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_capacity_exact_enough_allows_content_and_remote_phase(tmp_path):
    report, gateway, _, transport = execute(
        tmp_path, ("reused",), capacity_delta=0
    )
    assert report["status"] == "ok"
    assert len(gateway.calls) == 1
    assert len(transport.lookup_calls) == 1


@pytest.mark.parametrize(
    "bad_size,expected_code",
    [
        (None, "download_preflight_size_missing"),
        (-1, "download_preflight_size_invalid"),
        (True, "download_preflight_size_invalid"),
        (download_core.MAX_SOURCE_FILE_BYTES + 1, "download_preflight_file_too_large"),
    ],
)
def test_bad_expected_source_size_fails_closed_before_clients(
    tmp_path, bad_size, expected_code
):
    handles = make_handles(1)
    gateway = Gateway(handles)
    object.__setattr__(handles[0], "_size_bytes", bad_size)
    report, gateway, factory, transport = execute(
        tmp_path, ("created",), handles=handles,
        gateway=gateway,
        disk_usage_reader=lambda path: SimpleNamespace(free=10**12),
    )
    assert report["status"] == "blocked"
    assert report["blocking_issues"] == [expected_code]
    assert gateway.calls == []
    assert factory.content_settings == []
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_combined_capacity_formula_is_reused():
    source = inspect.getsource(execution_core)
    assert "execute_prepared_webp_conversion_batch" in source
    assert "MAX_WEBP_OUTPUT_FILE_BYTES" not in source
    assert "DOWNLOAD_WORKSPACE_SAFETY_RESERVE_BYTES" in source


def test_full_existing_batch_is_naturally_reused(tmp_path):
    report, gateway, _, transport = execute(
        tmp_path, ("reused", "reused", "reused")
    )
    assert report["status"] == "ok"
    assert report["reused"] == 3
    assert report["created"] == 0
    assert report["write_requests_performed"] == 0
    assert report["references_created"] == 3
    assert len(gateway.calls) == 3
    assert transport.upload_calls == []


def test_reality_shape_one_reused_and_95_created(tmp_path):
    report, _, _, transport = execute(
        tmp_path, ("reused",) + ("created",) * 95
    )
    assert report["status"] == "ok"
    assert report["selected_items"] == 96
    assert report["references_created"] == 96
    assert report["reused"] == 1
    assert report["created"] == 95
    assert report["created_reconciled"] == 0
    assert report["lookup_requests_performed"] == 96
    assert report["upload_requests_performed"] == 95
    assert report["write_requests_performed"] == 95
    assert len(transport.upload_calls) == 95


def test_reality_shape_is_eight_primary_and_88_gallery(tmp_path):
    report, _, _, _ = execute(tmp_path, ("reused",) * 96)
    assert report["preparation_summary"]["primary_handles"] == 8
    assert report["preparation_summary"]["gallery_handles"] == 88


def test_all_96_created_synthetic_batch(tmp_path):
    report, _, _, transport = execute(tmp_path, ("created",) * 96)
    assert report["status"] == "ok"
    assert report["created"] == 96
    assert report["reused"] == 0
    assert report["references_created"] == 96
    assert report["write_requests_performed"] == 96
    assert len(transport.upload_calls) == 96


def test_mixed_created_reused_and_reconciled_batch(tmp_path):
    report, _, _, transport = execute(
        tmp_path, ("reused", "created", "reconciled")
    )
    assert report["status"] == "ok"
    assert report["reused"] == 1
    assert report["created"] == 1
    assert report["created_reconciled"] == 1
    assert report["remote_media_created"] == 2
    assert report["remote_media_reused"] == 1
    assert report["write_requests_performed"] == 2
    assert report["upload_requests_performed"] == 2
    assert report["reconciliation_requests_performed"] == 1
    assert len(transport.upload_calls) == 2


def test_first_remote_failure_stops_later_items(tmp_path):
    report, _, _, transport = execute(
        tmp_path, ("created", "fail403", "created", "created")
    )
    assert report["status"] == "blocked"
    assert report["failed_at_index"] == 2
    assert report["write_requests_performed"] == 2
    assert len(transport.lookup_calls) == 2
    assert len(transport.upload_calls) == 2
    assert [item["upload_status"] for item in report["results"]] == [
        "created", "blocked", "not_attempted", "not_attempted"
    ]


def test_reality_middle_failure_at_21_preserves_20_and_stops_22_to_96(tmp_path):
    report, _, _, transport = execute(
        tmp_path, ("created",) * 20 + ("fail403",) + ("created",) * 75
    )
    assert report["status"] == "blocked"
    assert report["failed_at_index"] == 21
    assert report["references_created"] == 20
    assert report["created"] == 20
    assert report["write_requests_performed"] == 21
    assert len(transport.lookup_calls) == 21
    assert len(transport.upload_calls) == 21
    assert all(
        item["upload_status"] == "not_attempted"
        for item in report["results"][21:]
    )
    assert report["delete_requests_performed"] == 0
    assert report["rollback_requests_performed"] == 0


@pytest.mark.parametrize(
    "modes,failed_index,successful_before",
    [
        (("fail403",) + ("created",) * 2, 1, 0),
        (("created",) * 2 + ("fail403",), 3, 2),
    ],
)
def test_first_and_last_remote_failure_are_fail_stop(
    tmp_path, modes, failed_index, successful_before
):
    report, _, _, transport = execute(tmp_path, modes)
    assert report["failed_at_index"] == failed_index
    assert report["references_created"] == successful_before
    assert len(transport.upload_calls) == failed_index
    assert report["delete_requests_performed"] == 0


@pytest.mark.parametrize("mode", ["timeout", "fail429", "fail500", "fail503"])
def test_uncertain_or_retryable_response_never_reposts(tmp_path, mode):
    report, _, _, transport = execute(tmp_path, (mode,))
    assert report["status"] == "blocked"
    assert report["write_requests_performed"] == 1
    assert report["reconciliation_requests_performed"] == 1
    assert len(transport.lookup_calls) == 2
    assert len(transport.upload_calls) == 1


def test_reconciliation_success_is_one_post_and_one_reference(tmp_path):
    report, _, _, transport = execute(tmp_path, ("reconciled",))
    assert report["status"] == "ok"
    assert report["created_reconciled"] == 1
    assert report["references_created"] == 1
    assert report["write_requests_performed"] == 1
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 2


@pytest.mark.parametrize(
    "mutation",
    ["wrong_mime", "wrong_host", "production_host", "wrong_slug", "filename_collision"],
)
def test_existing_media_response_validation_remains_strict(tmp_path, mutation):
    class UnsafeExistingTransport(WordPressTransport):
        def lookup_media(self, *, slug, authorization):
            self.lookup_calls.append((slug, authorization))
            value = wp_record(slug, 123)
            if mutation == "wrong_mime":
                value["mime_type"] = "image/jpeg"
            elif mutation == "wrong_host":
                value["source_url"] = f"https://example.com/{slug}.webp"
            elif mutation == "production_host":
                value["source_url"] = f"https://xxxxdoll.com/{slug}.webp"
            elif mutation == "wrong_slug":
                value["slug"] = slug + "-1"
            else:
                value["source_url"] = value["source_url"].replace(
                    ".webp", "-1.webp"
                )
                value["media_details"]["file"] = value["media_details"][
                    "file"
                ].replace(".webp", "-1.webp")
            return response(200, [value])

    transport = UnsafeExistingTransport(("reused",))
    report, _, _, _ = execute(
        tmp_path, ("reused",), transport=transport
    )
    assert report["status"] == "blocked"
    assert report["references_created"] == 0
    assert report["write_requests_performed"] == 0
    assert transport.upload_calls == []


@pytest.mark.parametrize(
    "mutation",
    ["wrong_mime", "wrong_host", "production_host", "wrong_slug", "filename_collision"],
)
def test_created_201_response_validation_remains_strict(tmp_path, mutation):
    class UnsafeCreatedTransport(WordPressTransport):
        def upload_media(self, *, slug, upload_filename, body, authorization):
            self.upload_calls.append((slug, upload_filename, body, authorization))
            value = wp_record(slug, 456)
            if mutation == "wrong_mime":
                value["mime_type"] = "image/jpeg"
            elif mutation == "wrong_host":
                value["source_url"] = f"https://example.com/{slug}.webp"
            elif mutation == "production_host":
                value["source_url"] = f"https://xxxxdoll.com/{slug}.webp"
            elif mutation == "wrong_slug":
                value["slug"] = slug + "-1"
            else:
                value["source_url"] = value["source_url"].replace(
                    ".webp", "-1.webp"
                )
                value["media_details"]["file"] = value["media_details"][
                    "file"
                ].replace(".webp", "-1.webp")
            return response(201, value)

    transport = UnsafeCreatedTransport(("created",))
    report, _, _, _ = execute(
        tmp_path, ("created",), transport=transport
    )
    assert report["status"] == "blocked"
    assert report["references_created"] == 0
    assert report["write_requests_performed"] == 1
    assert len(transport.upload_calls) == 1


def test_source_failure_blocks_conversion_gate_and_wordpress(tmp_path):
    handles = make_handles(3)
    gateway = Gateway(handles)
    failed_id = handle_core._provider_file_id_for_download(handles[1])
    gateway.content[failed_id] = JPEG[:-1] + b"x"
    with patch.object(conversion_core, "convert_verified_media_to_webp") as converted, patch.object(
        gate_core, "create_wordpress_media_upload_intents"
    ) as gated:
        report, _, _, transport = execute(
            tmp_path, ("created",) * 3, handles=handles, gateway=gateway
        )
    assert report["status"] == "blocked"
    assert report["download_summary"]["downloads_failed"] == 1
    converted.assert_not_called()
    gated.assert_not_called()
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_conversion_failure_blocks_gate_and_wordpress(tmp_path):
    calls = {"value": 0}
    original = conversion_core._encode_webp

    def fail_second(image, target):
        calls["value"] += 1
        if calls["value"] == 2:
            raise conversion_core._ConversionBlocked("webp_output_write_failed")
        return original(image, target)

    with patch.object(conversion_core, "_encode_webp", side_effect=fail_second), patch.object(
        gate_core, "create_wordpress_media_upload_intents"
    ) as gated:
        report, _, _, transport = execute(tmp_path, ("created",) * 3)
    assert report["status"] == "blocked"
    assert report["conversion_summary"]["conversion_failed"] == 1
    gated.assert_not_called()
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_one_gate_failure_blocks_all_wordpress_authority(tmp_path):
    blocked = SimpleNamespace(
        status="blocked",
        intents=(),
        to_safe_dict=lambda: {
            "summary": {
                "artifacts_received": 3,
                "gate_passed": 2,
                "gate_blocked": 1,
                "intents_created": 0,
            },
            "blocking_issues": ["synthetic_full_gate_blocked"],
        },
    )
    with patch.object(
        gate_core, "create_wordpress_media_upload_intents", return_value=blocked
    ), patch.object(
        transport_core, "_create_staging_application_password_credentials",
        side_effect=AssertionError("credentials forbidden"),
    ):
        report, _, _, transport = execute(tmp_path, ("created",) * 3)
    assert report["status"] == "blocked"
    assert report["blocking_issues"] == ["synthetic_full_gate_blocked"]
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_noncanonical_handle_order_fails_closed_without_resort(tmp_path):
    handles = tuple(reversed(make_handles(3)))
    report, gateway, _, transport = execute(
        tmp_path, ("created",) * 3, handles=handles
    )
    assert report["status"] == "blocked"
    assert report["blocking_issues"] == ["download_handles_not_canonical_order"]
    assert gateway.calls == []
    assert transport.lookup_calls == []


def test_canonical_order_is_preserved_through_gate_and_transport(tmp_path):
    handles = make_handles(5)
    seen = []
    original = transport_core.execute_wordpress_media_uploads

    def wrapped(intents, *args, **kwargs):
        seen.extend((item.sku, item.selection_position) for item in intents)
        return original(intents, *args, **kwargs)

    with patch.object(transport_core, "execute_wordpress_media_uploads", side_effect=wrapped):
        report, _, _, _ = execute(tmp_path, ("reused",) * 5, handles=handles)
    assert report["status"] == "ok"
    assert seen == [(item.sku, item.selection_position) for item in handles]


def test_gate_is_full_batch_and_called_once(tmp_path):
    calls = []
    original = gate_core.create_wordpress_media_upload_intents

    def wrapped(artifacts, settings):
        calls.append(tuple(artifacts))
        return original(artifacts, settings)

    with patch.object(gate_core, "create_wordpress_media_upload_intents", side_effect=wrapped):
        report, _, _, _ = execute(tmp_path, ("reused",) * 4)
    assert report["status"] == "ok"
    assert len(calls) == 1
    assert len(calls[0]) == 4


def test_credentials_and_permit_are_only_created_after_complete_gate(tmp_path):
    order = []
    original_gate = gate_core.create_wordpress_media_upload_intents
    original_credentials = transport_core._create_staging_application_password_credentials
    original_permit = transport_core._create_staging_media_write_permit

    def gate(*args, **kwargs):
        order.append("gate")
        return original_gate(*args, **kwargs)

    def credentials(*args, **kwargs):
        order.append("credentials")
        return original_credentials(*args, **kwargs)

    def permit(*args, **kwargs):
        order.append("permit")
        return original_permit(*args, **kwargs)

    with patch.object(gate_core, "create_wordpress_media_upload_intents", side_effect=gate), patch.object(
        transport_core, "_create_staging_application_password_credentials", side_effect=credentials
    ), patch.object(
        transport_core, "_create_staging_media_write_permit", side_effect=permit
    ):
        report, _, _, _ = execute(tmp_path, ("reused", "reused"))
    assert report["status"] == "ok"
    assert order == ["gate", "credentials", "permit"]


def test_dry_run_stays_true_while_independent_permit_is_used(tmp_path):
    values = []
    original = transport_core._create_staging_media_write_permit

    def wrapped(settings):
        values.append(settings.dry_run)
        return original(settings)

    with patch.object(
        transport_core, "_create_staging_media_write_permit", side_effect=wrapped
    ):
        report, _, _, _ = execute(tmp_path, ("created",))
    assert report["status"] == "ok"
    assert values == [True]


def test_success_cleanup_order_webp_then_source(tmp_path):
    order = []
    webp_cleanup = conversion_core.VerifiedWebPConversionBatchResult.cleanup
    source_cleanup = download_core.SecureMediaDownloadBatchResult.cleanup

    def clean_webp(self):
        order.append("webp")
        return webp_cleanup(self)

    def clean_source(self):
        order.append("source")
        return source_cleanup(self)

    with patch.object(conversion_core.VerifiedWebPConversionBatchResult, "cleanup", clean_webp), patch.object(
        download_core.SecureMediaDownloadBatchResult, "cleanup", clean_source
    ):
        report, _, _, _ = execute(tmp_path, ("created", "reused"))
    assert report["status"] == "ok"
    assert order[-2:] == ["webp", "source"]
    assert report["webp_files_remaining"] == 0
    assert report["source_files_remaining"] == 0


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7)])
def test_baseexception_during_transport_cleans_both_and_reraises(tmp_path, error):
    handles = make_handles(2)
    gateway = Gateway(handles)
    with patch.object(
        download_execution, "GoogleDriveContentGateway", return_value=gateway
    ), patch.object(
        transport_core, "execute_wordpress_media_uploads", side_effect=error
    ):
        with pytest.raises(type(error)) as raised:
            execution_core.execute_wordpress_media_upload_execution(
                preparation(handles), 2, google_settings(), Factory(), wp_settings(),
                WordPressTransport(("created", "created")),
                execution_core.EXACT_CONFIRMATION_TOKEN,
                workspace_parent=tmp_path,
                disk_usage_reader=required_capacity(handles),
            )
    assert raised.value is error
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_custom_baseexception_during_gate_cleans_both_and_reraises(tmp_path):
    class StopNow(BaseException):
        pass

    error = StopNow("stop")
    with patch.object(
        gate_core, "create_wordpress_media_upload_intents", side_effect=error
    ):
        with pytest.raises(StopNow) as raised:
            execute(tmp_path, ("created", "created"))
    assert raised.value is error
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


@pytest.mark.parametrize("stage", ["download", "conversion"])
def test_baseexception_in_local_phase_cleans_and_reraises(tmp_path, stage):
    error = KeyboardInterrupt()
    handles = make_handles(2)
    gateway = Gateway(handles)
    context = (
        patch.object(gateway, "download_file", side_effect=error)
        if stage == "download"
        else patch.object(conversion_core, "_encode_webp", side_effect=error)
    )
    with context:
        with pytest.raises(KeyboardInterrupt) as raised:
            execute(
                tmp_path, ("created", "created"), handles=handles,
                gateway=gateway,
            )
    assert raised.value is error
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_baseexception_in_report_projection_occurs_after_cleanup(tmp_path):
    cleaned = []
    webp_cleanup = conversion_core.VerifiedWebPConversionBatchResult.cleanup
    source_cleanup = download_core.SecureMediaDownloadBatchResult.cleanup

    def clean_webp(self):
        cleaned.append("webp")
        return webp_cleanup(self)

    def clean_source(self):
        cleaned.append("source")
        return source_cleanup(self)

    with patch.object(
        conversion_core.VerifiedWebPConversionBatchResult, "cleanup", clean_webp
    ), patch.object(
        download_core.SecureMediaDownloadBatchResult, "cleanup", clean_source
    ), patch.object(execution_core, "_safe_report", side_effect=KeyboardInterrupt()):
        with pytest.raises(KeyboardInterrupt):
            execute(tmp_path, ("created",))
    assert cleaned[-2:] == ["webp", "source"]
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_interruption_after_one_remote_write_keeps_remote_and_never_deletes(tmp_path):
    seen = {"uploads_started": 0}

    def progress(event):
        if event["stage"] == "wordpress_media" and event["status"] == "upload_started":
            seen["uploads_started"] += 1
            if seen["uploads_started"] == 2:
                raise KeyboardInterrupt()

    transport = WordPressTransport(("created", "created", "created"))
    with pytest.raises(KeyboardInterrupt):
        execute(
            tmp_path, ("created", "created", "created"),
            progress_callback=progress, transport=transport,
        )
    assert len(transport.upload_calls) == 1
    assert not hasattr(transport, "delete_media")
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_complete_96_item_mock_batch(tmp_path):
    report, gateway, _, transport = execute(tmp_path, ("reused",) * 96)
    assert report["status"] == "ok"
    assert report["selected_items"] == 96
    assert report["references_created"] == 96
    assert report["reused"] == 96
    assert len(report["results"]) == 96
    assert len(gateway.calls) == 96
    assert len(transport.lookup_calls) == 96
    assert transport.upload_calls == []


def test_top_level_write_count_matches_transport_posts(tmp_path):
    report, _, _, transport = execute(
        tmp_path, ("created", "reused", "created", "reconciled")
    )
    assert report["write_requests_performed"] == len(transport.upload_calls)
    assert report["write_requests_performed"] == report["transport_summary"][
        "write_requests_performed"
    ]
    assert report["upload_requests_performed"] == len(transport.upload_calls)


def test_write_audit_mismatch_fails_closed():
    summary = execution_core._zero(execution_core._TRANSPORT_FIELDS)
    summary["wordpress_upload_requests_performed"] = 1
    with pytest.raises(
        execution_core.WordPressMediaUploadExecutionError,
        match="wordpress_media_execution_write_audit_mismatch",
    ):
        execution_core._safe_report(
            status="blocked", selected_items=1, expected_selected_items=1,
            preparation_summary=execution_core._zero(execution_core._PREPARATION_FIELDS),
            capacity_preflight=execution_core._empty_capacity(),
            download_summary=execution_core._zero(execution_core._DOWNLOAD_FIELDS),
            conversion_summary=execution_core._zero(execution_core._CONVERSION_FIELDS),
            gate_summary=execution_core._zero(execution_core._GATE_FIELDS),
            transport_summary=summary, results=[],
            webp_cleanup_completed=True, webp_files_remaining=0,
            source_cleanup_completed=True, source_files_remaining=0,
            retained_webp_artifacts=0, retained_download_artifacts=0,
        )


@pytest.mark.parametrize(
    "field",
    [
        "status", "policy_version", "selected_items", "expected_selected_items",
        "capacity_preflight", "preparation_summary", "download_summary",
        "conversion_summary", "gate_summary", "transport_summary",
        "lookup_requests_performed", "upload_requests_performed",
        "reconciliation_requests_performed", "write_requests_performed",
        "network_requests_performed", "remote_media_created",
        "remote_media_reused", "created", "reused", "created_reconciled",
        "references_created", "failed_at_index", "webp_cleanup_completed",
        "webp_files_remaining", "source_cleanup_completed",
        "source_files_remaining", "retained_webp_artifacts",
        "retained_download_artifacts", "delete_requests_performed",
        "rollback_requests_performed", "woocommerce_requests_performed",
        "woocommerce_write_requests_performed", "warnings", "blocking_issues",
        "results",
    ],
)
def test_report_contains_required_field(tmp_path, field):
    report, _, _, _ = execute(tmp_path, ("reused",))
    assert field in report


@pytest.mark.parametrize(
    "field",
    [
        "sku", "selection_position", "image_role", "media_identity",
        "upload_filename", "wordpress_slug", "wordpress_media_id",
        "upload_status", "mime_type", "warnings", "blocking_issues",
    ],
)
def test_result_uses_allowlisted_schema(tmp_path, field):
    report, _, _, _ = execute(tmp_path, ("reused",))
    assert set(report["results"][0]) == set(execution_core._RESULT_FIELDS)
    assert field in report["results"][0]


@pytest.mark.parametrize(
    "forbidden",
    [
        "mock-wp-user", "mock application password", "Authorization", "Basic ",
        "Cookie", "nonce", "https://staging-unit-test.wpcomstaging.com",
        "source_url", "local_webp_path", "local_source_path", "workspace_root",
        "provider_file_id", "opaque_file_000", "ck_mock_never_use",
        "cs_mock_never_use", "private_key", "client_email", "refresh_token",
        str(PROJECT_ROOT), "wp-content/uploads",
    ],
)
def test_report_does_not_contain_secret_url_path_or_authority(tmp_path, forbidden):
    report, _, _, _ = execute(tmp_path, ("created",))
    assert forbidden not in safe_text(report)


@pytest.mark.parametrize(
    "counter",
    [
        "delete_requests_performed", "rollback_requests_performed",
        "woocommerce_requests_performed", "woocommerce_write_requests_performed",
    ],
)
def test_forbidden_write_counters_are_zero(tmp_path, counter):
    report, _, _, _ = execute(tmp_path, ("created",))
    assert report[counter] == 0


def test_report_results_do_not_contain_target_fingerprint(tmp_path):
    report, _, _, _ = execute(tmp_path, ("reused",))
    assert "target_fingerprint" not in report["results"][0]


def test_progress_is_allowlisted_and_ordered(tmp_path):
    events = []
    report, _, _, _ = execute(
        tmp_path, ("created", "reused"), progress_callback=events.append
    )
    assert report["status"] == "ok"
    allowed = {
        "current_index", "total_items", "sku", "selection_position",
        "stage", "status",
    }
    assert events
    assert all(set(event) == allowed for event in events)
    assert events[-1]["stage"] == "cleanup"
    assert events[-1]["status"] == "cleanup_completed"


def test_no_report_can_restore_live_authority():
    signature = inspect.signature(
        execution_core.execute_wordpress_media_upload_execution
    )
    for name in (
        "download_report", "conversion_report", "gate_report", "upload_report",
        "transport_report",
    ):
        assert name not in signature.parameters


def test_no_parallel_delete_rollback_or_woocommerce_surface():
    source = inspect.getsource(execution_core)
    assert "ThreadPool" not in source
    assert "asyncio" not in source
    assert "delete_media" not in source
    assert "rollback_media" not in source
    assert "woocommerce_product" not in source


def test_cli_dispatches_every_input_and_safe_callback(monkeypatch):
    captured = {}
    report = {
        "status": "ok", "selected_items": 96, "expected_selected_items": 96,
        "lookup_requests_performed": 96, "upload_requests_performed": 0,
        "reconciliation_requests_performed": 0, "write_requests_performed": 0,
        "references_created": 96, "failed_at_index": None,
        "webp_cleanup_completed": True, "source_cleanup_completed": True,
    }
    monkeypatch.setattr(cli, "load_config", lambda: wp_settings())
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", google_settings)
    monkeypatch.setattr(cli, "OfficialGoogleClientFactory", lambda: object())
    monkeypatch.setattr(cli, "StdlibWordPressMediaHttpTransport", lambda value: object())

    def run(*args, **kwargs):
        captured["args"] = args
        captured["callback"] = kwargs["progress_callback"]
        return report, Path("ignored.json")

    monkeypatch.setattr(cli, "run_wordpress_media_upload_execution", run)
    assert cli.main(valid_argv()) == 0
    assert captured["args"][:7] == (
        Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
        "RMB Price List", Path("sku.json"), 96,
        execution_core.EXACT_CONFIRMATION_TOKEN,
    )
    captured["callback"]({
        "current_index": 1, "total_items": 96, "sku": "MOCK-001",
        "selection_position": 0, "stage": "download", "status": "started",
        "provider_file_id": "must-not-be-logged",
    })


@pytest.mark.parametrize("status,expected", [("ok", 0), ("blocked", 1), ("failed", 2)])
def test_cli_exit_status(status, expected, monkeypatch):
    report = {
        "status": status, "selected_items": 96, "expected_selected_items": 96,
        "lookup_requests_performed": 0, "upload_requests_performed": 0,
        "reconciliation_requests_performed": 0, "write_requests_performed": 0,
        "references_created": 0, "failed_at_index": None,
        "webp_cleanup_completed": True, "source_cleanup_completed": True,
    }
    monkeypatch.setattr(cli, "load_config", lambda: wp_settings())
    monkeypatch.setattr(cli, "load_google_drive_metadata_config", google_settings)
    monkeypatch.setattr(cli, "OfficialGoogleClientFactory", lambda: object())
    monkeypatch.setattr(cli, "StdlibWordPressMediaHttpTransport", lambda value: object())
    monkeypatch.setattr(
        cli, "run_wordpress_media_upload_execution",
        lambda *args, **kwargs: (report, Path("ignored.json")),
    )
    assert cli.main(valid_argv()) == expected


def test_run_uses_fresh_preparation_and_safe_write_audit(tmp_path):
    handles = make_handles(2)
    prep = preparation(handles)
    gateway = Gateway(handles)
    transport = WordPressTransport(("created", "reused"))
    with patch.object(
        execution_core, "prepare_selected_media_handles", return_value=prep
    ) as fresh, patch.object(
        download_execution, "GoogleDriveContentGateway", return_value=gateway
    ):
        report, output = execution_core.run_wordpress_media_upload_execution(
            Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
            "RMB Price List", Path("sku.json"), 2,
            execution_core.EXACT_CONFIRMATION_TOKEN,
            google_settings(), Factory(), wp_settings(), transport,
            project_root=tmp_path, workspace_parent=tmp_path,
            disk_usage_reader=required_capacity(handles),
        )
    assert fresh.call_count == 1
    assert output == tmp_path / "reports" / execution_core.REPORT_FILENAME
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == report
    assert saved["write_requests_performed"] == 1


def test_execution_uses_safe_write_audit_writer():
    source = inspect.getsource(execution_core.run_wordpress_media_upload_execution)
    assert "SafeWriteAuditJsonReportWriter" in source
    assert "SafeJsonReportWriter" not in source


def test_write_audit_writer_preserves_nonzero_count(tmp_path):
    output = tmp_path / "audit.json"
    SafeWriteAuditJsonReportWriter(output, execution_core.Redactor()).write(
        {"status": "ok", "write_requests_performed": 3}
    )
    assert json.loads(output.read_text(encoding="utf-8"))[
        "write_requests_performed"
    ] == 3


def test_readme_documents_full_batch_command_and_exact_confirmation():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "upload-selected-media-batch" in readme
    assert "--expected-selected-items" in readme
    assert execution_core.EXACT_CONFIRMATION_TOKEN in readme
    assert "reports/wordpress-media-upload-execution.json" in readme


def test_production_does_not_hardcode_existing_media_id_or_reality_count():
    source = inspect.getsource(execution_core)
    assert "18169" not in source
    assert "selected_items == 96" not in source
    assert "expected_selected_items == 96" not in source


def test_execution_report_counts_actual_lookup_retry_attempts(tmp_path):
    class RetryThenCreateTransport(WordPressTransport):
        def lookup_media(self, *, slug, authorization):
            if not self.lookup_calls:
                self.lookup_calls.append((slug, authorization))
                raise TimeoutError("synthetic transient lookup")
            return super().lookup_media(slug=slug, authorization=authorization)

    transport = RetryThenCreateTransport(("created",))
    with patch.object(transport_core, "_sleep_lookup_backoff", return_value=None):
        report, _, _, _ = execute(
            tmp_path, ("created",), transport=transport
        )
    assert report["status"] == "ok"
    assert report["lookup_requests_performed"] == 2
    assert report["transport_summary"]["lookup_requests_performed"] == 2
    assert report["upload_requests_performed"] == 1
    assert report["write_requests_performed"] == 1
    assert len(transport.lookup_calls) == 2
    assert len(transport.upload_calls) == 1
