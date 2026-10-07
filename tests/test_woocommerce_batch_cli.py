from __future__ import annotations

import ast
import builtins
import inspect
import io
import json
import logging
import socket
import sys
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli  # noqa: E402
from sync_worker import woocommerce_batch_authorization as authorization  # noqa: E402
from sync_worker import woocommerce_batch_coordinator as coordinator  # noqa: E402
from sync_worker import woocommerce_batch_item_adapter as adapter  # noqa: E402
from sync_worker import woocommerce_batch_runtime as runtime_core  # noqa: E402
from tests.test_woocommerce_batch_authorization import approval_input  # noqa: E402
from tests.test_woocommerce_batch_coordinator import execution, make_runtime  # noqa: E402
from tests.test_woocommerce_batch_runtime import Fixture  # noqa: E402


RESULT_FIELDS = {
    "mode", "batch_hash", "status", "result_code", "exit_code", "total_items",
    "dispatched_items", "successful_items", "stopped_sequence", "counters", "counters_complete", "items",
}
ITEM_FIELDS = {
    "sequence", "sku", "plan_hash", "observed_state", "status", "operation",
    "dispatched", "result_code", "execution_result",
}


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch, capsys):
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("external")
        raise AssertionError("real Core/credentials/network access forbidden")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    for module, name in (
        (adapter.apply_core, "run_woo_product_apply"),
        (adapter.receipt_core, "run_woo_apply_receipt_verification"),
        (adapter.recovery_core, "inspect_woo_apply_pending"),
        (adapter.reconciliation_core, "reconcile_woo_apply_pending"),
        (adapter.apply_core, "_default_credential_loader"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    for name in ("run_woo_product_apply", "run_woo_apply_receipt_verification",
                 "inspect_woo_apply_pending", "reconcile_woo_apply_pending", "load_config",
                 "load_woo_category_credential_source", "load_woo_category_credentials"):
        monkeypatch.setattr(cli, name, forbidden)
    for module in (builtins, io):
        original = module.open

        def guarded(file, *args, _original=original, **kwargs):
            if isinstance(file, (str, Path)):
                path = Path(file).absolute()
                if path.name.startswith(".env") or PROJECT_ROOT / "reports" in path.parents:
                    return forbidden()
            return _original(file, *args, **kwargs)

        monkeypatch.setattr(module, "open", guarded)

    def configure():
        logger = logging.Logger("batch-cli-test", level=logging.INFO)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger

    monkeypatch.setattr(cli, "_configure_logging", configure)
    yield
    assert attempted == []


class Workflow:
    """In-memory Runtime and mock Adapter; no Core or transport executes."""

    def __init__(self, tmp_path, monkeypatch, *, count=3):
        self.initial = make_runtime(tmp_path)
        if count != 3:
            self.initial = replace(self.initial, items=tuple(
                runtime_core.BatchItemRuntime(
                    i, f"SKU-{i}", f"{i:064x}",
                    self.initial.workspace_root / "items" / f"{i:06d}" / "authorities" / "woo-apply-plan.json",
                    "NOT_STARTED", (),
                ) for i in range(1, count + 1)
            ))
        self.current = self.initial
        self.load_count = 0
        self.on_load = None
        self.results = {}
        self.path = tmp_path / "woo-batch-approvals.json"
        self.value = approval_input(self.initial)
        self.persist()
        self.loader = Mock(side_effect=self.load)
        self.dispatch = Mock(side_effect=self.execute)
        monkeypatch.setattr(runtime_core, "load_woo_batch_runtime", self.loader)
        monkeypatch.setattr(adapter, "execute_batch_item", self.dispatch)

    def persist(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")

    def state(self, sequence, state):
        names = {"NOT_STARTED": (), "ALREADY_APPLIED_OBSERVED": ("woo-apply-receipt.json",),
                 "RECOVERY_REQUIRED": ("woo-apply-pending.json",)}
        self.current = replace(self.current, items=tuple(
            replace(item, state=state, runtime_files=names[state]) if item.sequence == sequence else item
            for item in self.current.items
        ))

    def load(self, root):
        self.load_count += 1
        if self.on_load is not None:
            self.on_load(self.load_count)
        return self.current

    def execute(self, item, approved, *, operation, base_url):
        assert base_url == adapter.apply_core.APPROVED_BASE_URL
        assert approved.batch_hash == self.current.batch_hash
        assert approved.sequence == item.sequence
        assert approved.sku == item.sku
        assert approved.confirmed_plan_hash == item.plan_hash
        assert operation in approved.allowed_operations
        result = self.results.get(item.sequence, execution(item, operation))
        if isinstance(result, BaseException):
            raise result
        if result.exit_code == 0:
            self.state(item.sequence, "ALREADY_APPLIED_OBSERVED")
        return result

    def argv(self, mode="run"):
        return [f"{mode}-woo-batch", "--batch-hash", self.initial.batch_hash,
                "--batch-root", str(self.initial.workspace_root.parent),
                *(["--authorization-file", str(self.path)] if mode == "run" else [])]


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    return Workflow(tmp_path, monkeypatch)


def finished(capsys, exit_code):
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    report = json.loads(captured.out)
    assert set(report) == RESULT_FIELDS
    assert report["exit_code"] == exit_code
    for item in report["items"]:
        assert set(item) == ITEM_FIELDS
    logs = [json.loads(line) for line in captured.err.splitlines()]
    assert len(logs) == 1
    assert logs[0]["result_code"] == report["result_code"]
    return report


@pytest.mark.parametrize("command", ["inspect-woo-batch", "run-woo-batch"])
def test_commands_registered_with_minimal_surface(command):
    argv = [command, "--batch-hash", "a" * 64]
    if command == "run-woo-batch":
        argv += ["--authorization-file", "woo-batch-approvals.json"]
    args = cli.build_parser().parse_args(argv)
    assert args.command == command
    assert args.batch_root is None
    assert set(vars(args)) == {"command", "batch_hash", "batch_root"} | (
        {"authorization_file"} if command == "run-woo-batch" else set()
    )


@pytest.mark.parametrize("argv", [
    ["inspect-woo-batch"], ["run-woo-batch"],
    ["run-woo-batch", "--batch-hash", "a" * 64],
    ["run-woo-batch", "--authorization-file", "woo-batch-approvals.json"],
])
def test_required_arguments_fail_before_execution(argv):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize("flag", [
    "--target", "--base-url", "--plan", "--start-sequence", "--resume-from", "--retry",
    "--skip", "--cleanup", "--recover", "--parallel", "--batch-h", "--authorization-f",
])
def test_override_and_abbreviated_flags_rejected(flag):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args([
            "run-woo-batch", "--batch-hash", "a" * 64,
            "--authorization-file", "woo-batch-approvals.json", flag, "value",
        ])
    assert caught.value.code == 2


def test_no_separate_resume_command():
    assert "resume-woo-batch" not in cli.build_parser().format_help()


def test_inspect_is_observation_only_and_never_reads_approvals(workflow, monkeypatch, capsys):
    workflow.state(1, "ALREADY_APPLIED_OBSERVED")
    monkeypatch.setattr(coordinator, "run_woo_batch", lambda *a, **k: pytest.fail("inspect called Coordinator"))
    monkeypatch.setattr(authorization, "load_woo_batch_authorization", lambda *a: pytest.fail("inspect read approvals"))
    assert cli.main(workflow.argv("inspect")) == 0
    report = finished(capsys, 0)
    assert report["mode"] == "inspect"
    assert report["result_code"] == cli.WOO_BATCH_INSPECT_COMPLETED
    assert report["items"][0]["observed_state"] == "ALREADY_APPLIED_OBSERVED"
    assert all(item["status"] == "NOT_DISPATCHED" and item["execution_result"] is None for item in report["items"])
    assert report["successful_items"] == report["dispatched_items"] == 0
    assert report["counters"] == asdict(adapter.ItemCounters())
    assert report["counters_complete"] is True
    workflow.dispatch.assert_not_called()
    workflow.loader.assert_called_once()


def test_default_root_uses_project_reports_without_creation(workflow, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(["inspect-woo-batch", "--batch-hash", workflow.initial.batch_hash]) == 0
    finished(capsys, 0)
    workflow.loader.assert_called_once_with(tmp_path / "reports" / "woo-batches" / workflow.initial.batch_hash)
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "short", ""])
def test_full_lowercase_hash_required_before_loader(workflow, capsys, digest):
    assert cli.main(["inspect-woo-batch", "--batch-hash", digest]) == 2
    report = finished(capsys, 2)
    assert report["result_code"] == cli.WOO_BATCH_HASH_INVALID
    workflow.loader.assert_not_called()


def test_missing_workspace_fails_without_creating_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("directory creation forbidden"))
    missing = tmp_path / "does-not-exist"
    assert cli.main(["inspect-woo-batch", "--batch-hash", "a" * 64, "--batch-root", str(missing)]) == 1
    report = finished(capsys, 1)
    assert report["result_code"] == cli.WOO_BATCH_WORKSPACE_REJECTED
    assert not missing.exists()


@pytest.mark.parametrize("root", ["https://example.test/batches", "file:///batches", r"\\server\share\batches"])
def test_unsafe_workspace_roots_rejected_without_share_or_url_access(root, capsys):
    assert cli.main(["inspect-woo-batch", "--batch-hash", "a" * 64, "--batch-root", root]) == 1
    report = finished(capsys, 1)
    assert report["result_code"] == cli.WOO_BATCH_WORKSPACE_REJECTED
    assert root not in json.dumps(report)


@pytest.mark.parametrize("mode", ["inspect", "run"])
@pytest.mark.parametrize("tamper", ["batch", "plan", "target", "directory", "symlink", "reparse"])
def test_real_loader_rejects_mock_workspace_tampering_before_coordinator(
    tmp_path, monkeypatch, capsys, mode, tamper,
):
    frozen = Fixture(tmp_path)
    observed = runtime_core.load_woo_batch_runtime(frozen.root)
    approvals = tmp_path / "woo-batch-approvals.json"
    approvals.write_text(json.dumps(approval_input(observed)), encoding="utf-8")
    if tamper == "batch":
        frozen.report["items"][0]["sku"] = "TAMPERED"
        frozen.persist()
    elif tamper == "plan":
        frozen.plan_path().write_bytes(frozen.plan_path().read_bytes() + b"\n")
    elif tamper == "target":
        frozen.report["target"]["environment"] = "production"
        frozen.persist(rehash=True)
    elif tamper == "directory":
        frozen.root.rename(frozen.root.parent / ("d" * 64))
    else:
        monkeypatch.setattr(runtime_core.package_io, "_has_link_or_reparse", lambda _: True)
    call = Mock(side_effect=AssertionError("Coordinator must not execute"))
    monkeypatch.setattr(coordinator, "run_woo_batch", call)
    argv = [f"{mode}-woo-batch", "--batch-hash", frozen.report["batch_hash"],
            "--batch-root", str(frozen.root.parent)]
    if mode == "run":
        argv += ["--authorization-file", str(approvals)]
    assert cli.main(argv) == 1
    finished(capsys, 1)
    call.assert_not_called()


@pytest.mark.parametrize("names", [
    ("woo-apply-pending.json",), ("woo-apply.lock.json",),
    ("woo-apply-pending.json", "woo-apply-receipt.json"),
    ("woo-apply.lock.json", "woo-apply-receipt.json"),
])
def test_real_inspection_uses_presence_only_and_does_not_modify_runtime(tmp_path, monkeypatch, capsys, names):
    frozen = Fixture(tmp_path)
    frozen.seed_runtime(names, sequence=2)
    frozen.seed_runtime(("woo-apply-pending.json",), sequence=1)
    before = {path: path.read_bytes() for path in frozen.root.rglob("*") if path.is_file()}
    read = Path.read_bytes

    def guarded_read(path):
        assert path.name not in runtime_core._RUNTIME_FILENAMES, "journal contents must not be opened"
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("no directory creation"))
    assert cli.main(["inspect-woo-batch", "--batch-hash", frozen.report["batch_hash"],
                     "--batch-root", str(frozen.root.parent)]) == 3
    report = finished(capsys, 3)
    assert report["stopped_sequence"] == 1
    assert report["result_code"] == cli.WOO_BATCH_INSPECT_RECOVERY_REQUIRED
    assert report["successful_items"] == report["dispatched_items"] == 0
    assert {path: read(path) for path in frozen.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("change", ["partial", "extra", "batch", "sequence", "sku", "plan"])
def test_complete_binding_gate_blocks_before_any_dispatch(workflow, capsys, change):
    if change == "partial":
        workflow.value["items"].pop()
    elif change == "extra":
        workflow.value["items"].append({**workflow.value["items"][-1], "sequence": 4})
    elif change == "batch":
        workflow.value["batch_hash"] = "f" * 64
    elif change == "sequence":
        workflow.value["items"][0]["sequence"] = 99
    elif change == "sku":
        workflow.value["items"][0]["sku"] = "OTHER"
    else:
        workflow.value["items"][0]["confirmed_plan_hash"] = "f" * 64
    workflow.persist()
    assert cli.main(workflow.argv()) == 1
    report = finished(capsys, 1)
    assert report["result_code"] == authorization.AUTHORIZATION_BINDING_BLOCKED
    assert report["dispatched_items"] == 0
    assert report["counters"] == asdict(adapter.ItemCounters())
    workflow.dispatch.assert_not_called()
    assert workflow.loader.call_count == 1


@pytest.mark.parametrize("raw", [
    "not-json", '{"policy_version":"x","policy_version":"x"}',
    '{"credential":"ck_' + "z" * 30 + '"}',
])
def test_malformed_authorization_input_is_local_error_and_not_echoed(workflow, capsys, raw):
    workflow.path.write_text(raw, encoding="utf-8")
    assert cli.main(workflow.argv()) == 2
    report = finished(capsys, 2)
    assert report["result_code"] == authorization.AUTHORIZATION_INPUT_INVALID
    assert raw not in json.dumps(report)
    workflow.dispatch.assert_not_called()


def test_run_routes_once_through_coordinator_and_forwards_existing_contexts(workflow, monkeypatch, capsys):
    original = authorization.load_woo_batch_authorization
    loaded = []

    def capture(*args):
        provider = original(*args)
        loaded.append(provider)
        return provider

    monkeypatch.setattr(authorization, "load_woo_batch_authorization", capture)
    real_run = coordinator.run_woo_batch
    invoke = Mock(wraps=real_run)
    monkeypatch.setattr(coordinator, "run_woo_batch", invoke)
    assert cli.main(workflow.argv()) == 0
    report = finished(capsys, 0)
    assert report["result_code"] == "woo_batch_completed"
    assert report["total_items"] == report["successful_items"] == report["dispatched_items"] == 3
    assert [row["operation"] for row in report["items"]] == ["apply"] * 3
    assert report["counters"]["write_requests_performed"] == 3  # mock counters only
    invoke.assert_called_once()
    assert len(loaded) == 1
    for dispatch in workflow.dispatch.call_args_list:
        item, approved = dispatch.args
        assert approved is loaded[0](workflow.initial.batch_hash, item, dispatch.kwargs["operation"])


def test_reinvocation_starts_at_one_and_verifies_receipts_without_new_apply(workflow, capsys):
    assert cli.main(workflow.argv()) == 0
    finished(capsys, 0)
    workflow.dispatch.reset_mock()
    assert cli.main(workflow.argv()) == 0
    report = finished(capsys, 0)
    assert [call.args[0].sequence for call in workflow.dispatch.call_args_list] == [1, 2, 3]
    assert [row["operation"] for row in report["items"]] == ["verify_receipt"] * 3
    assert [row["status"] for row in report["items"]] == ["ALREADY_APPLIED"] * 3
    assert report["counters"] == asdict(adapter.ItemCounters(3, 3, 0, 0, 0, 0))


def test_receipts_then_not_started_follow_fresh_runtime(workflow, capsys):
    workflow.state(1, "ALREADY_APPLIED_OBSERVED")
    workflow.state(2, "ALREADY_APPLIED_OBSERVED")
    assert cli.main(workflow.argv()) == 0
    report = finished(capsys, 0)
    assert [row["operation"] for row in report["items"]] == ["verify_receipt", "verify_receipt", "apply"]


def test_recovery_stop_preserves_prefix_and_prevents_later_dispatch(workflow, capsys):
    workflow.state(1, "ALREADY_APPLIED_OBSERVED")
    workflow.state(2, "RECOVERY_REQUIRED")
    assert cli.main(workflow.argv()) == 3
    report = finished(capsys, 3)
    assert report["result_code"] == "woo_batch_item_recovery_required"
    assert report["stopped_sequence"] == 2
    assert report["successful_items"] == report["dispatched_items"] == 1
    assert report["items"][2]["status"] == "NOT_DISPATCHED"
    workflow.dispatch.assert_called_once()


@pytest.mark.parametrize("state", ["NOT_STARTED", "RECOVERY_REQUIRED"])
def test_processed_prefix_regression_result_is_propagated_unchanged(workflow, capsys, state):
    def regress(count):
        if count == 5:  # CLI initial load + refresh before item 2
            workflow.state(1, state)

    workflow.on_load = regress
    assert cli.main(workflow.argv()) == 3
    report = finished(capsys, 3)
    assert report["status"] == "recovery_required"
    assert report["result_code"] == "woo_batch_processed_item_regressed"
    assert report["stopped_sequence"] == 1
    assert report["successful_items"] == 0
    assert report["dispatched_items"] == 1
    assert report["items"][0]["execution_result"]["exit_code"] == 0
    workflow.dispatch.assert_called_once()


def test_missing_operation_is_blocked_by_real_adapter_without_permission_expansion(tmp_path, capsys):
    frozen = Fixture(tmp_path)
    observed = runtime_core.load_woo_batch_runtime(frozen.root)
    value = approval_input(observed)
    value["items"][0]["allowed_operations"] = ["verify_receipt"]
    path = tmp_path / "woo-batch-approvals.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert cli.main(["run-woo-batch", "--batch-hash", observed.batch_hash,
                     "--batch-root", str(frozen.root.parent), "--authorization-file", str(path)]) == 1
    report = finished(capsys, 1)
    assert report["items"][0]["result_code"] == "woo_batch_item_authorization_invalid"
    assert report["counters"] == asdict(adapter.ItemCounters())
    assert report["items"][1]["status"] == "NOT_DISPATCHED"


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_coordinator_originated_result_is_not_rewritten(workflow, monkeypatch, capsys, exit_code):
    rows = (coordinator.BatchItemResult(
        1, "SKU", "b" * 64, "NOT_STARTED", "NOT_DISPATCHED", None, False,
        "woo_batch_not_dispatched", None,
    ),)
    result = coordinator.BatchExecutionResult(
        workflow.initial.batch_hash,
        {0: "completed", 1: "blocked", 2: "local_error", 3: "recovery_required"}[exit_code],
        exit_code, "woo_batch_runtime_local_error", rows, 1, 0, 0,
        None, adapter.ItemCounters.unknown(), False,
    )
    invoke = Mock(return_value=result)
    monkeypatch.setattr(coordinator, "run_woo_batch", invoke)
    assert cli.main(workflow.argv()) == exit_code
    report = finished(capsys, exit_code)
    assert report == json.loads(json.dumps({"mode": "run", **asdict(result)}))
    assert all(count is None for count in report["counters"].values())
    assert report["counters_complete"] is False
    invoke.assert_called_once()


@pytest.mark.parametrize("stage", ["initial_runtime", "authorization"])
def test_keyboard_interrupt_before_coordinator_is_definite_local_error(workflow, monkeypatch, capsys, stage):
    if stage == "initial_runtime":
        workflow.loader.side_effect = KeyboardInterrupt()
    else:
        monkeypatch.setattr(authorization, "load_woo_batch_authorization", Mock(side_effect=KeyboardInterrupt()))
    assert cli.main(workflow.argv()) == 2
    report = finished(capsys, 2)
    assert report["result_code"] == cli.WOO_BATCH_PRE_DISPATCH_INTERRUPTED
    assert report["counters"] == asdict(adapter.ItemCounters())
    assert report["counters_complete"] is True
    workflow.dispatch.assert_not_called()


def test_keyboard_interrupt_escaping_coordinator_has_unknown_effects(workflow, monkeypatch, capsys):
    invoke = Mock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(coordinator, "run_woo_batch", invoke)
    assert cli.main(workflow.argv()) == 3
    report = finished(capsys, 3)
    assert report["result_code"] == cli.WOO_BATCH_EXECUTION_INTERRUPTED
    assert report["status"] == "recovery_required"
    assert report["dispatched_items"] is report["successful_items"] is None
    assert all(value is None for value in report["counters"].values())
    assert report["counters_complete"] is False
    invoke.assert_called_once()


@pytest.mark.parametrize("refresh", [2, 5])
def test_keyboard_interrupt_during_real_coordinator_reload_is_exit_three(workflow, capsys, refresh):
    def interrupt(count):
        if count == refresh:
            raise KeyboardInterrupt()

    workflow.on_load = interrupt
    assert cli.main(workflow.argv()) == 3
    report = finished(capsys, 3)
    assert report["result_code"] == cli.WOO_BATCH_EXECUTION_INTERRUPTED
    assert report["counters_complete"] is False
    assert report["counters"]["write_requests_performed"] is None
    assert workflow.dispatch.call_count == (0 if refresh == 2 else 1)


def test_keyboard_interrupt_during_authorization_callback_is_exit_three(workflow, monkeypatch, capsys):
    monkeypatch.setattr(authorization.BatchManualAuthorization, "__call__", Mock(side_effect=KeyboardInterrupt()))
    assert cli.main(workflow.argv()) == 3
    report = finished(capsys, 3)
    assert report["result_code"] == cli.WOO_BATCH_EXECUTION_INTERRUPTED
    workflow.dispatch.assert_not_called()


def test_keyboard_interrupt_in_adapter_preserves_coordinator_recovery_result(workflow, capsys):
    workflow.results[2] = KeyboardInterrupt()
    assert cli.main(workflow.argv()) == 3
    report = finished(capsys, 3)
    assert report["result_code"] == "woo_batch_adapter_exception"
    assert report["stopped_sequence"] == 2
    assert report["successful_items"] == 1
    assert report["counters_complete"] is False
    assert report["counters"]["write_requests_performed"] is None
    assert workflow.dispatch.call_count == 2


@pytest.mark.parametrize("error", [
    RuntimeError("Authorization Cookie ck_" + "s" * 30 + " C:\\secret\\file https://user:password@example.test"),
    OSError("private payload"),
])
def test_escaped_execution_exception_is_safe_uncertain_without_retry(workflow, monkeypatch, capsys, error):
    invoke = Mock(side_effect=error)
    monkeypatch.setattr(coordinator, "run_woo_batch", invoke)
    assert cli.main(workflow.argv()) == 3
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["result_code"] == cli.WOO_BATCH_EXECUTION_UNCERTAIN
    assert report["counters_complete"] is False
    assert "private payload" not in captured.out + captured.err
    assert "C:\\secret" not in captured.out + captured.err
    assert "https://" not in captured.out + captured.err
    assert "ck_" not in captured.out + captured.err
    invoke.assert_called_once()


def test_output_contains_no_paths_authorization_or_raw_payload(workflow, capsys):
    assert cli.main(workflow.argv()) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    for forbidden in (str(workflow.path), str(workflow.initial.workspace_root),
                      "allowed_operations", "confirmed_plan_hash", "payload", "https://", "Authorization", "Cookie"):
        assert forbidden not in captured.out + captured.err
    assert "provenance" in report["items"][0]["execution_result"]
    assert report["items"][0]["execution_result"]["counters"]["write_requests_performed"] == 1


def test_unsafe_injected_result_fails_closed_without_exposure(workflow, monkeypatch, capsys):
    original = coordinator.BatchExecutionResult(
        workflow.initial.batch_hash, "completed", 0, "https://user:pass@example.test", (),
        0, 0, 0, None, adapter.ItemCounters(), True,
    )
    monkeypatch.setattr(coordinator, "run_woo_batch", Mock(return_value=original))
    assert cli.main(workflow.argv()) == 3
    captured = capsys.readouterr()
    assert "https://" not in captured.out + captured.err
    assert json.loads(captured.out)["counters_complete"] is False


def test_preflight_error_output_is_also_checked_for_paths(workflow, capsys):
    workflow.current = replace(workflow.current, items=tuple(
        replace(item, sku="C:\\private\\product") if item.sequence == 1 else item
        for item in workflow.current.items
    ))
    assert cli.main(workflow.argv()) == 2
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err
    report = json.loads(captured.out)
    assert report["result_code"] == cli.WOO_BATCH_LOCAL_ERROR
    assert report["counters_complete"] is True
    workflow.dispatch.assert_not_called()


def test_stdout_write_failure_does_not_reinvoke_execution(workflow, monkeypatch, capsys):
    invoke = Mock(wraps=coordinator.run_woo_batch)
    monkeypatch.setattr(coordinator, "run_woo_batch", invoke)
    monkeypatch.setattr(builtins, "print", Mock(side_effect=BrokenPipeError()))
    assert cli.main(workflow.argv()) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["event"] == cli.WOO_BATCH_OUTPUT_FAILED
    invoke.assert_called_once()


def test_no_cli_eight_item_cap(tmp_path, monkeypatch, capsys):
    workflow = Workflow(tmp_path, monkeypatch, count=9)
    assert cli.main(workflow.argv()) == 0
    report = finished(capsys, 0)
    assert report["total_items"] == report["successful_items"] == 9
    assert workflow.dispatch.call_count == 9


def test_cli_creates_no_local_execution_authority_or_result_file(workflow, monkeypatch, capsys):
    root = workflow.path.parent
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("CLI must not mutate files or create directories")

    for name in ("mkdir", "write_bytes", "write_text", "unlink", "rename"):
        monkeypatch.setattr(Path, name, forbidden)
    assert cli.main(workflow.argv()) == 0
    finished(capsys, 0)
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_new_cli_helpers_have_no_transport_mutation_or_state_persistence():
    source = "\n".join(inspect.getsource(function) for function in (
        cli._load_cli_woo_batch, cli._woo_batch_local_result, cli._encode_woo_batch_result, cli._run_woo_batch_cli,
    ))
    tree = ast.parse(source)
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"mkdir", "unlink", "remove", "write_bytes", "write_text", "create_product", "execute_batch_item"}
    assert not any(token in source for token in (
        "batch_workspace_path(", "load_config(", "requests.", "httpx.", "urllib", "load_woo_category_credentials(",
        "inspect_woo_apply_pending(", "reconcile_woo_apply_pending(", "run_woo_product_apply(",
        "last_successful_sequence", "checkpoint", "cursor",
    ))
