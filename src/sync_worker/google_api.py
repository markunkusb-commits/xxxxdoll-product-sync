"""Official Google client construction and a strictly read-only API gateway."""

from __future__ import annotations

import re
from collections.abc import Sequence
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


class GoogleDriveMetadataClientFactory(Protocol):
    def create_drive_metadata(self, settings: GoogleSettings) -> Any: ...


class GoogleDriveMetadataAndSheetsClientFactory(Protocol):
    def create_drive_metadata_clients(
        self, settings: GoogleSettings
    ) -> GoogleClients: ...


class GoogleSheetsReadonlyClientFactory(Protocol):
    def create_sheets_readonly(self, settings: GoogleSettings) -> Any: ...


_ALLOWED_GOOGLE_OPERATIONS = frozenset(
    {
        "drive.files.get",
        "drive.files.list",
        "sheets.spreadsheets.get",
        "sheets.values.get",
        "sheets.values.batchGet",
    }
)
_ALLOWED_GOOGLE_HTTP_METHODS = frozenset({"GET", "HEAD"})
_SINGLE_CELL_A1_PATTERN = re.compile(r"^[A-Z]+[1-9][0-9]*$")
_DRIVE_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SHEETS_LINK_CELL_FIELDS = (
    "sheets(data(startRow,startColumn,rowData(values("
    "formattedValue,hyperlink,textFormatRuns(format(link(uri))),"
    "userEnteredFormat(textFormat(link(uri))),"
    "chipRuns(startIndex,chip(richLinkProperties(uri,mimeType))),"
    "userEnteredValue(formulaValue)))))"
)


def ensure_google_operation_allowed(operation: str) -> None:
    if operation not in _ALLOWED_GOOGLE_OPERATIONS:
        raise GoogleOperationBlocked(
            "Google operation is not in the read-only allowlist"
        )


def ensure_google_http_method_allowed(method: object) -> None:
    if str(method).upper() not in _ALLOWED_GOOGLE_HTTP_METHODS:
        raise GoogleOperationBlocked("Google transport allows only GET and HEAD")


def protect_google_transport(authorized_http: Any) -> Any:
    """Install a fail-closed HTTP verb guard while retaining the same transport."""
    original_request = authorized_http.request

    def read_only_request(
        uri: str, method: str = "GET", *args: object, **kwargs: object
    ) -> object:
        ensure_google_http_method_allowed(method)
        return original_request(uri, method, *args, **kwargs)

    authorized_http.request = read_only_request
    return authorized_http


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

    def _create_authorized_http(
        self,
        settings: GoogleSettings,
        scopes: Sequence[str],
        redactor: Redactor,
    ) -> Any:
        try:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                str(settings.resolved_service_account_file),
                scopes=list(scopes),
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
            protect_google_transport(authorized_http)
        except Exception as error:
            raise _stage_error("transport_authorize", error, redactor) from None
        return authorized_http

    def create(self, settings: GoogleSettings) -> GoogleClients:
        settings.validate()
        redactor = google_redactor_for_settings(settings)
        authorized_http = self._create_authorized_http(
            settings,
            (settings.drive_scope, settings.sheets_scope),
            redactor,
        )

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

    def create_drive_metadata(self, settings: GoogleSettings) -> Any:
        """Build only Drive v3 with the exact metadata-only OAuth scope."""

        settings.validate_drive_metadata()
        redactor = google_redactor_for_settings(settings)
        authorized_http = self._create_authorized_http(
            settings,
            (settings.drive_scope,),
            redactor,
        )
        try:
            from googleapiclient.discovery import build

            return build(
                "drive",
                "v3",
                http=authorized_http,
                cache_discovery=False,
            )
        except Exception as error:
            raise _stage_error("drive_client_build", error, redactor) from None

    def create_sheets_readonly(self, settings: GoogleSettings) -> Any:
        """Build only Sheets v4 with the exact read-only Sheets scope."""

        settings.validate_sheets_readonly()
        redactor = google_redactor_for_settings(settings)
        authorized_http = self._create_authorized_http(
            settings,
            (settings.sheets_scope,),
            redactor,
        )
        try:
            from googleapiclient.discovery import build

            return build(
                "sheets",
                "v4",
                http=authorized_http,
                cache_discovery=False,
            )
        except Exception as error:
            raise _stage_error("sheets_client_build", error, redactor) from None

    def create_drive_metadata_clients(
        self, settings: GoogleSettings
    ) -> GoogleClients:
        """Build Drive metadata and Sheets clients on one guarded transport."""

        settings.validate_drive_metadata_with_sheets()
        redactor = google_redactor_for_settings(settings)
        authorized_http = self._create_authorized_http(
            settings,
            (settings.drive_scope, settings.sheets_scope),
            redactor,
        )
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


class ReadOnlySheetsGateway:
    """Expose only Sheets grid reads for layout and secure media workflows."""

    def __init__(self, sheets_client: Any) -> None:
        self._sheets = sheets_client
        self.counters = GoogleRequestCounters()

    def _execute(self, operation: str, request: Any) -> object:
        if operation != "sheets.spreadsheets.get":
            raise GoogleOperationBlocked("Sheets gateway allows only grid reads")
        ensure_google_operation_allowed(operation)
        self.counters.read_requests_performed += 1
        return request.execute()

    def batch_get_sheet_link_cells(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        coordinates: Sequence[str],
    ) -> object:
        """Read hyperlink metadata for approved exact cells in one GET."""

        if isinstance(coordinates, (str, bytes)) or not isinstance(
            coordinates, Sequence
        ):
            raise ValueError("coordinates must be a sequence")
        stable_coordinates = tuple(coordinates)
        if not 1 <= len(stable_coordinates) <= 100:
            raise ValueError("coordinates must contain from 1 to 100 cells")
        if len(set(stable_coordinates)) != len(stable_coordinates):
            raise ValueError("coordinates must be unique")
        if any(
            not isinstance(item, str)
            or _SINGLE_CELL_A1_PATTERN.fullmatch(item) is None
            for item in stable_coordinates
        ):
            raise ValueError("coordinates must be exact single-cell A1 values")
        if not isinstance(sheet_title, str) or not sheet_title:
            raise ValueError("sheet_title must be non-empty text")
        escaped_title = sheet_title.replace("'", "''")
        ranges = [
            f"'{escaped_title}'!{coordinate}"
            for coordinate in stable_coordinates
        ]
        request = self._sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            includeGridData=True,
            fields=SHEETS_LINK_CELL_FIELDS,
        )
        return self._execute("sheets.spreadsheets.get", request)

    def inspect_sheet_layout(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        a1_range: str,
    ) -> object:
        """Read one bounded grid region with display values and merge metadata."""
        escaped_title = sheet_title.replace("'", "''")
        request = self._sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            includeGridData=True,
            ranges=[f"'{escaped_title}'!{a1_range}"],
            fields=(
                "properties(title),"
                "sheets(properties(title),"
                "merges(startRowIndex,endRowIndex,startColumnIndex,"
                "endColumnIndex),"
                "data(startRow,startColumn,rowData(values(formattedValue))))"
            ),
        )
        return self._execute("sheets.spreadsheets.get", request)


class ReadOnlyGoogleGateway(ReadOnlySheetsGateway):
    """Retain the read operations used by legacy full Google workflows."""

    def __init__(self, clients: GoogleClients) -> None:
        super().__init__(clients.sheets)
        self._drive = clients.drive

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

    def list_inventory_children(
        self, folder_id: str, *, item_limit: int = 500
    ) -> object:
        """Read one bounded child page for recursive inventory traversal."""
        if not 1 <= item_limit <= 500:
            raise ValueError("Drive inventory item_limit must be from 1 to 500")
        request = self._drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=item_limit,
            fields="nextPageToken,files(id,name,mimeType)",
            orderBy="name",
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

    def batch_get_sheet_cells(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        coordinates: Sequence[str],
    ) -> object:
        """Read at most 100 already-validated single cells in one GET batch."""

        if isinstance(coordinates, (str, bytes)) or not isinstance(
            coordinates, Sequence
        ):
            raise ValueError("coordinates must be a sequence")
        stable_coordinates = tuple(coordinates)
        if not 1 <= len(stable_coordinates) <= 100:
            raise ValueError("coordinates must contain from 1 to 100 cells")
        if len(set(stable_coordinates)) != len(stable_coordinates):
            raise ValueError("coordinates must be unique")
        if any(
            not isinstance(item, str)
            or _SINGLE_CELL_A1_PATTERN.fullmatch(item) is None
            for item in stable_coordinates
        ):
            raise ValueError("coordinates must be exact single-cell A1 values")
        if not isinstance(sheet_title, str) or not sheet_title:
            raise ValueError("sheet_title must be non-empty text")
        escaped_title = sheet_title.replace("'", "''")
        ranges = [
            f"'{escaped_title}'!{coordinate}"
            for coordinate in stable_coordinates
        ]
        request = self._sheets.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            majorDimension="ROWS",
            valueRenderOption="FORMATTED_VALUE",
        )
        return self._execute("sheets.values.batchGet", request)

    def get_inventory_spreadsheet(self, spreadsheet_id: str) -> object:
        """Read only inventory-safe sheet metadata without requesting sheet IDs."""
        request = self._sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            includeGridData=False,
            fields=(
                "properties(title),"
                "sheets(properties(title,index,hidden,"
                "gridProperties(rowCount,columnCount,frozenRowCount,"
                "frozenColumnCount)))"
            ),
        )
        return self._execute("sheets.spreadsheets.get", request)

    def get_inventory_sheet_sample(
        self, spreadsheet_id: str, sheet_title: str
    ) -> object:
        """Read the fixed, bounded structure sample A1:AZ10."""
        escaped_title = sheet_title.replace("'", "''")
        request = self._sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{escaped_title}'!A1:AZ10",
            majorDimension="ROWS",
            valueRenderOption="FORMULA",
        )
        return self._execute("sheets.values.get", request)


DRIVE_FOLDER_MANIFEST_FIELDS = (
    "nextPageToken,"
    "files(id,name,mimeType,size,modifiedTime,md5Checksum,"
    "imageMediaMetadata(width,height,rotation))"
)


class GoogleDriveMetadataGateway:
    """Expose only one metadata-only Drive children-list operation."""

    def __init__(self, drive_client: Any) -> None:
        self._drive = drive_client
        self.counters = GoogleRequestCounters()

    def list_folder_children(
        self,
        folder_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> object:
        if (
            not isinstance(folder_id, str)
            or _DRIVE_RESOURCE_ID_PATTERN.fullmatch(folder_id) is None
        ):
            raise GoogleOperationBlocked("Drive folder identifier is invalid")
        if not 1 <= page_size <= 100:
            raise ValueError("Drive manifest page_size must be from 1 to 100")
        request_arguments: dict[str, object] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": page_size,
            "fields": DRIVE_FOLDER_MANIFEST_FIELDS,
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
        }
        if page_token is not None:
            request_arguments["pageToken"] = page_token
        request = self._drive.files().list(**request_arguments)
        ensure_google_operation_allowed("drive.files.list")
        self.counters.read_requests_performed += 1
        return request.execute()
