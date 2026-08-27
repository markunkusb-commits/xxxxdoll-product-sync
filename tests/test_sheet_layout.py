from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.cli import build_parser, main  # noqa: E402
from sync_worker.config import (  # noqa: E402
    ConfigError,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    load_google_sheets_readonly_config,
)
from sync_worker.google_api import (  # noqa: E402
    GoogleClients,
    GoogleOperationBlocked,
    ReadOnlyGoogleGateway,
    ReadOnlySheetsGateway,
    ensure_google_http_method_allowed,
    ensure_google_operation_allowed,
    google_redactor_for_settings,
)
from sync_worker.report import SafeJsonReportWriter  # noqa: E402
from sync_worker.sheet_layout import (  # noqa: E402
    SheetLayoutInspector,
    SheetLayoutValidationError,
    column_index_to_label,
    column_label_to_index,
    parse_a1_range,
    safe_sheet_report_filename,
)


class FakeRequest:
    def __init__(self, result: object) -> None:
        self._result = result

    def execute(self) -> object:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class FakeSpreadsheets:
    def __init__(self, result: object) -> None:
        self.result = result
        self.get_calls: list[dict[str, object]] = []
        self.write_calls = 0

    def get(self, **kwargs: object) -> FakeRequest:
        self.get_calls.append(kwargs)
        return FakeRequest(self.result)

    def batchUpdate(self, **kwargs: object) -> FakeRequest:
        self.write_calls += 1
        raise AssertionError("Write API must not be called")


class FakeSheets:
    def __init__(self, spreadsheets: FakeSpreadsheets) -> None:
        self._spreadsheets = spreadsheets

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


class FakeFactory:
    def __init__(self, sheets: FakeSheets) -> None:
        self.sheets = sheets
        self.calls = 0
        self.full_create_calls = 0
        self.drive_client_created = False

    def create_sheets_readonly(self, settings: object) -> object:
        self.calls += 1
        settings.validate_sheets_readonly()
        return self.sheets

    def create(self, settings: object) -> object:
        self.full_create_calls += 1
        raise AssertionError("full Google client factory must not be used")


def _row_values(length: int, entries: dict[int, dict[str, object]]) -> list[object]:
    values: list[object] = [{} for _ in range(length)]
    for index, value in entries.items():
        values[index] = value
    return values


class SheetLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        credentials_path = Path(self.temporary_directory.name) / "fake.json"
        credentials_path.write_text("{}", encoding="utf-8")
        self.values = {
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(credentials_path),
            "CLM_SPREADSHEET_ID": "spreadsheet_secret_ID_1234567890",
            "CLM_DRIVE_FOLDER_ID": "",
            "MD_DRIVE_FOLDER_ID": "",
            "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
            "GOOGLE_SHEETS_SCOPE": GOOGLE_SHEETS_READONLY_SCOPE,
            "GOOGLE_PROXY_MODE": "socks5",
            "GOOGLE_PROXY_HOST": "127.0.0.1",
            "GOOGLE_PROXY_PORT": "26001",
            "GOOGLE_PROXY_RDNS": "true",
        }
        self.settings = load_google_sheets_readonly_config(self.values)
        row_8 = _row_values(
            32,
            {
                0: {"formattedValue": "◆ CLM Classic ◆"},
                31: {"formattedValue": "More collocation"},
            },
        )
        row_9 = _row_values(
            27,
            {
                7: {"formattedValue": "Height(Model)"},
                13: {
                    "formattedValue": "J59cm",
                    "userEnteredValue": {
                        "formulaValue": '=HYPERLINK("https://secret.invalid")'
                    },
                },
                21: {"formattedValue": "Upper arm circumference"},
                26: {"formattedValue": "8.5cm(3.34in)"},
            },
        )
        row_10 = _row_values(
            27,
            {
                7: {"formattedValue": "Upper Chest"},
                13: {"formattedValue": "33cm(12.99in)"},
            },
        )
        self.response = {
            "spreadsheetId": self.values["CLM_SPREADSHEET_ID"],
            "properties": {"title": "CLM Price Workbook"},
            "namedRanges": [{"name": "must-not-enter-report"}],
            "sheets": [
                {
                    "properties": {
                        "title": "RMB Price List",
                        "sheetId": 987654,
                    },
                    "merges": [
                        {
                            "sheetId": 987654,
                            "startRowIndex": 7,
                            "endRowIndex": 8,
                            "startColumnIndex": 1,
                            "endColumnIndex": 8,
                        },
                        {
                            "sheetId": 987654,
                            "startRowIndex": 9,
                            "endRowIndex": 11,
                            "startColumnIndex": 25,
                            "endColumnIndex": 27,
                        },
                    ],
                    "data": [
                        {
                            "startRow": 7,
                            "startColumn": 1,
                            "rowData": [
                                {"values": row_8},
                                {"values": row_9},
                                {"values": row_10},
                            ],
                        },
                        {
                            "startRow": 9,
                            "startColumn": 44,
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "formattedValue": (
                                                "Silicone body + vinyl head "
                                                "https://docs.google.com/private"
                                            )
                                        }
                                    ]
                                }
                            ],
                        },
                    ],
                },
                {
                    "properties": {"title": "Other Sheet", "sheetId": 123},
                    "data": [
                        {
                            "startRow": 0,
                            "startColumn": 0,
                            "rowData": [
                                {"values": [{"formattedValue": "do-not-read"}]}
                            ],
                        }
                    ],
                },
            ],
        }
        self.spreadsheets = FakeSpreadsheets(self.response)
        self.factory = FakeFactory(FakeSheets(self.spreadsheets))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(
        self,
        *,
        sheet_title: str = "RMB Price List",
        a1_range: str = "A1:AZ50",
    ) -> dict[str, object]:
        return SheetLayoutInspector(
            self.settings,
            self.factory,
            sheet_title=sheet_title,
            a1_range=a1_range,
            redactor=google_redactor_for_settings(self.settings),
        ).run()

    def test_inspector_uses_sheets_gateway_without_dummy_drive(self) -> None:
        with patch(
            "sync_worker.sheet_layout.ReadOnlySheetsGateway",
            wraps=ReadOnlySheetsGateway,
        ) as gateway:
            report = self._run()
        gateway.assert_called_once_with(self.factory.sheets)
        self.assertEqual(report["read_requests_performed"], 1)
        self.assertEqual(report["drive_requests_performed"], 0)
        self.assertEqual(report["download_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_sheets_gateway_blocks_cross_service_and_write_execution(self) -> None:
        gateway = ReadOnlySheetsGateway(self.factory.sheets)
        request = MagicMock()
        for operation in (
            "drive.files.get", "drive.files.list", "drive.files.create",
            "drive.files.update", "drive.files.delete", "drive.files.get_media",
            "drive.files.export", "sheets.spreadsheets.batchUpdate",
            "sheets.values.update", "sheets.values.append",
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(GoogleOperationBlocked):
                    gateway._execute(operation, request)
        request.execute.assert_not_called()
        self.assertEqual(gateway.counters.read_requests_performed, 0)
        self.assertEqual(gateway.counters.write_requests_performed, 0)

    def test_temporary_diagnostic_queries_are_removed_from_gateways(self) -> None:
        for gateway_type in (ReadOnlySheetsGateway, ReadOnlyGoogleGateway):
            for method in (
                "batch_get_sheet_cell_shapes",
                "batch_get_sheet_values_for_parity",
                "batch_get_sheet_grid_strings_for_parity",
            ):
                with self.subTest(gateway=gateway_type.__name__, method=method):
                    self.assertFalse(hasattr(gateway_type, method))

    def test_legacy_gateway_retains_exact_values_batch_read(self) -> None:
        drive = MagicMock()
        sheets = MagicMock()
        values = sheets.spreadsheets.return_value.values.return_value
        values.batchGet.return_value.execute.return_value = {"valueRanges": []}
        gateway = ReadOnlyGoogleGateway(GoogleClients(drive=drive, sheets=sheets))
        response = gateway.batch_get_sheet_cells("mock-id", "Mock Sheet", ["I12"])
        self.assertEqual(response, {"valueRanges": []})
        values.batchGet.assert_called_once_with(
            spreadsheetId="mock-id",
            ranges=["'Mock Sheet'!I12"],
            majorDimension="ROWS",
            valueRenderOption="FORMATTED_VALUE",
        )
        drive.files.assert_not_called()
        self.assertEqual(gateway.counters.read_requests_performed, 1)
        self.assertEqual(gateway.counters.write_requests_performed, 0)

    def test_cli_parses_required_sheet_and_range(self) -> None:
        arguments = build_parser().parse_args(
            [
                "inspect-sheet-layout",
                "--sheet",
                "RMB Price List",
                "--range",
                "A1:AZ50",
            ]
        )

        self.assertEqual(arguments.command, "inspect-sheet-layout")
        self.assertEqual(arguments.sheet, "RMB Price List")
        self.assertEqual(arguments.a1_range, "A1:AZ50")
        for missing in (
            ["inspect-sheet-layout", "--range", "A1:B2"],
            ["inspect-sheet-layout", "--sheet", "RMB Price List"],
        ):
            with self.subTest(missing=missing):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(missing)

    def test_valid_a1_ranges_parse_with_exact_dimensions(self) -> None:
        first = parse_a1_range("A1:AZ50")
        second = parse_a1_range("b8:as40")

        self.assertEqual(first.a1, "A1:AZ50")
        self.assertEqual(first.row_count, 50)
        self.assertEqual(first.column_count, 52)
        self.assertEqual(first.cell_count, 2600)
        self.assertEqual(second.a1, "B8:AS40")
        self.assertEqual(second.start_column_index, 2)
        self.assertEqual(second.end_column_index, 45)

    def test_invalid_range_shapes_are_rejected_before_client_creation(self) -> None:
        invalid_ranges = (
            "A:A",
            "1:100",
            "A1:",
            "A1:B2,C3:D4",
            "'RMB Price List'!A1:B2",
            "RMB Price List!A1:B2",
            "A0:B2",
            "B2:A1",
            "A1:ZZZ100000",
        )
        for a1_range in invalid_ranges:
            with self.subTest(a1_range=a1_range):
                with self.assertRaises(SheetLayoutValidationError):
                    SheetLayoutInspector(
                        self.settings,
                        self.factory,
                        sheet_title="RMB Price List",
                        a1_range=a1_range,
                    )

        self.assertEqual(self.factory.calls, 0)
        self.assertEqual(self.spreadsheets.get_calls, [])

    def test_each_range_safety_limit_is_rejected(self) -> None:
        cases = (
            ("A1:A101", "100 rows"),
            ("A1:BA1", "column AZ"),
            ("A1:AZ101", "5200 cells"),
        )
        for a1_range, expected in cases:
            with self.subTest(a1_range=a1_range):
                with self.assertRaisesRegex(SheetLayoutValidationError, expected):
                    parse_a1_range(a1_range)

    def test_sheet_title_validation_happens_before_client_creation(self) -> None:
        for title in ("", " ", "x" * 151, "bad\nname"):
            with self.subTest(title=title):
                with self.assertRaises(SheetLayoutValidationError):
                    SheetLayoutInspector(
                        self.settings,
                        self.factory,
                        sheet_title=title,
                        a1_range="A1:B2",
                    )

        self.assertEqual(self.factory.calls, 0)

    def test_spreadsheets_get_is_one_bounded_grid_request(self) -> None:
        report = self._run()

        self.assertEqual(len(self.spreadsheets.get_calls), 1)
        call = self.spreadsheets.get_calls[0]
        self.assertEqual(call["spreadsheetId"], self.values["CLM_SPREADSHEET_ID"])
        self.assertTrue(call["includeGridData"])
        self.assertEqual(call["ranges"], ["'RMB Price List'!A1:AZ50"])
        fields = str(call["fields"])
        self.assertIn("formattedValue", fields)
        self.assertIn("merges", fields)
        for forbidden in (
            "formulaValue",
            "userEnteredValue",
            "developerMetadata",
            "namedRanges",
            "charts",
            "conditionalFormats",
            "protectedRanges",
            "notes",
        ):
            self.assertNotIn(forbidden, fields)
        self.assertEqual(report["read_requests_performed"], 1)
        self.assertEqual(report["drive_requests_performed"], 0)
        self.assertEqual(report["download_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_formatted_values_and_absolute_coordinates_are_preserved(self) -> None:
        report = self._run()
        cells = {
            cell["coordinate"]: cell for cell in report["non_empty_cells"]
        }

        self.assertEqual(cells["B8"]["formatted_value"], "◆ CLM Classic ◆")
        self.assertEqual(cells["AG8"]["formatted_value"], "More collocation")
        self.assertEqual(cells["I9"]["formatted_value"], "Height(Model)")
        self.assertEqual(cells["O9"]["formatted_value"], "J59cm")
        self.assertEqual(cells["W9"]["formatted_value"], "Upper arm circumference")
        self.assertEqual(cells["AB9"]["formatted_value"], "8.5cm(3.34in)")
        self.assertEqual(cells["I10"]["formatted_value"], "Upper Chest")
        self.assertEqual(cells["O10"]["formatted_value"], "33cm(12.99in)")
        self.assertIn("Silicone body + vinyl head", cells["AS10"]["formatted_value"])
        self.assertEqual(cells["I9"]["row"], 9)
        self.assertEqual(cells["I9"]["column"], "I")
        self.assertEqual(cells["I9"]["column_index"], 9)
        self.assertNotIn("do-not-read", json.dumps(report))

    def test_formula_source_never_enters_report(self) -> None:
        serialized = json.dumps(self._run(), sort_keys=True)

        self.assertNotIn("formulaValue", serialized)
        self.assertNotIn("userEnteredValue", serialized)
        self.assertNotIn("HYPERLINK", serialized)
        self.assertNotIn("secret.invalid", serialized)
        self.assertIn("J59cm", serialized)

    def test_coordinate_conversion_handles_z_to_aa_boundary(self) -> None:
        self.assertEqual(column_label_to_index("Z"), 26)
        self.assertEqual(column_label_to_index("AA"), 27)
        self.assertEqual(column_label_to_index("AZ"), 52)
        self.assertEqual(column_index_to_label(26), "Z")
        self.assertEqual(column_index_to_label(27), "AA")
        self.assertEqual(column_index_to_label(52), "AZ")

    def test_merged_ranges_convert_end_exclusive_without_off_by_one(self) -> None:
        report = self._run()
        merges = {merge["range"]: merge for merge in report["merged_ranges"]}

        self.assertEqual(report["merged_range_count"], 2)
        self.assertEqual(
            merges["B8:H8"],
            {
                "range": "B8:H8",
                "start_row": 8,
                "end_row": 8,
                "start_column": "B",
                "end_column": "H",
                "anchor": "B8",
            },
        )
        self.assertEqual(merges["Z10:AA11"]["anchor"], "Z10")
        self.assertEqual(merges["Z10:AA11"]["end_row"], 11)
        self.assertEqual(merges["Z10:AA11"]["end_column"], "AA")

    def test_merge_anchor_is_marked_without_copying_to_interior_cells(self) -> None:
        report = self._run()
        cells = {
            cell["coordinate"]: cell for cell in report["non_empty_cells"]
        }

        self.assertTrue(cells["B8"]["is_merged"])
        self.assertTrue(cells["B8"]["is_merge_anchor"])
        self.assertEqual(cells["B8"]["merged_range"], "B8:H8")
        for coordinate in ("C8", "D8", "E8", "F8", "G8", "H8"):
            self.assertNotIn(coordinate, cells)

    def test_row_summary_keeps_original_coordinates_only(self) -> None:
        report = self._run()
        rows = {row["row"]: row["non_empty_cells"] for row in report["row_summary"]}

        self.assertEqual(
            [cell["coordinate"] for cell in rows[8]], ["B8", "AG8"]
        )
        self.assertEqual(
            [cell["coordinate"] for cell in rows[9]],
            ["I9", "O9", "W9", "AB9"],
        )

    def test_empty_grid_and_no_merges_are_safe(self) -> None:
        self.spreadsheets.result = {
            "properties": {"title": "Empty Workbook"},
            "sheets": [
                {
                    "properties": {"title": "RMB Price List", "sheetId": 1},
                    "data": [],
                }
            ],
        }

        report = self._run(a1_range="A1:B2")

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["non_empty_cell_count"], 0)
        self.assertEqual(report["non_empty_cells"], [])
        self.assertEqual(report["merged_range_count"], 0)
        self.assertEqual(report["merged_ranges"], [])
        self.assertEqual(report["row_summary"], [])

    def test_missing_grid_data_is_safe_and_warned(self) -> None:
        self.spreadsheets.result = {
            "properties": {"title": "Workbook"},
            "sheets": [{"properties": {"title": "RMB Price List"}}],
        }

        report = self._run(a1_range="A1:B2")

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["non_empty_cells"], [])
        self.assertTrue(any("GridData" in item for item in report["warnings"]))

    def test_malformed_or_missing_sheet_response_is_safely_reported(self) -> None:
        for response in (
            [],
            {"properties": {"title": "Workbook"}, "sheets": "bad"},
            {
                "properties": {"title": "Workbook"},
                "sheets": [{"properties": {"title": "Different"}}],
            },
        ):
            with self.subTest(response=response):
                self.spreadsheets.result = response
                report = self._run(a1_range="A1:B2")
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["write_requests_performed"], 0)

    def test_network_errors_are_redacted_and_write_count_remains_zero(self) -> None:
        self.spreadsheets.result = RuntimeError(
            "403 spreadsheet_secret_ID_1234567890 token=unsafe-token "
            "proxy=http://127.0.0.1:26001"
        )

        report = self._run(a1_range="A1:B2")
        serialized = json.dumps(report, sort_keys=True)

        self.assertEqual(report["status"], "error")
        self.assertEqual(report["read_requests_performed"], 1)
        self.assertEqual(report["write_requests_performed"], 0)
        for forbidden in (
            self.values["CLM_SPREADSHEET_ID"],
            "unsafe-token",
            "127.0.0.1",
            "26001",
            "http://",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_urls_and_explicit_credentials_in_cells_are_redacted(self) -> None:
        report = self._run()
        serialized = json.dumps(report, sort_keys=True)

        self.assertNotIn("docs.google.com", serialized)
        self.assertNotIn("https://", serialized)
        self.assertIn("[URL_REDACTED]", serialized)

        self.spreadsheets.result["sheets"][0]["data"][1]["rowData"][0][
            "values"
        ][0]["formattedValue"] = (
            "ck_short stock_status " + "ck_" + "a" * 24
        )
        second = json.dumps(self._run(), sort_keys=True)
        self.assertIn("ck_short", second)
        self.assertIn("stock_status", second)
        self.assertNotIn("ck_" + "a" * 24, second)

    def test_ids_and_credentials_never_enter_report_or_saved_json(self) -> None:
        report = self._run()
        report.update(
            {
                "sheetId": 987654,
                "spreadsheetId": self.values["CLM_SPREADSHEET_ID"],
                "fileId": "private-file-id",
                "client_email": "service-account@example.invalid",
                "private_key": "unsafe-private-key",
                "access_token": "unsafe-access-token",
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "sheet-layout-RMB-Price-List.json"
            SafeJsonReportWriter(
                path, google_redactor_for_settings(self.settings)
            ).write(report)
            saved = path.read_text(encoding="utf-8")

        for forbidden in (
            "987654",
            self.values["CLM_SPREADSHEET_ID"],
            "private-file-id",
            "service-account@example.invalid",
            "unsafe-private-key",
            "unsafe-access-token",
            "sheetId",
            "spreadsheetId",
            "fileId",
        ):
            self.assertNotIn(forbidden, saved)
        self.assertEqual(json.loads(saved)["write_requests_performed"], 0)

    def test_safe_report_filename_is_deterministic(self) -> None:
        self.assertEqual(
            safe_sheet_report_filename("RMB Price List"),
            "sheet-layout-RMB-Price-List.json",
        )
        self.assertNotIn("/", safe_sheet_report_filename("Price / 2026"))

    def test_all_google_writes_remain_blocked(self) -> None:
        for operation in (
            "sheets.spreadsheets.create",
            "sheets.spreadsheets.batchUpdate",
            "sheets.values.update",
            "sheets.values.append",
            "sheets.values.batchUpdate",
            "drive.files.create",
            "drive.files.update",
            "drive.files.copy",
            "drive.files.delete",
            "drive.permissions.create",
            "drive.permissions.update",
            "drive.permissions.delete",
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(GoogleOperationBlocked):
                    ensure_google_operation_allowed(operation)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                with self.assertRaises(GoogleOperationBlocked):
                    ensure_google_http_method_allowed(method)
        self.assertEqual(self.spreadsheets.write_calls, 0)

    def test_google_safety_failure_prevents_client_creation(self) -> None:
        unsafe = replace(
            self.settings,
            sheets_scope="https://www.googleapis.com/auth/spreadsheets",
        )
        inspector = SheetLayoutInspector(
            unsafe,
            self.factory,
            sheet_title="RMB Price List",
            a1_range="A1:B2",
        )

        with self.assertRaisesRegex(ConfigError, "GOOGLE_SHEETS_SCOPE"):
            inspector.run()

        self.assertEqual(self.factory.calls, 0)
        self.assertEqual(self.spreadsheets.get_calls, [])

    def test_sheets_only_accepts_metadata_scope_without_drive_folder_ids(self) -> None:
        report = self._run(a1_range="A1:B2")

        self.assertEqual(
            self.settings.drive_scope, GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        )
        self.assertEqual(self.settings.clm_drive_folder_id, "")
        self.assertEqual(self.settings.md_drive_folder_id, "")
        self.assertEqual(report["status"], "ok")

    def test_inspector_does_not_validate_drive_scope(self) -> None:
        sheets_only = replace(self.settings, drive_scope="not-used-by-sheets")
        report = SheetLayoutInspector(
            sheets_only,
            self.factory,
            sheet_title="RMB Price List",
            a1_range="A1:B2",
            redactor=google_redactor_for_settings(sheets_only),
        ).run()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["drive_requests_performed"], 0)

    def test_inspector_uses_only_sheets_factory(self) -> None:
        report = self._run(a1_range="A1:B2")

        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.factory.full_create_calls, 0)
        self.assertFalse(self.factory.drive_client_created)
        self.assertEqual(report["read_requests_performed"], 1)
        self.assertEqual(report["drive_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_cli_uses_sheets_only_configuration_loader(self) -> None:
        safe_report = {
            "status": "ok",
            "read_requests_performed": 1,
            "drive_requests_performed": 0,
            "write_requests_performed": 0,
        }
        with (
            patch(
                "sync_worker.cli.load_google_sheets_readonly_config",
                return_value=self.settings,
            ) as sheets_loader,
            patch(
                "sync_worker.cli.load_google_config",
                side_effect=AssertionError("full config loader must not be used"),
            ) as full_loader,
            patch("sync_worker.cli.SheetLayoutInspector") as inspector_class,
            patch("sync_worker.cli.SafeJsonReportWriter"),
        ):
            inspector_class.return_value.run.return_value = safe_report
            result = main(
                [
                    "inspect-sheet-layout",
                    "--sheet",
                    "RMB Price List",
                    "--range",
                    "A1:B2",
                ]
            )
        self.assertEqual(result, 0)
        sheets_loader.assert_called_once_with()
        full_loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
