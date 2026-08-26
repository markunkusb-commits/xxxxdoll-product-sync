"""Metadata-only, non-recursive Google Drive folder manifest core."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from .config import GOOGLE_DRIVE_METADATA_READONLY_SCOPE, GoogleSettings
from .google_api import (
    GoogleDriveMetadataClientFactory,
    GoogleDriveMetadataGateway,
)
from .image_mapping import ProductSourceRange
from .media_source_discovery import MediaSourceDiscoveryResult
from .sanitization import REPORT_SECRET_SCAN_PATTERN, Redactor


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
GOOGLE_WORKSPACE_MIME_PREFIX = "application/vnd.google-apps."
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_ITEMS_PER_FOLDER = 1000
MAX_RETRY_ATTEMPTS = 3

ManifestStatus = Literal[
    "listed",
    "empty_folder",
    "access_denied",
    "missing_or_inaccessible",
    "limit_exceeded",
    "invalid_folder_handle",
    "read_failed",
]
ManifestItemKind = Literal[
    "image_candidate",
    "nested_folder",
    "shortcut",
    "google_workspace_file",
    "other_file",
]

_DRIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_SKU_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_MIME_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9.+_/-]{1,200}$")
_MODIFIED_TIME_PATTERN = re.compile(r"^[0-9TZ:.+\-]{1,80}$")
_MD5_PATTERN = re.compile(r"^[a-fA-F0-9]{32}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_PATH_CHARACTER_PATTERN = re.compile(r"[\\/:*?\"<>|]")
_URL_PATTERN = re.compile(r"(?i)https?://\S+")
_UNSAFE_REPORT_PATTERN = re.compile(
    r"(?i)raw_folder_id|raw_file_id|provider_resource_id|resource_key|"
    r"shortcut_target_id|webContentLink|webViewLink|thumbnailLink|"
    r"alt\s*=\s*media|https?://|"
    r"(?:private_key|client_email|access_token|refresh_token|token|password)"
    r"\s*[:=]"
)


class GoogleDriveFolderManifestError(ValueError):
    """Safe manifest validation error without provider identifiers."""


class DriveMetadataScopeUnavailable(GoogleDriveFolderManifestError):
    """Raised before credentials/client creation for a non-metadata scope."""


class DriveMetadataListGateway(Protocol):
    def list_folder_children(
        self,
        folder_id: str,
        *,
        page_token: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class SecureGoogleDriveFolderHandle:
    provider: Literal["google_drive"]
    resource_kind: Literal["folder"]
    raw_folder_id: str = field(repr=False)
    folder_id_fingerprint: str
    sku: str
    product_source: ProductSourceRange
    resource_key: str | None = field(default=None, repr=False)

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "resource_kind": self.resource_kind,
            "folder_id_fingerprint": self.folder_id_fingerprint,
            "sku": self.sku,
            "product_source": self.product_source.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DriveManifestItem:
    safe_name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None
    md5_checksum: str | None
    file_id_fingerprint: str | None
    item_kind: ManifestItemKind
    image_candidate: bool
    image_candidate_status: str | None
    image_width: int | None
    image_height: int | None
    image_rotation: int | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "safe_name": self.safe_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "modified_time": self.modified_time,
            "md5_checksum": self.md5_checksum,
            "file_id_fingerprint": self.file_id_fingerprint,
            "item_kind": self.item_kind,
            "image_candidate": self.image_candidate,
            "image_candidate_status": self.image_candidate_status,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "image_rotation": self.image_rotation,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class GoogleDriveFolderManifest:
    sku: str
    product_source: ProductSourceRange
    folder_id_fingerprint: str
    status: ManifestStatus
    items: tuple[DriveManifestItem, ...]
    pages_read: int
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "product_source": self.product_source.to_dict(),
            "folder_id_fingerprint": self.folder_id_fingerprint,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "pages_read": self.pages_read,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


@dataclass(frozen=True, slots=True)
class GoogleDriveFolderManifestSummary:
    total_folders: int
    folders_listed: int
    empty_folders: int
    folders_access_denied: int
    folders_missing_or_inaccessible: int
    folders_limit_exceeded: int
    folders_read_failed: int
    total_items: int
    image_candidates: int
    nested_folders: int
    shortcuts: int
    google_workspace_files: int
    other_files: int
    duplicate_name_candidates: int
    duplicate_content_candidates: int
    pages_read: int
    drive_read_requests_performed: int
    download_requests_performed: Literal[0] = 0
    write_requests_performed: Literal[0] = 0

    def to_dict(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class GoogleDriveFolderManifestBatchResult:
    manifests: tuple[GoogleDriveFolderManifest, ...]
    summary: GoogleDriveFolderManifestSummary
    download_requests_performed: Literal[0] = 0
    write_requests_performed: Literal[0] = 0

    def to_report_dict(self) -> dict[str, object]:
        report: dict[str, object] = {
            "status": "ok",
            "summary": self.summary.to_dict(),
            "download_requests_performed": 0,
            "write_requests_performed": 0,
            "manifests": [item.to_dict() for item in self.manifests],
        }
        _assert_report_safe(report)
        return report


@dataclass(frozen=True, slots=True)
class _SafeDriveReadFailure(Exception):
    code: str

    def __str__(self) -> str:
        return self.code


def fingerprint_drive_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_valid_drive_id(value: object) -> bool:
    return isinstance(value, str) and _DRIVE_ID_PATTERN.fullmatch(value) is not None


def create_secure_google_drive_folder_handle(
    discovery: MediaSourceDiscoveryResult,
    product_source: ProductSourceRange,
) -> SecureGoogleDriveFolderHandle:
    """Promote one in-memory Drive folder classification to a safe handle."""

    if not isinstance(discovery, MediaSourceDiscoveryResult):
        raise TypeError("discovery must be a MediaSourceDiscoveryResult")
    if not isinstance(product_source, ProductSourceRange):
        raise TypeError("product_source must be a ProductSourceRange")
    if discovery.provider != "google_drive" or discovery.resource_kind != "folder":
        raise GoogleDriveFolderManifestError("invalid_google_drive_folder_source")
    if not is_valid_drive_id(discovery.provider_resource_id):
        raise GoogleDriveFolderManifestError("invalid_google_drive_folder_id")
    if not isinstance(discovery.sku, str) or not _SAFE_SKU_PATTERN.fullmatch(
        discovery.sku
    ):
        raise GoogleDriveFolderManifestError("verified_sku_required")
    raw_folder_id = discovery.provider_resource_id
    return SecureGoogleDriveFolderHandle(
        provider="google_drive",
        resource_kind="folder",
        raw_folder_id=raw_folder_id,
        folder_id_fingerprint=fingerprint_drive_id(raw_folder_id),
        sku=discovery.sku,
        product_source=product_source,
        resource_key=discovery.resource_key,
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _safe_name(value: object) -> tuple[str, tuple[str, ...]]:
    raw = value if isinstance(value, str) else ""
    cleaned = _CONTROL_PATTERN.sub(" ", raw)
    cleaned = _PATH_CHARACTER_PATTERN.sub("_", cleaned)
    cleaned = _URL_PATTERN.sub("[URL]", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ". .")
    cleaned = Redactor().text(cleaned, limit=255).strip()
    if not cleaned:
        return "[unnamed]", ("unsafe_or_missing_name_sanitized",)
    return cleaned, (() if cleaned == raw else ("file_name_sanitized",))


def _safe_mime_type(value: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, str) and _MIME_TYPE_PATTERN.fullmatch(value):
        return value, ()
    return "application/octet-stream", ("invalid_mime_type_preserved_as_other",)


def _optional_nonnegative_int(
    value: object,
    warning: str,
) -> tuple[int | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
    elif type(value) is int:
        parsed = value
    else:
        return None, (warning,)
    return (parsed, ()) if parsed >= 0 else (None, (warning,))


def _optional_positive_int(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _optional_rotation(value: object) -> int | None:
    return value if type(value) is int else None


def _safe_modified_time(value: object) -> tuple[str | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    if isinstance(value, str) and _MODIFIED_TIME_PATTERN.fullmatch(value):
        return value, ()
    return None, ("invalid_modified_time",)


def _safe_md5(value: object) -> tuple[str | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    if isinstance(value, str) and _MD5_PATTERN.fullmatch(value):
        return value.lower(), ()
    return None, ("invalid_md5_checksum",)


def _item_kind(mime_type: str) -> ManifestItemKind:
    if mime_type == FOLDER_MIME_TYPE:
        return "nested_folder"
    if mime_type == SHORTCUT_MIME_TYPE:
        return "shortcut"
    if mime_type.startswith(GOOGLE_WORKSPACE_MIME_PREFIX):
        return "google_workspace_file"
    if mime_type.startswith("image/"):
        return "image_candidate"
    return "other_file"


def _manifest_item(payload: object) -> DriveManifestItem:
    item = payload if isinstance(payload, Mapping) else {}
    warnings: list[str] = []
    name, name_warnings = _safe_name(item.get("name"))
    warnings.extend(name_warnings)
    mime_type, mime_warnings = _safe_mime_type(item.get("mimeType"))
    warnings.extend(mime_warnings)
    size, size_warnings = _optional_nonnegative_int(
        item.get("size"), "invalid_file_size"
    )
    warnings.extend(size_warnings)
    modified_time, time_warnings = _safe_modified_time(item.get("modifiedTime"))
    warnings.extend(time_warnings)
    md5, md5_warnings = _safe_md5(item.get("md5Checksum"))
    warnings.extend(md5_warnings)
    raw_file_id = item.get("id")
    if isinstance(raw_file_id, str) and raw_file_id:
        file_fingerprint = fingerprint_drive_id(raw_file_id)
    else:
        file_fingerprint = None
        warnings.append("missing_file_id")
    kind = _item_kind(mime_type)
    image_metadata = item.get("imageMediaMetadata")
    metadata = image_metadata if isinstance(image_metadata, Mapping) else {}
    if kind == "nested_folder":
        warnings.append("nested_folder_not_traversed")
    elif kind == "shortcut":
        warnings.append("shortcut_not_followed")
    return DriveManifestItem(
        safe_name=name,
        mime_type=mime_type,
        size_bytes=size,
        modified_time=modified_time,
        md5_checksum=md5,
        file_id_fingerprint=file_fingerprint,
        item_kind=kind,
        image_candidate=kind == "image_candidate",
        image_candidate_status=(
            "drive_metadata_image_candidate"
            if kind == "image_candidate"
            else None
        ),
        image_width=_optional_positive_int(metadata.get("width")),
        image_height=_optional_positive_int(metadata.get("height")),
        image_rotation=_optional_rotation(metadata.get("rotation")),
        warnings=_unique(warnings),
    )


def _mark_duplicate_candidates(
    items: Sequence[DriveManifestItem],
) -> tuple[DriveManifestItem, ...]:
    name_counts = Counter(item.safe_name.casefold() for item in items)
    checksum_counts = Counter(
        item.md5_checksum for item in items if item.md5_checksum is not None
    )
    marked: list[DriveManifestItem] = []
    for item in items:
        warnings = list(item.warnings)
        if name_counts[item.safe_name.casefold()] > 1:
            warnings.append("duplicate_name_candidate")
        if (
            item.md5_checksum is not None
            and checksum_counts[item.md5_checksum] > 1
        ):
            warnings.append("duplicate_content_candidate")
        marked.append(replace(item, warnings=_unique(warnings)))
    return tuple(marked)


def _redact_provider_identifiers_from_names(
    items: Sequence[DriveManifestItem],
    provider_identifiers: Sequence[str],
) -> tuple[DriveManifestItem, ...]:
    stable_identifiers = tuple(
        sorted(
            {value for value in provider_identifiers if value},
            key=len,
            reverse=True,
        )
    )
    redacted: list[DriveManifestItem] = []
    for item in items:
        safe_name = item.safe_name
        for identifier in stable_identifiers:
            safe_name = safe_name.replace(identifier, "[PROVIDER_ID]")
        warnings = item.warnings
        if safe_name != item.safe_name:
            warnings = _unique((*warnings, "provider_identifier_redacted_from_name"))
        redacted.append(replace(item, safe_name=safe_name, warnings=warnings))
    return tuple(redacted)


def _http_status(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    if type(status) is int:
        return status
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    return status if type(status) is int else None


def _read_failure_code(error: BaseException, final_attempt: bool) -> str | None:
    status = _http_status(error)
    if status == 401:
        return "drive_authentication_failed"
    if status == 403:
        return "drive_folder_access_denied"
    if status == 404:
        return "drive_folder_missing_or_inaccessible"
    retryable = (
        isinstance(error, (TimeoutError, ConnectionError, ConnectionResetError))
        or status == 429
        or (status is not None and 500 <= status <= 599)
    )
    if retryable:
        return "drive_metadata_temporarily_unavailable" if final_attempt else None
    return "drive_metadata_read_failed"


def _list_page_with_retry(
    gateway: DriveMetadataListGateway,
    folder_id: str,
    *,
    page_token: str | None,
    page_size: int,
) -> object:
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return gateway.list_folder_children(
                folder_id,
                page_token=page_token,
                page_size=page_size,
            )
        except Exception as error:
            code = _read_failure_code(
                error,
                final_attempt=attempt + 1 == MAX_RETRY_ATTEMPTS,
            )
            if code is not None:
                raise _SafeDriveReadFailure(code) from None
    raise AssertionError("bounded Drive retry loop exited unexpectedly")


def _invalid_handle(handle: SecureGoogleDriveFolderHandle) -> bool:
    return (
        handle.provider != "google_drive"
        or handle.resource_kind != "folder"
        or not is_valid_drive_id(handle.raw_folder_id)
        or handle.folder_id_fingerprint
        != fingerprint_drive_id(handle.raw_folder_id)
        or not isinstance(handle.sku, str)
        or _SAFE_SKU_PATTERN.fullmatch(handle.sku) is None
        or not isinstance(handle.product_source, ProductSourceRange)
        or type(handle.product_source.start_row) is not int
        or type(handle.product_source.end_row) is not int
        or handle.product_source.start_row <= 0
        or handle.product_source.end_row < handle.product_source.start_row
    )


def _safe_handle_fingerprint(handle: SecureGoogleDriveFolderHandle) -> str:
    if (
        isinstance(handle.folder_id_fingerprint, str)
        and _SHA256_PATTERN.fullmatch(handle.folder_id_fingerprint)
    ):
        return handle.folder_id_fingerprint
    if isinstance(handle.raw_folder_id, str):
        return fingerprint_drive_id(handle.raw_folder_id)
    return "0" * 64


def _safe_handle_sku(handle: SecureGoogleDriveFolderHandle) -> str:
    if isinstance(handle.sku, str) and _SAFE_SKU_PATTERN.fullmatch(handle.sku):
        return handle.sku
    return "[INVALID_SKU]"


def _failure_manifest(
    handle: SecureGoogleDriveFolderHandle,
    code: str,
    *,
    pages_read: int,
) -> GoogleDriveFolderManifest:
    if code in {"drive_authentication_failed", "drive_folder_access_denied"}:
        status: ManifestStatus = "access_denied"
    elif code == "drive_folder_missing_or_inaccessible":
        status = "missing_or_inaccessible"
    else:
        status = "read_failed"
    return GoogleDriveFolderManifest(
        sku=_safe_handle_sku(handle),
        product_source=handle.product_source,
        folder_id_fingerprint=_safe_handle_fingerprint(handle),
        status=status,
        items=(),
        pages_read=pages_read,
        warnings=(code,),
        blocking_issues=(code,),
    )


def _list_one_manifest(
    handle: SecureGoogleDriveFolderHandle,
    gateway: DriveMetadataListGateway,
    *,
    page_size: int,
    max_pages: int,
    max_items_per_folder: int,
) -> GoogleDriveFolderManifest:
    if _invalid_handle(handle):
        return GoogleDriveFolderManifest(
            sku=_safe_handle_sku(handle),
            product_source=handle.product_source,
            folder_id_fingerprint=_safe_handle_fingerprint(handle),
            status="invalid_folder_handle",
            items=(),
            pages_read=0,
            warnings=("invalid_folder_handle",),
            blocking_issues=("invalid_folder_handle",),
        )
    items: list[DriveManifestItem] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    provider_identifiers: set[str] = {handle.raw_folder_id}
    if handle.resource_key:
        provider_identifiers.add(handle.resource_key)
    pages_read = 0
    limit_issue: str | None = None
    while True:
        try:
            response = _list_page_with_retry(
                gateway,
                handle.raw_folder_id,
                page_token=page_token,
                page_size=page_size,
            )
        except _SafeDriveReadFailure as error:
            return _failure_manifest(handle, error.code, pages_read=pages_read)
        pages_read += 1
        if not isinstance(response, Mapping):
            return _failure_manifest(
                handle,
                "invalid_drive_metadata_response",
                pages_read=pages_read,
            )
        raw_items = response.get("files", [])
        if not isinstance(raw_items, list):
            return _failure_manifest(
                handle,
                "invalid_drive_metadata_response",
                pages_read=pages_read,
            )
        provider_identifiers.update(
            raw_item["id"]
            for raw_item in raw_items
            if isinstance(raw_item, Mapping)
            and isinstance(raw_item.get("id"), str)
            and raw_item.get("id")
        )
        remaining = max_items_per_folder - len(items)
        items.extend(_manifest_item(item) for item in raw_items[:remaining])
        if len(raw_items) > remaining:
            limit_issue = "folder_manifest_limit_exceeded"
            break
        next_token = response.get("nextPageToken")
        if next_token is None or next_token == "":
            break
        if not isinstance(next_token, str):
            return _failure_manifest(
                handle,
                "invalid_drive_metadata_response",
                pages_read=pages_read,
            )
        if next_token in seen_tokens:
            limit_issue = "duplicate_drive_page_token"
            break
        seen_tokens.add(next_token)
        if pages_read >= max_pages:
            limit_issue = "folder_manifest_limit_exceeded"
            break
        page_token = next_token
    stable_items = _redact_provider_identifiers_from_names(
        items,
        tuple(provider_identifiers),
    )
    stable_items = _mark_duplicate_candidates(stable_items)
    stable_items = tuple(
        sorted(
            stable_items,
            key=lambda item: (
                item.safe_name.casefold(),
                item.file_id_fingerprint or "",
            ),
        )
    )
    warnings: list[str] = []
    if any(item.item_kind == "nested_folder" for item in stable_items):
        warnings.append("nested_folder_present")
    if any(item.item_kind == "shortcut" for item in stable_items):
        warnings.append("shortcut_not_followed")
    if any("duplicate_name_candidate" in item.warnings for item in stable_items):
        warnings.append("duplicate_name_candidate")
    if any(
        "duplicate_content_candidate" in item.warnings for item in stable_items
    ):
        warnings.append("duplicate_content_candidate")
    blockers: tuple[str, ...] = ()
    if limit_issue is not None:
        warnings.append(limit_issue)
        blockers = (limit_issue,)
        status: ManifestStatus = "limit_exceeded"
    elif stable_items:
        status = "listed"
    else:
        status = "empty_folder"
    return GoogleDriveFolderManifest(
        sku=handle.sku,
        product_source=handle.product_source,
        folder_id_fingerprint=handle.folder_id_fingerprint,
        status=status,
        items=stable_items,
        pages_read=pages_read,
        warnings=_unique(warnings),
        blocking_issues=blockers,
    )


def _request_count(gateway: object) -> int:
    counters = getattr(gateway, "counters", None)
    value = getattr(counters, "read_requests_performed", 0)
    return value if type(value) is int and value >= 0 else 0


def _summary(
    manifests: Sequence[GoogleDriveFolderManifest],
    drive_read_requests: int,
) -> GoogleDriveFolderManifestSummary:
    items = tuple(item for manifest in manifests for item in manifest.items)
    return GoogleDriveFolderManifestSummary(
        total_folders=len(manifests),
        folders_listed=sum(item.status == "listed" for item in manifests),
        empty_folders=sum(item.status == "empty_folder" for item in manifests),
        folders_access_denied=sum(
            item.status == "access_denied" for item in manifests
        ),
        folders_missing_or_inaccessible=sum(
            item.status == "missing_or_inaccessible" for item in manifests
        ),
        folders_limit_exceeded=sum(
            item.status == "limit_exceeded" for item in manifests
        ),
        folders_read_failed=sum(
            item.status == "read_failed" for item in manifests
        ),
        total_items=len(items),
        image_candidates=sum(item.image_candidate for item in items),
        nested_folders=sum(item.item_kind == "nested_folder" for item in items),
        shortcuts=sum(item.item_kind == "shortcut" for item in items),
        google_workspace_files=sum(
            item.item_kind == "google_workspace_file" for item in items
        ),
        other_files=sum(item.item_kind == "other_file" for item in items),
        duplicate_name_candidates=sum(
            "duplicate_name_candidate" in item.warnings for item in items
        ),
        duplicate_content_candidates=sum(
            "duplicate_content_candidate" in item.warnings for item in items
        ),
        pages_read=sum(item.pages_read for item in manifests),
        drive_read_requests_performed=drive_read_requests,
        download_requests_performed=0,
        write_requests_performed=0,
    )


def build_drive_folder_manifests_with_gateway(
    handles: Sequence[SecureGoogleDriveFolderHandle],
    gateway: DriveMetadataListGateway,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items_per_folder: int = DEFAULT_MAX_ITEMS_PER_FOLDER,
) -> GoogleDriveFolderManifestBatchResult:
    """Build immutable manifests from a metadata-only mocked or real gateway."""

    if isinstance(handles, (str, bytes)) or not isinstance(handles, Sequence):
        raise TypeError("handles must be a sequence")
    stable_handles = tuple(handles)
    if any(
        not isinstance(item, SecureGoogleDriveFolderHandle)
        for item in stable_handles
    ):
        raise TypeError("handles must contain SecureGoogleDriveFolderHandle values")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be from 1 to 100")
    if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
        raise ValueError("max_pages must be from 1 to 20")
    if not 1 <= max_items_per_folder <= DEFAULT_MAX_ITEMS_PER_FOLDER:
        raise ValueError("max_items_per_folder must be from 1 to 1000")
    manifests = tuple(
        sorted(
            (
                _list_one_manifest(
                    handle,
                    gateway,
                    page_size=page_size,
                    max_pages=max_pages,
                    max_items_per_folder=max_items_per_folder,
                )
                for handle in stable_handles
            ),
            key=lambda item: (
                item.product_source.start_row,
                item.product_source.end_row,
                item.sku,
                item.folder_id_fingerprint,
            ),
        )
    )
    summary = _summary(manifests, _request_count(gateway))
    result = GoogleDriveFolderManifestBatchResult(
        manifests=manifests,
        summary=summary,
        download_requests_performed=0,
        write_requests_performed=0,
    )
    result.to_report_dict()
    return result


def build_drive_folder_manifests(
    handles: Sequence[SecureGoogleDriveFolderHandle],
    settings: GoogleSettings,
    client_factory: GoogleDriveMetadataClientFactory,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items_per_folder: int = DEFAULT_MAX_ITEMS_PER_FOLDER,
) -> GoogleDriveFolderManifestBatchResult:
    """Create the shared official client only after exact scope validation."""

    if settings.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE:
        raise DriveMetadataScopeUnavailable("drive_metadata_scope_unavailable")
    stable_handles = tuple(handles)
    if stable_handles and all(_invalid_handle(handle) for handle in stable_handles):
        return build_drive_folder_manifests_with_gateway(
            stable_handles,
            GoogleDriveMetadataGateway(object()),
            page_size=page_size,
            max_pages=max_pages,
            max_items_per_folder=max_items_per_folder,
        )
    drive_client = client_factory.create_drive_metadata(settings)
    gateway = GoogleDriveMetadataGateway(drive_client)
    return build_drive_folder_manifests_with_gateway(
        stable_handles,
        gateway,
        page_size=page_size,
        max_pages=max_pages,
        max_items_per_folder=max_items_per_folder,
    )


def _assert_report_safe(report: Mapping[str, object]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if (
        REPORT_SECRET_SCAN_PATTERN.search(serialized)
        or _UNSAFE_REPORT_PATTERN.search(serialized)
    ):
        raise GoogleDriveFolderManifestError("unsafe_drive_manifest_output")
