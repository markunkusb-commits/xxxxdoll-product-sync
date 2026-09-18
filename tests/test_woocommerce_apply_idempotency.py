from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli  # noqa: E402
from sync_worker import woocommerce_apply_idempotency as verify  # noqa: E402
from sync_worker import woocommerce_apply_plan as freeze  # noqa: E402
from sync_worker import woocommerce_product_apply as apply_core  # noqa: E402
from sync_worker import woocommerce_target_snapshot as target  # noqa: E402
from sync_worker.single_product_staging_package import (  # noqa: E402
    POLICY_VERSION as PACKAGE_POLICY_VERSION,
)
from sync_worker.woo_category_binding import STAGING_EXPECTED_HOST  # noqa: E402
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    WooCategoryCredentials,
)


SKU = "CLM-PRO-FD160CM-MERU"
PRODUCT_ID = 18294
BASE_URL = f"https://{STAGING_EXPECTED_HOST}"
CREDENTIALS = WooCategoryCredentials("ck_mock_only", "cs_mock_only")


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
        "target": {**apply_core._TARGET, "read_only": True},
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


def json_bytes(value: object, *, indent: int = 2) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    ).encode("utf-8")


def valid_receipt(
    plan: dict[str, object],
    plan_source: dict[str, str],
) -> dict[str, object]:
    payload = plan["operation"]["payload"]
    return {
        "status": "applied",
        "policy_version": apply_core.RECEIPT_POLICY_VERSION,
        "plan_hash": plan["plan_hash"],
        "source_plan": dict(plan_source),
        "target": dict(apply_core._TARGET),
        "operation": {
            "action": "create",
            "sku": SKU,
            "payload_sha256": apply_core.canonical_payload_hash(payload),
        },
        "product": {
            "id": PRODUCT_ID,
            "sku": SKU,
            "name": payload["name"],
            "type": payload["type"],
            "status": payload["status"],
        },
        "manual_confirmation_verified": True,
        "readback_verified": True,
        "post_transport_completed_without_error": True,
        "network_requests_performed": 3,
        "woocommerce_requests_performed": 3,
        "woocommerce_write_requests_performed": 1,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 1,
        "write_requests_performed": 1,
    }


def remote_product(
    *,
    product_id: int = PRODUCT_ID,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    value = copy.deepcopy(payload or frozen_payload())
    value["id"] = product_id
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


def page(items: object, *, total=None, total_pages=None):
    count = len(items) if isinstance(items, list) else 0
    return target.WooProductTargetPage(
        items,
        count if total is None else total,
        (1 if count else 0) if total_pages is None else total_pages,
    )


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


class Harness:
    def __init__(
        self,
        root: Path,
        *,
        receipt_mutator=None,
        responses: list[object] | None = None,
    ) -> None:
        self.root = root
        self.plan = valid_plan()
        self.plan_path = root / "woo-apply-plan.json"
        self.plan_raw = json_bytes(self.plan)
        self.plan_path.write_bytes(self.plan_raw)
        self.plan_source = {
            "basename": self.plan_path.name,
            "sha256": hashlib.sha256(self.plan_raw).hexdigest(),
        }
        self.receipt = valid_receipt(self.plan, self.plan_source)
        if receipt_mutator is not None:
            receipt_mutator(self.receipt)
        self.receipt_path = root / "woo-apply-receipt.json"
        self.receipt_raw = json_bytes(self.receipt)
        self.receipt_path.write_bytes(self.receipt_raw)
        self.transport = FakeGetTransport(
            responses if responses is not None else [page([remote_product()])]
        )
        self.credential_loads = 0
        self.factory_calls = 0

    def credential_loader(self):
        self.credential_loads += 1
        return CREDENTIALS, apply_core.redactor_for_woo_category_credentials(
            CREDENTIALS
        )

    def get_factory(self, base_url, credentials):
        assert base_url == BASE_URL
        assert credentials is CREDENTIALS
        self.factory_calls += 1
        return self.transport

    def run(
        self,
        *,
        confirm: str | None = None,
        base_url: str = BASE_URL,
        receipt_path: Path | None = None,
    ):
        return verify.run_woo_apply_receipt_verification(
            self.plan_path,
            self.receipt_path if receipt_path is None else receipt_path,
            self.plan["plan_hash"] if confirm is None else confirm,
            base_url,
            project_root=self.root,
            credential_loader=self.credential_loader,
            get_transport_factory=self.get_factory,
        )


def assert_local_failure(harness: Harness, result: dict[str, object]) -> None:
    assert result["exit_code"] == 2
    assert result["network_requests_performed"] == 0
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.factory_calls == 0
    assert harness.transport.calls == []


def test_valid_plan_receipt_and_exact_remote_is_already_applied(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result["status"] == "already_applied"
    assert result["result_code"] == "woo_apply_already_applied"
    assert result["exit_code"] == 0
    assert result["product"] == {"id": PRODUCT_ID, "sku": SKU}
    assert result["network_requests_performed"] == 1
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.transport.calls == [(SKU, 1, 100)]
    assert harness.credential_loads == 1
    assert harness.factory_calls == 1


def test_receipt_raw_sha_is_reported_and_both_authorities_are_read_only(tmp_path):
    harness = Harness(tmp_path)
    plan_before = harness.plan_path.read_bytes()
    receipt_before = harness.receipt_path.read_bytes()
    result = harness.run()
    assert result["exit_code"] == 0
    assert result["source_receipt"] == {
        "basename": harness.receipt_path.name,
        "sha256": hashlib.sha256(receipt_before).hexdigest(),
    }
    assert harness.plan_path.read_bytes() == plan_before
    assert harness.receipt_path.read_bytes() == receipt_before


def test_receipt_source_plan_raw_sha_binding_is_exact(tmp_path):
    harness = Harness(tmp_path)
    harness.plan_raw = json_bytes(harness.plan, indent=4)
    harness.plan_path.write_bytes(harness.plan_raw)
    result = harness.run()
    assert_local_failure(harness, result)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(plan_hash="f" * 64),
        lambda value: value["operation"].update(payload_sha256="f" * 64),
        lambda value: value["product"].update(id=0),
        lambda value: value["product"].update(id=True),
        lambda value: value["product"].update(sku="OTHER-SKU"),
        lambda value: value["product"].update(name="Changed"),
        lambda value: value["target"].update(source_host="xxxxdoll.com"),
        lambda value: value.update(woocommerce_write_requests_performed=0),
        lambda value: value.update(woocommerce_write_requests_performed=True),
        lambda value: value.update(manual_confirmation_verified=False),
        lambda value: value.update(readback_verified=False),
        lambda value: value["source_plan"].update(basename="other.json"),
        lambda value: value["operation"].update(action="update"),
        lambda value: value.update(unapproved="field"),
    ],
)
def test_receipt_contract_mismatch_is_local_exit_two(tmp_path, mutator):
    harness = Harness(tmp_path, receipt_mutator=mutator)
    assert_local_failure(harness, harness.run())


def test_manual_plan_hash_mismatch_is_local_exit_two(tmp_path):
    harness = Harness(tmp_path)
    assert_local_failure(harness, harness.run(confirm="f" * 64))


def test_tampered_plan_is_local_exit_two(tmp_path):
    harness = Harness(tmp_path)
    harness.plan["operation"]["payload"]["regular_price"] = "1.00"
    harness.plan_path.write_bytes(json_bytes(harness.plan))
    assert_local_failure(harness, harness.run())


@pytest.mark.parametrize(
    "base_url",
    [
        "https://xxxxdoll.com",
        f"https://{STAGING_EXPECTED_HOST}:443",
        f"https://{STAGING_EXPECTED_HOST}/shop",
        f"https://{STAGING_EXPECTED_HOST}?query=1",
    ],
)
def test_non_exact_staging_root_is_rejected_before_credentials(tmp_path, base_url):
    harness = Harness(tmp_path)
    assert_local_failure(harness, harness.run(base_url=base_url))


def test_missing_safe_receipt_is_blocked_without_credentials(tmp_path):
    harness = Harness(tmp_path)
    harness.receipt_path.unlink()
    result = harness.run()
    assert result["exit_code"] == 1
    assert result["status"] == "blocked"
    assert result["result_code"] == "woo_apply_receipt_not_found"
    assert harness.credential_loads == 0
    assert harness.transport.calls == []


@pytest.mark.parametrize(
    "unsafe_path",
    [
        Path("https://example.test/woo-apply-receipt.json"),
        Path(r"\\server\share\woo-apply-receipt.json"),
        Path("password.json"),
        Path("woo-apply-receipt.txt"),
    ],
)
def test_unsafe_receipt_path_is_local_exit_two(tmp_path, unsafe_path):
    harness = Harness(tmp_path)
    assert_local_failure(harness, harness.run(receipt_path=unsafe_path))


def test_duplicate_receipt_json_keys_are_rejected_before_credentials(tmp_path):
    harness = Harness(tmp_path)
    harness.receipt_path.write_text(
        '{"status":"applied","status":"applied"}', encoding="utf-8"
    )
    assert_local_failure(harness, harness.run())


@pytest.mark.parametrize(
    "responses",
    [
        [page([])],
        [page([remote_product(), remote_product(product_id=PRODUCT_ID + 1)])],
        [page([remote_product(product_id=PRODUCT_ID + 1)])],
        [target.WooTargetSnapshotDataError("mock GET failure")],
    ],
    ids=("zero", "multiple", "id-mismatch", "get-failure"),
)
def test_remote_unconfirmed_state_requires_recovery(tmp_path, responses):
    harness = Harness(tmp_path, responses=responses)
    result = harness.run()
    assert result["status"] == "recovery_required"
    assert result["exit_code"] == 3
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.transport.network_requests_performed >= 1


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(name="Changed"),
        lambda value: value.update(regular_price="1.00"),
        lambda value: value.update(categories=[{"id": 1432}]),
        lambda value: value["attributes"][0].update(options=["Changed"]),
        lambda value: value.update(images=list(reversed(value["images"]))),
    ],
    ids=("name", "price", "category", "attribute", "image-order"),
)
def test_remote_payload_mismatch_requires_recovery(tmp_path, mutator):
    remote = remote_product()
    mutator(remote)
    harness = Harness(tmp_path, responses=[page([remote])])
    result = harness.run()
    assert result["exit_code"] == 3
    assert result["woocommerce_write_requests_performed"] == 0


def test_remote_extra_woo_fields_are_ignored(tmp_path):
    harness = Harness(tmp_path, responses=[page([remote_product()])])
    assert harness.run()["exit_code"] == 0


def test_transport_must_remain_get_only(tmp_path):
    harness = Harness(tmp_path)
    harness.transport.write_requests_performed = 1
    result = harness.run()
    assert result["exit_code"] == 2
    assert result["network_requests_performed"] == 0
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.credential_loads == 1
    assert harness.factory_calls == 1
    assert harness.transport.calls == []


def test_no_pending_or_lock_is_created(tmp_path):
    harness = Harness(tmp_path)
    assert harness.run()["exit_code"] == 0
    assert not (tmp_path / "reports" / apply_core.PENDING_FILENAME).exists()
    assert not (tmp_path / "reports" / apply_core.LOCK_FILENAME).exists()


def test_canonical_receipt_does_not_count_as_unresolved_runtime_state(tmp_path):
    harness = Harness(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    canonical_receipt = reports / apply_core.RECEIPT_FILENAME
    harness.receipt_path.replace(canonical_receipt)
    harness.receipt_path = canonical_receipt
    assert harness.run()["exit_code"] == 0


@pytest.mark.parametrize(
    "filenames",
    [
        (apply_core.PENDING_FILENAME,),
        (apply_core.LOCK_FILENAME,),
        (apply_core.PENDING_FILENAME, apply_core.LOCK_FILENAME),
    ],
    ids=("pending", "lock", "pending-and-lock"),
)
def test_existing_runtime_state_requires_recovery_without_mutation(
    tmp_path, filenames
):
    harness = Harness(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    expected: dict[Path, bytes] = {}
    for filename in filenames:
        path = reports / filename
        content = json_bytes({"sentinel": filename})
        path.write_bytes(content)
        expected[path] = content

    result = harness.run()

    assert result["status"] == "recovery_required"
    assert result["result_code"] == (
        "woo_apply_receipt_runtime_state_requires_recovery"
    )
    assert result["exit_code"] == 3
    assert result["network_requests_performed"] == 0
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.factory_calls == 0
    assert harness.transport.calls == []
    assert {path: path.read_bytes() for path in expected} == expected


def test_module_has_no_create_or_post_capability():
    source = inspect.getsource(verify)
    assert "StdlibWooProductCreateTransport" not in source
    assert "create_product" not in source
    assert "http.client" not in source
    assert '"POST"' not in source
    assert ".unlink(" not in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    assert "os.remove(" not in source


def test_cli_registers_only_read_only_receipt_authorities():
    arguments = cli.build_parser().parse_args(
        [
            "verify-woo-apply-receipt",
            "--plan-report",
            "reports/woo-apply-plan.json",
            "--receipt-report",
            "reports/woo-apply-receipt.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--base-url",
            BASE_URL,
        ]
    )
    assert arguments.command == "verify-woo-apply-receipt"
    for forbidden in ("sku", "product_id", "payload", "force", "retry", "production"):
        assert not hasattr(arguments, forbidden)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_cli_preserves_idempotency_exit_codes(exit_code, monkeypatch):
    monkeypatch.setattr(
        cli,
        "run_woo_apply_receipt_verification",
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
            "verify-woo-apply-receipt",
            "--plan-report",
            "plan.json",
            "--receipt-report",
            "receipt.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--base-url",
            BASE_URL,
        ]
    ) == exit_code


def test_focused_harness_never_constructs_real_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        target.http.client,
        "HTTPSConnection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("real network forbidden")
        ),
    )
    result = Harness(tmp_path).run()
    assert result["exit_code"] == 0
    assert result["woocommerce_write_requests_performed"] == 0
