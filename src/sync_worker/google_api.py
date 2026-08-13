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
    proxy_url = None
    if settings.google_proxy_host and settings.google_proxy_port is not None:
        proxy_url = (
            f"socks5://{settings.google_proxy_host}:"
            f"{settings.google_proxy_port}"
        )
    return Redactor.from_values(
        (
            str(settings.resolved_service_account_file),
            settings.clm_spreadsheet_id,
            settings.clm_drive_folder_id,
            settings.md_drive_folder_id,
            settings.google_proxy_host,
            (
                str(settings.google_proxy_port)
                if settings.google_proxy_port is not None
                else None
            ),
            proxy_url,
        )
    )


class OfficialGoogleClientFactory:
    """Create authenticated Drive v3 and Sheets v4 clients after validation."""

    def create(self, settings: GoogleSettings) -> GoogleClients:
        settings.validate()
        redactor = google_redactor_for_settings(settings)

        try:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                str(settings.resolved_service_account_file),
                scopes=[settings.drive_scope, settings.sheets_scope],
            )
        except Exception as error:
            raise _stage_error("credentials_create", error, redactor) from None

        try:
            import httplib2
            import socks
            from google_auth_httplib2 import AuthorizedHttp, Request

            if settings.google_proxy_mode == "socks5":
                proxy_info = httplib2.ProxyInfo(
                    proxy_type=socks.PROXY_TYPE_SOCKS5,
                    proxy_host=settings.google_proxy_host,
                    proxy_port=settings.google_proxy_port,
                    proxy_rdns=settings.google_proxy_rdns,
                )
            else:
                proxy_info = None
            raw_http = httplib2.Http(
                proxy_info=proxy_info,
                timeout=30,
            )
        except Exception as error:
            raise _stage_error("transport_create", error, redactor) from None

        try:
            credentials.refresh(Request(raw_http))
        except Exception as error:
            raise _stage_error("token_refresh", error, redactor) from None

        try:
            authorized_http = AuthorizedHttp(
                credentials,
                http=raw_http,
            )
        except Exception as error:
            raise _stage_error("transport_authorize", error, redactor) from None

        try:
            from googleapiclient.discovery import build

            drive = build(
                "drive",
                "v3",
                http=authorized_http,
                cache_discovery=False,
            )
        except Exception as error:
            raise _stage_error("drive_client_build", error, redactor) from None

        try:
            sheets = build(
                "sheets",
                "v4",
                http=authorized_http,
                cache_discovery=False,
            )
        except Exception as error:
            raise _stage_error("sheets_client_build", error, redactor) from None

        return GoogleClients(drive=drive, sheets=sheets)


def _stage_error(
    stage: str, error: BaseException, redactor: Redactor
) -> GoogleClientCreationError:
    error_type = type(error).__name__
    summary = redactor.text(str(error), limit=180).strip()
    message = f"{stage} failed: {error_type}"
    if summary:
        message += f": {summary}"
    return GoogleClientCreationError(message)


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
