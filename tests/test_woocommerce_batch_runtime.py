from __future__ import annotations

import ast
import hashlib
import inspect
import json
import socket
import stat
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import woocommerce_batch_runtime as runtime  # noqa: E402
from sync_worker import woocommerce_batch_plan as batch  # noqa: E402
from sync_worker import woocommerce_batch_workspace as workspace  # noqa: E402
from sync_worker import woocommerce_product_apply as apply_core  # noqa: E402
from sync_worker import woocommerce_apply_idempotency as idempotency  # noqa: E402
from sync_worker import woocommerce_pending_recovery as recovery  # noqa: E402
from sync_worker import woocommerce_pending_reconciliation as reconciliation  # noqa: E402
from tests.test_woocommerce_batch_plan import freeze  # noqa: E402


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


class Fixture:
    def __init__(self, tmp_path):
        self.report, self.root, _ = freeze(
            tmp_path, [("SKU-ONE", "One"), ("SKU-TWO", "Two")]
        )
        assert self.root is not None

    @property
    def report_path(self):
        return self.root / workspace.BATCH_REPORT_FILENAME

    def item_root(self, sequence=1):
        return self.root / workspace.ITEMS_DIRECTORY / workspace.item_directory_name(sequence)

    def plan_path(self, sequence=1):
        return self.item_root(sequence) / workspace.AUTHORITIES_DIRECTORY / workspace.PLAN_FILENAME

    def reports_root(self, sequence=1):
        return self.item_root(sequence) / workspace.REPORTS_DIRECTORY

    def persist(self, *, rehash=False):
        if rehash:
            digest = batch.compute_batch_hash(batch.semantic_batch_body(self.report))
            new_root = self.root.parent / digest
            if new_root != self.root:
                self.root.rename(new_root)
                self.root = new_root
            self.report["batch_hash"] = digest
        self.report_path.write_bytes(json_bytes(self.report))

    def seed_runtime(self, names, sequence=1):
        for name in names:
            # Deliberately not a validated Receipt/Pending: presence-only observation.
            (self.reports_root(sequence) / name).write_bytes(b"opaque runtime sentinel\n")


@pytest.fixture
def frozen(tmp_path):
    return Fixture(tmp_path)


def assert_blocked(root, code=None):
    with pytest.raises(runtime.WooBatchRuntimeError) as caught:
        runtime.load_woo_batch_runtime(root)
    assert caught.value.state == "BLOCKED"
    assert str(caught.value).startswith("woo_batch_runtime_")
    assert str(root) not in str(caught.value)
    if code is not None:
        assert str(caught.value) == code


def test_valid_frozen_workspace_loads_bound_items_in_sequence(frozen):
    result = runtime.load_woo_batch_runtime(frozen.root)
    assert isinstance(result, runtime.BatchRuntime)
    assert result.batch_hash == frozen.report["batch_hash"]
    assert result.workspace_root == frozen.root
    assert isinstance(result.items, tuple)
    assert [item.sequence for item in result.items] == [1, 2]
    assert [item.sku for item in result.items] == ["SKU-ONE", "SKU-TWO"]
    for item, source in zip(result.items, frozen.report["items"], strict=True):
        assert isinstance(item, runtime.BatchItemRuntime)
        assert item.plan_hash == source["plan_hash"]
        assert item.plan_path == frozen.plan_path(item.sequence)
        assert item.state == "NOT_STARTED"
        assert item.runtime_files == ()


def test_output_models_are_immutable(frozen):
    result = runtime.load_woo_batch_runtime(frozen.root)
    with pytest.raises(FrozenInstanceError):
        result.batch_hash = "changed"
    with pytest.raises(FrozenInstanceError):
        result.items[0].state = "APPLIED"
    assert isinstance(result.items[0].runtime_files, tuple)


@pytest.mark.parametrize("digest", [None, True, "invalid", "A" * 64, "a" * 63])
def test_invalid_batch_hash_blocked(frozen, digest):
    frozen.report["batch_hash"] = digest
    frozen.persist()
    assert_blocked(frozen.root, "woo_batch_runtime_batch_hash_invalid")


def test_workspace_directory_name_must_match_batch_hash(frozen):
    renamed = frozen.root.parent / ("f" * 64)
    frozen.root.rename(renamed)
    assert_blocked(renamed, "woo_batch_runtime_batch_hash_invalid")


def test_batch_hash_recomputed_from_semantics(frozen):
    frozen.report["items"][0]["sku"] = "ALTERED"
    frozen.persist()
    assert_blocked(frozen.root, "woo_batch_runtime_batch_hash_mismatch")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(extra="unknown"),
        lambda r: r.pop("source_manifest"),
        lambda r: r.update(status="blocked"),
        lambda r: r.update(policy_version="unknown"),
        lambda r: r.update(write_authorized=True),
        lambda r: r.update(write_authorized=0),
        lambda r: r.update(blocking_issues=["blocked"]),
        lambda r: r["target"].update(environment="production"),
        lambda r: r["target"].update(extra=True),
        lambda r: r.update(network_requests_performed=1),
        lambda r: r.update(write_requests_performed=False),
        lambda r: r["source_manifest"].update(sha256="invalid"),
        lambda r: r["source_manifest"].update(basename="password.json"),
        lambda r: r["execution_policy"].update(concurrency=True),
        lambda r: r["execution_policy"].update(concurrency=2),
        lambda r: r["execution_policy"].update(failure_policy="continue"),
        lambda r: r["execution_policy"].update(extra=True),
    ],
)
def test_batch_schema_and_policy_fail_closed_even_with_recomputed_hash(frozen, mutation):
    mutation(frozen.report)
    frozen.persist(rehash=True)
    assert_blocked(frozen.root)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(items=[]),
        lambda r: r.update(items={}),
        lambda r: r["items"].reverse(),
        lambda r: r["items"][0].update(sequence=True),
        lambda r: r["items"][0].update(sequence=0),
        lambda r: r["items"][1].update(sequence=3),
        lambda r: r["items"][0].update(sku=" "),
        lambda r: r["items"][0].update(plan_hash="invalid"),
        lambda r: r["items"][0].update(plan_path="override.json"),
        lambda r: r["items"][0]["source_plan"].update(basename="other.json"),
        lambda r: r["items"][0]["source_plan"].update(sha256="invalid"),
    ],
)
def test_item_schema_and_sequence_are_strict(frozen, mutation):
    mutation(frozen.report)
    frozen.persist(rehash=True)
    assert_blocked(frozen.root)


@pytest.mark.parametrize("field", ["sku", "plan_hash", "source_plan"])
def test_duplicate_item_authorities_are_rejected(frozen, field):
    frozen.report["items"][1][field] = frozen.report["items"][0][field]
    frozen.persist(rehash=True)
    assert_blocked(frozen.root, "woo_batch_runtime_duplicate_item")


@pytest.mark.parametrize("field", ["sku", "plan_hash"])
def test_copied_plan_must_match_batch_item_semantics(frozen, field):
    frozen.report["items"][0][field] = "DIFFERENT" if field == "sku" else "f" * 64
    frozen.persist(rehash=True)
    assert_blocked(frozen.root, "woo_batch_runtime_plan_binding_mismatch")


def test_raw_plan_sha_mismatch_even_if_semantics_identical(frozen):
    path = frozen.plan_path()
    path.write_bytes(path.read_bytes() + b"\n")
    assert_blocked(frozen.root, "woo_batch_runtime_plan_sha_mismatch")


def test_canonical_plan_validator_rejects_semantic_tampering_despite_new_raw_sha(frozen):
    plan = json.loads(frozen.plan_path().read_bytes())
    plan["operation"]["payload"]["name"] = "Changed without PLAN_HASH change"
    raw = json_bytes(plan)
    frozen.plan_path().write_bytes(raw)
    frozen.report["items"][0]["source_plan"]["sha256"] = hashlib.sha256(raw).hexdigest()
    frozen.persist(rehash=True)
    assert_blocked(frozen.root, "woo_batch_runtime_plan_invalid")


def test_existing_integrity_validator_is_used_for_every_plan(frozen, monkeypatch):
    original = apply_core.validate_frozen_plan_integrity
    calls = []

    def spy(value):
        calls.append(value["plan_hash"])
        return original(value)

    monkeypatch.setattr(apply_core, "validate_frozen_plan_integrity", spy)
    runtime.load_woo_batch_runtime(frozen.root)
    assert calls == [item["plan_hash"] for item in frozen.report["items"]]


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ((), "NOT_STARTED"),
        ((apply_core.RECEIPT_FILENAME,), "ALREADY_APPLIED_OBSERVED"),
        ((apply_core.PENDING_FILENAME,), "RECOVERY_REQUIRED"),
        ((apply_core.LOCK_FILENAME,), "RECOVERY_REQUIRED"),
        ((apply_core.PENDING_FILENAME, apply_core.RECEIPT_FILENAME), "RECOVERY_REQUIRED"),
        ((apply_core.LOCK_FILENAME, apply_core.RECEIPT_FILENAME), "RECOVERY_REQUIRED"),
        ((apply_core.PENDING_FILENAME, apply_core.LOCK_FILENAME), "RECOVERY_REQUIRED"),
        (tuple(runtime._RUNTIME_FILENAMES), "RECOVERY_REQUIRED"),
    ],
)
def test_presence_only_state_rules_and_item_isolation(frozen, names, expected):
    frozen.seed_runtime(names)
    result = runtime.load_woo_batch_runtime(frozen.root)
    assert result.items[0].state == expected
    assert result.items[0].runtime_files == tuple(sorted(names))
    assert result.items[1].state == "NOT_STARTED"
    assert result.items[1].runtime_files == ()
    assert all(item.state not in {"APPLIED", "ALREADY_APPLIED"} for item in result.items)


@pytest.mark.parametrize("relative", ["", "items", "items/000001", "items/000001/authorities", "items/000001/reports"])
@pytest.mark.parametrize("is_directory", [False, True])
def test_unknown_file_or_directory_blocks_entire_workspace(frozen, relative, is_directory):
    extra = frozen.root / relative / "unexpected"
    if is_directory:
        extra.mkdir()
    else:
        extra.write_bytes(b"sentinel")
    assert_blocked(frozen.root)


@pytest.mark.parametrize("relative", ["items/000002", "items/000001/authorities", "items/000001/reports"])
def test_missing_expected_directory_is_blocked(frozen, relative):
    original = frozen.root / relative
    original.rename(frozen.root.parent / "detached")
    assert_blocked(frozen.root)


def test_missing_copied_plan_is_blocked(frozen):
    frozen.plan_path().unlink()
    assert_blocked(frozen.root)


def test_approved_runtime_basename_cannot_be_directory(frozen):
    (frozen.reports_root() / apply_core.RECEIPT_FILENAME).mkdir()
    assert_blocked(frozen.root)


@pytest.mark.parametrize("kind", ["symlink", "reparse"])
@pytest.mark.parametrize("relative", ["", "items", "items/000001", "items/000001/authorities", "items/000001/authorities/woo-apply-plan.json", "items/000001/reports", "items/000001/reports/woo-apply-receipt.json", "woo-batch-plan.json"])
def test_linked_or_reparse_components_are_blocked(frozen, monkeypatch, kind, relative):
    frozen.seed_runtime([apply_core.RECEIPT_FILENAME])
    linked = frozen.root / relative
    original = Path.lstat

    def unsafe_lstat(path, *args, **kwargs):
        if path == linked:
            info = original(path, *args, **kwargs)
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if kind == "symlink" else info.st_mode,
                st_file_attributes=0 if kind == "symlink" else 0x400,
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unsafe_lstat)
    assert_blocked(frozen.root)


@pytest.mark.parametrize("root", ["https://example.test/batch", "file:///batch", r"\\server\share\batch"])
def test_unsafe_root_rejected_before_filesystem_access(root, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe path must be rejected before filesystem access")

    monkeypatch.setattr(Path, "lstat", forbidden)
    assert_blocked(Path(root))


def test_post_abspath_unc_is_blocked_without_share_access(frozen, monkeypatch):
    monkeypatch.setattr(runtime.package_io.os.path, "abspath", lambda p: r"\\server\share\woo-batch-plan.json")
    monkeypatch.setattr(Path, "lstat", lambda *a, **k: pytest.fail("share access forbidden"))
    assert_blocked(frozen.root)


def test_missing_root_is_not_created(tmp_path):
    root = tmp_path / "absent"
    assert_blocked(root)
    assert not root.exists()


@pytest.mark.parametrize("location", ["batch", "plan"])
@pytest.mark.parametrize("raw", [b'{"status":"ok","status":"ok"}', b'{"a":{"x":1,"x":1}}', b"\xff", b"[]", b"not-json"])
def test_invalid_utf8_json_or_duplicate_keys_fail_closed(frozen, location, raw):
    path = frozen.report_path if location == "batch" else frozen.plan_path()
    path.write_bytes(raw)
    assert_blocked(frozen.root)


def test_loading_reads_only_plan_authorities_and_has_no_side_effects(frozen, monkeypatch):
    frozen.seed_runtime(tuple(runtime._RUNTIME_FILENAMES))
    before = {p.relative_to(frozen.root): p.read_bytes() for p in frozen.root.rglob("*") if p.is_file()}
    allowed_reads = {frozen.report_path, frozen.plan_path(1), frozen.plan_path(2)}
    original_read = Path.read_bytes
    reads = []

    def guarded_read(path):
        assert path in allowed_reads, "only mock Plan authorities may be read"
        reads.append(path)
        return original_read(path)

    def forbidden(*args, **kwargs):
        pytest.fail("credentials, execution, network and mutation are forbidden")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "read_bytes", guarded_read)
        for method in ("write_bytes", "write_text", "unlink", "rename", "replace", "mkdir", "rmdir", "touch"):
            guard.setattr(Path, method, forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        for name in ("run_woo_product_apply", "_default_credential_loader", "load_woo_category_credential_source", "load_woo_category_credentials", "StdlibWooProductCreateTransport"):
            guard.setattr(apply_core, name, forbidden)
        guard.setattr(apply_core.target_snapshot, "StdlibWooProductTargetTransport", forbidden)
        guard.setattr(idempotency, "run_woo_apply_receipt_verification", forbidden)
        guard.setattr(recovery, "inspect_woo_apply_pending", forbidden)
        guard.setattr(reconciliation, "reconcile_woo_apply_pending", forbidden)
        result = runtime.load_woo_batch_runtime(frozen.root)
    assert result.items[0].state == "RECOVERY_REQUIRED"
    assert set(reads) == allowed_reads
    after = {p.relative_to(frozen.root): p.read_bytes() for p in frozen.root.rglob("*") if p.is_file()}
    assert after == before


def test_module_has_no_mutation_or_execution_calls():
    tree = ast.parse(inspect.getsource(runtime))
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert not calls.intersection({
        "unlink", "remove", "write", "write_text", "write_bytes", "replace", "rename",
        "mkdir", "makedirs", "rmtree", "touch", "run_woo_product_apply",
        "run_woo_apply_receipt_verification", "inspect_woo_apply_pending",
        "reconcile_woo_apply_pending", "create_product", "credential_loader",
        "_default_credential_loader", "load_dotenv", "load_woo_category_credentials",
        "publish_batch_workspace", "freeze_woo_batch_plan",
    })
