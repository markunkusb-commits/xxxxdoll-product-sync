from __future__ import annotations

import ast
import inspect
import io
import json
import socket
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import woocommerce_batch_item_adapter as adapter  # noqa: E402
from sync_worker import woocommerce_batch_runtime as runtime  # noqa: E402
from sync_worker import woocommerce_product_apply as apply_core  # noqa: E402
from sync_worker import woocommerce_apply_idempotency as receipt_core  # noqa: E402
from sync_worker import woocommerce_pending_recovery as recovery_core  # noqa: E402
from sync_worker import woocommerce_pending_reconciliation as reconciliation_core  # noqa: E402
from tests.test_woocommerce_batch_runtime import Fixture  # noqa: E402


BASE_URL = apply_core.APPROVED_BASE_URL
PENDING_SHA = "e" * 64
OPERATIONS = frozenset({"apply", "verify_receipt", "inspect_pending", "reconcile_pending"})
CORE_FUNCTIONS = {
    "apply": (apply_core, "run_woo_product_apply"),
    "verify_receipt": (receipt_core, "run_woo_apply_receipt_verification"),
    "inspect_pending": (recovery_core, "inspect_woo_apply_pending"),
    "reconcile_pending": (reconciliation_core, "reconcile_woo_apply_pending"),
}
DEFAULT_OUTCOMES = {
    "apply": ("applied", 0, "woo_apply_applied"),
    "verify_receipt": ("already_applied", 0, "woo_apply_already_applied"),
    "inspect_pending": ("recovery_observation", 3, "woo_apply_pending_remote_exact"),
    "reconcile_pending": ("reconciled", 0, "woo_apply_pending_reconciled"),
}


def core_result(status, exit_code, code, *, network=0, writes=0):
    return {
        "status": status, "exit_code": exit_code, "result_code": code,
        "network_requests_performed": network,
        "woocommerce_requests_performed": network,
        "woocommerce_write_requests_performed": writes,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": writes,
        "write_requests_performed": writes,
    }


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("external_access")
        raise AssertionError("real credentials/network forbidden")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for module, name in CORE_FUNCTIONS.values():
        monkeypatch.setitem(getattr(module, name).__kwdefaults__, "credential_loader", forbidden)
    monkeypatch.setattr(apply_core, "_default_credential_loader", forbidden)

    original_open = io.open

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)):
            path = Path(file).absolute()
            if path.name.startswith(".env") or PROJECT_ROOT / "reports" in path.parents:
                attempts.append("real_local_data")
                raise AssertionError("real reports/env forbidden")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", guarded_open)
    yield attempts
    assert attempts == []


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.fixture = Fixture(tmp_path)
        self.calls = {}
        for operation, (module, name) in CORE_FUNCTIONS.items():
            mock = Mock(return_value=core_result(*DEFAULT_OUTCOMES[operation]))
            monkeypatch.setattr(module, name, mock)
            self.calls[operation] = mock

    def item(self, sequence=1):
        return runtime.load_woo_batch_runtime(self.fixture.root).items[sequence - 1]

    def auth(self, item=None, **overrides):
        item = item or self.item()
        return adapter.ManualAuthorizationContext(**{
            "batch_hash": self.fixture.report["batch_hash"],
            "sequence": item.sequence, "sku": item.sku,
            "confirmed_plan_hash": item.plan_hash,
            "confirmed_pending_sha256": PENDING_SHA,
            "allowed_operations": OPERATIONS,
            **overrides,
        })

    def prepare(self, operation):
        if operation == "verify_receipt":
            self.fixture.seed_runtime([apply_core.RECEIPT_FILENAME])
        elif operation in {"inspect_pending", "reconcile_pending"}:
            self.fixture.seed_runtime([apply_core.PENDING_FILENAME])
        return self.item()

    def run(self, operation="apply", *, item=None, auth=None, base_url=BASE_URL):
        item = item or self.item()
        return adapter.execute_batch_item(
            item, auth if auth is not None else self.auth(item),
            operation=operation, base_url=base_url,
        )

    def assert_no_calls(self):
        for mock in self.calls.values():
            mock.assert_not_called()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_binds_only_item_paths_and_passes_explicit_confirmation(harness, operation):
    item = harness.prepare(operation)
    confirmed = (" " + item.plan_hash)[1:]
    auth = harness.auth(item, confirmed_plan_hash=confirmed)
    result = harness.run(operation, item=item, auth=auth)
    kwargs = harness.calls[operation].call_args.kwargs
    assert kwargs == {
        "plan_report_path": harness.fixture.plan_path(),
        "confirmed_plan_hash": confirmed,
        "base_url": BASE_URL,
        "project_root": harness.fixture.item_root(),
        **({"receipt_report_path": harness.fixture.reports_root() / apply_core.RECEIPT_FILENAME}
           if operation == "verify_receipt" else {}),
        **({"confirmed_pending_sha256": PENDING_SHA} if operation == "reconcile_pending" else {}),
    }
    assert kwargs["confirmed_plan_hash"] is auth.confirmed_plan_hash
    assert kwargs["project_root"] != PROJECT_ROOT
    assert "pending_report_path" not in kwargs
    harness.calls[operation].assert_called_once()
    assert sum(mock.call_count for mock in harness.calls.values()) == 1
    assert result.provenance.core_function == CORE_FUNCTIONS[operation][1]
    assert result.provenance.batch_hash == auth.batch_hash
    assert result.provenance.adapter == adapter.ADAPTER_VERSION


def test_second_item_uses_own_root_not_first_item(harness):
    item = harness.item(2)
    result = harness.run(item=item)
    assert result.sequence == 2
    assert harness.calls["apply"].call_args.kwargs["project_root"] == harness.fixture.item_root(2)
    assert harness.calls["apply"].call_args.kwargs["plan_report_path"] == item.plan_path


@pytest.mark.parametrize("overrides", [
    {"batch_hash": "a" * 64}, {"sequence": 2}, {"sequence": True},
    {"sku": "OTHER"}, {"confirmed_plan_hash": "b" * 64},
    {"confirmed_plan_hash": None}, {"confirmed_plan_hash": ""},
    {"allowed_operations": frozenset()}, {"allowed_operations": {"verify_receipt"}},
])
def test_wrong_or_missing_authorization_never_dispatches(harness, overrides):
    result = harness.run(auth=harness.auth(**overrides))
    assert result.exit_code == 1
    assert result.result_code == "woo_batch_item_authorization_invalid"
    assert result.counters == adapter.ItemCounters()
    harness.assert_no_calls()


def test_another_item_authorization_cannot_be_reused(harness):
    result = harness.run(auth=harness.auth(harness.item(2)))
    assert result.exit_code == 1
    harness.assert_no_calls()


def test_missing_authorization_cannot_be_inferred_from_item(harness):
    result = adapter.execute_batch_item(harness.item(), None, operation="apply", base_url=BASE_URL)
    assert result.exit_code == 1
    harness.assert_no_calls()


def test_inspection_does_not_require_pending_sha_confirmation(harness):
    item = harness.prepare("inspect_pending")
    result = harness.run("inspect_pending", item=item,
                         auth=harness.auth(item, confirmed_pending_sha256=None))
    assert result.exit_code == 3
    harness.calls["inspect_pending"].assert_called_once()
    harness.calls["reconcile_pending"].assert_not_called()


@pytest.mark.parametrize("pending_sha", [None, "", "e" * 63, "E" * 64, "not-a-hash", True])
def test_reconciliation_requires_manual_pending_sha(harness, pending_sha):
    item = harness.prepare("reconcile_pending")
    result = harness.run("reconcile_pending", item=item,
                         auth=harness.auth(item, confirmed_pending_sha256=pending_sha))
    assert result.exit_code == 1
    assert result.counters == adapter.ItemCounters()
    harness.assert_no_calls()


def test_pending_sha_is_forwarded_not_generated_or_replaced(harness):
    item = harness.prepare("reconcile_pending")
    # No attempt to hash the fixture's opaque Pending to manufacture confirmation.
    confirmed = "f" * 64
    harness.run("reconcile_pending", item=item,
                auth=harness.auth(item, confirmed_pending_sha256=confirmed))
    assert harness.calls["reconcile_pending"].call_args.kwargs["confirmed_pending_sha256"] == confirmed


@pytest.mark.parametrize("state,allowed", [
    ("NOT_STARTED", {"apply"}),
    ("ALREADY_APPLIED_OBSERVED", {"verify_receipt"}),
    ("RECOVERY_REQUIRED", {"inspect_pending", "reconcile_pending"}),
])
@pytest.mark.parametrize("operation", [None, "apply", "verify_receipt", "inspect_pending", "reconcile_pending"])
def test_state_gate_never_selects_an_operation_implicitly(harness, state, allowed, operation):
    if state == "ALREADY_APPLIED_OBSERVED":
        harness.fixture.seed_runtime([apply_core.RECEIPT_FILENAME])
    elif state == "RECOVERY_REQUIRED":
        harness.fixture.seed_runtime([apply_core.PENDING_FILENAME])
    result = harness.run(operation)
    if operation in allowed:
        harness.calls[operation].assert_called_once()
        assert sum(mock.call_count for mock in harness.calls.values()) == 1
    else:
        harness.assert_no_calls()
        assert result.exit_code == (3 if state == "RECOVERY_REQUIRED" else 1)


def test_blocked_input_cannot_be_reinterpreted_as_not_started(harness):
    item = replace(harness.item(), state="BLOCKED")
    result = harness.run(item=item)
    assert result.state_before == result.state_after == "BLOCKED"
    assert result.exit_code == 1
    harness.assert_no_calls()


@pytest.mark.parametrize("name", [apply_core.PENDING_FILENAME, apply_core.LOCK_FILENAME])
def test_stale_observation_with_new_pending_or_lock_blocks_apply(harness, name):
    item = harness.item()
    harness.fixture.seed_runtime([name])
    result = harness.run(item=item)
    assert result.state_before == result.state_after == "RECOVERY_REQUIRED"
    assert result.exit_code == 3
    harness.assert_no_calls()


def test_receipt_appearing_does_not_switch_apply_to_verify(harness):
    item = harness.item()
    harness.fixture.seed_runtime([apply_core.RECEIPT_FILENAME])
    result = harness.run(item=item)
    assert result.exit_code == 2
    harness.assert_no_calls()


@pytest.mark.parametrize("field,value", [
    ("sku", "TAMPERED"), ("sequence", True), ("plan_hash", "0" * 64),
])
def test_dataclass_identity_is_rebound_to_existing_authority(harness, field, value):
    result = harness.run(item=replace(harness.item(), **{field: value}))
    assert result.exit_code == 2
    assert result.sku is None
    assert result.provenance.batch_hash is None
    harness.assert_no_calls()


def test_other_items_plan_path_cannot_be_substituted(harness):
    item = replace(harness.item(), plan_path=harness.item(2).plan_path)
    assert harness.run(item=item).exit_code == 2
    harness.assert_no_calls()


def test_frozen_plan_bytes_are_revalidated_without_modifying_them(harness):
    item = harness.item()
    raw = item.plan_path.read_bytes() + b"\n"
    item.plan_path.write_bytes(raw)
    result = harness.run(item=item)
    assert result.exit_code == 2
    assert item.plan_path.read_bytes() == raw
    harness.assert_no_calls()


@pytest.mark.parametrize("target", [
    "https://xxxxdoll.com", "https://other.wpcomstaging.com",
    "http://localhost", "https://user:password@example.com/?token=secret",
])
def test_target_is_validated_before_core_call_and_never_echoed(harness, target):
    result = harness.run(base_url=target)
    assert result.exit_code == 2
    assert result.result_code == "woo_batch_item_target_invalid"
    assert target not in repr(result)
    harness.assert_no_calls()


@pytest.mark.parametrize("operation,after,exit_code,disposition", [
    ("apply", "APPLIED", 0, "success"),
    ("verify_receipt", "ALREADY_APPLIED", 0, "success"),
    ("inspect_pending", "RECOVERY_REQUIRED", 3, "recovery_required"),
    ("reconcile_pending", "APPLIED", 0, "success"),
])
def test_result_projection(harness, operation, after, exit_code, disposition):
    item = harness.prepare(operation)
    result = harness.run(operation, item=item)
    assert result.state_before == item.state
    assert result.state_after == after
    assert result.exit_code == exit_code
    assert result.disposition == disposition
    assert result.sku == item.sku
    assert result.plan_hash == item.plan_hash
    assert result.operation == operation
    assert result.provenance.core_status == DEFAULT_OUTCOMES[operation][0]
    assert result.provenance.core_result_code == DEFAULT_OUTCOMES[operation][2]


@pytest.mark.parametrize("status,exit_code,code,disposition", [
    ("blocked_pre_write", 1, "woo_apply_target_sku_already_exists", "blocked"),
    ("pre_write_error", 2, "woo_apply_preflight_get_failed", "local_error"),
    ("recovery_required", 3, "woo_apply_readback_failed", "recovery_required"),
])
def test_core_exit_mapping_never_retries_or_recovers(harness, status, exit_code, code, disposition):
    harness.calls["apply"].return_value = core_result(status, exit_code, code, network=2)
    result = harness.run()
    assert result.exit_code == exit_code
    assert result.disposition == disposition
    assert result.result_code == code
    assert result.counters.network_requests_performed == 2
    assert sum(mock.call_count for mock in harness.calls.values()) == 1


@pytest.mark.parametrize("code", ["woo_apply_pending_remote_exact", "woo_apply_pending_remote_absent"])
def test_inspection_never_falls_through_to_reconcile_or_apply(harness, code):
    harness.prepare("inspect_pending")
    harness.calls["inspect_pending"].return_value = core_result("recovery_observation", 3, code)
    result = harness.run("inspect_pending")
    assert result.exit_code == 3
    assert result.state_after == "RECOVERY_REQUIRED"
    harness.calls["reconcile_pending"].assert_not_called()
    harness.calls["apply"].assert_not_called()


def test_failed_receipt_verification_never_falls_back_to_apply(harness):
    harness.prepare("verify_receipt")
    harness.calls["verify_receipt"].return_value = core_result(
        "blocked", 1, "woo_apply_receipt_not_found")
    assert harness.run("verify_receipt").exit_code == 1
    harness.calls["apply"].assert_not_called()


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_unknown_core_exception_is_safe_recovery_with_unknown_counters(harness, operation):
    harness.prepare(operation)
    harness.calls[operation].side_effect = RuntimeError(
        "Authorization Cookie token=fixture-secret https://user:password@example.com payload")
    result = harness.run(operation)
    assert result.exit_code == 3
    assert result.state_after == "RECOVERY_REQUIRED"
    assert result.result_code == "woo_batch_item_core_exception"
    assert result.counters == adapter.ItemCounters.unknown()
    assert result.provenance.core_status is None
    assert result.provenance.core_result_code is None
    assert "fixture-secret" not in repr(result)
    assert sum(mock.call_count for mock in harness.calls.values()) == 1


def test_interrupted_core_call_never_claims_zero_or_success(harness):
    harness.calls["apply"].side_effect = KeyboardInterrupt()
    result = harness.run()
    assert result.exit_code == 3
    assert result.counters.write_requests_performed is None
    assert sum(mock.call_count for mock in harness.calls.values()) == 1


def test_known_prewrite_exception_is_local_error_without_exception_text(harness):
    harness.calls["apply"].side_effect = apply_core.WooProductApplyPreWriteError("private-secret")
    result = harness.run()
    assert result.exit_code == 2
    assert result.result_code == "woo_batch_item_core_pre_write_error"
    assert "private-secret" not in repr(result)


@pytest.mark.parametrize("overrides", [
    {"status": "already_applied"}, {"status": "secret-token"},
    {"result_code": "woo_apply_secret_token"}, {"result_code": "https://example.com?token=secret"},
    {"exit_code": True}, {"exit_code": 99}, {"status": "recovery_required"},
    {"network_requests_performed": -1}, {"write_requests_performed": True},
    {"write_requests_performed": None},
])
def test_malformed_core_result_is_never_success(harness, overrides):
    harness.calls["apply"].return_value.update(overrides)
    result = harness.run()
    assert result.exit_code == 3
    assert result.result_code == "woo_batch_item_core_result_invalid"
    assert result.provenance.core_status is None
    assert result.provenance.core_result_code is None
    assert "secret" not in repr(result)


@pytest.mark.parametrize("value", [None, [], "secret response"])
def test_non_mapping_core_response_fails_closed(harness, value):
    harness.calls["apply"].return_value = value
    result = harness.run()
    assert result.exit_code == 3
    assert result.counters == adapter.ItemCounters.unknown()


def test_result_conversion_exception_is_also_safe_recovery(harness):
    class BrokenResponse(dict):
        def get(self, *args):
            raise RuntimeError("secret response")

    harness.calls["apply"].return_value = BrokenResponse()
    result = harness.run()
    assert result.exit_code == 3
    assert result.result_code == "woo_batch_item_core_exception"
    assert "secret response" not in repr(result)
    harness.calls["apply"].assert_called_once()


def test_inspect_cannot_claim_applied_even_with_exit_zero(harness):
    harness.prepare("inspect_pending")
    harness.calls["inspect_pending"].return_value = core_result("applied", 0, "woo_apply_applied")
    assert harness.run("inspect_pending").exit_code == 3


def test_only_current_safe_counters_and_provenance_are_projected(harness):
    raw = core_result("applied", 0, "woo_apply_applied", network=3, writes=1)
    raw.update({
        "receipt": {"write_requests_performed": 999, "secret": "hidden-credential"},
        "payload": {"description": "hidden-product-payload"},
        "credentials": "hidden-credential", "token": "hidden-token",
        "Authorization": "hidden-auth", "base_url": "https://user:pass@example.com/?token=x",
        "product": {"private_data": "hidden-product"},
    })
    harness.calls["apply"].return_value = raw
    result = harness.run()
    assert result.counters.network_requests_performed == 3
    assert result.counters.write_requests_performed == 1
    serialized = json.dumps(asdict(result))
    for forbidden in ("hidden-", "payload", "credentials", "token", "Authorization", "https://", "999"):
        assert forbidden not in serialized
    raw["write_requests_performed"] = 999
    assert result.counters.write_requests_performed == 1


def test_authorization_and_nested_results_are_immutable(harness):
    operations = {"apply"}
    auth = harness.auth(allowed_operations=operations)
    operations.add("reconcile_pending")
    assert auth.allowed_operations == frozenset({"apply"})
    result = harness.run(auth=auth)
    for obj, field, value in [
        (auth, "confirmed_plan_hash", "x"), (result, "exit_code", 0),
        (result.provenance, "core_status", "x"), (result.counters, "write_requests_performed", 99),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)


@pytest.mark.parametrize("operations", [None, "apply", {"unknown"}, [True]])
def test_invalid_allowed_operations_rejected_without_echo(harness, operations):
    with pytest.raises(ValueError, match="^woo_batch_item_authorization_invalid$"):
        harness.auth(allowed_operations=operations)
    harness.assert_no_calls()


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_adapter_does_not_create_modify_or_remove_runtime_files(harness, operation):
    harness.prepare(operation)
    root = harness.fixture.root
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    harness.run(operation)
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert before == after


def test_adapter_has_no_transport_credentials_journal_or_cli_capability():
    tree = ast.parse(inspect.getsource(adapter))
    forbidden_calls = {
        "create_product", "post", "put", "patch", "delete", "urlopen", "request",
        "write_text", "write_bytes", "unlink", "remove", "mkdir", "replace", "rename",
        "_default_credential_loader", "from_env", "load_dotenv", "_acquire_lock",
        "_write_pending", "_write_receipt", "subprocess", "compute_plan_hash",
    }
    calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
             for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, (ast.Attribute, ast.Name))}
    assert calls.isdisjoint(forbidden_calls)
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom))
                   and any(alias.name in {"cli", "requests", "socket", "urllib", "subprocess"}
                           for alias in node.names) for node in ast.walk(tree))
