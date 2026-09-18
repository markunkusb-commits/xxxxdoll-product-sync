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
from sync_worker import woocommerce_apply_plan as freeze  # noqa: E402
from sync_worker import woocommerce_pending_recovery as recovery  # noqa: E402
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


def valid_pending(
    plan: dict[str, object],
    plan_source: dict[str, str],
) -> dict[str, object]:
    payload = plan["operation"]["payload"]
    return {
        "plan_hash": plan["plan_hash"],
        "source_plan": dict(plan_source),
        "target": dict(apply_core._TARGET),
        "operation": {
            "action": "create",
            "sku": SKU,
            "payload_sha256": apply_core.canonical_payload_hash(payload),
        },
        "manual_confirmation_verified": True,
        "status": "pending",
        "policy_version": apply_core.PENDING_POLICY_VERSION,
        "post_state": "attempting",
        "post_attempts_started": 1,
        "write_requests_performed": 0,
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
        pending_mutator=None,
        responses: list[object] | None = None,
        include_pending: bool = True,
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
        self.reports = root / "reports"
        self.reports.mkdir()
        self.pending = valid_pending(self.plan, self.plan_source)
        if pending_mutator is not None:
            pending_mutator(self.pending)
        self.pending_path = self.reports / apply_core.PENDING_FILENAME
        self.pending_raw = json_bytes(self.pending)
        if include_pending:
            self.pending_path.write_bytes(self.pending_raw)
        self.receipt_path = self.reports / apply_core.RECEIPT_FILENAME
        self.lock_path = self.reports / apply_core.LOCK_FILENAME
        self.transport = FakeGetTransport(
            responses if responses is not None else [page([])]
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

    def run(self, *, confirm: str | None = None, base_url: str = BASE_URL):
        return recovery.inspect_woo_apply_pending(
            self.plan_path,
            self.plan["plan_hash"] if confirm is None else confirm,
            base_url,
            project_root=self.root,
            credential_loader=self.credential_loader,
            get_transport_factory=self.get_factory,
        )


def assert_local_exit_two(harness: Harness, result: dict[str, object]) -> None:
    assert result["status"] == "pre_write_error"
    assert result["exit_code"] == 2
    assert result["network_requests_performed"] == 0
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.factory_calls == 0
    assert harness.transport.calls == []


def test_attempting_pending_remote_zero_is_not_applied_observed(tmp_path):
    harness = Harness(tmp_path, responses=[page([])])
    pending_before = harness.pending_path.read_bytes()
    result = harness.run()
    assert result["status"] == "recovery_observation"
    assert result["decision"] == "not_applied_observed"
    assert result["result_code"] == "woo_apply_pending_remote_absent"
    assert result["exit_code"] == 3
    assert result["network_requests_performed"] == 1
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.transport.calls == [(SKU, 1, 100)]
    assert harness.pending_path.read_bytes() == pending_before


def test_attempting_pending_remote_exact_is_applied_reconcilable(tmp_path):
    harness = Harness(tmp_path, responses=[page([remote_product()])])
    pending_before = harness.pending_path.read_bytes()
    result = harness.run()
    assert result["status"] == "recovery_observation"
    assert result["decision"] == "applied_reconcilable"
    assert result["result_code"] == "woo_apply_pending_remote_exact"
    assert result["exit_code"] == 3
    assert result["product"] == {"id": PRODUCT_ID, "sku": SKU}
    assert result["source_pending"] == {
        "basename": harness.pending_path.name,
        "sha256": hashlib.sha256(pending_before).hexdigest(),
    }
    assert result["network_requests_performed"] == 1
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.pending_path.read_bytes() == pending_before


@pytest.mark.parametrize(
    "responses",
    [
        [page([remote_product(), remote_product(product_id=PRODUCT_ID + 1)])],
        [target.WooTargetSnapshotDataError("mock GET failure")],
        [page([remote_product(product_id=0)])],
    ],
    ids=("multiple", "get-error", "invalid-id"),
)
def test_remote_non_authoritative_result_requires_recovery(tmp_path, responses):
    harness = Harness(tmp_path, responses=responses)
    pending_before = harness.pending_path.read_bytes()
    result = harness.run()
    assert result["status"] == "recovery_required"
    assert result["exit_code"] == 3
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.pending_path.read_bytes() == pending_before


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
    assert result["status"] == "recovery_required"
    assert result["result_code"] == "woo_apply_pending_remote_state_inconsistent"
    assert result["exit_code"] == 3


def test_raw_and_exact_count_mismatch_requires_recovery(tmp_path, monkeypatch):
    harness = Harness(tmp_path)

    def mismatched_collect(transport, sku):
        transport.network_requests_performed += 1
        return ([{"id": PRODUCT_ID, "sku": sku}], [])

    monkeypatch.setattr(recovery.apply_core, "_collect_exact", mismatched_collect)
    result = harness.run()
    assert result["status"] == "recovery_required"
    assert result["exit_code"] == 3


def test_pending_missing_is_blocked_before_credentials(tmp_path):
    harness = Harness(tmp_path, include_pending=False)
    result = harness.run()
    assert result["status"] == "blocked"
    assert result["result_code"] == "woo_apply_pending_not_found"
    assert result["exit_code"] == 1
    assert result["network_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.transport.calls == []


@pytest.mark.parametrize(
    ("runtime_name", "content"),
    [
        (apply_core.LOCK_FILENAME, b'{"lock":"sentinel"}\n'),
        (apply_core.RECEIPT_FILENAME, b'{"receipt":"sentinel"}\n'),
    ],
    ids=("lock", "receipt"),
)
def test_existing_runtime_residue_requires_recovery_without_mutation(
    tmp_path, runtime_name, content
):
    harness = Harness(tmp_path)
    runtime_path = harness.reports / runtime_name
    runtime_path.write_bytes(content)
    pending_before = harness.pending_path.read_bytes()
    result = harness.run()
    assert result["status"] == "recovery_required"
    assert result["exit_code"] == 3
    assert result["network_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.transport.calls == []
    assert runtime_path.read_bytes() == content
    assert harness.pending_path.read_bytes() == pending_before


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(plan_hash="f" * 64),
        lambda value: value["source_plan"].update(sha256="f" * 64),
        lambda value: value["target"].update(source_host="xxxxdoll.com"),
        lambda value: value["operation"].update(sku="OTHER-SKU"),
        lambda value: value["operation"].update(payload_sha256="f" * 64),
        lambda value: value.update(manual_confirmation_verified=False),
        lambda value: value.update(write_requests_performed=True),
        lambda value: value.update(unapproved="field"),
    ],
    ids=(
        "plan-hash",
        "plan-raw-sha",
        "target",
        "sku",
        "payload-sha",
        "confirmation",
        "write-counter-bool",
        "extra-field",
    ),
)
def test_pending_cross_binding_mismatch_is_local_exit_two(
    tmp_path, mutator
):
    harness = Harness(tmp_path, pending_mutator=mutator)
    assert_local_exit_two(harness, harness.run())


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(post_state="prepared"),
        lambda value: value.update(post_attempts_started=0),
        lambda value: value.update(post_attempts_started=True),
        lambda value: value.update(post_attempts_started=2),
        lambda value: value.update(post_state="unknown"),
    ],
    ids=("prepared", "zero", "bool", "two", "unknown-state"),
)
def test_unsupported_pending_state_is_exit_three_without_get(tmp_path, mutator):
    harness = Harness(tmp_path, pending_mutator=mutator)
    result = harness.run()
    assert result["status"] == "recovery_required"
    assert result["result_code"] == "woo_apply_pending_state_not_supported"
    assert result["exit_code"] == 3
    assert result["network_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.transport.calls == []


def test_duplicate_pending_json_keys_are_local_exit_two(tmp_path):
    harness = Harness(tmp_path)
    harness.pending_path.write_text(
        '{"status":"pending","status":"pending"}', encoding="utf-8"
    )
    assert_local_exit_two(harness, harness.run())


def test_unsafe_canonical_runtime_path_fails_closed(
    tmp_path, monkeypatch
):
    harness = Harness(tmp_path)
    monkeypatch.setattr(
        recovery.apply_core,
        "_runtime_paths",
        lambda root: (_ for _ in ()).throw(
            apply_core.WooProductApplyPreWriteError("unsafe runtime path")
        ),
    )
    result = harness.run()
    assert result["status"] == "recovery_required"
    assert result["exit_code"] == 3
    assert result["network_requests_performed"] == 0
    assert harness.credential_loads == 0


def test_plan_and_pending_remain_byte_identical_and_no_artifacts_are_created(
    tmp_path
):
    harness = Harness(tmp_path, responses=[page([remote_product()])])
    plan_before = harness.plan_path.read_bytes()
    pending_before = harness.pending_path.read_bytes()
    assert harness.run()["decision"] == "applied_reconcilable"
    assert harness.plan_path.read_bytes() == plan_before
    assert harness.pending_path.read_bytes() == pending_before
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


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
    result = harness.run(base_url=base_url)
    assert_local_exit_two(harness, result)


def test_manual_plan_hash_mismatch_is_before_pending_and_credentials(tmp_path):
    harness = Harness(tmp_path)
    assert_local_exit_two(harness, harness.run(confirm="f" * 64))


def test_module_has_no_mutation_or_create_capability():
    source = inspect.getsource(recovery)
    for forbidden in (
        "StdlibWooProductCreateTransport",
        "create_product",
        "http.client",
        '"POST"',
        '"PUT"',
        '"PATCH"',
        '"DELETE"',
        ".unlink(",
        ".write_text(",
        ".write_bytes(",
        "os.remove(",
        "SafeJsonReportWriter",
        "SafeWriteAuditJsonReportWriter",
    ):
        assert forbidden not in source


def test_cli_has_only_plan_hash_and_target_authorities():
    arguments = cli.build_parser().parse_args(
        [
            "inspect-woo-apply-pending",
            "--plan-report",
            "reports/woo-apply-plan.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--base-url",
            BASE_URL,
        ]
    )
    assert arguments.command == "inspect-woo-apply-pending"
    for forbidden in (
        "pending_report",
        "receipt_report",
        "product_id",
        "sku",
        "payload",
        "force",
        "retry",
        "write",
        "cleanup",
        "production",
    ):
        assert not hasattr(arguments, forbidden)


@pytest.mark.parametrize("exit_code", [1, 2, 3])
def test_cli_preserves_pending_decision_exit_codes(exit_code, monkeypatch):
    monkeypatch.setattr(
        cli,
        "inspect_woo_apply_pending",
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
            "inspect-woo-apply-pending",
            "--plan-report",
            "plan.json",
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
    assert result["decision"] == "not_applied_observed"
    assert result["woocommerce_write_requests_performed"] == 0
