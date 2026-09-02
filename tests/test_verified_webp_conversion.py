from __future__ import annotations

import ast
import hashlib
import io
import json
import pickle
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import image_selection_policy as selection_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import secure_media_download as download_core
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker import verified_webp_conversion as webp_core
from sync_worker.google_api import GoogleDriveContentDownloadReceipt
from sync_worker.image_mapping import ProductSourceRange


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def image_bytes(
    image_format: str = "JPEG",
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (4, 3),
    exif: bool = False,
    gps: bool = False,
) -> bytes:
    if mode == "P":
        image = Image.new("P", size, 0)
        image.putpalette([255, 0, 0, 0, 255, 0] + [0, 0, 0] * 254)
        image.info["transparency"] = 0
    else:
        color = {
            "RGB": (20, 40, 60),
            "RGBA": (20, 40, 60, 91),
            "L": 64,
            "LA": (64, 91),
            "CMYK": (10, 20, 30, 40),
        }[mode]
        image = Image.new(mode, size, color)
    output = io.BytesIO()
    kwargs: dict[str, object] = {}
    if exif or gps:
        metadata = image.getexif()
        if exif:
            metadata[274] = 6
            metadata[270] = "supplier metadata must be stripped"
        if gps:
            gps_ifd = metadata.get_ifd(0x8825)
            gps_ifd[1] = "N"
            gps_ifd[2] = (1.0, 2.0, 3.0)
            metadata[0x8825] = gps_ifd
        kwargs["exif"] = metadata
    image.save(output, format=image_format, **kwargs)
    image.close()
    return output.getvalue()


class BytesGateway:
    def __init__(self, content: dict[str, bytes]):
        self.content = content
        self.calls: list[str] = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append(provider_file_id)
        data = self.content[provider_file_id]
        for offset in range(0, len(data), min(chunk_size, 64 * 1024)):
            sink.write(data[offset : offset + min(chunk_size, 64 * 1024)])
        return GoogleDriveContentDownloadReceipt(1, len(data))


def make_handle(
    data: bytes,
    *,
    mime: str = "image/jpeg",
    sku: str = "MOCK-001",
    position: int = 0,
    raw_id: str = "opaque-source-001",
    safe_name: str = "supplier-photo.jpg",
    width: int = 4,
    height: int = 3,
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
        image_role=image_role,
        selection_reason=reason,
    )
    item = root_core.DriveManifestItem(
        safe_name=safe_name,
        mime_type=mime,
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
        root_folder_id_fingerprint=root_core.fingerprint_drive_id("folder-" + raw_id),
        nested_folder_id_fingerprint=root_core.fingerprint_drive_id("nested-" + raw_id),
        safe_folder_name="Storefront Photos",
        depth=1,
        status="listed",
        items=(item,),
        pages_read=1,
    )
    baseline = handle_core.create_selected_media_baseline_identity(selection, manifest)
    return handle_core.create_secure_selected_media_handle(selection, baseline, manifest)


def make_download_batch(tmp_path: Path, specs: list[dict[str, object]]):
    handles = []
    content = {}
    for index, spec in enumerate(specs):
        data = spec["data"]
        raw_id = str(spec.get("raw_id", f"opaque-source-{index:03d}"))
        handle = make_handle(
            data,
            mime=str(spec.get("mime", "image/jpeg")),
            sku=str(spec.get("sku", f"MOCK-{index:03d}")),
            position=int(spec.get("position", 0)),
            raw_id=raw_id,
            safe_name=str(spec.get("safe_name", f"supplier-{index:03d}.jpg")),
            width=int(spec.get("width", 4)),
            height=int(spec.get("height", 3)),
        )
        handles.append(handle)
        content[raw_id] = data
    result = download_core.download_secure_media(
        tuple(handles), BytesGateway(content), workspace_parent=tmp_path
    )
    assert result.status == "ok"
    return result


def make_source(
    tmp_path: Path,
    *,
    image_format: str = "JPEG",
    mode: str = "RGB",
    mime: str | None = None,
    size: tuple[int, int] = (4, 3),
    safe_name: str = "supplier-photo.jpg",
    exif: bool = False,
    gps: bool = False,
):
    data = image_bytes(image_format, mode=mode, size=size, exif=exif, gps=gps)
    effective_mime = mime or {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[image_format]
    downloaded = make_download_batch(
        tmp_path,
        [{
            "data": data,
            "mime": effective_mime,
            "safe_name": safe_name,
            "width": size[0],
            "height": size[1],
        }],
    )
    return downloaded, downloaded.artifacts[0], data


def convert_one(tmp_path: Path, **kwargs):
    downloaded, source, data = make_source(tmp_path, **kwargs)
    result = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    return downloaded, result, data


def cleanup(downloaded, converted=None):
    if converted is not None:
        converted.cleanup()
    downloaded.cleanup()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("POLICY_VERSION", "xxxxdoll-verified-webp-conversion-v1"),
        ("ENCODER_PROFILE_VERSION", "xxxxdoll-pillow-webp-q85-m6-v1"),
        ("EXISTING_WEBP_PROFILE_VERSION", "xxxxdoll-existing-webp-byte-copy-v1"),
        ("WEBP_QUALITY", 85),
        ("WEBP_METHOD", 6),
        ("MAX_DECODE_PIXELS", 100_000_000),
        ("MAX_ARTIFACTS_PER_BATCH", 200),
    ],
)
def test_public_policy_constants(name, expected):
    assert getattr(webp_core, name) == expected


def test_pyproject_declares_pillow():
    assert '"Pillow>=11,<13"' in (PROJECT_ROOT / "pyproject.toml").read_text("utf-8")


@pytest.mark.parametrize(
    "value",
    [
        "photo.jpg",
        Path("photo.jpg"),
        {"source_verified": True},
        {"status": "ok", "results": []},
        b"raw jpeg bytes",
        bytearray(b"raw jpeg bytes"),
        memoryview(b"raw jpeg bytes"),
        [],
        set(),
        1,
        None,
        object(),
    ],
)
def test_non_authority_inputs_rejected(value, tmp_path):
    with pytest.raises(
        webp_core.VerifiedWebPConversionError,
        match="verified_downloaded_media_artifacts_required",
    ):
        webp_core.convert_verified_media_to_webp(value, workspace_parent=tmp_path)


def test_empty_tuple_rejected(tmp_path):
    with pytest.raises(webp_core.VerifiedWebPConversionError):
        webp_core.convert_verified_media_to_webp((), workspace_parent=tmp_path)


def test_tuple_with_non_authority_rejected(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    with pytest.raises(webp_core.VerifiedWebPConversionError):
        webp_core.convert_verified_media_to_webp((source, {}), workspace_parent=tmp_path)
    downloaded.cleanup()


def test_forged_download_artifact_is_blocked(tmp_path):
    forged = object.__new__(download_core.VerifiedDownloadedMediaArtifact)
    with pytest.raises(
        webp_core.VerifiedWebPConversionError,
        match="verified_downloaded_media_artifacts_required",
    ):
        webp_core.convert_verified_media_to_webp(forged, workspace_parent=tmp_path)


def test_duplicate_identity_rejected(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="duplicate"):
        webp_core.convert_verified_media_to_webp((source, source), workspace_parent=tmp_path)
    downloaded.cleanup()


def test_download_private_revalidation_helper_is_called(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    with patch.object(
        download_core,
        "_local_source_path_for_conversion",
        wraps=download_core._local_source_path_for_conversion,
    ) as revalidate:
        converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert revalidate.call_args.args == (source,)
    cleanup(downloaded, converted)


def test_source_local_mutation_is_blocked(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    source_path = download_core._local_source_path_for_conversion(source)
    source_path.write_bytes(source_path.read_bytes() + b"changed")
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    assert converted.artifacts == ()
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


@pytest.mark.parametrize(
    ("image_format", "mime", "action", "counter"),
    [
        ("JPEG", "image/jpeg", "convert_to_webp", "converted_from_jpeg"),
        ("PNG", "image/png", "convert_to_webp", "converted_from_png"),
        ("WEBP", "image/webp", "validate_existing_webp", "validated_existing_webp"),
    ],
)
def test_supported_source_mimes(image_format, mime, action, counter, tmp_path):
    downloaded, converted, _ = convert_one(
        tmp_path, image_format=image_format, mime=mime
    )
    report = converted.to_safe_report_dict()
    assert converted.status == "ok"
    assert report["results"][0]["conversion_action"] == action
    assert report["summary"][counter] == 1
    cleanup(downloaded, converted)


def test_other_mime_fails_closed_after_source_revalidation(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    original = download_core._local_source_path_for_conversion(source)
    object.__setattr__(source, "_source_mime_type", "image/gif")
    monkeypatch.setattr(download_core, "_local_source_path_for_conversion", lambda value: original)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    assert converted.to_safe_report_dict()["results"][0]["blocking_issues"] == [
        "webp_source_mime_not_allowed"
    ]
    downloaded.cleanup()


@pytest.mark.parametrize(
    ("image_format", "mode", "expected_mode"),
    [
        ("JPEG", "RGB", "RGB"),
        ("JPEG", "L", "RGB"),
        ("JPEG", "CMYK", "RGB"),
        ("PNG", "RGB", "RGB"),
        ("PNG", "RGBA", "RGBA"),
        ("PNG", "LA", "RGBA"),
        ("PNG", "P", "RGBA"),
    ],
)
def test_color_mode_normalization_and_alpha(image_format, mode, expected_mode, tmp_path):
    downloaded, converted, _ = convert_one(
        tmp_path,
        image_format=image_format,
        mode=mode,
        safe_name=f"source-{mode}.png",
    )
    path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
    with Image.open(path) as output:
        output.load()
        assert output.mode == expected_mode
        if "A" in expected_mode:
            assert output.getchannel("A").getextrema()[1] < 255
    cleanup(downloaded, converted)


def test_encoder_receives_only_fixed_minimal_parameters(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    original_save = Image.Image.save
    calls = []

    def capture(self, fp, format=None, **params):
        calls.append((format, dict(params), self.size))
        return original_save(self, fp, format=format, **params)

    monkeypatch.setattr(Image.Image, "save", capture)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert calls == [("WEBP", {"quality": 85, "method": 6}, (4, 3))]
    cleanup(downloaded, converted)


def test_no_resize_and_dimensions_preserved(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path, size=(17, 11))
    artifact = converted.artifacts[0]
    assert (artifact.image_width, artifact.image_height) == (17, 11)
    with Image.open(webp_core._local_webp_path_for_upload(artifact)) as output:
        assert output.size == (17, 11)
    cleanup(downloaded, converted)


def test_no_exif_transpose_and_metadata_is_stripped(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path, size=(7, 4), exif=True)
    artifact = converted.artifacts[0]
    with Image.open(webp_core._local_webp_path_for_upload(artifact)) as output:
        output.load()
        assert output.size == (7, 4)
        assert len(output.getexif()) == 0
        assert "exif" not in output.info
        assert "icc_profile" not in output.info
        assert "xmp" not in output.info
    cleanup(downloaded, converted)


def test_gps_metadata_is_not_copied(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path, gps=True)
    with Image.open(webp_core._local_webp_path_for_upload(converted.artifacts[0])) as output:
        output.load()
        assert output.getexif().get_ifd(0x8825) == {}
        assert len(output.getexif()) == 0
    cleanup(downloaded, converted)


def test_source_md5_lineage_and_output_sha256_are_distinct(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    source = downloaded.artifacts[0]
    artifact = converted.artifacts[0]
    assert artifact.source_md5_checksum == source.actual_md5_checksum
    assert len(artifact.source_md5_checksum) == 32
    assert len(artifact.output_sha256) == 64
    assert artifact.output_sha256 != artifact.source_md5_checksum
    cleanup(downloaded, converted)


@pytest.mark.parametrize(("slot", "value"), [("_expected_image_width", 5), ("_expected_image_height", 4)])
def test_decoded_dimension_mismatch_blocks(slot, value, tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    object.__setattr__(source, slot, value)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    report = converted.to_safe_report_dict()
    assert converted.status == "blocked"
    assert report["summary"]["dimension_mismatch"] == 1
    assert report["results"][0]["blocking_issues"] == ["source_decoded_dimensions_mismatch"]
    downloaded.cleanup()


@pytest.mark.parametrize(("width", "height"), [(0, 3), (4, 0), (-1, 3), (4, -1), (None, 3), (4, None)])
def test_invalid_expected_dimensions_block(width, height, tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    object.__setattr__(source, "_expected_image_width", width)
    object.__setattr__(source, "_expected_image_height", height)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    assert converted.to_safe_report_dict()["results"][0]["blocking_issues"] == [
        "webp_expected_dimensions_unsafe"
    ]
    downloaded.cleanup()


def test_expected_dimension_pixel_ceiling_blocks_before_decode(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    object.__setattr__(source, "_expected_image_width", 100_001)
    object.__setattr__(source, "_expected_image_height", 1000)
    with patch.object(Image, "open", wraps=Image.open) as opened:
        converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    opened.assert_not_called()
    downloaded.cleanup()


def test_decoded_dimension_ceiling_checked_before_load(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path, size=(2, 2))
    monkeypatch.setattr(webp_core, "MAX_DECODE_PIXELS", 1)
    object.__setattr__(source, "_expected_image_width", 1)
    object.__setattr__(source, "_expected_image_height", 1)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.to_safe_report_dict()["results"][0]["blocking_issues"] == [
        "webp_decode_dimensions_unsafe"
    ]
    downloaded.cleanup()


@pytest.mark.parametrize(
    ("mime", "prefix"),
    [
        ("image/jpeg", b"\xff\xd8\xffcorrupt-jpeg"),
        ("image/png", b"\x89PNG\r\n\x1a\ncorrupt-png"),
        ("image/webp", b"RIFF\x10\x00\x00\x00WEBPcorrupt-webp"),
    ],
)
def test_corrupt_source_decode_blocks(mime, prefix, tmp_path):
    downloaded = make_download_batch(tmp_path, [{"data": prefix, "mime": mime, "width": 4, "height": 3}])
    converted = webp_core.convert_verified_media_to_webp(
        downloaded.artifacts[0], workspace_parent=tmp_path
    )
    assert converted.status == "blocked"
    assert converted.to_safe_report_dict()["summary"]["decode_failed"] == 1
    downloaded.cleanup()


def test_decompression_bomb_warning_fails_closed(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    monkeypatch.setattr(
        Image,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(Image.DecompressionBombWarning("unsafe")),
    )
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    assert converted.to_safe_report_dict()["results"][0]["blocking_issues"] == [
        "webp_decode_dimensions_unsafe"
    ]
    downloaded.cleanup()


def test_output_format_suffix_magic_full_decode_size_and_sha256(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    artifact = converted.artifacts[0]
    path = webp_core._local_webp_path_for_upload(artifact)
    data = path.read_bytes()
    assert path.suffix == ".webp"
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert artifact.output_size_bytes == len(data) > 0
    assert artifact.output_sha256 == hashlib.sha256(data).hexdigest()
    with Image.open(path) as output:
        assert output.format == "WEBP"
        output.load()
        assert output.size == (4, 3)
    cleanup(downloaded, converted)


def test_existing_webp_is_copied_byte_for_byte_without_reencode(tmp_path, monkeypatch):
    downloaded, source, original = make_source(tmp_path, image_format="WEBP")
    with patch.object(webp_core, "_encode_webp") as encoder:
        converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
    assert path.read_bytes() == original
    assert converted.artifacts[0].output_sha256 == hashlib.sha256(original).hexdigest()
    encoder.assert_not_called()
    cleanup(downloaded, converted)


def test_existing_webp_uses_distinct_output_workspace(tmp_path):
    downloaded, source, _ = make_source(tmp_path, image_format="WEBP")
    source_path = download_core._local_source_path_for_conversion(source)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    output_path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
    assert output_path != source_path
    assert output_path.parent != source_path.parent
    cleanup(downloaded, converted)


@pytest.mark.parametrize(
    "field",
    [
        "sku",
        "selection_position",
        "image_role",
        "folder_role",
        "safe_name",
        "file_id_fingerprint",
        "source_mime_type",
        "source_size_bytes",
        "source_md5_checksum",
        "output_size_bytes",
        "output_sha256",
        "image_width",
        "image_height",
        "conversion_action",
        "encoder_profile_version",
        "webp_verified",
        "warnings",
        "blocking_issues",
    ],
)
def test_artifact_is_immutable_for_every_public_field(field, tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    with pytest.raises(AttributeError, match="immutable"):
        setattr(converted.artifacts[0], field, "changed")
    cleanup(downloaded, converted)


def test_artifact_uses_slots_and_has_no_dict(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    assert not hasattr(converted.artifacts[0], "__dict__")
    cleanup(downloaded, converted)


@pytest.mark.parametrize("protocol", list(range(pickle.HIGHEST_PROTOCOL + 1)))
def test_artifact_is_not_pickleable(protocol, tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    with pytest.raises(TypeError, match="not_serializable"):
        pickle.dumps(converted.artifacts[0], protocol=protocol)
    cleanup(downloaded, converted)


def test_artifact_public_constructor_is_capability_gated(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="factory_required"):
        webp_core.VerifiedWebPArtifact(
            object(),
            source=source,
            local_webp_path=tmp_path / "x.webp",
            workspace_root=tmp_path,
            output_size_bytes=1,
            output_sha256="0" * 64,
            image_width=4,
            image_height=3,
            conversion_action="convert_to_webp",
            encoder_profile_version=webp_core.ENCODER_PROFILE_VERSION,
        )
    downloaded.cleanup()


@pytest.mark.parametrize(
    "field",
    [
        "policy_version",
        "sku",
        "selection_position",
        "image_role",
        "folder_role",
        "safe_name",
        "file_id_fingerprint",
        "source_mime_type",
        "source_size_bytes",
        "source_md5_checksum",
        "output_mime_type",
        "output_extension",
        "output_size_bytes",
        "output_sha256",
        "image_width",
        "image_height",
        "conversion_action",
        "encoder_profile_version",
        "webp_verified",
        "warnings",
        "blocking_issues",
    ],
)
def test_safe_artifact_schema_contains_required_field(field, tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    assert field in converted.artifacts[0].to_safe_dict()
    cleanup(downloaded, converted)


@pytest.mark.parametrize(
    "forbidden",
    [
        "local_source_path",
        "local_webp_path",
        "workspace_root",
        "workspace",
        "provider_file_id",
        "drive.google.com",
        "http://",
        "https://",
        "authorization",
        "cookie",
        "client_email",
        "private_key",
        "access_token",
        "refresh_token",
    ],
)
def test_safe_artifact_and_report_exclude_forbidden_authority(forbidden, tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    payload = json.dumps(
        {
            "artifact": converted.artifacts[0].to_safe_dict(),
            "report": converted.to_safe_report_dict(),
            "repr": repr(converted.artifacts[0]),
        },
        ensure_ascii=False,
    ).casefold()
    assert forbidden not in payload
    cleanup(downloaded, converted)


def test_artifact_repr_is_safe_and_has_no_path(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    rendered = repr(converted.artifacts[0])
    assert "VerifiedWebPArtifact" in rendered
    assert str(tmp_path).casefold() not in rendered.casefold()
    cleanup(downloaded, converted)


def test_private_upload_helper_revalidates_sha(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    artifact = converted.artifacts[0]
    path = webp_core._local_webp_path_for_upload(artifact)
    path.write_bytes(path.read_bytes() + b"mutation")
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="content_changed"):
        webp_core._local_webp_path_for_upload(artifact)
    cleanup(downloaded, converted)


def test_private_upload_helper_revalidates_magic(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    artifact = converted.artifacts[0]
    path = webp_core._local_webp_path_for_upload(artifact)
    data = bytearray(path.read_bytes())
    data[0:4] = b"NOPE"
    path.write_bytes(data)
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="magic_mismatch"):
        webp_core._local_webp_path_for_upload(artifact)
    cleanup(downloaded, converted)


def test_private_upload_helper_rejects_missing_file(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    artifact = converted.artifacts[0]
    webp_core._local_webp_path_for_upload(artifact).unlink()
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="unavailable"):
        webp_core._local_webp_path_for_upload(artifact)
    cleanup(downloaded, converted)


def test_forged_webp_artifact_rejected():
    forged = object.__new__(webp_core.VerifiedWebPArtifact)
    with pytest.raises(webp_core.VerifiedWebPConversionError):
        webp_core._local_webp_path_for_upload(forged)


def test_non_artifact_upload_path_input_rejected():
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="artifact_required"):
        webp_core._local_webp_path_for_upload(Path("x.webp"))


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "escape.jpg",
        "safe-name.jpg",
        "photo_001.jpg",
        "name with spaces.jpg",
        "photo;rm.webp",
        "photo $(bad).jpg",
        "UPPER.JPG",
        "照片 ❤️.jpg",
    ],
)
def test_supplier_safe_name_never_controls_output_path(unsafe_name, tmp_path):
    downloaded, converted, _ = convert_one(tmp_path, safe_name=unsafe_name)
    path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
    assert webp_core._WEBP_NAME_PATTERN.fullmatch(path.name)
    assert unsafe_name not in path.name
    cleanup(downloaded, converted)


def test_workspace_is_separate_from_source_and_outside_project(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    source_path = download_core._local_source_path_for_conversion(source)
    output_path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
    assert output_path.parent.name.startswith("xxxxdoll-webp-")
    assert output_path.parent != source_path.parent
    assert PROJECT_ROOT not in output_path.parents
    cleanup(downloaded, converted)


@pytest.mark.parametrize(
    "forbidden",
    [PROJECT_ROOT, PROJECT_ROOT / "reports", PROJECT_ROOT / "src", PROJECT_ROOT / "tests"],
)
def test_workspace_inside_project_is_rejected(forbidden, tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    with pytest.raises(webp_core.VerifiedWebPConversionError, match="workspace_parent_invalid"):
        webp_core.convert_verified_media_to_webp(source, workspace_parent=forbidden)
    downloaded.cleanup()


def test_success_outputs_retained_until_explicit_cleanup(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
    assert path.exists()
    converted.cleanup()
    assert not path.exists()
    assert converted.artifacts == ()
    downloaded.cleanup()


def test_cleanup_is_idempotent_and_counter_stable(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    converted.cleanup()
    once = converted.to_safe_report_dict()["summary"]["output_files_cleaned"]
    converted.cleanup()
    twice = converted.to_safe_report_dict()["summary"]["output_files_cleaned"]
    assert once == twice == 1
    downloaded.cleanup()


def test_context_manager_cleans_outputs_only(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    source_path = download_core._local_source_path_for_conversion(source)
    with webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path) as converted:
        output_path = webp_core._local_webp_path_for_upload(converted.artifacts[0])
        assert output_path.exists()
    assert not output_path.exists()
    assert source_path.exists()
    downloaded.cleanup()


def make_failure_batch(tmp_path: Path, failing_index: int, total: int = 4):
    good = image_bytes("JPEG")
    corrupt = b"\xff\xd8\xffcorrupt"
    specs = []
    for index in range(total):
        specs.append({
            "data": corrupt if index == failing_index else good,
            "mime": "image/jpeg",
            "sku": f"MOCK-{index:03d}",
            "raw_id": f"opaque-{index:03d}",
            "position": 0,
            "width": 4,
            "height": 3,
        })
    return make_download_batch(tmp_path, specs)


@pytest.mark.parametrize("failing_index", [0, 2, 3])
def test_failure_is_all_or_nothing_and_cleans_every_output(failing_index, tmp_path):
    downloaded = make_failure_batch(tmp_path, failing_index)
    converted = webp_core.convert_verified_media_to_webp(
        downloaded.artifacts, workspace_parent=tmp_path
    )
    report = converted.to_safe_report_dict()
    assert converted.status == "blocked"
    assert converted.artifacts == ()
    assert report["summary"]["authoritative_webp_artifacts"] == 0
    assert report["summary"]["conversion_verified"] == failing_index
    assert report["summary"]["output_files_cleaned"] == failing_index
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(9)])
def test_interruptions_cleanup_and_rethrow_original(exception, tmp_path, monkeypatch):
    downloaded = make_download_batch(
        tmp_path,
        [
            {"data": image_bytes("JPEG"), "sku": "MOCK-001", "raw_id": "opaque-1"},
            {"data": image_bytes("JPEG"), "sku": "MOCK-002", "raw_id": "opaque-2"},
        ],
    )
    original = webp_core._encode_webp
    calls = 0

    def interrupted(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise exception
        return original(source, target)

    monkeypatch.setattr(webp_core, "_encode_webp", interrupted)
    with pytest.raises(type(exception)) as raised:
        webp_core.convert_verified_media_to_webp(downloaded.artifacts, workspace_parent=tmp_path)
    assert raised.value is exception
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


def test_custom_baseexception_cleanup_and_rethrow(tmp_path, monkeypatch):
    class StopNow(BaseException):
        pass

    downloaded, source, _ = make_source(tmp_path)
    original = StopNow("stop")
    monkeypatch.setattr(webp_core, "_encode_webp", lambda *args: (_ for _ in ()).throw(original))
    with pytest.raises(StopNow) as raised:
        webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert raised.value is original
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


def test_generic_exception_becomes_safe_blocker_and_cleans(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    monkeypatch.setattr(webp_core, "_encode_webp", lambda *args: (_ for _ in ()).throw(RuntimeError("secret path")))
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    payload = json.dumps(converted.to_safe_report_dict())
    assert converted.status == "blocked"
    assert "secret path" not in payload
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


def test_source_lifecycle_is_never_cleaned_by_conversion_success(tmp_path):
    downloaded, source, _ = make_source(tmp_path)
    source_path = download_core._local_source_path_for_conversion(source)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    converted.cleanup()
    assert source_path.exists()
    downloaded.cleanup()


def test_source_lifecycle_is_never_cleaned_by_conversion_failure(tmp_path):
    corrupt = b"\xff\xd8\xffcorrupt"
    downloaded = make_download_batch(tmp_path, [{"data": corrupt, "width": 4, "height": 3}])
    source = downloaded.artifacts[0]
    source_path = download_core._local_source_path_for_conversion(source)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    assert source_path.exists()
    downloaded.cleanup()


def test_input_order_primary_gallery_safe_name_and_identity_preserved(tmp_path):
    data = image_bytes("JPEG")
    downloaded = make_download_batch(
        tmp_path,
        [
            {"data": data, "sku": "A", "position": 0, "raw_id": "a0", "safe_name": "first.jpg"},
            {"data": data, "sku": "A", "position": 1, "raw_id": "a1", "safe_name": "second.jpg"},
            {"data": data, "sku": "B", "position": 0, "raw_id": "b0", "safe_name": "imani.jpg"},
        ],
    )
    converted = webp_core.convert_verified_media_to_webp(downloaded.artifacts, workspace_parent=tmp_path)
    assert [(a.sku, a.selection_position, a.image_role, a.safe_name) for a in converted.artifacts] == [
        ("A", 0, "primary", "first.jpg"),
        ("A", 1, "gallery", "second.jpg"),
        ("B", 0, "primary", "imani.jpg"),
    ]
    cleanup(downloaded, converted)


def test_96_jpeg_current_shape_success(tmp_path):
    data = image_bytes("JPEG", size=(2, 2))
    specs = []
    for sku_index in range(8):
        for position in range(12):
            index = sku_index * 12 + position
            specs.append({
                "data": data,
                "mime": "image/jpeg",
                "sku": f"MOCK-{sku_index:03d}",
                "position": position,
                "raw_id": f"opaque-{index:03d}",
                "safe_name": f"supplier-{index:03d}.jpg",
                "width": 2,
                "height": 2,
            })
    downloaded = make_download_batch(tmp_path, specs)
    converted = webp_core.convert_verified_media_to_webp(downloaded.artifacts, workspace_parent=tmp_path)
    report = converted.to_safe_report_dict()
    assert converted.status == "ok"
    assert len(converted.artifacts) == 96
    assert report["summary"]["source_artifacts_received"] == 96
    assert report["summary"]["conversion_attempted"] == 96
    assert report["summary"]["conversion_verified"] == 96
    assert report["summary"]["conversion_failed"] == 0
    assert report["summary"]["converted_from_jpeg"] == 96
    assert report["summary"]["authoritative_webp_artifacts"] == 96
    assert sum(a.image_role == "primary" for a in converted.artifacts) == 8
    assert sum(a.image_role == "gallery" for a in converted.artifacts) == 88
    assert all(a.output_mime_type == "image/webp" for a in converted.artifacts)
    assert all(a.output_extension == ".webp" for a in converted.artifacts)
    paths = [webp_core._local_webp_path_for_upload(a) for a in converted.artifacts]
    assert all(path.exists() for path in paths)
    converted.cleanup()
    assert all(not path.exists() for path in paths)
    assert converted.to_safe_report_dict()["summary"]["output_files_cleaned"] == 96
    downloaded.cleanup()


@pytest.mark.parametrize(
    "counter",
    [
        "source_artifacts_received",
        "conversion_attempted",
        "conversion_verified",
        "conversion_failed",
        "converted_from_jpeg",
        "converted_from_png",
        "validated_existing_webp",
        "decode_verified",
        "decode_failed",
        "dimension_verified",
        "dimension_mismatch",
        "webp_signature_verified",
        "webp_signature_mismatch",
        "webp_decode_verified",
        "webp_decode_failed",
        "output_files_created",
        "output_files_cleaned",
        "source_total_bytes",
        "output_total_bytes",
        "authoritative_webp_artifacts",
        "conversion_requests_performed",
        "wordpress_upload_requests_performed",
        "external_write_requests_performed",
    ],
)
def test_summary_contains_required_counter(counter, tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    assert counter in converted.to_safe_report_dict()["summary"]
    cleanup(downloaded, converted)


def test_local_conversion_counter_is_not_a_network_counter(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    report = converted.to_safe_report_dict()
    assert report["summary"]["conversion_requests_performed"] == 1
    assert report["summary"]["wordpress_upload_requests_performed"] == 0
    assert report["summary"]["external_write_requests_performed"] == 0
    cleanup(downloaded, converted)


def test_existing_webp_has_zero_conversion_operations(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path, image_format="WEBP")
    assert converted.to_safe_report_dict()["summary"]["conversion_requests_performed"] == 0
    cleanup(downloaded, converted)


def test_output_total_bytes_is_audit_only(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    report = converted.to_safe_report_dict()
    assert report["summary"]["output_total_bytes"] == converted.artifacts[0].output_size_bytes
    cleanup(downloaded, converted)


def test_output_signature_failure_is_blocked_and_cleaned(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    monkeypatch.setattr(webp_core, "_encode_webp", lambda source, target: target.write_bytes(b"not-webp"))
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    report = converted.to_safe_report_dict()
    assert report["summary"]["webp_signature_mismatch"] == 1
    assert report["summary"]["authoritative_webp_artifacts"] == 0
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


def test_output_full_decode_failure_is_blocked(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    original = webp_core._verify_final_webp
    monkeypatch.setattr(
        webp_core,
        "_verify_final_webp",
        lambda *args, **kwargs: (_ for _ in ()).throw(webp_core._ConversionBlocked("webp_output_decode_failed")),
    )
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.to_safe_report_dict()["summary"]["webp_decode_failed"] == 1
    monkeypatch.setattr(webp_core, "_verify_final_webp", original)
    downloaded.cleanup()


def test_output_dimension_failure_is_blocked(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)
    monkeypatch.setattr(
        webp_core,
        "_verify_final_webp",
        lambda *args, **kwargs: (_ for _ in ()).throw(webp_core._ConversionBlocked("webp_output_dimensions_mismatch")),
    )
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    assert converted.status == "blocked"
    assert converted.to_safe_report_dict()["summary"]["webp_decode_failed"] == 1
    assert converted.to_safe_report_dict()["summary"]["dimension_mismatch"] == 1
    downloaded.cleanup()


def test_partial_output_write_failure_is_counted_and_cleaned(tmp_path, monkeypatch):
    downloaded, source, _ = make_source(tmp_path)

    def partial_write(image, target):
        target.write_bytes(b"partial")
        raise OSError("local write failed")

    monkeypatch.setattr(webp_core, "_encode_webp", partial_write)
    converted = webp_core.convert_verified_media_to_webp(source, workspace_parent=tmp_path)
    report = converted.to_safe_report_dict()
    assert converted.status == "blocked"
    assert report["summary"]["output_files_created"] == 1
    assert report["summary"]["output_files_cleaned"] == 1
    assert not tuple(tmp_path.glob("xxxxdoll-webp-*"))
    downloaded.cleanup()


def test_no_cli_no_report_writer_no_google_no_network_imports():
    source = (PROJECT_ROOT / "src" / "sync_worker" / "verified_webp_conversion.py").read_text("utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    from_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    combined = imports | from_imports
    assert not any(name.startswith(("requests", "urllib", "httpx", "google")) for name in combined)
    assert "SafeJsonReportWriter" not in source
    assert "argparse" not in combined
    assert "wordpress" not in combined


@pytest.mark.parametrize(
    "forbidden_method",
    [
        "requests.get",
        "requests.post",
        "urlopen",
        "socket.create_connection",
        "files().get",
        "files().list",
        "media().create",
        "wp-json",
        "woocommerce",
        "SafeJsonReportWriter",
    ],
)
def test_core_source_contains_no_external_or_report_operation(forbidden_method):
    source = (PROJECT_ROOT / "src" / "sync_worker" / "verified_webp_conversion.py").read_text("utf-8")
    assert forbidden_method not in source


def test_report_counters_prove_zero_external_and_wordpress_writes(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    report = converted.to_safe_report_dict()
    assert report["wordpress_upload_requests_performed"] == 0
    assert report["external_write_requests_performed"] == 0
    assert report["summary"]["wordpress_upload_requests_performed"] == 0
    assert report["summary"]["external_write_requests_performed"] == 0
    cleanup(downloaded, converted)


def test_batch_result_nonpickle_and_safe_repr(tmp_path):
    downloaded, converted, _ = convert_one(tmp_path)
    with pytest.raises(TypeError, match="not_serializable"):
        pickle.dumps(converted)
    assert str(tmp_path).casefold() not in repr(converted).casefold()
    cleanup(downloaded, converted)
