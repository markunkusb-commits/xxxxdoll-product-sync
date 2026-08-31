from __future__ import annotations

import copy
import hashlib
import inspect
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import folder_role_policy as folder_core
from sync_worker import google_drive_folder_manifest as root_core
from sync_worker import google_drive_nested_folder_manifest as nested_core
from sync_worker import google_drive_depth2_folder_manifest as depth2_core
from sync_worker import image_selection_policy as selection_core
from sync_worker import secure_selected_media_handle as handle_core
from sync_worker import selected_media_baseline_snapshot as snapshot_core
from sync_worker import selected_media_handle_preparation as prep
from sync_worker.google_drive_folder_manifest_dry_run import RootDriveManifestRead
from sync_worker.image_mapping import ProductSourceRange


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
        monkeypatch.setattr(cli, name, denied)


class FakeGateway:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.counters = SimpleNamespace(read_requests_performed=0)
        self.calls = []

    def list_folder_children(self, folder_id, *, page_token=None, page_size=100):
        self.counters.read_requests_performed += 1
        self.calls.append((folder_id, page_token, page_size))
        value = self.responses.get(folder_id, {"files": []})
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def selection(
    *, sku="MOCK-001", source=ProductSourceRange(10, 20), name="photo-1.jpg",
    kind="nested", folder="Storefront Photos", parent=None, position=0,
):
    factory = kind == "depth2"
    role = folder_core.FolderRole.FACTORY_PHOTOS if factory else folder_core.FolderRole.STOREFRONT_PHOTOS
    image_role = selection_core.ImageSelectionRole.PRIMARY if position == 0 else selection_core.ImageSelectionRole.GALLERY
    reason = {
        (False, 0): selection_core.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY,
        (False, 1): selection_core.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY,
        (True, 0): selection_core.ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK,
        (True, 1): selection_core.ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL,
    }[(factory, 0 if position == 0 else 1)]
    return selection_core.ImageSelectionItem(
        sku=sku, folder_role=role, safe_name=name,
        source_manifest_kind=kind, depth=1 if kind == "nested" else 2,
        safe_folder_name=folder, parent_safe_folder_name=parent,
        product_source=source, requires_deeper_inventory=factory,
        quality_eligible=True, selected=True, selection_position=position,
        image_role=image_role, selection_reason=reason,
    )


def file_payload(name="photo-1.jpg", raw_id="file-1", *, md5="a" * 32, mime="image/jpeg", size=1000, width=2000, height=3000, modified="2026-01-01T00:00:00Z"):
    return {
        "id": raw_id, "name": name, "mimeType": mime, "size": str(size),
        "modifiedTime": modified, "md5Checksum": md5,
        "imageMediaMetadata": {"width": width, "height": height, "rotation": 0},
    }


def folder_item(name, raw_id):
    return root_core.DriveManifestItem(
        safe_name=name, mime_type=root_core.FOLDER_MIME_TYPE,
        size_bytes=None, modified_time="2026-01-01T00:00:00Z", md5_checksum=None,
        file_id_fingerprint=root_core.fingerprint_drive_id(raw_id),
        item_kind="nested_folder", image_candidate=False,
        image_candidate_status=None, image_width=None, image_height=None,
        image_rotation=None, warnings=("nested_folder_not_traversed",),
        provider_file_id=raw_id,
    )


def root_manifest(sku, source, *items):
    return root_core.GoogleDriveFolderManifest(
        sku=sku, product_source=source,
        folder_id_fingerprint=root_core.fingerprint_drive_id("root-" + sku),
        status="listed", items=tuple(items), pages_read=1,
    )


def root_read(manifests, reads=None):
    manifests = tuple(manifests)
    summary = root_core._summary(manifests, len(manifests) if reads is None else reads)
    return RootDriveManifestRead(
        core_batch=root_core.GoogleDriveFolderManifestBatchResult(manifests, summary),
        blocked_results=(), sku_joined=len(manifests), sku_join_not_found=0,
        sku_join_ambiguous=0,
        drive_read_requests_performed=len(manifests) if reads is None else reads,
        forbidden_values=(),
    )


def safe_baseline(item, *, raw_id="file-1", md5="a" * 32, mime="image/jpeg", size=1000, width=2000, height=3000):
    return {
        "policy_version": handle_core.POLICY_VERSION,
        "sku": item.sku, "product_source": item.product_source.to_dict(),
        "source_manifest_kind": item.source_manifest_kind, "depth": item.depth,
        "safe_folder_name": item.safe_folder_name,
        "parent_safe_folder_name": item.parent_safe_folder_name,
        "safe_name": item.safe_name,
        "file_id_fingerprint": root_core.fingerprint_drive_id(raw_id),
        "md5_checksum": md5, "source_mime_type": mime,
        "size_bytes": size, "image_width": width, "image_height": height,
    }


def baseline(item, **kwargs):
    return handle_core.restore_selected_media_baseline_identity(item, safe_baseline(item, **kwargs))


def one_case(kind="nested"):
    if kind == "nested":
        item = selection()
        roots = [root_manifest(item.sku, item.product_source, folder_item(item.safe_folder_name, "folder-1"))]
        gateway = FakeGateway({"folder-1": {"files": [file_payload()]}})
    else:
        item = selection(kind="depth2", folder="Factory Deep", parent="Factory Photos")
        roots = [root_manifest(item.sku, item.product_source, folder_item("Factory Photos", "folder-parent"))]
        gateway = FakeGateway({
            "folder-parent": {"files": [{"id": "folder-deep", "name": "Factory Deep", "mimeType": root_core.FOLDER_MIME_TYPE, "modifiedTime": "2026-01-01T00:00:00Z"}]},
            "folder-deep": {"files": [file_payload()]},
        })
    return (item,), (baseline(item),), root_read(roots), gateway


def run_case(kind="nested"):
    items, baselines, roots, gateway = one_case(kind)
    return prep.prepare_selected_media_handles_from_fresh_root(
        items, baselines, roots, gateway, sheets_read_requests_performed=1,
    )


def snapshot_report(items, identities, selection_bytes=b"selection"):
    results = []
    for item, identity in zip(items, identities, strict=True):
        results.append({
            "selection_position": item.selection_position,
            "image_role": item.image_role.value,
            "folder_role": item.folder_role.value,
            "selection_reason": item.selection_reason.value,
            "baseline_identity": identity,
        })
    summary = {
        "selected_items": len(items), "baseline_created": len(identities),
        "baseline_nested": sum(item.source_manifest_kind == "nested" for item in items),
        "baseline_depth2": sum(item.source_manifest_kind == "depth2" for item in items),
        "baseline_missing": 0, "baseline_ambiguous": 0,
        "missing_fingerprint": 0, "invalid_fingerprint": 0,
        "missing_checksum": 0, "invalid_checksum": 0,
        "jpeg_baselines": sum(value["source_mime_type"] == "image/jpeg" for value in identities),
        "blocking_items": 0,
    }
    return {
        "status": "ok", "snapshot_version": snapshot_core.SNAPSHOT_VERSION,
        "source_selection_policy_version": selection_core.POLICY_VERSION,
        "source_handle_policy_version": handle_core.POLICY_VERSION,
        "selection_report_sha256": hashlib.sha256(selection_bytes).hexdigest(),
        "nested_baseline_report_sha256": "b" * 64,
        "depth2_baseline_report_sha256": "c" * 64,
        "summary": summary, "results": results,
        "network_requests_performed": 0, "drive_read_requests_performed": 0,
        "download_requests_performed": 0, "media_read_requests_performed": 0,
        "conversion_requests_performed": 0,
        "wordpress_upload_requests_performed": 0,
        "write_requests_performed": 0,
    }


def current_shape():
    items = []
    baselines = []
    roots = []
    responses = {}
    for sku_index in range(8):
        sku = f"MOCK-{sku_index + 1:03d}"
        source = ProductSourceRange(100 + sku_index * 10, 109 + sku_index * 10)
        root_folders = []
        if sku_index < 7:
            folder_count = 2 if sku_index < 2 else 1
            per_folder = 6 if folder_count == 2 else 12
            for folder_index in range(folder_count):
                folder = f"Storefront {folder_index + 1}"
                folder_id = f"folder-{sku_index}-{folder_index}"
                root_folders.append(folder_item(folder, folder_id))
                files = []
                for local in range(per_folder):
                    position = folder_index * per_folder + local
                    name = f"photo-{position + 1}.jpg"
                    raw_id = f"file-{sku_index}-{position}"
                    selected = selection(sku=sku, source=source, name=name, folder=folder, position=position)
                    items.append(selected); baselines.append(baseline(selected, raw_id=raw_id))
                    files.append(file_payload(name, raw_id))
                responses[folder_id] = {"files": files}
        else:
            parent, deep = "Factory Photos", "Factory Deep"
            root_folders.append(folder_item(parent, "folder-parent-7"))
            responses["folder-parent-7"] = {"files": [{"id": "folder-deep-7", "name": deep, "mimeType": root_core.FOLDER_MIME_TYPE, "modifiedTime": "2026-01-01T00:00:00Z"}]}
            files = []
            for position in range(12):
                name = "SiW160 Amara(Cinnamon) 2.jpg" if position == 0 else f"factory-{position + 1}.jpg"
                raw_id = f"file-7-{position}"
                selected = selection(sku=sku, source=source, name=name, kind="depth2", folder=deep, parent=parent, position=position)
                items.append(selected); baselines.append(baseline(selected, raw_id=raw_id))
                files.append(file_payload(name, raw_id))
            responses["folder-deep-7"] = {"files": files}
        roots.append(root_manifest(sku, source, *root_folders))
    return tuple(items), tuple(baselines), root_read(roots, reads=8), FakeGateway(responses)


def test_001_policy_version():
    assert prep.POLICY_VERSION == "xxxxdoll-selected-media-handle-preparation-v1"


def test_002_valid_nested_handle():
    result = run_case()
    assert result.status == "ok" and len(result.handles) == 1


def test_003_valid_depth2_handle():
    result = run_case("depth2")
    assert result.status == "ok" and result.handles[0].depth == 2


def test_004_handles_hidden_from_repr():
    assert "SecureSelectedMediaHandle" not in repr(run_case())


def test_005_report_does_not_serialize_handles():
    assert "handles" not in run_case().to_safe_report_dict()


@pytest.mark.parametrize("missing", ["--selection-report", "--baseline-snapshot", "--mapping", "--sheet", "--sku-report"])
def test_006_cli_required(missing):
    values = {"--selection-report": "a.json", "--baseline-snapshot": "b.json", "--mapping": "c.json", "--sheet": "Mock", "--sku-report": "d.json"}
    argv = ["prepare-selected-media-handles"]
    for flag, value in values.items():
        if flag != missing: argv.extend((flag, value))
    with pytest.raises(SystemExit): cli.build_parser().parse_args(argv)


def test_011_cli_registered():
    assert "prepare-selected-media-handles" in cli.build_parser().format_help()


@pytest.mark.parametrize("extra", [
    "provider_file_id", "raw_file_id", "raw_folder_id", "raw_nested_folder_id",
    "raw_depth2_folder_id", "provider_resource_id", "resource_key", "url",
    "download_url", "local_path", "credentials", "authorization", "cookie",
    "token", "client_secret", "download_ready", "wordpress_upload_ready",
])
def test_012_baseline_extra_field_rejected(extra):
    item = selection(); value = safe_baseline(item); value[extra] = "unsafe"
    with pytest.raises(handle_core.SecureSelectedMediaHandleError, match="invalid_safe_baseline_identity"):
        handle_core.restore_selected_media_baseline_identity(item, value)


@pytest.mark.parametrize("field,value", [
    ("sku", "MOCK-002"), ("source_manifest_kind", "depth2"), ("depth", 2),
    ("safe_folder_name", "Other"), ("parent_safe_folder_name", "Parent"),
    ("safe_name", "Other.jpg"),
])
def test_029_baseline_provenance_exact(field, value):
    item = selection(); identity = safe_baseline(item); identity[field] = value
    with pytest.raises(handle_core.SecureSelectedMediaHandleError, match="selected_media_baseline_provenance_mismatch"):
        handle_core.restore_selected_media_baseline_identity(item, identity)


@pytest.mark.parametrize("field,value", [
    ("file_id_fingerprint", None), ("file_id_fingerprint", "bad"),
    ("md5_checksum", None), ("md5_checksum", "bad"),
    ("source_mime_type", "bad mime"), ("size_bytes", -1),
    ("image_width", 0), ("image_height", 0),
])
def test_035_baseline_identity_format(field, value):
    item = selection(); identity = safe_baseline(item); identity[field] = value
    with pytest.raises(handle_core.SecureSelectedMediaHandleError):
        handle_core.restore_selected_media_baseline_identity(item, identity)


def test_043_baseline_has_no_provider_authority():
    value = baseline(selection())
    assert not hasattr(value, "provider_file_id")
    with pytest.raises(handle_core.SecureSelectedMediaHandleError):
        handle_core._provider_file_id_for_download(value)


def test_044_snapshot_restore_success():
    item = selection(); raw = safe_baseline(item); report = snapshot_report((item,), (raw,))
    restored = prep._restore_snapshot(report, (item,), hashlib.sha256(b"selection").hexdigest())
    assert restored[0].to_safe_dict() == raw


@pytest.mark.parametrize("field,value,code", [
    ("snapshot_version", "wrong", "baseline_snapshot_version_mismatch"),
    ("source_handle_policy_version", "wrong", "handle_policy_version_mismatch"),
    ("source_selection_policy_version", "wrong", "selection_policy_version_mismatch"),
    ("status", "blocked", "baseline_snapshot_status_not_ok"),
])
def test_045_snapshot_contract(field, value, code):
    item = selection(); report = snapshot_report((item,), (safe_baseline(item),)); report[field] = value
    with pytest.raises(prep.SelectedMediaHandlePreparationError, match=code):
        prep._restore_snapshot(report, (item,), hashlib.sha256(b"selection").hexdigest())


def test_049_selection_sha_mismatch():
    item = selection(); report = snapshot_report((item,), (safe_baseline(item),))
    with pytest.raises(prep.SelectedMediaHandlePreparationError, match="selection_snapshot_hash_mismatch"):
        prep._restore_snapshot(report, (item,), "0" * 64)


@pytest.mark.parametrize("counter", ["network_requests_performed", "drive_read_requests_performed", *prep._ZERO_COUNTERS])
def test_050_snapshot_nonzero_counter(counter):
    item = selection(); report = snapshot_report((item,), (safe_baseline(item),)); report[counter] = 1
    with pytest.raises(prep.SelectedMediaHandlePreparationError, match="baseline_snapshot_not_offline"):
        prep._restore_snapshot(report, (item,), hashlib.sha256(b"selection").hexdigest())


def test_057_nested_factory_reused():
    original = nested_core.create_secure_google_drive_nested_folder_handle
    with patch.object(nested_core, "create_secure_google_drive_nested_folder_handle", wraps=original) as mocked:
        run_case()
    mocked.assert_called_once()


def test_058_depth2_factory_reused():
    original = depth2_core.create_secure_google_drive_depth2_folder_handle
    with patch.object(depth2_core, "create_secure_google_drive_depth2_folder_handle", wraps=original) as mocked:
        run_case("depth2")
    mocked.assert_called_once()


def test_059_handle_core_reused():
    original = handle_core.create_secure_selected_media_handle
    with patch.object(handle_core, "create_secure_selected_media_handle", wraps=original) as mocked:
        run_case()
    mocked.assert_called_once()


@pytest.mark.parametrize("mutation,code", [
    (lambda payload: payload.update(id="changed-id"), "selected_media_file_identity_changed"),
    (lambda payload: payload.update(md5Checksum="b" * 32), "selected_media_content_changed"),
    (lambda payload: payload.update(mimeType="image/png"), "selected_media_metadata_changed"),
    (lambda payload: payload.update(size="1001"), "selected_media_metadata_changed"),
    (lambda payload: payload["imageMediaMetadata"].update(width=2001), "selected_media_metadata_changed"),
    (lambda payload: payload["imageMediaMetadata"].update(height=3001), "selected_media_metadata_changed"),
])
def test_060_drift_blocks(mutation, code):
    items, baselines, roots, gateway = one_case()
    mutation(gateway.responses["folder-1"]["files"][0])
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    assert result.status == "blocked" and result.handles == ()
    assert code in result.to_safe_report_dict()["results"][0]["blocking_issues"]


def test_066_modified_time_only_does_not_block():
    items, baselines, roots, gateway = one_case()
    gateway.responses["folder-1"]["files"][0]["modifiedTime"] = "2030-01-01T00:00:00Z"
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    assert result.status == "ok"


def test_067_fresh_file_missing():
    items, baselines, roots, gateway = one_case(); gateway.responses["folder-1"] = {"files": []}
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    assert result.handles == () and result.status == "blocked"


def test_068_fresh_file_ambiguous():
    items, baselines, roots, gateway = one_case(); gateway.responses["folder-1"]["files"].append(file_payload(raw_id="file-2"))
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    assert "selected_media_source_ambiguous" in result.to_safe_report_dict()["results"][0]["blocking_issues"]


@pytest.mark.parametrize("folder_name", ["Missing", "storefront photos", "Storefront", "Photos"])
def test_069_depth1_exact_missing(folder_name):
    items, baselines, roots, gateway = one_case()
    changed = root_manifest(items[0].sku, items[0].product_source, folder_item(folder_name, "folder-1"))
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, root_read([changed]), gateway, sheets_read_requests_performed=1)
    assert result.handles == () and result.status == "blocked"


def test_073_depth1_ambiguous():
    items, baselines, _, gateway = one_case()
    root = root_manifest(items[0].sku, items[0].product_source, folder_item("Storefront Photos", "folder-1"), folder_item("Storefront Photos", "folder-2"))
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, root_read([root]), gateway, sheets_read_requests_performed=1)
    assert result.to_safe_report_dict()["summary"]["depth1_folder_ambiguous"] == 1


def test_074_root_missing():
    items, baselines, _, gateway = one_case()
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, root_read([]), gateway, sheets_read_requests_performed=1)
    assert result.handles == ()


def test_075_no_depth3():
    assert "depth3" not in inspect.getsource(prep).casefold()


def test_076_current_shape_96():
    items, baselines, roots, gateway = current_shape()
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    summary = result.to_safe_report_dict()["summary"]
    assert result.status == "ok" and len(result.handles) == 96
    assert (summary["required_root_sources"], summary["required_depth1_folders"], summary["required_depth2_folders"]) == (8, 10, 1)
    assert (summary["nested_handles"], summary["depth2_handles"], summary["primary_handles"], summary["gallery_handles"]) == (84, 12, 8, 88)
    assert (summary["sheets_read_requests_performed"], summary["root_drive_read_requests_performed"], summary["depth1_drive_read_requests_performed"], summary["depth2_drive_read_requests_performed"], summary["network_requests_performed"]) == (1, 8, 10, 1, 20)


def test_077_current_shape_folder_reads_not_image_reads():
    items, baselines, roots, gateway = current_shape()
    prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    assert len(gateway.calls) == 11


def test_078_imani_preserved():
    items, baselines, roots, gateway = current_shape()
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    imani = [value for value in result.to_safe_report_dict()["results"] if value["safe_name"] == "SiW160 Amara(Cinnamon) 2.jpg"][0]
    assert imani["selection_position"] == 0


def test_079_one_blocker_clears_all_handles():
    items, baselines, roots, gateway = current_shape()
    gateway.responses["folder-deep-7"]["files"][0]["md5Checksum"] = "b" * 32
    result = prep.prepare_selected_media_handles_from_fresh_root(items, baselines, roots, gateway, sheets_read_requests_performed=1)
    assert result.status == "blocked" and result.handles == ()
    assert result.to_safe_report_dict()["summary"]["handles_prepared"] == 95


@pytest.mark.parametrize("field", [
    "sku", "selection_position", "image_role", "folder_role", "source_manifest_kind",
    "depth", "safe_folder_name", "parent_safe_folder_name", "safe_name",
    "baseline_file_id_fingerprint", "fresh_file_id_fingerprint",
    "baseline_md5_checksum", "fresh_md5_checksum", "baseline_mime_type",
    "fresh_mime_type", "baseline_size_bytes", "fresh_size_bytes",
    "baseline_image_width", "fresh_image_width", "baseline_image_height",
    "fresh_image_height", "handle_status", "warnings", "blocking_issues",
])
def test_080_report_item_fields(field):
    assert field in run_case().to_safe_report_dict()["results"][0]


@pytest.mark.parametrize("field", [
    "selected_items", "baseline_restored", "required_root_sources",
    "required_depth1_folders", "required_depth2_folders", "root_sources_ok",
    "root_sources_blocked", "depth1_folders_listed", "depth1_folder_missing",
    "depth1_folder_ambiguous", "depth2_folders_listed", "depth2_folder_missing",
    "depth2_folder_ambiguous", "handles_prepared", "handles_blocked",
    "nested_handles", "depth2_handles", "primary_handles", "gallery_handles",
    "file_identity_changed", "content_changed", "metadata_changed",
    "source_missing", "source_ambiguous", "sheets_read_requests_performed",
    "root_drive_read_requests_performed", "depth1_drive_read_requests_performed",
    "depth2_drive_read_requests_performed", "network_requests_performed",
    "download_requests_performed", "media_read_requests_performed",
    "conversion_requests_performed", "wordpress_upload_requests_performed",
    "write_requests_performed",
])
def test_104_summary_fields(field):
    assert field in run_case().to_safe_report_dict()["summary"]


@pytest.mark.parametrize("needle", [
    "provider_file_id", "raw_file_id", "raw_folder_id", "raw_nested_folder_id",
    "raw_depth2_folder_id", "provider_resource_id", "resource_key",
    "drive.google.com", "download_url", "local_path", "authorization",
    "cookie", "access_token", "client_secret", "credentials",
])
def test_138_safe_report_forbidden(needle):
    assert needle not in json.dumps(run_case().to_safe_report_dict(), sort_keys=True).casefold()


@pytest.mark.parametrize("counter", prep._ZERO_COUNTERS)
def test_153_zero_activity_counters(counter):
    report = run_case().to_safe_report_dict()
    assert report[counter] == 0 and report["summary"][counter] == 0


@pytest.mark.parametrize("needle", [
    "get_media", "alt=media", "PIL", "ImageMagick", "cwebp", "ffmpeg",
    "requests.get", "urllib.request", "wordpress_client", "pickle", "asdict(",
])
def test_158_no_download_conversion_upload_code(needle):
    assert needle.casefold() not in inspect.getsource(prep).casefold()


def test_169_deterministic_report():
    assert run_case().to_safe_report_dict() == run_case().to_safe_report_dict()


def test_170_tuple_order_sku_position():
    items, baselines, roots, gateway = current_shape()
    result = prep.prepare_selected_media_handles_from_fresh_root(tuple(reversed(items)), tuple(reversed(baselines)), roots, gateway, sheets_read_requests_performed=1)
    keys = [(item.sku, item.selection_position) for item in result.handles]
    assert keys == sorted(keys)


def test_171_local_validation_failure_precedes_client_creation(tmp_path):
    paths = [tmp_path / name for name in ("selection.json", "baseline.json", "mapping.json", "sku.json")]
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    calls = []

    class Factory:
        def create_drive_metadata_clients(self, settings):
            calls.append(settings)
            raise AssertionError("client must not be created")

    with patch.object(
        prep, "validate_preparation_inputs",
        side_effect=prep.SelectedMediaHandlePreparationError("selection_snapshot_hash_mismatch"),
    ):
        with pytest.raises(prep.SelectedMediaHandlePreparationError, match="selection_snapshot_hash_mismatch"):
            prep.run_selected_media_handle_preparation(
                paths[0], paths[1], paths[2], "Mock Sheet", paths[3],
                SimpleNamespace(), Factory(), project_root=tmp_path,
            )
    assert calls == []


@pytest.mark.parametrize("name", [
    "google-drive-folder-manifest-dry-run.json",
    "google-drive-nested-folder-manifest-dry-run.json",
    "google-drive-depth2-folder-manifest-dry-run.json",
])
def test_172_no_intermediate_report_writes(name):
    assert name not in inspect.getsource(prep)


def test_175_arbitrary_exception_text_is_not_propagated():
    assert prep._safe_error(ValueError("rawprovideridentity"), "safe_failure") == "safe_failure"


@pytest.mark.parametrize("field", ["nested_baseline_report_sha256", "depth2_baseline_report_sha256"])
def test_176_malformed_historical_hash_rejected(field):
    item = selection(); report = snapshot_report((item,), (safe_baseline(item),)); report[field] = "bad"
    with pytest.raises(prep.SelectedMediaHandlePreparationError, match="malformed_baseline_snapshot"):
        prep._restore_snapshot(report, (item,), hashlib.sha256(b"selection").hexdigest())


def test_178_snapshot_summary_must_match_records():
    item = selection(); report = snapshot_report((item,), (safe_baseline(item),))
    report["summary"]["baseline_created"] = 2
    with pytest.raises(prep.SelectedMediaHandlePreparationError, match="baseline_snapshot_summary_mismatch"):
        prep._restore_snapshot(report, (item,), hashlib.sha256(b"selection").hexdigest())
