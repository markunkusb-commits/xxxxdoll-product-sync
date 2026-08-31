from __future__ import annotations

import copy
import hashlib
import inspect
import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import secure_selected_media_handle as baseline_core
from sync_worker import selected_media_baseline_snapshot as snapshot


SHA_A = hashlib.sha256(b"a").hexdigest()
SHA_B = hashlib.sha256(b"b").hexdigest()
SHA_C = hashlib.sha256(b"c").hexdigest()
FP_ROOT = hashlib.sha256(b"root").hexdigest()
FP_NESTED = hashlib.sha256(b"nested").hexdigest()
FP_DEPTH2 = hashlib.sha256(b"depth2").hexdigest()
FP_FILE = hashlib.sha256(b"file").hexdigest()
MD5 = hashlib.md5(b"safe mock content").hexdigest()


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("external access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in (
        "load_config", "load_google_config", "load_google_drive_metadata_config",
        "load_google_sheets_readonly_config",
    ):
        monkeypatch.setattr(cli, name, denied)


def selection_item(
    *, sku="MOCK-001", start=10, end=20, name="photo-1.jpg",
    kind="nested", position=0, selected=True, folder=None, parent=None,
):
    if folder is None:
        folder = "Storefront Photos" if kind == "nested" else "Factory Photos Deep"
    if parent is None and kind == "depth2":
        parent = "Factory Photos"
    factory = kind == "depth2"
    if selected:
        image_role = "primary" if position == 0 else "gallery"
        if factory:
            reason = (
                "selected_factory_primary_fallback" if position == 0
                else "selected_factory_gallery_fill"
            )
        else:
            reason = (
                "selected_storefront_primary" if position == 0
                else "selected_storefront_gallery"
            )
    else:
        position = None
        image_role = "not_selected"
        reason = "not_selected_image_limit"
    return {
        "sku": sku,
        "product_source": {"start_row": start, "end_row": end},
        "source_manifest_kind": kind,
        "depth": 1 if kind == "nested" else 2 if kind == "depth2" else 0,
        "safe_folder_name": folder if kind != "root" else None,
        "parent_safe_folder_name": parent if kind == "depth2" else None,
        "safe_name": name,
        "folder_role": "factory_photos" if factory else "storefront_photos",
        "image_width": 2000,
        "image_height": 3000,
        "short_edge": 2000,
        "long_edge": 3000,
        "pixel_count": 6_000_000,
        "megapixels": 6.0,
        "size_bytes": 1_000_000,
        "orientation": "portrait",
        "quality_reason": "quality_pass",
        "min_short_edge_px": 1600,
        "min_megapixels": 3.0,
        "quality_policy_version": "xxxxdoll-image-quality-v1",
        "quality_eligible": True,
        "selected": selected,
        "selection_position": position,
        "image_role": image_role,
        "selection_reason": reason,
        "selection_policy_version": "xxxxdoll-image-selection-v1",
        "requires_deeper_inventory": kind == "depth2",
        "warnings": [],
        "blocking_issues": [],
    }


def selection_report(items):
    groups = {}
    for item in items:
        groups.setdefault(item["sku"], []).append(item)
    batches = []
    for sku, group in groups.items():
        selected = [item for item in group if item["selected"]]
        batches.append({
            "sku": sku,
            "total_candidates": len(group),
            "quality_candidates": len(group),
            "storefront_candidates": sum(item["folder_role"] == "storefront_photos" for item in group),
            "factory_candidates": sum(item["folder_role"] == "factory_photos" for item in group),
            "selected_count": len(selected),
            "selected_storefront": sum(item["folder_role"] == "storefront_photos" for item in selected),
            "selected_factory": sum(item["folder_role"] == "factory_photos" for item in selected),
            "primary_count": sum(item["image_role"] == "primary" for item in selected),
            "gallery_count": sum(item["image_role"] == "gallery" for item in selected),
            "warnings": [],
            "blocking_issues": [],
            "items": group,
        })
    counters = {
        "network_requests_performed": 0,
        "download_requests_performed": 0,
        "conversion_requests_performed": 0,
        "wordpress_upload_requests_performed": 0,
        "write_requests_performed": 0,
    }
    return {
        "status": "ok",
        "policy_version": "xxxxdoll-image-selection-v1",
        "source_quality_policy_version": "xxxxdoll-image-quality-v1",
        "summary": {
            "selected_total": sum(item["selected"] for item in items),
            "total_skus": len(groups),
            "blocking_assets": 0,
            **counters,
        },
        "results": batches,
        **counters,
    }


def historical_item(
    name="photo-1.jpg", *, fingerprint=FP_FILE, checksum=MD5,
    mime="image/jpeg", image_candidate=True, kind="image_candidate",
    modified="2026-01-01T00:00:00Z",
):
    return {
        "safe_name": name,
        "mime_type": mime,
        "size_bytes": 1_000_000,
        "modified_time": modified,
        "provider_content_checksum": checksum,
        "file_id_fingerprint": fingerprint,
        "item_kind": kind,
        "image_candidate": image_candidate,
        "image_width": 2000,
        "image_height": 3000,
        "warnings": [],
    }


def manifest_result(
    item=None, *, sku="MOCK-001", start=10, end=20, kind="nested",
    folder=None, parent=None,
):
    items = [] if item is None else [item]
    if folder is None:
        folder = "Storefront Photos" if kind == "nested" else "Factory Photos Deep"
    if parent is None:
        parent = "Factory Photos"
    common = {
        "sku": sku,
        "product_source": {"start_row": start, "end_row": end},
        "root_folder_id_fingerprint": FP_ROOT,
        "depth": 1 if kind == "nested" else 2,
        "status": "listed" if items else "empty_folder",
        "item_count": len(items),
        "image_candidate_count": sum(value["image_candidate"] for value in items),
        "nested_folder_at_depth_limit_count": sum(value["item_kind"] == "nested_folder" for value in items),
        "shortcut_count": sum(value["item_kind"] == "shortcut" for value in items),
        "google_workspace_file_count": sum(value["item_kind"] == "google_workspace_file" for value in items),
        "other_file_count": sum(value["item_kind"] == "other_file" for value in items),
        "duplicate_name_candidate_count": sum("duplicate_name_candidate" in value["warnings"] for value in items),
        "duplicate_content_candidate_count": sum("duplicate_content_candidate" in value["warnings"] for value in items),
        "pages_read": 1,
        "items": items,
        "warnings": [],
        "blocking_issues": [],
    }
    if kind == "nested":
        return {
            **common,
            "nested_folder_id_fingerprint": FP_NESTED,
            "safe_folder_name": folder,
        }
    return {
        **common,
        "depth1_folder_id_fingerprint": FP_NESTED,
        "depth2_folder_id_fingerprint": FP_DEPTH2,
        "depth1_safe_folder_name": parent,
        "depth2_safe_folder_name": folder,
    }


def historical_report(results, kind="nested"):
    report = {
        "status": "ok",
        "inputs": {"mapping": "reports/mock-mapping.json", "sheet": "Mock", "sku_report": "reports/mock-sku.json"},
        "summary": {"download_requests_performed": 0, "write_requests_performed": 0},
        "results": results,
        "root_issues": [],
        "warnings": [],
        "blocking_issues": [],
        "write_requests_performed": 0,
    }
    if kind == "depth2":
        report["depth1_issues"] = []
    return report


def valid_inputs(item=None, manifest=None):
    item = selection_item() if item is None else item
    manifest = manifest_result(historical_item(name=item["safe_name"])) if manifest is None else manifest
    return (
        selection_report([item]),
        historical_report([manifest]),
        historical_report([], "depth2"),
    )


def build(selection=None, nested=None, depth2=None):
    if selection is None or nested is None or depth2 is None:
        defaults = valid_inputs()
        selection = defaults[0] if selection is None else selection
        nested = defaults[1] if nested is None else nested
        depth2 = defaults[2] if depth2 is None else depth2
    return snapshot.build_selected_media_baseline_snapshot(
        selection, nested, depth2,
        selection_report_sha256=SHA_A,
        nested_baseline_report_sha256=SHA_B,
        depth2_baseline_report_sha256=SHA_C,
    )


def write_inputs(root: Path, selection=None, nested=None, depth2=None):
    if selection is None or nested is None or depth2 is None:
        defaults = valid_inputs()
        selection = defaults[0] if selection is None else selection
        nested = defaults[1] if nested is None else nested
        depth2 = defaults[2] if depth2 is None else depth2
    paths = (
        root / "image-selection-dry-run.json",
        root / "google-drive-nested-folder-manifest-dry-run.json",
        root / "google-drive-depth2-folder-manifest-dry-run.json",
    )
    for path, value in zip(paths, (selection, nested, depth2), strict=True):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return paths


def duplicate_item_result(result):
    changed = copy.deepcopy(result)
    changed["items"].append(copy.deepcopy(changed["items"][0]))
    changed["item_count"] = 2
    changed["image_candidate_count"] = 2
    return changed


def test_001_snapshot_version():
    assert snapshot.SNAPSHOT_VERSION == "xxxxdoll-selected-media-baseline-snapshot-v1"


def test_002_handle_version_unchanged():
    assert baseline_core.POLICY_VERSION == "xxxxdoll-secure-selected-media-handle-v1"


def test_003_cli_registered():
    args = cli.build_parser().parse_args([
        "freeze-selected-media-baseline", "--selection-report", "selection.json",
        "--nested-baseline", "nested.json", "--depth2-baseline", "depth2.json",
    ])
    assert args.command == "freeze-selected-media-baseline"


@pytest.mark.parametrize("missing", ["--selection-report", "--nested-baseline", "--depth2-baseline"])
def test_004_required_cli_arguments(missing):
    values = {
        "--selection-report": "selection.json",
        "--nested-baseline": "nested.json",
        "--depth2-baseline": "depth2.json",
    }
    argv = ["freeze-selected-media-baseline"]
    for flag, value in values.items():
        if flag != missing:
            argv.extend((flag, value))
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


def test_007_valid_snapshot():
    report = build()
    assert report["status"] == "ok"
    assert report["summary"]["baseline_created"] == 1


@pytest.mark.parametrize("field,expected", [
    ("selection_report_sha256", SHA_A),
    ("nested_baseline_report_sha256", SHA_B),
    ("depth2_baseline_report_sha256", SHA_C),
])
def test_008_report_hash_bindings(field, expected):
    assert build()[field] == expected


@pytest.mark.parametrize("field", [
    "policy_version", "sku", "product_source", "source_manifest_kind", "depth",
    "safe_folder_name", "parent_safe_folder_name", "safe_name",
    "file_id_fingerprint", "md5_checksum", "source_mime_type", "size_bytes",
    "image_width", "image_height",
])
def test_011_baseline_identity_allowlist(field):
    assert field in build()["results"][0]["baseline_identity"]


@pytest.mark.parametrize("field", sorted(snapshot._FORBIDDEN_OUTPUT_KEYS))
def test_025_forbidden_authority_absent(field):
    assert field not in json.dumps(build(), sort_keys=True).casefold()


def test_044_selected_only():
    chosen = selection_item()
    skipped = selection_item(name="photo-2.jpg", selected=False)
    report = build(selection=selection_report([chosen, skipped]))
    assert [item["baseline_identity"]["safe_name"] for item in report["results"]] == ["photo-1.jpg"]


def test_045_formal_baseline_core_reused():
    original = baseline_core.create_selected_media_baseline_identity
    with patch.object(baseline_core, "create_selected_media_baseline_identity", wraps=original) as core:
        build()
    core.assert_called_once()


def test_046_provider_file_id_none_during_core_call():
    original = baseline_core.create_selected_media_baseline_identity

    def inspect_manifest(selection, manifest):
        assert all(item.provider_file_id is None for item in manifest.items)
        return original(selection, manifest)

    with patch.object(baseline_core, "create_selected_media_baseline_identity", side_effect=inspect_manifest):
        build()


@pytest.mark.parametrize("field,value,code", [
    ("sku", "MOCK-002", "baseline_snapshot_source_missing"),
    ("start_row", 11, "baseline_snapshot_source_missing"),
    ("end_row", 21, "baseline_snapshot_source_missing"),
    ("depth", 2, "invalid_nested_baseline_depth"),
    ("safe_folder_name", "Other Photos", "baseline_snapshot_source_missing"),
])
def test_047_exact_nested_join_mismatch(field, value, code):
    selection, nested, depth2 = valid_inputs()
    if field in {"start_row", "end_row"}:
        nested["results"][0]["product_source"][field] = value
    else:
        nested["results"][0][field] = value
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match=code):
        build(selection, nested, depth2)


@pytest.mark.parametrize("field,value", [
    ("sku", "mock-001"),
    ("safe_folder_name", "storefront Photos"),
])
def test_052_case_sensitive_nested_join(field, value):
    selection, nested, depth2 = valid_inputs()
    nested["results"][0][field] = value
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="baseline_snapshot_source_missing"):
        build(selection, nested, depth2)


def test_054_safe_name_case_sensitive():
    selection, nested, depth2 = valid_inputs()
    nested["results"][0]["items"][0]["safe_name"] = "Photo-1.jpg"
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="baseline_snapshot_source_missing"):
        build(selection, nested, depth2)


def test_055_missing_join_blocks():
    selection, _, depth2 = valid_inputs()
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="baseline_snapshot_source_missing"):
        build(selection, historical_report([]), depth2)


def test_056_ambiguous_join_blocks_without_first_match():
    selection, nested, depth2 = valid_inputs()
    nested["results"][0] = duplicate_item_result(nested["results"][0])
    with patch.object(baseline_core, "create_selected_media_baseline_identity") as core:
        with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="baseline_snapshot_source_ambiguous"):
            build(selection, nested, depth2)
    core.assert_not_called()


@pytest.mark.parametrize("value,code", [
    (None, "baseline_snapshot_fingerprint_missing"),
    ("bad", "baseline_snapshot_fingerprint_invalid"),
    ("A" * 64, "baseline_snapshot_fingerprint_invalid"),
    (123, "baseline_snapshot_fingerprint_invalid"),
])
def test_057_fingerprint_fail_closed(value, code):
    selection, nested, depth2 = valid_inputs()
    nested["results"][0]["items"][0]["file_id_fingerprint"] = value
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match=code):
        build(selection, nested, depth2)


@pytest.mark.parametrize("value,code", [
    (None, "baseline_snapshot_checksum_missing"),
    ("bad", "baseline_snapshot_checksum_invalid"),
    ("G" * 32, "baseline_snapshot_checksum_invalid"),
    (123, "baseline_snapshot_checksum_invalid"),
])
def test_061_checksum_fail_closed(value, code):
    selection, nested, depth2 = valid_inputs()
    nested["results"][0]["items"][0]["provider_content_checksum"] = value
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match=code):
        build(selection, nested, depth2)


@pytest.mark.parametrize("kind,candidate", [("other_file", False), ("image_candidate", False)])
def test_065_image_candidate_required(kind, candidate):
    selection, nested, depth2 = valid_inputs()
    item = nested["results"][0]["items"][0]
    item["item_kind"] = kind
    item["image_candidate"] = candidate
    nested["results"][0]["image_candidate_count"] = 0
    nested["results"][0]["other_file_count"] = int(kind == "other_file")
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="baseline_snapshot_source_not_image_candidate"):
        build(selection, nested, depth2)


@pytest.mark.parametrize("counter", snapshot._SELECTION_COUNTERS)
def test_067_nonzero_selection_counter_blocks(counter):
    selection, nested, depth2 = valid_inputs()
    selection[counter] = 1
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="selection_report_not_offline"):
        build(selection, nested, depth2)


@pytest.mark.parametrize("counter", snapshot._SELECTION_COUNTERS)
def test_072_nonzero_selection_summary_counter_blocks(counter):
    selection, nested, depth2 = valid_inputs()
    selection["summary"][counter] = 1
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="selection_report_not_offline"):
        build(selection, nested, depth2)


@pytest.mark.parametrize("mutation,code", [
    (lambda report: report.update(status="partial"), "selection_report_status_not_ok"),
    (lambda report: report.update(policy_version="wrong"), "selection_policy_version_mismatch"),
    (lambda report: report["summary"].update(blocking_assets=1), "selection_report_contains_blockers"),
    (lambda report: report["summary"].update(selected_total=2), "selection_summary_mismatch"),
    (lambda report: report["summary"].update(total_skus=2), "selection_summary_mismatch"),
    (lambda report: report["results"][0].update(selected_count=2), "selection_batch_count_mismatch"),
    (lambda report: report["results"][0].update(sku="OTHER-001"), "selection_batch_sku_mismatch"),
    (lambda report: report["results"][0]["items"][0].update(selection_policy_version="wrong"), "selection_policy_version_mismatch"),
])
def test_077_selection_contract_validation(mutation, code):
    selection, nested, depth2 = valid_inputs()
    mutation(selection)
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match=code):
        build(selection, nested, depth2)


@pytest.mark.parametrize("kind,mutation,code", [
    ("nested", lambda report: report.update(status="partial"), "nested_baseline_status_not_ok"),
    ("depth2", lambda report: report.update(status="partial"), "depth2_baseline_status_not_ok"),
    ("nested", lambda report: report.update(write_requests_performed=1), "historical_report_contains_writes"),
    ("depth2", lambda report: report.update(write_requests_performed=1), "historical_report_contains_writes"),
    ("nested", lambda report: report["blocking_issues"].append("blocked"), "nested_baseline_contains_blockers"),
    ("depth2", lambda report: report["blocking_issues"].append("blocked"), "depth2_baseline_contains_blockers"),
])
def test_085_historical_report_validation(kind, mutation, code):
    selection, nested, depth2 = valid_inputs()
    target = nested if kind == "nested" else depth2
    mutation(target)
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match=code):
        build(selection, nested, depth2)


def test_091_uses_results_not_manifests():
    selection, nested, depth2 = valid_inputs()
    nested["manifests"] = nested.pop("results")
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="invalid_nested_baseline_report"):
        build(selection, nested, depth2)


def test_092_historical_checksum_mapping():
    assert build()["results"][0]["baseline_identity"]["md5_checksum"] == MD5


def test_093_modified_time_not_output_or_identity():
    selection, nested, depth2 = valid_inputs()
    nested["results"][0]["items"][0]["modified_time"] = "2030-12-31T23:59:59Z"
    assert "modified_time" not in json.dumps(build(selection, nested, depth2))


def test_094_modified_time_does_not_change_identity():
    selection, nested, depth2 = valid_inputs()
    first = build(selection, nested, depth2)["results"][0]["baseline_identity"]
    nested["results"][0]["items"][0]["modified_time"] = None
    second = build(selection, nested, depth2)["results"][0]["baseline_identity"]
    assert first == second


def test_095_depth2_exact_join():
    item = selection_item(kind="depth2")
    selection = selection_report([item])
    depth2 = historical_report([manifest_result(historical_item(), kind="depth2")], "depth2")
    report = build(selection, historical_report([]), depth2)
    assert report["summary"]["baseline_depth2"] == 1


@pytest.mark.parametrize("field,value,code", [
    ("depth1_safe_folder_name", "Other Parent", "baseline_snapshot_source_missing"),
    ("depth2_safe_folder_name", "Other Folder", "baseline_snapshot_source_missing"),
    ("depth", 1, "invalid_depth2_baseline_depth"),
])
def test_096_depth2_provenance_mismatch(field, value, code):
    item = selection_item(kind="depth2")
    selection = selection_report([item])
    result = manifest_result(historical_item(), kind="depth2")
    result[field] = value
    depth2 = historical_report([result], "depth2")
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match=code):
        build(selection, historical_report([]), depth2)


def test_099_selection_audit_preserved():
    result = build()["results"][0]
    assert result == {
        "selection_position": 0,
        "image_role": "primary",
        "folder_role": "storefront_photos",
        "selection_reason": "selected_storefront_primary",
        "baseline_identity": result["baseline_identity"],
    }


def test_100_imani_name_and_position_preserved():
    item = selection_item(sku="IMANI-001", name="SiW160 Amara(Cinnamon) 2.jpg")
    manifest = manifest_result(historical_item("SiW160 Amara(Cinnamon) 2.jpg"), sku="IMANI-001")
    report = build(*valid_inputs(item, manifest))
    result = report["results"][0]
    assert result["selection_position"] == 0
    assert result["baseline_identity"]["safe_name"] == "SiW160 Amara(Cinnamon) 2.jpg"


def test_101_deterministic_output_under_batch_order_change():
    first = selection_item(sku="MOCK-002", start=30, end=40)
    second = selection_item(sku="MOCK-001", name="photo-2.jpg")
    selection = selection_report([first, second])
    nested = historical_report([
        manifest_result(historical_item(), sku="MOCK-002", start=30, end=40),
        manifest_result(historical_item("photo-2.jpg"), sku="MOCK-001"),
    ])
    forward = build(selection, nested, historical_report([], "depth2"))
    selection["results"].reverse()
    nested["results"].reverse()
    reverse = build(selection, nested, historical_report([], "depth2"))
    assert forward == reverse


def test_102_position_order_not_filename_natural_sort():
    first = selection_item(name="photo-10.jpg", position=0)
    second = selection_item(name="photo-1.jpg", position=1)
    selection = selection_report([first, second])
    nested = historical_report([manifest_result(
        historical_item("photo-10.jpg")
    )])
    second_manifest = manifest_result(historical_item("photo-1.jpg"))
    nested["results"].append(second_manifest)
    report = build(selection, nested, historical_report([], "depth2"))
    assert [item["baseline_identity"]["safe_name"] for item in report["results"]] == ["photo-10.jpg", "photo-1.jpg"]


def test_103_inputs_not_mutated():
    values = valid_inputs()
    before = copy.deepcopy(values)
    build(*values)
    assert values == before


@pytest.mark.parametrize("summary_field", [
    "selected_items", "baseline_created", "baseline_nested", "baseline_depth2",
    "baseline_missing", "baseline_ambiguous", "missing_fingerprint",
    "invalid_fingerprint", "missing_checksum", "invalid_checksum",
    "jpeg_baselines", "blocking_items",
])
def test_104_summary_contract(summary_field):
    assert summary_field in build()["summary"]


def test_116_mock_aggregate_96_without_hardcoding_core():
    selected = []
    nested_results = []
    depth2_results = []
    for index in range(96):
        sku = f"MOCK-{index + 1:03d}"
        start = index + 1
        name = f"photo-{index + 1}.jpg"
        kind = "nested" if index < 84 else "depth2"
        item = selection_item(sku=sku, start=start, end=start, name=name, kind=kind)
        selected.append(item)
        result = manifest_result(
            historical_item(name, fingerprint=hashlib.sha256(name.encode()).hexdigest()),
            sku=sku, start=start, end=start, kind=kind,
        )
        (nested_results if kind == "nested" else depth2_results).append(result)
    report = build(
        selection_report(selected),
        historical_report(nested_results),
        historical_report(depth2_results, "depth2"),
    )
    assert report["summary"] | {} == report["summary"]
    assert report["summary"]["baseline_created"] == 96
    assert report["summary"]["baseline_nested"] == 84
    assert report["summary"]["baseline_depth2"] == 12
    assert report["summary"]["jpeg_baselines"] == 96


@pytest.mark.parametrize("counter", snapshot._OUTPUT_COUNTERS)
def test_117_output_counters_zero(counter):
    assert build()[counter] == 0


@pytest.mark.parametrize("needle", [
    "googleapiclient", "OfficialGoogleClientFactory", "GoogleDriveMetadataGateway",
    "requests.", "urllib", "PIL", "ImageMagick", "cwebp", "ffmpeg", "subprocess",
])
def test_124_snapshot_module_has_no_external_or_media_tool(needle):
    assert needle not in inspect.getsource(snapshot)


def test_134_no_selection_rerun():
    assert "select_images(" not in inspect.getsource(snapshot)


def test_135_no_config_or_env_loading():
    source = inspect.getsource(snapshot)
    assert "load_config" not in source
    assert "dotenv" not in source


def test_136_output_refuses_overwrite_before_input_read(tmp_path):
    output = tmp_path / "reports" / snapshot.REPORT_FILENAME
    output.parent.mkdir()
    output.write_text("historical", encoding="utf-8")
    with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read inputs")):
        with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="selected_media_baseline_snapshot_already_exists"):
            snapshot.run_selected_media_baseline_snapshot(
                tmp_path / "missing-a.json", tmp_path / "missing-b.json",
                tmp_path / "missing-c.json", project_root=tmp_path,
            )
    assert output.read_text(encoding="utf-8") == "historical"


def test_137_local_run_writes_snapshot_once(tmp_path):
    paths = write_inputs(tmp_path)
    report, output = snapshot.run_selected_media_baseline_snapshot(*paths, project_root=tmp_path)
    assert output == tmp_path / "reports" / snapshot.REPORT_FILENAME
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_138_raw_byte_hashes_used(tmp_path):
    paths = write_inputs(tmp_path)
    raw_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    report, _ = snapshot.run_selected_media_baseline_snapshot(*paths, project_root=tmp_path)
    assert [
        report["selection_report_sha256"],
        report["nested_baseline_report_sha256"],
        report["depth2_baseline_report_sha256"],
    ] == raw_hashes


@pytest.mark.parametrize("index,name", [
    (0, "wrong-selection.json"),
    (1, "wrong-nested.json"),
    (2, "wrong-depth2.json"),
])
def test_139_only_expected_input_report_names(tmp_path, index, name):
    paths = list(write_inputs(tmp_path))
    wrong = tmp_path / name
    wrong.write_bytes(paths[index].read_bytes())
    paths[index] = wrong
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="unexpected_baseline_input_file"):
        snapshot.run_selected_media_baseline_snapshot(*paths, project_root=tmp_path)


@pytest.mark.parametrize("bad_hash", ["", "a" * 63, "a" * 65, "A" * 64, None, 123])
def test_142_invalid_audit_hash_rejected(bad_hash):
    selection, nested, depth2 = valid_inputs()
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="invalid_selection_report_sha256"):
        snapshot.build_selected_media_baseline_snapshot(
            selection, nested, depth2,
            selection_report_sha256=bad_hash,
            nested_baseline_report_sha256=SHA_B,
            depth2_baseline_report_sha256=SHA_C,
        )


def test_148_duplicate_json_key_rejected(tmp_path):
    paths = list(write_inputs(tmp_path))
    paths[0].write_text('{"status":"ok","status":"ok"}', encoding="utf-8")
    with pytest.raises(snapshot.SelectedMediaBaselineSnapshotError, match="duplicate_json_key"):
        snapshot.run_selected_media_baseline_snapshot(*paths, project_root=tmp_path)


def test_149_cli_runner_is_local_and_returns_zero(tmp_path, monkeypatch):
    paths = write_inputs(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    code = cli.main([
        "freeze-selected-media-baseline",
        "--selection-report", str(paths[0]),
        "--nested-baseline", str(paths[1]),
        "--depth2-baseline", str(paths[2]),
    ])
    assert code == 0


def test_150_cli_existing_snapshot_returns_two(tmp_path, monkeypatch):
    paths = write_inputs(tmp_path)
    output = tmp_path / "reports" / snapshot.REPORT_FILENAME
    output.parent.mkdir()
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    code = cli.main([
        "freeze-selected-media-baseline",
        "--selection-report", str(paths[0]),
        "--nested-baseline", str(paths[1]),
        "--depth2-baseline", str(paths[2]),
    ])
    assert code == 2
    assert output.read_text(encoding="utf-8") == "keep"
