from __future__ import annotations

import ast
import builtins
import copy
import inspect
import io
import json
import socket
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import woocommerce_batch_authorization as authorization  # noqa: E402
from sync_worker import woocommerce_batch_runtime as runtime_core  # noqa: E402
from tests.test_woocommerce_batch_runtime import Fixture  # noqa: E402


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("external")
        raise AssertionError("network/credentials/real data forbidden")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden)
    for module in (builtins, io):
        original = module.open

        def guarded(file, *args, _original=original, **kwargs):
            if isinstance(file, (str, Path)):
                local = Path(file).absolute()
                if local.name.startswith(".env") or PROJECT_ROOT / "reports" in local.parents:
                    return forbidden()
            return _original(file, *args, **kwargs)

        monkeypatch.setattr(module, "open", guarded)
    yield
    assert attempted == []


def approval_input(observed):
    # Synthetic operator approvals for mock fixtures only.
    return {
        "policy_version": authorization.POLICY_VERSION,
        "batch_hash": observed.batch_hash,
        "items": [{
            "sequence": item.sequence, "sku": item.sku,
            "confirmed_plan_hash": item.plan_hash,
            "allowed_operations": ["apply", "verify_receipt"],
        } for item in observed.items],
    }


@pytest.fixture
def inputs(tmp_path):
    frozen = Fixture(tmp_path)
    observed = runtime_core.load_woo_batch_runtime(frozen.root)
    value = approval_input(observed)
    path = tmp_path / "woo-batch-approvals.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return observed, value, path


def test_policy_version_and_exact_complete_input(inputs):
    observed, value, path = inputs
    assert authorization.POLICY_VERSION == "xxxxdoll-woo-batch-manual-authorization-v1"
    provider = authorization.load_woo_batch_authorization(path, observed)
    assert provider.batch_hash == value["batch_hash"]
    assert len(provider.items) == len(observed.items)
    for item in observed.items:
        assert provider(observed.batch_hash, item, "apply") is provider.items[item.sequence - 1]


def test_manual_values_originate_from_input_without_normalization(inputs):
    observed, value, _ = inputs
    provider = authorization.parse_woo_batch_authorization(value, observed)
    for supplied, approved in zip(value["items"], provider.items, strict=True):
        assert approved.batch_hash is value["batch_hash"]
        assert approved.sku is supplied["sku"]
        assert approved.confirmed_plan_hash is supplied["confirmed_plan_hash"]
        assert approved.sequence is supplied["sequence"]
        assert approved.allowed_operations == frozenset(supplied["allowed_operations"])
        assert approved.confirmed_pending_sha256 is None


def test_input_order_is_not_execution_sequence_authority(inputs):
    observed, value, _ = inputs
    value["items"].reverse()
    provider = authorization.parse_woo_batch_authorization(value, observed)
    assert provider(observed.batch_hash, observed.items[0], "apply").sequence == 1


@pytest.mark.parametrize("change", ["partial", "empty", "extra", "batch", "sequence", "sku", "plan"])
def test_valid_schema_binding_or_coverage_mismatch_is_blocked(inputs, change):
    observed, value, _ = inputs
    if change == "partial":
        value["items"].pop()
    elif change == "empty":
        value["items"] = []
    elif change == "extra":
        extra = copy.deepcopy(value["items"][-1])
        extra["sequence"] = 3
        value["items"].append(extra)
    elif change == "batch":
        value["batch_hash"] = "a" * 64
    elif change == "sequence":
        value["items"][0]["sequence"] = 42
    elif change == "sku":
        value["items"][0]["sku"] = "OTHER-SKU"
    else:
        value["items"][0]["confirmed_plan_hash"] = "b" * 64
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 1
    assert str(caught.value) == authorization.AUTHORIZATION_BINDING_BLOCKED


@pytest.mark.parametrize("value", [None, [], "input", {}, {"policy_version": authorization.POLICY_VERSION}])
def test_malformed_root_is_local_error(inputs, value):
    observed, _, _ = inputs
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 2
    assert str(caught.value) == authorization.AUTHORIZATION_INPUT_INVALID


@pytest.mark.parametrize("field", ["policy_version", "batch_hash", "items"])
def test_missing_root_field_is_never_inferred(inputs, field):
    observed, value, _ = inputs
    del value[field]
    with pytest.raises(authorization.WooBatchAuthorizationError):
        authorization.parse_woo_batch_authorization(value, observed)


@pytest.mark.parametrize("field", ["sequence", "sku", "confirmed_plan_hash", "allowed_operations"])
def test_missing_item_confirmation_is_never_inferred(inputs, field):
    observed, value, _ = inputs
    del value["items"][0][field]
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("field,bad", [
    ("sequence", True), ("sequence", False), ("sequence", 0), ("sequence", -1),
    ("sequence", "1"), ("sequence", 1.0),
    ("sku", None), ("sku", 3), ("sku", ""), ("sku", " SKU"), ("sku", "SKU\n"),
    ("sku", "https://user:pass@example.test"), ("sku", "C:\\private\\file"),
    ("sku", "ck_" + "x" * 30),
    ("confirmed_plan_hash", None), ("confirmed_plan_hash", True),
    ("confirmed_plan_hash", "a" * 63), ("confirmed_plan_hash", "A" * 64),
    ("confirmed_plan_hash", "g" * 64),
    ("allowed_operations", []), ("allowed_operations", None),
    ("allowed_operations", "apply"), ("allowed_operations", [True]),
    ("allowed_operations", ["apply", "apply"]),
    ("allowed_operations", ["inspect_pending"]), ("allowed_operations", ["reconcile_pending"]),
    ("allowed_operations", ["retry"]), ("allowed_operations", ["cleanup"]),
    ("allowed_operations", ["recovery"]), ("allowed_operations", ["unknown"]),
])
def test_malformed_item_fields_are_local_error(inputs, field, bad):
    observed, value, _ = inputs
    value["items"][0][field] = bad
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 2
    assert str(caught.value) == authorization.AUTHORIZATION_INPUT_INVALID


@pytest.mark.parametrize("bad", [None, True, "a" * 63, "A" * 64, "not-a-hash"])
def test_malformed_batch_hash_is_local_error(inputs, bad):
    observed, value, _ = inputs
    value["batch_hash"] = bad
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("location", ["root", "item"])
def test_unknown_fields_are_rejected(inputs, location):
    observed, value, _ = inputs
    target = value if location == "root" else value["items"][0]
    target["credential"] = "synthetic-secret"
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 2
    assert "synthetic-secret" not in str(caught.value)


def test_duplicate_sequence_is_local_error(inputs):
    observed, value, _ = inputs
    value["items"].append(copy.deepcopy(value["items"][0]))
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.parse_woo_batch_authorization(value, observed)
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("raw", [
    b"not-json", b"[]", b"null", b"\xff", b"", b'{"a":NaN}', b'{"a":Infinity}',
    b'{"policy_version":"one","policy_version":"two"}',
    b'{"items":[{"sequence":1,"sequence":2}]}',
])
def test_strict_json_and_fixed_safe_errors(inputs, raw):
    observed, _, path = inputs
    path.write_bytes(raw)
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(path, observed)
    assert caught.value.exit_code == 2
    assert str(caught.value) == authorization.AUTHORIZATION_INPUT_INVALID
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("path", [
    "https://example.test/approvals.json", r"\\server\share\approvals.json",
    ".env.json", "credential-report.json", "authorization.json", "secret-token.json",
    "service-account.json", "approvals.txt", "missing-approvals.json",
])
def test_unsafe_or_missing_file_is_rejected_without_reading(inputs, path):
    observed, _, _ = inputs
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(Path(path), observed)
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("linked", ["symlink", "reparse", "hardlink"])
def test_linked_input_fails_closed(inputs, monkeypatch, linked):
    observed, _, path = inputs
    if linked == "hardlink":
        monkeypatch.setattr(authorization.workspace, "_regular_unlinked_file", lambda _: False)
    else:
        monkeypatch.setattr(authorization.package_io, "_has_link_or_reparse", lambda _: True)
    with pytest.raises(authorization.WooBatchAuthorizationError):
        authorization.load_woo_batch_authorization(path, observed)


def test_authorization_cannot_be_read_inside_frozen_workspace(inputs, monkeypatch):
    observed, value, _ = inputs
    path = observed.workspace_root / "woo-batch-approvals.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("must reject before reading"))
    with pytest.raises(authorization.WooBatchAuthorizationError):
        authorization.load_woo_batch_authorization(path, observed)


def test_read_and_parse_once_provider_never_reopens_input(inputs, monkeypatch):
    observed, _, path = inputs
    reads, parses = [], []
    read = Path.read_bytes
    decode = authorization.json.loads

    def counted_read(local):
        reads.append(local)
        return read(local)

    def counted_parse(*args, **kwargs):
        parses.append(1)
        return decode(*args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    monkeypatch.setattr(authorization.json, "loads", counted_parse)
    provider = authorization.load_woo_batch_authorization(path, observed)
    path.write_text("changed after invocation preflight", encoding="utf-8")
    for _ in range(3):
        assert provider(observed.batch_hash, observed.items[0], "apply") is provider.items[0]
    assert reads == [path.absolute()]
    assert parses == [1]


def test_input_and_provider_are_immutable_and_permissions_not_expanded(inputs):
    observed, value, _ = inputs
    value["items"][0]["allowed_operations"] = ["apply"]
    provider = authorization.parse_woo_batch_authorization(value, observed)
    original = provider.items[0]
    value["items"][0]["sku"] = "changed"
    value["items"][0]["allowed_operations"].append("verify_receipt")
    assert provider(observed.batch_hash, observed.items[0], "verify_receipt") is original
    assert original.allowed_operations == frozenset({"apply"})
    with pytest.raises(FrozenInstanceError):
        original.confirmed_plan_hash = "a" * 64
    with pytest.raises(FrozenInstanceError):
        provider.batch_hash = "a" * 64
    with pytest.raises(TypeError):
        provider._by_sequence[1] = original


@pytest.mark.parametrize("change", ["batch", "sku", "hash", "sequence", "operation"])
def test_provider_never_rebinds_approvals_to_changed_identity(inputs, change):
    observed, value, _ = inputs
    provider = authorization.parse_woo_batch_authorization(value, observed)
    item, batch, operation = observed.items[0], observed.batch_hash, "apply"
    if change == "batch":
        batch = "a" * 64
    elif change == "sku":
        item = replace(item, sku="OTHER")
    elif change == "hash":
        item = replace(item, plan_hash="b" * 64)
    elif change == "sequence":
        item = replace(item, sequence=9)
    else:
        operation = "reconcile_pending"
    assert provider(batch, item, operation) is None


def test_no_eight_item_limit(tmp_path):
    digest = "c" * 64
    observed = runtime_core.BatchRuntime(digest, tmp_path / digest, tuple(
        runtime_core.BatchItemRuntime(i, f"SKU-{i}", f"{i:064x}", tmp_path / f"{i}.json", "NOT_STARTED", ())
        for i in range(1, 10)
    ))
    provider = authorization.parse_woo_batch_authorization(approval_input(observed), observed)
    assert len(provider.items) == 9


def test_module_has_no_execution_mutation_or_transport_capability():
    source = inspect.getsource(authorization)
    tree = ast.parse(source)
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"mkdir", "unlink", "remove", "write_bytes", "write_text", "create_product"}
    assert not any(token in source for token in ("load_config(", "requests.", "httpx.", "urllib", "run_woo_batch("))
