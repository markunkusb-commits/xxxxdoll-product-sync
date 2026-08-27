"""Depth-one Drive metadata traversal over trusted, in-memory root items.

No client construction, report loading, content download, or recursive call is
provided here. Root Core owns listing, pagination, retries and item normalization.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from . import google_drive_folder_manifest as root_core
from .image_mapping import ProductSourceRange


MAX_TRAVERSAL_DEPTH = 1
MAX_NESTED_FOLDERS_PER_RUN = 100
NestedManifestStatus = Literal[
    "listed", "empty_folder", "access_denied", "missing_or_inaccessible",
    "limit_exceeded", "read_failed", "invalid_nested_folder_handle",
]


class GoogleDriveNestedFolderManifestError(root_core.GoogleDriveFolderManifestError):
    """Fixed error codes only; never includes provider data or exception repr."""


@dataclass(frozen=True, slots=True, repr=False)
class SecureGoogleDriveNestedFolderHandle:
    sku: str
    product_source: ProductSourceRange
    root_folder_id_fingerprint: str
    nested_folder_id_fingerprint: str
    raw_nested_folder_id: str = field(repr=False)
    safe_folder_name: str
    depth: Literal[1] = MAX_TRAVERSAL_DEPTH

    def to_safe_dict(self) -> dict[str, object]:
        return _safe_identity(self)

    def to_dict(self) -> dict[str, object]:
        return self.to_safe_dict()

    def __repr__(self) -> str:
        return f"SecureGoogleDriveNestedFolderHandle({self.to_safe_dict()!r})"


def _root_handle(
    handle: SecureGoogleDriveNestedFolderHandle,
) -> root_core.SecureGoogleDriveFolderHandle:
    return root_core.SecureGoogleDriveFolderHandle(
        provider="google_drive",
        resource_kind="folder",
        raw_folder_id=handle.raw_nested_folder_id,
        folder_id_fingerprint=handle.nested_folder_id_fingerprint,
        sku=handle.sku,
        product_source=handle.product_source,
    )


def _identity(handle: SecureGoogleDriveNestedFolderHandle) -> dict[str, object]:
    return {
        "sku": handle.sku,
        "product_source": handle.product_source.to_dict(),
        "root_folder_id_fingerprint": handle.root_folder_id_fingerprint,
        "nested_folder_id_fingerprint": handle.nested_folder_id_fingerprint,
        "safe_folder_name": handle.safe_folder_name,
        "depth": handle.depth,
    }


def _valid_handle(handle: SecureGoogleDriveNestedFolderHandle) -> bool:
    if type(handle.depth) is not int or handle.depth != MAX_TRAVERSAL_DEPTH:
        return False
    if root_core._invalid_handle(_root_handle(handle)):
        return False
    if (
        not isinstance(handle.root_folder_id_fingerprint, str)
        or root_core._SHA256_PATTERN.fullmatch(handle.root_folder_id_fingerprint) is None
        or not isinstance(handle.safe_folder_name, str)
        or root_core._safe_name(handle.safe_folder_name)[0] != handle.safe_folder_name
    ):
        return False
    identity = _identity(handle)
    try:
        root_core._assert_report_safe(identity)
    except root_core.GoogleDriveFolderManifestError:
        return False
    # A manually constructed handle cannot smuggle its raw ID into public fields.
    return all(
        handle.raw_nested_folder_id not in value
        for value in identity.values() if isinstance(value, str)
    )


def _safe_identity(handle: SecureGoogleDriveNestedFolderHandle) -> dict[str, object]:
    if _valid_handle(handle):
        return _identity(handle)
    return {
        "sku": "[INVALID_SKU]",
        "product_source": {"start_row": 0, "end_row": 0},
        "root_folder_id_fingerprint": "0" * 64,
        "nested_folder_id_fingerprint": "0" * 64,
        "safe_folder_name": "[INVALID_FOLDER]",
        "depth": MAX_TRAVERSAL_DEPTH,
    }


def create_secure_google_drive_nested_folder_handle(
    root_manifest: root_core.GoogleDriveFolderManifest,
    item: root_core.DriveManifestItem,
) -> SecureGoogleDriveNestedFolderHandle:
    """Promote only an actual nested-folder item from a successful Root result."""

    if not isinstance(root_manifest, root_core.GoogleDriveFolderManifest):
        raise GoogleDriveNestedFolderManifestError("root_manifest_domain_object_required")
    if (
        not isinstance(item, root_core.DriveManifestItem)
        or not any(candidate is item for candidate in root_manifest.items)
        or root_manifest.status != "listed"
        or root_manifest.blocking_issues
        or item.item_kind != "nested_folder"
        or item.mime_type != root_core.FOLDER_MIME_TYPE
        or item.image_candidate
    ):
        raise GoogleDriveNestedFolderManifestError("invalid_nested_folder_handle")
    handle = SecureGoogleDriveNestedFolderHandle(
        sku=root_manifest.sku,
        product_source=root_manifest.product_source,
        root_folder_id_fingerprint=root_manifest.folder_id_fingerprint,
        nested_folder_id_fingerprint=item.file_id_fingerprint,
        raw_nested_folder_id=item.provider_file_id,
        safe_folder_name=item.safe_name,
    )
    if not _valid_handle(handle):
        raise GoogleDriveNestedFolderManifestError("invalid_nested_folder_handle")
    return handle


@dataclass(frozen=True, slots=True)
class GoogleDriveNestedFolderManifest:
    sku: str
    product_source: ProductSourceRange
    root_folder_id_fingerprint: str
    nested_folder_id_fingerprint: str
    safe_folder_name: str
    depth: Literal[1]
    status: NestedManifestStatus
    items: tuple[root_core.DriveManifestItem, ...]
    pages_read: int
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result = {
            "sku": self.sku,
            "product_source": self.product_source.to_dict(),
            "root_folder_id_fingerprint": self.root_folder_id_fingerprint,
            "nested_folder_id_fingerprint": self.nested_folder_id_fingerprint,
            "safe_folder_name": self.safe_folder_name,
            "depth": self.depth,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "pages_read": self.pages_read,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }
        root_core._assert_report_safe(result)
        return result


@dataclass(frozen=True, slots=True)
class GoogleDriveNestedFolderManifestSummary:
    total_nested_folders: int
    nested_folders_listed: int
    empty_nested_folders: int
    nested_folders_access_denied: int
    nested_folders_missing_or_inaccessible: int
    nested_folders_limit_exceeded: int
    nested_folders_read_failed: int
    invalid_nested_folder_handles: int
    total_nested_items: int
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
            "download_requests_performed": 0,
            "write_requests_performed": 0,
        }


@dataclass(frozen=True, slots=True)
class GoogleDriveNestedFolderManifestBatchResult:
    manifests: tuple[GoogleDriveNestedFolderManifest, ...]
    summary: GoogleDriveNestedFolderManifestSummary

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
            "download_requests_performed": 0,
            "write_requests_performed": 0,
        }
        root_core._assert_report_safe(report)
        return report

    def to_dict(self) -> dict[str, object]:
        return self.to_report_dict()


def _manifest(
    handle: SecureGoogleDriveNestedFolderHandle,
    listed: root_core.GoogleDriveFolderManifest | None,
    *,
    shared: bool,
) -> GoogleDriveNestedFolderManifest:
    identity = handle.to_safe_dict()
    source = identity.pop("product_source")
    if listed is None:
        return GoogleDriveNestedFolderManifest(
            **identity,
            product_source=ProductSourceRange(**source),
            status="invalid_nested_folder_handle",
            items=(), pages_read=0,
            warnings=("invalid_nested_folder_handle",),
            blocking_issues=("invalid_nested_folder_handle",),
        )
    items = tuple(
        replace(item, warnings=root_core._unique((
            *item.warnings, "max_traversal_depth_reached",
        ))) if item.item_kind == "nested_folder" else item
        for item in listed.items
    )
    warnings = list(listed.warnings)
    if any(item.item_kind == "nested_folder" for item in items):
        warnings.append("max_traversal_depth_reached")
    if shared:
        warnings.append("shared_nested_folder_candidate")
    return GoogleDriveNestedFolderManifest(
        **identity,
        product_source=ProductSourceRange(**source),
        status=listed.status, items=items, pages_read=listed.pages_read,
        warnings=root_core._unique(warnings), blocking_issues=listed.blocking_issues,
    )


def _handle_sort_key(handle: SecureGoogleDriveNestedFolderHandle) -> tuple[object, ...]:
    identity = handle.to_safe_dict()
    source = identity["product_source"]
    return (
        identity["sku"], identity["safe_folder_name"].casefold(),
        identity["nested_folder_id_fingerprint"], identity["root_folder_id_fingerprint"],
        source["start_row"], source["end_row"], identity["safe_folder_name"],
    )


def build_nested_drive_folder_manifests_with_gateway(
    handles: Sequence[SecureGoogleDriveNestedFolderHandle],
    gateway: root_core.DriveMetadataListGateway,
    *,
    max_nested_folders_per_run: int = MAX_NESTED_FOLDERS_PER_RUN,
) -> GoogleDriveNestedFolderManifestBatchResult:
    """List depth-one handles once each using the unchanged Root listing policy.

    Serialized reports, root items themselves and Nested manifests are not valid
    inputs. Create handles from Root domain objects explicitly. There is no
    promotion/traversal loop over the returned children.
    """

    if (
        isinstance(handles, (str, bytes)) or not isinstance(handles, Sequence)
    ):
        raise GoogleDriveNestedFolderManifestError("nested_folder_handles_required")
    if (
        type(max_nested_folders_per_run) is not int
        or not 1 <= max_nested_folders_per_run <= MAX_NESTED_FOLDERS_PER_RUN
    ):
        raise GoogleDriveNestedFolderManifestError("invalid_nested_folder_batch_limit")
    if len(handles) > max_nested_folders_per_run:
        raise GoogleDriveNestedFolderManifestError("nested_folder_batch_limit_exceeded")
    stable_handles = tuple(handles)
    if any(not isinstance(item, SecureGoogleDriveNestedFolderHandle) for item in stable_handles):
        raise GoogleDriveNestedFolderManifestError("nested_folder_handles_required")
    stable_handles = tuple(sorted(stable_handles, key=_handle_sort_key))
    owners: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    for handle in stable_handles:
        if _valid_handle(handle):
            owners[handle.nested_folder_id_fingerprint].add((
                handle.sku, handle.product_source, handle.root_folder_id_fingerprint,
            ))
    reads_before = root_core._request_count(gateway)
    manifests = []
    core_manifests = []
    for handle in stable_handles:
        listed = None
        if _valid_handle(handle):
            # Deliberately use the Core defaults: 100 / 20 / 1000 and its retry policy.
            core_batch = root_core.build_drive_folder_manifests_with_gateway(
                (_root_handle(handle),), gateway,
            )
            listed = core_batch.manifests[0]
            core_manifests.append(listed)
        manifests.append(_manifest(
            handle, listed,
            shared=listed is not None and len(owners[handle.nested_folder_id_fingerprint]) > 1,
        ))
    counts = root_core._summary(
        core_manifests, root_core._request_count(gateway) - reads_before,
    )
    summary = GoogleDriveNestedFolderManifestSummary(
        total_nested_folders=len(manifests),
        nested_folders_listed=counts.folders_listed,
        empty_nested_folders=counts.empty_folders,
        nested_folders_access_denied=counts.folders_access_denied,
        nested_folders_missing_or_inaccessible=counts.folders_missing_or_inaccessible,
        nested_folders_limit_exceeded=counts.folders_limit_exceeded,
        nested_folders_read_failed=counts.folders_read_failed,
        invalid_nested_folder_handles=len(manifests) - len(core_manifests),
        total_nested_items=counts.total_items,
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
    result = GoogleDriveNestedFolderManifestBatchResult(tuple(manifests), summary)
    result.to_report_dict()
    return result
