"""Explicit Windows/Schannel exact-SKU GET backend, not a general HTTP client.

Only the snapshot CLI opts into this backend. System curl is locked and verified
offline before executing even a credential-free capability probe. File and
ancestor handles deny write/delete sharing throughout verification and execution.
CreateProcess still executes a pathname: this is not descriptor-bound execution
and cannot defend against a hostile administrator/kernel or DLL replacement by
an administrator. The protected system directory, ACL checks and retained handles
bound the ordinary replacement/configuration threat; no PATH fallback exists.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import json
import ntpath
import os
import platform
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from .security import basic_auth_headers
from .sku_dry_run import is_safe_sku
from .sku_policy import MAX_SKU_LENGTH
from .woocommerce_category_discovery import WooCategoryCredentials
from . import woocommerce_target_snapshot as snapshot


PROXY_ENVIRONMENT_VARIABLE = "WOO_SNAPSHOT_HTTP_PROXY"
METADATA_LIMIT = 16 * 1024
PARENT_TIMEOUT = 30
OFFLINE_TRUST_FLAGS = 0x1000 | 0x10 | 0x2000  # CACHE_ONLY, revocation NONE, no MD2/4
# Corruption protection, not a normal catalog-selection limit: thousands of
# catalogs for a single file hash are already far above realistic inbox counts.
_MAX_CATALOG_CANDIDATES = 4096
_FRAME_START = "WOO_SNAPSHOT_META_V1"
_FRAME_END = "END_WOO_SNAPSHOT_META_V1"
_FIELDS = ("status", "content_type", "total", "total_pages", "connect_status", "redirects", "retries")
_WRITE_OUT = (
    "%{stderr}" + _FRAME_START + "\nstatus=%{http_code}\n"
    "content_type=%{content_type}\ntotal=%header{x-wp-total}\n"
    "total_pages=%header{x-wp-totalpages}\nconnect_status=%{http_connect}\n"
    "redirects=%{num_redirects}\nretries=%{num_retries}\n" + _FRAME_END + "\n"
)
_TRUSTED_SIDS = frozenset({
    "S-1-5-18", "S-1-5-32-544",  # SYSTEM and Administrators
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",  # TrustedInstaller
})


def _configuration_failure(code: str) -> snapshot.WooTargetSnapshotConfigurationError:
    return snapshot.WooTargetSnapshotConfigurationError("woo_target_snapshot_curl_" + code)


def _clean_text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    ):
        raise _configuration_failure("config_invalid")
    return value


def validate_curl_snapshot_base_url(base_url: str) -> str:
    """Reuse staging authority validation, narrowing only this transport to an origin."""
    value = _clean_text(base_url)
    normalized = snapshot.validate_staging_target_base_url(value)
    try:
        parsed = urlsplit(value)
        if (
            parsed.path not in ("", "/") or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or "?" in value or "#" in value
        ):
            raise ValueError
    except Exception:
        raise _configuration_failure("origin_invalid") from None
    return normalized


@dataclass(frozen=True, slots=True)
class CurlSnapshotOptions:
    """Invocation-only non-secret proxy configuration; deliberately not report data."""

    proxy: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            value = _clean_text(self.proxy)
            parsed = urlsplit(value)
            if (
                parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or "?" in value or "#" in value
                or parsed.port is None or not 1 <= parsed.port <= 65535
            ):
                raise ValueError
            host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
            object.__setattr__(self, "proxy", f"http://{host}:{parsed.port}")
        except Exception:
            raise _configuration_failure("proxy_invalid") from None


def load_curl_snapshot_options(environ: Mapping[str, str] | None = None) -> CurlSnapshotOptions:
    """Process environment only; never dotenv, credential fields or generic proxies."""
    source = os.environ if environ is None else environ
    return CurlSnapshotOptions(source.get(PROXY_ENVIRONMENT_VARIABLE, ""))


class _GUID(ctypes.Structure):
    _fields_ = [("data1", w.DWORD), ("data2", w.WORD), ("data3", w.WORD), ("data4", w.BYTE * 8)]

    @classmethod
    def from_text(cls, value: str):
        return cls.from_buffer_copy(UUID(value).bytes_le)


class _TrustFile(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("path", w.LPCWSTR), ("handle", w.HANDLE), ("subject", ctypes.c_void_p)]


class _TrustCatalog(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD), ("version", w.DWORD), ("catalog", w.LPCWSTR),
        ("tag", w.LPCWSTR), ("path", w.LPCWSTR), ("handle", w.HANDLE),
        ("hash", ctypes.c_void_p), ("hash_size", w.DWORD),
        ("context", ctypes.c_void_p), ("admin", w.HANDLE),
    ]


class _TrustData(ctypes.Structure):
    # pInfo is the file/catalog union pointer; pSignatureSettings is Win8+.
    _fields_ = [
        ("size", w.DWORD), ("policy", ctypes.c_void_p), ("sip", ctypes.c_void_p),
        ("ui", w.DWORD), ("revocation", w.DWORD), ("choice", w.DWORD),
        ("info", ctypes.c_void_p), ("action", w.DWORD), ("state", w.HANDLE),
        ("url", w.LPCWSTR), ("flags", w.DWORD), ("ui_context", w.DWORD),
        ("signature_settings", ctypes.c_void_p),
    ]


class _CatalogInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("path", w.WCHAR * 260)]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]


def _dll(name: str):
    # System32-only DLL resolution, not the current directory or inherited PATH.
    return ctypes.WinDLL(name, use_last_error=True, winmode=0x800)


def _function(library, name: str, result, arguments: list):
    function = getattr(library, name)
    function.restype, function.argtypes = result, arguments
    return function


class _WindowsTrust:
    """Small native boundary, mockable without executing processes or reading keys."""

    def __init__(self) -> None:
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8 or platform.machine().casefold() not in {"amd64", "arm64"}:
            raise _configuration_failure("platform_unsupported")
        self.kernel = _dll("kernel32.dll")
        self.security = _dll("advapi32.dll")
        self.trust = _dll("wintrust.dll")
        self.close = _function(self.kernel, "CloseHandle", w.BOOL, [w.HANDLE])
        self.free = _function(self.kernel, "LocalFree", w.HANDLE, [w.HANDLE])
        wow = w.BOOL()
        current = _function(self.kernel, "GetCurrentProcess", w.HANDLE, [])
        if not _function(self.kernel, "IsWow64Process", w.BOOL, [w.HANDLE, ctypes.POINTER(w.BOOL)])(current(), ctypes.byref(wow)) or wow.value:
            raise _configuration_failure("platform_unsupported")

    def system_paths(self) -> tuple[str, str]:
        paths = []
        for name in ("GetSystemWindowsDirectoryW", "GetSystemDirectoryW"):
            buffer = ctypes.create_unicode_buffer(32768)
            length = _function(self.kernel, name, w.UINT, [w.LPWSTR, w.UINT])(buffer, len(buffer))
            if not 0 < length < len(buffer):
                raise _configuration_failure("system_path_invalid")
            paths.append(ntpath.normpath(buffer.value))
        windows, system = paths
        if (
            not ntpath.isabs(windows) or windows.startswith("\\\\")
            or ntpath.normcase(system) != ntpath.normcase(ntpath.join(windows, "System32"))
        ):
            raise _configuration_failure("system_path_invalid")
        return windows, system

    def open_protected(self, path: str, *, directory: bool):
        attributes = _function(self.kernel, "GetFileAttributesW", w.DWORD, [w.LPCWSTR])(path)
        if attributes == 0xFFFFFFFF or attributes & 0x400 or bool(attributes & 0x10) != directory:
            raise _configuration_failure("executable_path_unsafe")
        # FILE_SHARE_READ only: executable and every ancestor cannot be renamed,
        # deleted or opened for writing while our verification/execution holds them.
        create = _function(self.kernel, "CreateFileW", w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE])
        handle = create(path, 0x80000000 | 0x20000, 1, None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise _configuration_failure("executable_lock_failed")
        try:
            # Recheck via the retained handle to close attribute-check/open races.
            final = ctypes.create_unicode_buffer(32768)
            length = _function(self.kernel, "GetFinalPathNameByHandleW", w.DWORD, [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD])(handle, final, len(final), 0)
            resolved = final.value.removeprefix("\\\\?\\")
            if not 0 < length < len(final) or ntpath.normcase(ntpath.normpath(resolved)) != ntpath.normcase(ntpath.normpath(path)):
                raise _configuration_failure("executable_path_unsafe")
            # FILE_ATTRIBUTE_TAG_INFO (FileAttributeTagInfo=9) works for directories
            # and rejects a reparse point even if its final target happens to match.
            tag = (w.DWORD * 2)()
            info = _function(self.kernel, "GetFileInformationByHandleEx", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD])
            if not info(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)) or tag[0] & 0x400 or bool(tag[0] & 0x10) != directory:
                raise _configuration_failure("executable_path_unsafe")
            if _function(self.kernel, "GetFileType", w.DWORD, [w.HANDLE])(handle) != 1:
                raise _configuration_failure("executable_path_unsafe")
            self.check_acl(
                handle, directory=directory,
                drive_root=directory and ntpath.dirname(path) == path,
            )
            return handle
        except BaseException:
            self.close(handle)
            raise

    def _sid_text(self, sid) -> str:
        output = w.LPWSTR()
        convert = _function(self.security, "ConvertSidToStringSidW", w.BOOL, [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)])
        if not convert(sid, ctypes.byref(output)):
            raise _configuration_failure("executable_protection_invalid")
        try:
            return output.value
        finally:
            self.free(ctypes.cast(output, w.HANDLE))

    def check_acl(self, handle, *, directory: bool, drive_root: bool = False) -> None:
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        get = _function(self.security, "GetSecurityInfo", w.DWORD, [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p])
        result = get(handle, 1, 1 | 4, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        try:
            if result or not owner.value or not acl.value or self._sid_text(owner) not in _TRUSTED_SIDS:
                raise _configuration_failure("executable_protection_invalid")
            size = _AclSize()
            get_info = _function(self.security, "GetAclInformation", w.BOOL, [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_int])
            if not get_info(acl, ctypes.byref(size), ctypes.sizeof(size), 2) or size.count > 256:
                raise _configuration_failure("executable_protection_invalid")
            get_ace = _function(self.security, "GetAce", w.BOOL, [ctypes.c_void_p, w.DWORD, ctypes.c_void_p])
            # All objects: delete, owner/DACL changes and generic ALL/WRITE. For
            # directories reject DELETE_CHILD too; child creation alone at the
            # drive root does not permit replacing a locked, protected System32.
            dangerous = 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
            dangerous |= 0x10 | 0x100
            dangerous |= 0x40 if directory else 0
            if not drive_root:
                dangerous |= 0x2 | 0x4
            for index in range(size.count):
                ace = ctypes.c_void_p()
                if not get_ace(acl, index, ctypes.byref(ace)):
                    raise _configuration_failure("executable_protection_invalid")
                header = ctypes.string_at(ace, 4)
                if header[0] not in {0, 1} or int.from_bytes(header[2:4], "little") < 12:
                    raise _configuration_failure("executable_protection_invalid")
                if header[0] == 1 or header[1] & 0x8:  # deny / inherit-only ACE
                    continue
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                if mask & dangerous and self._sid_text(ace.value + 8) not in _TRUSTED_SIDS:
                    raise _configuration_failure("executable_protection_invalid")
        finally:
            if descriptor.value:
                self.free(descriptor)

    def _verify(self, info, choice: int) -> int:
        action = _GUID.from_text("00AAC56B-CD44-11d0-8CC2-00C04FC295EE")
        data = _TrustData()
        data.size, data.ui, data.revocation, data.choice = ctypes.sizeof(data), 2, 0, choice
        data.info = ctypes.cast(ctypes.pointer(info), ctypes.c_void_p)
        data.action, data.flags = 1, OFFLINE_TRUST_FLAGS
        verify = _function(self.trust, "WinVerifyTrust", w.LONG, [w.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(_TrustData)])
        try:
            return verify(ctypes.c_void_p(-1), ctypes.byref(action), ctypes.byref(data)) & 0xFFFFFFFF
        finally:
            data.action = 2  # WTD_STATEACTION_CLOSE, on success and failure
            verify(ctypes.c_void_p(-1), ctypes.byref(action), ctypes.byref(data))

    def verify_signature(self, path: str, handle) -> None:
        file = _TrustFile(ctypes.sizeof(_TrustFile), path, handle, None)
        status = self._verify(file, 1)
        if status == 0:
            return
        if status != 0x800B0100:  # TRUST_E_NOSIGNATURE; never mask a bad embedded signature
            raise _configuration_failure("signature_untrusted")
        # Windows inbox binaries may be catalog-signed. Compute their hash from
        # the same locked file, find a LOCAL catalog and verify it offline too.
        acquire = _function(self.trust, "CryptCATAdminAcquireContext2", w.BOOL, [ctypes.POINTER(w.HANDLE), ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p, w.DWORD])
        calculate = _function(self.trust, "CryptCATAdminCalcHashFromFileHandle2", w.BOOL, [w.HANDLE, w.HANDLE, ctypes.POINTER(w.DWORD), ctypes.c_void_p, w.DWORD])
        enumerate_catalog = _function(self.trust, "CryptCATAdminEnumCatalogFromHash", w.HANDLE, [w.HANDLE, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.HANDLE)])
        catalog_info = _function(self.trust, "CryptCATCatalogInfoFromContext", w.BOOL, [w.HANDLE, ctypes.POINTER(_CatalogInfo), w.DWORD])
        release_catalog = _function(self.trust, "CryptCATAdminReleaseCatalogContext", w.BOOL, [w.HANDLE, w.HANDLE, w.DWORD])
        release_admin = _function(self.trust, "CryptCATAdminReleaseContext", w.BOOL, [w.HANDLE, w.DWORD])
        for algorithm in ("SHA256", "SHA1"):
            admin, catalog = w.HANDLE(), None
            try:
                if not acquire(ctypes.byref(admin), None, algorithm, None, 0):
                    raise _configuration_failure("signature_untrusted")
                length = w.DWORD()
                if not calculate(admin, handle, ctypes.byref(length), None, 0) or not 0 < length.value <= 64:
                    raise _configuration_failure("signature_untrusted")
                digest = (w.BYTE * length.value)()
                if not calculate(admin, handle, ctypes.byref(length), digest, 0):
                    raise _configuration_failure("signature_untrusted")
                catalog = enumerate_catalog(admin, digest, length, 0, None)
                examined = 0
                while catalog:
                    if examined >= _MAX_CATALOG_CANDIDATES:
                        raise _configuration_failure("signature_untrusted")
                    examined += 1
                    info = _CatalogInfo()
                    info.size = ctypes.sizeof(info)
                    if not catalog_info(catalog, ctypes.byref(info), 0):
                        raise _configuration_failure("signature_untrusted")
                    member = _TrustCatalog()
                    member.size, member.catalog, member.tag = ctypes.sizeof(member), info.path, bytes(digest).hex().upper()
                    member.path, member.handle = path, handle
                    member.hash, member.hash_size, member.admin = ctypes.cast(digest, ctypes.c_void_p), length.value, admin
                    if self._verify(member, 2) == 0:
                        return
                    # EnumCatalogFromHash consumes/releases the previous context
                    # passed by pointer, including when it returns NULL. Do not
                    # release it ourselves before or after continuation. The new
                    # return value is the only context we still own. If the call
                    # raises before entering the native API, assignment has not
                    # occurred and finally still owns/cleans the previous one.
                    previous = w.HANDLE(catalog)
                    catalog = enumerate_catalog(admin, digest, length, 0, ctypes.byref(previous))
            finally:
                try:
                    if catalog:
                        release_catalog(admin, catalog, 0)
                finally:
                    if admin:
                        release_admin(admin, 0)
        raise _configuration_failure("signature_untrusted")


class _TrustedSystemCurl:
    def __init__(self) -> None:
        self._handles = []
        self._native = None
        try:
            self._native = _WindowsTrust()
            self.windows, self.directory = self._native.system_paths()
            self.path = ntpath.join(self.directory, "curl.exe")
            # Lock every ancestor from the drive root down (not just curl.exe).
            ancestors = []
            current = self.directory
            while True:
                ancestors.append(current)
                parent = ntpath.dirname(current)
                if parent == current:
                    break
                current = parent
            for ancestor in reversed(ancestors):
                self._handles.append(self._native.open_protected(ancestor, directory=True))
            handle = self._native.open_protected(self.path, directory=False)
            self._handles.append(handle)
            self._native.verify_signature(self.path, handle)
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            raise _configuration_failure("executable_untrusted") from None

    @property
    def child_environment(self) -> dict[str, str]:
        return {"SystemRoot": self.windows, "WINDIR": self.windows}

    def close(self) -> None:
        for handle in reversed(self._handles):
            self._native.close(handle)
        self._handles.clear()


def _bounded_process(
    executable: _TrustedSystemCurl,
    arguments: tuple[str, ...],
    input_bytes: bytes = b"",
    *,
    on_launch: Callable[[], None] | None = None,
    stdout_limit: int = snapshot.MAX_RESPONSE_BYTES,
) -> tuple[int, bytes, bytes]:
    """Direct process, bounded concurrent pipes, hard deadline, always reap/close."""
    try:
        process = subprocess.Popen(
            [executable.path, "-q", *arguments], shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=executable.child_environment, cwd=executable.directory,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_curl_launch_failed") from None
    output = [bytearray(), bytearray()]
    failures: list[str] = []
    lock = threading.Lock()

    def fail(code: str) -> None:
        with lock:
            if not failures:
                failures.append(code)
        try:
            process.kill()
        except OSError:
            pass

    def read(stream, index: int, limit: int) -> None:
        try:
            while chunk := stream.read(8192):
                if len(output[index]) + len(chunk) > limit:
                    fail("response_too_large" if index == 0 else "metadata_too_large")
                    return
                output[index].extend(chunk)
        except Exception:
            fail("pipe_failed")

    def write() -> None:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except Exception:
            # Never accept a successful result if the controlled config could
            # not be delivered completely; never print a thread traceback.
            fail("pipe_failed")

    threads = [
        threading.Thread(target=read, args=(process.stdout, 0, stdout_limit)),
        threading.Thread(target=read, args=(process.stderr, 1, METADATA_LIMIT)),
        threading.Thread(target=write),
    ]
    try:
        if on_launch is not None:
            on_launch()
        for thread in threads:
            thread.start()
        try:
            return_code = process.wait(timeout=PARENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            fail("timeout")
            return_code = process.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=3)
        if any(thread.is_alive() for thread in threads):
            fail("pipe_failed")
        if failures:
            if failures[0] == "timeout":
                raise snapshot.WooTargetSnapshotRetryableError("woo_target_snapshot_curl_timeout")
            raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_curl_" + failures[0])
        return return_code, bytes(output[0]), bytes(output[1])
    except snapshot.WooTargetSnapshotError:
        raise
    except Exception:
        raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_curl_process_failed") from None
    finally:
        cleanup_failed = False
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        except Exception:
            cleanup_failed = True
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except Exception:
                cleanup_failed = True
        for thread in threads:
            try:
                if thread.ident is not None:
                    thread.join(timeout=3)
                    cleanup_failed |= thread.is_alive()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_curl_cleanup_failed") from None


def _quote(value: str) -> str:
    return '"' + _clean_text(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _metadata_config() -> str:
    # Static template only: remote/user text is never used as curl format text.
    return 'write-out = "' + _WRITE_OUT.replace("\n", "\\n") + '"\n'


def _parse_metadata(raw: bytes) -> dict[str, str]:
    try:
        if len(raw) > METADATA_LIMIT:
            raise ValueError
        lines = raw.decode("ascii").splitlines()
        if len(lines) != len(_FIELDS) + 2 or lines[0] != _FRAME_START or lines[-1] != _FRAME_END:
            raise ValueError
        values = {}
        for field_name, line in zip(_FIELDS, lines[1:-1], strict=True):
            name, separator, value = line.partition("=")
            if name != field_name or not separator or len(value) > 256 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError
            values[name] = value
        if not re.fullmatch(r"\d{3}", values["status"]) or not re.fullmatch(r"\d{3}", values["connect_status"]):
            raise ValueError
        for name in ("total", "total_pages", "redirects", "retries"):
            if values[name] and not re.fullmatch(r"[0-9]{1,10}", values[name]):
                raise ValueError
        if values["redirects"] != "0" or values["retries"] != "0":
            raise ValueError
        return values
    except Exception:
        raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_curl_metadata_invalid") from None


def _validate_capabilities(executable: _TrustedSystemCurl) -> None:
    try:
        code, version, _ = _bounded_process(executable, ("--version",), stdout_limit=METADATA_LIMIT)
        text = version.decode("ascii")
        match = re.match(r"curl (\d+)\.(\d+)\.(\d+) .*\bSchannel\b", text)
        if code or not match or tuple(map(int, match.groups())) < (8, 9, 0) or not re.search(r"^Protocols:.*\bhttps\b", text, re.M):
            raise ValueError
        code, help_text, _ = _bounded_process(executable, ("--help", "all"), stdout_limit=64 * 1024)
        help_text = help_text.decode("ascii")
        required = ("--config", "--proxy", "--noproxy", "--proto", "--proto-redir", "--max-filesize", "--connect-timeout", "--max-time", "--write-out", "--request", "--retry", "--globoff")
        if code or not all(option in help_text for option in required):
            raise ValueError
        # file:// is explicitly DISALLOWED by proto=https, so this local probe
        # cannot retrieve a file or initiate a network connection. It exercises
        # the exact header/write-out variables, not a version-number assumption.
        config = 'silent\nproto = "=https"\nurl = "file:///__disabled_offline_capability_probe__"\n' + _metadata_config()
        code, body, metadata = _bounded_process(executable, ("--config", "-"), config.encode("utf-8"), stdout_limit=METADATA_LIMIT)
        parsed = _parse_metadata(metadata)
        if code != 1 or body or parsed["status"] != "000" or parsed["connect_status"] != "000" or parsed["total"] or parsed["total_pages"]:
            raise ValueError
    except Exception:
        raise _configuration_failure("capability_unsupported") from None


class CurlWooProductTargetTransport:
    """GET-only protocol implementation; close explicitly or use a context manager."""

    __slots__ = ("_base_url", "_authorization", "_options", "_executable", "_network_requests")

    def __init__(self, base_url: str, credentials: WooCategoryCredentials, *, options: CurlSnapshotOptions) -> None:
        self._network_requests = 0
        self._executable = None
        self._base_url = validate_curl_snapshot_base_url(base_url)
        if not isinstance(options, CurlSnapshotOptions) or not isinstance(credentials, WooCategoryCredentials):
            raise _configuration_failure("config_invalid")
        # Validate again at the trust boundary, including injected immutable options.
        self._options = CurlSnapshotOptions(options.proxy)
        key, secret = _clean_text(credentials.consumer_key), _clean_text(credentials.consumer_secret)
        if ":" in key:
            raise _configuration_failure("config_invalid")
        try:
            self._executable = _TrustedSystemCurl()
            _validate_capabilities(self._executable)
            self._authorization = basic_auth_headers(key, secret)["Authorization"]
        except BaseException:
            self.close()
            raise

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def network_requests_performed(self) -> int:
        return self._network_requests

    @property
    def write_requests_performed(self) -> int:
        return 0

    def close(self) -> None:
        if self._executable is not None:
            self._executable.close()
            self._executable = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _launched(self) -> None:
        self._network_requests += 1

    def get_products_by_sku(self, sku: str, *, page: int, per_page: int = snapshot.DEFAULT_PER_PAGE) -> snapshot.WooProductTargetPage:
        if not is_safe_sku(sku) or len(sku) > MAX_SKU_LENGTH:
            raise snapshot.WooTargetSnapshotConfigurationError("woo_target_snapshot_sku_invalid")
        if type(page) is not int or page <= 0 or type(per_page) is not int or per_page != snapshot.DEFAULT_PER_PAGE:
            raise snapshot.WooTargetSnapshotConfigurationError("woo_target_snapshot_pagination_invalid")
        if self._executable is None:
            raise _configuration_failure("transport_closed")
        url = self._base_url + snapshot.PRODUCT_ENDPOINT + "?" + urlencode({"sku": sku, "page": page, "per_page": per_page})
        config = (
            'silent\ngloboff\nrequest = "GET"\n'
            'proto = "=https"\nproto-redir = "=https"\n'
            'retry = 0\nconnect-timeout = 5\nmax-time = 25\nmax-filesize = 2000000\n'
            'noproxy = ""\noutput = "-"\n'
            'header = "Accept: application/json"\n'
            + "url = " + _quote(url) + "\nproxy = " + _quote(self._options.proxy)
            + "\nheader = " + _quote("Authorization: " + self._authorization) + "\n"
            + _metadata_config()
        ).encode("utf-8")
        if len(config) > 8192:
            raise _configuration_failure("config_invalid")
        code, body, raw_metadata = _bounded_process(self._executable, ("--config", "-"), config, on_launch=self._launched)
        metadata = _parse_metadata(raw_metadata)
        status = int(metadata["status"])
        if status in snapshot._RETRYABLE_HTTP_STATUSES:
            raise snapshot.WooTargetSnapshotRetryableError("woo_target_snapshot_transient_get_failure")
        if status and not 200 <= status < 300:
            raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_get_failed")
        if code in {5, 6, 7, 28, 52, 55, 56}:
            raise snapshot.WooTargetSnapshotRetryableError("woo_target_snapshot_transport_retryable")
        if code or not 200 <= status < 300:
            raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_curl_get_failed")
        try:
            items = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise snapshot.WooTargetSnapshotTransportError("woo_target_snapshot_response_json_invalid") from None
        return snapshot.WooProductTargetPage(
            items, snapshot._header_integer(metadata["total"] or None, "total"),
            snapshot._header_integer(metadata["total_pages"] or None, "total_pages"),
        )
