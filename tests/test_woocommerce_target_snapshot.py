from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli
from sync_worker import woocommerce_target_snapshot as snapshot
from sync_worker.single_product_staging_package import (
    POLICY_VERSION as PACKAGE_POLICY_VERSION,
)
from sync_worker.woo_category_binding import STAGING_EXPECTED_HOST
from sync_worker.woocommerce_category_discovery import WooCategoryCredentials


SKU = "CLM-PRO-FD160CM-MERU"
BASE_URL = f"https://{STAGING_EXPECTED_HOST}"
CREDENTIALS = WooCategoryCredentials("ck_test_value", "cs_test_value")


def package_report() -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": PACKAGE_POLICY_VERSION,
        "target_sku": SKU,
        "future_woo_payload": {
            "name": "Safe Product",
            "sku": SKU,
            "status": "draft",
            "type": "simple",
        },
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


def product(
    product_id: object = 123,
    sku: object = SKU,
    *,
    product_type: object = "simple",
    status: object = "draft",
) -> dict[str, object]:
    return {
        "id": product_id,
        "sku": sku,
        "type": product_type,
        "status": status,
        "permalink": "https://forbidden.example/product",
    }


def page(
    items: object,
    *,
    total: int | None = None,
    total_pages: int | None = None,
) -> snapshot.WooProductTargetPage:
    count = len(items) if isinstance(items, list) else 0
    if total is None:
        total = count
    if total_pages is None:
        total_pages = 1 if count else 0
    return snapshot.WooProductTargetPage(items, total, total_pages)


class FakeTransport:
    def __init__(
        self,
        responses: dict[int, object],
        *,
        base_url: str = BASE_URL,
        write_requests: int = 0,
    ) -> None:
        self.base_url = base_url
        self.responses = responses
        self.calls: list[tuple[str, int, int]] = []
        self.network_requests_performed = 0
        self.write_requests_performed = write_requests

    def get_products_by_sku(
        self,
        sku: str,
        *,
        page: int,
        per_page: int = snapshot.DEFAULT_PER_PAGE,
    ) -> snapshot.WooProductTargetPage:
        self.calls.append((sku, page, per_page))
        self.network_requests_performed += 1
        response = self.responses[page]
        if isinstance(response, list):
            if not response:
                raise AssertionError("response sequence exhausted")
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, snapshot.WooProductTargetPage)
        return response


def write_package(
    root: Path,
    value: dict[str, object] | None = None,
    *,
    name: str = "single-product-staging-package.json",
) -> Path:
    path = root / name
    path.write_text(
        json.dumps(value or package_report(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def run(
    root: Path,
    transport: FakeTransport,
    value: dict[str, object] | None = None,
) -> tuple[dict[str, object], Path]:
    package_path = write_package(root, value)
    return snapshot.run_woo_target_snapshot(
        package_path,
        BASE_URL,
        None,
        project_root=root,
        transport=transport,
        sleeper=lambda _: None,
    )


def test_cli_command_is_registered_and_has_no_sku_argument():
    arguments = cli.build_parser().parse_args(
        [
            "snapshot-woo-target",
            "--package-report",
            "reports/single-product-staging-package.json",
            "--base-url",
            BASE_URL,
        ]
    )
    assert arguments.command == "snapshot-woo-target"
    assert arguments.package_report_path.name == "single-product-staging-package.json"
    assert not hasattr(arguments, "sku")


def test_valid_poc02_package_is_accepted():
    assert snapshot.validate_package_report(package_report()) == SKU


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "blocked"),
        ("policy_version", "old-policy"),
        ("write_authorized", True),
        ("blocking_issues", ["blocked"]),
        ("woocommerce_write_requests_performed", 1),
        ("wordpress_requests_performed", 1),
        ("external_write_requests_performed", 1),
        ("write_requests_performed", 1),
    ],
)
def test_ineligible_package_root_contract_is_rejected(field, value):
    value_map = package_report()
    value_map[field] = value
    with pytest.raises(snapshot.WooTargetSnapshotInputError):
        snapshot.validate_package_report(value_map)


@pytest.mark.parametrize(
    "field",
    [
        "sku_verified",
        "payload_verified",
        "category_verified",
        "selection_verified",
        "media_verified",
    ],
)
def test_any_false_source_validation_is_rejected(field):
    value = package_report()
    value["source_validation"][field] = False
    with pytest.raises(snapshot.WooTargetSnapshotInputError):
        snapshot.validate_package_report(value)


def test_package_target_sku_and_payload_sku_mismatch_is_rejected():
    value = package_report()
    value["future_woo_payload"]["sku"] = "CLM-PRO-OTHER"
    with pytest.raises(snapshot.WooTargetSnapshotInputError):
        snapshot.validate_package_report(value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(target_sku="unsafe sku"),
        lambda value: value.update(future_woo_payload=[]),
        lambda value: value["future_woo_payload"].update(status="publish"),
        lambda value: value["future_woo_payload"].update(type="variable"),
    ],
)
def test_unsafe_sku_or_payload_contract_is_rejected(mutation):
    value = package_report()
    mutation(value)
    with pytest.raises(snapshot.WooTargetSnapshotInputError):
        snapshot.validate_package_report(value)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        Path("https://example.test/package.json"),
        Path("file:///tmp/package.json"),
        Path(r"\\server\share\package.json"),
        Path("package.txt"),
    ],
)
def test_url_unc_and_non_json_package_paths_are_rejected(unsafe_path):
    with pytest.raises(snapshot.WooTargetSnapshotInputError):
        snapshot._safe_local_json_file(unsafe_path)


def test_symlink_or_reparse_package_is_rejected_before_read(tmp_path, monkeypatch):
    path = write_package(tmp_path)
    monkeypatch.setattr(snapshot.package_io, "_has_link_or_reparse", lambda _: True)
    with pytest.raises(snapshot.WooTargetSnapshotInputError) as caught:
        snapshot.read_package_report(path)
    assert str(caught.value) == "woo_target_snapshot_linked_path_not_allowed"


@pytest.mark.parametrize(
    "basename",
    [
        "password.json",
        "credential-report.json",
        "secret-token.json",
        ".env.json",
    ],
)
def test_poc02_sensitive_basename_policy_is_reused(tmp_path, basename):
    path = write_package(tmp_path, name=basename)
    with pytest.raises(snapshot.WooTargetSnapshotInputError) as caught:
        snapshot.read_package_report(path)
    assert str(caught.value) == "woo_target_snapshot_local_json_required"


def test_post_abspath_unc_is_rejected_without_network_share_access(
    tmp_path, monkeypatch
):
    path = write_package(tmp_path)
    monkeypatch.setattr(
        snapshot.package_io.os.path,
        "abspath",
        lambda _: r"\\server\share\single-product-staging-package.json",
    )
    with pytest.raises(snapshot.WooTargetSnapshotInputError) as caught:
        snapshot.read_package_report(path)
    assert str(caught.value) == "woo_target_snapshot_local_json_required"


def test_ordinary_single_product_package_path_still_passes(tmp_path):
    path = write_package(tmp_path)
    value, source, local = snapshot.read_package_report(path)
    assert snapshot.validate_package_report(value) == SKU
    assert local == path
    assert source["basename"] == "single-product-staging-package.json"


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "single-product-staging-package.json"
    path.write_text('{"status":"ok","status":"blocked"}', encoding="utf-8")
    with pytest.raises(snapshot.WooTargetSnapshotInputError) as caught:
        snapshot.read_package_report(path)
    assert str(caught.value) == "woo_target_snapshot_duplicate_json_key"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://xxxxdoll.com",
        "https://other.wpcomstaging.com",
        "http://staging-1d07-owenau512-iqjhz.wpcomstaging.com",
        "http://localhost:8765",
        f"https://user:password@{STAGING_EXPECTED_HOST}",
        f"https://{STAGING_EXPECTED_HOST}?consumer_key=secret",
        f"https://{STAGING_EXPECTED_HOST}#fragment",
        "not-a-url",
    ],
)
def test_invalid_target_is_rejected_before_network(tmp_path, base_url):
    package_path = write_package(tmp_path)
    transport = FakeTransport({1: page([])})
    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
        snapshot.run_woo_target_snapshot(
            package_path,
            base_url,
            None,
            project_root=tmp_path,
            transport=transport,
        )
    assert transport.calls == []
    assert transport.network_requests_performed == 0
    assert transport.write_requests_performed == 0


def test_zero_exact_matches_is_ok_and_create_eligible(tmp_path):
    transport = FakeTransport({1: page([])})
    report, _ = run(tmp_path, transport)
    assert report["status"] == "ok"
    assert report["match_count"] == 0
    assert report["create_eligible"] is True
    assert report["existing_target"] is None
    assert report["blocking_issues"] == []


def test_one_exact_match_is_blocked_with_minimal_projection(tmp_path):
    transport = FakeTransport({1: page([product()])})
    report, _ = run(tmp_path, transport)
    assert report["status"] == "blocked"
    assert report["match_count"] == 1
    assert report["create_eligible"] is False
    assert report["blocking_issues"] == ["woo_target_sku_already_exists"]
    assert report["existing_target"] == {
        "id": 123,
        "sku": SKU,
        "type": "simple",
        "status": "draft",
    }
    assert "permalink" not in json.dumps(report)


def test_duplicate_exact_matches_are_fully_counted_and_blocked(tmp_path):
    transport = FakeTransport({1: page([product(1), product(2)])})
    report, _ = run(tmp_path, transport)
    assert report["status"] == "blocked"
    assert report["match_count"] == 2
    assert report["create_eligible"] is False
    assert report["existing_target"] is None
    assert report["blocking_issues"] == ["woo_target_sku_ambiguous"]


def test_complete_pagination_is_checked_before_reporting_match_count(tmp_path):
    transport = FakeTransport(
        {
            1: page([product(1)], total=2, total_pages=2),
            2: page([product(2)], total=2, total_pages=2),
        }
    )
    report, _ = run(tmp_path, transport)
    assert report["match_count"] == 2
    assert [call[1] for call in transport.calls] == [1, 2]


@pytest.mark.parametrize("returned_sku", [SKU.casefold(), SKU + "-EXTRA", SKU[:-4]])
def test_non_exact_case_sensitive_server_result_fails_closed(tmp_path, returned_sku):
    transport = FakeTransport({1: page([product(sku=returned_sku)])})
    with pytest.raises(snapshot.WooTargetSnapshotDataError) as caught:
        run(tmp_path, transport)
    assert str(caught.value) == "woo_target_snapshot_filter_mismatch"


@pytest.mark.parametrize("product_id", [0, -1, True, "123", None])
def test_invalid_or_bool_product_id_is_rejected(tmp_path, product_id):
    transport = FakeTransport({1: page([product(product_id)])})
    with pytest.raises(snapshot.WooTargetSnapshotDataError):
        run(tmp_path, transport)


def test_non_array_response_is_rejected(tmp_path):
    transport = FakeTransport({1: page({"id": 1}, total=1, total_pages=1)})
    with pytest.raises(snapshot.WooTargetSnapshotDataError) as caught:
        run(tmp_path, transport)
    assert str(caught.value) == "woo_target_snapshot_root_not_array"


def test_pagination_count_mismatch_fails_closed(tmp_path):
    transport = FakeTransport({1: page([], total=1, total_pages=1)})
    with pytest.raises(snapshot.WooTargetSnapshotDataError):
        run(tmp_path, transport)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        total: str = "0",
        total_pages: str = "0",
    ) -> None:
        self.status = status
        self.body = body
        self.headers = {
            "X-WP-Total": total,
            "X-WP-TotalPages": total_pages,
        }

    def read(self, _: int) -> bytes:
        return self.body

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.sock = None
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        return None

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def stdlib_transport_call(response: FakeResponse, *, max_bytes: int = 1000):
    connection = FakeConnection(response)
    with patch.object(
        snapshot.http.client,
        "HTTPSConnection",
        return_value=connection,
    ):
        transport = snapshot.StdlibWooProductTargetTransport(
            BASE_URL,
            CREDENTIALS,
            max_response_bytes=max_bytes,
        )
        result = transport.get_products_by_sku(SKU, page=1)
    return result, transport, connection


def test_transport_uses_only_get_and_safely_encodes_sku_filter():
    result, transport, connection = stdlib_transport_call(
        FakeResponse(b"[]", total="0", total_pages="0")
    )
    assert result.items == []
    assert len(connection.requests) == 1
    method, target, _ = connection.requests[0]
    assert method == "GET"
    assert target.startswith(snapshot.PRODUCT_ENDPOINT + "?")
    assert "sku=CLM-PRO-FD160CM-MERU" in target
    assert "consumer_key" not in target
    assert "consumer_secret" not in target
    assert transport.network_requests_performed == 1
    assert transport.write_requests_performed == 0


def test_malformed_json_response_is_rejected():
    with pytest.raises(snapshot.WooTargetSnapshotTransportError):
        stdlib_transport_call(FakeResponse(b"not-json"))


def test_response_size_cap_is_enforced():
    with pytest.raises(snapshot.WooTargetSnapshotTransportError) as caught:
        stdlib_transport_call(FakeResponse(b"[" + b"x" * 100 + b"]"), max_bytes=32)
    assert str(caught.value) == "woo_target_snapshot_response_too_large"


def test_get_retry_never_becomes_a_write(tmp_path):
    transport = FakeTransport(
        {
            1: [
                snapshot.WooTargetSnapshotRetryableError("retry"),
                page([]),
            ]
        }
    )
    report, _ = run(tmp_path, transport)
    assert report["network_requests_performed"] == 2
    assert report["woocommerce_requests_performed"] == 2
    assert report["write_requests_performed"] == 0
    assert transport.write_requests_performed == 0


def test_transport_that_reports_a_write_after_get_fails_closed(tmp_path):
    class UnsafeTransport(FakeTransport):
        def get_products_by_sku(self, sku, *, page, per_page=100):
            result = super().get_products_by_sku(
                sku, page=page, per_page=per_page
            )
            self.write_requests_performed = 1
            return result

    with pytest.raises(snapshot.WooTargetSnapshotConfigurationError):
        run(tmp_path, UnsafeTransport({1: page([])}))


def test_transport_exposes_no_write_method():
    transport = snapshot.StdlibWooProductTargetTransport(BASE_URL, CREDENTIALS)
    assert not hasattr(transport, "post")
    assert not hasattr(transport, "put")
    assert not hasattr(transport, "patch")
    assert not hasattr(transport, "delete")


def test_report_contains_no_credentials_full_url_or_unsafe_response_fields(tmp_path):
    transport = FakeTransport({1: page([product()])})
    package_path = write_package(tmp_path)
    report, _ = snapshot.run_woo_target_snapshot(
        package_path,
        BASE_URL,
        None,
        project_root=tmp_path,
        transport=transport,
        redactor=snapshot.Redactor.from_values(
            (CREDENTIALS.consumer_key, CREDENTIALS.consumer_secret)
        ),
    )
    text = json.dumps(report, sort_keys=True)
    forbidden = (
        "ck_test_value",
        "cs_test_value",
        "Authorization",
        "Cookie",
        "https://",
        "permalink",
        "consumer_key",
        "consumer_secret",
    )
    assert all(token not in text for token in forbidden)


def test_raw_package_sha256_is_exact_and_only_basename_is_exposed(tmp_path):
    path = tmp_path / "authority.json"
    raw = (json.dumps(package_report(), indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    report, _ = snapshot.run_woo_target_snapshot(
        path,
        BASE_URL,
        None,
        project_root=tmp_path,
        transport=FakeTransport({1: page([])}),
    )
    assert report["source_package"] == {
        "basename": "authority.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert str(tmp_path) not in json.dumps(report)


@pytest.mark.parametrize(
    "items",
    [[], [product()], [product(1), product(2)]],
)
def test_write_authorization_and_all_write_counters_are_always_zero(tmp_path, items):
    report, _ = run(tmp_path, FakeTransport({1: page(items)}))
    assert report["write_authorized"] is False
    for field in (
        "woocommerce_write_requests_performed",
        "wordpress_requests_performed",
        "external_write_requests_performed",
        "write_requests_performed",
    ):
        assert report[field] == 0


def test_transport_error_does_not_overwrite_prior_success(tmp_path):
    package_path = write_package(tmp_path)
    output = tmp_path / "reports" / snapshot.REPORT_FILENAME
    output.parent.mkdir()
    original = b'{"status":"ok","sentinel":true}\n'
    output.write_bytes(original)
    transport = FakeTransport(
        {1: snapshot.WooTargetSnapshotTransportError("safe_failure")}
    )
    with pytest.raises(snapshot.WooTargetSnapshotTransportError):
        snapshot.run_woo_target_snapshot(
            package_path,
            BASE_URL,
            None,
            project_root=tmp_path,
            transport=transport,
        )
    assert output.read_bytes() == original


def test_duplicate_json_error_does_not_overwrite_prior_success(tmp_path):
    package_path = tmp_path / "package.json"
    package_path.write_text('{"status":"ok","status":"blocked"}', encoding="utf-8")
    output = tmp_path / "reports" / snapshot.REPORT_FILENAME
    output.parent.mkdir()
    original = b'{"status":"ok","sentinel":true}\n'
    output.write_bytes(original)
    with pytest.raises(snapshot.WooTargetSnapshotInputError):
        snapshot.run_woo_target_snapshot(
            package_path,
            BASE_URL,
            None,
            project_root=tmp_path,
            transport=FakeTransport({1: page([])}),
        )
    assert output.read_bytes() == original


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("ok", 0), ("blocked", 1)],
)
def test_cli_exit_zero_and_one(status, expected_exit, tmp_path, monkeypatch):
    package_path = write_package(tmp_path)
    report = {
        "status": status,
        "create_eligible": status == "ok",
        "network_requests_performed": 1,
    }
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        cli,
        "load_woo_category_credential_source",
        lambda: {"WC_CONSUMER_KEY": "ck_test", "WC_CONSUMER_SECRET": "cs_test"},
    )
    monkeypatch.setattr(cli, "run_woo_target_snapshot", lambda *args, **kwargs: (report, Path("report.json")))
    assert cli.main(
        [
            "snapshot-woo-target",
            "--package-report",
            str(package_path),
            "--base-url",
            BASE_URL,
        ]
    ) == expected_exit


def test_cli_contract_error_returns_two(tmp_path, monkeypatch):
    package_path = write_package(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        cli,
        "load_woo_category_credential_source",
        lambda: {"WC_CONSUMER_KEY": "ck_test", "WC_CONSUMER_SECRET": "cs_test"},
    )
    monkeypatch.setattr(
        cli,
        "run_woo_target_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            snapshot.WooTargetSnapshotInputError("invalid")
        ),
    )
    assert cli.main(
        [
            "snapshot-woo-target",
            "--package-report",
            str(package_path),
            "--base-url",
            BASE_URL,
        ]
    ) == 2


def test_cli_rejects_wrong_host_before_loading_credentials(monkeypatch):
    loaded = False

    def forbidden_loader():
        nonlocal loaded
        loaded = True
        raise AssertionError("credential loader must not run")

    monkeypatch.setattr(cli, "load_woo_category_credential_source", forbidden_loader)
    exit_code = cli._run_snapshot_woo_target(
        logging.getLogger("test"),
        Path("package.json"),
        "https://xxxxdoll.com",
    )
    assert exit_code == 2
    assert loaded is False


def test_no_real_network_or_write_api_is_used(monkeypatch, tmp_path):
    monkeypatch.setattr(
        snapshot.http.client,
        "HTTPSConnection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("real network forbidden")
        ),
    )
    report, _ = run(tmp_path, FakeTransport({1: page([])}))
    assert report["network_requests_performed"] == 1
    assert report["write_requests_performed"] == 0
