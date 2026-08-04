from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.config import (  # noqa: E402
    ConfigError,
    GOOGLE_DRIVE_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    load_google_config,
)


class GoogleConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.credentials_path = Path(self.temporary_directory.name) / "fake.json"
        self.credentials_path.write_text("{}", encoding="utf-8")
        self.valid = {
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(self.credentials_path),
            "CLM_SPREADSHEET_ID": "spreadsheet_ID_1234567890",
            "CLM_DRIVE_FOLDER_ID": "clm_folder_ID_1234567890",
            "MD_DRIVE_FOLDER_ID": "md_folder_ID_1234567890",
            "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_READONLY_SCOPE,
            "GOOGLE_SHEETS_SCOPE": GOOGLE_SHEETS_READONLY_SCOPE,
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_missing_json_file_is_rejected(self) -> None:
        values = {
            **self.valid,
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(
                Path(self.temporary_directory.name) / "missing.json"
            ),
        }

        with self.assertRaisesRegex(ConfigError, "does not exist"):
            load_google_config(values)

    def test_credentials_inside_project_are_rejected_before_file_read(self) -> None:
        values = {
            **self.valid,
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(PROJECT_ROOT / "credentials.json"),
        }

        with self.assertRaisesRegex(ConfigError, "outside the project"):
            load_google_config(values)

    def test_non_json_extension_is_rejected(self) -> None:
        path = Path(self.temporary_directory.name) / "fake.txt"
        path.write_text("not-read", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, r"\.json"):
            load_google_config(
                {**self.valid, "GOOGLE_SERVICE_ACCOUNT_FILE": str(path)}
            )

    def test_non_readonly_drive_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "GOOGLE_DRIVE_SCOPE"):
            load_google_config(
                {
                    **self.valid,
                    "GOOGLE_DRIVE_SCOPE": "https://www.googleapis.com/auth/drive",
                }
            )

    def test_non_readonly_sheets_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "GOOGLE_SHEETS_SCOPE"):
            load_google_config(
                {
                    **self.valid,
                    "GOOGLE_SHEETS_SCOPE": "https://www.googleapis.com/auth/spreadsheets",
                }
            )

    def test_missing_folder_ids_are_rejected(self) -> None:
        for variable in ("CLM_DRIVE_FOLDER_ID", "MD_DRIVE_FOLDER_ID"):
            with self.subTest(variable=variable):
                with self.assertRaisesRegex(ConfigError, variable):
                    load_google_config({**self.valid, variable: ""})

    def test_missing_spreadsheet_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "CLM_SPREADSHEET_ID"):
            load_google_config({**self.valid, "CLM_SPREADSHEET_ID": ""})

    def test_google_settings_repr_hides_paths_ids_and_scopes(self) -> None:
        settings = load_google_config(self.valid)
        representation = repr(settings)

        for value in self.valid.values():
            self.assertNotIn(value, representation)


if __name__ == "__main__":
    unittest.main()
