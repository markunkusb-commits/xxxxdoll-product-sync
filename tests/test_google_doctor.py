from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
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
    ensure_google_operation_allowed,
    google_redactor_for_settings,
)
from sync_worker.google_doctor import GoogleDoctorRunner  # noqa: E402
from sync_worker.report import SafeJsonReportWriter  # noqa: E402


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class MockGoogleFactory:
    def __init__(self, clients: GoogleClients) -> None:
        self.clients = clients
        self.calls = 0

    def create(self, settings: object) -> GoogleClients:
        self.calls += 1
        return self.clients


class GoogleDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        credentials_path = Path(self.temporary_directory.name) / "fake.json"
        credentials_path.write_text("{}", encoding="utf-8")
        self.values = {
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(credentials_path),
            "CLM_SPREADSHEET_ID": "spreadsheet_ID_1234567890",
            "CLM_DRIVE_FOLDER_ID": "clm_folder_ID_1234567890",
            "MD_DRIVE_FOLDER_ID": "md_folder_ID_1234567890",
            "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_READONLY_SCOPE,
            "GOOGLE_SHEETS_SCOPE": GOOGLE_SHEETS_READONLY_SCOPE,
        }
        self.settings = load_google_config(self.values)
        self.drive = MagicMock()
        self.sheets = MagicMock()
        self.factory = MockGoogleFactory(
            GoogleClients(drive=self.drive, sheets=self.sheets)
        )
        self._configure_success_responses()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _configure_success_responses(self) -> None:
        files = self.drive.files.return_value
        files.get.return_value.execute.side_effect = [
            {"name": "CLM Root", "mimeType": FOLDER_MIME_TYPE, "trashed": False},
            {"name": "MD Root", "mimeType": FOLDER_MIME_TYPE, "trashed": False},
        ]
        files.list.return_value.execute.side_effect = [
            {
                "files": [
                    {
                        "id": "must-not-be-reported-clm-child-id",
                        "name": "Products",
                        "mimeType": FOLDER_MIME_TYPE,
                        "modifiedTime": "2026-08-01T00:00:00Z",
                    },
                    {
                        "id": "must-not-be-reported-image-id",
                        "name": "catalog.csv",
                        "mimeType": "text/csv",
                        "modifiedTime": "2026-08-02T00:00:00Z",
                        "webContentLink": "https://download.invalid/private",
                    },
                ]
            },
            {
                "files": [
                    {
                        "id": "must-not-be-reported-md-child-id",
                        "name": "Reference",
                        "mimeType": FOLDER_MIME_TYPE,
                        "modifiedTime": "2026-08-03T00:00:00Z",
                    }
                ]
            },
        ]

        spreadsheets = self.sheets.spreadsheets.return_value
        spreadsheets.get.return_value.execute.return_value = {
            "spreadsheetId": self.values["CLM_SPREADSHEET_ID"],
            "properties": {"title": "CLM Products"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 101,
                        "title": "Products",
                        "gridProperties": {"rowCount": 500, "columnCount": 26},
                    }
                },
                {
                    "properties": {
                        "sheetId": 102,
                        "title": "Pricing",
                        "gridProperties": {"rowCount": 100, "columnCount": 10},
                    }
                },
            ],
        }
        spreadsheets.values.return_value.get.return_value.execute.side_effect = [
            {
                "range": "Products!A1:Z5",
                "values": [
                    ["private-cell-alpha", "", 3],
                    [None, "private-cell-beta"],
                ],
            },
            {"range": "Pricing!A1:Z5", "values": [[]]},
        ]

    def _run(self) -> dict[str, object]:
        return GoogleDoctorRunner(
            self.settings,
            self.factory,
            redactor=google_redactor_for_settings(self.settings),
            clock=lambda: datetime(2026, 8, 4, tzinfo=timezone.utc),
        ).run()

    def test_drive_files_get_and_list_return_only_safe_metadata(self) -> None:
        report = self._run()
        files = self.drive.files.return_value

        self.assertTrue(report["drive_api_status"])
        self.assertEqual(report["clm_folder_name"], "CLM Root")
        self.assertEqual(report["md_folder_name"], "MD Root")
        self.assertEqual(files.get.call_count, 2)
        self.assertEqual(files.list.call_count, 2)
        for call in files.list.call_args_list:
            self.assertEqual(call.kwargs["pageSize"], 100)
            self.assertIn("in parents and trashed = false", call.kwargs["q"])
            self.assertEqual(
                call.kwargs["fields"], "files(name,mimeType,modifiedTime)"
            )
        self.assertEqual(
            set(report["clm_child_summary"][0]),
            {"name", "mime_type", "modified_time", "is_folder"},
        )
        self.assertTrue(report["clm_child_summary"][0]["is_folder"])
        self.assertFalse(report["clm_child_summary"][1]["is_folder"])

    def test_spreadsheet_metadata_and_small_samples_are_summarized(self) -> None:
        report = self._run()
        spreadsheets = self.sheets.spreadsheets.return_value
        values_get = spreadsheets.values.return_value.get

        self.assertTrue(report["spreadsheet_access"])
        self.assertTrue(report["sheets_api_status"])
        self.assertEqual(report["spreadsheet_title"], "CLM Products")
        self.assertEqual(spreadsheets.get.call_count, 1)
        self.assertEqual(values_get.call_count, 2)
        self.assertEqual(
            [call.kwargs["range"] for call in values_get.call_args_list],
            ["'Products'!A1:Z5", "'Pricing'!A1:Z5"],
        )
        first = report["sheet_summaries"][0]
        self.assertEqual(first["sheet_id"], 101)
        self.assertEqual(first["row_count"], 500)
        self.assertEqual(first["column_count"], 26)
        self.assertTrue(first["sample_read_success"])
        self.assertEqual(first["returned_row_count"], 2)
        self.assertEqual(first["returned_column_count"], 3)
        self.assertEqual(first["non_empty_cell_count"], 3)

    def test_cell_values_ids_and_download_links_never_enter_report(self) -> None:
        report = self._run()
        serialized = json.dumps(report, sort_keys=True)

        for forbidden in (
            "private-cell-alpha",
            "private-cell-beta",
            self.values["CLM_SPREADSHEET_ID"],
            self.values["CLM_DRIVE_FOLDER_ID"],
            self.values["MD_DRIVE_FOLDER_ID"],
            "must-not-be-reported-clm-child-id",
            "must-not-be-reported-image-id",
            "must-not-be-reported-md-child-id",
            "download.invalid",
            "webContentLink",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_all_documented_google_write_operations_are_blocked(self) -> None:
        forbidden_operations = (
            "drive.files.create",
            "drive.files.update",
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

    def test_safety_failure_prevents_client_factory_creation(self) -> None:
        unsafe_settings = replace(
            self.settings,
            drive_scope="https://www.googleapis.com/auth/drive",
        )

        with self.assertRaisesRegex(ConfigError, "GOOGLE_DRIVE_SCOPE"):
            GoogleDoctorRunner(unsafe_settings, self.factory).run()

        self.assertEqual(self.factory.calls, 0)

    def test_authentication_and_request_counters_are_read_only(self) -> None:
        report = self._run()

        self.assertEqual(self.factory.calls, 1)
        self.assertTrue(report["service_account_authentication"])
        self.assertEqual(report["read_requests_performed"], 7)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_report_writer_removes_google_credentials_and_full_ids(self) -> None:
        report = self._run()
        report.update(
            {
                "private_key": "-----BEGIN PRIVATE KEY-----fake-----END PRIVATE KEY-----",
                "private_key_id": "fake-private-key-id",
                "client_email": "service-account@example.invalid",
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "file_id": self.values["CLM_DRIVE_FOLDER_ID"],
                "safe_error": (
                    '"client_email":"service-account@example.invalid" '
                    '"private_key":"unsafe"'
                ),
            }
        )
        redactor = google_redactor_for_settings(self.settings)

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "google-doctor-report.json"
            SafeJsonReportWriter(path, redactor).write(report)
            saved_text = path.read_text(encoding="utf-8")
            saved = json.loads(saved_text)

        for forbidden in (
            "fake-private-key-id",
            "service-account@example.invalid",
            "fake-access-token",
            "fake-refresh-token",
            self.values["CLM_DRIVE_FOLDER_ID"],
            "BEGIN PRIVATE KEY",
        ):
            self.assertNotIn(forbidden, saved_text)
        for forbidden_key in (
            "private_key",
            "private_key_id",
            "client_email",
            "access_token",
            "refresh_token",
            "file_id",
        ):
            self.assertNotIn(forbidden_key, saved)
        self.assertEqual(saved["write_requests_performed"], 0)

    def test_cli_parser_accepts_google_doctor_without_running_it(self) -> None:
        arguments = build_parser().parse_args(["google-doctor"])

        self.assertEqual(arguments.command, "google-doctor")


if __name__ == "__main__":
    unittest.main()
