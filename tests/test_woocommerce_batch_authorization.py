from __future__ import annotations

import ast
import builtins
import copy
import inspect
import io
import json
import os
import socket
import stat
import sys
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

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
        os.link(path, path.with_name("linked-approvals.json"))
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
    read = authorization._read_opened_authorization
    decode = authorization.json.loads

    def counted_read(descriptor):
        reads.append(descriptor)
        assert stat.S_ISREG(os.fstat(descriptor).st_mode)
        return read(descriptor)

    def counted_parse(*args, **kwargs):
        parses.append(1)
        return decode(*args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("second path lookup forbidden"))
    monkeypatch.setattr(authorization, "_read_opened_authorization", counted_read)
    monkeypatch.setattr(authorization.json, "loads", counted_parse)
    provider = authorization.load_woo_batch_authorization(path, observed)
    path.write_text("changed after invocation preflight", encoding="utf-8")
    for _ in range(3):
        assert provider(observed.batch_hash, observed.items[0], "apply") is provider.items[0]
    assert len(reads) == 1
    with pytest.raises(OSError):
        os.fstat(reads[0])  # descriptor is closed before parsing/provider reuse
    assert parses == [1]


def test_symlink_approval_path_is_rejected(inputs, monkeypatch):
    observed, _, path = inputs
    alias = path.with_name("alias-approvals.json")
    if os.name == "nt":
        # Creating Windows symlinks requires a privilege not granted to the test
        # process. Model lstat's link metadata, not a privilege-related failure.
        lstat = Path.lstat

        def linked(local, *args, **kwargs):
            if local == alias:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0x400)
            return lstat(local, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", linked)
    else:
        alias.symlink_to(path)
    read = Mock(side_effect=AssertionError("linked bytes must not be read"))
    monkeypatch.setattr(authorization, "_read_opened_authorization", read)
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(alias, observed)
    assert caught.value.exit_code == 2
    read.assert_not_called()


def test_leaf_link_swap_after_path_validation_is_rejected(inputs, monkeypatch):
    observed, _, path = inputs
    target = path.with_name("unchecked-approvals.json")
    target.write_bytes(b"unchecked linked bytes")
    validate = authorization.package_io._local_path

    def swap(local, *, require_file):
        validated = validate(local, require_file=require_file)
        path.unlink()
        os.link(target, path)  # real, unprivileged link-swap; target now has two links
        return validated

    monkeypatch.setattr(authorization.package_io, "_local_path", swap)
    read = Mock(side_effect=AssertionError("link-swap target must never be read"))
    monkeypatch.setattr(authorization, "_read_opened_authorization", read)
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(path, observed)
    assert caught.value.exit_code == 2
    read.assert_not_called()


def test_ancestor_link_swap_after_path_validation_is_rejected(inputs, monkeypatch):
    observed, _, path = inputs
    validate = authorization.package_io._local_path
    validation_done = []

    def checked(local, *, require_file):
        validated = validate(local, require_file=require_file)
        validation_done.append(validated)
        return validated

    monkeypatch.setattr(authorization.package_io, "_local_path", checked)
    if os.name == "nt":
        import ctypes

        def metadata(handle, pointer):
            pointer._obj.attributes = 0x10
            return True

        kernel = SimpleNamespace(CreateFileW=Mock(return_value=10), GetFileType=Mock(return_value=1),
                                 GetFileInformationByHandle=Mock(side_effect=metadata),
                                 CloseHandle=Mock(return_value=True))
        refused = []

        def opened(out, access, attributes, status, allocation, file_attributes,
                   share, disposition, options, extended, extended_length):
            assert validation_done == [path.absolute()]
            request = attributes._obj
            assert request.Attributes & 0x1000 and options & 0x200000
            name = ctypes.wstring_at(request.ObjectName.contents.Buffer)
            if name == path.parent.name:
                refused.append(name)
                return -1  # model STATUS_REPARSE_POINT_ENCOUNTERED, not privilege denial
            out._obj.value = 11
            return 0

        native = SimpleNamespace(NtCreateFile=Mock(side_effect=opened))
        monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else native)
    else:
        original_open = os.open
        refused = []

        def opened(name, flags, *, dir_fd=None):
            assert validation_done == [path.absolute()]
            assert flags & os.O_NOFOLLOW
            if name == path.parent.name:
                refused.append(name)
                raise OSError("simulated ancestor no-follow refusal")
            return original_open(name, flags, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", opened)
        monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, opened})
    read = Mock(side_effect=AssertionError("reparse ancestor must never be traversed"))
    monkeypatch.setattr(authorization, "_read_opened_authorization", read)
    with pytest.raises(authorization.WooBatchAuthorizationError):
        authorization.load_woo_batch_authorization(path, observed)
    assert refused == [path.parent.name]
    read.assert_not_called()


def test_swap_after_descriptor_validation_never_reads_replacement(inputs, monkeypatch):
    observed, _, path = inputs
    original_bytes = path.read_bytes()
    target = path.with_name("unchecked-approvals.json")
    target.write_bytes(b"unchecked replacement bytes")
    checked, fdopen = authorization._checked_file_stat, os.fdopen
    consumed, attempts = [], []

    def swap_after_check(descriptor):
        info = checked(descriptor)
        if not attempts:
            attempts.append(descriptor)
            try:
                path.rename(path.with_name("original-approvals.json"))
            except PermissionError:
                # Windows SHARE_READ excludes DELETE: the pinned leaf cannot
                # be renamed/replaced while the validated handle is open.
                assert os.name == "nt"
            else:
                path.symlink_to(target)
        return info

    @contextmanager
    def track_read(descriptor, *args, **kwargs):
        with fdopen(descriptor, *args, **kwargs) as opened:
            def read(size):
                raw = opened.read(size)
                consumed.append(raw)
                return raw
            yield SimpleNamespace(read=read)

    monkeypatch.setattr(authorization, "_checked_file_stat", swap_after_check)
    monkeypatch.setattr(os, "fdopen", track_read)
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("second pathname read forbidden"))
    try:
        provider = authorization.load_woo_batch_authorization(path, observed)
    except authorization.WooBatchAuthorizationError:
        # POSIX may detect the rename via the post-read change check. Either
        # refusal or reading the originally opened object is safe; redirection
        # to the new symlink target is never allowed.
        assert os.name == "posix"
    else:
        assert provider.batch_hash == observed.batch_hash
    assert len(attempts) == 1
    assert consumed == [original_bytes]


def test_one_descriptor_is_validated_read_closed_then_parsed(inputs, monkeypatch):
    observed, _, path = inputs
    checked, fdopen, decode = authorization._checked_file_stat, os.fdopen, json.loads
    events = []

    def validate(descriptor):
        events.append(("validate", descriptor))
        return checked(descriptor)

    @contextmanager
    def read_opened(descriptor, *args, **kwargs):
        assert events == [("validate", descriptor)]
        assert kwargs["closefd"] is False
        with fdopen(descriptor, *args, **kwargs) as opened:
            def read(size):
                events.append(("read", descriptor))
                assert size == authorization.MAX_AUTHORIZATION_BYTES + 1
                return opened.read(size)
            yield SimpleNamespace(read=read)

    def parse(raw, **kwargs):
        descriptor = events[0][1]
        assert events == [("validate", descriptor), ("read", descriptor), ("validate", descriptor)]
        with pytest.raises(OSError):
            os.fstat(descriptor)
        events.append(("parse", descriptor))
        return decode(raw, **kwargs)

    monkeypatch.setattr(authorization, "_checked_file_stat", validate)
    monkeypatch.setattr(os, "fdopen", read_opened)
    monkeypatch.setattr(json, "loads", parse)
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("second path read forbidden"))
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("second text read forbidden"))
    assert authorization.load_woo_batch_authorization(path, observed).batch_hash == observed.batch_hash
    assert [name for name, _ in events] == ["validate", "read", "validate", "parse"]


@pytest.mark.parametrize("change", ["oversized", "empty", "not_regular", "hardlink", "reparse", "unknown_links"]
                         + (["unknown_attributes"] if os.name == "nt" else []))
def test_unsafe_opened_metadata_blocks_before_read(inputs, monkeypatch, change):
    observed, _, path = inputs
    fstat, fdopen = os.fstat, Mock(side_effect=AssertionError("unsafe descriptor must not be read"))

    def metadata(descriptor):
        actual = fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode):
            return actual
        values = {"st_mode": actual.st_mode, "st_nlink": actual.st_nlink,
                  "st_size": actual.st_size, "st_file_attributes": 0}
        if change == "oversized":
            values["st_size"] = authorization.MAX_AUTHORIZATION_BYTES + 1
        elif change == "empty":
            values["st_size"] = 0
        elif change == "not_regular":
            values["st_mode"] = stat.S_IFIFO
        elif change == "hardlink":
            values["st_nlink"] = 2
        elif change == "reparse":
            values["st_file_attributes"] = 0x400
        elif change == "unknown_links":
            del values["st_nlink"]
        else:
            del values["st_file_attributes"]
        return SimpleNamespace(**values)

    monkeypatch.setattr(os, "fstat", metadata)
    monkeypatch.setattr(os, "fdopen", fdopen)
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(path, observed)
    assert caught.value.exit_code == 2
    fdopen.assert_not_called()


def test_actual_oversized_file_is_rejected_without_read(inputs, monkeypatch):
    observed, _, path = inputs
    monkeypatch.setattr(authorization, "MAX_AUTHORIZATION_BYTES", path.stat().st_size - 1)
    read = Mock(side_effect=AssertionError("oversized descriptor must not be read"))
    monkeypatch.setattr(os, "fdopen", read)
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(path, observed)
    assert caught.value.exit_code == 2
    read.assert_not_called()


def test_empty_file_is_rejected_without_read(inputs, monkeypatch):
    observed, _, path = inputs
    path.write_bytes(b"")
    monkeypatch.setattr(os, "fdopen", Mock(side_effect=AssertionError("empty descriptor must not be read")))
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(path, observed)
    assert caught.value.exit_code == 2


def test_file_metadata_change_during_read_fails_closed(inputs, monkeypatch):
    observed, _, path = inputs
    fstat = os.fstat
    calls = []

    def changed(descriptor):
        actual = fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode):
            return actual
        calls.append(descriptor)
        if len(calls) == 1:
            return actual
        values = {field: getattr(actual, field) for field in (
            "st_mode", "st_nlink", "st_size", "st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns",
        )}
        if hasattr(actual, "st_file_attributes"):
            values["st_file_attributes"] = actual.st_file_attributes
        values["st_mtime_ns"] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(authorization.WooBatchAuthorizationError):
        authorization.load_woo_batch_authorization(path, observed)
    assert len(calls) == 2 and calls[0] == calls[1]


def test_secure_open_failure_has_safe_error_without_fallback(inputs, monkeypatch):
    observed, _, path = inputs
    monkeypatch.setattr(authorization, "_open_authorization", Mock(side_effect=OSError(str(path))))
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("unsafe fallback forbidden"))
    with pytest.raises(authorization.WooBatchAuthorizationError) as caught:
        authorization.load_woo_batch_authorization(path, observed)
    assert str(caught.value) == authorization.AUTHORIZATION_INPUT_INVALID
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("unsupported", ["O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK", "dir_fd"])
def test_posix_missing_secure_open_semantics_fails_closed(monkeypatch, unsupported):
    opened = Mock()
    with monkeypatch.context() as scoped:
        for index, name in enumerate(("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")):
            scoped.setattr(os, name, 1 << (index + 8), raising=False)
        scoped.setattr(os, "open", opened)
        scoped.setattr(os, "supports_dir_fd", {opened})
        if unsupported == "dir_fd":
            scoped.setattr(os, "supports_dir_fd", set())
        else:
            scoped.setattr(os, unsupported, 0)
        with pytest.raises(authorization.WooBatchAuthorizationError):
            with authorization._open_posix_authorization(PurePosixPath("/intake/approvals.json")):
                pytest.fail("unsupported open must not succeed")
        opened.assert_not_called()


def test_posix_opens_all_components_relative_with_no_follow(monkeypatch):
    calls, closed = [], []

    def opened(name, flags, *, dir_fd=None):
        calls.append((name, flags, dir_fd))
        return len(calls) + 10

    with monkeypatch.context() as scoped:
        for index, name in enumerate(("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")):
            scoped.setattr(os, name, 1 << (index + 8), raising=False)
        scoped.setattr(os, "open", opened)
        scoped.setattr(os, "supports_dir_fd", {opened})
        scoped.setattr(os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFDIR))
        scoped.setattr(os, "close", closed.append)
        with authorization._open_posix_authorization(PurePosixPath("/intake/nested/approvals.json")) as descriptor:
            assert descriptor == 14
        assert [row[0] for row in calls] == ["/", "intake", "nested", "approvals.json"]
        assert [row[2] for row in calls] == [None, 11, 12, 13]
        assert all(flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC for _, flags, _ in calls)
        assert all(flags & os.O_DIRECTORY for _, flags, _ in calls[:-1])
        assert calls[-1][1] & os.O_NONBLOCK
        assert closed == [14, 13, 12, 11]


@pytest.mark.parametrize("failed_component", ["intake", "approvals.json"])
def test_posix_swap_failure_closes_pinned_directories(monkeypatch, failed_component):
    calls, closed = [], []

    def opened(name, flags, *, dir_fd=None):
        if name == failed_component:
            raise OSError("no-follow refusal")
        calls.append(name)
        return len(calls) + 10

    with monkeypatch.context() as scoped:
        for index, name in enumerate(("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")):
            scoped.setattr(os, name, 1 << (index + 8), raising=False)
        scoped.setattr(os, "open", opened)
        scoped.setattr(os, "supports_dir_fd", {opened})
        scoped.setattr(os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFDIR))
        scoped.setattr(os, "close", closed.append)
        with pytest.raises(OSError):
            with authorization._open_posix_authorization(PurePosixPath("/intake/approvals.json")):
                pytest.fail("no-follow failure must not proceed")
        assert closed == list(reversed(range(11, len(calls) + 11)))


@pytest.mark.parametrize("failure", [None, "ancestor_reparse", "leaf_reparse", "handle_conversion", "unsupported_api"])
def test_windows_relative_open_pins_checked_parents_and_closes_handles(monkeypatch, failure):
    calls, checks, closed, descriptor_closed = [], set(), [], []

    class FakeApi:
        def open_root(self, anchor):
            assert anchor == "C:\\"
            calls.append((anchor, True, None))
            return 10

        def open_child(self, parent, name, *, directory):
            assert parent in checks
            if failure == "unsupported_api":
                raise AttributeError("required NT API unavailable")
            calls.append((name, directory, parent))
            return len(calls) + 9

        def check(self, handle, *, directory):
            if (failure == "ancestor_reparse" and handle == 11
                    or failure == "leaf_reparse" and not directory):
                raise authorization.WooBatchAuthorizationError()
            checks.add(handle)

        def to_descriptor(self, handle):
            assert handle in checks
            if failure == "handle_conversion":
                raise OSError("conversion failed")
            assert handle == 12
            return 100

        def close(self, handle):
            closed.append(handle)

    with monkeypatch.context() as scoped:
        scoped.setattr(authorization, "_WindowsReadOnlyFiles", FakeApi)
        scoped.setattr(os, "close", descriptor_closed.append)
        if failure is None:
            with authorization._open_windows_authorization(PureWindowsPath("C:/intake/approvals.json")) as fd:
                assert fd == 100
            assert calls == [("C:\\", True, None), ("intake", True, 10), ("approvals.json", False, 11)]
            assert descriptor_closed == [100]
            assert closed == [11, 10]
        else:
            with pytest.raises((authorization.WooBatchAuthorizationError, OSError, AttributeError)):
                with authorization._open_windows_authorization(PureWindowsPath("C:/intake/approvals.json")):
                    pytest.fail("unsafe/unsupported Windows open must fail closed")
            assert descriptor_closed == []
            assert closed == list(reversed(range(10, len(calls) + 10)))


@pytest.mark.parametrize("path", ["//server/share/approvals.json", "//?/C:/approvals.json", "C:/intake/stream:approvals.json"])
def test_windows_rejects_unc_device_and_alternate_stream_paths(monkeypatch, path):
    factory = Mock(side_effect=AssertionError("must reject before native open"))
    monkeypatch.setattr(authorization, "_WindowsReadOnlyFiles", factory)
    with pytest.raises(authorization.WooBatchAuthorizationError):
        with authorization._open_windows_authorization(PureWindowsPath(path)):
            pytest.fail("unsafe namespace")
    factory.assert_not_called()


def test_windows_native_flags_are_existing_readonly_and_no_reparse(monkeypatch):
    import ctypes

    kernel = SimpleNamespace(CreateFileW=Mock(return_value=10), GetFileType=Mock(return_value=1),
                             GetFileInformationByHandle=Mock(), CloseHandle=Mock(return_value=True))
    nt_calls = []

    def native_open(out, access, attributes, status, allocation, file_attributes,
                    share, disposition, options, extended, extended_length):
        request = attributes._obj
        nt_calls.append((access, request.RootDirectory, request.Attributes,
                         ctypes.wstring_at(request.ObjectName.contents.Buffer), share, disposition, options))
        out._obj.value = 11
        return 0

    native = SimpleNamespace(NtCreateFile=Mock(side_effect=native_open))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else native, raising=False)
    api = authorization._WindowsReadOnlyFiles()
    assert api.open_root("C:\\") == 10
    assert kernel.CreateFileW.call_args.args == ("C:\\", 0x1000A0, 1, None, 3, 0x02200000, None)
    api.open_child(10, "intake", directory=True)
    api.open_child(11, "approvals.json", directory=False)
    assert nt_calls == [(0x1000A0, 10, 0x1040, "intake", 1, 1, 0x200021),
                        (0x80100000, 11, 0x1040, "approvals.json", 1, 1, 0x200060)]


@pytest.mark.parametrize("failure", ["reparse", "not_disk", "info_failed", "not_regular", "hardlink", "oversized", "empty"])
def test_windows_opened_handle_metadata_must_prove_safety(monkeypatch, failure):
    import ctypes

    def information(handle, pointer):
        info = pointer._obj
        info.attributes = 0x400 if failure == "reparse" else 0x10 if failure == "not_regular" else 0
        info.links = 2 if failure == "hardlink" else 1
        info.size_low = 0 if failure == "empty" else authorization.MAX_AUTHORIZATION_BYTES + 1 if failure == "oversized" else 10
        return failure != "info_failed"

    kernel = SimpleNamespace(CreateFileW=Mock(), CloseHandle=Mock(),
                             GetFileType=Mock(return_value=0 if failure == "not_disk" else 1),
                             GetFileInformationByHandle=Mock(side_effect=information))
    native = SimpleNamespace(NtCreateFile=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else native, raising=False)
    api = authorization._WindowsReadOnlyFiles()
    with pytest.raises(authorization.WooBatchAuthorizationError):
        api.check(10, directory=False)


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
    assert not calls & {"mkdir", "unlink", "remove", "write_bytes", "write_text", "create_product",
                        "read_bytes", "read_text", "_regular_unlinked_file"}
    assert not any(token in source for token in ("load_config(", "requests.", "httpx.", "urllib", "run_woo_batch("))
