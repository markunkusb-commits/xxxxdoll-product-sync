from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.config import (  # noqa: E402
    GOOGLE_DRIVE_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
    load_google_config,
)
from sync_worker.google_api import (  # noqa: E402
    GoogleClientCreationError,
    OfficialGoogleClientFactory,
)
from sync_worker.google_doctor import GoogleDoctorRunner  # noqa: E402


class CredentialsCreateFailure(RuntimeError):
    pass


class TokenRefreshFailure(RuntimeError):
    pass


class DriveBuildFailure(RuntimeError):
    pass


class SheetsBuildFailure(RuntimeError):
    pass


def _package(name: str) -> ModuleType:
    package = ModuleType(name)
    package.__path__ = []  # type: ignore[attr-defined]
    return package


class OfficialGoogleClientFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.credentials_path = Path(self.temporary_directory.name) / "fake.json"
        self.credentials_path.write_text("{}", encoding="utf-8")
        self.values = {
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(self.credentials_path),
            "CLM_SPREADSHEET_ID": "spreadsheet_ID_1234567890",
            "CLM_DRIVE_FOLDER_ID": "clm_folder_ID_1234567890",
            "MD_DRIVE_FOLDER_ID": "md_folder_ID_1234567890",
            "GOOGLE_DRIVE_SCOPE": GOOGLE_DRIVE_READONLY_SCOPE,
            "GOOGLE_SHEETS_SCOPE": GOOGLE_SHEETS_READONLY_SCOPE,
        }
        self.settings = load_google_config(self.values)
        self.credentials = MagicMock(name="credentials")
        self.request_instance = object()
        self.request_class = MagicMock(return_value=self.request_instance)
        self.from_service_account_file = MagicMock(return_value=self.credentials)
        self.drive_client = MagicMock(name="drive_client")
        self.sheets_client = MagicMock(name="sheets_client")
        self.build = MagicMock(
            side_effect=[self.drive_client, self.sheets_client]
        )
        self.modules = self._fake_google_modules()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _fake_google_modules(self) -> dict[str, ModuleType]:
        google = _package("google")
        google_auth = _package("google.auth")
        google_transport = _package("google.auth.transport")
        google_requests = ModuleType("google.auth.transport.requests")
        google_requests.Request = self.request_class  # type: ignore[attr-defined]
        google_oauth2 = _package("google.oauth2")
        service_account = ModuleType("google.oauth2.service_account")
        service_account.Credentials = SimpleNamespace(  # type: ignore[attr-defined]
            from_service_account_file=self.from_service_account_file
        )
        google_oauth2.service_account = service_account  # type: ignore[attr-defined]

        googleapiclient = _package("googleapiclient")
        discovery = ModuleType("googleapiclient.discovery")
        discovery.build = self.build  # type: ignore[attr-defined]
        googleapiclient.discovery = discovery  # type: ignore[attr-defined]
        return {
            "google": google,
            "google.auth": google_auth,
            "google.auth.transport": google_transport,
            "google.auth.transport.requests": google_requests,
            "google.oauth2": google_oauth2,
            "google.oauth2.service_account": service_account,
            "googleapiclient": googleapiclient,
            "googleapiclient.discovery": discovery,
        }

    def _create(self):
        with patch.dict(sys.modules, self.modules):
            return OfficialGoogleClientFactory().create(self.settings)

    def _sensitive_error_text(self) -> str:
        return (
            f"private_key=unsafe-key private_key_id=unsafe-key-id "
            f"client_email=service-account@example.invalid token=unsafe-token "
            f"path={self.credentials_path} "
            f"folder={self.values['CLM_DRIVE_FOLDER_ID']} "
            f"spreadsheet={self.values['CLM_SPREADSHEET_ID']}"
        )

    def _assert_safe_stage_error(
        self, stage: str, exception_type: type[Exception]
    ) -> str:
        with self.assertRaises(GoogleClientCreationError) as caught:
            self._create()
        message = str(caught.exception)
        lowered = message.lower()
        self.assertIn(stage, message)
        self.assertIn(exception_type.__name__, message)
        for forbidden in (
            "private_key",
            "client_email",
            "unsafe-key",
            "unsafe-key-id",
            "service-account@example.invalid",
            "unsafe-token",
            str(self.credentials_path),
            self.values["CLM_DRIVE_FOLDER_ID"],
            self.values["CLM_SPREADSHEET_ID"],
        ):
            self.assertNotIn(forbidden.lower(), lowered)
        self.assertIsNone(caught.exception.__cause__)
        return message

    def test_credentials_refresh_and_build_follow_verified_call_path(self) -> None:
        clients = self._create()

        self.from_service_account_file.assert_called_once_with(
            str(self.settings.resolved_service_account_file),
            scopes=[self.settings.drive_scope, self.settings.sheets_scope],
        )
        self.request_class.assert_called_once_with()
        self.credentials.refresh.assert_called_once_with(self.request_instance)
        self.assertEqual(
            self.build.call_args_list,
            [
                call(
                    "drive",
                    "v3",
                    credentials=self.credentials,
                    cache_discovery=False,
                ),
                call(
                    "sheets",
                    "v4",
                    credentials=self.credentials,
                    cache_discovery=False,
                ),
            ],
        )
        for build_call in self.build.call_args_list:
            self.assertNotIn("http", build_call.kwargs)
        self.assertIs(clients.drive, self.drive_client)
        self.assertIs(clients.sheets, self.sheets_client)

    def test_credentials_create_failure_reports_safe_stage(self) -> None:
        self.from_service_account_file.side_effect = CredentialsCreateFailure(
            self._sensitive_error_text()
        )

        self._assert_safe_stage_error(
            "credentials_create", CredentialsCreateFailure
        )
        self.credentials.refresh.assert_not_called()
        self.build.assert_not_called()

    def test_token_refresh_failure_reports_safe_stage(self) -> None:
        self.credentials.refresh.side_effect = TokenRefreshFailure(
            self._sensitive_error_text()
        )

        self._assert_safe_stage_error("token_refresh", TokenRefreshFailure)
        self.build.assert_not_called()

    def test_drive_client_build_failure_reports_safe_stage(self) -> None:
        self.build.side_effect = DriveBuildFailure(self._sensitive_error_text())

        self._assert_safe_stage_error("drive_client_build", DriveBuildFailure)
        self.assertEqual(self.build.call_count, 1)

    def test_sheets_client_build_failure_reports_safe_stage(self) -> None:
        self.build.side_effect = [
            self.drive_client,
            SheetsBuildFailure(self._sensitive_error_text()),
        ]

        self._assert_safe_stage_error("sheets_client_build", SheetsBuildFailure)
        self.assertEqual(self.build.call_count, 2)
        self.drive_client.files.assert_not_called()

    def test_factory_failure_report_has_zero_data_and_write_requests(self) -> None:
        self.credentials.refresh.side_effect = TokenRefreshFailure(
            self._sensitive_error_text()
        )

        with patch.dict(sys.modules, self.modules):
            report = GoogleDoctorRunner(
                self.settings, OfficialGoogleClientFactory()
            ).run()
        serialized = str(report).lower()

        self.assertFalse(report["service_account_authentication"])
        self.assertEqual(report["read_requests_performed"], 0)
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertIn("token_refresh", serialized)
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("client_email", serialized)
        self.assertNotIn(str(self.credentials_path).lower(), serialized)
        self.drive_client.files.assert_not_called()
        self.sheets_client.spreadsheets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
