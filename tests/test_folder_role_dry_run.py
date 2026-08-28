from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import stat
import sys
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker import cli, folder_role_dry_run as dry_run, folder_role_policy as policy
from sync_worker.folder_role_dry_run import FolderRoleDryRunInputError
from sync_worker.image_mapping import ProductSourceRange


def manifest(*results):
    # These fixtures are constructed by tests, never loaded from real reports.
    return {
        "status": "ok", "results": list(results),
        "summary": {"network_requests_performed": 17, "download_requests_performed": 0},
        "warnings": [], "blocking_issues": [], "write_requests_performed": 0,
    }


def nested(name="Photos-Mock", *, sku="CLM-MOCK-001", deeper=0, **overrides):
    return {
        "sku": sku, "product_source": {"start_row": 10, "end_row": 20},
        "safe_folder_name": name, "depth": 1, "status": "listed",
        "root_folder_id_fingerprint": "sha256:" + "a" * 64,
        "nested_folder_id_fingerprint": "sha256:" + "b" * 64,
        "nested_folder_at_depth_limit_count": deeper,
        "item_count": 1, "image_candidate_count": 1, "pages_read": 1,
        "items": [{
            "safe_name": "mock-image.jpg", "mime_type": "image/jpeg",
            "file_id_fingerprint": "sha256:" + "c" * 64,
            "item_kind": "image_file", "image_candidate": True, "warnings": [],
        }],
        "warnings": [], "blocking_issues": [], **overrides,
    }


def depth2(name="Eye Options-Mock", *, parent="Photos-Mock Parent", **kwargs):
    result = nested(name, **kwargs)
    result.pop("safe_folder_name")
    result["depth1_folder_id_fingerprint"] = result.pop("nested_folder_id_fingerprint")
    result.update({
        "depth2_safe_folder_name": name, "depth1_safe_folder_name": parent,
        "depth": 2, "depth2_folder_id_fingerprint": "sha256:" + "d" * 64,
    })
    return result


ROLE_NAMES = (
    "Photos-Mock", "Factory Photos-Mock", "Banner-Mock", "Factory Videos-Mock",
    "Eye Options-Mock", "Promo assets-Mock", "Other Skin Tone-Mock", "Mock Model Collection",
)


def sample_reports():
    # A 24 + 8 fixture exercises the stated shape without real supplier data.
    return (
        manifest(*(nested(name, sku=f"MOCK-{index}", deeper=int(index == 4))
                   for index, name in enumerate(ROLE_NAMES) for _ in range(3))),
        manifest(*(depth2(name, sku=f"MOCK-{index}", deeper=int(index == 7))
                   for index, name in enumerate(ROLE_NAMES))),
    )


def build(nested_report=None, depth2_report=None):
    return dry_run.build_folder_role_dry_run_report(
        manifest(nested()) if nested_report is None else nested_report,
        manifest(depth2()) if depth2_report is None else depth2_report,
    )


class FolderRoleDryRunTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(self.enterContext(TemporaryDirectory()))
        self.output = self.project / "reports" / dry_run.REPORT_FILENAME
        self.denied = []
        targets = (
            "socket.socket.connect", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "sync_worker.cli.OfficialGoogleClientFactory",
            "sync_worker.google_api.OfficialGoogleClientFactory",
            "sync_worker.google_api.GoogleDriveMetadataGateway.list_folder_children",
            "sync_worker.google_api.ReadOnlySheetsGateway.inspect_sheet_layout",
            "sync_worker.http_client.ReadOnlyHttpClient.request",
        )
        for target in targets:
            self.denied.append(self.enterContext(patch(target, side_effect=AssertionError("Offline fixtures only"))))
        for module in ("sync_worker.cli", "sync_worker.config"):
            for name in ("load_config", "load_google_config", "load_google_drive_metadata_config", "load_google_sheets_readonly_config"):
                self.denied.append(self.enterContext(patch(f"{module}.{name}", side_effect=AssertionError("No real configuration"))))

    def tearDown(self):
        for operation in self.denied:
            operation.assert_not_called()

    def files(self, nested_report=None, depth2_report=None):
        paths = (self.project / "nested.json", self.project / "depth2.json")
        values = (manifest(nested()) if nested_report is None else nested_report,
                  manifest(depth2()) if depth2_report is None else depth2_report)
        for path, value in zip(paths, values):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return paths

    def run_cli(self, paths):
        with patch.object(cli, "PROJECT_ROOT", self.project):
            return cli.main(["classify-folder-roles", "--nested-manifest", str(paths[0]),
                             "--depth2-manifest", str(paths[1])])

    def assert_role(self, name, expected):
        record = build(manifest(nested(name)), manifest())["results"][0]
        self.assertEqual(record["role"], expected)
        return record

    def test_01_cli_registered(self):
        args = cli.build_parser().parse_args([
            "classify-folder-roles", "--nested-manifest", "nested.json",
            "--depth2-manifest", "depth2.json",
        ])
        self.assertEqual(args.command, "classify-folder-roles")
        self.assertEqual(args.nested_manifest_path, Path("nested.json"))
        self.assertEqual(args.depth2_manifest_path, Path("depth2.json"))

    def test_02_nested_argument_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["classify-folder-roles", "--depth2-manifest", "depth2.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_depth2_argument_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["classify-folder-roles", "--nested-manifest", "nested.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_04_nested_status_must_be_ok(self):
        for status in ("partial", "error", "OK", None, True):
            with self.subTest(status=status), self.assertRaisesRegex(FolderRoleDryRunInputError, "nested_manifest_status_not_ok"):
                build({**manifest(nested()), "status": status})

    def test_05_depth2_status_must_be_ok(self):
        for status in ("partial", "error", "OK", None, True):
            with self.subTest(status=status), self.assertRaisesRegex(FolderRoleDryRunInputError, "depth2_manifest_status_not_ok"):
                build(depth2_report={**manifest(depth2()), "status": status})

    def test_06_depth1_classification(self):
        result = build(manifest(nested()), manifest())["results"][0]
        self.assertEqual((result["depth"], result["safe_folder_name"], result["source_manifest_kind"]),
                         (1, "Photos-Mock", "nested"))
        self.assertEqual(result["role"], "storefront_photos")
        self.assertIsNone(result["parent_safe_folder_name"])

    def test_07_depth2_classification(self):
        result = build(manifest(), manifest(depth2("Banner-Mock")))["results"][0]
        self.assertEqual((result["depth"], result["safe_folder_name"], result["source_manifest_kind"]),
                         (2, "Banner-Mock", "depth2"))
        self.assertEqual(result["role"], "banner")

    def test_08_storefront(self):
        self.assert_role("Photos-Mock", "storefront_photos")

    def test_09_factory(self):
        self.assert_role("Factory Photos-Mock", "factory_photos")

    def test_10_banner(self):
        self.assert_role("Banner-Mock", "banner")

    def test_11_videos(self):
        for name in ("Video", "Videos", "Factory Videos-Mock"):
            with self.subTest(name=name):
                self.assert_role(name, "video")

    def test_12_eye_options(self):
        self.assert_role("Eye Options-Mock", "eye_options")

    def test_13_promo_assets(self):
        self.assert_role("Promo assets-Mock", "promo_assets")

    def test_14_other_skin_tone(self):
        self.assert_role("Other Skin Tone-Mock", "other_skin_tone")

    def test_15_unknown_is_allowed_without_fuzzy_rules(self):
        for name in ("Mock Product Collection", "Phots-Mock", "Factory Phots-Mock"):
            self.assert_role(name, "unknown")

    def test_16_core_reused_for_each_result(self):
        with patch.object(policy, "classify_folder_role", wraps=policy.classify_folder_role) as classify:
            build(manifest(nested(deeper=2)), manifest(depth2(deeper=3)))
        self.assertEqual(classify.call_count, 2)
        self.assertEqual(classify.call_args_list[0].args, ("Photos-Mock",))
        self.assertEqual(classify.call_args_list[1].args, ("Eye Options-Mock",))
        self.assertEqual(classify.call_args_list[0].kwargs, {
            "parent_safe_folder_name": None, "depth": 1, "sku": "CLM-MOCK-001",
            "product_source": ProductSourceRange(10, 20), "has_depth_limit_children": True,
        })
        self.assertEqual(classify.call_args_list[1].kwargs["parent_safe_folder_name"], "Photos-Mock Parent")

    def test_17_no_copied_role_rule_table(self):
        source = inspect.getsource(dry_run)
        constants = {node.value for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertTrue(constants.isdisjoint({role.value for role in policy.FolderRole}))
        self.assertNotIn("_RULES", source)
        self.assertNotIn("ROLE_PRIORITY", source)

    def test_18_depth_retained(self):
        self.assertEqual([item["depth"] for item in build()["results"]], [1, 2])

    def test_19_parent_retained_without_role_inheritance(self):
        result = build(manifest(), manifest(depth2("Mock Neutral", parent="Factory Photos-Parent")))["results"][0]
        self.assertEqual(result["parent_safe_folder_name"], "Factory Photos-Parent")
        self.assertEqual(result["role"], "unknown")

    def test_20_zero_children_means_no_deeper_inventory(self):
        for result in build()["results"]:
            self.assertIs(result["requires_deeper_inventory"], False)

    def test_21_positive_children_means_deeper_inventory(self):
        for result in build(manifest(nested(deeper=2)), manifest(depth2(deeper=1)))["results"]:
            self.assertIs(result["requires_deeper_inventory"], True)

    def test_22_no_role_forces_deeper_inventory(self):
        report = build(manifest(*(nested(name) for name in ROLE_NAMES)), manifest())
        self.assertTrue(all(not item["requires_deeper_inventory"] for item in report["results"]))

    def test_23_gallery_eligibility_from_core(self):
        report = build(manifest(*(nested(name) for name in ROLE_NAMES)), manifest())
        eligible = [item["role"] for item in report["results"] if item["gallery_eligible"]]
        self.assertCountEqual(eligible, ["storefront_photos", "factory_photos"])

    def test_24_unknown_warning_is_nonblocking(self):
        report = build(manifest(nested("Mock Neutral")), manifest())
        self.assertEqual(report["results"][0]["warnings"], ["folder_role_unknown"])
        self.assertEqual(report["results"][0]["blocking_issues"], [])
        self.assertEqual(report["status"], "ok")

    def test_25_deterministic_ordering(self):
        first, second = sample_reports()
        before = build(first, second)
        first["results"].reverse()
        second["results"].reverse()
        self.assertEqual(before, build(first, second))
        keys = [(r["sku"], r["depth"], r["normalized_folder_name"], r["safe_folder_name"])
                for r in before["results"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(json.dumps(before, sort_keys=True), json.dumps(build(first, second), sort_keys=True))

    def test_26_sku_not_used_to_classify(self):
        record = build(manifest(nested("Mock Neutral", sku="Factory Photos")), manifest())["results"][0]
        self.assertEqual(record["role"], "unknown")
        self.assertEqual(record["sku"], "Factory Photos")

    def test_27_fingerprint_not_used_or_forwarded(self):
        item = nested("Mock Neutral")
        expected = build(manifest(item), manifest())
        item["nested_folder_id_fingerprint"] = "Photos-Mock"
        item["root_folder_id_fingerprint"] = "Factory Photos-Mock"
        self.assertEqual(expected, build(manifest(item), manifest()))
        self.assertNotIn("fingerprint", json.dumps(expected))

    def test_28_raw_identifiers_rejected(self):
        for key in ("id", "file_id", "fileId", "folder_id", "raw_folder_id", "provider_file_id", "providerFileId", "spreadsheetId"):
            with self.subTest(key=key), self.assertRaisesRegex(FolderRoleDryRunInputError, "unsafe_manifest_field"):
                build(manifest(nested(**{key: "MOCK_RAW_IDENTIFIER"})), manifest())

    def test_29_urls_rejected_even_in_unused_metadata(self):
        for value in ("https://drive.google.com/drive/folders/MOCK_ONLY", "https://example.invalid/mock.jpg", "ftp://example.invalid/mock", "www.example.invalid/mock"):
            item = nested()
            item["items"][0]["safe_name"] = value
            with self.subTest(value=value), self.assertRaisesRegex(FolderRoleDryRunInputError, "unsafe_manifest_text"):
                build(manifest(item), manifest())

    def test_30_summary_dynamic_total(self):
        self.assertEqual(build(*sample_reports())["summary"]["total_folders"], 32)
        self.assertEqual(build(manifest(nested()), manifest())["summary"]["total_folders"], 1)
        self.assertEqual(build(manifest(), manifest())["summary"]["total_folders"], 0)

    def test_31_summary_depth_counts(self):
        summary = build(*sample_reports())["summary"]
        self.assertEqual((summary["depth1_folders"], summary["depth2_folders"]), (24, 8))

    def test_32_summary_every_role(self):
        summary = build(*sample_reports())["summary"]
        for role in policy.FolderRole:
            self.assertEqual(summary[role.value], 4)

    def test_33_gallery_summary(self):
        self.assertEqual(build(*sample_reports())["summary"]["gallery_eligible_folders"], 8)

    def test_34_deeper_summary(self):
        self.assertEqual(build(*sample_reports())["summary"]["requires_deeper_inventory_folders"], 4)

    def test_35_network_always_zero(self):
        report = build(*sample_reports())
        self.assertEqual(report["network_requests_performed"], 0)
        self.assertEqual(report["summary"]["network_requests_performed"], 0)

    def test_36_downloads_always_zero(self):
        report = build(*sample_reports())
        self.assertEqual(report["download_requests_performed"], 0)
        self.assertEqual(report["summary"]["download_requests_performed"], 0)

    def test_37_writes_always_zero(self):
        report = build(*sample_reports())
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(report["summary"]["write_requests_performed"], 0)

    def test_38_reads_exactly_two_fixture_files(self):
        paths = self.files()
        with patch.object(dry_run, "load_local_json_report", wraps=dry_run.load_local_json_report) as loader:
            dry_run.run_folder_role_dry_run(*paths, project_root=self.project)
        self.assertEqual([call.args for call in loader.call_args_list], [(path.resolve(),) for path in paths])

    def test_39_report_round_trip(self):
        report, path = dry_run.run_folder_role_dry_run(*self.files(), project_root=self.project)
        self.assertEqual(path, self.output)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_40_cli_runs_local_fixtures(self):
        with self.assertLogs("sync_worker", level="INFO") as logs:
            self.assertEqual(self.run_cli(self.files(*sample_reports())), 0)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["summary"]["total_folders"], 32)
        self.assertIn("folder_role_dry_run_report_written", " ".join(logs.output))

    def test_41_cli_never_loads_config_or_creates_clients(self):
        self.assertEqual(self.run_cli(self.files()), 0)
        for operation in self.denied:
            operation.assert_not_called()

    def test_42_invalid_status_does_not_write_report(self):
        for kind in ("nested", "depth2"):
            first, second = manifest(nested()), manifest(depth2())
            (first if kind == "nested" else second)["status"] = "partial"
            with self.subTest(kind=kind), self.assertLogs("sync_worker", level="ERROR"):
                self.assertEqual(self.run_cli(self.files(first, second)), 2)
            self.assertFalse(self.output.exists())

    def test_43_wrong_depth_not_silently_overwritten(self):
        for factory, depth in ((nested, 1), (depth2, 2)):
            for value in (None, True, 0, 3, str(depth), float(depth)):
                item = factory()
                item["depth"] = value
                args = (manifest(item), manifest()) if depth == 1 else (manifest(), manifest(item))
                with self.subTest(depth=depth, value=value), self.assertRaises(FolderRoleDryRunInputError):
                    build(*args)

    def test_44_child_count_must_be_nonnegative_integer(self):
        for value in (None, True, -1, 1.0, "1"):
            with self.subTest(value=value), self.assertRaisesRegex(FolderRoleDryRunInputError, "invalid_depth_limit_count"):
                build(manifest(nested(deeper=value)), manifest())

    def test_45_product_source_restored_and_validated(self):
        self.assertEqual(build()["results"][0]["product_source"], {"start_row": 10, "end_row": 20})
        for value in (None, {}, {"start_row": True, "end_row": 2}, {"start_row": 2, "end_row": 1}, {"start_row": 0, "end_row": 1}):
            with self.subTest(value=value), self.assertRaisesRegex(FolderRoleDryRunInputError, "invalid_product_source"):
                build(manifest(nested(product_source=value)), manifest())

    def test_46_required_name_parent_and_sku(self):
        for key in ("safe_folder_name", "sku"):
            for value in (None, "", 123):
                item = nested()
                item[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(FolderRoleDryRunInputError):
                    build(manifest(item), manifest())
        with self.assertRaisesRegex(FolderRoleDryRunInputError, "invalid_parent_safe_folder_name"):
            build(manifest(), manifest(depth2(parent=None)))

    def test_47_bad_json_and_missing_file_fail_closed(self):
        paths = self.files()
        for content in ("{", "[]", "null"):
            paths[0].write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(FolderRoleDryRunInputError):
                dry_run.run_folder_role_dry_run(*paths, project_root=self.project)
        with self.assertRaises(FolderRoleDryRunInputError):
            dry_run.run_folder_role_dry_run(self.project / "missing.json", paths[1], project_root=self.project)
        self.assertFalse(self.output.exists())

    def test_48_non_json_including_env_is_rejected_before_read(self):
        with patch.object(dry_run, "load_local_json_report") as loader:
            for name in (".env", "fixture.txt", "fixture.json.tmp"):
                with self.subTest(name=name), self.assertRaisesRegex(FolderRoleDryRunInputError, "json_manifest_path_required"):
                    dry_run.run_folder_role_dry_run(self.project / name, self.project / "depth2.json", project_root=self.project)
            loader.assert_not_called()

    def test_49_remote_paths_rejected_before_filesystem_access(self):
        with patch.object(Path, "resolve", side_effect=AssertionError("Remote path must not be resolved")) as resolve:
            for name in ("https://example.invalid/report.json", "file://server/share/mock.json", r"\\server\share\mock.json", "//server/share/mock.json"):
                with self.subTest(name=name), self.assertRaisesRegex(FolderRoleDryRunInputError, "local_manifest_path_required"):
                    dry_run.run_folder_role_dry_run(Path(name), Path("depth2.json"), project_root=self.project)
            resolve.assert_not_called()

    def test_50_input_files_remain_unchanged(self):
        paths = self.files()
        before = [path.read_bytes() for path in paths]
        dry_run.run_folder_role_dry_run(*paths, project_root=self.project)
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_51_failure_does_not_replace_stale_output(self):
        self.output.parent.mkdir()
        self.output.write_text('{"mock_stale": true}', encoding="utf-8")
        paths = self.files({**manifest(nested()), "status": "partial"})
        with self.assertLogs("sync_worker", level="ERROR"):
            self.assertEqual(self.run_cli(paths), 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), '{"mock_stale": true}')

    def test_52_output_cannot_overwrite_input(self):
        paths = self.files()
        self.output.parent.mkdir()
        self.output.write_text(paths[0].read_text(encoding="utf-8"), encoding="utf-8")
        with patch.object(dry_run, "load_local_json_report") as loader, self.assertRaisesRegex(FolderRoleDryRunInputError, "manifest_output_collision"):
            dry_run.run_folder_role_dry_run(self.output, paths[1], project_root=self.project)
        loader.assert_not_called()

    def test_53_report_uses_only_audit_allowlist(self):
        allowed = {"sku", "product_source", "depth", "safe_folder_name", "parent_safe_folder_name",
                   "role", "policy_version", "normalized_folder_name", "matched_rule", "gallery_eligible",
                   "requires_deeper_inventory", "warnings", "blocking_issues", "source_manifest_kind"}
        for result in build()["results"]:
            self.assertEqual(set(result), allowed)
        output = json.dumps(build())
        for text in ("fingerprint", "items", "mime_type", "mock-image.jpg", "image_candidate_count"):
            self.assertNotIn(text, output)

    def test_54_credential_and_resource_keys_rejected_recursively(self):
        for key in ("private_key", "privateKey", "client_email", "clientEmail", "credentials", "resource_key", "resourceKey", "Authorization", "Cookie", "webViewLink", "download_url"):
            item = nested()
            item["items"][0][key] = "MOCK_SENSITIVE"
            with self.subTest(key=key), self.assertRaisesRegex(FolderRoleDryRunInputError, "unsafe_manifest_field"):
                build(manifest(item), manifest())

    def test_55_secret_values_rejected_without_echo(self):
        for value in ("ck_" + "m" * 30, "cs_" + "n" * 30, "token=MOCK_TOKEN", "resource_key=MOCK_KEY", "mock-service@example.invalid", "Authorization: MOCK", "Cookie: MOCK"):
            with self.subTest(value=value), self.assertRaises(FolderRoleDryRunInputError) as caught:
                build(manifest(nested(value)), manifest())
            self.assertNotIn(value, str(caught.exception))

    def test_56_warning_summary_counts_folders_not_issues(self):
        report = build(manifest(nested("Mock Neutral", warnings=["mock_warning_b", "mock_warning_a", "mock_warning_b"])), manifest())
        self.assertEqual(report["summary"]["folders_with_warnings"], 1)
        self.assertEqual(report["results"][0]["warnings"], ["folder_role_unknown", "mock_warning_a", "mock_warning_b"])

    def test_57_blockers_preserved_and_counted(self):
        report = build(manifest(nested(blocking_issues=["mock_blocker", "mock_blocker"])), manifest())
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["summary"]["blocking_folders"], 1)
        self.assertEqual(report["results"][0]["blocking_issues"], ["mock_blocker"])

    def test_58_file_metadata_not_used_to_classify_or_select_images(self):
        item = nested("Mock Neutral", image_candidate_count=99)
        item["items"][0]["safe_name"] = "Factory Photos-Mock"
        self.assertEqual(build(manifest(item), manifest())["results"][0]["role"], "unknown")

    def test_59_deeper_flag_ignores_other_counts_and_children(self):
        item = nested("Eye Options", deeper=0, item_count=9)
        item["items"][0]["item_kind"] = "nested_folder"
        item["items"][0]["warnings"] = ["max_traversal_depth_reached"]
        self.assertFalse(build(manifest(item), manifest())["results"][0]["requires_deeper_inventory"])

    def test_60_empty_reports_have_all_zero_summary(self):
        report = build(manifest(), manifest())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["results"], [])
        self.assertTrue(all(value == 0 for value in report["summary"].values()))

    def test_61_same_named_folders_not_deduplicated(self):
        report = build(manifest(nested(), nested()), manifest())
        self.assertEqual(report["summary"]["total_folders"], 2)

    def test_62_tied_names_sort_by_parent_source_and_issues(self):
        items = [depth2(parent="Mock B"), depth2(parent="Mock A"), depth2(parent="Mock A", warnings=["mock_b"])]
        first = build(manifest(), manifest(*items))
        self.assertEqual(first, build(manifest(), manifest(*reversed(items))))
        self.assertEqual(first["results"][0]["parent_safe_folder_name"], "Mock A")

    def test_63_cli_failure_logs_no_sensitive_values(self):
        value = "https://drive.google.com/drive/folders/MOCK_ONLY?resourcekey=MOCK_KEY"
        paths = self.files(manifest(nested(value)))
        with self.assertLogs("sync_worker", level="ERROR") as logs:
            self.assertEqual(self.run_cli(paths), 2)
        output = " ".join(logs.output)
        self.assertIn("folder_role_dry_run_aborted", output)
        for forbidden in (value, "drive.google.com", "MOCK_ONLY", "MOCK_KEY"):
            self.assertNotIn(forbidden, output)
        self.assertFalse(self.output.exists())

    def test_64_policy_version_audit(self):
        report = build()
        self.assertEqual(report["policy_version"], "xxxxdoll-folder-role-v1")
        self.assertTrue(all(item["policy_version"] == policy.POLICY_VERSION for item in report["results"]))

    def test_65_both_report_statuses_checked_before_classification(self):
        with patch.object(policy, "classify_folder_role") as classify, self.assertRaises(FolderRoleDryRunInputError):
            build(manifest(nested()), {**manifest(depth2()), "status": "partial"})
        classify.assert_not_called()

    def test_66_input_objects_not_mutated(self):
        reports = sample_reports()
        before = copy.deepcopy(reports)
        build(*reports)
        self.assertEqual(reports, before)

    def test_67_read_error_does_not_expose_path_or_original_exception(self):
        paths = self.files()
        with patch.object(dry_run, "load_local_json_report", side_effect=OSError("MOCK_SECRET_AND_PATH")):
            with self.assertRaisesRegex(FolderRoleDryRunInputError, "local_manifest_read_failed") as caught:
                dry_run.run_folder_role_dry_run(*paths, project_root=self.project)
        self.assertNotIn("MOCK_SECRET_AND_PATH", str(caught.exception))

    def test_68_core_decisions_not_recomputed_by_adapter(self):
        sentinel = replace(policy.classify_folder_role("Mock Neutral", sku="MOCK", depth=1,
                           product_source=ProductSourceRange(10, 20)), role=policy.FolderRole.BANNER,
                           matched_rule="mock_core_rule", gallery_eligible=False)
        with patch.object(policy, "classify_folder_role", return_value=sentinel):
            result = build(manifest(nested("Photos-Mock")), manifest())["results"][0]
        self.assertEqual(result["role"], "banner")
        self.assertEqual(result["matched_rule"], "mock_core_rule")
        self.assertFalse(result["gallery_eligible"])

    def test_69_results_must_be_arrays_of_objects(self):
        for value in (None, {}, "mock", ["mock"]):
            with self.subTest(value=value), self.assertRaises(FolderRoleDryRunInputError):
                build({"status": "ok", "results": value}, manifest())

    def test_70_saved_json_has_no_credentials_urls_or_raw_ids(self):
        _, path = dry_run.run_folder_role_dry_run(*self.files(*sample_reports()), project_root=self.project)
        output = path.read_text(encoding="utf-8")
        for forbidden in ("https://", "http://", "provider_file_id", "raw_folder_id", "resource_key", "private_key", "client_email", "Authorization", "Cookie", "fingerprint"):
            self.assertNotIn(forbidden, output)

    def test_71_invalid_issues_rejected(self):
        for field in ("warnings", "blocking_issues"):
            with self.subTest(field=field), self.assertRaisesRegex(FolderRoleDryRunInputError, "invalid_manifest_issues"):
                build(manifest(nested(**{field: "not-an-array"})), manifest())

    def test_72_cli_partial_report_return_code(self):
        paths = self.files(manifest(nested(blocking_issues=["mock_blocker"])))
        self.assertEqual(self.run_cli(paths), 1)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["status"], "partial")

    def test_73_symlink_and_parent_junction_rejected_without_reading(self):
        paths = self.files()
        original = Path.lstat
        scenarios = (
            (paths[0], SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)),
            (paths[0].parent, SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)),
        )
        for linked, metadata in scenarios:
            def lstat(path, **kwargs):
                return metadata if path == linked else original(path, **kwargs)
            with self.subTest(linked=linked), patch.object(Path, "lstat", lstat), patch.object(dry_run, "load_local_json_report") as loader:
                with self.assertRaises(FolderRoleDryRunInputError):
                    dry_run.run_folder_role_dry_run(*paths, project_root=self.project)
                loader.assert_not_called()

    def test_74_temporary_report_symlink_rejected_before_read_or_write(self):
        paths = self.files()
        self.output.parent.mkdir()
        temporary = self.output.with_name(self.output.name + ".tmp")
        original = Path.lstat
        def lstat(path, **kwargs):
            if path == temporary:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original(path, **kwargs)
        with patch.object(Path, "lstat", lstat), patch.object(dry_run, "load_local_json_report") as loader:
            with self.assertRaises(FolderRoleDryRunInputError):
                dry_run.run_folder_role_dry_run(*paths, project_root=self.project)
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
