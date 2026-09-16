from __future__ import annotations

import copy
import json
import socket
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli  # noqa: E402
from sync_worker import single_product_staging_package as core  # noqa: E402
from sync_worker import single_product_staging_package_dry_run as dry_run  # noqa: E402
from sync_worker import woo_category_binding  # noqa: E402
from sync_worker.image_selection_policy import (  # noqa: E402
    MAX_IMAGES_PER_SKU,
    POLICY_VERSION as SELECTION_POLICY_VERSION,
)
from sync_worker.sku_policy import SKU_POLICY_VERSION  # noqa: E402
from sync_worker.wordpress_media_upload_execution import (  # noqa: E402
    POLICY_VERSION as MEDIA_POLICY_VERSION,
)


SKU = "CLM-PRO-FD160CM-MERU"
SOURCE_NAMES = {
    "woocommerce_payload": "woocommerce-payload-dry-run.json",
    "sku": "sku-dry-run.json",
    "image_selection": "image-selection-dry-run.json",
    "wordpress_media": "wordpress-media-upload-execution.json",
    "woo_category_discovery": "woo-category-discovery.json",
}


@pytest.fixture(autouse=True)
def deny_external_access(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("local package tests must remain offline")

    for target in (socket.socket,):
        monkeypatch.setattr(target, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in (
        "load_config",
        "load_google_config",
        "load_google_drive_metadata_config",
        "load_google_sheets_readonly_config",
        "load_woo_category_credential_source",
    ):
        monkeypatch.setattr(cli, name, denied)
    monkeypatch.setattr(cli, "OfficialGoogleClientFactory", denied)
    monkeypatch.setattr(cli, "ReadOnlyHttpClient", denied)
    monkeypatch.setattr(cli, "StdlibWordPressMediaHttpTransport", denied)


def payload_report() -> dict[str, object]:
    return {
        "status": "ok",
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "candidates": [
            {
                "payload": {
                    "name": "FD160cm-Meru",
                    "sku": SKU,
                    "type": "simple",
                    "status": "draft",
                    "regular_price": "1399.00",
                    "attributes": [
                        {
                            "name": "Height",
                            "position": 0,
                            "options": ["160cm"],
                            "visible": True,
                            "variation": False,
                        }
                    ],
                    "categories": [{"id": 1431}],
                },
                "api": {
                    "version": "wc/v3",
                    "resource": "products",
                    "intended_method": "POST",
                    "write_performed": False,
                },
                "storefront_options": [{
                    "name": "Mock Option",
                    "price_usd": "19.00",
                    "option_type": "paid_upgrade",
                }],
                "audit": {
                    "category": {
                        "internal_registry_version": "clm-category-map-v1",
                        "internal_category_key": "clm-pro",
                        "binding_profile_version": woo_category_binding.STAGING_BINDING_PROFILE_VERSION,
                        "environment": woo_category_binding.STAGING_ENVIRONMENT,
                        "target_host": woo_category_binding.STAGING_EXPECTED_HOST,
                        "woo_category_id": 1431,
                        "verified_name": "Realistic sex dolls",
                        "binding_status": "bound_verified",
                        "host_verified": True,
                        "discovery_verified": True,
                    },
                    "pricing": [],
                    "supplier_costs": {"fob_price": "RMB 1"},
                    "fx_rate": "0.15",
                    "margin": "internal",
                },
                "public_content": {},
                "warnings": [
                    "images_not_mapped",
                    "customer_description_not_generated",
                ],
                "blocking_issues": [],
                "ready_for_write": False,
            }
        ],
    }


def sku_report() -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": SKU_POLICY_VERSION,
        "network_requests_performed": 0,
        "write_requests_performed": 0,
        "results": [
            {
                "sku": SKU,
                "status": "ok",
                "policy_version": SKU_POLICY_VERSION,
                "blocking_issues": [],
                "warnings": [],
            }
        ],
    }


def selection_item(position: int, role: str) -> dict[str, object]:
    return {
        "sku": SKU,
        "safe_name": f"private-file-{position}.jpg",
        "selected": True,
        "selection_position": position,
        "image_role": role,
        "blocking_issues": [],
        "warnings": [],
    }


def selection_report(count: int = 3) -> dict[str, object]:
    items = [
        selection_item(position, "primary" if position == 0 else "gallery")
        for position in range(count)
    ]
    return {
        "status": "ok",
        "policy_version": SELECTION_POLICY_VERSION,
        "network_requests_performed": 0,
        "download_requests_performed": 0,
        "write_requests_performed": 0,
        "results": [
            {
                "sku": SKU,
                "selected_count": count,
                "primary_count": 1 if count else 0,
                "gallery_count": max(0, count - 1),
                "blocking_issues": [],
                "warnings": [],
                "items": items,
            }
        ],
    }


def media_result(position: int, role: str, media_id: int) -> dict[str, object]:
    return {
        "sku": SKU,
        "selection_position": position,
        "image_role": role,
        "wordpress_media_id": media_id,
        "upload_status": "reused" if position == 0 else "created",
        "upload_filename": f"must-not-leak-{position}.webp",
        "media_identity": "must-not-leak",
        "blocking_issues": [],
        "warnings": [],
    }


def media_report() -> dict[str, object]:
    return {
        "status": "ok",
        "policy_version": MEDIA_POLICY_VERSION,
        "woocommerce_requests_performed": 0,
        "woocommerce_write_requests_performed": 0,
        "delete_requests_performed": 0,
        "rollback_requests_performed": 0,
        "write_requests_performed": 2,
        "webp_cleanup_completed": True,
        "source_cleanup_completed": True,
        "webp_files_remaining": 0,
        "source_files_remaining": 0,
        "results": [
            media_result(2, "gallery", 700),
            media_result(0, "primary", 900),
            media_result(1, "gallery", 800),
        ],
    }


def category_report() -> dict[str, object]:
    return {
        "status": "ok",
        "source_host": woo_category_binding.STAGING_EXPECTED_HOST,
        "network_requests_performed": 3,
        "write_requests_performed": 0,
        "categories": [
            {"id": 1431, "name": "Realistic sex dolls"},
            {"id": 1432, "name": "Silicone sex dolls"},
        ],
    }


def reports() -> list[dict[str, object]]:
    return [
        payload_report(),
        sku_report(),
        selection_report(),
        media_report(),
        category_report(),
    ]


def fingerprints() -> dict[str, object]:
    return {
        role: {"basename": name, "sha256": f"{index:064x}"}
        for index, (role, name) in enumerate(SOURCE_NAMES.items(), 1)
    }


def build(
    values: list[dict[str, object]] | None = None,
    *,
    target_sku: str = SKU,
) -> dict[str, object]:
    return core.build_single_product_staging_package(
        *(values or reports()),
        target_sku=target_sku,
        source_fingerprints=fingerprints(),
    )


def assert_blocked(report: dict[str, object], code: str) -> None:
    assert report["status"] == "blocked"
    assert code in report["blocking_issues"]
    assert report["write_authorized"] is False


def write_inputs(root: Path, values: list[dict[str, object]] | None = None) -> list[Path]:
    paths: list[Path] = []
    for name, value in zip(SOURCE_NAMES.values(), values or reports(), strict=True):
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)
    return paths


def valid_argv(paths: list[Path]) -> list[str]:
    return [
        "build-single-product-staging-package",
        "--payload-report", str(paths[0]),
        "--sku-report", str(paths[1]),
        "--selection-report", str(paths[2]),
        "--media-report", str(paths[3]),
        "--woo-category-discovery", str(paths[4]),
        "--sku", SKU,
    ]


def test_valid_meru_like_fixture_builds_package():
    report = build()
    assert report["status"] == "ok"
    assert report["policy_version"] == core.POLICY_VERSION
    assert report["target_sku"] == SKU
    assert report["product"] == {
        "name": "FD160cm-Meru",
        "sku": SKU,
        "type": "simple",
        "status": "draft",
        "regular_price": "1399.00",
        "category_id": 1431,
    }


def test_cli_command_and_all_arguments_are_registered():
    paths = [Path(name) for name in SOURCE_NAMES.values()]
    args = cli.build_parser().parse_args(valid_argv(paths))
    assert args.command == "build-single-product-staging-package"
    assert args.payload_report_path == paths[0]
    assert args.sku_report_path == paths[1]
    assert args.selection_report_path == paths[2]
    assert args.media_report_path == paths[3]
    assert args.category_discovery_path == paths[4]
    assert args.target_sku == SKU


@pytest.mark.parametrize(
    "flag",
    [
        "--payload-report",
        "--sku-report",
        "--selection-report",
        "--media-report",
        "--woo-category-discovery",
        "--sku",
    ],
)
def test_each_cli_argument_is_required(flag):
    argv = valid_argv([Path(name) for name in SOURCE_NAMES.values()])
    index = argv.index(flag)
    del argv[index : index + 2]
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


def test_cli_success_writes_fixed_report(tmp_path, monkeypatch):
    paths = write_inputs(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(valid_argv(paths)) == 0
    output = tmp_path / "reports" / core.REPORT_FILENAME
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "ok"


@pytest.mark.parametrize("target", [SKU.lower(), "PRO-FD160", f"{SKU}-EXTRA"])
def test_sku_join_is_case_sensitive_exact_only(target):
    assert_blocked(build(target_sku=target.upper() if target == "PRO-FD160" else target), "single_product_sku_not_found")


def test_missing_sku_blocks():
    values = reports()
    values[1]["results"] = []
    assert_blocked(build(values), "single_product_sku_not_found")


def test_duplicate_sku_blocks():
    values = reports()
    values[1]["results"].append(copy.deepcopy(values[1]["results"][0]))
    assert_blocked(build(values), "single_product_sku_ambiguous")


@pytest.mark.parametrize("mutation", ["status", "blocker", "root_policy", "item_policy"])
def test_ineligible_sku_record_blocks(mutation):
    values = reports()
    item = values[1]["results"][0]
    if mutation == "status":
        item["status"] = "invalid_identity"
    elif mutation == "blocker":
        item["blocking_issues"] = ["sku_collision"]
    elif mutation == "root_policy":
        values[1]["policy_version"] = "old-policy"
    else:
        item["policy_version"] = "old-policy"
    assert_blocked(build(values), "single_product_sku_not_eligible")


def test_payload_candidate_missing_blocks():
    values = reports()
    values[0]["candidates"] = []
    assert_blocked(build(values), "single_product_payload_not_found")


def test_duplicate_payload_candidate_blocks():
    values = reports()
    values[0]["candidates"].append(copy.deepcopy(values[0]["candidates"][0]))
    assert_blocked(build(values), "single_product_payload_ambiguous")


@pytest.mark.parametrize(
    "mutation",
    [
        "blocker", "ready", "type", "status", "name", "price", "no_category",
        "two_categories", "zero_category", "unknown_payload_key",
    ],
)
def test_invalid_payload_contract_blocks(mutation):
    values = reports()
    candidate = values[0]["candidates"][0]
    payload = candidate["payload"]
    if mutation == "blocker":
        candidate["blocking_issues"] = ["missing_product_name"]
    elif mutation == "ready":
        candidate["ready_for_write"] = True
    elif mutation == "type":
        payload["type"] = "variable"
    elif mutation == "status":
        payload["status"] = "publish"
    elif mutation == "name":
        payload["name"] = " "
    elif mutation == "price":
        payload["regular_price"] = "1399"
    elif mutation == "no_category":
        payload["categories"] = []
    elif mutation == "two_categories":
        payload["categories"] = [{"id": 1431}, {"id": 1432}]
    elif mutation == "zero_category":
        payload["categories"] = [{"id": 0}]
    else:
        payload["supplier_costs"] = {"fob": 1}
    assert_blocked(build(values), "single_product_payload_not_eligible")


def test_category_missing_from_current_discovery_blocks():
    values = reports()
    values[4]["categories"] = []
    assert_blocked(build(values), "single_product_category_not_found")


def test_duplicate_category_id_is_ambiguous():
    values = reports()
    values[4]["categories"].append({"id": 1431, "name": "Duplicate"})
    assert_blocked(build(values), "single_product_category_ambiguous")


@pytest.mark.parametrize("name", ["", "   ", None])
def test_category_name_must_be_nonempty(name):
    values = reports()
    values[4]["categories"][0]["name"] = name
    assert_blocked(build(values), "single_product_category_not_eligible")


def test_category_discovery_write_counter_must_be_zero():
    values = reports()
    values[4]["write_requests_performed"] = 1
    assert_blocked(build(values), "single_product_category_not_eligible")


def test_selection_target_missing_blocks():
    values = reports()
    values[2]["results"] = []
    assert_blocked(build(values), "single_product_selection_not_found")


def test_duplicate_selection_target_blocks():
    values = reports()
    values[2]["results"].append(copy.deepcopy(values[2]["results"][0]))
    assert_blocked(build(values), "single_product_selection_ambiguous")


def test_zero_selected_images_blocks():
    values = reports()
    values[2] = selection_report(0)
    values[3]["results"] = []
    assert_blocked(build(values), "single_product_selection_not_eligible")


def test_multiple_primary_images_block():
    values = reports()
    values[2]["results"][0]["items"][1]["image_role"] = "primary"
    assert_blocked(build(values), "single_product_primary_media_invalid")


def test_duplicate_selection_position_blocks():
    values = reports()
    values[2]["results"][0]["items"][1]["selection_position"] = 0
    assert_blocked(build(values), "single_product_selection_position_ambiguous")


@pytest.mark.parametrize("position", [-1, True, "1"])
def test_invalid_selection_position_blocks(position):
    values = reports()
    values[2]["results"][0]["items"][1]["selection_position"] = position
    assert_blocked(build(values), "single_product_selection_not_eligible")


def test_invalid_selection_role_blocks():
    values = reports()
    values[2]["results"][0]["items"][1]["image_role"] = "thumbnail"
    assert_blocked(build(values), "single_product_selection_not_eligible")


def test_selection_blocker_is_not_ignored():
    values = reports()
    values[2]["results"][0]["blocking_issues"] = ["selection_blocked"]
    assert_blocked(build(values), "single_product_selection_not_eligible")


def test_selection_count_contract_is_checked():
    values = reports()
    values[2]["results"][0]["selected_count"] = 2
    assert_blocked(build(values), "single_product_selection_not_eligible")


def test_selection_limit_is_current_policy_limit():
    values = reports()
    values[2] = selection_report(MAX_IMAGES_PER_SKU + 1)
    values[3]["results"] = [
        media_result(i, "primary" if i == 0 else "gallery", 1000 + i)
        for i in range(MAX_IMAGES_PER_SKU + 1)
    ]
    assert_blocked(build(values), "single_product_selection_not_eligible")


def test_media_result_missing_blocks():
    values = reports()
    values[3]["results"] = [item for item in values[3]["results"] if item["selection_position"] != 1]
    report = build(values)
    assert_blocked(report, "single_product_media_count_mismatch")
    assert "single_product_media_join_missing" in report["blocking_issues"]


def test_duplicate_media_join_blocks():
    values = reports()
    values[3]["results"].append(copy.deepcopy(values[3]["results"][0]))
    report = build(values)
    assert_blocked(report, "single_product_media_join_ambiguous")


def test_media_role_mismatch_blocks():
    values = reports()
    next(item for item in values[3]["results"] if item["selection_position"] == 1)["image_role"] = "primary"
    assert_blocked(build(values), "single_product_media_role_mismatch")


@pytest.mark.parametrize("media_id", [0, -1, True, "123"])
def test_invalid_wordpress_media_id_blocks(media_id):
    values = reports()
    values[3]["results"][0]["wordpress_media_id"] = media_id
    assert_blocked(build(values), "single_product_media_not_eligible")


@pytest.mark.parametrize("status", ["not_attempted", "blocked", "uploaded", None])
def test_only_existing_success_upload_status_is_accepted(status):
    values = reports()
    values[3]["results"][0]["upload_status"] = status
    assert_blocked(build(values), "single_product_media_not_eligible")


def test_media_item_blocker_is_not_ignored():
    values = reports()
    values[3]["results"][0]["blocking_issues"] = ["upload_failed"]
    assert_blocked(build(values), "single_product_media_not_eligible")


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_version", "old"),
        ("woocommerce_requests_performed", 1),
        ("woocommerce_write_requests_performed", 1),
        ("delete_requests_performed", 1),
        ("rollback_requests_performed", 1),
        ("webp_cleanup_completed", False),
        ("source_cleanup_completed", False),
        ("webp_files_remaining", 1),
        ("source_files_remaining", 1),
    ],
)
def test_media_execution_root_safety_is_revalidated(field, value):
    values = reports()
    values[3][field] = value
    assert_blocked(build(values), "single_product_media_report_not_eligible")


def test_duplicate_wordpress_reference_blocks():
    values = reports()
    values[3]["results"][1]["wordpress_media_id"] = values[3]["results"][0]["wordpress_media_id"]
    assert_blocked(build(values), "single_product_media_reference_duplicate")


def test_primary_is_first_and_gallery_uses_selection_position():
    report = build()
    assert [(x["selection_position"], x["image_role"]) for x in report["image_plan"]] == [
        (0, "primary"), (1, "gallery"), (2, "gallery")
    ]
    assert report["future_woo_payload"]["images"] == [
        {"id": 900}, {"id": 800}, {"id": 700}
    ]


def test_media_id_order_does_not_control_image_order():
    assert [item["id"] for item in build()["future_woo_payload"]["images"]] == [900, 800, 700]


def test_filename_does_not_enter_image_plan_or_control_sorting():
    text = json.dumps(build())
    assert "must-not-leak" not in text
    assert "private-file" not in text


def test_future_payload_is_allowlisted_and_excludes_internal_surfaces():
    payload = build()["future_woo_payload"]
    assert "storefront_options" not in payload
    assert "audit" not in payload
    assert "supplier_costs" not in payload
    assert "fx_rate" not in payload
    assert "margin" not in payload
    assert set(payload) == {
        "name", "sku", "type", "status", "regular_price", "attributes",
        "categories", "images",
    }


def test_original_description_fields_are_preserved_without_generation():
    values = reports()
    values[0]["candidates"][0]["payload"].update(
        {"description": "Approved description", "short_description": "Approved short"}
    )
    payload = build(values)["future_woo_payload"]
    assert payload["description"] == "Approved description"
    assert payload["short_description"] == "Approved short"


def test_no_description_is_generated_when_core_payload_has_none():
    payload = build()["future_woo_payload"]
    assert "description" not in payload
    assert "short_description" not in payload


def test_input_candidate_is_not_mutated():
    values = reports()
    before = copy.deepcopy(values)
    build(values)
    assert values == before


def test_images_warning_is_resolved_without_mutating_other_warnings():
    report = build()
    assert report["resolved_warnings"] == ["images_not_mapped"]
    assert report["unresolved_warnings"] == ["customer_description_not_generated"]


def test_size_enrichment_warning_remains_unresolved():
    values = reports()
    values[0]["candidates"][0]["warnings"].append("size_enrichment_unmatched")
    report = build(values)
    assert "size_enrichment_unmatched" in report["unresolved_warnings"]


def test_write_authorized_and_all_package_counters_are_always_zero():
    report = build()
    assert report["write_authorized"] is False
    for field in core._PACKAGE_COUNTERS:
        assert report[field] == 0


def test_prior_media_upload_writes_do_not_authorize_package_write():
    report = build()
    assert media_report()["write_requests_performed"] == 2
    assert report["wordpress_requests_performed"] == 0
    assert report["external_write_requests_performed"] == 0
    assert report["write_authorized"] is False


def test_source_validation_is_explicit():
    assert build()["source_validation"] == {
        "sku_verified": True,
        "payload_verified": True,
        "category_verified": True,
        "selection_verified": True,
        "media_verified": True,
    }


def test_source_fingerprint_projection_is_deterministic():
    first, second = build(), build()
    assert first["source_fingerprints"] == second["source_fingerprints"]
    assert first == second


@pytest.mark.parametrize("root_index", range(5))
def test_non_ok_source_report_is_contract_error(root_index):
    values = reports()
    values[root_index]["status"] = "blocked"
    with pytest.raises(core.SingleProductStagingPackageError) as caught:
        build(values)
    assert str(caught.value) == "single_product_source_report_not_ok"


@pytest.mark.parametrize(
    "path",
    [
        Path("https://example.test/report.json"),
        Path("http://example.test/report.json"),
        Path(r"\\server\share\report.json"),
    ],
)
def test_url_and_unc_inputs_are_rejected(path):
    with pytest.raises(dry_run.SingleProductStagingPackageInputError):
        dry_run._local_path(path, require_file=True)


def test_symlink_or_junction_input_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dry_run, "_has_link_or_reparse", lambda value: True)
    with pytest.raises(dry_run.SingleProductStagingPackageInputError) as caught:
        dry_run._local_path(path, require_file=True)
    assert str(caught.value) == "single_product_linked_path_not_allowed"


def test_non_file_json_path_is_rejected(tmp_path):
    path = tmp_path / "directory.json"
    path.mkdir()
    with pytest.raises(dry_run.SingleProductStagingPackageInputError):
        dry_run._local_path(path, require_file=True)


def test_invalid_json_does_not_overwrite_previous_success(tmp_path):
    paths = write_inputs(tmp_path)
    output = tmp_path / "reports" / core.REPORT_FILENAME
    output.parent.mkdir()
    original = b'{"status":"ok","sentinel":"preserve"}\n'
    output.write_bytes(original)
    paths[0].write_text("{invalid", encoding="utf-8")
    with pytest.raises(dry_run.SingleProductStagingPackageInputError):
        dry_run.run_single_product_staging_package_dry_run(
            *paths, SKU, project_root=tmp_path
        )
    assert output.read_bytes() == original


def test_non_ok_input_does_not_overwrite_previous_success(tmp_path):
    values = reports()
    values[0]["status"] = "blocked"
    paths = write_inputs(tmp_path, values)
    output = tmp_path / "reports" / core.REPORT_FILENAME
    output.parent.mkdir()
    original = b'{"status":"ok","sentinel":"preserve"}\n'
    output.write_bytes(original)
    with pytest.raises(dry_run.SingleProductStagingPackageInputError):
        dry_run.run_single_product_staging_package_dry_run(
            *paths, SKU, project_root=tmp_path
        )
    assert output.read_bytes() == original


def test_runner_fingerprints_raw_input_bytes_and_only_exposes_basenames(tmp_path):
    paths = write_inputs(tmp_path)
    report, _ = dry_run.run_single_product_staging_package_dry_run(
        *paths, SKU, project_root=tmp_path
    )
    assert set(report["source_fingerprints"]) == set(core.SOURCE_ROLES)
    assert all(
        entry["basename"] in SOURCE_NAMES.values()
        and len(entry["sha256"]) == 64
        for entry in report["source_fingerprints"].values()
    )
    assert str(tmp_path) not in json.dumps(report)


def test_blocked_report_is_sanitized(tmp_path):
    values = reports()
    values[1]["results"] = []
    values[0]["private_key"] = "-----BEGIN PRIVATE KEY----- secret"
    values[3]["authorization"] = "Bearer secret"
    report = build(values)
    text = json.dumps(report).casefold()
    assert report["status"] == "blocked"
    assert "private key" not in text
    assert "bearer secret" not in text
    assert "authorization" not in text


def test_cli_blocked_report_returns_one(tmp_path, monkeypatch):
    values = reports()
    values[1]["results"] = []
    paths = write_inputs(tmp_path, values)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(valid_argv(paths)) == 1
    assert json.loads(
        (tmp_path / "reports" / core.REPORT_FILENAME).read_text(encoding="utf-8")
    )["status"] == "blocked"


def test_cli_contract_error_returns_two_without_overwrite(tmp_path, monkeypatch):
    paths = write_inputs(tmp_path)
    paths[0].write_text("not json", encoding="utf-8")
    output = tmp_path / "reports" / core.REPORT_FILENAME
    output.parent.mkdir()
    output.write_text('{"status":"ok","sentinel":true}\n', encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(valid_argv(paths)) == 2
    assert json.loads(output.read_text(encoding="utf-8"))["sentinel"] is True


def test_no_core_module_constructs_clients_or_loads_configuration():
    source = Path(core.__file__).read_text(encoding="utf-8")
    io_source = Path(dry_run.__file__).read_text(encoding="utf-8")
    forbidden = (
        "load_config(", "load_google_config(", "OfficialGoogleClientFactory(",
        "ReadOnlyHttpClient(", "StdlibWordPressMediaHttpTransport(",
        "requests.", "urllib.request", "http.client",
    )
    assert all(token not in source and token not in io_source for token in forbidden)


def _candidate(values: list[dict[str, object]]) -> dict[str, object]:
    return values[0]["candidates"][0]


def _category_audit(values: list[dict[str, object]]) -> dict[str, object]:
    return _candidate(values)["audit"]["category"]


def test_selected_candidate_is_passed_to_canonical_woo_validator(monkeypatch):
    original = core.woo_mapper.validate_woocommerce_product_payload
    seen = []

    def validating(candidate):
        seen.append(candidate)
        return original(candidate)

    monkeypatch.setattr(
        core.woo_mapper, "validate_woocommerce_product_payload", validating
    )
    assert build()["status"] == "ok"
    assert len(seen) == 1
    assert seen[0]["ready_for_write"] is False


def test_valid_fixture_satisfies_current_canonical_woo_candidate_contract():
    values = reports()
    assert core.woo_mapper.validate_woocommerce_product_payload(
        _candidate(values)
    ) == ()


def test_any_canonical_validator_issue_blocks_payload(monkeypatch):
    monkeypatch.setattr(
        core.woo_mapper,
        "validate_woocommerce_product_payload",
        lambda candidate: ("canonical_mock_issue",),
    )
    assert_blocked(build(), "single_product_payload_not_eligible")


def test_ready_for_write_false_remains_required_and_legal():
    values = reports()
    assert _candidate(values)["ready_for_write"] is False
    assert build(values)["status"] == "ok"


def test_attribute_variation_true_is_blocked_by_canonical_validator():
    values = reports()
    _candidate(values)["payload"]["attributes"][0]["variation"] = True
    assert_blocked(build(values), "single_product_payload_not_eligible")


def test_malformed_attribute_structure_is_blocked_by_canonical_validator():
    values = reports()
    _candidate(values)["payload"]["attributes"] = [
        {"name": "Height", "options": ["160cm"]}
    ]
    assert_blocked(build(values), "single_product_payload_not_eligible")


def test_invalid_attribute_name_is_blocked_by_canonical_validator():
    values = reports()
    _candidate(values)["payload"]["attributes"][0]["name"] = "Supplier Cost"
    assert_blocked(build(values), "single_product_payload_not_eligible")


@pytest.mark.parametrize(
    "mutation",
    ["missing", "status", "host_verified", "discovery_verified", "woo_id"],
)
def test_malformed_category_audit_is_blocked_by_canonical_validator(mutation):
    values = reports()
    audit = _candidate(values)["audit"]
    category = audit["category"]
    if mutation == "missing":
        audit.pop("category")
    elif mutation == "status":
        category["binding_status"] = "binding_target_changed"
    elif mutation == "host_verified":
        category["host_verified"] = False
    elif mutation == "discovery_verified":
        category["discovery_verified"] = False
    else:
        category["woo_category_id"] = 1432
    assert_blocked(build(values), "single_product_payload_not_eligible")


def test_current_discovery_same_id_but_renamed_is_blocked():
    values = reports()
    values[4]["categories"][0]["name"] = "Renamed category"
    assert_blocked(build(values), "single_product_category_binding_changed")


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding_profile_version", "wrong-staging-profile"),
        ("environment", "production"),
        ("target_host", "xxxxdoll.com"),
        ("woo_category_id", 1432),
        ("verified_name", "Changed verified name"),
        ("host_verified", False),
        ("discovery_verified", False),
    ],
)
def test_category_audit_must_exactly_match_staging_binding_contract(field, value):
    values = reports()
    _category_audit(values)[field] = value
    assert_blocked(build(values), "single_product_category_binding_changed")


def test_staging_binding_profile_is_the_authority_source(monkeypatch):
    original = woo_category_binding.staging_category_binding_profile
    calls = []

    def profile():
        calls.append(True)
        return original()

    monkeypatch.setattr(core.woo_category_binding, "staging_category_binding_profile", profile)
    assert build()["status"] == "ok"
    assert calls == [True]


def test_package_core_does_not_redeclare_approved_category_id_or_name():
    source = Path(core.__file__).read_text(encoding="utf-8")
    assert "1431" not in source
    assert "1432" not in source
    assert "Realistic sex dolls" not in source
    assert "Silicone sex dolls" not in source


def test_valid_staging_discovery_source_host_passes_host_provenance():
    report = build()
    assert report["status"] == "ok"
    assert report["source_validation"]["category_verified"] is True


def test_production_discovery_source_host_is_blocked():
    values = reports()
    values[4]["source_host"] = "xxxxdoll.com"
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )


def test_unrelated_staging_discovery_hostname_is_blocked():
    values = reports()
    values[4]["source_host"] = "unrelated-site.wpcomstaging.com"
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )


def test_missing_discovery_source_host_fails_closed():
    values = reports()
    values[4].pop("source_host")
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )


@pytest.mark.parametrize(
    "source_host",
    [
        "not-a-url",
        "https://" + woo_category_binding.STAGING_EXPECTED_HOST,
        woo_category_binding.STAGING_EXPECTED_HOST + "/path",
    ],
)
def test_malformed_discovery_source_host_fails_closed(source_host):
    values = reports()
    values[4]["source_host"] = source_host
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )


def test_historical_discovery_get_count_is_not_required_to_be_zero():
    values = reports()
    values[4]["network_requests_performed"] = 99
    assert build(values)["status"] == "ok"


def test_discovery_source_host_uses_existing_hostname_normalization(monkeypatch):
    original = core.woo_category_binding._normalize_hostname
    calls = []

    def normalize(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(
        core.woo_category_binding, "_normalize_hostname", normalize
    )
    assert build()["status"] == "ok"
    assert calls == [woo_category_binding.STAGING_EXPECTED_HOST]


def test_candidate_audit_cannot_override_wrong_discovery_source_host():
    values = reports()
    assert _category_audit(values)["target_host"] == (
        woo_category_binding.STAGING_EXPECTED_HOST
    )
    values[4]["source_host"] = "different.wpcomstaging.com"
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )


def test_same_category_id_and_name_cannot_override_wrong_source_host():
    values = reports()
    assert values[4]["categories"][0] == {
        "id": 1431,
        "name": "Realistic sex dolls",
    }
    values[4]["source_host"] = "other.wpcomstaging.com"
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )


def test_legacy_base_url_is_not_a_source_host_fallback():
    values = reports()
    values[4].pop("source_host")
    values[4]["base_url"] = (
        "https://" + woo_category_binding.STAGING_EXPECTED_HOST
    )
    assert_blocked(
        build(values), "single_product_category_discovery_host_mismatch"
    )
