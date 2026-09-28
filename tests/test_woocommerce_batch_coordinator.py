from __future__ import annotations

import ast
import builtins
import inspect
import io
import json
import socket
import sys
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import woocommerce_batch_coordinator as coordinator  # noqa: E402
from sync_worker import woocommerce_batch_item_adapter as adapter  # noqa: E402
from sync_worker import woocommerce_batch_runtime as runtime  # noqa: E402
from tests.test_woocommerce_batch_runtime import Fixture  # noqa: E402


BASE_URL = adapter.apply_core.APPROVED_BASE_URL
BATCH_HASH = "a" * 64


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("forbidden")
        raise AssertionError("real Core/credentials/network access forbidden")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(adapter.apply_core, "_default_credential_loader", forbidden)
    for module, name in (
        (adapter.apply_core, "run_woo_product_apply"),
        (adapter.receipt_core, "run_woo_apply_receipt_verification"),
        (adapter.recovery_core, "inspect_woo_apply_pending"),
        (adapter.reconciliation_core, "reconcile_woo_apply_pending"),
    ):
        monkeypatch.setattr(module, name, forbidden)

    for module in (io, builtins):
        original = module.open

        def guarded(file, *args, _original=original, **kwargs):
            if isinstance(file, (str, Path)):
                path = Path(file).absolute()
                if path.name.startswith(".env") or PROJECT_ROOT / "reports" in path.parents:
                    attempted.append("real_data")
                    raise AssertionError("real reports/env forbidden")
            return _original(file, *args, **kwargs)

        monkeypatch.setattr(module, "open", guarded)
    yield attempted
    assert attempted == []


def make_runtime(root):
    workspace = root / BATCH_HASH
    return runtime.BatchRuntime(BATCH_HASH, workspace, tuple(
        runtime.BatchItemRuntime(
            index, sku, f"{index:064x}",
            workspace / "items" / f"{index:06d}" / "authorities" / "woo-apply-plan.json",
            "NOT_STARTED", (),
        )
        for index, sku in enumerate(("ZETA", "ALPHA", "MIDDLE"), start=1)
    ))


def execution(item, operation="apply", *, exit_code=0, counters=None, batch_hash=BATCH_HASH):
    state = ("APPLIED" if operation == "apply" else "ALREADY_APPLIED") if exit_code == 0 else (
        "RECOVERY_REQUIRED" if exit_code == 3 else "BLOCKED"
    )
    code = "woo_apply_applied" if operation == "apply" else "woo_apply_already_applied"
    core_status = "applied" if operation == "apply" else "already_applied"
    if exit_code:
        code = {1: "woo_apply_target_sku_already_exists", 2: "woo_apply_preflight_get_failed",
                3: "woo_apply_readback_failed"}[exit_code]
        core_status = {1: "blocked_pre_write", 2: "pre_write_error", 3: "recovery_required"}[exit_code]
    if counters is None:
        network, writes = (3, 1) if operation == "apply" else (1, 0)
        counters = adapter.ItemCounters(network, network, writes, 0, writes, writes)
    return adapter.ItemExecutionResult(
        item.sequence, item.sku, item.plan_hash, item.state, state, operation,
        {0: "success", 1: "blocked", 2: "local_error", 3: "recovery_required"}[exit_code],
        exit_code, code,
        adapter.ItemProvenance(
            batch_hash, adapter.ADAPTER_VERSION,
            "run_woo_product_apply" if operation == "apply" else "run_woo_apply_receipt_verification",
            core_status, code,
        ), counters,
    )


class Harness:
    """Pure in-memory dependencies; confirmations are explicit mock approvals."""

    def __init__(self, root):
        self.initial = make_runtime(root)
        self.current = self.initial
        self.events = []
        self.load_count = 0
        self.on_load = None
        self.on_provider = None
        self.on_dispatch = None
        self.results = {}
        self.complete_runtime = True
        self.approvals = {
            (item.sequence, operation): adapter.ManualAuthorizationContext(
                batch_hash=BATCH_HASH, sequence=item.sequence, sku=item.sku,
                confirmed_plan_hash=item.plan_hash, allowed_operations=frozenset({operation}),
            )
            for item in self.initial.items for operation in ("apply", "verify_receipt")
        }
        self.loader = Mock(side_effect=self.load)
        self.provider = Mock(side_effect=self.provide)
        self.dispatch = Mock(side_effect=self.execute)

    def state(self, sequence, state):
        names = {"NOT_STARTED": (), "ALREADY_APPLIED_OBSERVED": ("woo-apply-receipt.json",),
                 "RECOVERY_REQUIRED": ("woo-apply-pending.json",), "BLOCKED": ()}
        items = list(self.current.items)
        items[sequence - 1] = replace(items[sequence - 1], state=state, runtime_files=names.get(state, ()))
        self.current = replace(self.current, items=tuple(items))

    def load(self, root):
        self.events.append(("load",))
        self.load_count += 1
        assert root == self.initial.workspace_root
        if self.on_load:
            self.on_load(self.load_count)
        return self.current

    def provide(self, batch_hash, item, operation):
        self.events.append(("authorize", item.sequence, operation))
        assert batch_hash == BATCH_HASH
        if self.on_provider:
            return self.on_provider(item, operation)
        return self.approvals.get((item.sequence, operation))

    def execute(self, item, authorization, *, operation, base_url):
        self.events.append(("dispatch", item.sequence, operation))
        assert base_url == BASE_URL
        assert authorization is self.approvals[(item.sequence, operation)]
        if self.on_dispatch:
            self.on_dispatch(item, operation)
        result = self.results.get(item.sequence, execution(item, operation))
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, adapter.ItemExecutionResult) and result.exit_code == 0 and self.complete_runtime:
            self.state(item.sequence, "ALREADY_APPLIED_OBSERVED")
        return result

    def run(self, **kwargs):
        return coordinator.run_woo_batch(
            kwargs.pop("runtime", self.initial), self.provider,
            base_url=BASE_URL, item_adapter=self.dispatch, runtime_loader=self.loader, **kwargs,
        )


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_successful_run_preserves_frozen_order_not_sku_order(harness):
    result = harness.run()
    assert result.exit_code == 0
    assert result.status == "completed"
    assert result.result_code == "woo_batch_completed"
    assert result.batch_hash == BATCH_HASH
    assert result.stopped_sequence is None
    assert result.total_items == result.dispatched_items == result.successful_items == 3
    assert [row.sku for row in result.items] == ["ZETA", "ALPHA", "MIDDLE"]
    assert [call.args[0].sequence for call in harness.dispatch.call_args_list] == [1, 2, 3]
    assert [call.args[0].sku for call in harness.dispatch.call_args_list] == ["ZETA", "ALPHA", "MIDDLE"]
    assert all(row.status == "APPLIED" and row.dispatched for row in result.items)
    assert result.counters == adapter.ItemCounters(9, 9, 3, 0, 3, 3)
    assert result.counters_complete


def test_runtime_reload_happens_before_every_dispatch_and_after_success(harness):
    harness.run()
    assert harness.events == [("load",)] + [event
        for sequence in (1, 2, 3)
        for event in (("load",), ("authorize", sequence, "apply"),
                      ("dispatch", sequence, "apply"), ("load",))]
    assert harness.loader.call_count == 7


def test_stale_initial_state_is_not_used_to_apply(harness):
    harness.state(1, "ALREADY_APPLIED_OBSERVED")
    fresh_item = harness.current.items[0]
    result = harness.run()
    first_call = harness.dispatch.call_args_list[0]
    assert first_call.args[0] is fresh_item
    assert first_call.kwargs["operation"] == "verify_receipt"
    assert result.items[0].status == "ALREADY_APPLIED"
    assert result.counters.write_requests_performed == 2


def test_state_refresh_before_later_item_changes_operation(harness):
    def change(count):
        if count == 4:  # before item 2, after successful item 1
            harness.state(2, "ALREADY_APPLIED_OBSERVED")

    harness.on_load = change
    result = harness.run()
    assert [row.operation for row in result.items] == ["apply", "verify_receipt", "apply"]
    assert result.exit_code == 0


@pytest.mark.parametrize("state,exit_code", [("RECOVERY_REQUIRED", 3), ("BLOCKED", 1)])
def test_stops_before_unresolved_item_without_authorization_or_dispatch(harness, state, exit_code):
    harness.state(2, state)
    result = harness.run()
    assert result.exit_code == exit_code
    assert result.stopped_sequence == 2
    assert result.successful_items == result.dispatched_items == 1
    assert result.items[0].status == "APPLIED"
    assert result.items[1].status == state
    assert result.items[1].execution_result is None
    assert result.items[2].status == "NOT_DISPATCHED"
    assert result.items[2].observed_state == "NOT_STARTED"
    assert result.items[2].operation is None
    assert result.items[2].execution_result is None
    assert not result.items[2].dispatched
    assert harness.provider.call_count == harness.dispatch.call_count == 1


def test_recovery_permissions_never_trigger_inspect_or_reconcile(harness):
    harness.state(1, "RECOVERY_REQUIRED")
    item = harness.initial.items[0]
    harness.approvals[(1, "apply")] = adapter.ManualAuthorizationContext(
        batch_hash=BATCH_HASH, sequence=1, sku=item.sku, confirmed_plan_hash=item.plan_hash,
        confirmed_pending_sha256="f" * 64,
        allowed_operations=frozenset({"apply", "inspect_pending", "reconcile_pending"}),
    )
    result = harness.run()
    assert result.exit_code == 3
    assert result.counters == adapter.ItemCounters()
    assert result.counters_complete
    harness.provider.assert_not_called()
    harness.dispatch.assert_not_called()


def test_missing_authorization_stops_without_generating_confirmation(harness):
    del harness.approvals[(2, "apply")]
    result = harness.run()
    assert result.exit_code == 1
    assert result.result_code == "woo_batch_authorization_missing"
    assert result.stopped_sequence == 2
    assert result.dispatched_items == 1
    assert result.items[1].operation == "apply"
    assert not result.items[1].dispatched
    assert harness.provider.call_count == 2


def test_provider_exception_is_local_error_without_sensitive_exception(harness):
    harness.provider.side_effect = RuntimeError("https://user:password@example.com?token=private")
    result = harness.run()
    assert result.exit_code == 2
    assert result.result_code == "woo_batch_authorization_provider_failed"
    assert result.counters == adapter.ItemCounters()
    assert result.counters_complete
    assert "private" not in repr(result)
    assert "https://" not in repr(result)
    harness.dispatch.assert_not_called()


def test_wrong_provider_return_type_is_local_contract_error(harness):
    harness.provider.side_effect = lambda *args: {"credential": "private"}
    result = harness.run()
    assert result.exit_code == 2
    assert result.result_code == "woo_batch_authorization_provider_invalid"
    assert "private" not in repr(result)
    harness.dispatch.assert_not_called()


def test_authorization_is_forwarded_unchanged_not_reconstructed(harness):
    result = harness.run()
    assert result.exit_code == 0
    for call in harness.dispatch.call_args_list:
        item, auth = call.args
        assert auth is harness.approvals[(item.sequence, call.kwargs["operation"])]
        assert set(call.kwargs) == {"operation", "base_url"}


@pytest.mark.parametrize("exit_code,status", [(1, "blocked"), (2, "local_error"), (3, "recovery_required")])
def test_adapter_failure_exit_mapping_preserves_prior_success_and_stops(harness, exit_code, status):
    failed = execution(harness.initial.items[1], exit_code=exit_code)
    harness.results[2] = failed
    result = harness.run()
    assert result.exit_code == exit_code
    assert result.status == status
    assert result.stopped_sequence == 2
    assert result.items[1].execution_result is failed
    assert result.items[1].result_code == failed.result_code
    assert result.items[0].status == "APPLIED"
    assert result.items[2].status == "NOT_DISPATCHED"
    assert result.dispatched_items == 2
    assert result.successful_items == 1
    assert harness.dispatch.call_count == 2
    assert harness.provider.call_count == 2
    assert harness.load_count == 4  # no recovery refresh/operation after non-success


@pytest.mark.parametrize("changes", [
    {"sequence": 2}, {"sequence": True}, {"sku": "UNRELATED"},
    {"plan_hash": "f" * 64}, {"operation": "reconcile_pending"},
    {"operation": "verify_receipt"}, {"exit_code": True}, {"exit_code": 9},
    {"disposition": "blocked"}, {"state_after": "ALREADY_APPLIED"},
    {"state_before": "ALREADY_APPLIED_OBSERVED"},
])
def test_result_identity_or_outcome_mismatch_fails_closed(harness, changes):
    harness.results[1] = replace(execution(harness.initial.items[0]), **changes)
    result = harness.run()
    assert result.exit_code == 3
    assert result.result_code == "woo_batch_adapter_result_invalid"
    assert result.dispatched_items == 1
    assert result.successful_items == 0
    assert result.items[0].execution_result is None
    assert result.counters == adapter.ItemCounters.unknown()
    assert not result.counters_complete
    assert "UNRELATED" not in repr(result)
    assert harness.dispatch.call_count == 1


@pytest.mark.parametrize("changes", [{"batch_hash": "f" * 64}, {"adapter": "other-adapter"}])
def test_result_provenance_binding_must_match(harness, changes):
    original = execution(harness.initial.items[0])
    harness.results[1] = replace(original, provenance=replace(original.provenance, **changes))
    assert harness.run().exit_code == 3
    assert harness.dispatch.call_count == 1


@pytest.mark.parametrize("value", [None, {}, "private response"])
def test_unknown_execution_result_cannot_be_success_or_leak(harness, value):
    harness.results[1] = value
    result = harness.run()
    assert result.exit_code == 3
    assert result.items[0].execution_result is None
    assert not result.counters_complete
    assert "private response" not in repr(result)


@pytest.mark.parametrize("error", [RuntimeError("secret-payload"), KeyboardInterrupt()])
def test_dispatch_exception_has_unknown_counters_and_no_retry(harness, error):
    harness.results[2] = error
    result = harness.run()
    assert result.exit_code == 3
    assert result.result_code == "woo_batch_adapter_exception"
    assert result.stopped_sequence == 2
    assert result.successful_items == 1
    assert result.dispatched_items == 2
    assert result.counters == adapter.ItemCounters.unknown()
    assert not result.counters_complete
    assert result.items[2].status == "NOT_DISPATCHED"
    assert "secret-payload" not in repr(result)
    assert harness.dispatch.call_count == 2


@pytest.mark.parametrize("field", [field.name for field in fields(adapter.ItemCounters)])
def test_unknown_counter_propagates_without_inventing_zero(harness, field):
    original = execution(harness.initial.items[1])
    harness.results[2] = replace(original, counters=replace(original.counters, **{field: None}))
    result = harness.run()
    assert result.exit_code == 0
    assert getattr(result.counters, field) is None
    assert not result.counters_complete
    expected = adapter.ItemCounters(9, 9, 3, 0, 3, 3)
    for other in fields(adapter.ItemCounters):
        if other.name != field:
            assert getattr(result.counters, other.name) == getattr(expected, other.name)


@pytest.mark.parametrize("value", [-1, True, "1"])
def test_invalid_counter_is_not_a_known_zero(harness, value):
    original = execution(harness.initial.items[0])
    harness.results[1] = replace(original, counters=replace(original.counters, write_requests_performed=value))
    result = harness.run()
    assert result.exit_code == 3
    assert result.counters.write_requests_performed is None


def test_resume_always_starts_at_one_and_only_counts_current_gets(harness):
    first = harness.run()
    assert first.counters.write_requests_performed == 3
    harness.events.clear()
    harness.dispatch.reset_mock()
    harness.provider.reset_mock()
    second = harness.run()  # deliberately pass the original stale NOT_STARTED Runtime again
    assert second.exit_code == 0
    assert second.total_items == second.dispatched_items == second.successful_items == 3
    assert [call.args[0].sequence for call in harness.dispatch.call_args_list] == [1, 2, 3]
    assert all(call.kwargs["operation"] == "verify_receipt" for call in harness.dispatch.call_args_list)
    assert [row.status for row in second.items] == ["ALREADY_APPLIED"] * 3
    assert second.counters == adapter.ItemCounters(3, 3, 0, 0, 0, 0)


def test_resume_encounters_pending_after_verifying_prior_receipt(harness):
    harness.state(1, "ALREADY_APPLIED_OBSERVED")
    harness.state(2, "RECOVERY_REQUIRED")
    result = harness.run()
    assert result.exit_code == 3
    assert result.items[0].status == "ALREADY_APPLIED"
    assert result.dispatched_items == 1
    assert result.counters.write_requests_performed == 0


@pytest.mark.parametrize("error,exit_code", [
    (runtime.WooBatchRuntimeError("private-code"), 1),
    (OSError("private-path"), 2),
])
def test_initial_loader_failure_is_safe_and_dispatches_nothing(harness, error, exit_code):
    harness.loader.side_effect = error
    result = harness.run()
    assert result.exit_code == exit_code
    assert result.batch_hash is None
    assert result.items == ()
    assert result.total_items == result.dispatched_items == result.successful_items == 0
    assert result.stopped_sequence is None
    assert result.counters == adapter.ItemCounters()
    assert "private" not in repr(result)
    harness.provider.assert_not_called()
    harness.dispatch.assert_not_called()


@pytest.mark.parametrize("change", ["batch_hash", "workspace_root", "order", "sku", "hash", "path", "count"])
def test_runtime_identity_change_before_next_dispatch_stops_without_sorting(harness, change):
    def alter(count):
        if count != 4:
            return
        if change == "batch_hash":
            harness.current = replace(harness.current, batch_hash="b" * 64)
        elif change == "workspace_root":
            harness.current = replace(harness.current, workspace_root=Path("unrelated"))
        else:
            items = list(harness.current.items)
            if change == "order":
                items.reverse()
            elif change == "count":
                items.pop()
            else:
                field, value = {"sku": ("sku", "OTHER"), "hash": ("plan_hash", "b" * 64),
                                "path": ("plan_path", Path("other.json"))}[change]
                items[1] = replace(items[1], **{field: value})
            harness.current = replace(harness.current, items=tuple(items))

    harness.on_load = alter
    result = harness.run()
    assert result.exit_code == 1
    assert result.result_code == "woo_batch_identity_mismatch"
    assert result.successful_items == result.dispatched_items == 1
    assert result.stopped_sequence == 2
    assert result.items[2].status == "NOT_DISPATCHED"
    assert harness.dispatch.call_count == 1


def test_input_runtime_cannot_claim_a_different_batch_or_reordered_members(harness):
    wrong = replace(harness.initial, items=tuple(reversed(harness.initial.items)))
    result = harness.run(runtime=wrong)
    assert result.exit_code == 1
    assert result.items == ()
    harness.dispatch.assert_not_called()


@pytest.mark.parametrize("value", [None, {}, "private"])
def test_invalid_input_or_loader_schema_is_local_error(harness, value):
    result = harness.run(runtime=value)
    assert result.exit_code == 2
    harness.loader.assert_not_called()
    harness.dispatch.assert_not_called()
    harness.loader.side_effect = lambda root: value
    assert harness.run().exit_code == 2


def test_empty_runtime_cannot_report_vacuous_success(harness):
    empty = replace(harness.initial, items=())
    assert harness.run(runtime=empty).exit_code == 2
    harness.dispatch.assert_not_called()


def test_success_without_receipt_observation_stops_before_next_item(harness):
    harness.complete_runtime = False
    result = harness.run()
    assert result.exit_code == 3
    assert result.result_code == "woo_batch_post_dispatch_runtime_unconfirmed"
    assert result.items[0].execution_result.exit_code == 0  # retained audit, not discarded
    assert result.items[0].status == "RECOVERY_REQUIRED"
    assert result.successful_items == 0
    assert result.dispatched_items == 1
    assert result.counters.write_requests_performed == 1
    assert result.counters_complete
    assert harness.provider.call_count == 1


@pytest.mark.parametrize("state", ["NOT_STARTED", "RECOVERY_REQUIRED", "BLOCKED"])
def test_post_success_runtime_change_never_triggers_cleanup_or_reapply(harness, state):
    def change(count):
        if count == 3:
            harness.state(1, state)

    harness.on_load = change
    result = harness.run()
    assert result.exit_code == 3
    assert result.items[0].observed_state == state
    assert result.dispatched_items == 1
    assert harness.dispatch.call_count == 1


def test_post_dispatch_loader_failure_is_recovery_not_success(harness):
    def fail(count):
        if count == 3:
            raise OSError("private-path")

    harness.on_load = fail
    result = harness.run()
    assert result.exit_code == 3
    assert result.counters_complete  # known Adapter counts remain known
    assert "private-path" not in repr(result)
    assert harness.dispatch.call_count == 1


@pytest.mark.parametrize("text", [
    "https://user:password@example.com?token=private", "Authorization: private",
    "Cookie: private", "ck_" + "a" * 25,
])
def test_sensitive_adapter_metadata_is_not_exposed(harness, text):
    original = execution(harness.initial.items[0])
    harness.results[1] = replace(original, provenance=replace(original.provenance, core_status=text))
    result = harness.run()
    assert result.exit_code == 3
    assert result.items[0].execution_result is None
    assert text not in json.dumps(asdict(result))


def test_result_and_nested_collections_are_immutable(harness):
    result = harness.run()
    assert isinstance(result.items, tuple)
    for value, name, new in [
        (result, "exit_code", 3), (result.items[0], "status", "BLOCKED"),
        (result.counters, "write_requests_performed", 999),
        (result.items[0].execution_result, "exit_code", 3),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, new)


def test_default_dependencies_resolve_to_existing_loader_and_adapter(harness, monkeypatch):
    monkeypatch.setattr(runtime, "load_woo_batch_runtime", harness.loader)
    monkeypatch.setattr(adapter, "execute_batch_item", harness.dispatch)
    result = coordinator.run_woo_batch(harness.initial, harness.provider, base_url=BASE_URL)
    assert result.exit_code == 0
    assert harness.loader.call_count == 7
    assert harness.dispatch.call_count == 3


def test_real_loader_mock_workspace_rejects_tampering_before_adapter(tmp_path):
    fixture = Fixture(tmp_path)
    initial = runtime.load_woo_batch_runtime(fixture.root)
    path = fixture.plan_path()
    path.write_bytes(path.read_bytes() + b"\n")
    dispatch, provider = Mock(), Mock()
    result = coordinator.run_woo_batch(initial, provider, base_url=BASE_URL, item_adapter=dispatch)
    assert result.exit_code == 1
    assert result.counters == adapter.ItemCounters()
    dispatch.assert_not_called()
    provider.assert_not_called()


def test_real_adapter_owns_authorization_validation_with_mock_workspace(tmp_path):
    fixture = Fixture(tmp_path)
    initial = runtime.load_woo_batch_runtime(fixture.root)
    item = initial.items[0]
    wrong = adapter.ManualAuthorizationContext(
        batch_hash=initial.batch_hash, sequence=item.sequence, sku=item.sku,
        confirmed_plan_hash="f" * 64, allowed_operations=frozenset({"apply"}),
    )
    result = coordinator.run_woo_batch(initial, lambda *args: wrong, base_url=BASE_URL)
    assert result.exit_code == 1
    assert result.items[0].execution_result.result_code == "woo_batch_item_authorization_invalid"
    assert result.counters == adapter.ItemCounters()
    assert result.items[1].status == "NOT_DISPATCHED"


def test_coordinator_does_not_touch_filesystem_or_make_core_calls(harness, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Coordinator must not read/write/enumerate files")

    for name in ("open", "read_bytes", "read_text", "write_bytes", "write_text", "mkdir",
                 "unlink", "rename", "replace", "iterdir", "glob", "rglob", "exists"):
        monkeypatch.setattr(Path, name, forbidden)
    assert harness.run().exit_code == 0


def test_static_boundary_no_core_transport_authorization_or_cursor_capability():
    tree = ast.parse(inspect.getsource(coordinator))
    calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
             for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, (ast.Name, ast.Attribute))}
    assert calls.isdisjoint({
        "ManualAuthorizationContext", "run_woo_product_apply", "run_woo_apply_receipt_verification",
        "inspect_woo_apply_pending", "reconcile_woo_apply_pending", "create_product",
        "request", "post", "put", "patch", "delete", "urlopen", "load_dotenv", "from_env",
        "write_text", "write_bytes", "unlink", "remove", "mkdir", "iterdir", "glob", "rglob",
        "sort", "sorted", "compute_plan_hash", "ThreadPoolExecutor", "create_task",
    })
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert names.isdisjoint({"cursor", "last_index", "previous_summary"})
    assert {field.name for field in fields(coordinator.BatchExecutionResult)} == {
        "batch_hash", "status", "exit_code", "result_code", "items", "total_items",
        "dispatched_items", "successful_items", "stopped_sequence", "counters", "counters_complete",
    }
    assert {field.name for field in fields(coordinator.BatchItemResult)} == {
        "sequence", "sku", "plan_hash", "observed_state", "status", "operation", "dispatched",
        "result_code", "execution_result",
    }
