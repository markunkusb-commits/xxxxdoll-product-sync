"""Bounded depth-two metadata inventory from fresh depth-one domain items.

No config/client creation, local report loading or persistence is provided here.
Root Core owns listing, pagination, retries, classification and sanitization.
There is deliberately no traversal/promotion loop over returned children.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from . import google_drive_folder_manifest as root_core
from .google_drive_nested_folder_manifest import GoogleDriveNestedFolderManifest
from .image_mapping import ProductSourceRange


MAX_TRAVERSAL_DEPTH = 2
MAX_DEPTH2_FOLDERS_PER_RUN = 50
Depth2ManifestStatus = Literal[
    "listed", "empty_folder", "access_denied", "missing_or_inaccessible",
    "limit_exceeded", "read_failed", "invalid_depth2_folder_handle",
]


class GoogleDriveDepth2FolderManifestError(root_core.GoogleDriveFolderManifestError):
    """Fixed error codes only, without provider identifiers or exception repr."""


@dataclass(frozen=True, slots=True, repr=False)
class SecureGoogleDriveDepth2FolderHandle:
    sku: str
    product_source: ProductSourceRange
    root_folder_id_fingerprint: str
    depth1_folder_id_fingerprint: str
    depth2_folder_id_fingerprint: str
    depth1_safe_folder_name: str
    depth2_safe_folder_name: str
    raw_depth2_folder_id: str = field(repr=False)
    depth: Literal[2] = MAX_TRAVERSAL_DEPTH

    def to_safe_dict(self) -> dict[str, object]:
        return _safe_identity(self)

    def to_dict(self) -> dict[str, object]:
        # Like DriveManifestItem, serialize only via this allowlist, not asdict().
        return self.to_safe_dict()

    def __repr__(self) -> str:
        return f"SecureGoogleDriveDepth2FolderHandle({self.to_safe_dict()!r})"


def _root_handle(handle: SecureGoogleDriveDepth2FolderHandle) -> root_core.SecureGoogleDriveFolderHandle:
    return root_core.SecureGoogleDriveFolderHandle(
        provider="google_drive", resource_kind="folder",
        raw_folder_id=handle.raw_depth2_folder_id,
        folder_id_fingerprint=handle.depth2_folder_id_fingerprint,
        sku=handle.sku, product_source=handle.product_source,
    )


def _identity(handle: SecureGoogleDriveDepth2FolderHandle) -> dict[str, object]:
    return {
        "sku": handle.sku,
        "product_source": handle.product_source.to_dict(),
        "root_folder_id_fingerprint": handle.root_folder_id_fingerprint,
        "depth1_folder_id_fingerprint": handle.depth1_folder_id_fingerprint,
        "depth2_folder_id_fingerprint": handle.depth2_folder_id_fingerprint,
        "depth1_safe_folder_name": handle.depth1_safe_folder_name,
        "depth2_safe_folder_name": handle.depth2_safe_folder_name,
        "depth": handle.depth,
    }


def _valid_handle(handle: SecureGoogleDriveDepth2FolderHandle) -> bool:
    if type(handle.depth) is not int or handle.depth != MAX_TRAVERSAL_DEPTH:
        return False
    if root_core._invalid_handle(_root_handle(handle)):
        return False
    for fingerprint in (handle.root_folder_id_fingerprint, handle.depth1_folder_id_fingerprint):
        if not isinstance(fingerprint, str) or root_core._SHA256_PATTERN.fullmatch(fingerprint) is None:
            return False
    for name in (handle.depth1_safe_folder_name, handle.depth2_safe_folder_name):
        if not isinstance(name, str) or root_core._safe_name(name)[0] != name:
            return False
    identity = _identity(handle)
    try:
        root_core._assert_report_safe(identity)
    except root_core.GoogleDriveFolderManifestError:
        return False
    return all(
        handle.raw_depth2_folder_id not in value
        for value in identity.values() if isinstance(value, str)
    )


def _safe_identity(handle: SecureGoogleDriveDepth2FolderHandle) -> dict[str, object]:
    if _valid_handle(handle):
        return _identity(handle)
    return {
        "sku": "[INVALID_SKU]",
        "product_source": {"start_row": 0, "end_row": 0},
        "root_folder_id_fingerprint": "0" * 64,
        "depth1_folder_id_fingerprint": "0" * 64,
        "depth2_folder_id_fingerprint": "0" * 64,
        "depth1_safe_folder_name": "[INVALID_FOLDER]",
        "depth2_safe_folder_name": "[INVALID_FOLDER]",
        "depth": MAX_TRAVERSAL_DEPTH,
    }


def create_secure_google_drive_depth2_folder_handle(
    depth1_manifest: GoogleDriveNestedFolderManifest,
    item: root_core.DriveManifestItem,
) -> SecureGoogleDriveDepth2FolderHandle:
    """Promote only an actual depth-limit child from a successful depth-one read.

    Neither serialized reports nor a Depth-2 result can authorize another read.
    An equal-looking copy of an item is not sufficient provenance either.
    """

    if not isinstance(depth1_manifest, GoogleDriveNestedFolderManifest):
        raise GoogleDriveDepth2FolderManifestError("depth1_manifest_domain_object_required")
    if (
        type(depth1_manifest.depth) is not int or depth1_manifest.depth != 1
        or depth1_manifest.status != "listed"
        or depth1_manifest.blocking_issues
        or not isinstance(item, root_core.DriveManifestItem)
        or not any(candidate is item for candidate in depth1_manifest.items)
        or item.item_kind != "nested_folder"
        or item.mime_type != root_core.FOLDER_MIME_TYPE
        or item.image_candidate
        or "max_traversal_depth_reached" not in item.warnings
    ):
        raise GoogleDriveDepth2FolderManifestError("invalid_depth2_folder_handle")
    handle = SecureGoogleDriveDepth2FolderHandle(
        sku=depth1_manifest.sku, product_source=depth1_manifest.product_source,
        root_folder_id_fingerprint=depth1_manifest.root_folder_id_fingerprint,
        depth1_folder_id_fingerprint=depth1_manifest.nested_folder_id_fingerprint,
        depth2_folder_id_fingerprint=item.file_id_fingerprint,
        depth1_safe_folder_name=depth1_manifest.safe_folder_name,
        depth2_safe_folder_name=item.safe_name,
        raw_depth2_folder_id=item.provider_file_id,
    )
    if not _valid_handle(handle):
        raise GoogleDriveDepth2FolderManifestError("invalid_depth2_folder_handle")
    return handle


@dataclass(frozen=True, slots=True)
class GoogleDriveDepth2FolderManifest:
    sku: str
    product_source: ProductSourceRange
    root_folder_id_fingerprint: str
    depth1_folder_id_fingerprint: str
    depth2_folder_id_fingerprint: str
    depth1_safe_folder_name: str
    depth2_safe_folder_name: str
    depth: Literal[2]
    status: Depth2ManifestStatus
    items: tuple[root_core.DriveManifestItem, ...]
    pages_read: int
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result = {
            "sku": self.sku,
            "product_source": self.product_source.to_dict(),
            "root_folder_id_fingerprint": self.root_folder_id_fingerprint,
            "depth1_folder_id_fingerprint": self.depth1_folder_id_fingerprint,
            "depth2_folder_id_fingerprint": self.depth2_folder_id_fingerprint,
            "depth1_safe_folder_name": self.depth1_safe_folder_name,
            "depth2_safe_folder_name": self.depth2_safe_folder_name,
            "depth": self.depth, "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "pages_read": self.pages_read,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }
        root_core._assert_report_safe(result)
        return result


@dataclass(frozen=True, slots=True)
class GoogleDriveDepth2FolderManifestSummary:
    total_depth2_folders: int
    depth2_folders_listed: int
    empty_depth2_folders: int
    depth2_folders_access_denied: int
    depth2_folders_missing_or_inaccessible: int
    depth2_folders_limit_exceeded: int
    depth2_folders_read_failed: int
    invalid_depth2_folder_handles: int
    total_depth2_items: int
    image_candidates: int
    nested_folders_at_depth_limit: int
    shortcuts: int
    google_workspace_files: int
    other_files: int
    duplicate_name_candidates: int
    duplicate_content_candidates: int
    pages_read: int
    drive_read_requests_performed: int

    @property
    def download_requests_performed(self) -> Literal[0]:
        return 0

    @property
    def write_requests_performed(self) -> Literal[0]:
        return 0

    def to_dict(self) -> dict[str, int]:
        return {
            **{name: getattr(self, name) for name in self.__dataclass_fields__},
            "download_requests_performed": 0, "write_requests_performed": 0,
        }


@dataclass(frozen=True, slots=True)
class GoogleDriveDepth2FolderManifestBatchResult:
    manifests: tuple[GoogleDriveDepth2FolderManifest, ...]
    summary: GoogleDriveDepth2FolderManifestSummary

    @property
    def download_requests_performed(self) -> Literal[0]:
        return 0

    @property
    def write_requests_performed(self) -> Literal[0]:
        return 0

    def to_report_dict(self) -> dict[str, object]:
        report = {
            "status": "ok",
            "manifests": [item.to_dict() for item in self.manifests],
            "summary": self.summary.to_dict(),
            "download_requests_performed": 0, "write_requests_performed": 0,
        }
        root_core._assert_report_safe(report)
        return report

    def to_dict(self) -> dict[str, object]:
        return self.to_report_dict()


def _manifest(
    handle: SecureGoogleDriveDepth2FolderHandle,
    listed: root_core.GoogleDriveFolderManifest | None,
) -> GoogleDriveDepth2FolderManifest:
    identity = handle.to_safe_dict()
    source = identity.pop("product_source")
    if listed is None:
        return GoogleDriveDepth2FolderManifest(
            **identity, product_source=ProductSourceRange(**source),
            status="invalid_depth2_folder_handle", items=(), pages_read=0,
            warnings=("invalid_depth2_folder_handle",),
            blocking_issues=("invalid_depth2_folder_handle",),
        )
    items = tuple(
        replace(item, warnings=root_core._unique((
            *item.warnings, "max_traversal_depth_reached",
        ))) if item.item_kind == "nested_folder" else item
        for item in listed.items
    )
    warnings = listed.warnings
    if any(item.item_kind == "nested_folder" for item in items):
        warnings = root_core._unique((*warnings, "max_traversal_depth_reached"))
    return GoogleDriveDepth2FolderManifest(
        **identity, product_source=ProductSourceRange(**source),
        status=listed.status, items=items, pages_read=listed.pages_read,
        warnings=warnings, blocking_issues=listed.blocking_issues,
    )


def _handle_sort_key(handle: SecureGoogleDriveDepth2FolderHandle) -> tuple[object, ...]:
    identity = handle.to_safe_dict()
    source = identity["product_source"]
    return (
        identity["sku"], identity["root_folder_id_fingerprint"],
        identity["depth1_folder_id_fingerprint"], identity["depth2_folder_id_fingerprint"],
        source["start_row"], source["end_row"],
        identity["depth1_safe_folder_name"], identity["depth2_safe_folder_name"],
    )


def build_depth2_drive_folder_manifests_with_gateway(
    handles: Sequence[SecureGoogleDriveDepth2FolderHandle],
    gateway: root_core.DriveMetadataListGateway,
    *,
    max_depth2_folders_per_run: int = MAX_DEPTH2_FOLDERS_PER_RUN,
) -> GoogleDriveDepth2FolderManifestBatchResult:
    """Read at most 50 depth-two handles, never recurse into their children."""

    if isinstance(handles, (str, bytes)) or not isinstance(handles, Sequence):
        raise GoogleDriveDepth2FolderManifestError("depth2_folder_handles_required")
    if (
        type(max_depth2_folders_per_run) is not int
        or not 1 <= max_depth2_folders_per_run <= MAX_DEPTH2_FOLDERS_PER_RUN
    ):
        raise GoogleDriveDepth2FolderManifestError("invalid_depth2_folder_batch_limit")
    if len(handles) > max_depth2_folders_per_run:
        raise GoogleDriveDepth2FolderManifestError("depth2_folder_batch_limit_exceeded")
    stable_handles = tuple(handles)
    if any(not isinstance(item, SecureGoogleDriveDepth2FolderHandle) for item in stable_handles):
        raise GoogleDriveDepth2FolderManifestError("depth2_folder_handles_required")
    stable_handles = tuple(sorted(stable_handles, key=_handle_sort_key))
    reads_before = root_core._request_count(gateway)
    manifests = []
    core_manifests = []
    for handle in stable_handles:
        listed = None
        if _valid_handle(handle):
            # Keep the existing 100 / 20 / 1000 bounds and three-attempt retry policy.
            batch = root_core.build_drive_folder_manifests_with_gateway((_root_handle(handle),), gateway)
            listed = batch.manifests[0]
            core_manifests.append(listed)
        manifests.append(_manifest(handle, listed))
    counts = root_core._summary(core_manifests, root_core._request_count(gateway) - reads_before)
    summary = GoogleDriveDepth2FolderManifestSummary(
        total_depth2_folders=len(manifests),
        depth2_folders_listed=counts.folders_listed,
        empty_depth2_folders=counts.empty_folders,
        depth2_folders_access_denied=counts.folders_access_denied,
        depth2_folders_missing_or_inaccessible=counts.folders_missing_or_inaccessible,
        depth2_folders_limit_exceeded=counts.folders_limit_exceeded,
        depth2_folders_read_failed=counts.folders_read_failed,
        invalid_depth2_folder_handles=len(manifests) - len(core_manifests),
        total_depth2_items=counts.total_items,
        image_candidates=counts.image_candidates,
        nested_folders_at_depth_limit=counts.nested_folders,
        shortcuts=counts.shortcuts,
        google_workspace_files=counts.google_workspace_files,
        other_files=counts.other_files,
        duplicate_name_candidates=counts.duplicate_name_candidates,
        duplicate_content_candidates=counts.duplicate_content_candidates,
        pages_read=counts.pages_read,
        drive_read_requests_performed=counts.drive_read_requests_performed,
    )
    result = GoogleDriveDepth2FolderManifestBatchResult(tuple(manifests), summary)
    serialized = json.dumps(result.to_report_dict(), ensure_ascii=False, sort_keys=True)
    # Root Core redacts IDs within each listing. Also fail closed if a known ID
    # from another folder was placed in any public identity/metadata string.
    identifiers = {
        handle.raw_depth2_folder_id for handle in stable_handles if _valid_handle(handle)
    } | {
        item.provider_file_id for manifest in manifests for item in manifest.items
        if item.provider_file_id
    }
    if any(identifier in serialized for identifier in identifiers):
        raise GoogleDriveDepth2FolderManifestError("unsafe_depth2_manifest_output")
    return result
