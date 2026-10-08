"""Mock-only process/Windows API tests. No system curl or credentials are used."""

from __future__ import annotations

import ctypes
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sync_worker import woocommerce_snapshot_curl_transport as curl
from sync_worker import woocommerce_target_snapshot as snapshot
from sync_worker.security import basic_auth_headers
from sync_worker.woo_category_binding import STAGING_EXPECTED_HOST
from sync_worker.woocommerce_category_discovery import WooCategoryCredentials


BASE = f"https://{STAGING_EXPECTED_HOST}"
SKU = "CLM-PRO-FD160CM-MERU"
CREDS = WooCategoryCredentials("ck_mock_only", "cs_mock_only")
PROXY = "http://127.0.0.1:26001"
AUTH = basic_auth_headers(CREDS.consumer_key, CREDS.consumer_secret)["Authorization"]


def frame(**overrides) -> bytes:
    values = dict(status="200", content_type="application/json", total="0", total_pages="0", connect_status="200", redirects="0", retries="0")
    values.update(overrides)
    return (curl._FRAME_START + "\n" + "\n".join(f"{key}={values[key]}" for key in curl._FIELDS) + "\n" + curl._FRAME_END + "\n").encode("ascii")


class FakeExecutable:
    path = r"C:\Windows\System32\curl.exe"
    directory = r"C:\Windows\System32"
    windows = r"C:\Windows"

    def __init__(self):
        self.closed = False

    @property
    def child_environment(self):
        return {"SystemRoot": self.windows, "WINDIR": self.windows}

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_real_process(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real process forbidden in mock tests")
    monkeypatch.setattr(curl.subprocess, "Popen", forbidden)


@pytest.fixture
def mocked_trust(monkeypatch):
    instances = []
    def trust():
        instance = FakeExecutable()
        instances.append(instance)
        return instance
    monkeypatch.setattr(curl, "_TrustedSystemCurl", trust)
    monkeypatch.setattr(curl, "_validate_capabilities", lambda _: None)
    return instances


def transport():
    return curl.CurlWooProductTargetTransport(BASE, CREDS, options=curl.CurlSnapshotOptions(PROXY))


class RecordingInput(io.BytesIO):
    def close(self):
        if not self.closed:
            self.written = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, body=b"[]", metadata=None, code=0, *, timeout=False):
        self.stdin = RecordingInput()
        self.stdout = io.BytesIO(body)
        self.stderr = io.BytesIO(frame() if metadata is None else metadata)
        self.code = code
        self.timeout = timeout
        self.returncode = None
        self.kills = 0
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.timeout:
            self.timeout = False
            raise curl.subprocess.TimeoutExpired("safe mock process", timeout)
        self.returncode = -9 if self.kills else self.code
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9


def process_mock(monkeypatch, process):
    calls = []
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process
    monkeypatch.setattr(curl.subprocess, "Popen", popen)
    return calls


@pytest.mark.parametrize("value, expected", [(PROXY, PROXY), ("http://[::1]:7890", "http://[::1]:7890"), ("HTTP://127.0.0.1:1", "http://127.0.0.1:1")])
def test_loopback_proxy_normalization(value, expected):
    options = curl.CurlSnapshotOptions(value)
    assert options.proxy == expected
    assert value not in repr(options)
    with pytest.raises((AttributeError, TypeError)):
        options.proxy = "http://127.0.0.1:2"


@pytest.mark.parametrize("value", [
    "", None, "https://127.0.0.1:7890", "socks5://127.0.0.1:7890", "http://localhost:7890",
    "http://example.invalid:7890", "http://127.0.0.2:7890", "http://user:password@127.0.0.1:7890",
    "http://127.0.0.1:7890/", "http://127.0.0.1:7890/path", "http://127.0.0.1:7890?secret=1",
    "http://127.0.0.1:7890#fragment", "http://127.0.0.1:7890?", "http://127.0.0.1:7890#",
    "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:-1", "http://127.0.0.1:65536",
    "http://127.0.0.1:abc", "http://127.0.0.1:7890\nheader=malicious", "http://127.0.0.1:7890\x00",
])
def test_unsafe_proxy_fails_closed_without_echo(value):
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError) as error:
        curl.CurlSnapshotOptions(value)
    assert str(error.value) == "woo_target_snapshot_curl_proxy_invalid"


def test_proxy_reads_only_dedicated_process_variable(monkeypatch):
    assert curl.load_curl_snapshot_options({curl.PROXY_ENVIRONMENT_VARIABLE: PROXY, "HTTPS_PROXY": "remote"}).proxy == PROXY
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
        curl.load_curl_snapshot_options({"HTTPS_PROXY": PROXY})
    monkeypatch.setenv(curl.PROXY_ENVIRONMENT_VARIABLE, PROXY)
    assert curl.load_curl_snapshot_options().proxy == PROXY


@pytest.mark.parametrize("base", [
    "https://xxxxdoll.com", "https://another.wpcomstaging.com", BASE.replace("https:", "http:"),
    BASE + ":444", BASE + "/other", BASE + "?consumer_key=secret", BASE + "#secret", BASE + "\n",
    "https://user:password@" + STAGING_EXPECTED_HOST,
    "https://@" + STAGING_EXPECTED_HOST, BASE + "?", BASE + "#", BASE + "//",
])
def test_origin_rejected_before_executable_or_network(base, monkeypatch):
    monkeypatch.setattr(curl, "_TrustedSystemCurl", lambda: pytest.fail("must not resolve"))
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
        curl.CurlWooProductTargetTransport(base, CREDS, options=curl.CurlSnapshotOptions(PROXY))


@pytest.mark.parametrize("key, secret", [("key\n--insecure", "secret"), ("key", "secret\r"), ("key\x00", "secret"), ("key", "secret\x1b"), ("key", "secret\x85"), ("key:another", "secret")])
def test_credential_controls_rejected_before_trust(key, secret, monkeypatch):
    monkeypatch.setattr(curl, "_TrustedSystemCurl", lambda: pytest.fail("must not resolve"))
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError) as error:
        curl.CurlWooProductTargetTransport(BASE, WooCategoryCredentials(key, secret), options=curl.CurlSnapshotOptions(PROXY))
    assert str(error.value) == "woo_target_snapshot_curl_config_invalid"


def test_fixed_process_argv_environment_stdin_and_cleanup(mocked_trust, monkeypatch):
    for key in ("WC_CONSUMER_KEY", "WC_CONSUMER_SECRET", "WOO_SNAPSHOT_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSLKEYLOGFILE", "CURL_HOME", "HOME", "PATH"):
        monkeypatch.setenv(key, "hostile-mock-value")
    process = FakeProcess()
    calls = process_mock(monkeypatch, process)
    with transport() as client:
        result = client.get_products_by_sku(SKU, page=1)
        assert result == snapshot.WooProductTargetPage([], 0, 0)
        assert client.network_requests_performed == 1
        assert client.write_requests_performed == 0
        assert not mocked_trust[0].closed  # lock held through child lifetime
    assert mocked_trust[0].closed
    argv, kwargs = calls[0]
    assert argv == [FakeExecutable.path, "-q", "--config", "-"]
    assert kwargs["shell"] is False
    assert kwargs["env"] == {"SystemRoot": FakeExecutable.windows, "WINDIR": FakeExecutable.windows}
    assert kwargs["cwd"] == FakeExecutable.directory
    assert all(secret not in repr((argv, kwargs)) for secret in (CREDS.consumer_key, CREDS.consumer_secret, AUTH, PROXY))
    data = process.stdin.written
    assert not data.startswith(b"\xef\xbb\xbf")
    config = data.decode("utf-8")
    assert config.count("Authorization: ") == 1 and AUTH in config
    assert CREDS.consumer_key not in config and CREDS.consumer_secret not in config
    assert 'request = "GET"' in config
    assert f'url = "{BASE}/wp-json/wc/v3/products?sku={SKU}&page=1&per_page=100"' in config
    assert f'proxy = "{PROXY}"' in config
    assert 'noproxy = ""' in config
    assert "retry = 0" in config and "connect-timeout = 5" in config and "max-time = 25" in config
    assert 'proto = "=https"' in config and 'proto-redir = "=https"' in config
    assert all(word not in config for word in ("location", "cookie", "netrc", "trace", "insecure", "cert =", "consumer_key=", "consumer_secret="))
    assert process.waits[0] == curl.PARENT_TIMEOUT
    assert process.poll() is not None
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
    assert not any(hasattr(client, method) for method in ("post", "put", "patch", "delete", "request"))


@pytest.mark.parametrize("sku, page, per_page", [("", 1, 100), ("ALL&foo=bar", 1, 100), ("SKU\n", 1, 100), (SKU, 0, 100), (SKU, -1, 100), (SKU, True, 100), (SKU, "1", 100), (SKU, 1, 99), (SKU, 1, True), (SKU, 1, 100.0)])
def test_request_validation_has_zero_attempts(mocked_trust, sku, page, per_page):
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
            client.get_products_by_sku(sku, page=page, per_page=per_page)
        assert client.network_requests_performed == client.write_requests_performed == 0


@pytest.mark.parametrize("status, error_type", [(401, snapshot.WooTargetSnapshotTransportError), (403, snapshot.WooTargetSnapshotTransportError), (301, snapshot.WooTargetSnapshotTransportError), (429, snapshot.WooTargetSnapshotRetryableError), (500, snapshot.WooTargetSnapshotRetryableError), (502, snapshot.WooTargetSnapshotRetryableError), (503, snapshot.WooTargetSnapshotRetryableError), (504, snapshot.WooTargetSnapshotRetryableError)])
def test_http_failure_safe_taxonomy(mocked_trust, monkeypatch, status, error_type, caplog):
    process_mock(monkeypatch, FakeProcess(b"<html>Checking your browser...ck_mock_only cs_mock_only</html>", frame(status=str(status), content_type="text/html", total="", total_pages="")))
    with transport() as client:
        with pytest.raises(error_type) as error:
            client.get_products_by_sku(SKU, page=1)
        assert type(error.value) is error_type
        assert client.network_requests_performed == 1 and client.write_requests_performed == 0
    assert all(token not in str(error.value) + repr(error.value) + caplog.text for token in ("Checking", CREDS.consumer_key, CREDS.consumer_secret, PROXY, AUTH))


@pytest.mark.parametrize("body", [b"not json secret", b"\xff", b"[truncated", b"<html>challenge</html>"])
def test_invalid_json_has_fixed_safe_error(mocked_trust, monkeypatch, body):
    process_mock(monkeypatch, FakeProcess(body))
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match="^woo_target_snapshot_response_json_invalid$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1


@pytest.mark.parametrize("metadata", [
    b"", b"unframed secret", frame() + frame(), frame().replace(b"META_V1", b"META_V2"),
    frame().replace(b"status=200", b"status=200\nstatus=200"), frame().replace(b"status=200", b"unknown=200"),
    frame().replace(b"status=200", b"status=oops"), frame(content_type="x" * 257),
    frame(total="-1"), frame(total="1.0"), frame(redirects="1"), frame(retries="1"),
    frame().replace(b"content_type=application/json", b"content_type=\xff"),
    frame().replace(b"connect_status=200", b"connect_status=bad"),
])
def test_malformed_metadata_fails_closed(mocked_trust, monkeypatch, metadata):
    process_mock(monkeypatch, FakeProcess(metadata=metadata))
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match="^woo_target_snapshot_curl_metadata_invalid$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1 and client.write_requests_performed == 0


@pytest.mark.parametrize("field", ["total", "total_pages"])
def test_missing_required_totals_reuses_current_contract(mocked_trust, monkeypatch, field):
    process_mock(monkeypatch, FakeProcess(metadata=frame(**{field: ""})))
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match=f"^woo_target_snapshot_{field}_header_missing$"):
            client.get_products_by_sku(SKU, page=1)


@pytest.mark.parametrize("code, retryable", [(7, True), (28, True), (56, True), (60, False), (63, False)])
def test_launched_failure_conservatively_counts_one(mocked_trust, monkeypatch, code, retryable):
    process_mock(monkeypatch, FakeProcess(metadata=frame(status="000", connect_status="000", total="", total_pages=""), code=code))
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError) as error:
            client.get_products_by_sku(SKU, page=1)
        assert isinstance(error.value, snapshot.WooTargetSnapshotRetryableError) is retryable
        assert client.network_requests_performed == 1 and client.write_requests_performed == 0


def test_launch_failure_is_zero_and_safe(mocked_trust, monkeypatch):
    def failure(*args, **kwargs):
        raise OSError("ck_mock_only cs_mock_only Authorization " + PROXY)
    monkeypatch.setattr(curl.subprocess, "Popen", failure)
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match="^woo_target_snapshot_curl_launch_failed$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == client.write_requests_performed == 0


def test_timeout_kills_reaps_closes_and_retains_attempt(mocked_trust, monkeypatch):
    process = FakeProcess(timeout=True)
    process_mock(monkeypatch, process)
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotRetryableError, match="^woo_target_snapshot_curl_timeout$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1 and client.write_requests_performed == 0
    assert process.kills >= 1 and process.poll() is not None
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
    assert len(process.waits) >= 2


@pytest.mark.parametrize("stream, limit, code", [("stdout", snapshot.MAX_RESPONSE_BYTES, "response_too_large"), ("stderr", curl.METADATA_LIMIT, "metadata_too_large")])
def test_parent_output_bounds_terminate_without_propagating_data(mocked_trust, monkeypatch, stream, limit, code):
    process = FakeProcess()
    setattr(process, stream, io.BytesIO(b"x" * (limit + 1)))
    process_mock(monkeypatch, process)
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match=f"^woo_target_snapshot_curl_{code}$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1 and client.write_requests_performed == 0
    assert process.kills and all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


def test_closed_transport_never_relaunches(mocked_trust):
    client = transport()
    client.close()
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="transport_closed"):
        client.get_products_by_sku(SKU, page=1)
    assert client.network_requests_performed == 0


@pytest.mark.parametrize("base", [BASE, BASE + "/", BASE + ":443", BASE.upper()])
def test_exact_staging_origin_allowed(mocked_trust, base):
    with curl.CurlWooProductTargetTransport(base, CREDS, options=curl.CurlSnapshotOptions(PROXY)) as client:
        assert client.base_url == snapshot.validate_staging_target_base_url(base)
        assert client.network_requests_performed == 0


def test_pipe_read_error_reaps_and_never_echoes_remote_data(mocked_trust, monkeypatch):
    class BrokenPipe(io.BytesIO):
        def read(self, size):
            raise OSError("Authorization ck_mock_only " + PROXY)
    process = FakeProcess()
    process.stderr = BrokenPipe()
    process_mock(monkeypatch, process)
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match="^woo_target_snapshot_curl_pipe_failed$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1
    assert process.kills and all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


def test_cleanup_closes_all_pipes_even_if_wait_fails(mocked_trust, monkeypatch):
    process = FakeProcess()
    def broken_wait(timeout):
        raise OSError("secret child failure")
    process.wait = broken_wait
    process_mock(monkeypatch, process)
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match="^woo_target_snapshot_curl_cleanup_failed$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1
    assert process.kills and all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("target_sku", [SKU, "CLM-ULTRA-SIQ157CM-MIKO"])
def test_explicit_curl_snapshot_preserves_schema_package_hash_and_no_proxy(tmp_path, mocked_trust, monkeypatch, target_sku):
    from tests.test_woocommerce_target_snapshot import package_report
    import hashlib
    path = tmp_path / "authority.json"
    package = package_report()
    package["target_sku"] = target_sku
    package["future_woo_payload"]["sku"] = target_sku
    raw = json.dumps(package, indent=2).encode("utf-8")
    path.write_bytes(raw)
    process_mock(monkeypatch, FakeProcess())
    with transport() as client:
        report, output = snapshot.run_woo_target_snapshot(path, BASE, CREDS, project_root=tmp_path, transport=client, max_retries=0)
    assert report["source_package"] == {"basename": path.name, "sha256": hashlib.sha256(raw).hexdigest()}
    assert report["sku"] == target_sku and report["create_eligible"] is True
    assert report["network_requests_performed"] == 1 and report["write_requests_performed"] == 0
    assert report["write_authorized"] is False and report["policy_version"] == snapshot.POLICY_VERSION
    assert set(report) == {"status", "policy_version", "target", "sku", "match_count", "create_eligible", "existing_target", "source_package", "blocking_issues", "write_authorized", "network_requests_performed", "woocommerce_requests_performed", "woocommerce_write_requests_performed", "wordpress_requests_performed", "external_write_requests_performed", "write_requests_performed"}
    text = output.read_text(encoding="utf-8")
    assert all(value not in text for value in (PROXY, AUTH, CREDS.consumer_key, CREDS.consumer_secret, "https://", "Authorization", "Cookie"))


def test_canonical_zero_retries_does_not_retry_curl_429(tmp_path, mocked_trust, monkeypatch):
    from tests.test_woocommerce_target_snapshot import write_package
    calls = process_mock(monkeypatch, FakeProcess(metadata=frame(status="429")))
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotRetryableError):
            snapshot.run_woo_target_snapshot(write_package(tmp_path), BASE, CREDS, project_root=tmp_path, transport=client, max_retries=0)
        assert client.network_requests_performed == 1
    assert len(calls) == 1
    assert not (tmp_path / "reports" / snapshot.REPORT_FILENAME).exists()


def test_non_windows_and_unsupported_architecture_fail_closed(monkeypatch):
    monkeypatch.setattr(curl, "os", SimpleNamespace(name="posix"))
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="platform_unsupported"):
        curl._WindowsTrust()


def test_unsupported_architecture_no_dll_loading(monkeypatch):
    monkeypatch.setattr(curl.platform, "machine", lambda: "untrusted-architecture")
    monkeypatch.setattr(curl, "_dll", lambda _: pytest.fail("no native calls"))
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="platform_unsupported"):
        curl._WindowsTrust()


class FakeWindowsTrust:
    def __init__(self):
        self.opened = []
        self.closed = []
        self.signatures = []

    def system_paths(self):
        return r"C:\Windows", r"C:\Windows\System32"

    def open_protected(self, path, *, directory):
        self.opened.append((path, directory))
        return len(self.opened)

    def verify_signature(self, path, handle):
        self.signatures.append((path, handle))

    def close(self, handle):
        self.closed.append(handle)


def test_trusted_os_path_ignores_environment_and_locks_ancestors(monkeypatch):
    native = FakeWindowsTrust()
    monkeypatch.setenv("SystemRoot", r"D:\Hostile")
    monkeypatch.setenv("PATH", r"D:\Hostile")
    monkeypatch.setattr(curl, "_WindowsTrust", lambda: native)
    executable = curl._TrustedSystemCurl()
    assert executable.path == FakeExecutable.path
    assert native.opened == [("C:\\", True), (r"C:\Windows", True), (r"C:\Windows\System32", True), (FakeExecutable.path, False)]
    assert native.signatures == [(FakeExecutable.path, 4)]
    assert native.closed == []
    executable.close()
    assert native.closed == [4, 3, 2, 1]


@pytest.mark.parametrize("failure", ["missing", "reparse", "ACL", "signature"])
def test_trust_failure_closes_all_locks_before_credentials(failure, monkeypatch):
    native = FakeWindowsTrust()
    original = native.open_protected
    def open_file(path, *, directory):
        if not directory and failure != "signature":
            raise OSError(failure)
        return original(path, directory=directory)
    def signature(*args):
        raise ValueError("signature")
    native.open_protected = open_file
    if failure == "signature":
        native.verify_signature = signature
    monkeypatch.setattr(curl, "_WindowsTrust", lambda: native)
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="^woo_target_snapshot_curl_executable_untrusted$"):
        curl._TrustedSystemCurl()
    assert native.closed == list(range(len(native.opened), 0, -1))


@pytest.mark.parametrize("attributes", [0xFFFFFFFF, 0x400, 0x10])
def test_native_missing_reparse_or_nonregular_file_rejected(attributes, monkeypatch):
    native = object.__new__(curl._WindowsTrust)
    native.kernel = object()
    monkeypatch.setattr(curl, "_function", lambda library, name, *args: (lambda path: attributes) if name == "GetFileAttributesW" else pytest.fail("must not open"))
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="executable_path_unsafe"):
        native.open_protected(FakeExecutable.path, directory=False)


@pytest.mark.parametrize("resolved, tag, pass_expected", [(FakeExecutable.path, 0, True), (r"C:\Hostile\curl.exe", 0, False), (FakeExecutable.path, 0x400, False)])
def test_native_handle_sharing_and_post_open_identity(resolved, tag, pass_expected, monkeypatch):
    native = object.__new__(curl._WindowsTrust)
    native.kernel = object()
    native.check_acl = lambda handle, **kwargs: None
    closed, created = [], []
    native.close = closed.append
    def create(*args):
        created.append(args)
        return 77
    def final(handle, buffer, size, flags):
        buffer.value = "\\\\?\\" + resolved
        return len(buffer.value)
    def info(handle, kind, buffer, size):
        ctypes.cast(buffer, ctypes.POINTER(curl.w.DWORD))[0] = tag
        return True
    functions = {"GetFileAttributesW": lambda _: 0, "CreateFileW": create, "GetFinalPathNameByHandleW": final, "GetFileInformationByHandleEx": info, "GetFileType": lambda _: 1}
    monkeypatch.setattr(curl, "_function", lambda library, name, *args: functions[name])
    if pass_expected:
        assert native.open_protected(FakeExecutable.path, directory=False) == 77
        assert not closed
    else:
        with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
            native.open_protected(FakeExecutable.path, directory=False)
        assert closed == [77]
    assert created[0][2] == 1  # FILE_SHARE_READ, no WRITE/DELETE sharing
    assert created[0][5] & 0x00200000  # OPEN_REPARSE_POINT


@pytest.mark.parametrize("status", [0, 0x800B0109])
def test_authenticode_verification_offline_flags_handle_and_state_close(status, monkeypatch):
    native = object.__new__(curl._WindowsTrust)
    native.trust = object()
    calls = []
    def verify(window, action, pointer):
        data = ctypes.cast(pointer, ctypes.POINTER(curl._TrustData)).contents
        file = ctypes.cast(data.info, ctypes.POINTER(curl._TrustFile)).contents
        calls.append((data.flags, data.ui, data.revocation, data.choice, data.action, file.path, file.handle))
        return status
    monkeypatch.setattr(curl, "_function", lambda *args: verify)
    if status:
        with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="signature_untrusted"):
            native.verify_signature(FakeExecutable.path, 77)
    else:
        native.verify_signature(FakeExecutable.path, 77)
    assert len(calls) == 2
    assert [row[4] for row in calls] == [1, 2]
    assert all(row[0] & 0x1000 and row[0] & 0x10 and row[2] == 0 and row[1] == 2 for row in calls)
    assert all(row[-2:] == (FakeExecutable.path, 77) for row in calls)


def test_catalog_signed_inbox_executable_uses_same_locked_member(monkeypatch):
    native = object.__new__(curl._WindowsTrust)
    native.trust = object()
    verified, released = [], []
    def verify(info, choice):
        verified.append((choice, info.handle, info.path))
        if choice == 2:
            assert info.hash_size == 32 and info.tag == "01" * 32
            assert info.catalog == r"C:\Windows\System32\CatRoot\mock.cat"
            return 0
        return 0x800B0100
    native._verify = verify
    def acquire(pointer, *args):
        ctypes.cast(pointer, ctypes.POINTER(curl.w.HANDLE))[0] = 88
        assert args[1] == "SHA256"
        return True
    def calculate(admin, handle, length, buffer, flags):
        assert handle == 77
        ctypes.cast(length, ctypes.POINTER(curl.w.DWORD))[0] = 32
        if buffer is not None:
            for index in range(32):
                buffer[index] = 1
        return True
    def info(catalog, pointer, flags):
        ctypes.cast(pointer, ctypes.POINTER(curl._CatalogInfo)).contents.path = r"C:\Windows\System32\CatRoot\mock.cat"
        return True
    functions = {"CryptCATAdminAcquireContext2": acquire, "CryptCATAdminCalcHashFromFileHandle2": calculate, "CryptCATAdminEnumCatalogFromHash": lambda *a: 99, "CryptCATCatalogInfoFromContext": info, "CryptCATAdminReleaseCatalogContext": lambda *a: released.append("catalog"), "CryptCATAdminReleaseContext": lambda *a: released.append("admin")}
    monkeypatch.setattr(curl, "_function", lambda library, name, *args: functions[name])
    native.verify_signature(FakeExecutable.path, 77)
    assert verified == [(1, 77, FakeExecutable.path), (2, 77, FakeExecutable.path)]
    assert released == ["catalog", "admin"]


@pytest.mark.parametrize("owner, mask, accepted", [("S-1-5-18", 0x10000, False), ("S-1-5-18", 0x120089, True), ("S-1-5-21-untrusted", 0, False)])
def test_protection_rejects_untrusted_owner_or_write_ace(owner, mask, accepted, monkeypatch):
    native = object.__new__(curl._WindowsTrust)
    native.security = object()
    freed = []
    native.free = freed.append
    # An untrusted allow ACE; valid owner alone must not establish protection.
    ace = ctypes.create_string_buffer(b"\x00\x00\x14\x00" + mask.to_bytes(4, "little") + b"\x00" * 12)
    def security(handle, kind, bits, owner_pointer, group, acl_pointer, sacl, descriptor):
        ctypes.cast(owner_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 100
        ctypes.cast(acl_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 200
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 300
        return 0
    def acl_info(acl, pointer, size, kind):
        ctypes.cast(pointer, ctypes.POINTER(curl._AclSize)).contents.count = 1
        return True
    def get_ace(acl, index, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(ace)
        return True
    native._sid_text = lambda value: owner if getattr(value, "value", value) == 100 else "S-1-5-21-untrusted"
    functions = {"GetSecurityInfo": security, "GetAclInformation": acl_info, "GetAce": get_ace}
    monkeypatch.setattr(curl, "_function", lambda library, name, *args: functions[name])
    if accepted:
        native.check_acl(77, directory=False)
    else:
        with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="executable_protection_invalid"):
            native.check_acl(77, directory=False)
    assert freed


@pytest.mark.parametrize("bad_stage", ["non_schannel", "old_version", "no_https", "missing_option", "missing_frame"])
def test_capability_failures_have_zero_gets(bad_stage, monkeypatch):
    calls = []
    version = b"curl 8.21.0 (Windows) libcurl/8.21.0 Schannel\nProtocols: http https\n"
    if bad_stage == "non_schannel":
        version = version.replace(b"Schannel", b"OpenSSL")
    elif bad_stage == "old_version":
        version = version.replace(b"8.21.0", b"8.8.0")
    elif bad_stage == "no_https":
        version = version.replace(b"http https", b"http")
    help_text = b"--config --proxy --noproxy --proto --proto-redir --max-filesize --connect-timeout --max-time --write-out --request --retry --globoff"
    if bad_stage == "missing_option":
        help_text = help_text.replace(b"--max-filesize", b"missing")
    def runner(executable, arguments, input_bytes=b"", **kwargs):
        calls.append((arguments, input_bytes, kwargs))
        if arguments == ("--version",):
            return 0, version, b""
        if arguments == ("--help", "all"):
            return 0, help_text, b""
        return 1, b"", b"" if bad_stage == "missing_frame" else frame(status="000", connect_status="000", total="", total_pages="")
    monkeypatch.setattr(curl, "_bounded_process", runner)
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError, match="capability_unsupported"):
        curl._validate_capabilities(FakeExecutable())
    assert all("on_launch" not in row[2] for row in calls)


def test_capability_probe_is_local_protocol_disabled_and_no_auth(monkeypatch):
    calls = []
    def runner(executable, arguments, input_bytes=b"", **kwargs):
        calls.append((arguments, input_bytes, kwargs))
        if arguments == ("--version",):
            return 0, b"curl 8.21.0 (Windows) libcurl/8.21.0 Schannel\nProtocols: http https\n", b""
        if arguments == ("--help", "all"):
            return 0, b"--config --proxy --noproxy --proto --proto-redir --max-filesize --connect-timeout --max-time --write-out --request --retry --globoff", b""
        return 1, b"", frame(status="000", connect_status="000", total="", total_pages="")
    monkeypatch.setattr(curl, "_bounded_process", runner)
    curl._validate_capabilities(FakeExecutable())
    assert len(calls) == 3
    config = calls[-1][1]
    assert b'proto = "=https"' in config and b"file:///__disabled_offline_capability_probe__" in config
    assert b"Authorization" not in config and b"proxy =" not in config
    assert all("on_launch" not in row[2] for row in calls)


def test_capability_failure_releases_executable_locks(monkeypatch):
    executable = FakeExecutable()
    monkeypatch.setattr(curl, "_TrustedSystemCurl", lambda: executable)
    def bad(*args):
        raise snapshot.WooTargetSnapshotConfigurationError("woo_target_snapshot_curl_capability_unsupported")
    monkeypatch.setattr(curl, "_validate_capabilities", bad)
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
        transport()
    assert executable.closed


@pytest.mark.parametrize("error_type", [BrokenPipeError, RuntimeError])
def test_stdin_failure_is_safe_and_reaped(mocked_trust, monkeypatch, error_type, caplog):
    class BrokenInput(RecordingInput):
        def write(self, data):
            raise error_type("Authorization " + AUTH + " " + PROXY)
    process = FakeProcess()
    process.stdin = BrokenInput()
    process_mock(monkeypatch, process)
    with transport() as client:
        with pytest.raises(snapshot.WooTargetSnapshotTransportError, match="^woo_target_snapshot_curl_pipe_failed$"):
            client.get_products_by_sku(SKU, page=1)
        assert client.network_requests_performed == 1 and client.write_requests_performed == 0
    assert process.kills and all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
    assert AUTH not in caplog.text and PROXY not in caplog.text


def test_transport_opens_no_credential_or_config_files(mocked_trust, monkeypatch):
    import builtins
    process_mock(monkeypatch, FakeProcess())
    with transport() as client:
        def no_files(*args, **kwargs):
            pytest.fail("transport must never open credential/config files")
        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", no_files)
            patch.setattr(curl.os, "open", no_files)
            assert client.get_products_by_sku(SKU, page=1).items == []


def test_interruption_during_capability_check_closes_locked_executable(monkeypatch):
    executable = FakeExecutable()
    monkeypatch.setattr(curl, "_TrustedSystemCurl", lambda: executable)
    def interrupted(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(curl, "_validate_capabilities", interrupted)
    with pytest.raises(KeyboardInterrupt):
        transport()
    assert executable.closed
