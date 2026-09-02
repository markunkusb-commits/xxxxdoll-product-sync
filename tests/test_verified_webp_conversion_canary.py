from __future__ import annotations

import hashlib
import inspect
import io
import json
import socket
import sys
from pathlib import Path
from unittest.mock import ANY, patch

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
from sync_worker import verified_webp_conversion_canary as canary_core
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


def synthetic_jpeg(
    *,
    size: tuple[int, int] = (8, 6),
    exif: bool = False,
    gps: bool = False,
) -> bytes:
    image = Image.new("RGB", size, (31, 63, 95))
    output = io.BytesIO()
    kwargs: dict[str, object] = {}
    if exif or gps:
        metadata = image.getexif()
        if exif:
            metadata[274] = 6
            metadata[270] = "synthetic supplier comment"
        if gps:
            gps_ifd = metadata.get_ifd(0x8825)
            gps_ifd[1] = "N"
            gps_ifd[2] = (1.0, 2.0, 3.0)
            metadata[0x8825] = gps_ifd
        kwargs["exif"] = metadata
    image.save(output, format="JPEG", **kwargs)
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


def make_handle(
    *,
    sku: str = "MOCK-001",
    position: int = 0,
    data: bytes = JPEG,
    raw_id: str | None = None,
    safe_name: str | None = None,
    width: int = 8,
    height: int = 6,
):
    raw_id = raw_id or f"opaque_{sku}_{position}"
    safe_name = safe_name or f"supplier-{position}.jpg"
    source = ProductSourceRange(10, 20)
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
        image_width=width,
        image_height=height,
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
    def __init__(self, drive=None, *, error=None):
        self.drive = object() if drive is None else drive
        self.error = error
        self.content_settings = []
        self.metadata_settings = []

    def create_drive_content_readonly(self, settings):
        self.content_settings.append(settings)
        if self.error is not None:
            raise self.error
        return self.drive

    def create_drive_metadata_clients(self, settings):
        self.metadata_settings.append(settings)
        raise AssertionError("fresh preparation is mocked in unit tests")


class FakeGateway:
    def __init__(self, content, *, exception=None):
        self.content = content
        self.exception = exception
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append((provider_file_id, chunk_size))
        if self.exception is not None:
            raise self.exception
        data = self.content[provider_file_id]
        for offset in range(0, len(data), 64):
            sink.write(data[offset : offset + 64])
        return GoogleDriveContentDownloadReceipt(1, len(data))


def execute(
    tmp_path: Path,
    *,
    handles=None,
    sku="MOCK-001",
    position=0,
    gateway_data=None,
    gateway_exception=None,
    factory=None,
    prep_status="ok",
    prep_overrides=None,
):
    handles = (make_handle(),) if handles is None else tuple(handles)
    match = next(
        (
            item
            for item in handles
            if item.sku == sku and item.selection_position == position
        ),
        handles[0],
    )
    raw_id = handle_core._provider_file_id_for_download(match)
    gateway = FakeGateway(
        {raw_id: JPEG} if gateway_data is None else gateway_data,
        exception=gateway_exception,
    )
    factory = Factory() if factory is None else factory
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=gateway):
        report = canary_core.execute_verified_webp_conversion_canary(
            preparation(handles, status=prep_status, overrides=prep_overrides),
            metadata_settings(),
            factory,
            sku=sku,
            position=position,
            workspace_parent=tmp_path,
        )
    return report, gateway, factory


def valid_argv():
    return [
        "convert-selected-media-canary",
        "--selection-report",
        "selection.json",
        "--baseline-snapshot",
        "baseline.json",
        "--mapping",
        "mapping.json",
        "--sheet",
        "RMB Price List",
        "--sku-report",
        "sku.json",
        "--sku",
        "MOCK-001",
        "--position",
        "0",
    ]


def test_policy_version():
    assert canary_core.POLICY_VERSION == "xxxxdoll-verified-webp-conversion-canary-v1"


def test_report_filename():
    assert canary_core.REPORT_FILENAME == "verified-webp-conversion-canary.json"


def test_cli_registered():
    parser = cli.build_parser()
    assert "convert-selected-media-canary" in parser.format_help()
    assert parser.parse_args(valid_argv()).command == "convert-selected-media-canary"


@pytest.mark.parametrize(
    "missing",
    [
        "--selection-report",
        "--baseline-snapshot",
        "--mapping",
        "--sheet",
        "--sku-report",
        "--sku",
        "--position",
    ],
)
def test_all_seven_cli_arguments_are_required(missing):
    argv = valid_argv()
    index = argv.index(missing)
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.mark.parametrize("position", ["-1", "1.5", "true", "x", ""])
def test_cli_position_validation(position):
    argv = valid_argv()
    argv[argv.index("--position") + 1] = position
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "sku",
    ["", " bad", "bad value", "../bad", "a/b", "a\\b", "❤️", "x" * 129],
)
def test_target_sku_validation(sku):
    with pytest.raises(canary_core.VerifiedWebPConversionCanaryError):
        canary_core._target(sku, 0)


@pytest.mark.parametrize("position", [-1, 1.5, "0", None, True])
def test_target_position_validation(position):
    with pytest.raises(canary_core.VerifiedWebPConversionCanaryError):
        canary_core._target("MOCK-001", position)


def test_synthetic_jpeg_to_webp_success(tmp_path):
    report, gateway, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert report["canary"]["conversion_action"] == "convert_to_webp"
    assert report["canary"]["webp_verified"] is True
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "sku",
        "selection_position",
        "image_role",
        "folder_role",
        "safe_name",
        "source_mime_type",
        "source_size_bytes",
        "source_md5_checksum",
        "source_width",
        "source_height",
        "conversion_action",
        "encoder_profile_version",
        "output_mime_type",
        "output_extension",
        "output_size_bytes",
        "output_sha256",
        "output_width",
        "output_height",
        "compression_ratio",
        "webp_verified",
    ],
)
def test_canary_report_schema(field, tmp_path):
    report, _, _ = execute(tmp_path)
    assert field in report["canary"]


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "policy_version",
        "canary",
        "preparation_summary",
        "download_summary",
        "conversion_summary",
        "source_cleanup_completed",
        "source_files_remaining",
        "webp_cleanup_completed",
        "webp_files_remaining",
        "retained_download_artifacts",
        "retained_webp_artifacts",
        "network_requests_performed",
        "download_requests_performed",
        "conversion_requests_performed",
        "wordpress_upload_requests_performed",
        "external_write_requests_performed",
        "write_requests_performed",
        "warnings",
        "blocking_issues",
    ],
)
def test_top_level_report_schema(field, tmp_path):
    report, _, _ = execute(tmp_path)
    assert field in report


@pytest.mark.parametrize("field", canary_core._PREPARATION_SUMMARY_FIELDS)
def test_preparation_summary_schema(field, tmp_path):
    report, _, _ = execute(tmp_path)
    assert field in report["preparation_summary"]


@pytest.mark.parametrize("field", canary_core._DOWNLOAD_SUMMARY_FIELDS)
def test_download_summary_schema(field, tmp_path):
    report, _, _ = execute(tmp_path)
    assert field in report["download_summary"]


@pytest.mark.parametrize("field", canary_core._CONVERSION_SUMMARY_FIELDS)
def test_conversion_summary_schema(field, tmp_path):
    report, _, _ = execute(tmp_path)
    assert field in report["conversion_summary"]


def test_full_preparation_authority_required(tmp_path):
    report, _, factory = execute(
        tmp_path,
        prep_status="blocked",
        prep_overrides={"handles_blocked": 1},
    )
    assert report["blocking_issues"] == ["webp_canary_preparation_not_authoritative"]
    assert factory.content_settings == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"handles_prepared": 0},
        {"selected_items": 2},
        {"handles_blocked": 1},
    ],
)
def test_incomplete_preparation_blocks_before_download(overrides, tmp_path):
    report, gateway, factory = execute(tmp_path, prep_overrides=overrides)
    assert report["status"] == "blocked"
    assert not gateway.calls
    assert not factory.content_settings


def test_exact_sku_and_position_selection(tmp_path):
    first = make_handle(sku="MOCK-001", position=0, raw_id="opaque-a")
    target = make_handle(sku="MOCK-002", position=1, raw_id="opaque-b")
    raw_id = handle_core._provider_file_id_for_download(target)
    report, gateway, _ = execute(
        tmp_path,
        handles=(first, target),
        sku="MOCK-002",
        position=1,
        gateway_data={raw_id: JPEG},
    )
    assert report["status"] == "ok"
    assert report["canary"]["sku"] == "MOCK-002"
    assert report["canary"]["selection_position"] == 1
    assert gateway.calls[0][0] == raw_id


def test_not_found_uses_fixed_code_and_no_fallback(tmp_path):
    report, gateway, factory = execute(tmp_path, sku="MOCK-999")
    assert report["blocking_issues"] == ["webp_canary_handle_not_found"]
    assert not gateway.calls and not factory.content_settings


def test_ambiguous_match_uses_fixed_code(tmp_path):
    item = make_handle()
    report, gateway, factory = execute(tmp_path, handles=(item, item))
    assert report["blocking_issues"] == ["webp_canary_handle_ambiguous"]
    assert not gateway.calls and not factory.content_settings


def test_sku_matching_is_case_sensitive(tmp_path):
    report, _, _ = execute(tmp_path, sku="mock-001")
    assert report["blocking_issues"] == ["webp_canary_handle_not_found"]


@pytest.mark.parametrize(
    ("drive_scope", "sheets_scope"),
    [
        (GOOGLE_DRIVE_CONTENT_READONLY_SCOPE, GOOGLE_SHEETS_READONLY_SCOPE),
        (GOOGLE_DRIVE_METADATA_READONLY_SCOPE, ""),
        ("https://www.googleapis.com/auth/drive", GOOGLE_SHEETS_READONLY_SCOPE),
    ],
)
def test_preparation_scope_mismatch_blocks(drive_scope, sheets_scope, tmp_path):
    item = make_handle()
    factory = Factory()
    report = canary_core.execute_verified_webp_conversion_canary(
        preparation((item,)),
        GoogleSettings(drive_scope=drive_scope, sheets_scope=sheets_scope),
        factory,
        sku=item.sku,
        position=0,
        workspace_parent=tmp_path,
    )
    assert report["blocking_issues"] == ["webp_canary_preparation_scope_mismatch"]
    assert not factory.content_settings


def test_content_client_uses_isolated_drive_readonly_scope(tmp_path):
    report, _, factory = execute(tmp_path)
    assert report["status"] == "ok"
    settings = factory.content_settings[0]
    assert settings.drive_scope == GOOGLE_DRIVE_CONTENT_READONLY_SCOPE
    assert settings.sheets_scope == ""


def test_metadata_settings_not_mutated(tmp_path):
    item = make_handle()
    configured = metadata_settings()
    raw_id = handle_core._provider_file_id_for_download(item)
    with patch.object(
        canary_core,
        "GoogleDriveContentGateway",
        return_value=FakeGateway({raw_id: JPEG}),
    ):
        canary_core.execute_verified_webp_conversion_canary(
            preparation((item,)),
            configured,
            Factory(),
            sku=item.sku,
            position=0,
            workspace_parent=tmp_path,
        )
    assert configured.drive_scope == GOOGLE_DRIVE_METADATA_READONLY_SCOPE
    assert configured.sheets_scope == GOOGLE_SHEETS_READONLY_SCOPE


def test_content_client_failure_is_safe(tmp_path):
    report, _, _ = execute(
        tmp_path,
        factory=Factory(error=RuntimeError("credential path and secret")),
    )
    assert report["status"] == "failed"
    assert report["blocking_issues"] == ["webp_canary_content_client_creation_failed"]
    assert "credential path" not in json.dumps(report)


def test_only_one_handle_is_passed_to_download_core(tmp_path):
    handles = (
        make_handle(position=0, raw_id="opaque-0"),
        make_handle(position=1, raw_id="opaque-1"),
    )
    raw_id = handle_core._provider_file_id_for_download(handles[0])
    original = download_core.download_secure_media
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        with patch.object(download_core, "download_secure_media", wraps=original) as downloaded:
            report = canary_core.execute_verified_webp_conversion_canary(
                preparation(handles),
                metadata_settings(),
                Factory(),
                sku=handles[0].sku,
                position=0,
                workspace_parent=tmp_path,
            )
    assert report["status"] == "ok"
    assert downloaded.call_args.args[0] == (handles[0],)


def test_download_core_called_once(tmp_path):
    item = make_handle()
    raw_id = handle_core._provider_file_id_for_download(item)
    original = download_core.download_secure_media
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        with patch.object(download_core, "download_secure_media", wraps=original) as downloaded:
            canary_core.execute_verified_webp_conversion_canary(
                preparation((item,)), metadata_settings(), Factory(),
                sku=item.sku, position=0, workspace_parent=tmp_path,
            )
    downloaded.assert_called_once()


def test_conversion_core_called_once_with_memory_artifact(tmp_path):
    item = make_handle()
    raw_id = handle_core._provider_file_id_for_download(item)
    original = conversion_core.convert_verified_media_to_webp
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        with patch.object(conversion_core, "convert_verified_media_to_webp", wraps=original) as converted:
            report = canary_core.execute_verified_webp_conversion_canary(
                preparation((item,)), metadata_settings(), Factory(),
                sku=item.sku, position=0, workspace_parent=tmp_path,
            )
    assert report["status"] == "ok"
    assert type(converted.call_args.args[0]) is download_core.VerifiedDownloadedMediaArtifact
    assert not isinstance(converted.call_args.args[0], (dict, Path, str, bytes))


def test_download_artifact_exists_until_conversion_consumes_it(tmp_path):
    item = make_handle()
    raw_id = handle_core._provider_file_id_for_download(item)
    original = conversion_core.convert_verified_media_to_webp

    def inspect_then_convert(artifact, **kwargs):
        assert download_core._local_source_path_for_conversion(artifact).exists()
        return original(artifact, **kwargs)

    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        with patch.object(conversion_core, "convert_verified_media_to_webp", side_effect=inspect_then_convert):
            report = canary_core.execute_verified_webp_conversion_canary(
                preparation((item,)), metadata_settings(), Factory(),
                sku=item.sku, position=0, workspace_parent=tmp_path,
            )
    assert report["status"] == "ok"


def test_cleanup_order_is_webp_then_source(tmp_path):
    events = []
    original_webp = conversion_core.VerifiedWebPConversionBatchResult.cleanup
    original_source = download_core.SecureMediaDownloadBatchResult.cleanup

    def cleanup_webp(self):
        events.append("webp")
        return original_webp(self)

    def cleanup_source(self):
        events.append("source")
        return original_source(self)

    with patch.object(conversion_core.VerifiedWebPConversionBatchResult, "cleanup", cleanup_webp):
        with patch.object(download_core.SecureMediaDownloadBatchResult, "cleanup", cleanup_source):
            report, _, _ = execute(tmp_path)
    assert report["status"] == "ok"
    assert events[-2:] == ["webp", "source"]


def test_success_cleans_both_workspaces_and_releases_artifacts(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["source_cleanup_completed"] is True
    assert report["webp_cleanup_completed"] is True
    assert report["source_files_remaining"] == 0
    assert report["webp_files_remaining"] == 0
    assert report["retained_download_artifacts"] == 0
    assert report["retained_webp_artifacts"] == 0
    assert not tuple(tmp_path.iterdir())


def test_download_integrity_summary_passes(tmp_path):
    report, _, _ = execute(tmp_path)
    summary = report["download_summary"]
    assert summary["downloads_verified"] == 1
    assert summary["checksum_verified"] == 1
    assert summary["size_verified"] == 1
    assert summary["signature_verified"] == 1


def test_conversion_summary_passes(tmp_path):
    report, _, _ = execute(tmp_path)
    summary = report["conversion_summary"]
    assert summary["source_artifacts_received"] == 1
    assert summary["conversion_attempted"] == 1
    assert summary["conversion_verified"] == 1
    assert summary["conversion_failed"] == 0
    assert summary["converted_from_jpeg"] == 1
    assert summary["authoritative_webp_artifacts"] == 1


def test_dimensions_are_preserved_without_resize(tmp_path):
    report, _, _ = execute(tmp_path)
    canary = report["canary"]
    assert (canary["source_width"], canary["source_height"]) == (8, 6)
    assert (canary["output_width"], canary["output_height"]) == (8, 6)


def test_encoder_profile_is_fixed(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["canary"]["encoder_profile_version"] == conversion_core.ENCODER_PROFILE_VERSION
    assert conversion_core.WEBP_QUALITY == 85
    assert conversion_core.WEBP_METHOD == 6


def test_output_identity_and_sha256_gate(tmp_path):
    report, _, _ = execute(tmp_path)
    canary = report["canary"]
    assert canary["output_mime_type"] == "image/webp"
    assert canary["output_extension"] == ".webp"
    assert canary["output_size_bytes"] > 0
    assert conversion_core._valid_sha256(canary["output_sha256"])


def test_compression_ratio_is_dynamic_audit(tmp_path):
    report, _, _ = execute(tmp_path)
    canary = report["canary"]
    assert canary["compression_ratio"] == round(
        canary["output_size_bytes"] / canary["source_size_bytes"], 8
    )


def test_output_magic_and_full_decode_revalidated_by_private_gate(tmp_path):
    item = make_handle()
    raw_id = handle_core._provider_file_id_for_download(item)
    original = conversion_core._local_webp_path_for_upload
    observations = {}

    def inspect_output(artifact):
        path = original(artifact)
        data = path.read_bytes()
        observations["magic"] = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        with Image.open(path) as image:
            observations["format"] = image.format
            image.load()
            observations["size"] = image.size
        return path

    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        with patch.object(conversion_core, "_local_webp_path_for_upload", side_effect=inspect_output) as gate:
            report = canary_core.execute_verified_webp_conversion_canary(
                preparation((item,)), metadata_settings(), Factory(),
                sku=item.sku, position=0, workspace_parent=tmp_path,
            )
    gate.assert_called_once()
    assert report["status"] == "ok"
    assert observations == {"magic": True, "format": "WEBP", "size": (8, 6)}


def test_exif_xmp_gps_not_present_in_converted_output(tmp_path):
    data = synthetic_jpeg(exif=True, gps=True)
    item = make_handle(data=data)
    raw_id = handle_core._provider_file_id_for_download(item)
    observed = {}
    original = conversion_core._local_webp_path_for_upload

    def inspect_output(artifact):
        path = original(artifact)
        with Image.open(path) as image:
            image.load()
            observed["exif"] = len(image.getexif())
            observed["gps"] = image.getexif().get_ifd(0x8825)
            observed["xmp"] = image.info.get("xmp")
        return path

    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: data})):
        with patch.object(conversion_core, "_local_webp_path_for_upload", side_effect=inspect_output):
            report = canary_core.execute_verified_webp_conversion_canary(
                preparation((item,)), metadata_settings(), Factory(),
                sku=item.sku, position=0, workspace_parent=tmp_path,
            )
    assert report["status"] == "ok"
    assert observed == {"exif": 0, "gps": {}, "xmp": None}


def test_download_failure_blocks_conversion_and_cleans_source(tmp_path):
    item = make_handle()
    raw_id = handle_core._provider_file_id_for_download(item)
    changed = JPEG + b"changed"
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: changed})):
        with patch.object(conversion_core, "convert_verified_media_to_webp") as converted:
            report = canary_core.execute_verified_webp_conversion_canary(
                preparation((item,)), metadata_settings(), Factory(),
                sku=item.sku, position=0, workspace_parent=tmp_path,
            )
    converted.assert_not_called()
    assert report["blocking_issues"] == ["webp_canary_download_not_verified"]
    assert report["source_files_remaining"] == 0
    assert report["webp_files_remaining"] == 0


def test_conversion_failure_cleans_webp_and_source(tmp_path):
    corrupt = b"\xff\xd8\xffcorrupt-jpeg"
    item = make_handle(data=corrupt)
    raw_id = handle_core._provider_file_id_for_download(item)
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: corrupt})):
        report = canary_core.execute_verified_webp_conversion_canary(
            preparation((item,)), metadata_settings(), Factory(),
            sku=item.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["blocking_issues"] == ["webp_canary_conversion_not_verified"]
    assert report["source_cleanup_completed"] is True
    assert report["webp_cleanup_completed"] is True
    assert not tuple(tmp_path.iterdir())


def test_partial_encode_failure_cleans_both_workspaces(tmp_path, monkeypatch):
    def fail(image, target):
        target.write_bytes(b"partial")
        raise OSError("synthetic local failure")

    monkeypatch.setattr(conversion_core, "_encode_webp", fail)
    report, _, _ = execute(tmp_path)
    assert report["status"] == "blocked"
    assert report["source_files_remaining"] == 0
    assert report["webp_files_remaining"] == 0
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(7)])
def test_download_baseexception_cleans_source_and_rethrows(exception, tmp_path):
    with pytest.raises(type(exception)) as raised:
        execute(tmp_path, gateway_exception=exception)
    assert raised.value is exception
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "stage",
    ["_open_and_load_source", "_encode_webp", "_verify_final_webp"],
)
@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(8)])
def test_conversion_stage_baseexception_cleans_both_and_rethrows(
    stage, exception, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        conversion_core,
        stage,
        lambda *args, **kwargs: (_ for _ in ()).throw(exception),
    )
    with pytest.raises(type(exception)) as raised:
        execute(tmp_path)
    assert raised.value is exception
    assert not tuple(tmp_path.iterdir())


def test_conversion_custom_baseexception_cleans_both(tmp_path, monkeypatch):
    class StopNow(BaseException):
        pass

    original = StopNow("stop")
    monkeypatch.setattr(
        conversion_core,
        "_encode_webp",
        lambda *args, **kwargs: (_ for _ in ()).throw(original),
    )
    with pytest.raises(StopNow) as raised:
        execute(tmp_path)
    assert raised.value is original
    assert not tuple(tmp_path.iterdir())


def test_report_projection_baseexception_cleans_both(tmp_path, monkeypatch):
    original = conversion_core.VerifiedWebPConversionBatchResult.to_safe_report_dict
    calls = 0
    interrupt = KeyboardInterrupt()

    def project(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise interrupt
        return original(self)

    monkeypatch.setattr(
        conversion_core.VerifiedWebPConversionBatchResult,
        "to_safe_report_dict",
        project,
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        execute(tmp_path)
    assert raised.value is interrupt
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "needle",
    [
        "provider_file_id",
        "raw_drive_id",
        "raw_file_id",
        "drive.google.com",
        "local_source_path",
        "local_webp_path",
        "workspace_root",
        "temp_directory",
        str(PROJECT_ROOT),
        "authorization",
        "cookie",
        "access_token",
        "refresh_token",
        "private_key",
        "client_email",
        "credentials",
    ],
)
def test_report_contains_no_authority_path_or_secret(needle, tmp_path):
    report, _, _ = execute(tmp_path)
    assert needle.casefold() not in json.dumps(report, ensure_ascii=False).casefold()


@pytest.mark.parametrize(
    "counter",
    [
        "wordpress_upload_requests_performed",
        "external_write_requests_performed",
        "write_requests_performed",
    ],
)
def test_forbidden_activity_counters_are_zero(counter, tmp_path):
    report, _, _ = execute(tmp_path)
    assert report[counter] == 0


def test_conversion_request_is_local_not_network(tmp_path):
    report, _, _ = execute(tmp_path)
    assert report["conversion_requests_performed"] == 1
    assert report["network_requests_performed"] == 4
    assert report["download_requests_performed"] == 1


def test_full_96_preparation_then_one_download_and_conversion(tmp_path):
    handles = []
    for index in range(96):
        sku_index, position = divmod(index, 12)
        handles.append(
            make_handle(
                sku=f"MOCK-{sku_index + 1:03d}",
                position=position,
                raw_id=f"opaque-{index:03d}",
                safe_name=f"supplier-{index:03d}.jpg",
            )
        )
    target = handles[0]
    raw_id = handle_core._provider_file_id_for_download(target)
    with patch.object(canary_core, "GoogleDriveContentGateway", return_value=FakeGateway({raw_id: JPEG})):
        report = canary_core.execute_verified_webp_conversion_canary(
            preparation(handles), metadata_settings(), Factory(),
            sku=target.sku, position=0, workspace_parent=tmp_path,
        )
    assert report["status"] == "ok"
    assert report["preparation_summary"]["selected_items"] == 96
    assert report["preparation_summary"]["handles_prepared"] == 96
    assert report["download_summary"]["handles_received"] == 1
    assert report["download_summary"]["downloads_verified"] == 1
    assert report["conversion_summary"]["conversion_verified"] == 1
    assert report["retained_download_artifacts"] == 0
    assert report["retained_webp_artifacts"] == 0


def test_run_reuses_fresh_preparation_and_same_process_execute(tmp_path):
    item = make_handle()
    prepared = preparation((item,))
    expected = canary_core._blocked_report(item.sku, 0, "mock")
    with patch.object(canary_core, "prepare_selected_media_handles", return_value=prepared) as prep:
        with patch.object(canary_core, "execute_verified_webp_conversion_canary", return_value=expected) as run:
            report, output = canary_core.run_verified_webp_conversion_canary(
                Path("selection.json"), Path("baseline.json"), Path("mapping.json"),
                "RMB Price List", Path("sku.json"), item.sku, 0,
                metadata_settings(), Factory(), project_root=tmp_path,
            )
    prep.assert_called_once()
    run.assert_called_once_with(
        prepared,
        ANY,
        ANY,
        sku=item.sku,
        position=0,
    )
    assert report == expected
    assert output == tmp_path / "reports" / canary_core.REPORT_FILENAME
    assert json.loads(output.read_text("utf-8")) == expected


def test_run_does_not_write_report_on_baseexception(tmp_path):
    item = make_handle()
    with patch.object(canary_core, "prepare_selected_media_handles", return_value=preparation((item,))):
        with patch.object(
            canary_core,
            "execute_verified_webp_conversion_canary",
            side_effect=KeyboardInterrupt(),
        ):
            with patch.object(canary_core.SafeJsonReportWriter, "write") as writer:
                with pytest.raises(KeyboardInterrupt):
                    canary_core.run_verified_webp_conversion_canary(
                        Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
                        item.sku, 0, metadata_settings(), Factory(), project_root=tmp_path,
                    )
    writer.assert_not_called()


def test_run_preparation_exception_writes_safe_failed_report(tmp_path):
    with patch.object(
        canary_core,
        "prepare_selected_media_handles",
        side_effect=RuntimeError("secret provider authority"),
    ):
        report, output = canary_core.run_verified_webp_conversion_canary(
            Path("a"), Path("b"), Path("c"), "Mock", Path("d"),
            "MOCK-001", 0, metadata_settings(), Factory(), project_root=tmp_path,
        )
    assert report["status"] == "failed"
    assert report["blocking_issues"] == ["webp_canary_preparation_failed"]
    assert "secret provider authority" not in output.read_text("utf-8")


def test_cli_dispatches_all_seven_arguments_without_real_config(tmp_path):
    report = canary_core._blocked_report("MOCK-001", 0, "mock")
    with patch.object(cli, "load_google_drive_metadata_config", return_value=metadata_settings()):
        with patch.object(cli, "PROJECT_ROOT", tmp_path):
            with patch.object(
                cli,
                "run_verified_webp_conversion_canary",
                return_value=(report, tmp_path / "reports" / canary_core.REPORT_FILENAME),
            ) as run:
                code = cli.main(valid_argv())
    assert code == 1
    args = run.call_args.args
    assert args[:7] == (
        Path("selection.json"),
        Path("baseline.json"),
        Path("mapping.json"),
        "RMB Price List",
        Path("sku.json"),
        "MOCK-001",
        0,
    )


@pytest.mark.parametrize(
    "forbidden",
    [
        "secure-media-download-execution.json",
        "secure-media-download-canary.json",
        "VerifiedDownloadedMediaArtifact(",
        "VerifiedWebPArtifact(",
        "provider_file_id",
        "local_source_path",
        "wp-json",
        "woocommerce",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
    ],
)
def test_canary_source_has_no_report_restore_or_forbidden_write(forbidden):
    source = inspect.getsource(canary_core)
    assert forbidden.casefold() not in source.casefold()


def test_reality_specific_values_not_hardcoded_in_production_core():
    source = inspect.getsource(canary_core)
    for value in (
        "CLM-CLASSIC-SI70CM-AR",
        "12458951",
        "5c53847fc04463312e389b98e8184026",
        "6240",
        "4160",
    ):
        assert value not in source


def test_readme_documents_exact_cli_and_cleanup():
    readme = (PROJECT_ROOT / "README.md").read_text("utf-8")
    section = readme.split("### Verified WebP Conversion Canary V1", 1)[1]
    assert "convert-selected-media-canary" in section
    for flag in (
        "--selection-report",
        "--baseline-snapshot",
        "--mapping",
        "--sheet",
        "--sku-report",
        "--sku",
        "--position",
    ):
        assert flag in section
    assert "cleanup WebP workspace" in section
    assert "cleanup source workspace" in section


def test_report_is_deterministic_for_same_synthetic_input(tmp_path):
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    first, _, _ = execute(first_root)
    second, _, _ = execute(second_root)
    assert first == second
