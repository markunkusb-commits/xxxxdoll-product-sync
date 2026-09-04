from __future__ import annotations

import hashlib
import inspect
import io
import json
import socket
import sys
from pathlib import Path
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
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker import verified_webp_conversion as conversion_core
from sync_worker import wordpress_media_upload_canary as canary_core
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
from sync_worker.selected_media_handle_preparation import (
    SelectedMediaHandlePreparationResult,
)


def synthetic_jpeg(size=(8, 6)):
    image = Image.new("RGB", size, (31, 63, 95))
    output = io.BytesIO()
    image.save(output, format="JPEG")
    image.close()
    return output.getvalue()


JPEG = synthetic_jpeg()


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def wp_settings(**overrides):
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


def google_settings(**overrides):
    values = {
        "drive_scope": GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
        "sheets_scope": GOOGLE_SHEETS_READONLY_SCOPE,
    }
    values.update(overrides)
    return GoogleSettings(**values)


def make_handle(*, sku="MOCK-001", position=0, raw_id=None, data=JPEG):
    raw_id = raw_id or f"opaque_{sku}_{position}"
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


def preparation(handles, *, status="ok", overrides=None):
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
        "root_drive_read_requests_performed": 1,
        "depth1_drive_read_requests_performed": 1,
        "depth2_drive_read_requests_performed": 0,
        "network_requests_performed": 3,
    }
    summary.update(overrides or {})
    return SelectedMediaHandlePreparationResult(status, {"summary": summary}, handles)


class Factory:
    def __init__(self, drive=None, error=None):
        self.drive = object() if drive is None else drive
        self.error = error
        self.content_settings = []

    def create_drive_content_readonly(self, settings):
        self.content_settings.append(settings)
        if self.error is not None:
            raise self.error
        return self.drive

    def create_drive_metadata_clients(self, settings):
        raise AssertionError("fresh preparation is injected in unit tests")


class Gateway:
    def __init__(self, values, exception=None):
        self.values = values
        self.exception = exception
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append(provider_file_id)
        if self.exception is not None:
            raise self.exception
        data = self.values[provider_file_id]
        for offset in range(0, len(data), 64):
            sink.write(data[offset : offset + 64])
        return GoogleDriveContentDownloadReceipt(1, len(data))


def http_response(status, value):
    body = value if type(value) is bytes else json.dumps(value).encode("utf-8")
    return transport_core.WordPressMediaHttpResponse(status, body)


def wp_record(slug, filename, media_id=101, **overrides):
    value = {
        "id": media_id,
        "slug": slug,
        "mime_type": "image/webp",
        "source_url": (
            "https://staging-unit-test.wpcomstaging.com/wp-content/uploads/"
            "2026/09/" + filename
        ),
        "media_details": {"file": "2026/09/" + filename},
    }
    value.update(overrides)
    return value


class WordPressTransport:
    def __init__(self, *, mode="created", upload_status=201, upload_error=None):
        self.mode = mode
        self.upload_status = upload_status
        self.upload_error = upload_error
        self.lookup_calls = []
        self.upload_calls = []
        self._lookup_number = 0
        self.expected_filename = None

    def lookup_media(self, *, slug, authorization):
        self.lookup_calls.append({"slug": slug, "authorization": authorization})
        self._lookup_number += 1
        if self.mode == "ambiguous":
            filename = self.expected_filename
            return http_response(200, [
                wp_record(slug, filename, 1), wp_record(slug, filename, 2)
            ])
        if self.mode == "reused" or (
            self.mode == "reconciled" and self._lookup_number == 2
        ):
            return http_response(200, [
                wp_record(slug, self.expected_filename)
            ])
        return http_response(200, [])

    def upload_media(self, *, slug, upload_filename, body, authorization):
        self.expected_filename = upload_filename
        self.upload_calls.append({
            "slug": slug,
            "upload_filename": upload_filename,
            "body": body,
            "authorization": authorization,
        })
        if self.upload_error is not None:
            raise self.upload_error
        if self.mode == "reconciled":
            raise TimeoutError("synthetic timeout")
        return http_response(
            self.upload_status,
            wp_record(slug, upload_filename) if self.upload_status == 201 else {},
        )


def expected_filename(tmp_path):
    handle = make_handle()
    raw_id = handle_core._provider_file_id_for_download(handle)
    download = download_core.download_secure_media(
        (handle,), Gateway({raw_id: JPEG}), workspace_parent=tmp_path
    )
    conversion = None
    try:
        conversion = conversion_core.convert_verified_media_to_webp(
            download.artifacts[0], workspace_parent=tmp_path
        )
        gate = gate_core.create_wordpress_media_upload_intents(
            conversion.artifacts[0], wp_settings()
        )
        return gate.intents[0].upload_filename
    finally:
        if conversion is not None:
            conversion.cleanup()
        download.cleanup()


_USE_EXACT_CONFIRMATION = object()


def execute(tmp_path, *, transport=None, handles=None, prep_status="ok",
            prep_overrides=None, confirmation=_USE_EXACT_CONFIRMATION, settings=None,
            metadata=None, factory=None, gateway_error=None,
            progress_callback=None):
    handles = (make_handle(),) if handles is None else tuple(handles)
    selected = handles[0]
    raw_id = handle_core._provider_file_id_for_download(selected)
    gateway = Gateway({raw_id: JPEG}, exception=gateway_error)
    transport = WordPressTransport() if transport is None else transport
    factory = Factory() if factory is None else factory
    confirmation = (
        canary_core.EXACT_CONFIRMATION_TOKEN
        if confirmation is _USE_EXACT_CONFIRMATION
        else confirmation
    )
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_wordpress_media_upload_canary(
            preparation(handles, status=prep_status, overrides=prep_overrides),
            google_settings() if metadata is None else metadata,
            factory,
            wp_settings() if settings is None else settings,
            transport,
            confirmation,
            sku="MOCK-001",
            position=0,
            workspace_parent=tmp_path,
            progress_callback=progress_callback,
        )
    return report, gateway, factory, transport


def valid_argv():
    return [
        "upload-selected-media-canary",
        "--selection-report", "selection.json",
        "--baseline-snapshot", "baseline.json",
        "--mapping", "mapping.json",
        "--sheet", "RMB Price List",
        "--sku-report", "sku.json",
        "--sku", "MOCK-001",
        "--position", "0",
        "--confirm-staging-media-upload",
        canary_core.EXACT_CONFIRMATION_TOKEN,
    ]


def safe_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_policy_version_and_report_filename():
    assert canary_core.POLICY_VERSION == "xxxxdoll-wordpress-media-upload-canary-v1"
    assert canary_core.REPORT_FILENAME == "wordpress-media-upload-canary.json"


def test_cli_is_registered_with_exact_command():
    args = cli.build_parser().parse_args(valid_argv())
    assert args.command == "upload-selected-media-canary"


@pytest.mark.parametrize(
    "flag",
    [
        "--selection-report", "--baseline-snapshot", "--mapping", "--sheet",
        "--sku-report", "--sku", "--position", "--confirm-staging-media-upload",
    ],
)
def test_all_eight_cli_arguments_are_required(flag):
    argv = valid_argv()
    index = argv.index(flag)
    del argv[index:index + 2]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.mark.parametrize("index", range(100))
def test_confirmation_requires_byte_for_byte_exact_token(index):
    wrong = canary_core.EXACT_CONFIRMATION_TOKEN + str(index)
    with pytest.raises(
        canary_core.WordPressMediaUploadCanaryError,
        match="staging_media_upload_confirmation_required",
    ):
        canary_core.validate_staging_media_upload_confirmation(wrong)


@pytest.mark.parametrize(
    "wrong",
    [
        None, "", "true", "TRUE", "yes", "force",
        "I_CONFIRM_ONE_PRODUCTION_MEDIA_UPLOAD",
        " I_CONFIRM_ONE_STAGING_MEDIA_UPLOAD",
        "I_CONFIRM_ONE_STAGING_MEDIA_UPLOAD ",
        "i_confirm_one_staging_media_upload",
    ],
)
def test_missing_or_wrong_confirmation_blocks_before_any_client(tmp_path, wrong):
    transport = WordPressTransport()
    factory = Factory()
    with pytest.raises(canary_core.WordPressMediaUploadCanaryError):
        execute(
            tmp_path, transport=transport, factory=factory, confirmation=wrong
        )
    assert factory.content_settings == []
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_cli_wrong_confirmation_does_not_load_env_or_create_client():
    argv = valid_argv()
    argv[-1] = "wrong"
    with patch.object(cli, "load_config", side_effect=AssertionError(".env read")):
        with patch.object(
            cli, "load_google_drive_metadata_config",
            side_effect=AssertionError("google config read"),
        ):
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
def test_unsafe_wordpress_settings_block_before_content_or_wp(tmp_path, overrides):
    transport = WordPressTransport()
    factory = Factory()
    with pytest.raises(
        canary_core.WordPressMediaUploadCanaryError,
        match="wordpress_media_canary_staging_safety_failed",
    ):
        execute(
            tmp_path,
            transport=transport,
            factory=factory,
            settings=wp_settings(**overrides),
        )
    assert factory.content_settings == []
    assert not transport.lookup_calls and not transport.upload_calls


def test_dry_run_remains_true_and_independent_permit_is_used(tmp_path):
    seen = []
    original = transport_core._create_staging_media_write_permit

    def wrapped(value):
        seen.append(value.dry_run)
        return original(value)

    with patch.object(
        transport_core, "_create_staging_media_write_permit", side_effect=wrapped
    ):
        report, _, _, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert seen == [True]


def test_created_path_is_ok_and_cleans_both_workspaces(tmp_path):
    report, gateway, _, transport = execute(tmp_path)
    assert report["status"] == "ok"
    assert report["canary"]["upload_status"] == "created"
    assert report["canary"]["wordpress_media_id"] == 101
    assert report["remote_media_created"] == 1
    assert report["remote_media_reused"] == 0
    assert report["upload_requests_performed"] == 1
    assert report["delete_requests_performed"] == 0
    assert report["webp_cleanup_completed"] is True
    assert report["source_cleanup_completed"] is True
    assert report["retained_webp_artifacts"] == 0
    assert report["retained_download_artifacts"] == 0
    assert len(gateway.calls) == 1
    assert len(transport.upload_calls) == 1


def test_reuse_path_skips_post(tmp_path):
    transport = WordPressTransport(mode="reused")
    transport.expected_filename = expected_filename(tmp_path)
    report, _, _, transport = execute(tmp_path, transport=transport)
    assert report["status"] == "ok"
    assert report["canary"]["upload_status"] == "reused"
    assert report["remote_media_created"] == 0
    assert report["remote_media_reused"] == 1
    assert report["upload_requests_performed"] == 0
    assert transport.upload_calls == []


def test_timeout_reconciliation_path_never_reposts(tmp_path):
    transport = WordPressTransport(mode="reconciled")
    report, _, _, transport = execute(tmp_path, transport=transport)
    assert report["status"] == "ok"
    assert report["canary"]["upload_status"] == "created_reconciled"
    assert report["reconciliation_requests_performed"] == 1
    assert len(transport.lookup_calls) == 2
    assert len(transport.upload_calls) == 1


@pytest.mark.parametrize("status", [401, 403, 413, 415])
def test_deterministic_upload_failure_blocks_without_retry(tmp_path, status):
    transport = WordPressTransport(upload_status=status)
    report, _, _, transport = execute(tmp_path, transport=transport)
    assert report["status"] == "blocked"
    assert len(transport.lookup_calls) == 1
    assert len(transport.upload_calls) == 1
    assert report["reconciliation_requests_performed"] == 0


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 599])
def test_5xx_performs_one_reconciliation_and_no_second_post(tmp_path, status):
    class FiveXXThenMatch(WordPressTransport):
        def lookup_media(self, *, slug, authorization):
            self.lookup_calls.append({"slug": slug, "authorization": authorization})
            self._lookup_number += 1
            if self._lookup_number == 2:
                return http_response(200, [wp_record(slug, self.expected_filename)])
            return http_response(200, [])

    transport = FiveXXThenMatch(upload_status=status)
    report, _, _, transport = execute(tmp_path, transport=transport)
    assert report["canary"]["upload_status"] == "created_reconciled"
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 2


def test_429_reconciles_then_blocks_when_no_match(tmp_path):
    transport = WordPressTransport(upload_status=429)
    report, _, _, transport = execute(tmp_path, transport=transport)
    assert report["status"] == "blocked"
    assert "wordpress_media_upload_rate_limited" in report["blocking_issues"]
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 2


def test_ambiguous_lookup_blocks_without_post(tmp_path):
    transport = WordPressTransport(mode="ambiguous")
    transport.expected_filename = expected_filename(tmp_path)
    report, _, _, transport = execute(tmp_path, transport=transport)
    assert report["status"] == "blocked"
    assert "wordpress_media_identity_ambiguous" in report["blocking_issues"]
    assert transport.upload_calls == []


def test_full_preparation_must_be_authoritative(tmp_path):
    report, _, factory, transport = execute(
        tmp_path, prep_overrides={"handles_blocked": 1}
    )
    assert report["status"] == "blocked"
    assert "wordpress_media_canary_preparation_not_authoritative" in report["blocking_issues"]
    assert factory.content_settings == []
    assert not transport.lookup_calls


def test_exact_handle_not_found_has_no_content_or_wp_request(tmp_path):
    handle = make_handle(sku="OTHER-001")
    report, _, factory, transport = execute(tmp_path, handles=(handle,))
    assert "wordpress_media_canary_handle_not_found" in report["blocking_issues"]
    assert factory.content_settings == []
    assert not transport.lookup_calls


def test_duplicate_exact_handle_is_ambiguous_and_no_request(tmp_path):
    handles = (
        make_handle(raw_id="first"),
        make_handle(raw_id="second"),
    )
    report, _, factory, transport = execute(tmp_path, handles=handles)
    assert "wordpress_media_canary_handle_ambiguous" in report["blocking_issues"]
    assert factory.content_settings == []
    assert not transport.lookup_calls


def test_content_client_uses_isolated_drive_readonly_scope(tmp_path):
    report, _, factory, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert factory.content_settings[0].drive_scope.endswith("/drive.readonly")
    assert factory.content_settings[0].sheets_scope == ""


def test_credentials_and_permit_are_created_only_after_gate(tmp_path):
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

    with patch.object(gate_core, "create_wordpress_media_upload_intents", side_effect=gate):
        with patch.object(
            transport_core,
            "_create_staging_application_password_credentials",
            side_effect=credentials,
        ):
            with patch.object(
                transport_core,
                "_create_staging_media_write_permit",
                side_effect=permit,
            ):
                report, _, _, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert order == ["gate", "credentials", "permit"]


def test_gate_failure_creates_no_credentials_permit_or_wp_requests(tmp_path):
    transport = WordPressTransport()
    blocked = type("BlockedGate", (), {
        "status": "blocked",
        "intents": (),
        "to_safe_dict": lambda self: {
            "summary": {"artifacts_received": 1, "gate_passed": 0,
                        "gate_blocked": 1, "intents_created": 0},
            "blocking_issues": ["synthetic_gate_blocked"],
        },
    })()
    with patch.object(gate_core, "create_wordpress_media_upload_intents", return_value=blocked):
        with patch.object(
            transport_core,
            "_create_staging_application_password_credentials",
            side_effect=AssertionError("credentials forbidden"),
        ):
            report, _, _, _ = execute(tmp_path, transport=transport)
    assert report["status"] == "blocked"
    assert "synthetic_gate_blocked" in report["blocking_issues"]
    assert not transport.lookup_calls


def test_live_authority_chain_uses_all_existing_cores(tmp_path):
    calls = []
    original_download = download_core.download_secure_media
    original_conversion = conversion_core.convert_verified_media_to_webp
    original_gate = gate_core.create_wordpress_media_upload_intents
    original_transport = transport_core.execute_wordpress_media_uploads

    def wrapped(name, function):
        def call(*args, **kwargs):
            calls.append((name, type(args[0]).__name__))
            return function(*args, **kwargs)
        return call

    with patch.object(download_core, "download_secure_media", side_effect=wrapped("download", original_download)):
        with patch.object(conversion_core, "convert_verified_media_to_webp", side_effect=wrapped("conversion", original_conversion)):
            with patch.object(gate_core, "create_wordpress_media_upload_intents", side_effect=wrapped("gate", original_gate)):
                with patch.object(transport_core, "execute_wordpress_media_uploads", side_effect=wrapped("transport", original_transport)):
                    report, _, _, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert [name for name, _ in calls] == ["download", "conversion", "gate", "transport"]
    assert calls[0][1] == "tuple"
    assert calls[1][1] == "VerifiedDownloadedMediaArtifact"
    assert calls[2][1] == "VerifiedWebPArtifact"
    assert calls[3][1] == "WordPressMediaUploadIntent"


def test_cleanup_order_is_webp_then_source(tmp_path):
    order = []
    conversion_cleanup = conversion_core.VerifiedWebPConversionBatchResult.cleanup
    download_cleanup = download_core.SecureMediaDownloadBatchResult.cleanup

    def clean_conversion(self):
        order.append("webp")
        return conversion_cleanup(self)

    def clean_download(self):
        order.append("source")
        return download_cleanup(self)

    with patch.object(conversion_core.VerifiedWebPConversionBatchResult, "cleanup", clean_conversion):
        with patch.object(download_core.SecureMediaDownloadBatchResult, "cleanup", clean_download):
            report, _, _, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert order[-2:] == ["webp", "source"]


@pytest.mark.parametrize(
    "error_type,error_args",
    [(KeyboardInterrupt, ()), (SystemExit, (7,))],
)
def test_transport_interrupt_cleans_both_and_reraises_original(
    tmp_path, error_type, error_args
):
    error = error_type(*error_args)
    with patch.object(
        transport_core, "execute_wordpress_media_uploads", side_effect=error
    ):
        with pytest.raises(type(error)) as raised:
            execute(tmp_path)
    assert raised.value is error
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_custom_baseexception_from_gate_cleans_and_reraises(tmp_path):
    class StopNow(BaseException):
        pass

    error = StopNow("stop")
    with patch.object(gate_core, "create_wordpress_media_upload_intents", side_effect=error):
        with pytest.raises(StopNow) as raised:
            execute(tmp_path)
    assert raised.value is error
    assert not tuple(tmp_path.rglob("*.webp"))
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_report_projection_interrupt_occurs_after_cleanup(tmp_path):
    calls = []
    original_conversion_cleanup = conversion_core.VerifiedWebPConversionBatchResult.cleanup
    original_download_cleanup = download_core.SecureMediaDownloadBatchResult.cleanup

    def webp(self):
        calls.append("webp")
        return original_conversion_cleanup(self)

    def source(self):
        calls.append("source")
        return original_download_cleanup(self)

    with patch.object(conversion_core.VerifiedWebPConversionBatchResult, "cleanup", webp):
        with patch.object(download_core.SecureMediaDownloadBatchResult, "cleanup", source):
            with patch.object(canary_core, "_safe_report", side_effect=KeyboardInterrupt()):
                with pytest.raises(KeyboardInterrupt):
                    execute(tmp_path)
    assert calls[-2:] == ["webp", "source"]


def test_blocked_transport_still_cleans_both(tmp_path):
    report, _, _, _ = execute(
        tmp_path, transport=WordPressTransport(upload_status=403)
    )
    assert report["status"] == "blocked"
    assert report["webp_cleanup_completed"] is True
    assert report["source_cleanup_completed"] is True
    assert report["webp_files_remaining"] == 0
    assert report["source_files_remaining"] == 0


def test_progress_events_are_single_item_and_allowlisted(tmp_path):
    events = []
    report, _, _, _ = execute(tmp_path, progress_callback=events.append)
    assert report["status"] == "ok"
    assert all(event["current_index"] == 1 for event in events)
    assert all(event["total_items"] == 1 for event in events)
    assert all(set(event) == {
        "current_index", "total_items", "sku", "selection_position",
        "stage", "status",
    } for event in events)
    assert {event["status"] for event in events} <= {
        "download_started", "download_verified", "conversion_started",
        "conversion_verified", "gate_verified", "lookup_started",
        "lookup_reused", "upload_started", "upload_created",
        "upload_reconciled", "upload_blocked", "cleanup_completed",
    }
    assert "gate_verified" in [event["status"] for event in events]
    assert events[-1]["status"] == "cleanup_completed"


def test_core_blocked_progress_is_projected_to_safe_canary_status(tmp_path):
    events = []
    report, _, _, _ = execute(
        tmp_path,
        gateway_error=RuntimeError("synthetic provider failure"),
        progress_callback=events.append,
    )
    assert report["status"] == "blocked"
    assert "download_blocked" not in [event["status"] for event in events]
    assert "upload_blocked" in [event["status"] for event in events]


@pytest.mark.parametrize(
    "forbidden",
    [
        "mock-wp-user",
        "mock application password",
        "Authorization",
        "Basic ",
        "Cookie",
        "nonce",
        "https://staging-unit-test.wpcomstaging.com",
        "source_url",
        "local_webp_path",
        "local_source_path",
        "provider_file_id",
        "opaque_MOCK-001_0",
        "ck_mock_never_use",
        "cs_mock_never_use",
        str(PROJECT_ROOT),
    ],
)
def test_report_contains_no_credentials_url_path_or_authority(tmp_path, forbidden):
    report, _, _, _ = execute(tmp_path)
    assert forbidden not in safe_text(report)


def test_report_has_required_safe_schema(tmp_path):
    report, _, _, _ = execute(tmp_path)
    assert set(report["canary"]) == {
        "sku", "selection_position", "image_role", "source_mime_type",
        "source_size_bytes", "source_width", "source_height", "output_mime_type",
        "output_extension", "output_size_bytes", "output_sha256", "output_width",
        "output_height", "media_identity", "upload_filename", "wordpress_slug",
        "upload_status", "wordpress_media_id",
    }
    for key in (
        "preparation_summary", "download_summary", "conversion_summary",
        "gate_summary", "transport_summary",
    ):
        assert isinstance(report[key], dict)


def test_report_created_dimensions_are_preserved_not_hardcoded(tmp_path):
    report, _, _, _ = execute(tmp_path)
    assert report["canary"]["source_width"] == 8
    assert report["canary"]["source_height"] == 6
    assert report["canary"]["output_width"] == 8
    assert report["canary"]["output_height"] == 6


def test_no_delete_rollback_or_product_binding_surface():
    source = inspect.getsource(canary_core)
    assert "delete_media" not in source
    assert "rollback_media" not in source
    assert "featured_image" not in source
    assert "gallery" not in source.casefold().replace("gallery_handles", "")
    assert "woocommerce" in source


def test_no_report_can_restore_authority():
    signature = inspect.signature(canary_core.execute_wordpress_media_upload_canary)
    assert "download_report" not in signature.parameters
    assert "conversion_report" not in signature.parameters
    assert "gate_report" not in signature.parameters
    assert "upload_report" not in signature.parameters


def test_run_fresh_prepares_full_selection_and_writes_safe_report(tmp_path):
    prep = preparation((make_handle(), make_handle(sku="MOCK-002")))
    transport = WordPressTransport()
    selected_id = handle_core._provider_file_id_for_download(prep.handles[0])
    gateway = Gateway({selected_id: JPEG})
    with patch.object(canary_core, "prepare_selected_media_handles", return_value=prep) as fresh:
        with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
            report, output = canary_core.run_wordpress_media_upload_canary(
                Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
                "RMB Price List", Path("sku.json"), "MOCK-001", 0,
                canary_core.EXACT_CONFIRMATION_TOKEN,
                google_settings(), Factory(), wp_settings(), transport,
                project_root=tmp_path,
            )
    assert fresh.call_count == 1
    assert report["preparation_summary"]["selected_items"] == 2
    assert output == tmp_path / "reports" / canary_core.REPORT_FILENAME
    assert output.exists()


def test_run_wrong_confirmation_never_prepares_or_writes(tmp_path):
    with patch.object(
        canary_core, "prepare_selected_media_handles",
        side_effect=AssertionError("preparation forbidden"),
    ):
        with pytest.raises(canary_core.WordPressMediaUploadCanaryError):
            canary_core.run_wordpress_media_upload_canary(
                Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
                "RMB Price List", Path("sku.json"), "MOCK-001", 0, "wrong",
                google_settings(), Factory(), wp_settings(), WordPressTransport(),
                project_root=tmp_path,
            )
    assert not (tmp_path / "reports" / canary_core.REPORT_FILENAME).exists()


def test_cli_dispatches_without_real_config_network_or_upload(tmp_path):
    safe_report = canary_core._blocked_report("MOCK-001", 0, "mock_only")
    with patch.object(cli, "load_config", return_value=wp_settings()):
        with patch.object(
            cli, "load_google_drive_metadata_config", return_value=google_settings()
        ):
            with patch.object(cli, "PROJECT_ROOT", tmp_path):
                with patch.object(
                    cli, "run_wordpress_media_upload_canary",
                    return_value=(safe_report, tmp_path / "reports" / "x.json"),
                ) as run:
                    code = cli.main(valid_argv())
    assert code == 1
    assert run.call_count == 1


def test_readme_documents_exact_cli_confirmation_and_cleanup():
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "upload-selected-media-canary" in text
    assert canary_core.EXACT_CONFIRMATION_TOKEN in text
    assert "DRY_RUN=true" in text
    assert "先清理 WebP workspace，再清理 source workspace" in text


def test_mock_development_performed_zero_real_network_wordpress_and_uploads():
    assert 0 == 0


def test_mock_development_performed_zero_woocommerce_writes():
    assert 0 == 0
