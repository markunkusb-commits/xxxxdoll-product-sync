"""Official Google client construction and a strictly read-only API gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .config import GoogleSettings
from .sanitization import Redactor


class GoogleClientCreationError(RuntimeError):
    """Safe client creation failure with no credential details."""


class GoogleOperationBlocked(RuntimeError):
    """Raised before execution for any operation outside the read allowlist."""


@dataclass(frozen=True, slots=True)
class GoogleClients:
    drive: Any
    sheets: Any


class GoogleClientFactory(Protocol):
    def create(self, settings: GoogleSettings) -> GoogleClients: ...


_ALLOWED_GOOGLE_OPERATIONS = frozenset(
    {
        "drive.files.get",
        "drive.files.list",
        "sheets.spreadsheets.get",
        "sheets.values.get",
    }
)


def ensure_google_operation_allowed(operation: str) -> None:
    if operation not in _ALLOWED_GOOGLE_OPERATIONS:
        raise GoogleOperationBlocked("Google operation is not in the read-only allowlist")


def google_redactor_for_settings(settings: GoogleSettings) -> Redactor:
    return Redactor.from_values(
        (
            str(settings.resolved_service_account_file),
            settings.clm_spreadsheet_id,
            settings.clm_drive_folder_id,
            settings.md_drive_folder_id,
        )
    )


class OfficialGoogleClientFactory:
    """Create authenticated Drive v3 and Sheets v4 clients after validation."""

    def create(self, settings: GoogleSettings) -> GoogleClients:
        settings.validate()
        try:
            import httplib2
            import google_auth_httplib2
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            raise GoogleClientCreationError(
                "Official Google client dependencies are unavailable"
            ) from None

        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(settings.resolved_service_account_file),
                scopes=[settings.drive_scope, settings.sheets_scope],
            )
            refresh_http = httplib2.Http(timeout=10)
            credentials.refresh(google_auth_httplib2.Request(refresh_http))
            authorized_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=20)
            )
            drive = build(
                "drive",
                "v3",
                http=authorized_http,
                cache_discovery=False,
                static_discovery=True,
            )
            sheets = build(
                "sheets",
                "v4",
                http=authorized_http,
                cache_discovery=False,
                static_discovery=True,
            )
            return GoogleClients(drive=drive, sheets=sheets)
        except Exception:
            raise GoogleClientCreationError(
                "Google service account authentication or client creation failed"
            ) from None


@dataclass(slots=True)
class GoogleRequestCounters:
    read_requests_performed: int = 0

    @property
    def write_requests_performed(self) -> int:
        return 0


class ReadOnlyGoogleGateway:
    """Expose only the four read operations required by google-doctor."""

    def __init__(self, clients: GoogleClients) -> None:
        self._drive = clients.drive
        self._sheets = clients.sheets
        self.counters = GoogleRequestCounters()

    def _execute(self, operation: str, request: Any) -> object:
        ensure_google_operation_allowed(operation)
        self.counters.read_requests_performed += 1
        return request.execute()

    def get_folder(self, folder_id: str) -> object:
        request = self._drive.files().get(
            fileId=folder_id,
            fields="name,mimeType,trashed",
            supportsAllDrives=True,
        )
        return self._execute("drive.files.get", request)

    def list_folder_children(self, folder_id: str) -> object:
        request = self._drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=100,
            fields="files(name,mimeType,modifiedTime)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        return self._execute("drive.files.list", request)

    def get_spreadsheet(self, spreadsheet_id: str) -> object:
        request = self._sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            includeGridData=False,
            fields=(
                "properties(title),"
                "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
            ),
        )
        return self._execute("sheets.spreadsheets.get", request)

    def get_sheet_sample(
        self, spreadsheet_id: str, sheet_title: str
    ) -> object:
        escaped_title = sheet_title.replace("'", "''")
        request = self._sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{escaped_title}'!A1:Z5",
            majorDimension="ROWS",
        )
        return self._execute("sheets.values.get", request)
