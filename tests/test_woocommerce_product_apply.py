from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli  # noqa: E402
from sync_worker import woocommerce_apply_plan as freeze  # noqa: E402
from sync_worker import woocommerce_product_apply as apply  # noqa: E402
from sync_worker import woocommerce_target_snapshot as target  # noqa: E402
from sync_worker.single_product_staging_package import (  # noqa: E402
    POLICY_VERSION as PACKAGE_POLICY_VERSION,
)
from sync_worker.woo_category_binding import STAGING_EXPECTED_HOST  # noqa: E402
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    WooCategoryCredentials,
)


SKU = "CLM-PRO-FD160CM-MERU"
BASE_URL = f"https://{STAGING_EXPECTED_HOST}"
CREDENTIALS = WooCategoryCredentials("ck_test_private", "cs_test_private")


def frozen_payload() -> dict[str, object]:
    return {
        "name": "Meru",
        "sku": SKU,
        "type": "simple",
        "status": "draft",
        "regular_price": "1399.00",
        "description": "Safe description",
        "short_description": "Safe summary",
        "categories": [{"id": 1431}],
        "attributes": [
            {
                "name": "Height",
                "position": 0,
                "visible": True,
                "variation": False,
                "options": ["160 cm"],
            }
        ],
        "images": [{"id": value} for value in range(100, 112)],
    }


def package_report() -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": PACKAGE_POLICY_VERSION,
        "target_sku": SKU,
        "future_woo_payload": frozen_payload(),
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


def target_snapshot_report() -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": target.POLICY_VERSION,
        "target": {**apply._TARGET, "read_only": True},
        "sku": SKU,
        "match_count": 0,
        "create_eligible": True,
        "existing_target": None,
        "source_package": {
            "basename": "single-product-staging-package.json",
            "sha256": "a" * 64,
        },
        "blocking_issues": [],
        "write_authorized": False,
        "network_requests_performed": 1,
        "woocommerce_requests_performed": 1,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }


def valid_plan() -> dict[str, object]:
    return freeze.build_woo_apply_plan(
        package_report(),
        target_snapshot_report(),
        source_package={
            "basename": "single-product-staging-package.json",
            "sha256": "a" * 64,
        },
        source_target_snapshot={
            "basename": "woo-target-snapshot.json",
            "sha256": "b" * 64,
        },
    )


def write_plan(
    root: Path,
    value: dict[str, object] | None = None,
    *,
    name: str = "woo-apply-plan.json",
) -> tuple[Path, bytes]:
    path = root / name
    raw = (
        json.dumps(value or valid_plan(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    path.write_bytes(raw)
    return path, raw


def page(items: object, *, total=None, total_pages=None):
    count = len(items) if isinstance(items, list) else 0
    return target.WooProductTargetPage(
        items,
        count if total is None else total,
        (1 if count else 0) if total_pages is None else total_pages,
    )


def remote_product(payload: dict[str, object] | None = None) -> dict[str, object]:
    value = copy.deepcopy(payload or frozen_payload())
    value["id"] = 991
    value["permalink"] = "https://ignored.example/product"
    value["slug"] = "ignored"
    value["metadata"] = [{"ignored": True}]
    value["categories"] = [
        {"id": item["id"], "name": "Ignored category"}
        for item in value["categories"]
    ]
    value["attributes"] = [
        {**item, "id": index + 1}
        for index, item in enumerate(value["attributes"])
    ]
    value["images"] = [
        {"id": item["id"], "src": "https://ignored.example/image"}
        for item in value["images"]
    ]
    return value


class FakeGetTransport:
    def __init__(self, responses: list[object], *, base_url: str = BASE_URL) -> None:
        self.responses = list(responses)
        self.base_url = base_url
        self.network_requests_performed = 0
        self.write_requests_performed = 0
        self.calls: list[tuple[str, int, int]] = []

    def get_products_by_sku(self, sku, *, page, per_page=100):
        self.calls.append((sku, page, per_page))
        self.network_requests_performed += 1
        if not self.responses:
            raise AssertionError("unexpected GET")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeCreateTransport:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        before_call=None,
        payload_mutator=None,
    ) -> None:
        self.error = error
        self.before_call = before_call
        self.payload_mutator = payload_mutator
        self.network_requests_performed = 0
        self.write_requests_performed = 0
        self.calls: list[dict[str, object]] = []

    def create_product(self, payload):
        if self.before_call is not None:
            self.before_call()
        if self.payload_mutator is not None:
            self.payload_mutator(payload)
        self.calls.append(copy.deepcopy(dict(payload)))
        self.network_requests_performed += 1
        self.write_requests_performed += 1
        if self.error is not None:
            raise self.error
        return {"id": 991, "sku": SKU}


class Harness:
    def __init__(
        self,
        root: Path,
        *,
        plan_value: dict[str, object] | None = None,
        get_responses: list[object] | None = None,
        create_error: Exception | None = None,
        before_post=None,
    ) -> None:
        self.root = root
        self.plan_value = plan_value or valid_plan()
        self.plan_path, self.plan_raw = write_plan(root, self.plan_value)
        self.get = FakeGetTransport(
            get_responses
            if get_responses is not None
            else [page([]), page([remote_product()])]
        )
        self.create = FakeCreateTransport(
            error=create_error,
            before_call=before_post,
        )
        self.credential_loads = 0
        self.get_factory_calls = 0
        self.create_factory_calls = 0

    def credential_loader(self):
        self.credential_loads += 1
        return CREDENTIALS, apply.redactor_for_woo_category_credentials(CREDENTIALS)

    def get_factory(self, base_url, credentials):
        assert base_url == BASE_URL
        assert credentials is CREDENTIALS
        self.get_factory_calls += 1
        return self.get

    def create_factory(self, base_url, credentials):
        assert base_url == BASE_URL
        assert credentials is CREDENTIALS
        self.create_factory_calls += 1
        return self.create

    def run(self, *, confirm: str | None = None, base_url: str = BASE_URL):
        return apply.run_woo_product_apply(
            self.plan_path,
            self.plan_value["plan_hash"] if confirm is None else confirm,
            base_url,
            project_root=self.root,
            credential_loader=self.credential_loader,
            get_transport_factory=self.get_factory,
            create_transport_factory=self.create_factory,
        )


def test_valid_plan_and_exact_manual_hash_pass_local_validation():
    source = valid_plan()
    sku, payload, digest = apply.validate_frozen_plan(source, source["plan_hash"])
    assert sku == SKU
    assert payload == frozen_payload()
    assert digest == source["plan_hash"]


@pytest.mark.parametrize(
    ("mutation", "confirmation"),
    [
        (lambda value: value.update(plan_hash="f" * 64), None),
        (
            lambda value: value["operation"]["payload"].update(
                regular_price="1499.00"
            ),
            None,
        ),
        (lambda value: None, "e" * 64),
        (lambda value: value.update(policy_version="old-policy"), None),
        (
            lambda value: value.update(
                status="blocked", blocking_issues=["blocked"], plan_hash=None
            ),
            None,
        ),
        (lambda value: value["operation"].update(action="update"), None),
    ],
)
def test_invalid_authorization_contract_fails_before_credentials(
    tmp_path, mutation, confirmation
):
    value = valid_plan()
    mutation(value)
    harness = Harness(tmp_path, plan_value=value)
    with pytest.raises(apply.WooProductApplyPreWriteError):
        harness.run(
            confirm=(value.get("plan_hash") if confirmation is None else confirmation)
        )
    assert harness.credential_loads == 0
    assert harness.get.network_requests_performed == 0
    assert harness.create.write_requests_performed == 0


def test_extra_plan_root_field_fails_before_credentials(tmp_path):
    value = valid_plan()
    value["unapproved"] = "value"
    harness = Harness(tmp_path, plan_value=value)
    with pytest.raises(apply.WooProductApplyPreWriteError):
        harness.run()
    assert harness.credential_loads == 0
    assert harness.get.network_requests_performed == 0
    assert harness.create.write_requests_performed == 0


@pytest.mark.parametrize(
    "unsafe_path",
    [
        Path("https://example.test/woo-apply-plan.json"),
        Path("file:///tmp/woo-apply-plan.json"),
        Path(r"\\server\share\woo-apply-plan.json"),
        Path("woo-apply-plan.txt"),
    ],
)
def test_unsafe_plan_path_fails_closed(unsafe_path):
    with pytest.raises(apply.WooProductApplyPreWriteError):
        apply._read_plan(unsafe_path)


def test_duplicate_plan_json_keys_fail_closed(tmp_path):
    path = tmp_path / "woo-apply-plan.json"
    path.write_text('{"status":"ok","status":"blocked"}', encoding="utf-8")
    with pytest.raises(apply.WooProductApplyPreWriteError) as caught:
        apply._read_plan(path)
    assert str(caught.value) == "woo_apply_plan_duplicate_json_key"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://xxxxdoll.com",
        "http://staging-1d07-owenau512-iqjhz.wpcomstaging.com",
        f"https://{STAGING_EXPECTED_HOST}:443",
        f"https://{STAGING_EXPECTED_HOST}:8443",
        f"https://{STAGING_EXPECTED_HOST}/shop",
        f"https://{STAGING_EXPECTED_HOST}/wp-json",
        f"https://{STAGING_EXPECTED_HOST}?x=1",
        f"https://{STAGING_EXPECTED_HOST}#fragment",
        f"https://user:password@{STAGING_EXPECTED_HOST}",
        "https://127.0.0.1",
        "https://other.wpcomstaging.com",
    ],
)
def test_non_exact_staging_root_fails_before_credentials_and_network(
    tmp_path, base_url
):
    harness = Harness(tmp_path)
    with pytest.raises(apply.WooProductApplyPreWriteError):
        harness.run(base_url=base_url)
    assert harness.credential_loads == 0
    assert harness.get_factory_calls == 0
    assert harness.create_factory_calls == 0


@pytest.mark.parametrize("base_url", [BASE_URL, BASE_URL + "/"])
def test_exact_approved_staging_root_is_accepted(base_url):
    assert apply.validate_apply_base_url(base_url) == BASE_URL


def test_existing_receipt_blocks_before_credentials_and_post(tmp_path):
    harness = Harness(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    receipt = reports / apply.RECEIPT_FILENAME
    receipt.write_text("{}", encoding="utf-8")
    result = harness.run()
    assert result["exit_code"] == 1
    assert result["status"] == "blocked_pre_write"
    assert harness.credential_loads == 0
    assert harness.create.calls == []
    assert receipt.exists()


@pytest.mark.parametrize("filename", [apply.PENDING_FILENAME, apply.LOCK_FILENAME])
def test_existing_pending_or_lock_requires_recovery_without_deletion(
    tmp_path, filename
):
    harness = Harness(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    artifact = reports / filename
    original = b'{"sentinel":true}\n'
    artifact.write_bytes(original)
    result = harness.run()
    assert result["exit_code"] == 3
    assert result["status"] == "recovery_required"
    assert artifact.read_bytes() == original
    assert harness.credential_loads == 0
    assert harness.create.calls == []


def test_fresh_preflight_zero_matches_allows_one_post(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result["exit_code"] == 0
    assert len(harness.create.calls) == 1


@pytest.mark.parametrize("count", [1, 2, 4])
def test_fresh_preflight_existing_matches_block_without_post(tmp_path, count):
    products = [remote_product() for _ in range(count)]
    for index, item in enumerate(products, start=1):
        item["id"] = index
    harness = Harness(tmp_path, get_responses=[page(products)])
    result = harness.run()
    assert result["exit_code"] == 1
    assert result["status"] == "blocked_pre_write"
    assert harness.create.calls == []
    assert result["woocommerce_write_requests_performed"] == 0


def test_preflight_get_error_is_exit_two_without_post(tmp_path):
    harness = Harness(
        tmp_path,
        get_responses=[target.WooTargetSnapshotDataError("mock_get_failure")],
    )
    result = harness.run()
    assert result["exit_code"] == 2
    assert result["status"] == "pre_write_error"
    assert harness.create.calls == []


@pytest.mark.parametrize("returned_sku", [SKU.casefold(), SKU + "-EXTRA"])
def test_preflight_case_or_partial_sku_fails_closed(tmp_path, returned_sku):
    remote = remote_product()
    remote["sku"] = returned_sku
    harness = Harness(tmp_path, get_responses=[page([remote])])
    result = harness.run()
    assert result["exit_code"] == 2
    assert harness.create.calls == []


def test_preflight_incomplete_pagination_fails_closed(tmp_path):
    harness = Harness(
        tmp_path,
        get_responses=[page([], total=1, total_pages=1)],
    )
    result = harness.run()
    assert result["exit_code"] == 2
    assert harness.create.calls == []


def test_pending_prepared_then_attempting_is_persisted_before_post(
    tmp_path, monkeypatch
):
    states: list[dict[str, object]] = []
    original = apply._write_pending

    def recording_write(path, value, redactor):
        original(path, value, redactor)
        states.append(copy.deepcopy(dict(value)))

    monkeypatch.setattr(apply, "_write_pending", recording_write)

    def before_post():
        persisted = json.loads(
            (tmp_path / "reports" / apply.PENDING_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        assert persisted["post_state"] == "attempting"
        assert persisted["post_attempts_started"] == 1

    harness = Harness(tmp_path, before_post=before_post)
    result = harness.run()
    assert result["exit_code"] == 0
    assert [value["post_state"] for value in states] == ["prepared", "attempting"]
    assert [value["post_attempts_started"] for value in states] == [0, 1]


def test_pending_binds_plan_payload_sku_and_target_before_post(tmp_path):
    observed: dict[str, object] = {}

    def before_post():
        observed.update(
            json.loads(
                (tmp_path / "reports" / apply.PENDING_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
        )

    harness = Harness(tmp_path, before_post=before_post)
    result = harness.run()
    assert result["exit_code"] == 0
    assert observed["plan_hash"] == harness.plan_value["plan_hash"]
    assert observed["source_plan"] == {
        "basename": harness.plan_path.name,
        "sha256": hashlib.sha256(harness.plan_raw).hexdigest(),
    }
    assert observed["target"] == apply._TARGET
    assert observed["operation"] == {
        "action": "create",
        "sku": SKU,
        "payload_sha256": apply.canonical_payload_hash(frozen_payload()),
    }
    assert observed["manual_confirmation_verified"] is True


def test_post_body_is_deep_equal_to_frozen_payload_and_plan_is_not_mutated(tmp_path):
    harness = Harness(tmp_path)
    original_plan = copy.deepcopy(harness.plan_value)
    original_bytes = harness.plan_path.read_bytes()
    result = harness.run()
    assert result["exit_code"] == 0
    assert harness.create.calls == [frozen_payload()]
    assert harness.plan_value == original_plan
    assert harness.plan_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("mock timeout"),
        apply.WooProductCreateTransportError("mock_http_error"),
        apply.WooProductCreateTransportError("mock_invalid_json"),
    ],
)
def test_post_exception_with_exact_readback_converges_without_second_post(
    tmp_path, error
):
    harness = Harness(tmp_path, create_error=error)
    result = harness.run()
    assert result["exit_code"] == 0
    assert len(harness.create.calls) == 1
    assert result["woocommerce_write_requests_performed"] == 1
    assert len(harness.get.calls) == 2
    assert not (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    receipt = json.loads(
        (tmp_path / "reports" / apply.RECEIPT_FILENAME).read_text(encoding="utf-8")
    )
    assert receipt["post_transport_completed_without_error"] is False
    assert "post_transport_response_received" not in receipt


def _mismatched_readback():
    remote = remote_product()
    remote["regular_price"] = "1.00"
    return page([remote])


@pytest.mark.parametrize(
    "readback_response",
    [
        page([]),
        page([remote_product(), {**remote_product(), "id": 992}]),
        target.WooTargetSnapshotDataError("mock_reconciliation_failure"),
        _mismatched_readback(),
    ],
    ids=("zero", "multiple", "get-error", "payload-mismatch"),
)
def test_post_exception_with_unconfirmed_readback_requires_recovery(
    tmp_path, readback_response
):
    harness = Harness(
        tmp_path,
        create_error=TimeoutError("mock timeout"),
        get_responses=[page([]), readback_response],
    )
    result = harness.run()
    assert result["exit_code"] == 3
    assert len(harness.create.calls) == 1
    assert len(harness.get.calls) == 2
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    assert not (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()


def test_invalid_post_write_counter_still_reconciles_but_cannot_apply(tmp_path):
    harness = Harness(tmp_path)

    def create_without_count(payload):
        harness.create.calls.append(copy.deepcopy(dict(payload)))
        harness.create.network_requests_performed += 1
        return {"id": 991, "sku": SKU}

    harness.create.create_product = create_without_count
    result = harness.run()
    assert result["exit_code"] == 3
    assert len(harness.create.calls) == 1
    assert len(harness.get.calls) == 2
    assert result["woocommerce_write_requests_performed"] == 0
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    assert not (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()


def test_create_transport_exposes_no_update_delete_or_generic_request_method():
    transport = apply.StdlibWooProductCreateTransport(BASE_URL, CREDENTIALS)
    for name in ("put", "patch", "delete", "update", "batch", "request"):
        assert not hasattr(transport, name)
    source = inspect.getsource(transport.create_product)
    assert "for " not in source
    assert "while " not in source


class FakeHttpResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def read(self, _: int) -> bytes:
        return self.body


class FakeConnection:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.sock = None
        self.requests = []

    def connect(self):
        if self.error:
            raise self.error

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        return None


@pytest.mark.parametrize(
    "connection",
    [
        FakeConnection(error=TimeoutError("mock")),
        FakeConnection(FakeHttpResponse(500, b'{}')),
        FakeConnection(FakeHttpResponse(201, b'not-json')),
    ],
)
def test_stdlib_create_transport_never_retries_failed_post(connection):
    with patch.object(apply.http.client, "HTTPSConnection", return_value=connection):
        transport = apply.StdlibWooProductCreateTransport(BASE_URL, CREDENTIALS)
        with pytest.raises(apply.WooProductCreateTransportError):
            transport.create_product(frozen_payload())
    assert transport.network_requests_performed == 1
    assert transport.write_requests_performed == 1
    assert len(connection.requests) <= 1


def test_exact_readback_with_remote_extra_fields_succeeds(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result["exit_code"] == 0
    assert result["status"] == "applied"


@pytest.mark.parametrize("readback_count", [0, 2, 3])
def test_readback_non_unique_requires_recovery_and_keeps_pending(
    tmp_path, readback_count
):
    items = [remote_product() for _ in range(readback_count)]
    for index, item in enumerate(items, start=1):
        item["id"] = index
    harness = Harness(tmp_path, get_responses=[page([]), page(items)])
    result = harness.run()
    assert result["exit_code"] == 3
    assert len(harness.create.calls) == 1
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    assert not (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()


def test_readback_get_error_requires_recovery_and_keeps_pending(tmp_path):
    harness = Harness(
        tmp_path,
        get_responses=[
            page([]),
            target.WooTargetSnapshotDataError("mock_readback_failure"),
        ],
    )
    result = harness.run()
    assert result["exit_code"] == 3
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(name="Changed"),
        lambda value: value.update(regular_price="1.00"),
        lambda value: value.update(categories=[{"id": 1432}]),
        lambda value: value.update(categories=[{"id": True}]),
        lambda value: value.update(categories=[{"id": 0}]),
        lambda value: value["attributes"][0].update(options=["Changed"]),
        lambda value: value["attributes"][0].update(position=False),
        lambda value: value["images"][0].update(id=999),
        lambda value: value["images"][0].update(id=True),
        lambda value: value["images"][0].update(id=0),
        lambda value: value.update(images=list(reversed(value["images"]))),
        lambda value: value.update(status="publish"),
        lambda value: value.update(description="Changed"),
        lambda value: value.update(short_description="Changed"),
    ],
)
def test_readback_field_mismatch_requires_recovery(tmp_path, mutator):
    remote = remote_product()
    mutator(remote)
    harness = Harness(tmp_path, get_responses=[page([]), page([remote])])
    result = harness.run()
    assert result["exit_code"] == 3
    assert len(harness.create.calls) == 1
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    assert not (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()


def test_success_receipt_contains_only_safe_bound_fields(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    receipt_path = tmp_path / "reports" / apply.RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result["exit_code"] == 0
    assert receipt["status"] == "applied"
    assert receipt["policy_version"] == apply.RECEIPT_POLICY_VERSION
    assert receipt["product"] == {
        "id": 991,
        "sku": SKU,
        "name": "Meru",
        "type": "simple",
        "status": "draft",
    }
    assert receipt["plan_hash"] == harness.plan_value["plan_hash"]
    assert receipt["source_plan"]["sha256"] == hashlib.sha256(
        harness.plan_raw
    ).hexdigest()
    assert receipt["operation"]["payload_sha256"] == apply.canonical_payload_hash(
        frozen_payload()
    )
    assert receipt["manual_confirmation_verified"] is True
    assert receipt["readback_verified"] is True
    assert receipt["post_transport_completed_without_error"] is True
    assert "post_transport_response_received" not in receipt
    assert receipt["woocommerce_write_requests_performed"] == 1
    text = json.dumps(receipt, sort_keys=True)
    for forbidden in (
        "https://",
        "Authorization",
        "Cookie",
        "ck_test_private",
        "cs_test_private",
        str(tmp_path),
    ):
        assert forbidden not in text


def test_receipt_is_written_only_after_readback_passes(tmp_path, monkeypatch):
    calls: list[str] = []
    original = apply._write_receipt

    def recording_write(path, value, redactor):
        calls.append("receipt")
        original(path, value, redactor)

    monkeypatch.setattr(apply, "_write_receipt", recording_write)
    remote = remote_product()
    remote["name"] = "Mismatch"
    harness = Harness(tmp_path, get_responses=[page([]), page([remote])])
    result = harness.run()
    assert result["exit_code"] == 3
    assert calls == []


def test_receipt_write_failure_keeps_pending_and_requires_recovery(
    tmp_path, monkeypatch
):
    harness = Harness(tmp_path)
    monkeypatch.setattr(
        apply,
        "_write_receipt",
        lambda *args: (_ for _ in ()).throw(OSError("mock receipt failure")),
    )
    result = harness.run()
    assert result["exit_code"] == 3
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    assert not (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()


def test_pending_deleted_only_after_receipt_write(tmp_path, monkeypatch):
    order: list[str] = []
    original_write = apply._write_receipt
    original_unlink = Path.unlink

    def write_receipt(path, value, redactor):
        original_write(path, value, redactor)
        order.append("receipt")

    def unlink(path, *args, **kwargs):
        if path.name == apply.PENDING_FILENAME:
            assert (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()
            order.append("pending_delete")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(apply, "_write_receipt", write_receipt)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = Harness(tmp_path).run()
    assert result["exit_code"] == 0
    assert order == ["receipt", "pending_delete"]


def test_pending_delete_failure_keeps_receipt_and_pending_and_returns_three(
    tmp_path, monkeypatch
):
    harness = Harness(tmp_path)
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name == apply.PENDING_FILENAME:
            raise OSError("mock pending cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = harness.run()
    assert result["exit_code"] == 3
    assert (tmp_path / "reports" / apply.PENDING_FILENAME).exists()
    assert (tmp_path / "reports" / apply.RECEIPT_FILENAME).exists()


def test_success_counters_are_two_gets_and_exactly_one_write(tmp_path):
    result = Harness(tmp_path).run()
    assert result["exit_code"] == 0
    assert result["network_requests_performed"] == 3
    assert result["woocommerce_requests_performed"] == 3
    assert result["woocommerce_write_requests_performed"] == 1
    assert result["external_write_requests_performed"] == 1
    assert result["wordpress_requests_performed"] == 0


def test_create_transport_receives_deep_copy_and_cannot_mutate_frozen_payload(
    tmp_path
):
    harness = Harness(tmp_path)
    original_payload = copy.deepcopy(harness.plan_value["operation"]["payload"])
    harness.create.payload_mutator = lambda payload: payload.update(name="Mutated")
    result = harness.run()
    assert result["exit_code"] == 0
    assert harness.create.calls[0]["name"] == "Mutated"
    assert harness.plan_value["operation"]["payload"] == original_payload
    assert harness.plan_path.read_bytes() == harness.plan_raw


def test_cli_requires_only_plan_hash_and_base_url_authorities():
    arguments = cli.build_parser().parse_args(
        [
            "apply-woo-plan",
            "--plan-report",
            "reports/woo-apply-plan.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--base-url",
            BASE_URL,
        ]
    )
    assert arguments.command == "apply-woo-plan"
    for forbidden in ("sku", "payload", "product_id", "production", "force", "retry_post"):
        assert not hasattr(arguments, forbidden)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_cli_preserves_exit_zero_one_two_three(exit_code, monkeypatch):
    monkeypatch.setattr(
        cli,
        "run_woo_product_apply",
        lambda *args, **kwargs: {
            "status": "mock",
            "result_code": "mock",
            "exit_code": exit_code,
            "network_requests_performed": 0,
            "woocommerce_write_requests_performed": 0,
        },
    )
    assert cli.main(
        [
            "apply-woo-plan",
            "--plan-report",
            "plan.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--base-url",
            BASE_URL,
        ]
    ) == exit_code


def test_cli_prewrite_exception_is_exit_two(monkeypatch):
    monkeypatch.setattr(
        cli,
        "run_woo_product_apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            apply.WooProductApplyPreWriteError("safe_error")
        ),
    )
    assert cli.main(
        [
            "apply-woo-plan",
            "--plan-report",
            "plan.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--base-url",
            BASE_URL,
        ]
    ) == 2


def test_no_auto_confirmation_already_applied_or_recovery_retry_implementation():
    source = Path(apply.__file__).read_text(encoding="utf-8")
    assert "ALREADY_APPLIED" not in source
    assert "retry_post" not in source
    assert "resume" not in source
    assert "rollback" not in source
    assert "confirmed_plan_hash =" not in source


def test_no_real_network_is_used_by_focused_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        apply.http.client,
        "HTTPSConnection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("real network forbidden")
        ),
    )
    result = Harness(tmp_path).run()
    assert result["exit_code"] == 0
    assert result["woocommerce_write_requests_performed"] == 1
