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
from sync_worker import woocommerce_apply_idempotency as idempotency  # noqa: E402
from sync_worker import woocommerce_apply_plan as freeze  # noqa: E402
from sync_worker import woocommerce_pending_reconciliation as reconciliation  # noqa: E402
from sync_worker import woocommerce_product_apply as apply_core  # noqa: E402
from sync_worker import woocommerce_target_snapshot as target  # noqa: E402
from sync_worker.report import SafeWriteAuditJsonReportWriter  # noqa: E402
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
        self.pending_sha256 = hashlib.sha256(self.pending_raw).hexdigest()
        if include_pending:
            self.pending_path.write_bytes(self.pending_raw)
        self.receipt_path = self.reports / apply_core.RECEIPT_FILENAME
        self.lock_path = self.reports / apply_core.LOCK_FILENAME
        self.transport = FakeGetTransport(
            responses
            if responses is not None
            else [page([remote_product()])]
        )
        self.credential_loads = 0
        self.factory_calls = 0
        self.receipt_writer = reconciliation._default_receipt_writer
        self.lock_acquirer = apply_core._acquire_lock
        self.unlinker = reconciliation._default_unlinker
        self.after_lock_hook = None
        self.after_get_hook = None
        self.before_pending_cleanup_hook = None

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
        plan_confirm: str | None = None,
        pending_confirm: str | None = None,
        base_url: str = BASE_URL,
    ):
        return reconciliation.reconcile_woo_apply_pending(
            self.plan_path,
            self.plan["plan_hash"] if plan_confirm is None else plan_confirm,
            self.pending_sha256 if pending_confirm is None else pending_confirm,
            base_url,
            project_root=self.root,
            credential_loader=self.credential_loader,
            get_transport_factory=self.get_factory,
            receipt_writer=self.receipt_writer,
            lock_acquirer=self.lock_acquirer,
            unlinker=self.unlinker,
            after_lock_hook=self.after_lock_hook,
            after_get_hook=self.after_get_hook,
            before_pending_cleanup_hook=self.before_pending_cleanup_hook,
        )


def read_receipt(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_pre_lock_failure(harness: Harness, result: dict[str, object], exit_code=2):
    assert result["exit_code"] == exit_code
    assert result["network_requests_performed"] == 0
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.credential_loads == 0
    assert harness.factory_calls == 0
    assert harness.transport.calls == []
    assert not harness.lock_path.exists()


def test_exact_remote_reconciles_and_cleans_pending_and_owned_lock(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result["status"] == "reconciled"
    assert result["result_code"] == "woo_apply_pending_reconciled"
    assert result["exit_code"] == 0
    assert result["product"] == {"id": PRODUCT_ID, "sku": SKU}
    assert result["source_pending"] == {
        "basename": apply_core.PENDING_FILENAME,
        "sha256": harness.pending_sha256,
    }
    assert result["network_requests_performed"] == 1
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.receipt_path.exists()
    assert not harness.pending_path.exists()
    assert not harness.lock_path.exists()


def test_recovery_receipt_exact_schema_and_semantics(tmp_path):
    harness = Harness(tmp_path)
    assert harness.run()["exit_code"] == 0
    receipt = read_receipt(harness.receipt_path)
    assert set(receipt) == reconciliation._RECOVERY_RECEIPT_FIELDS
    assert receipt["policy_version"] == (
        reconciliation.RECOVERY_RECEIPT_POLICY_VERSION
    )
    assert receipt["source_pending"] == {
        "basename": apply_core.PENDING_FILENAME,
        "sha256": harness.pending_sha256,
    }
    assert receipt["network_requests_performed"] == 1
    assert receipt["woocommerce_requests_performed"] == 1
    assert receipt["woocommerce_write_requests_performed"] == 0
    assert receipt["wordpress_requests_performed"] == 0
    assert receipt["external_write_requests_performed"] == 0
    assert receipt["write_requests_performed"] == 0
    assert receipt["reconciliation"] == {
        "kind": "pending_attempting_remote_exact",
        "pending_post_state": "attempting",
        "pending_post_attempts_started": 1,
        "original_post_outcome": "unknown",
        "create_retry_performed": False,
    }
    assert reconciliation.validate_recovery_receipt(
        receipt,
        plan_source=harness.plan_source,
        plan_hash=harness.plan["plan_hash"],
        sku=SKU,
        payload=frozen_payload(),
        expected_pending_source=receipt["source_pending"],
    ) == PRODUCT_ID


def test_recovery_receipt_is_accepted_by_idempotency_verifier(tmp_path):
    harness = Harness(tmp_path)
    assert harness.run()["exit_code"] == 0
    verify_transport = FakeGetTransport([page([remote_product()])])
    result = idempotency.run_woo_apply_receipt_verification(
        harness.plan_path,
        harness.receipt_path,
        harness.plan["plan_hash"],
        BASE_URL,
        project_root=tmp_path,
        credential_loader=harness.credential_loader,
        get_transport_factory=lambda base_url, credentials: verify_transport,
    )
    assert result["status"] == "already_applied"
    assert result["exit_code"] == 0
    assert result["woocommerce_write_requests_performed"] == 0


@pytest.mark.parametrize(
    "pending_confirm",
    ["f" * 64, "not-a-sha", "A" * 64],
    ids=("mismatch", "malformed", "uppercase"),
)
def test_pending_confirmation_failure_is_before_lock_credentials_and_get(
    tmp_path, pending_confirm
):
    harness = Harness(tmp_path)
    result = harness.run(pending_confirm=pending_confirm)
    assert result["result_code"] == "woo_apply_pending_confirmation_mismatch"
    assert_pre_lock_failure(harness, result)


def test_plan_hash_mismatch_is_before_lock(tmp_path):
    harness = Harness(tmp_path)
    assert_pre_lock_failure(harness, harness.run(plan_confirm="f" * 64))


def test_pending_contract_mismatch_is_before_lock(tmp_path):
    harness = Harness(
        tmp_path,
        pending_mutator=lambda value: value["operation"].update(sku="OTHER"),
    )
    assert_pre_lock_failure(harness, harness.run())


def test_prepared_pending_is_exit_three_before_lock(tmp_path):
    harness = Harness(
        tmp_path,
        pending_mutator=lambda value: value.update(
            post_state="prepared", post_attempts_started=0
        ),
    )
    result = harness.run()
    assert result["result_code"] == "woo_apply_pending_state_not_supported"
    assert_pre_lock_failure(harness, result, exit_code=3)


@pytest.mark.parametrize("artifact", ["lock", "receipt"])
def test_existing_lock_or_receipt_is_unchanged_and_blocks(tmp_path, artifact):
    harness = Harness(tmp_path)
    path = harness.lock_path if artifact == "lock" else harness.receipt_path
    content = json_bytes({"sentinel": artifact})
    path.write_bytes(content)
    result = harness.run()
    assert result["exit_code"] == 3
    assert result["network_requests_performed"] == 0
    assert path.read_bytes() == content
    assert harness.credential_loads == 0


def test_pending_missing_is_exit_one(tmp_path):
    harness = Harness(tmp_path, include_pending=False)
    result = harness.run()
    assert result["result_code"] == "woo_apply_pending_not_found"
    assert_pre_lock_failure(harness, result, exit_code=1)


def test_pending_change_after_lock_releases_owned_lock_without_get(tmp_path):
    harness = Harness(tmp_path)
    changed = json_bytes({**harness.pending, "post_attempts_started": 2})
    harness.after_lock_hook = lambda: harness.pending_path.write_bytes(changed)
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.credential_loads == 0
    assert harness.transport.calls == []
    assert harness.pending_path.read_bytes() == changed
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_receipt_appears_after_lock_is_not_overwritten(tmp_path):
    harness = Harness(tmp_path)
    foreign = json_bytes({"foreign": "receipt"})
    harness.after_lock_hook = lambda: harness.receipt_path.write_bytes(foreign)
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.receipt_path.read_bytes() == foreign
    assert harness.credential_loads == 0
    assert not harness.lock_path.exists()


def test_exclusive_lock_creation_race_leaves_foreign_lock(tmp_path):
    harness = Harness(tmp_path)
    foreign = json_bytes({"foreign": "lock"})

    def losing_race(path, value):
        path.write_bytes(foreign)
        raise FileExistsError("mock race")

    harness.lock_acquirer = losing_race
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.lock_path.read_bytes() == foreign
    assert harness.credential_loads == 0


@pytest.mark.parametrize("failure", ["credentials", "factory"])
def test_get_setup_failure_preserves_pending_and_releases_lock(tmp_path, failure):
    harness = Harness(tmp_path)
    pending_before = harness.pending_path.read_bytes()
    if failure == "credentials":
        harness.credential_loader = lambda: (_ for _ in ()).throw(
            ValueError("mock credentials failure")
        )
    else:
        harness.get_factory = lambda *args: (_ for _ in ()).throw(
            ValueError("mock factory failure")
        )
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.read_bytes() == pending_before
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


@pytest.mark.parametrize(
    "responses",
    [
        [page([])],
        [page([remote_product(), remote_product(product_id=PRODUCT_ID + 1)])],
        [target.WooTargetSnapshotDataError("mock GET failure")],
        [page([remote_product(product_id=0)])],
    ],
    ids=("zero", "multiple", "get-error", "invalid-id"),
)
def test_remote_unresolved_preserves_pending_and_creates_no_receipt(
    tmp_path, responses
):
    harness = Harness(tmp_path, responses=responses)
    pending_before = harness.pending_path.read_bytes()
    result = harness.run()
    assert result["exit_code"] == 3
    assert result["woocommerce_write_requests_performed"] == 0
    assert harness.pending_path.read_bytes() == pending_before
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(name="Changed"),
        lambda value: value.update(categories=[{"id": 1432}]),
        lambda value: value["attributes"][0].update(options=["Changed"]),
        lambda value: value.update(images=list(reversed(value["images"]))),
    ],
    ids=("payload", "category", "attribute", "image-order"),
)
def test_remote_projection_mismatch_preserves_pending(tmp_path, mutator):
    remote = remote_product()
    mutator(remote)
    harness = Harness(tmp_path, responses=[page([remote])])
    result = harness.run()
    assert result["result_code"] == (
        "woo_apply_reconciliation_remote_state_inconsistent"
    )
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert not harness.receipt_path.exists()


def test_pending_change_after_get_prevents_receipt_write(tmp_path):
    harness = Harness(tmp_path)
    changed = json_bytes({**harness.pending, "post_attempts_started": 2})
    harness.after_get_hook = lambda: harness.pending_path.write_bytes(changed)
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.read_bytes() == changed
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_owned_lock_change_is_never_deleted(tmp_path):
    harness = Harness(tmp_path)
    foreign = json_bytes({"foreign": "replacement-lock"})
    harness.after_get_hook = lambda: harness.lock_path.write_bytes(foreign)
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.lock_path.read_bytes() == foreign
    assert harness.pending_path.exists()
    assert not harness.receipt_path.exists()


def test_receipt_write_failure_preserves_pending_and_releases_lock(tmp_path):
    harness = Harness(tmp_path)
    harness.receipt_writer = lambda *args: (_ for _ in ()).throw(
        OSError("mock receipt failure")
    )
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_invalid_persisted_receipt_preserves_pending(tmp_path):
    harness = Harness(tmp_path)
    harness.receipt_writer = lambda path, value, redactor: path.write_text(
        '{}', encoding="utf-8"
    )
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_pending_change_during_receipt_write_preserves_both(tmp_path):
    harness = Harness(tmp_path)

    def writer(path, value, redactor):
        SafeWriteAuditJsonReportWriter(path, redactor).write(value)
        harness.pending_path.write_bytes(
            json_bytes({**harness.pending, "post_attempts_started": 2})
        )

    harness.receipt_writer = writer
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_lock_replaced_after_receipt_validation_preserves_pending_and_foreign_lock(
    tmp_path,
):
    harness = Harness(tmp_path)
    foreign = json_bytes({"foreign": "replacement-lock"})
    unlink_calls: list[Path] = []

    def replace_lock():
        harness.lock_path.write_bytes(foreign)

    def unlinker(path):
        unlink_calls.append(path)
        path.unlink()

    harness.before_pending_cleanup_hook = replace_lock
    harness.unlinker = unlinker
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert harness.receipt_path.exists()
    assert harness.lock_path.read_bytes() == foreign
    assert harness.lock_path not in unlink_calls


def test_receipt_replaced_after_first_validation_preserves_pending(tmp_path):
    harness = Harness(tmp_path)
    foreign = json_bytes({"foreign": "replacement-receipt"})
    harness.before_pending_cleanup_hook = lambda: harness.receipt_path.write_bytes(
        foreign
    )
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert harness.receipt_path.read_bytes() == foreign
    assert not harness.lock_path.exists()


def test_receipt_deleted_after_first_validation_preserves_pending(tmp_path):
    harness = Harness(tmp_path)
    harness.before_pending_cleanup_hook = harness.receipt_path.unlink
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.pending_path.exists()
    assert not harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_semantically_identical_receipt_with_different_raw_bytes_fails_closed(
    tmp_path,
):
    harness = Harness(tmp_path)

    def rewrite_receipt():
        receipt = read_receipt(harness.receipt_path)
        rewritten = json.dumps(
            receipt,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        assert rewritten != harness.receipt_path.read_bytes()
        harness.receipt_path.write_bytes(rewritten)

    harness.before_pending_cleanup_hook = rewrite_receipt
    result = harness.run()
    assert result["exit_code"] == 3
    assert result["result_code"] == (
        "woo_apply_reconciliation_pre_cleanup_state_changed"
    )
    assert harness.pending_path.exists()
    assert harness.receipt_path.exists()
    assert not harness.lock_path.exists()


def test_pending_delete_failure_keeps_receipt_and_pending(tmp_path):
    harness = Harness(tmp_path)

    def unlinker(path):
        if path == harness.pending_path:
            raise OSError("mock pending failure")
        path.unlink()

    harness.unlinker = unlinker
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.receipt_path.exists()
    assert harness.pending_path.exists()
    assert not harness.lock_path.exists()


def test_lock_delete_failure_keeps_receipt_and_lock_after_pending_cleanup(tmp_path):
    harness = Harness(tmp_path)

    def unlinker(path):
        if path == harness.lock_path:
            raise OSError("mock lock failure")
        path.unlink()

    harness.unlinker = unlinker
    result = harness.run()
    assert result["exit_code"] == 3
    assert harness.receipt_path.exists()
    assert not harness.pending_path.exists()
    assert harness.lock_path.exists()


def test_commit_order_is_receipt_validate_then_pending_then_lock(
    tmp_path, monkeypatch
):
    harness = Harness(tmp_path)
    events: list[str] = []
    original_reader = reconciliation._read_recovery_receipt_with_source

    def writer(path, value, redactor):
        events.append("receipt_write")
        SafeWriteAuditJsonReportWriter(path, redactor).write(value)

    def reader(path):
        events.append("receipt_read")
        return original_reader(path)

    def unlinker(path):
        events.append("pending_delete" if path == harness.pending_path else "lock_delete")
        path.unlink()

    harness.receipt_writer = writer
    harness.unlinker = unlinker
    monkeypatch.setattr(
        reconciliation,
        "_read_recovery_receipt_with_source",
        reader,
    )
    assert harness.run()["exit_code"] == 0
    assert events[:5] == [
        "receipt_write",
        "receipt_read",
        "receipt_read",
        "pending_delete",
        "lock_delete",
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://xxxxdoll.com",
        f"https://{STAGING_EXPECTED_HOST}:443",
        f"https://{STAGING_EXPECTED_HOST}/shop",
        f"https://{STAGING_EXPECTED_HOST}?query=1",
    ],
)
def test_invalid_target_is_before_lock_and_credentials(tmp_path, base_url):
    harness = Harness(tmp_path)
    result = harness.run(base_url=base_url)
    assert_pre_lock_failure(harness, result)


@pytest.mark.parametrize(
    (
        "mutator",
        "expected_exit_code",
        "expected_network_requests",
        "expected_credential_loads",
    ),
    [
        (lambda value: value.update(policy_version="wrong-policy"), 2, 0, 0),
        (lambda value: value["product"].update(id=PRODUCT_ID + 1), 3, 1, 1),
        (
            lambda value: value["source_plan"].update(sha256="f" * 64),
            2,
            0,
            0,
        ),
        (
            lambda value: value["source_pending"].update(sha256="not-a-sha"),
            2,
            0,
            0,
        ),
        (
            lambda value: value.update(woocommerce_write_requests_performed=1),
            2,
            0,
            0,
        ),
        (
            lambda value: value["reconciliation"].update(
                pending_post_attempts_started=True
            ),
            2,
            0,
            0,
        ),
        (
            lambda value: value["reconciliation"].update(
                create_retry_performed=0
            ),
            2,
            0,
            0,
        ),
    ],
    ids=(
        "policy",
        "product-id",
        "plan-source",
        "pending-source",
        "write-counter",
        "attempts-bool",
        "retry-bool",
    ),
)
def test_invalid_recovery_receipt_fails_idempotency_closed(
    tmp_path,
    mutator,
    expected_exit_code,
    expected_network_requests,
    expected_credential_loads,
):
    harness = Harness(tmp_path)
    assert harness.run()["exit_code"] == 0
    receipt = read_receipt(harness.receipt_path)
    mutator(receipt)
    harness.receipt_path.write_bytes(json_bytes(receipt))
    credential_loads = 0

    def credential_loader():
        nonlocal credential_loads
        credential_loads += 1
        return CREDENTIALS, apply_core.redactor_for_woo_category_credentials(
            CREDENTIALS
        )

    result = idempotency.run_woo_apply_receipt_verification(
        harness.plan_path,
        harness.receipt_path,
        harness.plan["plan_hash"],
        BASE_URL,
        project_root=tmp_path,
        credential_loader=credential_loader,
        get_transport_factory=lambda *args: FakeGetTransport(
            [page([remote_product()])]
        ),
    )
    assert result["exit_code"] == expected_exit_code
    assert result["network_requests_performed"] == expected_network_requests
    assert credential_loads == expected_credential_loads


def test_module_has_no_remote_write_or_create_capability():
    source = inspect.getsource(reconciliation)
    for forbidden in (
        "StdlibWooProductCreateTransport",
        "create_product",
        "http.client",
        '"POST"',
        '"PUT"',
        '"PATCH"',
        '"DELETE"',
        "retry CREATE",
    ):
        assert forbidden not in source


def test_cli_requires_both_manual_confirmations_and_no_override_parameters():
    arguments = cli.build_parser().parse_args(
        [
            "reconcile-woo-apply-pending",
            "--plan-report",
            "reports/woo-apply-plan.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--confirm-pending-sha256",
            "b" * 64,
            "--base-url",
            BASE_URL,
        ]
    )
    assert arguments.command == "reconcile-woo-apply-pending"
    for forbidden in (
        "pending_report",
        "receipt_report",
        "decision_report",
        "product_id",
        "sku",
        "payload",
        "force",
        "retry",
        "write",
        "cleanup",
        "delete",
        "production",
    ):
        assert not hasattr(arguments, forbidden)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_cli_preserves_reconciliation_exit_codes(exit_code, monkeypatch):
    monkeypatch.setattr(
        cli,
        "reconcile_woo_apply_pending",
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
            "reconcile-woo-apply-pending",
            "--plan-report",
            "plan.json",
            "--confirm-plan-hash",
            "a" * 64,
            "--confirm-pending-sha256",
            "b" * 64,
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
