from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.config import (  # noqa: E402
    ConfigError,
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_DRIVE_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    load_google_config,
    load_google_sheets_readonly_config,
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
            "GOOGLE_PROXY_MODE": "none",
            "GOOGLE_PROXY_HOST": "",
            "GOOGLE_PROXY_PORT": "",
            "GOOGLE_PROXY_RDNS": "true",
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
            if value:
                self.assertNotIn(value, representation)

    def test_socks5_proxy_configuration_is_parsed_strictly(self) -> None:
        settings = load_google_config(
            {
                **self.valid,
                "GOOGLE_PROXY_MODE": "socks5",
                "GOOGLE_PROXY_HOST": "127.0.0.1",
                "GOOGLE_PROXY_PORT": "26001",
                "GOOGLE_PROXY_RDNS": "false",
            }
        )

        self.assertEqual(settings.google_proxy_mode, "socks5")
        self.assertEqual(settings.google_proxy_host, "127.0.0.1")
        self.assertEqual(settings.google_proxy_port, 26001)
        self.assertFalse(settings.google_proxy_rdns)

    def test_invalid_proxy_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "GOOGLE_PROXY_MODE"):
            load_google_config(
                {**self.valid, "GOOGLE_PROXY_MODE": "automatic"}
            )

    def test_socks5_requires_valid_host(self) -> None:
        for host in ("", "https://127.0.0.1", "bad host", "user@host"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ConfigError, "GOOGLE_PROXY_HOST"):
                    load_google_config(
                        {
                            **self.valid,
                            "GOOGLE_PROXY_MODE": "socks5",
                            "GOOGLE_PROXY_HOST": host,
                            "GOOGLE_PROXY_PORT": "26001",
                        }
                    )

    def test_invalid_proxy_port_is_rejected(self) -> None:
        for port in ("", "0", "65536", "not-a-port", "1.5"):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ConfigError, "GOOGLE_PROXY_PORT"):
                    load_google_config(
                        {
                            **self.valid,
                            "GOOGLE_PROXY_MODE": "socks5",
                            "GOOGLE_PROXY_HOST": "127.0.0.1",
                            "GOOGLE_PROXY_PORT": port,
                        }
                    )

    def test_proxy_rdns_accepts_only_true_or_false(self) -> None:
        for value in ("1", "yes", "on", "", "sometimes"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "GOOGLE_PROXY_RDNS"):
                    load_google_config(
                        {**self.valid, "GOOGLE_PROXY_RDNS": value}
                    )

    def test_sheets_only_accepts_drive_metadata_scope(self) -> None:
        settings = load_google_sheets_readonly_config(
            {
                **self.valid,
                "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
            }
        )
        self.assertEqual(
            settings.drive_scope, GOOGLE_DRIVE_METADATA_READONLY_SCOPE
        )
        self.assertEqual(settings.sheets_scope, GOOGLE_SHEETS_READONLY_SCOPE)

    def test_sheets_only_does_not_require_drive_scope_or_folder_ids(self) -> None:
        settings = load_google_sheets_readonly_config(
            {
                **self.valid,
                "GOOGLE_DRIVE_SCOPE": "",
                "CLM_DRIVE_FOLDER_ID": "",
                "MD_DRIVE_FOLDER_ID": "",
            }
        )
        self.assertEqual(settings.drive_scope, "")
        self.assertEqual(settings.clm_drive_folder_id, "")
        self.assertEqual(settings.md_drive_folder_id, "")

    def test_sheets_only_rejects_invalid_sheets_scope(self) -> None:
        with self.assertRaisesRegex(ConfigError, "GOOGLE_SHEETS_SCOPE"):
            load_google_sheets_readonly_config(
                {
                    **self.valid,
                    "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
                    "GOOGLE_SHEETS_SCOPE": (
                        "https://www.googleapis.com/auth/spreadsheets"
                    ),
                }
            )

    def test_full_validation_still_rejects_metadata_drive_scope(self) -> None:
        with self.assertRaisesRegex(ConfigError, "GOOGLE_DRIVE_SCOPE"):
            load_google_config(
                {
                    **self.valid,
                    "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
                }
            )


if __name__ == "__main__":
    unittest.main()
