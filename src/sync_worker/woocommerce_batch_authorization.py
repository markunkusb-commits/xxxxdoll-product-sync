"""Strict, invocation-local manual approvals for normal batch execution.

Workspace values are comparison authority only. Every authorization value is
supplied by the operator, and the provider retains those immutable objects.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_batch_workspace as workspace
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor
from .woocommerce_batch_item_adapter import ManualAuthorizationContext
from .woocommerce_batch_runtime import BatchItemRuntime, BatchRuntime


POLICY_VERSION = "xxxxdoll-woo-batch-manual-authorization-v1"
AUTHORIZATION_INPUT_INVALID = "woo_batch_authorization_input_invalid"
AUTHORIZATION_BINDING_BLOCKED = "woo_batch_authorization_binding_blocked"
NORMAL_OPERATIONS = frozenset({"apply", "verify_receipt"})
MAX_AUTHORIZATION_BYTES = workspace.MAX_BATCH_REPORT_BYTES
_ROOT_FIELDS = frozenset({"policy_version", "batch_hash", "items"})
_ITEM_FIELDS = frozenset({"sequence", "sku", "confirmed_plan_hash", "allowed_operations"})


class WooBatchAuthorizationError(ValueError):
    """Fixed safe outcomes; input contents and paths never enter the message."""

    def __init__(self, *, blocked: bool = False) -> None:
        self.exit_code = 1 if blocked else 2
        self.result_code = AUTHORIZATION_BINDING_BLOCKED if blocked else AUTHORIZATION_INPUT_INVALID
        super().__init__(self.result_code)


def _safe_sku(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and not any(char.isspace() or ord(char) < 32 or char in "/\\:" for char in value)
        and Redactor().text(value) == value
        and REPORT_SECRET_SCAN_PATTERN.search(value) is None
    )


@dataclass(frozen=True, slots=True)
class BatchManualAuthorization:
    batch_hash: str
    items: tuple[ManualAuthorizationContext, ...]
    _by_sequence: Mapping[int, ManualAuthorizationContext] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_sequence", MappingProxyType({
            item.sequence: item for item in self.items
        }))

    def __call__(
        self, batch_hash: str, item: BatchItemRuntime, operation: str,
    ) -> ManualAuthorizationContext | None:
        approval = self._by_sequence.get(item.sequence)
        if (batch_hash != self.batch_hash or approval is None
                or approval.sku != item.sku or approval.confirmed_plan_hash != item.plan_hash
                or operation not in NORMAL_OPERATIONS):
            return None
        # Adapter checks the requested operation against the unchanged approval.
        return approval


def parse_woo_batch_authorization(value: object, runtime: BatchRuntime) -> BatchManualAuthorization:
    """Validate the entire input before making any approvals available."""
    if (not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS
            or value.get("policy_version") != POLICY_VERSION
            or type(value.get("batch_hash")) is not str
            or workspace._HASH_PATTERN.fullmatch(value["batch_hash"]) is None
            or not isinstance(value.get("items"), list)):
        raise WooBatchAuthorizationError()

    approvals = []
    seen = set()
    for entry in value["items"]:
        if not isinstance(entry, Mapping) or set(entry) != _ITEM_FIELDS:
            raise WooBatchAuthorizationError()
        sequence = entry["sequence"]
        operations = entry["allowed_operations"]
        if (type(sequence) is not int or sequence <= 0 or sequence in seen
                or not _safe_sku(entry["sku"])
                or type(entry["confirmed_plan_hash"]) is not str
                or workspace._HASH_PATTERN.fullmatch(entry["confirmed_plan_hash"]) is None
                or not isinstance(operations, list) or not operations
                or any(type(operation) is not str or operation not in NORMAL_OPERATIONS
                       for operation in operations)
                or len(operations) != len(set(operations))):
            raise WooBatchAuthorizationError()
        seen.add(sequence)
        approvals.append(ManualAuthorizationContext(
            batch_hash=value["batch_hash"], sequence=sequence, sku=entry["sku"],
            confirmed_plan_hash=entry["confirmed_plan_hash"],
            allowed_operations=frozenset(operations),
        ))

    expected = {item.sequence: item for item in runtime.items}
    if value["batch_hash"] != runtime.batch_hash or seen != set(expected):
        raise WooBatchAuthorizationError(blocked=True)
    for approval in approvals:
        item = expected[approval.sequence]
        if approval.sku != item.sku or approval.confirmed_plan_hash != item.plan_hash:
            raise WooBatchAuthorizationError(blocked=True)
    return BatchManualAuthorization(value["batch_hash"], tuple(approvals))


def _reject_nonfinite(value: str) -> None:
    raise WooBatchAuthorizationError()


def _checked_file_stat(descriptor: int) -> os.stat_result:
    """The descriptor, not a previous pathname check, is the read authority."""
    info = os.fstat(descriptor)
    if os.name == "nt" and not hasattr(info, "st_file_attributes"):
        raise WooBatchAuthorizationError()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not 0 < info.st_size <= MAX_AUTHORIZATION_BYTES):
        raise WooBatchAuthorizationError()
    return info


@contextmanager
def _open_posix_authorization(path: Path):
    # Anchor every lookup to an already-open directory; O_NOFOLLOW on just the
    # leaf would still allow a swapped ancestor symlink to redirect the open.
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")
    if (any(not getattr(os, flag, None) for flag in required)
            or os.open not in os.supports_dir_fd):
        raise WooBatchAuthorizationError()
    directories = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors = []
    try:
        parent = os.open(path.anchor, directories)
        descriptors.append(parent)
        if not stat.S_ISDIR(os.fstat(parent).st_mode):
            raise WooBatchAuthorizationError()
        for component in path.parts[1:-1]:
            parent = os.open(component, directories, dir_fd=parent)
            descriptors.append(parent)
            if not stat.S_ISDIR(os.fstat(parent).st_mode):
                raise WooBatchAuthorizationError()
        # Nonblocking avoids hanging on a FIFO swapped in before the type check.
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
        descriptors.append(descriptor)
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class _WindowsReadOnlyFiles:
    """Handle-relative NT opens, with no reparse traversal at any component.

    CreateFileW's OPEN_REPARSE_POINT alone protects only the last component.
    NtCreateFile with a pinned RootDirectory opens one child at a time instead.
    All opens are existing/read-only, non-inheritable, and deny write/delete
    sharing. Missing APIs or unsupported semantics fail closed, without fallback.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                        ("Buffer", ctypes.c_void_p)]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                        ("ObjectName", ctypes.POINTER(UnicodeString)), ("Attributes", wintypes.ULONG),
                        ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]

        class StatusUnion(ctypes.Union):
            _fields_ = [("Status", wintypes.LONG), ("Pointer", ctypes.c_void_p)]

        class IoStatus(ctypes.Structure):
            _fields_ = [("Status", StatusUnion), ("Information", ctypes.c_size_t)]

        class FileInfo(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                        ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                        ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                        ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                        ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

        self.ctypes = ctypes
        self.types = (UnicodeString, ObjectAttributes, IoStatus, FileInfo)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.nt = ctypes.WinDLL("ntdll", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel.GetFileType.restype = wintypes.DWORD
        self.kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInfo)]
        self.kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.nt.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                       ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatus),
                                       ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
                                       wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
        self.nt.NtCreateFile.restype = wintypes.LONG

    def open_root(self, anchor: str) -> int:
        # READ_ATTRIBUTES | TRAVERSE | SYNCHRONIZE; SHARE_READ; OPEN_EXISTING;
        # BACKUP_SEMANTICS | OPEN_REPARSE_POINT. UNC/device roots are not allowed.
        handle = self.kernel.CreateFileW(anchor, 0x1000A0, 1, None, 3, 0x02200000, None)
        if handle in (None, self.ctypes.c_void_p(-1).value):
            raise WooBatchAuthorizationError()
        return handle

    def open_child(self, parent: int, name: str, *, directory: bool) -> int:
        from ctypes import wintypes

        ctypes = self.ctypes
        UnicodeString, ObjectAttributes, IoStatus, _ = self.types
        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        if length > 65532:
            raise WooBatchAuthorizationError()
        text = UnicodeString(length, length + 2, ctypes.cast(buffer, ctypes.c_void_p))
        # CASE_INSENSITIVE | DONT_REPARSE, relative to the validated parent.
        attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent, ctypes.pointer(text),
                                      0x1040, None, None)
        handle, status = wintypes.HANDLE(), IoStatus()
        access = 0x1000A0 if directory else 0x80100000  # read only + SYNCHRONIZE
        options = 0x200020 | (1 if directory else 0x40)  # no-follow + synchronous + type
        result = self.nt.NtCreateFile(
            ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(status),
            None, 0, 1, 1, options, None, 0,  # SHARE_READ; FILE_OPEN (never create)
        )
        if result != 0 or not handle.value:
            if handle.value:
                self.close(handle.value)
            raise WooBatchAuthorizationError()
        return handle.value

    def check(self, handle: int, *, directory: bool) -> None:
        info = self.types[3]()
        if (self.kernel.GetFileType(handle) != 1
                or not self.kernel.GetFileInformationByHandle(handle, self.ctypes.byref(info))
                or info.attributes & 0x400
                or bool(info.attributes & 0x10) != directory):
            raise WooBatchAuthorizationError()
        if not directory and (info.links != 1
                              or not 0 < (info.size_high << 32 | info.size_low) <= MAX_AUTHORIZATION_BYTES):
            raise WooBatchAuthorizationError()

    def to_descriptor(self, handle: int) -> int:
        import msvcrt

        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)

    def close(self, handle: int) -> None:
        if not self.kernel.CloseHandle(handle):
            raise WooBatchAuthorizationError()


@contextmanager
def _open_windows_authorization(path: Path):
    if (len(path.drive) != 2 or not path.drive[0].isalpha() or path.drive[1] != ":"
            or path.anchor != path.drive + "\\"
            or any(not part or part in {".", ".."} or any(char in part for char in "/\\:")
                   for part in path.parts[1:])):
        raise WooBatchAuthorizationError()
    api = _WindowsReadOnlyFiles()
    handles = []
    descriptor = None
    try:
        parent = api.open_root(path.anchor)
        handles.append(parent)
        api.check(parent, directory=True)
        for index, component in enumerate(path.parts[1:], start=1):
            directory = index < len(path.parts) - 1
            child = api.open_child(parent, component, directory=directory)
            handles.append(child)
            api.check(child, directory=directory)
            parent = child
        descriptor = api.to_descriptor(handles[-1])
        handles.pop()  # descriptor now owns the same leaf handle, not a new open
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for handle in reversed(handles):
            api.close(handle)


@contextmanager
def _open_authorization(path: Path):
    if os.name == "nt":
        opener = _open_windows_authorization
    elif os.name == "posix":
        opener = _open_posix_authorization
    else:
        raise WooBatchAuthorizationError()
    with opener(path) as descriptor:
        yield descriptor


def _read_opened_authorization(descriptor: int) -> bytes:
    before = _checked_file_stat(descriptor)
    # One bounded read from the validated descriptor; fdopen does not resolve a
    # pathname. The secure-open context owns and closes the descriptor/handles.
    with os.fdopen(descriptor, "rb", closefd=False) as opened:
        raw = opened.read(MAX_AUTHORIZATION_BYTES + 1)
    after = _checked_file_stat(descriptor)
    fields = ("st_dev", "st_ino", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (any(getattr(before, field) != getattr(after, field) for field in fields)
            or len(raw) != before.st_size):
        raise WooBatchAuthorizationError()
    return raw


def load_woo_batch_authorization(path: Path, runtime: BatchRuntime) -> BatchManualAuthorization:
    """Read and decode one safe local approvals file, exactly once."""
    try:
        local = package_io._local_path(path, require_file=True)
        if local.is_relative_to(runtime.workspace_root):
            raise WooBatchAuthorizationError()
        with _open_authorization(local) as descriptor:
            raw = _read_opened_authorization(descriptor)
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=package_io._json_object_no_duplicates,
            parse_constant=_reject_nonfinite,
        )
        return parse_woo_batch_authorization(value, runtime)
    except WooBatchAuthorizationError:
        raise
    except Exception:
        raise WooBatchAuthorizationError() from None
