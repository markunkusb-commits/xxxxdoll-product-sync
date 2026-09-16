from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli  # noqa: E402
from sync_worker import woocommerce_apply_plan as plan  # noqa: E402
from sync_worker import woocommerce_target_snapshot as target_snapshot  # noqa: E402
from sync_worker.single_product_staging_package import (  # noqa: E402
    POLICY_VERSION as PACKAGE_POLICY_VERSION,
)
from sync_worker.woo_category_binding import STAGING_EXPECTED_HOST  # noqa: E402


SKU = "CLM-PRO-FD160CM-MERU"
PACKAGE_BASENAME = "single-product-staging-package.json"
SNAPSHOT_BASENAME = "woo-target-snapshot.json"
PACKAGE_DIGEST = "a" * 64
SNAPSHOT_DIGEST = "b" * 64


def payload(sku: str = SKU) -> dict[str, object]:
    return {
        "name": "Meru",
        "sku": sku,
        "type": "simple",
        "status": "draft",
        "regular_price": "1299.00",
        "description": "Safe public description",
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


def package(sku: str = SKU) -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": PACKAGE_POLICY_VERSION,
        "target_sku": sku,
        "future_woo_payload": payload(sku),
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


def package_source(
    *, basename: str = PACKAGE_BASENAME, digest: str = PACKAGE_DIGEST
) -> dict[str, str]:
    return {"basename": basename, "sha256": digest}


def snapshot_source() -> dict[str, str]:
    return {"basename": SNAPSHOT_BASENAME, "sha256": SNAPSHOT_DIGEST}


def snapshot(
    sku: str = SKU,
    *,
    source_package: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": target_snapshot.POLICY_VERSION,
        "target": {
            "environment": "staging",
            "source_host": STAGING_EXPECTED_HOST,
            "api_version": "wc/v3",
            "resource": "products",
            "read_only": True,
        },
        "sku": sku,
        "match_count": 0,
        "create_eligible": True,
        "existing_target": None,
        "source_package": source_package or package_source(),
        "blocking_issues": [],
        "write_authorized": False,
        "network_requests_performed": 1,
        "woocommerce_requests_performed": 1,
        "woocommerce_write_requests_performed": 0,
        "wordpress_requests_performed": 0,
        "external_write_requests_performed": 0,
        "write_requests_performed": 0,
    }


def blocked_snapshot(
    match_count: int = 1,
    *,
    source_package: dict[str, str] | None = None,
) -> dict[str, object]:
    value = snapshot(source_package=source_package)
    value["status"] = "blocked"
    value["match_count"] = match_count
    value["create_eligible"] = False
    if match_count == 1:
        value["existing_target"] = {
            "id": 123,
            "sku": SKU,
            "type": "simple",
            "status": "draft",
        }
        value["blocking_issues"] = ["woo_target_sku_already_exists"]
    else:
        value["existing_target"] = None
        value["blocking_issues"] = ["woo_target_sku_ambiguous"]
    return value


def build(
    package_value: dict[str, object] | None = None,
    snapshot_value: dict[str, object] | None = None,
    *,
    package_fingerprint: dict[str, str] | None = None,
    snapshot_fingerprint: dict[str, str] | None = None,
) -> dict[str, object]:
    return plan.build_woo_apply_plan(
        package_value or package(),
        snapshot_value or snapshot(),
        source_package=package_fingerprint or package_source(),
        source_target_snapshot=snapshot_fingerprint or snapshot_source(),
    )


def write_inputs(
    root: Path,
    *,
    package_value: dict[str, object] | None = None,
    snapshot_value: dict[str, object] | None = None,
    package_name: str = PACKAGE_BASENAME,
    snapshot_name: str = SNAPSHOT_BASENAME,
) -> tuple[Path, Path, bytes, bytes]:
    package_path = root / package_name
    package_raw = (
        json.dumps(package_value or package(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    package_path.write_bytes(package_raw)
    current_package_source = {
        "basename": package_path.name,
        "sha256": hashlib.sha256(package_raw).hexdigest(),
    }
    if snapshot_value is None:
        snapshot_value = snapshot(source_package=current_package_source)
    snapshot_path = root / snapshot_name
    snapshot_raw = (
        json.dumps(snapshot_value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    snapshot_path.write_bytes(snapshot_raw)
    return package_path, snapshot_path, package_raw, snapshot_raw


def run_local(
    root: Path,
    *,
    package_value: dict[str, object] | None = None,
    snapshot_value: dict[str, object] | None = None,
) -> tuple[dict[str, object], Path, bytes, bytes]:
    package_path, snapshot_path, package_raw, snapshot_raw = write_inputs(
        root,
        package_value=package_value,
        snapshot_value=snapshot_value,
    )
    result, output = plan.run_woo_apply_plan(
        package_path,
        snapshot_path,
        project_root=root,
    )
    return result, output, package_raw, snapshot_raw


def test_valid_package_and_snapshot_freeze_passes():
    result = build()
    assert result["status"] == "ok"
    assert result["blocking_issues"] == []
    assert result["plan_hash"] is not None


def test_output_policy_version_is_exact():
    assert build()["policy_version"] == "xxxxdoll-woo-frozen-apply-plan-v1"


def test_operation_is_create_only_with_exact_sku():
    operation = build()["operation"]
    assert operation["action"] == "create"
    assert operation["sku"] == SKU


def test_payload_is_an_exact_deep_copy():
    source = package()
    result = build(source)
    assert result["operation"]["payload"] == source["future_woo_payload"]
    assert result["operation"]["payload"] is not source["future_woo_payload"]


def test_valid_current_poc02_shaped_payload_still_freezes():
    result = build()
    assert result["status"] == "ok"
    assert result["operation"]["payload"] == payload()
    assert re.fullmatch(r"[0-9a-f]{64}", result["plan_hash"])


@pytest.mark.parametrize(
    ("case", "mutator"),
    [
        ("missing_name", lambda value: value.pop("name")),
        ("blank_name", lambda value: value.update(name="  ")),
        ("missing_regular_price", lambda value: value.pop("regular_price")),
        ("invalid_regular_price", lambda value: value.update(regular_price="1299")),
        ("missing_categories", lambda value: value.pop("categories")),
        ("empty_categories", lambda value: value.update(categories=[])),
        (
            "two_categories",
            lambda value: value.update(categories=[{"id": 1431}, {"id": 1432}]),
        ),
        ("zero_category_id", lambda value: value.update(categories=[{"id": 0}])),
        ("bool_category_id", lambda value: value.update(categories=[{"id": True}])),
        ("missing_images", lambda value: value.pop("images")),
        ("empty_images", lambda value: value.update(images=[])),
        ("zero_media_id", lambda value: value.update(images=[{"id": 0}])),
        ("bool_media_id", lambda value: value.update(images=[{"id": True}])),
        (
            "duplicate_media_id",
            lambda value: value.update(images=[{"id": 100}, {"id": 100}]),
        ),
        ("missing_attributes", lambda value: value.pop("attributes")),
        ("attributes_not_list", lambda value: value.update(attributes={})),
        (
            "malformed_attribute_keys",
            lambda value: value["attributes"][0].update(unknown="unsafe"),
        ),
        (
            "attribute_variation_true",
            lambda value: value["attributes"][0].update(variation=True),
        ),
        (
            "unapproved_attribute_name",
            lambda value: value["attributes"][0].update(name="Private Cost"),
        ),
        (
            "attribute_options_empty",
            lambda value: value["attributes"][0].update(options=[]),
        ),
        (
            "attribute_option_blank",
            lambda value: value["attributes"][0].update(options=["  "]),
        ),
        (
            "attribute_options_not_list",
            lambda value: value["attributes"][0].update(options="160 cm"),
        ),
        (
            "attribute_position_bool",
            lambda value: value["attributes"][0].update(position=True),
        ),
        (
            "attribute_position_not_canonical",
            lambda value: value["attributes"][0].update(position=1),
        ),
        (
            "description_not_string",
            lambda value: value.update(description={"html": "unsafe"}),
        ),
        (
            "short_description_not_string",
            lambda value: value.update(short_description=["unsafe"]),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_tampered_success_payload_cannot_produce_plan_hash(case, mutator):
    package_value = package()
    mutator(package_value["future_woo_payload"])
    with pytest.raises(plan.WooApplyPlanPayloadError):
        build(package_value)


def test_package_input_is_not_mutated():
    source = package()
    original = copy.deepcopy(source)
    build(source)
    assert source == original


def test_snapshot_input_is_not_mutated():
    source = snapshot()
    original = copy.deepcopy(source)
    build(snapshot_value=source)
    assert source == original


def test_same_inputs_produce_same_plan_hash_and_semantic_plan():
    first, second = build(), build()
    assert first["plan_hash"] == second["plan_hash"]
    assert plan.semantic_plan_body(first) == plan.semantic_plan_body(second)


def test_plan_hash_is_lowercase_64_character_sha256():
    digest = build()["plan_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_plan_hash_recomputes_from_final_saved_plan(tmp_path):
    result, output, _, _ = run_local(tmp_path)
    saved = json.loads(output.read_text(encoding="utf-8"))
    recomputed = plan.compute_plan_hash(plan.semantic_plan_body(saved))
    assert saved["plan_hash"] == result["plan_hash"] == recomputed


def mutated_plan_hash(mutator) -> str:
    package_value = package()
    snapshot_value = snapshot()
    package_fingerprint = package_source()
    snapshot_fingerprint = snapshot_source()
    mutator(package_value, snapshot_value, package_fingerprint, snapshot_fingerprint)
    snapshot_value["source_package"] = copy.deepcopy(package_fingerprint)
    return build(
        package_value,
        snapshot_value,
        package_fingerprint=package_fingerprint,
        snapshot_fingerprint=snapshot_fingerprint,
    )["plan_hash"]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda package_value, *_: package_value["future_woo_payload"].update(
            regular_price="1399.00"
        ),
        lambda package_value, *_: package_value["future_woo_payload"]["categories"][0].update(
            id=1432
        ),
        lambda package_value, *_: package_value["future_woo_payload"]["attributes"][0]["options"].__setitem__(
            0, "161 cm"
        ),
        lambda package_value, *_: package_value["future_woo_payload"]["images"][0].update(
            id=999
        ),
        lambda package_value, *_: package_value["future_woo_payload"].update(
            images=list(reversed(package_value["future_woo_payload"]["images"]))
        ),
        lambda _p, _s, package_fingerprint, _t: package_fingerprint.update(
            sha256="c" * 64
        ),
        lambda _p, _s, _f, snapshot_fingerprint: snapshot_fingerprint.update(
            sha256="d" * 64
        ),
    ],
)
def test_plan_hash_changes_when_authorized_semantics_change(mutator):
    assert mutated_plan_hash(mutator) != build()["plan_hash"]


def test_sku_change_changes_plan_hash():
    changed_sku = "CLM-PRO-FD161CM-MERU"
    package_value = package(changed_sku)
    snapshot_value = snapshot(changed_sku)
    assert build(package_value, snapshot_value)["plan_hash"] != build()["plan_hash"]


@pytest.mark.parametrize(
    ("field_path", "replacement"),
    [
        (("target", "environment"), "production"),
        (("target", "source_host"), "xxxxdoll.com"),
        (("target", "source_host"), "other.wpcomstaging.com"),
        (("target", "api_version"), "wc/v2"),
        (("target", "resource"), "orders"),
        (("target", "read_only"), False),
    ],
)
def test_invalid_target_identity_fails_closed(field_path, replacement):
    source = snapshot()
    source[field_path[0]][field_path[1]] = replacement
    with pytest.raises(plan.WooApplyPlanInputError):
        build(snapshot_value=source)


def test_package_raw_sha_mismatch_with_snapshot_fails_closed():
    source = snapshot()
    source["source_package"]["sha256"] = "f" * 64
    with pytest.raises(plan.WooApplyPlanInputError) as caught:
        build(snapshot_value=source)
    assert str(caught.value) == "woo_apply_plan_package_snapshot_mismatch"


def test_package_basename_mismatch_with_snapshot_fails_closed():
    source = snapshot()
    source["source_package"]["basename"] = "other-package.json"
    with pytest.raises(plan.WooApplyPlanInputError) as caught:
        build(snapshot_value=source)
    assert str(caught.value) == "woo_apply_plan_package_snapshot_mismatch"


def test_snapshot_sku_mismatch_fails_closed():
    source = snapshot("CLM-PRO-OTHER")
    with pytest.raises(plan.WooApplyPlanInputError):
        build(snapshot_value=source)


def test_snapshot_policy_version_mismatch_fails_closed():
    source = snapshot()
    source["policy_version"] = "old-policy"
    with pytest.raises(plan.WooApplyPlanInputError):
        build(snapshot_value=source)


@pytest.mark.parametrize("match_count", [1, 2, 7])
def test_legal_business_block_has_no_hash_or_operation(match_count):
    result = build(snapshot_value=blocked_snapshot(match_count))
    assert result["status"] == "blocked"
    assert result["plan_hash"] is None
    assert result["operation"] is None
    assert result["write_authorized"] is False


def test_create_eligible_false_does_not_freeze_a_hash():
    result = build(snapshot_value=blocked_snapshot(1))
    assert result["preconditions"]["create_eligible"] is False
    assert result["plan_hash"] is None


def test_existing_target_on_create_eligible_snapshot_is_contract_error():
    source = snapshot()
    source["existing_target"] = {"id": 123, "sku": SKU}
    with pytest.raises(plan.WooApplyPlanInputError):
        build(snapshot_value=source)


@pytest.mark.parametrize(
    "counter",
    [
        "woocommerce_write_requests_performed",
        "wordpress_requests_performed",
        "external_write_requests_performed",
        "write_requests_performed",
    ],
)
def test_snapshot_nonzero_write_counter_fails_closed(counter):
    source = snapshot()
    source[counter] = 1
    with pytest.raises(plan.WooApplyPlanInputError):
        build(snapshot_value=source)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("network_requests_performed", 0),
        ("network_requests_performed", True),
        ("network_requests_performed", "1"),
        ("woocommerce_requests_performed", 2),
        ("woocommerce_requests_performed", True),
    ],
)
def test_snapshot_invalid_request_counter_fails_closed(field, value):
    source = snapshot()
    source[field] = value
    with pytest.raises(plan.WooApplyPlanInputError):
        build(snapshot_value=source)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        Path("https://example.test/snapshot.json"),
        Path("file:///tmp/snapshot.json"),
        Path(r"\\server\share\snapshot.json"),
        Path("snapshot.txt"),
    ],
)
def test_snapshot_url_unc_and_non_json_paths_fail_closed(unsafe_path):
    with pytest.raises(plan.WooApplyPlanInputError):
        plan._read_target_snapshot(unsafe_path)


@pytest.mark.parametrize(
    "basename",
    ["password.json", "credential-report.json", "secret-token.json", ".env.json"],
)
def test_snapshot_sensitive_basename_fails_closed(tmp_path, basename):
    path = tmp_path / basename
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(plan.WooApplyPlanInputError):
        plan._read_target_snapshot(path)


def test_snapshot_symlink_or_reparse_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / SNAPSHOT_BASENAME
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        target_snapshot.package_io,
        "_has_link_or_reparse",
        lambda _: True,
    )
    with pytest.raises(plan.WooApplyPlanInputError):
        plan._read_target_snapshot(path)


def test_snapshot_post_abspath_unc_fails_closed_without_share_access(
    tmp_path, monkeypatch
):
    path = tmp_path / SNAPSHOT_BASENAME
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        target_snapshot.package_io.os.path,
        "abspath",
        lambda _: r"\\server\share\woo-target-snapshot.json",
    )
    with pytest.raises(plan.WooApplyPlanInputError):
        plan._read_target_snapshot(path)


def test_snapshot_duplicate_json_key_fails_closed(tmp_path):
    path = tmp_path / SNAPSHOT_BASENAME
    path.write_text('{"status":"ok","status":"blocked"}', encoding="utf-8")
    with pytest.raises(plan.WooApplyPlanInputError) as caught:
        plan._read_target_snapshot(path)
    assert str(caught.value) == "woo_apply_plan_snapshot_duplicate_json_key"


def test_raw_package_and_snapshot_sha256_are_exact(tmp_path):
    result, _, package_raw, snapshot_raw = run_local(tmp_path)
    assert result["source_package"] == {
        "basename": PACKAGE_BASENAME,
        "sha256": hashlib.sha256(package_raw).hexdigest(),
    }
    assert result["source_target_snapshot"] == {
        "basename": SNAPSHOT_BASENAME,
        "sha256": hashlib.sha256(snapshot_raw).hexdigest(),
    }


def test_absolute_paths_never_enter_plan(tmp_path):
    result, output, _, _ = run_local(tmp_path)
    text = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert result["source_package"]["basename"] == PACKAGE_BASENAME
    assert result["source_target_snapshot"]["basename"] == SNAPSHOT_BASENAME


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audit", {"secret": "value"}),
        ("storefront_options", []),
        ("supplier_costs", {"fob": "100"}),
        ("fx", "0.15"),
        ("margin", "0.5"),
        ("source_rows", [1, 2]),
        ("permalink", "https://example.test/product"),
        ("consumer_secret", "cs_test_secret_value"),
    ],
)
def test_internal_audit_cost_fx_url_and_credentials_cannot_enter_payload(field, value):
    source = package()
    source["future_woo_payload"][field] = value
    with pytest.raises(plan.WooApplyPlanPayloadError):
        build(source)


def test_final_plan_contains_no_credentials_or_full_url():
    text = json.dumps(build(), sort_keys=True)
    forbidden = (
        "https://",
        "Authorization",
        "Cookie",
        "consumer_key",
        "consumer_secret",
        "password",
        "token",
        "ck_",
        "cs_",
    )
    assert all(value not in text for value in forbidden)


def test_write_authorization_false_and_all_runtime_counters_zero():
    result = build()
    assert result["write_authorized"] is False
    for counter in plan._ZERO_COUNTERS:
        assert result[counter] == 0


def test_cli_has_only_two_authority_arguments_and_no_confirmation():
    arguments = cli.build_parser().parse_args(
        [
            "freeze-woo-apply-plan",
            "--package-report",
            PACKAGE_BASENAME,
            "--target-snapshot",
            SNAPSHOT_BASENAME,
        ]
    )
    assert arguments.command == "freeze-woo-apply-plan"
    assert not hasattr(arguments, "sku")
    assert not hasattr(arguments, "base_url")
    assert not hasattr(arguments, "confirm_plan_hash")


def test_cli_success_is_local_and_does_not_load_credentials_or_construct_client(
    tmp_path, monkeypatch
):
    package_path, snapshot_path, _, _ = write_inputs(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        cli,
        "load_woo_category_credential_source",
        lambda: (_ for _ in ()).throw(AssertionError("credentials forbidden")),
    )
    monkeypatch.setattr(
        target_snapshot,
        "StdlibWooProductTargetTransport",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network client forbidden")
        ),
    )
    assert cli.main(
        [
            "freeze-woo-apply-plan",
            "--package-report",
            str(package_path),
            "--target-snapshot",
            str(snapshot_path),
        ]
    ) == 0


def test_cli_blocked_snapshot_returns_one_and_writes_non_actionable_plan(
    tmp_path, monkeypatch
):
    package_path = tmp_path / PACKAGE_BASENAME
    package_raw = (json.dumps(package(), sort_keys=True) + "\n").encode()
    package_path.write_bytes(package_raw)
    source = {
        "basename": PACKAGE_BASENAME,
        "sha256": hashlib.sha256(package_raw).hexdigest(),
    }
    snapshot_path = tmp_path / SNAPSHOT_BASENAME
    snapshot_path.write_text(
        json.dumps(blocked_snapshot(1, source_package=source), sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(
        [
            "freeze-woo-apply-plan",
            "--package-report",
            str(package_path),
            "--target-snapshot",
            str(snapshot_path),
        ]
    ) == 1
    saved = json.loads(
        (tmp_path / "reports" / plan.REPORT_FILENAME).read_text(encoding="utf-8")
    )
    assert saved["plan_hash"] is None
    assert saved["operation"] is None


def test_cli_contract_error_returns_two_and_preserves_prior_plan(
    tmp_path, monkeypatch
):
    package_path, snapshot_path, _, _ = write_inputs(tmp_path)
    snapshot_value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_value["source_package"]["sha256"] = "f" * 64
    snapshot_path.write_text(json.dumps(snapshot_value), encoding="utf-8")
    output = tmp_path / "reports" / plan.REPORT_FILENAME
    output.parent.mkdir()
    original = b'{"status":"ok","sentinel":true}\n'
    output.write_bytes(original)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(
        [
            "freeze-woo-apply-plan",
            "--package-report",
            str(package_path),
            "--target-snapshot",
            str(snapshot_path),
        ]
    ) == 2
    assert output.read_bytes() == original


def test_package_and_snapshot_path_collision_is_rejected(tmp_path):
    package_path = tmp_path / PACKAGE_BASENAME
    package_path.write_text(json.dumps(package()), encoding="utf-8")
    with pytest.raises(plan.WooApplyPlanInputError):
        plan.run_woo_apply_plan(
            package_path,
            package_path,
            project_root=tmp_path,
        )


def test_output_reparse_path_is_rejected(tmp_path, monkeypatch):
    package_path, snapshot_path, _, _ = write_inputs(tmp_path)
    original = package_io_has_link = plan.package_io._has_link_or_reparse

    def linked_output(path):
        if Path(path).name == plan.REPORT_FILENAME:
            return True
        return package_io_has_link(path)

    monkeypatch.setattr(plan.package_io, "_has_link_or_reparse", linked_output)
    with pytest.raises(plan.WooApplyPlanInputError):
        plan.run_woo_apply_plan(
            package_path,
            snapshot_path,
            project_root=tmp_path,
        )
    monkeypatch.setattr(plan.package_io, "_has_link_or_reparse", original)


def test_module_has_no_credentials_network_apply_or_write_transport_capability():
    source = Path(plan.__file__).read_text(encoding="utf-8")
    forbidden = (
        "load_woo_category_credential_source",
        "load_woo_category_credentials",
        "StdlibWooProductTargetTransport",
        "http.client",
        'request("POST"',
        'request("PUT"',
        'request("PATCH"',
        'request("DELETE"',
        "confirm_plan_hash",
        "pending_journal",
        "receipt",
        "ledger",
    )
    assert all(token not in source for token in forbidden)
    assert not hasattr(plan, "apply_woo_plan")


def test_plan_hash_excludes_report_metadata_but_covers_every_semantic_field():
    result = build()
    semantic = plan.semantic_plan_body(result)
    baseline = plan.compute_plan_hash(semantic)
    metadata_change = copy.deepcopy(result)
    metadata_change["status"] = "changed"
    metadata_change["write_authorized"] = True
    metadata_change["network_requests_performed"] = 999
    assert plan.compute_plan_hash(plan.semantic_plan_body(metadata_change)) == baseline
    for field in plan._SEMANTIC_FIELDS:
        changed = copy.deepcopy(semantic)
        changed[field] = {"changed": field}
        assert plan.compute_plan_hash(changed) != baseline
