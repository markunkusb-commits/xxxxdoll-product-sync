from __future__ import annotations

import hashlib
import inspect
import io
import json
import pickle
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import http_client
from sync_worker import image_selection_policy as selection_core
from sync_worker import secure_media_download as download_core
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker import verified_webp_conversion as conversion_core
from sync_worker import wordpress_media_upload_gate as gate_core
from sync_worker.config import Settings
from sync_worker.google_api import GoogleDriveContentDownloadReceipt
from sync_worker.image_mapping import ProductSourceRange


def image_bytes(color=(31, 71, 113)):
    image = Image.new("RGB", (8, 6), color)
    output = io.BytesIO()
    image.save(output, format="JPEG")
    image.close()
    return output.getvalue()


JPEG = image_bytes()


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network access forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def settings(**overrides):
    values = {
        "wp_base_url": "https://staging-unit-test.wpcomstaging.com",
        "wp_username": "mock-user-not-read",
        "wp_app_password": "mock-app-password-not-read",
        "wc_consumer_key": "ck_mock_value_not_read_1234567890",
        "wc_consumer_secret": "cs_mock_value_not_read_1234567890",
        "sync_environment": "staging",
        "dry_run": True,
        "default_product_status": "draft",
        "allow_delete": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_handle(*, sku="MOCK-001", position=0, data=JPEG, raw_id=None):
    raw_id = raw_id or f"opaque_{sku}_{position}"
    safe_name = f"supplier image {position}.jpg"
    suffix = sum(ord(character) for character in sku) % 500
    source = ProductSourceRange(10 + suffix, 15 + suffix)
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


class BytesGateway:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def download_file(self, provider_file_id, sink, *, chunk_size):
        self.calls.append(provider_file_id)
        data = self.values[provider_file_id]
        sink.write(data)
        return GoogleDriveContentDownloadReceipt(1, len(data))


@dataclass
class ArtifactBundle:
    download: download_core.SecureMediaDownloadBatchResult
    conversion: conversion_core.VerifiedWebPConversionBatchResult

    @property
    def artifacts(self):
        return self.conversion.artifacts

    def cleanup(self):
        self.conversion.cleanup()
        self.download.cleanup()


def make_bundle(tmp_path, count=1, *, sku_factory=None, data_factory=None):
    sku_factory = sku_factory or (lambda index: f"MOCK-{index // 12 + 1:03d}")
    data_factory = data_factory or (lambda index: JPEG)
    handles = []
    values = {}
    for index in range(count):
        sku = sku_factory(index)
        position = index % 12
        data = data_factory(index)
        handle = make_handle(
            sku=sku, position=position, data=data, raw_id=f"opaque_file_{index:03d}"
        )
        handles.append(handle)
        values[handle_core._provider_file_id_for_download(handle)] = data
    download = download_core.download_secure_media(
        tuple(handles), BytesGateway(values), workspace_parent=tmp_path
    )
    conversion = conversion_core.convert_verified_media_to_webp(
        download.artifacts, workspace_parent=tmp_path
    )
    return ArtifactBundle(download, conversion)


def gate_one(tmp_path, *, current_settings=None, sku_factory=None):
    bundle = make_bundle(tmp_path, sku_factory=sku_factory)
    result = gate_core.create_wordpress_media_upload_intents(
        bundle.artifacts, current_settings or settings()
    )
    return bundle, result


def mutate_artifact_file(artifact, content=b"RIFFxxxxWEBPbad"):
    path = conversion_core._local_webp_path_for_upload(artifact)
    path.write_bytes(content)
    return path


def test_policy_version():
    assert gate_core.POLICY_VERSION == "xxxxdoll-wordpress-media-upload-gate-v1"


def test_fixed_media_resource_and_endpoint():
    assert gate_core.WORDPRESS_RESOURCE == "media"
    assert gate_core.WORDPRESS_MEDIA_ENDPOINT == "/wp-json/wp/v2/media"


def test_valid_verified_webp_artifact_is_accepted(tmp_path):
    bundle, result = gate_one(tmp_path)
    try:
        assert result.status == "ok"
        assert len(result.intents) == 1
        assert result.intents[0].upload_gate_passed is True
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "value",
    [
        "file.webp", Path("file.webp"), {}, {"output_sha256": "a" * 64},
        [], ["file.webp"], 1, True, None, b"RIFFxxxxWEBP",
    ],
)
def test_non_authority_inputs_are_rejected(value):
    with pytest.raises(gate_core.WordPressMediaUploadGateError) as caught:
        gate_core.create_wordpress_media_upload_intents(value, settings())
    assert str(caught.value) == "verified_webp_artifacts_required"


def test_empty_tuple_rejected():
    with pytest.raises(gate_core.WordPressMediaUploadGateError):
        gate_core.create_wordpress_media_upload_intents((), settings())


def test_tuple_with_non_artifact_rejected(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        with pytest.raises(gate_core.WordPressMediaUploadGateError):
            gate_core.create_wordpress_media_upload_intents(
                (bundle.artifacts[0], {}), settings()
            )
    finally:
        bundle.cleanup()


def test_forged_artifact_rejected():
    forged = object.__new__(conversion_core.VerifiedWebPArtifact)
    with pytest.raises(gate_core.WordPressMediaUploadGateError):
        gate_core.create_wordpress_media_upload_intents(forged, settings())


def test_private_webp_upload_helper_is_reused(tmp_path):
    bundle = make_bundle(tmp_path)
    original = conversion_core._local_webp_path_for_upload
    try:
        with patch.object(
            conversion_core, "_local_webp_path_for_upload", wraps=original
        ) as verified:
            result = gate_core.create_wordpress_media_upload_intents(
                bundle.artifacts, settings()
            )
        assert result.status == "ok"
        assert verified.call_count == 1
        assert verified.call_args.args[0] is bundle.artifacts[0]
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "replacement",
    [b"not webp", b"RIFFxxxxNOPEpayload", b"RIFFxxxxWEBPchanged-content"],
)
def test_mutated_bad_magic_or_bad_sha_is_blocked(tmp_path, replacement):
    bundle = make_bundle(tmp_path)
    mutate_artifact_file(bundle.artifacts[0], replacement)
    result = gate_core.create_wordpress_media_upload_intents(
        bundle.artifacts, settings()
    )
    assert result.status == "blocked"
    assert result.intents == ()
    assert result.to_safe_dict()["blocking_issues"] == [
        "wordpress_media_verified_webp_revalidation_failed"
    ]
    bundle.cleanup()


@pytest.mark.parametrize(
    "property_name,value",
    [
        ("output_mime_type", "image/jpeg"),
        ("output_mime_type", "image/png"),
        ("output_mime_type", "IMAGE/WEBP"),
        ("output_extension", ".jpg"),
        ("output_extension", ".WEBP"),
        ("output_extension", "webp"),
    ],
)
def test_exact_webp_mime_and_extension_are_required(tmp_path, property_name, value):
    bundle = make_bundle(tmp_path)
    try:
        with patch.object(
            conversion_core.VerifiedWebPArtifact,
            property_name,
            new_callable=PropertyMock,
            return_value=value,
        ):
            result = gate_core.create_wordpress_media_upload_intents(
                bundle.artifacts, settings()
            )
        assert result.status == "blocked"
        assert result.intents == ()
        assert "wordpress_media_requires_verified_webp" in (
            result.to_safe_dict()["blocking_issues"]
        )
    finally:
        bundle.cleanup()


def test_webp_verified_true_is_required(tmp_path):
    bundle = make_bundle(tmp_path)
    object.__setattr__(bundle.artifacts[0], "_webp_verified", False)
    result = gate_core.create_wordpress_media_upload_intents(
        bundle.artifacts, settings()
    )
    assert result.status == "blocked"
    assert result.intents == ()
    bundle.cleanup()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://sandbox.wpcomstaging.com",
        "https://staging-1d07-owenau512-iqjhz.wpcomstaging.com",
        "https://WPComStaging.com",
        "https://sub.deep.wpcomstaging.com",
        "https://sandbox.wpcomstaging.com:443",
        "https://sandbox.wpcomstaging.com/wordpress",
    ],
)
def test_https_wpcomstaging_targets_are_accepted(tmp_path, base_url):
    bundle, result = gate_one(tmp_path, current_settings=settings(wp_base_url=base_url))
    try:
        assert result.status == "ok"
        assert result.intents[0].target_is_staging is True
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://xxxxdoll.com", "https://www.xxxxdoll.com",
        "https://staging.xxxxdoll.com", "https://deep.staging.xxxxdoll.com",
        "HTTPS://XXXXDOLL.COM", "https://a.b.xxxxdoll.com/path",
    ],
)
def test_production_host_and_subdomains_are_blocked(tmp_path, base_url):
    bundle, result = gate_one(tmp_path, current_settings=settings(wp_base_url=base_url))
    try:
        assert result.status == "blocked"
        assert result.intents == ()
        assert result.to_safe_dict()["blocking_issues"] == [
            "wordpress_media_production_host_forbidden"
        ]
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "base_url,code",
    [
        ("http://sandbox.wpcomstaging.com", "wordpress_media_https_required"),
        ("ftp://sandbox.wpcomstaging.com", "wordpress_media_https_required"),
        ("sandbox.wpcomstaging.com", "wordpress_media_https_required"),
        ("", "wordpress_media_https_required"),
        ("https://example.com", "wordpress_media_staging_host_required"),
        ("https://wpcomstaging.com.evil.test", "wordpress_media_staging_host_required"),
        ("https://user@sandbox.wpcomstaging.com", "wordpress_media_target_url_invalid"),
        ("https://sandbox.wpcomstaging.com?token=x", "wordpress_media_target_url_invalid"),
        ("https://sandbox.wpcomstaging.com#fragment", "wordpress_media_target_url_invalid"),
        ("https://[invalid", "wordpress_media_target_url_invalid"),
    ],
)
def test_malformed_or_unsafe_staging_urls_are_blocked(tmp_path, base_url, code):
    bundle, result = gate_one(tmp_path, current_settings=settings(wp_base_url=base_url))
    try:
        assert result.status == "blocked"
        assert result.intents == ()
        assert result.to_safe_dict()["blocking_issues"] == [code]
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"sync_environment": "production"}, "wordpress_media_staging_environment_required"),
        ({"sync_environment": "STAGING"}, "wordpress_media_staging_environment_required"),
        ({"sync_environment": ""}, "wordpress_media_staging_environment_required"),
        ({"dry_run": False}, "wordpress_media_dry_run_required"),
        ({"dry_run": 1}, "wordpress_media_dry_run_required"),
        ({"default_product_status": "publish"}, "wordpress_media_draft_status_required"),
        ({"default_product_status": "Draft"}, "wordpress_media_draft_status_required"),
        ({"default_product_status": ""}, "wordpress_media_draft_status_required"),
        ({"allow_delete": True}, "wordpress_media_delete_must_remain_disabled"),
        ({"allow_delete": 0}, "wordpress_media_delete_must_remain_disabled"),
    ],
)
def test_runtime_safety_flags_fail_closed(tmp_path, overrides, code):
    bundle, result = gate_one(tmp_path, current_settings=settings(**overrides))
    try:
        assert result.status == "blocked"
        assert result.intents == ()
        assert result.to_safe_dict()["blocking_issues"] == [code]
    finally:
        bundle.cleanup()


def test_settings_validation_is_reused(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        with patch.object(Settings, "validate", autospec=True) as validate:
            result = gate_core.create_wordpress_media_upload_intents(
                bundle.artifacts, settings()
            )
        assert result.status == "ok"
        validate.assert_called_once()
    finally:
        bundle.cleanup()


def test_read_only_http_client_remains_get_head_only():
    assert http_client._ALLOWED_METHODS == frozenset({"GET", "HEAD"})
    source = inspect.getsource(http_client.ReadOnlyHttpClient.request)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert method not in http_client._ALLOWED_METHODS
    assert "Only GET and HEAD requests are permitted" in source


def test_gate_implements_no_http_transport():
    source = inspect.getsource(gate_core).casefold()
    for forbidden in (
        "import requests", "import urllib", "http.client", "readonlyhttpclient",
        ".request(", "wordpressmediauploadtransport",
    ):
        assert forbidden.casefold() not in source


def test_upload_filename_is_deterministic_and_contains_sha_prefix(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        first = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).intents[0]
        second = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).intents[0]
        assert first.upload_filename == second.upload_filename
        assert first.output_sha256[:gate_core.UPLOAD_FILENAME_SHA_PREFIX_LENGTH] in (
            first.upload_filename
        )
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "sku",
    [
        "CLM-ULTRA-SI170CM", "A/B", "A\\B", "../escape", "..\\escape",
        "SKU WITH SPACES", "SKU_CONTROL", "plus+sku", "dots...sku",
        "中文产品", "éxample", "a" * 300,
    ],
)
def test_upload_filename_is_ascii_safe_bounded_and_no_traversal(sku):
    artifact_shape = SimpleNamespace(
        sku=sku,
        selection_position=0,
        output_sha256="a" * 64,
    )
    filename = gate_core._upload_filename(artifact_shape)
    assert filename.isascii()
    assert len(filename) <= gate_core.MAX_UPLOAD_FILENAME_LENGTH
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*\.webp", filename)
    assert "/" not in filename
    assert "\\" not in filename
    assert ".." not in filename
    assert not any(ord(character) < 32 for character in filename)


def test_supplier_safe_name_is_not_used_as_upload_filename(tmp_path):
    bundle, result = gate_one(tmp_path)
    try:
        intent = result.intents[0]
        assert intent.source_safe_name == "supplier image 0.jpg"
        assert intent.upload_filename != intent.source_safe_name
    finally:
        bundle.cleanup()


def make_identity(tmp_path, *, sku="MOCK-001", position=0, data=JPEG):
    handle = make_handle(sku=sku, position=position, data=data, raw_id=f"id_{sku}_{position}")
    raw_id = handle_core._provider_file_id_for_download(handle)
    download = download_core.download_secure_media(
        (handle,), BytesGateway({raw_id: data}), workspace_parent=tmp_path
    )
    conversion = conversion_core.convert_verified_media_to_webp(
        download.artifacts, workspace_parent=tmp_path
    )
    result = gate_core.create_wordpress_media_upload_intents(
        conversion.artifacts, settings()
    )
    return ArtifactBundle(download, conversion), result.intents[0].media_identity


def test_media_identity_is_deterministic(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        first = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).intents[0]
        second = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).intents[0]
        assert first.media_identity == second.media_identity
        assert gate_core._MEDIA_IDENTITY_PATTERN.fullmatch(first.media_identity)
    finally:
        bundle.cleanup()


def test_identity_changes_when_output_sha_changes(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first_bundle, first = make_identity(one, data=image_bytes((10, 20, 30)))
    second_bundle, second = make_identity(two, data=image_bytes((30, 20, 10)))
    try:
        assert first != second
    finally:
        first_bundle.cleanup()
        second_bundle.cleanup()


@pytest.mark.parametrize("field,value", [("sku", "MOCK-002"), ("position", 1)])
def test_identity_changes_with_sku_or_position(tmp_path, field, value):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first_bundle, first = make_identity(one)
    kwargs = {field: value}
    second_bundle, second = make_identity(two, **kwargs)
    try:
        assert first != second
    finally:
        first_bundle.cleanup()
        second_bundle.cleanup()


def test_credentials_are_never_stored_or_projected(tmp_path):
    credentials = settings()
    bundle = make_bundle(tmp_path)
    try:
        result = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, credentials
        )
        intent = result.intents[0]
        combined = json.dumps(intent.to_safe_dict(), sort_keys=True) + repr(intent)
        for secret in (
            credentials.wp_username, credentials.wp_app_password,
            credentials.wc_consumer_key, credentials.wc_consumer_secret,
        ):
            assert secret not in combined
        slot_names = set(intent.__slots__)
        assert not {"wp_username", "wp_app_password", "wc_consumer_key", "wc_consumer_secret"} & slot_names
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "needle",
    [
        "local_webp_path", "temp_directory", "wp_base_url", "wp_username",
        "wp_app_password", "wc_consumer_key", "wc_consumer_secret",
        "authorization", "cookie", "provider_file_id", "raw_file_id",
        "provider_resource_id", "resource_key", "drive.google.com", "download_url",
        "access_token", "refresh_token", "private_key", "client_secret",
        "mock-app-password-not-read", "ck_mock_value_not_read_1234567890",
        "cs_mock_value_not_read_1234567890", "staging-unit-test.wpcomstaging.com",
        str(PROJECT_ROOT), "xxxxdoll-webp-", "xxxxdoll-secure-media-",
    ],
)
def test_safe_projection_and_repr_contain_no_authority_or_secret(tmp_path, needle):
    bundle, result = gate_one(tmp_path)
    try:
        value = json.dumps(result.to_safe_dict(), sort_keys=True) + repr(result.intents[0])
        assert needle.casefold() not in value.casefold()
    finally:
        bundle.cleanup()


def test_private_transport_grant_contains_only_expected_material(tmp_path):
    bundle, result = gate_one(tmp_path)
    try:
        intent = result.intents[0]
        material = gate_core._upload_material_for_transport(intent)
        assert material.upload_filename == intent.upload_filename
        assert material.mime_type == "image/webp"
        assert material.size_bytes == intent.output_size_bytes
        assert material.sha256 == intent.output_sha256
        assert material.target_host_fingerprint == intent.target_host_fingerprint
        assert not hasattr(material, "local_path")
        assert gate_core._local_upload_path_for_transport(material).is_file()
    finally:
        bundle.cleanup()


def test_private_transport_grant_revalidates_artifact_again(tmp_path):
    bundle, result = gate_one(tmp_path)
    intent = result.intents[0]
    original = conversion_core._local_webp_path_for_upload
    try:
        with patch.object(
            conversion_core, "_local_webp_path_for_upload", wraps=original
        ) as verified:
            gate_core._upload_material_for_transport(intent)
        verified.assert_called_once_with(bundle.artifacts[0])
    finally:
        bundle.cleanup()


def test_transport_grant_rejects_artifact_replaced_after_gate(tmp_path):
    bundle, result = gate_one(tmp_path)
    mutate_artifact_file(bundle.artifacts[0], b"RIFFxxxxWEBPreplaced")
    with pytest.raises(gate_core.WordPressMediaUploadGateError) as caught:
        gate_core._upload_material_for_transport(result.intents[0])
    assert str(caught.value) == "wordpress_media_verified_webp_revalidation_failed"
    bundle.cleanup()


@pytest.mark.parametrize("value", [None, {}, "intent", Path("intent"), object()])
def test_private_transport_grant_rejects_non_intent(value):
    with pytest.raises(gate_core.WordPressMediaUploadGateError):
        gate_core._upload_material_for_transport(value)


def test_forged_intent_rejected():
    forged = object.__new__(gate_core.WordPressMediaUploadIntent)
    with pytest.raises(gate_core.WordPressMediaUploadGateError):
        gate_core._upload_material_for_transport(forged)


def test_intent_constructor_is_capability_gated(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        with pytest.raises(gate_core.WordPressMediaUploadGateError):
            gate_core.WordPressMediaUploadIntent(
                object(), artifact=bundle.artifacts[0], target_binding="bad",
                upload_filename="bad.webp", media_identity="bad",
            )
    finally:
        bundle.cleanup()


def test_intent_is_immutable_and_not_pickleable(tmp_path):
    bundle, result = gate_one(tmp_path)
    try:
        intent = result.intents[0]
        with pytest.raises(AttributeError):
            intent.upload_filename = "changed.webp"
        with pytest.raises(TypeError):
            pickle.dumps(intent)
    finally:
        bundle.cleanup()


def test_target_binding_is_deterministic_without_full_url(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        first = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).intents[0]
        second = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).intents[0]
        assert first.target_host_fingerprint == second.target_host_fingerprint
        assert gate_core._TARGET_BINDING_PATTERN.fullmatch(
            first.target_host_fingerprint
        )
        assert "wpcomstaging.com" not in first.target_host_fingerprint
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "url",
    [
        "https://other.wpcomstaging.com",
        "https://staging-unit-test.wpcomstaging.com:8443",
        "https://staging-unit-test.wpcomstaging.com/subsite",
    ],
)
def test_target_binding_changes_with_target(url):
    assert gate_core._target_binding(settings(wp_base_url=url)) != gate_core._target_binding(settings())


def test_96_shape_success_and_order_preserved(tmp_path):
    bundle = make_bundle(tmp_path, 96)
    try:
        result = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        )
        report = result.to_safe_dict()
        assert result.status == "ok"
        assert report["summary"] == {
            "artifacts_received": 96,
            "gate_passed": 96,
            "gate_blocked": 0,
            "intents_created": 96,
        }
        assert len(result.intents) == 96
        assert sum(intent.image_role == "primary" for intent in result.intents) == 8
        assert sum(intent.image_role == "gallery" for intent in result.intents) == 88
        assert [(item.sku, item.selection_position) for item in result.intents] == [
            (item.sku, item.selection_position) for item in bundle.artifacts
        ]
        assert all(intent.output_mime_type == "image/webp" for intent in result.intents)
        assert all(intent.output_extension == ".webp" for intent in result.intents)
        assert all(conversion_core._valid_sha256(intent.output_sha256) for intent in result.intents)
        assert all(intent.target_is_staging for intent in result.intents)
        assert all(intent.upload_gate_passed for intent in result.intents)
    finally:
        bundle.cleanup()


@pytest.mark.parametrize("failure_index", [0, 47, 95])
def test_first_middle_final_failure_is_all_or_nothing(tmp_path, failure_index):
    bundle = make_bundle(tmp_path, 96)
    paths = [conversion_core._local_webp_path_for_upload(item) for item in bundle.artifacts]
    mutate_artifact_file(bundle.artifacts[failure_index], b"not-webp")
    result = gate_core.create_wordpress_media_upload_intents(
        bundle.artifacts, settings()
    )
    assert result.status == "blocked"
    assert result.intents == ()
    assert result.to_safe_dict()["summary"]["intents_created"] == 0
    assert all(path.exists() for path in paths)
    bundle.cleanup()


def test_95_pass_plus_final_failure_exposes_zero_intents(tmp_path):
    bundle = make_bundle(tmp_path, 96)
    mutate_artifact_file(bundle.artifacts[-1], b"not-webp")
    result = gate_core.create_wordpress_media_upload_intents(
        bundle.artifacts, settings()
    )
    assert result.to_safe_dict()["summary"]["gate_passed"] == 95
    assert result.to_safe_dict()["summary"]["intents_created"] == 0
    assert result.intents == ()
    bundle.cleanup()


def test_gate_does_not_cleanup_webp_lifecycle(tmp_path):
    bundle = make_bundle(tmp_path, 2)
    paths = [conversion_core._local_webp_path_for_upload(item) for item in bundle.artifacts]
    with patch.object(
        conversion_core.VerifiedWebPConversionBatchResult,
        "cleanup",
        wraps=bundle.conversion.cleanup,
    ) as cleanup:
        result = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        )
    assert result.status == "ok"
    cleanup.assert_not_called()
    assert all(path.exists() for path in paths)
    bundle.cleanup()


def test_noncanonical_and_duplicate_batches_are_rejected(tmp_path):
    bundle = make_bundle(tmp_path, 2)
    try:
        with pytest.raises(gate_core.WordPressMediaUploadGateError):
            gate_core.create_wordpress_media_upload_intents(
                tuple(reversed(bundle.artifacts)), settings()
            )
        with pytest.raises(gate_core.WordPressMediaUploadGateError):
            gate_core.create_wordpress_media_upload_intents(
                (bundle.artifacts[0], bundle.artifacts[0]), settings()
            )
    finally:
        bundle.cleanup()


def test_batch_limit_is_enforced_before_artifact_use(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        with pytest.raises(gate_core.WordPressMediaUploadGateError):
            gate_core.create_wordpress_media_upload_intents(
                (bundle.artifacts[0],) * (gate_core.MAX_ARTIFACTS_PER_BATCH + 1),
                settings(),
            )
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "field_name",
    [
        "policy_version", "sku", "selection_position", "image_role", "folder_role",
        "source_safe_name", "output_mime_type", "output_extension",
        "output_size_bytes", "output_sha256", "image_width", "image_height",
        "upload_filename", "media_identity", "target_host_fingerprint",
        "target_is_staging", "wordpress_resource", "upload_gate_passed", "alt_text",
        "warnings", "blocking_issues",
    ],
)
def test_intent_safe_field_schema(tmp_path, field_name):
    bundle, result = gate_one(tmp_path)
    try:
        assert field_name in result.intents[0].to_safe_dict()
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "field_name",
    [
        "status", "policy_version", "summary", "results", "warnings",
        "blocking_issues", "network_requests_performed",
        "wordpress_upload_requests_performed", "woocommerce_requests_performed",
        "external_write_requests_performed", "write_requests_performed",
    ],
)
def test_gate_safe_result_schema(tmp_path, field_name):
    bundle, result = gate_one(tmp_path)
    try:
        assert field_name in result.to_safe_dict()
    finally:
        bundle.cleanup()


@pytest.mark.parametrize(
    "counter",
    [
        "network_requests_performed", "wordpress_upload_requests_performed",
        "woocommerce_requests_performed", "external_write_requests_performed",
        "write_requests_performed",
    ],
)
def test_all_activity_counters_are_zero(tmp_path, counter):
    bundle, result = gate_one(tmp_path)
    try:
        assert result.to_safe_dict()[counter] == 0
    finally:
        bundle.cleanup()


def test_gate_output_is_deterministic(tmp_path):
    bundle = make_bundle(tmp_path, 3)
    try:
        first = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).to_safe_dict()
        second = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        ).to_safe_dict()
        assert first == second
    finally:
        bundle.cleanup()
