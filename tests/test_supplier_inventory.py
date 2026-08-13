from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser  # noqa: E402
from sync_worker.config import (  # noqa: E402
    ConfigError,
    GOOGLE_DRIVE_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    load_google_config,
)
from sync_worker.google_api import (  # noqa: E402
    GoogleClients,
    GoogleOperationBlocked,
    ensure_google_http_method_allowed,
    ensure_google_operation_allowed,
    google_redactor_for_settings,
)
from sync_worker.report import SafeJsonReportWriter  # noqa: E402
from sync_worker.supplier_inventory import (  # noqa: E402
    DRIVE_ITEM_LIMIT,
    FOLDER_MIME_TYPE,
    SupplierInventoryRunner,
)


class FakeRequest:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def execute(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeFilesResource:
    def __init__(
        self,
        root_metadata: dict[str, dict[str, object]],
        children: dict[str, dict[str, object]],
    ) -> None:
        self._root_metadata = root_metadata
        self._children = children
        self.get_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.content_download_calls = 0

    def get(self, **kwargs: object) -> FakeRequest:
        self.get_calls.append(kwargs)
        return FakeRequest(self._root_metadata[str(kwargs["fileId"])])

    def list(self, **kwargs: object) -> FakeRequest:
        self.list_calls.append(kwargs)
        query = str(kwargs["q"])
        folder_id = query.split("'", maxsplit=2)[1]
        return FakeRequest(self._children.get(folder_id, {"files": []}))

    def get_media(self, **kwargs: object) -> FakeRequest:
        self.content_download_calls += 1
        raise AssertionError("Image/file content download must never be called")

    def export_media(self, **kwargs: object) -> FakeRequest:
        self.content_download_calls += 1
        raise AssertionError("Spreadsheet export must never be called")


class FakeDrive:
    def __init__(self, files_resource: FakeFilesResource) -> None:
        self._files_resource = files_resource

    def files(self) -> FakeFilesResource:
        return self._files_resource


class FakeValuesResource:
    def __init__(self, samples: dict[str, dict[str, object]]) -> None:
        self._samples = samples
        self.get_calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> FakeRequest:
        self.get_calls.append(kwargs)
        range_name = str(kwargs["range"])
        return FakeRequest(self._samples.get(range_name, {}))


class FakeSpreadsheetsResource:
    def __init__(
        self,
        metadata: dict[str, object],
        samples: dict[str, dict[str, object]],
    ) -> None:
        self._metadata = metadata
        self._values = FakeValuesResource(samples)
        self.get_calls: list[dict[str, object]] = []
        self.export_calls = 0

    def get(self, **kwargs: object) -> FakeRequest:
        self.get_calls.append(kwargs)
        return FakeRequest(self._metadata)

    def values(self) -> FakeValuesResource:
        return self._values


class FakeSheets:
    def __init__(self, spreadsheets: FakeSpreadsheetsResource) -> None:
        self._spreadsheets = spreadsheets

    def spreadsheets(self) -> FakeSpreadsheetsResource:
        return self._spreadsheets


class FakeFactory:
    def __init__(self, clients: GoogleClients) -> None:
        self._clients = clients
        self.calls = 0

    def create(self, settings: object) -> GoogleClients:
        self.calls += 1
        return self._clients


def folder(identifier: str, name: str) -> dict[str, str]:
    return {"id": identifier, "name": name, "mimeType": FOLDER_MIME_TYPE}


def file(identifier: str, name: str, mime_type: str) -> dict[str, str]:
    return {"id": identifier, "name": name, "mimeType": mime_type}


class SupplierInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        credentials_path = Path(self.temporary_directory.name) / "fake.json"
        credentials_path.write_text("{}", encoding="utf-8")
        self.values = {
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(credentials_path),
            "CLM_SPREADSHEET_ID": "spreadsheet_secret_ID_1234567890",
            "CLM_DRIVE_FOLDER_ID": "clm_root_secret_ID_1234567890",
            "MD_DRIVE_FOLDER_ID": "md_root_secret_ID_1234567890",
            "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_READONLY_SCOPE,
            "GOOGLE_SHEETS_SCOPE": GOOGLE_SHEETS_READONLY_SCOPE,
            "GOOGLE_PROXY_MODE": "none",
            "GOOGLE_PROXY_HOST": "",
            "GOOGLE_PROXY_PORT": "",
            "GOOGLE_PROXY_RDNS": "true",
        }
        self.settings = load_google_config(self.values)
        root_metadata = {
            self.values["CLM_DRIVE_FOLDER_ID"]: {
                "name": "CLM (ClimaxDoll)",
                "mimeType": FOLDER_MIME_TYPE,
                "trashed": False,
            },
            self.values["MD_DRIVE_FOLDER_ID"]: {
                "name": "Full Silicone Doll Pictures",
                "mimeType": FOLDER_MIME_TYPE,
                "trashed": False,
            },
        }
        children = {
            self.values["CLM_DRIVE_FOLDER_ID"]: {
                "files": [
                    folder("clm-ulw-internal-id", "CLM ULW"),
                    folder("option-internal-id", "Option"),
                    file("logo-internal-id", "logo.png", "image/png"),
                ]
            },
            "clm-ulw-internal-id": {
                "files": [file("ulw-pdf-id", "catalog.pdf", "application/pdf")]
            },
            "option-internal-id": {
                "files": [
                    folder("option-ultra-internal-id", "CLM Ultra"),
                    folder("option-pro-internal-id", "CLM Pro"),
                ]
            },
            "option-ultra-internal-id": {
                "files": [
                    folder("heads-internal-id", "Heads"),
                    file("ultra-jpg-id", "face.JPG", "image/jpeg"),
                ]
            },
            "heads-internal-id": {
                "files": [file("head-webp-id", "head.webp", "image/webp")]
            },
            "option-pro-internal-id": {
                "files": [file("pro-png-id", "skin.png", "image/png")]
            },
            self.values["MD_DRIVE_FOLDER_ID"]: {
                "files": [
                    folder("susan-internal-id", "Susan"),
                    folder("alice-internal-id", "Alice"),
                ]
            },
            "susan-internal-id": {
                "files": [
                    folder("susan-detail-internal-id", "Details"),
                    file("susan-main-id", "Susan-main.JPEG", "image/jpeg"),
                    file("susan-info-id", "readme.txt", "text/plain"),
                ]
            },
            "susan-detail-internal-id": {
                "files": [
                    file("susan-webp-id", "Susan-detail.webp", "image/webp"),
                    file("susan-png-id", "Susan-spec.PNG", "image/png"),
                ]
            },
            "alice-internal-id": {
                "files": [file("alice-jpg-id", "Alice.jpg", "image/jpeg")]
            },
        }
        metadata = {
            "spreadsheetId": self.values["CLM_SPREADSHEET_ID"],
            "properties": {"title": "CLM Product Structure"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 101,
                        "title": "Products",
                        "index": 0,
                        "hidden": False,
                        "gridProperties": {
                            "rowCount": 500,
                            "columnCount": 52,
                            "frozenRowCount": 2,
                            "frozenColumnCount": 1,
                        },
                    }
                },
                {
                    "properties": {
                        "sheetId": 102,
                        "title": "Empty",
                        "index": 1,
                        "hidden": True,
                        "gridProperties": {"rowCount": 100, "columnCount": 10},
                    }
                },
            ],
        }
        samples = {
            "'Products'!A1:AZ10": {
                "range": "Products!A1:AZ10",
                "values": [
                    ["CLM Products"],
                    ["SKU", "Name", "Image formula"],
                    [
                        "SKU-001",
                        "Susan",
                        '=IMAGE("https://private.example/image.jpg")',
                    ],
                ]
                + [[f"row-{index}"] for index in range(4, 15)],
            },
            "'Empty'!A1:AZ10": {},
        }
        self.files_resource = FakeFilesResource(root_metadata, children)
        self.spreadsheets_resource = FakeSpreadsheetsResource(metadata, samples)
        clients = GoogleClients(
            drive=FakeDrive(self.files_resource),
            sheets=FakeSheets(self.spreadsheets_resource),
        )
        self.factory = FakeFactory(clients)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(self, *, max_depth: int = 4) -> dict[str, object]:
        return SupplierInventoryRunner(
            self.settings,
            self.factory,
            max_depth=max_depth,
            redactor=google_redactor_for_settings(self.settings),
        ).run()

    def test_cli_parses_supplier_inventory_and_depth_bounds(self) -> None:
        default = build_parser().parse_args(["supplier-inventory"])
        explicit = build_parser().parse_args(
            ["supplier-inventory", "--max-depth", "6"]
        )

        self.assertEqual(default.command, "supplier-inventory")
        self.assertEqual(default.max_depth, 4)
        self.assertEqual(explicit.max_depth, 6)
        for invalid in ("0", "7"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(
                        ["supplier-inventory", "--max-depth", invalid]
                    )

    def test_clm_tree_is_recursive_and_contains_no_internal_ids(self) -> None:
        report = self._run()
        clm = report["clm"]
        tree = clm["tree"]
        by_name = {item["name"]: item for item in tree}

        self.assertEqual(clm["root_name"], "CLM (ClimaxDoll)")
        self.assertEqual(by_name["Option"]["depth"], 1)
        self.assertEqual(by_name["CLM Ultra"]["depth"], 2)
        self.assertEqual(by_name["Heads"]["depth"], 3)
        self.assertEqual(by_name["head.webp"]["depth"], 4)
        self.assertEqual(
            by_name["head.webp"]["path_labels"],
            ["CLM (ClimaxDoll)", "Option", "CLM Ultra", "Heads"],
        )
        serialized = json.dumps(report)
        for forbidden in (
            "option-internal-id",
            "head-webp-id",
            self.values["CLM_DRIVE_FOLDER_ID"],
        ):
            self.assertNotIn(forbidden, serialized)

    def test_max_depth_stops_recursion(self) -> None:
        report = self._run(max_depth=2)
        names = [item["name"] for item in report["clm"]["tree"]]
        listed_folder_ids = [
            str(call["q"]).split("'", maxsplit=2)[1]
            for call in self.files_resource.list_calls
        ]

        self.assertIn("CLM Ultra", names)
        self.assertNotIn("Heads", names)
        self.assertNotIn("option-ultra-internal-id", listed_folder_ids)
        self.assertTrue(any("max_depth" in warning for warning in report["warnings"]))

    def test_item_limit_marks_folder_truncated(self) -> None:
        many_items = [
            file(f"id-{index}", f"asset-{index}.jpg", "image/jpeg")
            for index in range(DRIVE_ITEM_LIMIT + 1)
        ]
        self.files_resource._children[self.values["CLM_DRIVE_FOLDER_ID"]] = {
            "files": many_items,
            "nextPageToken": "must-not-be-reported",
        }

        report = self._run(max_depth=1)
        root = report["clm"]["tree"][0]
        serialized = json.dumps(report)

        self.assertEqual(root["child_count"], DRIVE_ITEM_LIMIT)
        self.assertTrue(root["truncated"])
        self.assertEqual(len(report["clm"]["tree"]), DRIVE_ITEM_LIMIT + 1)
        self.assertNotIn("must-not-be-reported", serialized)
        clm_list_call = self.files_resource.list_calls[0]
        self.assertEqual(clm_list_call["pageSize"], DRIVE_ITEM_LIMIT)

    def test_option_folder_and_each_series_are_summarized(self) -> None:
        report = self._run()
        summaries = {
            summary["series_name"]: summary
            for summary in report["clm"]["option_summary"]
        }

        self.assertEqual(set(summaries), {"CLM Ultra", "CLM Pro"})
        self.assertEqual(summaries["CLM Ultra"]["folder_count"], 1)
        self.assertEqual(summaries["CLM Ultra"]["file_count"], 2)
        self.assertEqual(summaries["CLM Ultra"]["extensions"], ["jpg", "webp"])
        self.assertEqual(
            summaries["CLM Ultra"]["first_level_names"], ["Heads", "face.JPG"]
        )
        self.assertEqual(summaries["CLM Pro"]["file_count"], 1)

    def test_md_top_level_summary_counts_images_and_extensions(self) -> None:
        report = self._run()
        summaries = {
            summary["name"]: summary
            for summary in report["md"]["top_level_summary"]
        }

        self.assertEqual(set(summaries), {"Susan", "Alice"})
        self.assertEqual(summaries["Susan"]["subfolder_count"], 1)
        self.assertEqual(summaries["Susan"]["image_count"], 3)
        self.assertEqual(
            summaries["Susan"]["extensions"],
            {"jpeg": 1, "png": 1, "txt": 1, "webp": 1},
        )
        self.assertLessEqual(len(summaries["Susan"]["file_name_samples"]), 10)
        self.assertEqual(self.files_resource.content_download_calls, 0)

    def test_spreadsheet_metadata_and_samples_are_bounded(self) -> None:
        report = self._run()
        spreadsheet = report["spreadsheet"]
        products, empty = spreadsheet["sheets"]
        sample_calls = self.spreadsheets_resource._values.get_calls

        self.assertEqual(spreadsheet["title"], "CLM Product Structure")
        self.assertEqual(spreadsheet["sheet_count"], 2)
        self.assertEqual(products["title"], "Products")
        self.assertEqual(products["index"], 0)
        self.assertFalse(products["hidden"])
        self.assertEqual(products["frozen_row_count"], 2)
        self.assertEqual(products["frozen_column_count"], 1)
        self.assertEqual(products["detected_header_rows"], [1, 2])
        self.assertEqual(
            products["possible_field_names"], ["SKU", "Name", "Image formula"]
        )
        self.assertEqual(products["non_empty_columns"], ["A", "B", "C"])
        self.assertEqual(len(products["sample_rows"]), 10)
        self.assertNotIn("row-11", json.dumps(products))
        self.assertTrue(empty["sample_read_success"])
        self.assertEqual(empty["sample_rows"], [])
        self.assertEqual(
            [item["range"] for item in sample_calls],
            ["'Products'!A1:AZ10", "'Empty'!A1:AZ10"],
        )
        for item in sample_calls:
            self.assertEqual(item["majorDimension"], "ROWS")
            self.assertEqual(item["valueRenderOption"], "FORMULA")
        metadata_call = self.spreadsheets_resource.get_calls[0]
        self.assertNotIn("sheetId", str(metadata_call["fields"]))

    def test_report_omits_all_ids_urls_and_credentials(self) -> None:
        report = self._run()
        serialized = json.dumps(report, sort_keys=True)

        for forbidden in (
            self.values["CLM_DRIVE_FOLDER_ID"],
            self.values["MD_DRIVE_FOLDER_ID"],
            self.values["CLM_SPREADSHEET_ID"],
            "option-internal-id",
            "sheetId",
            "101",
            "private.example",
            "https://",
            "service-account@example.invalid",
            "private_key",
            "access_token",
            "refresh_token",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("[REDACTED_URL]", serialized)

    def test_safe_writer_drops_injected_credentials_ids_and_urls(self) -> None:
        report = self._run()
        report.update(
            {
                "file_id": "forbidden-file-id",
                "folder_id": "forbidden-folder-id",
                "spreadsheet_id": "forbidden-spreadsheet-id",
                "private_key": "forbidden-private-key",
                "client_email": "service-account@example.invalid",
                "download_url": "https://download.invalid/secret",
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "supplier-inventory.json"
            SafeJsonReportWriter(
                report_path, google_redactor_for_settings(self.settings)
            ).write(report)
            saved = report_path.read_text(encoding="utf-8")

        for forbidden in (
            "forbidden-file-id",
            "forbidden-folder-id",
            "forbidden-spreadsheet-id",
            "forbidden-private-key",
            "service-account@example.invalid",
            "download.invalid",
        ):
            self.assertNotIn(forbidden, saved)
        self.assertEqual(json.loads(saved)["write_requests_performed"], 0)

    def test_recursive_errors_redact_internal_folder_ids_and_urls(self) -> None:
        self.files_resource._children["option-internal-id"] = RuntimeError(
            "folder=option-internal-id url=https://private.example/folder"
        )

        report = self._run()
        serialized = json.dumps(report, sort_keys=True)

        self.assertEqual(report["status"], "partial")
        self.assertNotIn("option-internal-id", serialized)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("https://", serialized)
        self.assertIn("[REDACTED_URL]", serialized)

    def test_google_write_methods_remain_blocked_and_zero(self) -> None:
        forbidden_operations = (
            "drive.files.create",
            "drive.files.update",
            "drive.files.copy",
            "drive.files.delete",
            "drive.permissions.create",
            "drive.permissions.update",
            "drive.permissions.delete",
            "sheets.spreadsheets.create",
            "sheets.spreadsheets.batchUpdate",
            "sheets.values.update",
            "sheets.values.append",
            "sheets.values.batchUpdate",
        )
        for operation in forbidden_operations:
            with self.subTest(operation=operation):
                with self.assertRaises(GoogleOperationBlocked):
                    ensure_google_operation_allowed(operation)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                with self.assertRaises(GoogleOperationBlocked):
                    ensure_google_http_method_allowed(method)

        report = self._run()
        self.assertEqual(report["write_requests_performed"], 0)

    def test_safety_failure_prevents_google_client_creation(self) -> None:
        unsafe = replace(
            self.settings,
            sheets_scope="https://www.googleapis.com/auth/spreadsheets",
        )

        with self.assertRaisesRegex(ConfigError, "GOOGLE_SHEETS_SCOPE"):
            SupplierInventoryRunner(unsafe, self.factory).run()

        self.assertEqual(self.factory.calls, 0)
        self.assertEqual(self.files_resource.get_calls, [])
        self.assertEqual(self.files_resource.list_calls, [])
        self.assertEqual(self.spreadsheets_resource.get_calls, [])


if __name__ == "__main__":
    unittest.main()
