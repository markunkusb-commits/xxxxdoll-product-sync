from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli  # noqa: E402
from sync_worker import woocommerce_apply_plan as apply_plan  # noqa: E402
from sync_worker import woocommerce_batch_plan as batch  # noqa: E402
from sync_worker import woocommerce_batch_workspace as workspace  # noqa: E402
from sync_worker import woocommerce_product_apply as apply_core  # noqa: E402
from sync_worker import woocommerce_target_snapshot as target  # noqa: E402
from sync_worker.single_product_staging_package import (  # noqa: E402
    POLICY_VERSION as PACKAGE_POLICY_VERSION,
)


def payload(sku: str, name: str) -> dict[str, object]:
    return {
        "name": name,
        "sku": sku,
        "type": "simple",
        "status": "draft",
        "regular_price": "1399.00",
        "description": "Safe description",
        "short_description": "Safe summary",
        "categories": [{"id": 1431}],
        "attributes": [
            {
                "name": "Height",
                "position": 0,
                "visible": True,
                "variation": False,
                "options": ["160 cm"],
            }
        ],
        "images": [{"id": 100}, {"id": 101}],
    }


def valid_plan(
    sku: str,
    name: str,
    *,
    source_seed: str = "b",
) -> dict[str, object]:
    package = {
        "status": "ok",
        "policy_version": PACKAGE_POLICY_VERSION,
        "target_sku": sku,
        "future_woo_payload": payload(sku, name),
        "source_validation": {
            "sku_verified": True,
            "payload_verified": True,
            "category_verified": True,
            "selection_verified": True,
            "media_verified": True,
        },
        "blocking_issues": [],
        "write_authorized": False,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }
    package_source = {
        "basename": "single-product-staging-package.json",
        "sha256": "a" * 64,
    }
    snapshot = {
        "status": "ok",
        "policy_version": target.POLICY_VERSION,
        "target": {**apply_core._TARGET, "read_only": True},
        "sku": sku,
        "match_count": 0,
        "create_eligible": True,
        "existing_target": None,
        "source_package": package_source,
        "blocking_issues": [],
        "write_authorized": False,
        "network_requests_performed": 1,
        "woocommerce_requests_performed": 1,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }
    return apply_plan.build_woo_apply_plan(
        package,
        snapshot,
        source_package=package_source,
        source_target_snapshot={
            "basename": "woo-target-snapshot.json",
            "sha256": source_seed * 64,
        },
    )


def write_json(path: Path, value: object, *, indent: int = 2) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def write_manifest(path: Path, plan_paths: list[Path]) -> Path:
    write_json(
        path,
        {
            "policy_version": batch.INPUT_POLICY_VERSION,
            "items": [
                {
                    "sequence": index,
                    "plan_path": str(plan_path),
                }
                for index, plan_path in enumerate(plan_paths, start=1)
            ],
        },
    )
    return path


def freeze(
    root: Path,
    plans: list[tuple[str, str]],
    *,
    manifest_name: str = "batch-manifest.json",
):
    plan_paths: list[Path] = []
    for index, (sku, name) in enumerate(plans, start=1):
        plan_path = root / "input" / f"plan-{index}.json"
        write_json(plan_path, valid_plan(sku, name))
        plan_paths.append(plan_path)
    manifest = write_manifest(root / manifest_name, plan_paths)
    return batch.freeze_woo_batch_plan(
        manifest,
        output_root=root / "output",
    )


def test_valid_one_item_batch(tmp_path):
    report, path, reused = freeze(tmp_path, [("SKU-ONE", "One")])
    assert report["status"] == "ok"
    assert isinstance(report["batch_hash"], str)
    assert len(report["batch_hash"]) == 64
    assert report["items"][0]["sequence"] == 1
    assert report["items"][0]["sku"] == "SKU-ONE"
    assert path == tmp_path / "output" / report["batch_hash"]
    assert reused is False


def test_valid_multi_item_batch_has_sequential_policy_and_zero_counters(tmp_path):
    report, path, _ = freeze(
        tmp_path,
        [("SKU-ONE", "One"), ("SKU-TWO", "Two")],
    )
    assert path is not None
    assert [item["sequence"] for item in report["items"]] == [1, 2]
    assert report["execution_policy"] == batch.EXECUTION_POLICY
    assert report["write_authorized"] is False
    for counter in batch._ZERO_COUNTERS:
        assert type(report[counter]) is int
        assert report[counter] == 0


def test_batch_hash_is_deterministic_and_manifest_path_is_not_semantic(tmp_path):
    plan = valid_plan("SKU-ONE", "One")
    first_plan = tmp_path / "first" / "source.json"
    second_plan = tmp_path / "second" / "renamed.json"
    raw = write_json(first_plan, plan)
    second_plan.parent.mkdir()
    second_plan.write_bytes(raw)
    first_manifest = write_manifest(tmp_path / "first-manifest.json", [first_plan])
    second_manifest = write_manifest(tmp_path / "second-manifest.json", [second_plan])
    first, _, _ = batch.freeze_woo_batch_plan(
        first_manifest,
        output_root=tmp_path / "out-one",
    )
    second, _, _ = batch.freeze_woo_batch_plan(
        second_manifest,
        output_root=tmp_path / "out-two",
    )
    assert first["batch_hash"] == second["batch_hash"]
    assert batch.semantic_batch_body(first) == batch.semantic_batch_body(second)
    assert "plan_path" not in json.dumps(batch.semantic_batch_body(first))


def test_item_order_changes_batch_hash(tmp_path):
    plan_one = tmp_path / "one.json"
    plan_two = tmp_path / "two.json"
    write_json(plan_one, valid_plan("SKU-ONE", "One"))
    write_json(plan_two, valid_plan("SKU-TWO", "Two"))
    first_manifest = write_manifest(tmp_path / "first.json", [plan_one, plan_two])
    second_manifest = write_manifest(tmp_path / "second.json", [plan_two, plan_one])
    first, _, _ = batch.freeze_woo_batch_plan(
        first_manifest,
        output_root=tmp_path / "first-output",
    )
    second, _, _ = batch.freeze_woo_batch_plan(
        second_manifest,
        output_root=tmp_path / "second-output",
    )
    assert first["batch_hash"] != second["batch_hash"]


def test_duplicate_sequence_blocks_without_workspace(tmp_path):
    plan = tmp_path / "plan.json"
    write_json(plan, valid_plan("SKU-ONE", "One"))
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "policy_version": batch.INPUT_POLICY_VERSION,
            "items": [
                {"sequence": 1, "plan_path": str(plan)},
                {"sequence": 1, "plan_path": str(plan)},
            ],
        },
    )
    report, path, _ = batch.freeze_woo_batch_plan(
        manifest,
        output_root=tmp_path / "output",
    )
    assert report["status"] == "blocked"
    assert report["batch_hash"] is None
    assert "woo_batch_duplicate_sequence" in report["blocking_issues"]
    assert path is None


def test_duplicate_sku_blocks_entire_batch(tmp_path):
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    write_json(first, valid_plan("SKU-SAME", "Same", source_seed="b"))
    write_json(second, valid_plan("SKU-SAME", "Same", source_seed="c"))
    manifest = write_manifest(tmp_path / "manifest.json", [first, second])
    report, path, _ = batch.freeze_woo_batch_plan(
        manifest,
        output_root=tmp_path / "output",
    )
    assert "woo_batch_duplicate_sku" in report["blocking_issues"]
    assert report["batch_hash"] is None
    assert path is None


def test_duplicate_plan_hash_blocks_entire_batch(tmp_path):
    value = valid_plan("SKU-SAME", "Same")
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    write_json(first, value, indent=2)
    write_json(second, value, indent=4)
    manifest = write_manifest(tmp_path / "manifest.json", [first, second])
    report, _, _ = batch.freeze_woo_batch_plan(
        manifest,
        output_root=tmp_path / "output",
    )
    assert "woo_batch_duplicate_plan_hash" in report["blocking_issues"]
    assert "woo_batch_duplicate_raw_plan_sha" not in report["blocking_issues"]


def test_duplicate_raw_plan_sha_blocks_entire_batch(tmp_path):
    value = valid_plan("SKU-SAME", "Same")
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    raw = write_json(first, value)
    second.write_bytes(raw)
    manifest = write_manifest(tmp_path / "manifest.json", [first, second])
    report, _, _ = batch.freeze_woo_batch_plan(
        manifest,
        output_root=tmp_path / "output",
    )
    assert "woo_batch_duplicate_raw_plan_sha" in report["blocking_issues"]


def test_invalid_or_blocked_item_produces_non_actionable_batch(tmp_path):
    plan = valid_plan("SKU-ONE", "One")
    plan.update(status="blocked", blocking_issues=["blocked"], plan_hash=None)
    plan_path = tmp_path / "plan.json"
    write_json(plan_path, plan)
    manifest = write_manifest(tmp_path / "manifest.json", [plan_path])
    report, path, reused = batch.freeze_woo_batch_plan(
        manifest,
        output_root=tmp_path / "output",
    )
    assert report["status"] == "blocked"
    assert report["batch_hash"] is None
    assert report["write_authorized"] is False
    assert report["items"] == []
    assert path is None
    assert reused is False
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "https://example.test/plan.json",
        "file:///tmp/plan.json",
        r"\\server\share\plan.json",
        "plan.txt",
    ],
)
def test_unsafe_plan_path_is_rejected(tmp_path, unsafe_path):
    manifest = write_manifest(tmp_path / "manifest.json", [Path(unsafe_path)])
    with pytest.raises(batch.WooBatchPlanInputError) as raised:
        batch.freeze_woo_batch_plan(
            manifest,
            output_root=tmp_path / "output",
        )
    assert str(raised.value) == "woo_batch_local_plan_required"


def test_symlink_plan_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    write_json(source, valid_plan("SKU-ONE", "One"))
    original = batch.package_io._has_link_or_reparse

    def linked(path):
        return Path(path) == source.resolve() or original(path)

    monkeypatch.setattr(batch.package_io, "_has_link_or_reparse", linked)
    manifest = write_manifest(tmp_path / "manifest.json", [source])
    with pytest.raises(batch.WooBatchPlanInputError):
        batch.freeze_woo_batch_plan(
            manifest,
            output_root=tmp_path / "output",
        )


def test_manifest_duplicate_keys_are_rejected(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"policy_version":"%s","items":[],"items":[]}'
        % batch.INPUT_POLICY_VERSION,
        encoding="utf-8",
    )
    with pytest.raises(batch.WooBatchPlanInputError) as raised:
        batch.freeze_woo_batch_plan(
            manifest,
            output_root=tmp_path / "output",
        )
    assert str(raised.value) == "woo_batch_manifest_duplicate_json_key"


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("item", "product_id", 123),
        ("item", "sku", "OVERRIDE"),
        ("root", "target", {"environment": "production"}),
    ],
)
def test_manifest_does_not_accept_authority_overrides(
    tmp_path,
    location,
    field,
    value,
):
    plan = tmp_path / "plan.json"
    write_json(plan, valid_plan("SKU-ONE", "One"))
    manifest_value = {
        "policy_version": batch.INPUT_POLICY_VERSION,
        "items": [{"sequence": 1, "plan_path": str(plan)}],
    }
    if location == "root":
        manifest_value[field] = value
    else:
        manifest_value["items"][0][field] = value
    manifest = tmp_path / "manifest.json"
    write_json(manifest, manifest_value)
    with pytest.raises(batch.WooBatchPlanInputError):
        batch.freeze_woo_batch_plan(
            manifest,
            output_root=tmp_path / "output",
        )


def test_batch_plan_contains_no_payload_or_product_id(tmp_path):
    report, _, _ = freeze(tmp_path, [("SKU-ONE", "One")])
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "payload" not in encoded
    assert "product_id" not in encoded
    assert "regular_price" not in encoded


def test_freeze_never_loads_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        apply_core,
        "_default_credential_loader",
        lambda: (_ for _ in ()).throw(AssertionError("credentials forbidden")),
    )
    report, path, _ = freeze(tmp_path, [("SKU-ONE", "One")])
    assert report["status"] == "ok"
    assert path is not None


def test_source_plan_fingerprint_matches_exact_copied_bytes(tmp_path):
    report, path, _ = freeze(tmp_path, [("SKU-ONE", "One")])
    copied = (
        path
        / workspace.ITEMS_DIRECTORY
        / "000001"
        / workspace.AUTHORITIES_DIRECTORY
        / workspace.PLAN_FILENAME
    ).read_bytes()
    assert hashlib.sha256(copied).hexdigest() == report["items"][0][
        "source_plan"
    ]["sha256"]


def test_cli_registers_local_freeze_only_surface():
    parser = cli.build_parser()
    parsed = parser.parse_args(
        ["freeze-woo-batch-plan", "--manifest", "batch-manifest.json"]
    )
    assert parsed.command == "freeze-woo-batch-plan"
    assert parsed.output_root is None
    help_text = parser.format_help()
    assert "freeze-woo-batch-plan" in help_text
    command_source = inspect.getsource(cli._run_freeze_woo_batch_plan)
    for forbidden in ("credentials", "base_url", "POST", "execute"):
        assert forbidden not in command_source


def test_batch_modules_have_no_network_or_woo_write_capability():
    source = inspect.getsource(batch)
    for forbidden in (
        "credential_loader",
        "load_woo_category_credentials",
        "StdlibWooProductTargetTransport",
        "StdlibWooProductCreateTransport",
        "create_product",
        "http.client",
        '"POST"',
        '"PUT"',
        '"PATCH"',
        '"DELETE"',
    ):
        assert forbidden not in source
