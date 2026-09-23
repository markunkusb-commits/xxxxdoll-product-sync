from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import woocommerce_batch_workspace as workspace  # noqa: E402


BATCH_HASH = "a" * 64
PLAN_BYTES = b'{"safe":"single-product-plan"}\n'
PLAN_SHA = hashlib.sha256(PLAN_BYTES).hexdigest()


def report(*, manifest_sha: str = "b" * 64) -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": "xxxxdoll-woo-batch-frozen-plan-v1",
        "batch_hash": BATCH_HASH,
        "target": {"environment": "staging"},
        "execution_policy": {
            "mode": "sequential",
            "concurrency": 1,
            "failure_policy": "stop_on_non_success",
        },
        "source_manifest": {
            "basename": "batch-manifest.json",
            "sha256": manifest_sha,
        },
        "items": [
            {
                "sequence": 1,
                "sku": "SKU-ONE",
                "plan_hash": "c" * 64,
                "source_plan": {
                    "basename": workspace.PLAN_FILENAME,
                    "sha256": PLAN_SHA,
                },
            }
        ],
        "blocking_issues": [],
        "write_authorized": False,
        "network_requests_performed": 0,
        "woocommerce_requests_performed": 0,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }


def copies() -> tuple[workspace.BatchPlanCopy, ...]:
    return (workspace.BatchPlanCopy(1, PLAN_BYTES, PLAN_SHA),)


def copied_plan_path(root: Path) -> Path:
    return (
        root
        / workspace.ITEMS_DIRECTORY
        / "000001"
        / workspace.AUTHORITIES_DIRECTORY
        / workspace.PLAN_FILENAME
    )


def publish_reservation_path(output_root: Path) -> Path:
    return output_root / (
        f"{workspace.PUBLISH_RESERVATION_PREFIX}{BATCH_HASH}.reservation"
    )


def seed_publish_reservation(
    output_root: Path,
    *,
    owner_pid: int = 424_242,
) -> tuple[Path, bytes]:
    output_root.mkdir(parents=True, exist_ok=True)
    value = {
        "policy_version": workspace.PUBLISH_RESERVATION_POLICY_VERSION,
        "batch_hash": BATCH_HASH,
        "owner_pid": owner_pid,
        "owner_token": "d" * 32,
    }
    raw = workspace._reservation_bytes(value)
    path = publish_reservation_path(output_root)
    workspace._write_exclusive_bytes(path, raw)
    return path, raw


def test_workspace_uses_full_hash_and_sequence_not_sku(tmp_path):
    result = workspace.publish_batch_workspace(
        tmp_path / "batches",
        BATCH_HASH,
        report(),
        copies(),
    )
    assert result.path.name == BATCH_HASH
    assert (result.path / workspace.ITEMS_DIRECTORY / "000001").is_dir()
    assert not (result.path / workspace.ITEMS_DIRECTORY / "SKU-ONE").exists()


def test_source_plan_is_copied_as_exact_independent_bytes(tmp_path):
    source = tmp_path / "source-plan.json"
    source.write_bytes(PLAN_BYTES)
    result = workspace.publish_batch_workspace(
        tmp_path / "batches",
        BATCH_HASH,
        report(),
        copies(),
    )
    copied = copied_plan_path(result.path)
    assert copied.read_bytes() == source.read_bytes()
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == PLAN_SHA
    assert not os.path.samefile(source, copied)
    assert copied.stat().st_nlink == 1


def test_copy_sha_mismatch_fails_and_publishes_nothing(tmp_path, monkeypatch):
    original = workspace._write_exclusive_bytes

    def corrupt_plan(path, data):
        if path.name == workspace.PLAN_FILENAME:
            return original(path, data + b"corrupt")
        return original(path, data)

    monkeypatch.setattr(workspace, "_write_exclusive_bytes", corrupt_plan)
    with pytest.raises(workspace.WooBatchWorkspaceError) as raised:
        workspace.publish_batch_workspace(
            tmp_path / "batches",
            BATCH_HASH,
            report(),
            copies(),
        )
    assert str(raised.value) == "woo_batch_plan_copy_mismatch"
    assert not (tmp_path / "batches" / BATCH_HASH).exists()


def test_existing_identical_workspace_is_reused_without_overwrite(tmp_path):
    output = tmp_path / "batches"
    first = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
    )
    original_report_bytes = (first.path / workspace.BATCH_REPORT_FILENAME).read_bytes()
    original_plan_bytes = copied_plan_path(first.path).read_bytes()
    second = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
    )
    assert second.reused is True
    assert second.path == first.path
    assert (first.path / workspace.BATCH_REPORT_FILENAME).read_bytes() == (
        original_report_bytes
    )
    assert copied_plan_path(first.path).read_bytes() == original_plan_bytes


def test_existing_same_semantics_can_reuse_original_manifest_audit(tmp_path):
    output = tmp_path / "batches"
    first = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(manifest_sha="b" * 64),
        copies(),
    )
    second = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(manifest_sha="d" * 64),
        copies(),
    )
    assert second.reused is True
    assert second.persisted_report["source_manifest"] == first.persisted_report[
        "source_manifest"
    ]


@pytest.mark.parametrize("artifact", ["report", "plan", "extra"])
def test_existing_workspace_mismatch_fails_closed(tmp_path, artifact):
    output = tmp_path / "batches"
    first = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
    )
    if artifact == "report":
        report_path = first.path / workspace.BATCH_REPORT_FILENAME
        value = json.loads(report_path.read_text(encoding="utf-8"))
        value["write_authorized"] = True
        report_path.write_text(json.dumps(value), encoding="utf-8")
    elif artifact == "plan":
        copied_plan_path(first.path).write_bytes(b"changed")
    else:
        (first.path / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(workspace.WooBatchWorkspaceError) as raised:
        workspace.publish_batch_workspace(
            output,
            BATCH_HASH,
            report(),
            copies(),
        )
    assert str(raised.value) == "woo_batch_workspace_mismatch"


@pytest.mark.parametrize(
    "unsafe_root",
    [
        Path("https://example.test/batches"),
        Path("file:///tmp/batches"),
        Path(r"\\server\share\batches"),
    ],
)
def test_unsafe_output_root_is_rejected(unsafe_root):
    with pytest.raises(workspace.WooBatchWorkspaceError):
        workspace.publish_batch_workspace(
            unsafe_root,
            BATCH_HASH,
            report(),
            copies(),
        )


def test_linked_output_root_is_rejected(tmp_path, monkeypatch):
    linked = tmp_path / "linked"
    linked.mkdir()
    original = workspace.package_io._has_link_or_reparse

    def linked_path(path):
        return Path(path) == linked.resolve() or original(path)

    monkeypatch.setattr(
        workspace.package_io,
        "_has_link_or_reparse",
        linked_path,
    )
    with pytest.raises(workspace.WooBatchWorkspaceError):
        workspace.publish_batch_workspace(
            linked,
            BATCH_HASH,
            report(),
            copies(),
        )


def test_existing_runtime_artifact_blocks_freeze_reuse(tmp_path):
    output = tmp_path / "batches"
    first = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
    )
    runtime = (
        first.path
        / workspace.ITEMS_DIRECTORY
        / "000001"
        / workspace.REPORTS_DIRECTORY
        / "runtime-sentinel.json"
    )
    runtime.write_bytes(b'{"sentinel":"pending"}\n')
    with pytest.raises(workspace.WooBatchWorkspaceError) as raised:
        workspace.publish_batch_workspace(
            output,
            BATCH_HASH,
            report(),
            copies(),
        )
    assert str(raised.value) == "woo_batch_workspace_mismatch"
    assert runtime.read_bytes() == b'{"sentinel":"pending"}\n'


def test_no_temporary_or_publish_reservation_artifacts_remain_after_success(tmp_path):
    output = tmp_path / "batches"
    workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
    )
    assert {entry.name for entry in output.iterdir()} == {BATCH_HASH}


def test_crash_left_reservation_has_valid_recoverable_provenance(tmp_path):
    output = tmp_path / "batches"
    reservation, raw = seed_publish_reservation(output)
    value, persisted_raw = workspace._read_reservation(
        reservation,
        BATCH_HASH,
    )
    assert reservation.exists()
    assert persisted_raw == raw
    assert value == {
        "policy_version": workspace.PUBLISH_RESERVATION_POLICY_VERSION,
        "batch_hash": BATCH_HASH,
        "owner_pid": 424_242,
        "owner_token": "d" * 32,
    }


def test_next_freeze_recovers_incomplete_stale_reservation(tmp_path):
    output = tmp_path / "batches"
    reservation, _ = seed_publish_reservation(output)
    result = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
        process_liveness_checker=lambda owner_pid: False,
    )
    assert result.reused is False
    assert result.path == output / BATCH_HASH
    assert not reservation.exists()


def test_valid_existing_workspace_reuses_and_cleans_stale_reservation(tmp_path):
    output = tmp_path / "batches"
    first = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
    )
    reservation, _ = seed_publish_reservation(output)
    second = workspace.publish_batch_workspace(
        output,
        BATCH_HASH,
        report(),
        copies(),
        process_liveness_checker=lambda owner_pid: False,
    )
    assert second.reused is True
    assert second.path == first.path
    assert not reservation.exists()


def test_active_publish_reservation_blocks_without_removal(tmp_path):
    output = tmp_path / "batches"
    reservation, original = seed_publish_reservation(output)
    with pytest.raises(workspace.WooBatchWorkspaceError) as raised:
        workspace.publish_batch_workspace(
            output,
            BATCH_HASH,
            report(),
            copies(),
            process_liveness_checker=lambda owner_pid: True,
        )
    assert str(raised.value) == "woo_batch_publish_in_progress"
    assert reservation.read_bytes() == original
    assert not (output / BATCH_HASH).exists()


@pytest.mark.parametrize(
    "tampered",
    [
        b"not-json\n",
        b'{"batch_hash":"' + (b"e" * 64) + b'"}\n',
        workspace._reservation_bytes(
            {
                "policy_version": workspace.PUBLISH_RESERVATION_POLICY_VERSION,
                "batch_hash": BATCH_HASH,
                "owner_pid": 424_242,
                "owner_token": "not-a-valid-owner-token",
            }
        ),
    ],
)
def test_tampered_publish_reservation_fails_closed_without_removal(
    tmp_path,
    tampered,
):
    output = tmp_path / "batches"
    output.mkdir()
    reservation = publish_reservation_path(output)
    workspace._write_exclusive_bytes(reservation, tampered)
    with pytest.raises(workspace.WooBatchWorkspaceError) as raised:
        workspace.publish_batch_workspace(
            output,
            BATCH_HASH,
            report(),
            copies(),
            process_liveness_checker=lambda owner_pid: False,
        )
    assert str(raised.value) == "woo_batch_publish_reservation_invalid"
    assert reservation.read_bytes() == tampered
    assert not (output / BATCH_HASH).exists()
