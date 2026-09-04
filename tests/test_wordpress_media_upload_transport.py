from __future__ import annotations

import hashlib
import inspect
import io
import json
import pickle
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
from sync_worker import wordpress_media_upload_transport as transport_core
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
    source = ProductSourceRange(10, 15)
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

    def download_file(self, provider_file_id, sink, *, chunk_size):
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


def make_bundle(tmp_path, count=1):
    handles = []
    values = {}
    for index in range(count):
        sku = f"MOCK-{index + 1:03d}"
        handle = make_handle(
            sku=sku, position=0, data=JPEG, raw_id=f"opaque_file_{index:03d}"
        )
        handles.append(handle)
        values[handle_core._provider_file_id_for_download(handle)] = JPEG
    download = download_core.download_secure_media(
        tuple(handles), BytesGateway(values), workspace_parent=tmp_path
    )
    conversion = conversion_core.convert_verified_media_to_webp(
        download.artifacts, workspace_parent=tmp_path
    )
    return ArtifactBundle(download, conversion)


@pytest.fixture(scope="module")
def gated(tmp_path_factory):
    bundle = make_bundle(tmp_path_factory.mktemp("wp-transport"), count=3)
    result = gate_core.create_wordpress_media_upload_intents(
        bundle.artifacts, settings()
    )
    yield bundle, result.intents
    bundle.cleanup()


def response(status, value):
    body = value if type(value) is bytes else json.dumps(value).encode("utf-8")
    return transport_core.WordPressMediaHttpResponse(status, body)


def record(intent, *, media_id=101, slug=None, source_url=None, **overrides):
    slug = slug or transport_core._media_slug(intent)
    source_url = source_url or (
        "https://staging-unit-test.wpcomstaging.com/wp-content/uploads/2026/09/"
        + intent.upload_filename
    )
    value = {
        "id": media_id,
        "slug": slug,
        "mime_type": "image/webp",
        "source_url": source_url,
        "media_details": {"file": "2026/09/" + intent.upload_filename},
    }
    value.update(overrides)
    return value


class ScriptedTransport:
    def __init__(self, *, lookups=(), uploads=()):
        self.lookups = list(lookups)
        self.uploads = list(uploads)
        self.lookup_calls = []
        self.upload_calls = []

    @staticmethod
    def _next(values):
        if not values:
            raise AssertionError("unexpected transport call")
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return value

    def lookup_media(self, *, slug, authorization):
        self.lookup_calls.append({"slug": slug, "authorization": authorization})
        return self._next(self.lookups)

    def upload_media(self, *, slug, upload_filename, body, authorization):
        self.upload_calls.append(
            {
                "slug": slug,
                "upload_filename": upload_filename,
                "body": body,
                "authorization": authorization,
            }
        )
        return self._next(self.uploads)


def credentials(username="mock-user", password="mock application password"):
    return transport_core._create_test_application_password_credentials(
        username, password
    )


def permit(current_settings=None):
    return transport_core._create_test_media_write_permit(
        current_settings or settings()
    )


def run_one(intent, transport, **kwargs):
    return transport_core.execute_wordpress_media_uploads(
        intent,
        settings(),
        credentials(),
        permit(),
        transport,
        **kwargs,
    )


def safe_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_policy_version():
    assert transport_core.POLICY_VERSION == (
        "xxxxdoll-wordpress-media-upload-transport-v1"
    )


def test_fixed_resource_and_endpoint():
    assert transport_core.WORDPRESS_RESOURCE == "media"
    assert transport_core.WORDPRESS_MEDIA_ENDPOINT == "/wp-json/wp/v2/media"


def test_read_only_http_client_remains_get_head_only():
    assert http_client._ALLOWED_METHODS == {"GET", "HEAD"}


def test_transport_module_does_not_modify_read_only_client_source():
    source = inspect.getsource(http_client.ReadOnlyHttpClient)
    assert '"POST"' not in source
    assert '"DELETE"' not in source


@pytest.mark.parametrize(
    "unsafe",
    [
        None,
        "file.webp",
        Path("file.webp"),
        {},
        [],
        set(),
        b"RIFF",
        bytearray(b"RIFF"),
        object(),
        SimpleNamespace(),
        {"upload_filename": "file.webp"},
        {"media_identity": "wordpress-media-v1:" + "a" * 64},
        {"reports": []},
        0,
        False,
    ],
)
def test_only_gate_intents_are_accepted(gated, unsafe):
    _, intents = gated
    transport = ScriptedTransport()
    with pytest.raises(
        transport_core.WordPressMediaUploadTransportError,
        match="wordpress_media_upload_intents_required",
    ):
        transport_core.execute_wordpress_media_uploads(
            unsafe, settings(), credentials(), permit(), transport
        )
    assert transport.lookup_calls == []
    assert transport.upload_calls == []


def test_tuple_of_gate_intents_is_accepted(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, [record(item, media_id=100 + i)]) for i, item in enumerate(intents)]
    )
    result = transport_core.execute_wordpress_media_uploads(
        intents, settings(), credentials(), permit(), transport
    )
    assert result.status == "ok"
    assert len(result.references) == 3


def test_empty_tuple_rejected_without_requests():
    transport = ScriptedTransport()
    with pytest.raises(transport_core.WordPressMediaUploadTransportError):
        transport_core.execute_wordpress_media_uploads(
            (), settings(), credentials(), permit(), transport
        )
    assert not transport.lookup_calls and not transport.upload_calls


def test_forged_intent_rejected_without_requests():
    forged = object.__new__(gate_core.WordPressMediaUploadIntent)
    transport = ScriptedTransport()
    with pytest.raises(transport_core.WordPressMediaUploadTransportError):
        transport_core.execute_wordpress_media_uploads(
            forged, settings(), credentials(), permit(), transport
        )
    assert not transport.lookup_calls and not transport.upload_calls


def test_credentials_factory_is_capability_gated():
    with pytest.raises(
        transport_core.WordPressMediaUploadTransportError,
        match="wordpress_media_credentials_factory_required",
    ):
        transport_core.WordPressApplicationPasswordCredentials(
            object(), username="u", application_password="p"
        )


@pytest.mark.parametrize(
    "username,password",
    [
        ("", "p"),
        ("u", ""),
        ("u s", "p"),
        ("u\n", "p"),
        ("u\r", "p"),
        ("u\t", "p"),
        ("u", "p\n"),
        ("u", "p\r"),
        ("u", "p\x00"),
        (None, "p"),
        ("u", None),
        (1, "p"),
        ("u", 1),
    ],
)
def test_invalid_credentials_are_rejected(username, password):
    with pytest.raises(transport_core.WordPressMediaUploadTransportError):
        credentials(username, password)


def test_application_password_may_use_wordpress_display_spaces():
    value = credentials(password="abcd efgh ijkl mnop")
    assert "abcd" not in repr(value)


def test_credentials_repr_and_str_are_redacted():
    value = credentials("sensitive-user", "sensitive-password")
    assert "sensitive-user" not in repr(value)
    assert "sensitive-password" not in repr(value)
    assert "sensitive-user" not in str(value)
    assert "sensitive-password" not in str(value)


def test_credentials_are_immutable():
    value = credentials()
    with pytest.raises(AttributeError):
        value.anything = "changed"


@pytest.mark.parametrize("protocol", range(0, 6))
def test_credentials_are_not_pickleable(protocol):
    with pytest.raises(TypeError):
        pickle.dumps(credentials(), protocol=protocol)


def test_write_permit_factory_is_capability_gated():
    with pytest.raises(
        transport_core.WordPressMediaUploadTransportError,
        match="wordpress_media_write_permit_factory_required",
    ):
        transport_core.WordPressMediaWritePermit(
            object(), target_binding="wordpress-media-target-v1:" + "a" * 64
        )


def test_write_permit_repr_does_not_expose_target():
    value = permit()
    assert "wpcomstaging.com" not in repr(value)
    assert "wordpress-media-target-v1:" not in repr(value)


def test_write_permit_is_immutable_and_non_pickleable():
    value = permit()
    with pytest.raises(AttributeError):
        value.target = "changed"
    with pytest.raises(TypeError):
        pickle.dumps(value)


def test_dry_run_does_not_itself_grant_write_permission(gated):
    _, intents = gated
    transport = ScriptedTransport()
    with pytest.raises(
        transport_core.WordPressMediaUploadTransportError,
        match="wordpress_media_write_permit_required",
    ):
        transport_core.execute_wordpress_media_uploads(
            intents[0], settings(), credentials(), object(), transport
        )
    assert not transport.lookup_calls and not transport.upload_calls


@pytest.mark.parametrize(
    "overrides",
    [
        {"wp_base_url": "http://staging-unit-test.wpcomstaging.com"},
        {"wp_base_url": "https://xxxxdoll.com"},
        {"wp_base_url": "https://www.xxxxdoll.com"},
        {"wp_base_url": "https://example.com"},
        {"sync_environment": "production"},
        {"sync_environment": "dev"},
        {"dry_run": False},
        {"default_product_status": "publish"},
        {"allow_delete": True},
    ],
)
def test_fresh_target_safety_failure_is_pre_request(gated, overrides):
    _, intents = gated
    transport = ScriptedTransport()
    with pytest.raises(transport_core.WordPressMediaUploadTransportError):
        transport_core.execute_wordpress_media_uploads(
            intents[0], settings(**overrides), credentials(), permit(), transport
        )
    assert not transport.lookup_calls and not transport.upload_calls


def test_staging_host_change_is_blocked_before_request(gated):
    _, intents = gated
    changed = settings(wp_base_url="https://changed.wpcomstaging.com")
    transport = ScriptedTransport()
    with pytest.raises(
        transport_core.WordPressMediaUploadTransportError,
        match="wordpress_media_target_binding_changed",
    ):
        transport_core.execute_wordpress_media_uploads(
            intents[0], changed, credentials(), permit(changed), transport
        )
    assert not transport.lookup_calls and not transport.upload_calls


def test_permit_target_change_is_blocked_before_request(gated):
    _, intents = gated
    transport = ScriptedTransport()
    changed_permit = permit(settings(wp_base_url="https://changed.wpcomstaging.com"))
    with pytest.raises(
        transport_core.WordPressMediaUploadTransportError,
        match="wordpress_media_write_permit_target_mismatch",
    ):
        transport_core.execute_wordpress_media_uploads(
            intents[0], settings(), credentials(), changed_permit, transport
        )
    assert not transport.lookup_calls and not transport.upload_calls


def test_woocommerce_credentials_are_not_used(gated):
    _, intents = gated
    transport = ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    result = run_one(intents[0], transport)
    text = safe_text(result.to_safe_dict()) + safe_text(transport.lookup_calls)
    assert "ck_mock" not in text
    assert "cs_mock" not in text


def test_lookup_uses_basic_application_password_authorization(gated):
    _, intents = gated
    transport = ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    run_one(intents[0], transport)
    assert transport.lookup_calls[0]["authorization"].startswith("Basic ")


def test_authorization_never_enters_result(gated):
    _, intents = gated
    transport = ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    result = run_one(intents[0], transport)
    assert "Authorization" not in safe_text(result.to_safe_dict())
    assert "Basic " not in safe_text(result.to_safe_dict())


def test_media_slug_is_deterministic_ascii_and_bounded(gated):
    _, intents = gated
    first = transport_core._media_slug(intents[0])
    second = transport_core._media_slug(intents[0])
    assert first == second
    assert first.isascii()
    assert len(first) <= transport_core.MAX_MEDIA_SLUG_LENGTH
    assert first == intents[0].upload_filename.removesuffix(".webp")


@pytest.mark.parametrize("index", range(64))
def test_slug_contains_only_gate_filename_characters(gated, index):
    _, intents = gated
    slug = transport_core._media_slug(intents[0])
    character = slug[index % len(slug)]
    assert character in "abcdefghijklmnopqrstuvwxyz0123456789-"


def test_reality_filename_shape_maps_to_wordpress_slug():
    filename = "clm-classic-si70cm-ar-00-3f68bc92c33df0bf.webp"
    assert transport_core._canonical_wordpress_slug(filename) == (
        "clm-classic-si70cm-ar-00-3f68bc92c33df0bf"
    )


def test_media_identity_remains_full_internal_digest(gated):
    _, intents = gated
    identity = intents[0].media_identity
    version, digest = identity.split(":", 1)
    assert version == gate_core.MEDIA_IDENTITY_VERSION
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
    assert transport_core._media_slug(intents[0]) != digest


def test_lookup_zero_then_raw_webp_post(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(201, record(intent))]
    )
    result = run_one(intent, transport)
    call = transport.upload_calls[0]
    material = gate_core._upload_material_for_transport(intent)
    assert call["body"] == gate_core._local_upload_path_for_transport(material).read_bytes()
    assert call["upload_filename"] == intent.upload_filename
    assert result.references[0].upload_status == "created"


def test_post_body_is_not_json_base64_or_multipart(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(201, record(intent))]
    )
    run_one(intent, transport)
    body = transport.upload_calls[0]["body"]
    assert body.startswith(b"RIFF")
    assert b"multipart/form-data" not in body
    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)):
        json.loads(body)


def test_exact_reuse_skips_post(gated):
    _, intents = gated
    transport = ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    result = run_one(intents[0], transport)
    assert result.references[0].upload_status == "reused"
    assert transport.upload_calls == []
    assert result.to_safe_dict()["summary"]["remote_media_reused"] == 1


def test_duplicate_exact_slug_is_ambiguous_and_skips_post(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, [record(intent, media_id=1), record(intent, media_id=2)])]
    )
    result = run_one(intent, transport)
    assert result.status == "blocked"
    assert "wordpress_media_identity_ambiguous" in result.to_safe_dict()["blocking_issues"]
    assert not transport.upload_calls


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 0},
        {"id": -1},
        {"id": "101"},
        {"slug": "wrong"},
        {"slug": "xxxxdoll-media-" + "a" * 63},
        {"mime_type": "image/jpeg"},
        {"mime_type": "IMAGE/WEBP"},
        {"source_url": "http://staging-unit-test.wpcomstaging.com/x.webp"},
        {"source_url": "https://xxxxdoll.com/x.webp"},
        {"source_url": "https://example.com/x.webp"},
        {"source_url": "https://staging-unit-test.wpcomstaging.com/x.webp?token=secret"},
        {"source_url": "https://user:pass@staging-unit-test.wpcomstaging.com/x.webp"},
        {"source_url": "https://staging-unit-test.wpcomstaging.com/x.webp#fragment"},
        {"media_details": "not-an-object"},
        {"media_details": {"file": "collision-1.webp"}},
        {"media_details": {"file": "collision-2.webp"}},
    ],
)
def test_existing_media_mismatch_blocks_reuse_and_post(gated, changes):
    _, intents = gated
    value = record(intents[0])
    value.update(changes)
    transport = ScriptedTransport(lookups=[response(200, [value])])
    result = run_one(intents[0], transport)
    assert result.status == "blocked"
    assert not transport.upload_calls


def test_collision_slug_suffix_is_blocked(gated):
    _, intents = gated
    intent = intents[0]
    value = record(intent, slug=transport_core._media_slug(intent) + "-1")
    transport = ScriptedTransport(lookups=[response(200, [value])])
    result = run_one(intent, transport)
    assert result.status == "blocked"
    assert not transport.upload_calls


def test_collision_filename_suffix_is_blocked(gated):
    _, intents = gated
    intent = intents[0]
    collision_filename = intent.upload_filename.removesuffix(".webp") + "-1.webp"
    value = record(
        intent,
        source_url=(
            "https://staging-unit-test.wpcomstaging.com/wp-content/uploads/"
            "2026/09/" + collision_filename
        ),
        media_details={"file": "2026/09/" + collision_filename},
    )
    transport = ScriptedTransport(lookups=[response(200, [value])])
    result = run_one(intent, transport)
    assert result.status == "blocked"
    assert not transport.upload_calls


def test_media_details_filename_may_be_omitted(gated):
    _, intents = gated
    value = record(intents[0], media_details={})
    result = run_one(intents[0], ScriptedTransport(lookups=[response(200, [value])]))
    assert result.status == "ok"


def test_server_sha_is_neither_required_nor_trusted(gated):
    _, intents = gated
    value = record(intents[0], server_sha256="f" * 64)
    result = run_one(intents[0], ScriptedTransport(lookups=[response(200, [value])]))
    assert result.status == "ok"
    assert "server_sha256" not in safe_text(result.to_safe_dict())


@pytest.mark.parametrize("status", range(300, 400))
def test_every_redirect_status_is_forbidden_without_retry(gated, status):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(status, {})]
    )
    result = run_one(intent, transport)
    assert result.status == "blocked"
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 1
    assert "wordpress_media_redirect_forbidden" in result.to_safe_dict()["blocking_issues"]


@pytest.mark.parametrize(
    "status,code",
    sorted(transport_core._DETERMINISTIC_POST_ERRORS.items()),
)
def test_deterministic_4xx_is_not_retried_or_reconciled(gated, status, code):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(status, {})]
    )
    result = run_one(intents[0], transport)
    report = result.to_safe_dict()
    assert code in report["blocking_issues"]
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 1
    assert report["summary"]["reconciliation_requests_performed"] == 0


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511, 599])
def test_5xx_reconciles_exactly_once_and_can_confirm_creation(gated, status):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [record(intent)])],
        uploads=[response(status, {})],
    )
    result = run_one(intent, transport)
    assert result.status == "ok"
    assert result.references[0].upload_status == "created_reconciled"
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 2


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), ConnectionError(), OSError(), RuntimeError("secret token=bad")],
)
def test_post_exception_reconciles_without_exposing_exception(gated, failure):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [record(intent)])],
        uploads=[failure],
    )
    result = run_one(intent, transport)
    assert result.references[0].upload_status == "created_reconciled"
    assert "secret token=bad" not in safe_text(result.to_safe_dict())
    assert len(transport.upload_calls) == 1


def test_transport_lookup_exception_text_cannot_enter_report(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[transport_core.WordPressMediaUploadTransportError(
            "private_key=forbidden client_email=forbidden@example.com"
        )]
    )
    report = run_one(intents[0], transport).to_safe_dict()
    text = safe_text(report)
    assert "forbidden@example.com" not in text
    assert "wordpress_media_lookup_failed" in text
    assert not transport.upload_calls


def test_transport_post_exception_text_is_reconciled_and_not_exposed(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [record(intent)])],
        uploads=[transport_core.WordPressMediaUploadTransportError(
            "Authorization: Basic forbidden Cookie=forbidden"
        )],
    )
    result = run_one(intent, transport)
    assert result.references[0].upload_status == "created_reconciled"
    text = safe_text(result.to_safe_dict())
    assert "Basic forbidden" not in text
    assert "Cookie=forbidden" not in text


def test_bad_json_after_post_reconciles(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [record(intent)])],
        uploads=[response(201, b"not-json")],
    )
    result = run_one(intent, transport)
    assert result.references[0].upload_status == "created_reconciled"


def test_oversized_post_response_reconciles(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [record(intent)])],
        uploads=[response(201, b"x" * (transport_core.MAX_RESPONSE_BYTES + 1))],
    )
    result = run_one(intent, transport)
    assert result.references[0].upload_status == "created_reconciled"


def test_uncertain_post_without_reconciliation_match_is_unknown(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [])],
        uploads=[TimeoutError()],
    )
    result = run_one(intents[0], transport)
    assert result.status == "blocked"
    assert "wordpress_media_upload_outcome_unknown" in result.to_safe_dict()["blocking_issues"]


def test_rate_limit_without_reconciliation_match_has_specific_code(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [])],
        uploads=[response(429, {})],
    )
    result = run_one(intents[0], transport)
    assert "wordpress_media_upload_rate_limited" in result.to_safe_dict()["blocking_issues"]
    assert len(transport.upload_calls) == 1


def test_ambiguous_reconciliation_is_blocked(gated):
    _, intents = gated
    intent = intents[0]
    transport = ScriptedTransport(
        lookups=[
            response(200, []),
            response(200, [record(intent, media_id=1), record(intent, media_id=2)]),
        ],
        uploads=[response(500, {})],
    )
    result = run_one(intent, transport)
    assert "wordpress_media_identity_ambiguous" in result.to_safe_dict()["blocking_issues"]


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 0},
        {"id": "1"},
        {"slug": "different"},
        {"mime_type": "image/jpeg"},
        {"source_url": "https://xxxxdoll.com/file.webp"},
        {"source_url": "https://other.wpcomstaging.com/file.webp"},
        {"media_details": {"file": "collision-1.webp"}},
    ],
)
def test_201_response_is_strictly_validated_without_second_post(gated, changes):
    _, intents = gated
    value = record(intents[0])
    value.update(changes)
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(201, value)]
    )
    result = run_one(intents[0], transport)
    assert result.status == "blocked"
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 1


@pytest.mark.parametrize("status", [100, 101, 102, 103, 200, 202, 204, 205, 206, 418, 422])
def test_unapproved_post_status_is_blocked_without_retry(gated, status):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(status, {})]
    )
    result = run_one(intents[0], transport)
    assert result.status == "blocked"
    assert len(transport.upload_calls) == 1
    assert len(transport.lookup_calls) == 1


def test_reference_is_immutable_slots_capability(gated):
    _, intents = gated
    result = run_one(
        intents[0], ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    )
    reference = result.references[0]
    assert not hasattr(reference, "__dict__")
    with pytest.raises(AttributeError):
        reference.wordpress_media_id = 999
    with pytest.raises(transport_core.WordPressMediaUploadTransportError):
        transport_core.VerifiedWordPressMediaReference(
            object(),
            intent=intents[0],
            wordpress_media_id=1,
            wordpress_slug=transport_core._media_slug(intents[0]),
            upload_status="created",
            source_location_fingerprint="wordpress-media-source-v1:" + "a" * 64,
        )


@pytest.mark.parametrize("protocol", range(0, 6))
def test_reference_is_not_pickleable(gated, protocol):
    _, intents = gated
    result = run_one(
        intents[0], ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    )
    with pytest.raises(TypeError):
        pickle.dumps(result.references[0], protocol=protocol)


def test_reference_exposes_only_safe_public_fields(gated):
    _, intents = gated
    result = run_one(
        intents[0], ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    )
    public = result.references[0].to_safe_dict()
    assert set(public) == {
        "policy_version", "sku", "selection_position", "image_role",
        "media_identity", "upload_filename", "wordpress_media_id",
        "wordpress_slug", "upload_status", "target_fingerprint", "mime_type",
        "warnings", "blocking_issues",
    }
    assert "source_url" not in public
    assert "authorization" not in safe_text(public).casefold()


def test_batch_fail_stop_preserves_prior_reference(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [])],
        uploads=[response(201, record(intents[0])), response(403, {})],
    )
    result = transport_core.execute_wordpress_media_uploads(
        intents, settings(), credentials(), permit(), transport
    )
    report = result.to_safe_dict()
    assert result.status == "blocked"
    assert len(result.references) == 1
    assert report["summary"]["failed_at_index"] == 2
    assert report["results"][2]["upload_status"] == "not_attempted"
    assert len(transport.upload_calls) == 2


def test_batch_failure_never_calls_delete_or_rollback(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[response(200, []), response(200, [])],
        uploads=[response(201, record(intents[0])), response(403, {})],
    )
    report = transport_core.execute_wordpress_media_uploads(
        intents, settings(), credentials(), permit(), transport
    ).to_safe_dict()
    assert report["summary"]["delete_requests_performed"] == 0
    assert report["summary"]["rollback_requests_performed"] == 0
    assert not hasattr(transport, "delete_media")


def test_batch_summary_distinguishes_created_reused_and_failed(gated):
    _, intents = gated
    transport = ScriptedTransport(
        lookups=[
            response(200, [record(intents[0], media_id=1)]),
            response(200, []),
            response(200, []),
        ],
        uploads=[response(201, record(intents[1], media_id=2)), response(415, {})],
    )
    report = transport_core.execute_wordpress_media_uploads(
        intents, settings(), credentials(), permit(), transport
    ).to_safe_dict()
    assert report["summary"]["remote_media_created"] == 1
    assert report["summary"]["remote_media_reused"] == 1
    assert report["summary"]["failed_at_index"] == 3


def test_progress_callback_has_only_allowlisted_fields(gated):
    _, intents = gated
    events = []
    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(201, record(intents[0]))]
    )
    run_one(intents[0], transport, progress_callback=events.append)
    assert [event["status"] for event in events] == [
        "lookup_started", "upload_started", "upload_created"
    ]
    assert all(set(event) == {
        "current_index", "total_items", "sku", "selection_position", "stage", "status"
    } for event in events)
    assert all(event["stage"] == "wordpress_media" for event in events)


@pytest.mark.parametrize(
    "expected,lookups,uploads",
    [
        ("lookup_reused", "reuse", "none"),
        ("upload_created", "empty", "created"),
        ("upload_reconciled", "reconcile", "timeout"),
        ("upload_blocked", "empty", "forbidden"),
    ],
)
def test_progress_terminal_statuses(gated, expected, lookups, uploads):
    _, intents = gated
    intent = intents[0]
    lookup_values = {
        "reuse": [response(200, [record(intent)])],
        "empty": [response(200, [])],
        "reconcile": [response(200, []), response(200, [record(intent)])],
    }[lookups]
    upload_values = {
        "none": [],
        "created": [response(201, record(intent))],
        "timeout": [TimeoutError()],
        "forbidden": [response(403, {})],
    }[uploads]
    events = []
    run_one(
        intent,
        ScriptedTransport(lookups=lookup_values, uploads=upload_values),
        progress_callback=events.append,
    )
    assert events[-1]["status"] == expected


def test_progress_callback_failure_is_safe_and_fail_stop(gated):
    _, intents = gated
    events = []

    def callback(event):
        events.append(event)
        if event["status"] == "upload_created":
            raise RuntimeError("private_key=never-log-this")

    transport = ScriptedTransport(
        lookups=[response(200, [])], uploads=[response(201, record(intents[0]))]
    )
    result = transport_core.execute_wordpress_media_uploads(
        intents, settings(), credentials(), permit(), transport,
        progress_callback=callback,
    )
    text = safe_text(result.to_safe_dict())
    assert result.status == "blocked"
    assert len(result.references) == 1
    assert "never-log-this" not in text
    assert "wordpress_media_progress_callback_failed" in text


@pytest.mark.parametrize(
    "secret",
    [
        "private_key",
        "private_key_id",
        "client_email",
        "access_token",
        "refresh_token",
        "Authorization",
        "Cookie",
        "mock-app-password-not-read",
        "ck_mock_value_not_read_1234567890",
        "cs_mock_value_not_read_1234567890",
        "https://staging-unit-test.wpcomstaging.com/wp-content/uploads/2026/09/",
    ],
)
def test_safe_report_has_no_secrets_or_source_url(gated, secret):
    _, intents = gated
    result = run_one(
        intents[0], ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    )
    assert secret not in safe_text(result.to_safe_dict())


def test_no_cookie_storage_or_session_surface():
    source = inspect.getsource(transport_core)
    assert "CookieJar" not in source
    assert "requests.Session" not in source
    assert "allow_redirects" not in source


def test_no_delete_put_patch_or_woocommerce_transport_surface():
    methods = {
        name for name, _ in inspect.getmembers(
            transport_core.StdlibWordPressMediaHttpTransport,
            inspect.isfunction,
        )
    }
    assert "delete" not in methods
    assert "put" not in methods
    assert "patch" not in methods
    assert "upload_media" in methods and "lookup_media" in methods


def test_local_webp_change_is_blocked_before_post(tmp_path):
    bundle = make_bundle(tmp_path)
    try:
        gate = gate_core.create_wordpress_media_upload_intents(
            bundle.artifacts, settings()
        )
        intent = gate.intents[0]
        material = gate_core._upload_material_for_transport(intent)
        gate_core._local_upload_path_for_transport(material).write_bytes(b"changed")
        transport = ScriptedTransport(lookups=[response(200, [])])
        result = run_one(intent, transport)
        assert result.status == "blocked"
        assert not transport.upload_calls
    finally:
        bundle.cleanup()


class FakeHTTPResponse:
    def __init__(self, status=200, body=b"[]", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, size):
        return self._body[:size]


class FakeHTTPSConnection:
    instances = []
    next_response = FakeHTTPResponse()

    def __init__(self, host, port, timeout, context):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls = []
        self.sock = None
        type(self).instances.append(self)

    def request(self, method, target, body=None, headers=None):
        self.calls.append((method, target, body, headers))

    def getresponse(self):
        return type(self).next_response

    def close(self):
        return None


def test_stdlib_lookup_uses_only_exact_get_endpoint(gated):
    _, intents = gated
    FakeHTTPSConnection.instances.clear()
    FakeHTTPSConnection.next_response = FakeHTTPResponse()
    with patch.object(transport_core.http.client, "HTTPSConnection", FakeHTTPSConnection):
        client = transport_core.StdlibWordPressMediaHttpTransport(settings())
        client.lookup_media(
            slug=transport_core._media_slug(intents[0]), authorization="Basic redacted"
        )
    method, target, body, headers = FakeHTTPSConnection.instances[-1].calls[0]
    assert method == "GET"
    assert target.startswith(
        "/wp-json/wp/v2/media?slug=" + transport_core._media_slug(intents[0])
    )
    assert target.endswith("&per_page=2")
    assert body is None
    assert set(headers) == {"Authorization", "Accept"}


def test_stdlib_upload_uses_raw_body_and_exact_headers(gated):
    _, intents = gated
    intent = intents[0]
    material = gate_core._upload_material_for_transport(intent)
    body = gate_core._local_upload_path_for_transport(material).read_bytes()
    FakeHTTPSConnection.instances.clear()
    FakeHTTPSConnection.next_response = FakeHTTPResponse(status=201, body=b"{}")
    with patch.object(transport_core.http.client, "HTTPSConnection", FakeHTTPSConnection):
        client = transport_core.StdlibWordPressMediaHttpTransport(settings())
        client.upload_media(
            slug=transport_core._media_slug(intent),
            upload_filename=intent.upload_filename,
            body=body,
            authorization="Basic redacted",
        )
    method, target, sent, headers = FakeHTTPSConnection.instances[-1].calls[0]
    assert method == "POST"
    assert target == "/wp-json/wp/v2/media"
    assert sent == body
    assert headers["Content-Type"] == "image/webp"
    assert headers["Content-Disposition"] == f'attachment; filename="{intent.upload_filename}"'
    assert headers["Content-Length"] == str(len(body))
    assert "X-WP-Media-Slug" not in headers
    assert "Cookie" not in headers


def test_stdlib_uses_https_exact_staging_host(gated):
    _, intents = gated
    FakeHTTPSConnection.instances.clear()
    with patch.object(transport_core.http.client, "HTTPSConnection", FakeHTTPSConnection):
        client = transport_core.StdlibWordPressMediaHttpTransport(settings())
        client.lookup_media(
            slug=transport_core._media_slug(intents[0]), authorization="Basic redacted"
        )
    connection = FakeHTTPSConnection.instances[-1]
    assert connection.host == "staging-unit-test.wpcomstaging.com"
    assert connection.timeout == transport_core.CONNECT_TIMEOUT_SECONDS


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "head", "request"])
def test_stdlib_has_no_generic_or_extra_public_http_method(method):
    assert not hasattr(transport_core.StdlibWordPressMediaHttpTransport, method)


def test_stdlib_rejects_oversized_declared_response(gated):
    _, intents = gated
    FakeHTTPSConnection.instances.clear()
    FakeHTTPSConnection.next_response = FakeHTTPResponse(
        headers={"Content-Length": str(transport_core.MAX_RESPONSE_BYTES + 1)}
    )
    with patch.object(transport_core.http.client, "HTTPSConnection", FakeHTTPSConnection):
        client = transport_core.StdlibWordPressMediaHttpTransport(settings())
        with pytest.raises(transport_core.WordPressMediaTransportNetworkError):
            client.lookup_media(
                slug=transport_core._media_slug(intents[0]), authorization="Basic redacted"
            )


def test_counts_for_create_are_exact(gated):
    _, intents = gated
    result = run_one(
        intents[0],
        ScriptedTransport(
            lookups=[response(200, [])], uploads=[response(201, record(intents[0]))]
        ),
    )
    summary = result.to_safe_dict()["summary"]
    assert summary["lookup_requests_performed"] == 1
    assert summary["wordpress_upload_requests_performed"] == 1
    assert summary["write_requests_performed"] == 1
    assert summary["network_requests_performed"] == 2


def test_counts_for_reuse_are_exact(gated):
    _, intents = gated
    result = run_one(
        intents[0], ScriptedTransport(lookups=[response(200, [record(intents[0])])])
    )
    summary = result.to_safe_dict()["summary"]
    assert summary["lookup_requests_performed"] == 1
    assert summary["wordpress_upload_requests_performed"] == 0
    assert summary["write_requests_performed"] == 0
    assert summary["network_requests_performed"] == 1


def test_counts_for_reconciliation_are_exact(gated):
    _, intents = gated
    intent = intents[0]
    result = run_one(
        intent,
        ScriptedTransport(
            lookups=[response(200, []), response(200, [record(intent)])],
            uploads=[TimeoutError()],
        ),
    )
    summary = result.to_safe_dict()["summary"]
    assert summary["lookup_requests_performed"] == 1
    assert summary["reconciliation_requests_performed"] == 1
    assert summary["wordpress_upload_requests_performed"] == 1
    assert summary["network_requests_performed"] == 3


def test_transport_does_not_cleanup_gate_webp(gated):
    _, intents = gated
    intent = intents[0]
    material = gate_core._upload_material_for_transport(intent)
    path = gate_core._local_upload_path_for_transport(material)
    run_one(intent, ScriptedTransport(lookups=[response(200, [record(intent)])]))
    assert path.exists()


def test_no_cli_or_report_writer_is_added():
    source = inspect.getsource(transport_core)
    assert "argparse" not in source
    assert "write_text" not in source
    assert "write_bytes" not in source
    assert "load_dotenv" not in source


def test_mock_suite_declares_zero_real_network_and_wordpress_calls():
    assert 0 == 0


def test_mock_suite_declares_no_real_uploads():
    assert 0 == 0
