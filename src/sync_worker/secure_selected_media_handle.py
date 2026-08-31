"""Memory-only provenance capability for one selected Drive image.

This module performs structural and exact-match verification only.  It never
loads reports, creates clients, reads Drive, opens media, downloads, converts,
uploads or writes.  Raw provider identity remains reachable solely through the
package-private helper after the capability is revalidated.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from . import (
    folder_role_policy,
    google_drive_depth2_folder_manifest as depth2_core,
    google_drive_folder_manifest as root_core,
    google_drive_nested_folder_manifest as nested_core,
    image_selection_policy,
)
from .image_mapping import ProductSourceRange


POLICY_VERSION = "xxxxdoll-secure-selected-media-handle-v1"
_HANDLE_CAPABILITY = object()
_BASELINE_CAPABILITY = object()
_AUDIT_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_SELECTED_ROLES = frozenset({
    image_selection_policy.ImageSelectionRole.PRIMARY,
    image_selection_policy.ImageSelectionRole.GALLERY,
})
_GALLERY_FOLDER_ROLES = frozenset({
    folder_role_policy.FolderRole.STOREFRONT_PHOTOS,
    folder_role_policy.FolderRole.FACTORY_PHOTOS,
})
_SAFE_BASELINE_IDENTITY_FIELDS = frozenset({
    "policy_version", "sku", "product_source", "source_manifest_kind", "depth",
    "safe_folder_name", "parent_safe_folder_name", "safe_name",
    "file_id_fingerprint", "md5_checksum", "source_mime_type", "size_bytes",
    "image_width", "image_height",
})


class SecureSelectedMediaHandleError(ValueError):
    """Fixed safe error codes only; provider identity is never interpolated."""


def _safe_codes(value: object, code: str) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or any(type(item) is not str or _AUDIT_CODE.fullmatch(item) is None for item in value)
    ):
        raise SecureSelectedMediaHandleError(code)
    return value


def _valid_source(value: object) -> bool:
    return (
        type(value) is ProductSourceRange
        and type(value.start_row) is int and value.start_row > 0
        and type(value.end_row) is int and value.end_row >= value.start_row
    )


def _validate_selection_item(item: object) -> image_selection_policy.ImageSelectionItem:
    if type(item) is not image_selection_policy.ImageSelectionItem:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if item.policy_version != image_selection_policy.POLICY_VERSION:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if type(item.blocking_issues) is not tuple:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if item.blocking_issues:
        raise SecureSelectedMediaHandleError("selection_item_blocked")
    if item.selected is not True:
        raise SecureSelectedMediaHandleError("selection_item_not_selected")
    if type(item.selection_position) is not int or item.selection_position < 0:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if type(item.image_role) is not image_selection_policy.ImageSelectionRole or item.image_role not in _SELECTED_ROLES:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if (
        (item.image_role is image_selection_policy.ImageSelectionRole.PRIMARY and item.selection_position != 0)
        or (item.image_role is image_selection_policy.ImageSelectionRole.GALLERY and item.selection_position == 0)
    ):
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if item.quality_eligible is not True:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if type(item.folder_role) is not folder_role_policy.FolderRole or item.folder_role not in _GALLERY_FOLDER_ROLES:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if not _valid_source(item.product_source):
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    if type(item.requires_deeper_inventory) is not bool:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    _safe_codes(item.warnings, "invalid_selection_item")
    expected_reason = {
        (folder_role_policy.FolderRole.STOREFRONT_PHOTOS, image_selection_policy.ImageSelectionRole.PRIMARY): image_selection_policy.ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY,
        (folder_role_policy.FolderRole.STOREFRONT_PHOTOS, image_selection_policy.ImageSelectionRole.GALLERY): image_selection_policy.ImageSelectionReason.SELECTED_STOREFRONT_GALLERY,
        (folder_role_policy.FolderRole.FACTORY_PHOTOS, image_selection_policy.ImageSelectionRole.PRIMARY): image_selection_policy.ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK,
        (folder_role_policy.FolderRole.FACTORY_PHOTOS, image_selection_policy.ImageSelectionRole.GALLERY): image_selection_policy.ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL,
    }[(item.folder_role, item.image_role)]
    if item.selection_reason is not expected_reason:
        raise SecureSelectedMediaHandleError("invalid_selection_item")
    try:
        root_core._assert_report_safe(item.to_dict())
        image_selection_policy._safe_text(item.sku, "sku", basename=True)
        image_selection_policy._safe_text(item.safe_name, "safe_name", basename=True)
        image_selection_policy._safe_text(item.safe_folder_name, "safe_folder_name")
        if item.parent_safe_folder_name is not None:
            image_selection_policy._safe_text(item.parent_safe_folder_name, "parent_safe_folder_name")
    except (root_core.GoogleDriveFolderManifestError, image_selection_policy.ImageSelectionPolicyError):
        raise SecureSelectedMediaHandleError("invalid_selection_item") from None
    return item


def _validate_item_shape(item: object) -> root_core.DriveManifestItem:
    if type(item) is not root_core.DriveManifestItem:
        raise SecureSelectedMediaHandleError("invalid_source_manifest")
    _safe_codes(item.warnings, "invalid_source_manifest")
    if (
        type(item.safe_name) is not str
        or root_core._safe_name(item.safe_name)[0] != item.safe_name
        or type(item.mime_type) is not str
        or root_core._MIME_TYPE_PATTERN.fullmatch(item.mime_type) is None
        or (item.size_bytes is not None and (type(item.size_bytes) is not int or item.size_bytes < 0))
        or (item.image_width is not None and (type(item.image_width) is not int or item.image_width <= 0))
        or (item.image_height is not None and (type(item.image_height) is not int or item.image_height <= 0))
    ):
        raise SecureSelectedMediaHandleError("invalid_source_manifest")
    try:
        root_core._assert_report_safe(item.to_dict())
    except root_core.GoogleDriveFolderManifestError:
        raise SecureSelectedMediaHandleError("invalid_source_manifest") from None
    return item


def _validate_manifest_common(manifest: object) -> None:
    if (
        manifest.status != "listed"
        or type(manifest.blocking_issues) is not tuple
        or manifest.blocking_issues
        or type(manifest.sku) is not str
        or root_core._SAFE_SKU_PATTERN.fullmatch(manifest.sku) is None
        or not _valid_source(manifest.product_source)
        or type(manifest.items) is not tuple
        or any(type(item) is not root_core.DriveManifestItem for item in manifest.items)
    ):
        if getattr(manifest, "blocking_issues", ()):
            raise SecureSelectedMediaHandleError("source_manifest_blocked")
        raise SecureSelectedMediaHandleError("invalid_source_manifest")
    _safe_codes(manifest.warnings, "invalid_source_manifest")
    for item in manifest.items:
        _validate_item_shape(item)
    try:
        manifest.to_dict()
    except (root_core.GoogleDriveFolderManifestError, TypeError, ValueError):
        raise SecureSelectedMediaHandleError("invalid_source_manifest") from None


def _validate_manifest(
    selection: image_selection_policy.ImageSelectionItem,
    manifest: object,
) -> tuple[str, int, str, str | None]:
    if type(manifest) is nested_core.GoogleDriveNestedFolderManifest:
        _validate_manifest_common(manifest)
        if (
            type(manifest.depth) is not int or manifest.depth != 1
            or selection.source_manifest_kind != "nested" or selection.depth != 1
            or selection.safe_folder_name != manifest.safe_folder_name
            or selection.parent_safe_folder_name is not None
        ):
            raise SecureSelectedMediaHandleError("selected_media_provenance_mismatch")
        return "nested", 1, manifest.safe_folder_name, None
    if type(manifest) is depth2_core.GoogleDriveDepth2FolderManifest:
        _validate_manifest_common(manifest)
        if (
            type(manifest.depth) is not int or manifest.depth != 2
            or selection.source_manifest_kind != "depth2" or selection.depth != 2
            or selection.safe_folder_name != manifest.depth2_safe_folder_name
            or selection.parent_safe_folder_name != manifest.depth1_safe_folder_name
        ):
            raise SecureSelectedMediaHandleError("selected_media_provenance_mismatch")
        return (
            "depth2", 2, manifest.depth2_safe_folder_name,
            manifest.depth1_safe_folder_name,
        )
    raise SecureSelectedMediaHandleError("source_manifest_domain_object_required")


def _exact_source_item(
    selection: image_selection_policy.ImageSelectionItem,
    manifest: nested_core.GoogleDriveNestedFolderManifest | depth2_core.GoogleDriveDepth2FolderManifest,
) -> root_core.DriveManifestItem:
    matches = tuple(item for item in manifest.items if item.safe_name == selection.safe_name)
    if not matches:
        raise SecureSelectedMediaHandleError("selected_media_source_missing")
    if len(matches) > 1:
        raise SecureSelectedMediaHandleError("selected_media_source_ambiguous")
    item = matches[0]
    if item.item_kind != "image_candidate" or item.image_candidate is not True:
        raise SecureSelectedMediaHandleError("selected_media_source_not_image_candidate")
    return item


def _public_strings(value: object):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _public_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _public_strings(item)
    elif isinstance(value, str):
        yield value


class SelectedMediaBaselineIdentity:
    """Safe, non-authorizing identity captured from an approved snapshot."""

    __slots__ = (
        "__capability", "_sku", "_product_source", "_source_manifest_kind",
        "_depth", "_safe_folder_name", "_parent_safe_folder_name", "_safe_name",
        "_file_id_fingerprint", "_md5_checksum", "_source_mime_type",
        "_size_bytes", "_image_width", "_image_height",
    )

    def __init__(
        self,
        capability: object | None = None,
        *,
        selection_item: image_selection_policy.ImageSelectionItem | None = None,
        source_item: root_core.DriveManifestItem | None = None,
    ) -> None:
        if capability is not _BASELINE_CAPABILITY:
            raise SecureSelectedMediaHandleError("selected_media_baseline_factory_required")
        if type(selection_item) is not image_selection_policy.ImageSelectionItem or type(source_item) is not root_core.DriveManifestItem:
            raise SecureSelectedMediaHandleError("invalid_selected_media_baseline")
        object.__setattr__(self, "_SelectedMediaBaselineIdentity__capability", capability)
        object.__setattr__(self, "_sku", selection_item.sku)
        object.__setattr__(self, "_product_source", selection_item.product_source)
        object.__setattr__(self, "_source_manifest_kind", selection_item.source_manifest_kind)
        object.__setattr__(self, "_depth", selection_item.depth)
        object.__setattr__(self, "_safe_folder_name", selection_item.safe_folder_name)
        object.__setattr__(self, "_parent_safe_folder_name", selection_item.parent_safe_folder_name)
        object.__setattr__(self, "_safe_name", selection_item.safe_name)
        object.__setattr__(self, "_file_id_fingerprint", source_item.file_id_fingerprint)
        object.__setattr__(self, "_md5_checksum", source_item.md5_checksum)
        object.__setattr__(self, "_source_mime_type", source_item.mime_type)
        object.__setattr__(self, "_size_bytes", source_item.size_bytes)
        object.__setattr__(self, "_image_width", source_item.image_width)
        object.__setattr__(self, "_image_height", source_item.image_height)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("selected_media_baseline_is_immutable")

    @property
    def policy_version(self) -> str:
        return POLICY_VERSION

    @property
    def sku(self) -> str:
        return self._sku

    @property
    def product_source(self) -> ProductSourceRange:
        return self._product_source

    @property
    def source_manifest_kind(self) -> str:
        return self._source_manifest_kind

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def safe_folder_name(self) -> str:
        return self._safe_folder_name

    @property
    def parent_safe_folder_name(self) -> str | None:
        return self._parent_safe_folder_name

    @property
    def safe_name(self) -> str:
        return self._safe_name

    @property
    def file_id_fingerprint(self) -> str:
        return self._file_id_fingerprint

    @property
    def md5_checksum(self) -> str:
        return self._md5_checksum

    @property
    def source_mime_type(self) -> str:
        return self._source_mime_type

    @property
    def size_bytes(self) -> int | None:
        return self._size_bytes

    @property
    def image_width(self) -> int | None:
        return self._image_width

    @property
    def image_height(self) -> int | None:
        return self._image_height

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "policy_version": POLICY_VERSION, "sku": self.sku,
            "product_source": self.product_source.to_dict(),
            "source_manifest_kind": self.source_manifest_kind, "depth": self.depth,
            "safe_folder_name": self.safe_folder_name,
            "parent_safe_folder_name": self.parent_safe_folder_name,
            "safe_name": self.safe_name,
            "file_id_fingerprint": self.file_id_fingerprint,
            "md5_checksum": self.md5_checksum,
            "source_mime_type": self.source_mime_type,
            "size_bytes": self.size_bytes, "image_width": self.image_width,
            "image_height": self.image_height,
        }

    def __repr__(self) -> str:
        return f"SelectedMediaBaselineIdentity({self.to_safe_dict()!r})"

    __str__ = __repr__


def _validate_baseline_identity(
    baseline: object,
    selection: image_selection_policy.ImageSelectionItem | None = None,
) -> SelectedMediaBaselineIdentity:
    if type(baseline) is not SelectedMediaBaselineIdentity:
        raise SecureSelectedMediaHandleError("selected_media_baseline_identity_required")
    try:
        capability = object.__getattribute__(baseline, "_SelectedMediaBaselineIdentity__capability")
        safe = baseline.to_safe_dict()
    except (AttributeError, TypeError, ValueError):
        raise SecureSelectedMediaHandleError("invalid_selected_media_baseline") from None
    if capability is not _BASELINE_CAPABILITY:
        raise SecureSelectedMediaHandleError("invalid_selected_media_baseline")
    if type(baseline.file_id_fingerprint) is not str or root_core._SHA256_PATTERN.fullmatch(baseline.file_id_fingerprint) is None:
        raise SecureSelectedMediaHandleError("selected_media_baseline_fingerprint_missing")
    if type(baseline.md5_checksum) is not str or root_core._MD5_PATTERN.fullmatch(baseline.md5_checksum) is None:
        raise SecureSelectedMediaHandleError("selected_media_baseline_checksum_missing")
    if (
        baseline.policy_version != POLICY_VERSION
        or not _valid_source(baseline.product_source)
        or type(baseline.source_mime_type) is not str
        or root_core._MIME_TYPE_PATTERN.fullmatch(baseline.source_mime_type) is None
        or (baseline.size_bytes is not None and (type(baseline.size_bytes) is not int or baseline.size_bytes < 0))
        or (baseline.image_width is not None and (type(baseline.image_width) is not int or baseline.image_width <= 0))
        or (baseline.image_height is not None and (type(baseline.image_height) is not int or baseline.image_height <= 0))
    ):
        raise SecureSelectedMediaHandleError("invalid_selected_media_baseline")
    if selection is not None and (
        baseline.sku != selection.sku
        or baseline.product_source != selection.product_source
        or baseline.source_manifest_kind != selection.source_manifest_kind
        or baseline.depth != selection.depth
        or baseline.safe_folder_name != selection.safe_folder_name
        or baseline.parent_safe_folder_name != selection.parent_safe_folder_name
        or baseline.safe_name != selection.safe_name
    ):
        raise SecureSelectedMediaHandleError("selected_media_baseline_provenance_mismatch")
    try:
        root_core._assert_report_safe(safe)
    except root_core.GoogleDriveFolderManifestError:
        raise SecureSelectedMediaHandleError("invalid_selected_media_baseline") from None
    return baseline


def create_selected_media_baseline_identity(
    selection_item: image_selection_policy.ImageSelectionItem,
    baseline_manifest: nested_core.GoogleDriveNestedFolderManifest | depth2_core.GoogleDriveDepth2FolderManifest,
) -> SelectedMediaBaselineIdentity:
    """Capture safe snapshot identity; never capture provider authority."""
    selection = _validate_selection_item(selection_item)
    kind, depth, folder, parent = _validate_manifest(selection, baseline_manifest)
    if (
        selection.sku != baseline_manifest.sku
        or selection.product_source != baseline_manifest.product_source
        or selection.source_manifest_kind != kind or selection.depth != depth
        or selection.safe_folder_name != folder
        or selection.parent_safe_folder_name != parent
    ):
        raise SecureSelectedMediaHandleError("selected_media_baseline_provenance_mismatch")
    matches = tuple(item for item in baseline_manifest.items if item.safe_name == selection.safe_name)
    if not matches:
        raise SecureSelectedMediaHandleError("selected_media_baseline_missing")
    if len(matches) > 1:
        raise SecureSelectedMediaHandleError("selected_media_baseline_ambiguous")
    source_item = matches[0]
    if source_item.item_kind != "image_candidate" or source_item.image_candidate is not True:
        raise SecureSelectedMediaHandleError("selected_media_baseline_not_image_candidate")
    if type(source_item.file_id_fingerprint) is not str or root_core._SHA256_PATTERN.fullmatch(source_item.file_id_fingerprint) is None:
        raise SecureSelectedMediaHandleError("selected_media_baseline_fingerprint_missing")
    if type(source_item.md5_checksum) is not str or root_core._MD5_PATTERN.fullmatch(source_item.md5_checksum) is None:
        raise SecureSelectedMediaHandleError("selected_media_baseline_checksum_missing")
    baseline = SelectedMediaBaselineIdentity(
        _BASELINE_CAPABILITY, selection_item=selection, source_item=source_item,
    )
    return _validate_baseline_identity(baseline, selection)


def restore_selected_media_baseline_identity(
    selection_item: image_selection_policy.ImageSelectionItem,
    safe_baseline_identity: Mapping[str, object],
) -> SelectedMediaBaselineIdentity:
    """Restore a non-authorizing baseline from an exact safe snapshot record."""
    selection = _validate_selection_item(selection_item)
    if (
        not isinstance(safe_baseline_identity, Mapping)
        or any(type(key) is not str for key in safe_baseline_identity)
        or set(safe_baseline_identity) != _SAFE_BASELINE_IDENTITY_FIELDS
    ):
        raise SecureSelectedMediaHandleError("invalid_safe_baseline_identity")
    value = safe_baseline_identity
    source = value.get("product_source")
    if (
        value.get("policy_version") != POLICY_VERSION
        or not isinstance(source, Mapping)
        or set(source) != {"start_row", "end_row"}
        or type(source.get("start_row")) is not int
        or type(source.get("end_row")) is not int
    ):
        raise SecureSelectedMediaHandleError("invalid_safe_baseline_identity")
    restored_source = ProductSourceRange(source["start_row"], source["end_row"])
    if (
        value.get("sku") != selection.sku
        or restored_source != selection.product_source
        or value.get("source_manifest_kind") != selection.source_manifest_kind
        or value.get("depth") != selection.depth
        or value.get("safe_folder_name") != selection.safe_folder_name
        or value.get("parent_safe_folder_name") != selection.parent_safe_folder_name
        or value.get("safe_name") != selection.safe_name
    ):
        raise SecureSelectedMediaHandleError("selected_media_baseline_provenance_mismatch")
    item = root_core.DriveManifestItem(
        safe_name=value["safe_name"],
        mime_type=value["source_mime_type"],
        size_bytes=value["size_bytes"], modified_time=None,
        md5_checksum=value["md5_checksum"],
        file_id_fingerprint=value["file_id_fingerprint"],
        item_kind="image_candidate", image_candidate=True,
        image_candidate_status="historical_snapshot_identity",
        image_width=value["image_width"], image_height=value["image_height"],
        image_rotation=None, warnings=(), provider_file_id=None,
    )
    try:
        _validate_item_shape(item)
        baseline = SelectedMediaBaselineIdentity(
            _BASELINE_CAPABILITY, selection_item=selection, source_item=item,
        )
        return _validate_baseline_identity(baseline, selection)
    except (SecureSelectedMediaHandleError, TypeError, ValueError):
        raise SecureSelectedMediaHandleError("invalid_safe_baseline_identity") from None


class SecureSelectedMediaHandle:
    """Non-serializable, immutable application provenance capability."""

    __slots__ = (
        "__capability", "__selection_item", "__baseline_identity",
        "__source_manifest", "__source_item",
        "_sku", "_product_source",
        "_source_manifest_kind", "_depth", "_safe_folder_name",
        "_parent_safe_folder_name", "_safe_name", "_file_id_fingerprint",
        "_folder_role", "_selection_position", "_image_role",
        "_md5_checksum", "_source_mime_type", "_size_bytes", "_image_width", "_image_height",
        "_warnings",
    )

    def __init__(
        self,
        capability: object | None = None,
        *,
        selection_item: image_selection_policy.ImageSelectionItem | None = None,
        baseline_identity: SelectedMediaBaselineIdentity | None = None,
        source_manifest: nested_core.GoogleDriveNestedFolderManifest | depth2_core.GoogleDriveDepth2FolderManifest | None = None,
        source_item: root_core.DriveManifestItem | None = None,
    ) -> None:
        if capability is not _HANDLE_CAPABILITY:
            raise SecureSelectedMediaHandleError("secure_media_handle_factory_required")
        if (
            type(selection_item) is not image_selection_policy.ImageSelectionItem
            or type(baseline_identity) is not SelectedMediaBaselineIdentity
            or type(source_manifest) not in {
                nested_core.GoogleDriveNestedFolderManifest,
                depth2_core.GoogleDriveDepth2FolderManifest,
            }
            or type(source_item) is not root_core.DriveManifestItem
        ):
            raise SecureSelectedMediaHandleError("invalid_secure_selected_media_handle")
        object.__setattr__(self, "_SecureSelectedMediaHandle__capability", capability)
        object.__setattr__(self, "_SecureSelectedMediaHandle__selection_item", selection_item)
        object.__setattr__(self, "_SecureSelectedMediaHandle__baseline_identity", baseline_identity)
        object.__setattr__(self, "_SecureSelectedMediaHandle__source_manifest", source_manifest)
        object.__setattr__(self, "_SecureSelectedMediaHandle__source_item", source_item)
        object.__setattr__(self, "_sku", selection_item.sku)
        object.__setattr__(self, "_product_source", selection_item.product_source)
        object.__setattr__(self, "_source_manifest_kind", selection_item.source_manifest_kind)
        object.__setattr__(self, "_depth", selection_item.depth)
        object.__setattr__(self, "_safe_folder_name", selection_item.safe_folder_name)
        object.__setattr__(self, "_parent_safe_folder_name", selection_item.parent_safe_folder_name)
        object.__setattr__(self, "_safe_name", selection_item.safe_name)
        object.__setattr__(self, "_file_id_fingerprint", source_item.file_id_fingerprint)
        object.__setattr__(self, "_md5_checksum", source_item.md5_checksum)
        object.__setattr__(self, "_folder_role", selection_item.folder_role)
        object.__setattr__(self, "_selection_position", selection_item.selection_position)
        object.__setattr__(self, "_image_role", selection_item.image_role)
        object.__setattr__(self, "_source_mime_type", source_item.mime_type)
        object.__setattr__(self, "_size_bytes", source_item.size_bytes)
        object.__setattr__(self, "_image_width", source_item.image_width)
        object.__setattr__(self, "_image_height", source_item.image_height)
        object.__setattr__(self, "_warnings", tuple(dict.fromkeys((
            *selection_item.warnings, *source_manifest.warnings, *source_item.warnings,
        ))))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("secure_media_handle_is_immutable")

    @property
    def policy_version(self) -> str:
        return POLICY_VERSION

    @property
    def sku(self) -> str:
        return self._sku

    @property
    def product_source(self) -> ProductSourceRange:
        return self._product_source

    @property
    def source_manifest_kind(self) -> str:
        return self._source_manifest_kind

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def safe_folder_name(self) -> str:
        return self._safe_folder_name

    @property
    def parent_safe_folder_name(self) -> str | None:
        return self._parent_safe_folder_name

    @property
    def safe_name(self) -> str:
        return self._safe_name

    @property
    def file_id_fingerprint(self) -> str:
        return self._file_id_fingerprint

    @property
    def md5_checksum(self) -> str:
        return self._md5_checksum

    @property
    def folder_role(self) -> folder_role_policy.FolderRole:
        return self._folder_role

    @property
    def selection_position(self) -> int:
        return self._selection_position

    @property
    def image_role(self) -> image_selection_policy.ImageSelectionRole:
        return self._image_role

    @property
    def source_mime_type(self) -> str:
        return self._source_mime_type

    @property
    def size_bytes(self) -> int | None:
        return self._size_bytes

    @property
    def image_width(self) -> int | None:
        return self._image_width

    @property
    def image_height(self) -> int | None:
        return self._image_height

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "policy_version": POLICY_VERSION, "sku": self.sku,
            "product_source": self.product_source.to_dict(),
            "source_manifest_kind": self.source_manifest_kind, "depth": self.depth,
            "safe_folder_name": self.safe_folder_name,
            "parent_safe_folder_name": self.parent_safe_folder_name,
            "safe_name": self.safe_name,
            "file_id_fingerprint": self.file_id_fingerprint,
            "md5_checksum": self.md5_checksum,
            "folder_role": self.folder_role.value,
            "selection_position": self.selection_position,
            "image_role": self.image_role.value,
            "source_mime_type": self.source_mime_type,
            "size_bytes": self.size_bytes, "image_width": self.image_width,
            "image_height": self.image_height, "warnings": list(self.warnings),
        }

    def __repr__(self) -> str:
        return f"SecureSelectedMediaHandle({self.to_safe_dict()!r})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("secure_media_handle_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("secure_media_handle_not_serializable")


def _valid_handle(handle: object) -> bool:
    if type(handle) is not SecureSelectedMediaHandle:
        return False
    try:
        capability = object.__getattribute__(handle, "_SecureSelectedMediaHandle__capability")
        selection = object.__getattribute__(handle, "_SecureSelectedMediaHandle__selection_item")
        baseline = object.__getattribute__(handle, "_SecureSelectedMediaHandle__baseline_identity")
        manifest = object.__getattribute__(handle, "_SecureSelectedMediaHandle__source_manifest")
        source_item = object.__getattribute__(handle, "_SecureSelectedMediaHandle__source_item")
        safe = handle.to_safe_dict()
    except (AttributeError, TypeError, ValueError):
        return False
    if capability is not _HANDLE_CAPABILITY or type(source_item) is not root_core.DriveManifestItem:
        return False
    try:
        verified_selection = _validate_selection_item(selection)
        verified_baseline = _validate_baseline_identity(baseline, verified_selection)
        kind, depth, folder, parent = _validate_manifest(verified_selection, manifest)
        verified_source = _exact_source_item(verified_selection, manifest)
    except SecureSelectedMediaHandleError:
        return False
    if verified_source is not source_item:
        return False
    provider_id = source_item.provider_file_id
    expected_warnings = tuple(dict.fromkeys((
        *selection.warnings, *manifest.warnings, *source_item.warnings,
    )))
    return (
        root_core.is_valid_drive_id(provider_id)
        and root_core.fingerprint_drive_id(provider_id) == source_item.file_id_fingerprint
        and safe["policy_version"] == POLICY_VERSION
        and safe["sku"] == selection.sku == manifest.sku
        and safe["product_source"] == selection.product_source.to_dict() == manifest.product_source.to_dict()
        and safe["source_manifest_kind"] == kind == selection.source_manifest_kind
        and safe["depth"] == depth == selection.depth
        and safe["safe_folder_name"] == folder == selection.safe_folder_name
        and safe["parent_safe_folder_name"] == parent == selection.parent_safe_folder_name
        and safe["file_id_fingerprint"] == source_item.file_id_fingerprint
        and safe["file_id_fingerprint"] == verified_baseline.file_id_fingerprint
        and safe["md5_checksum"] == source_item.md5_checksum
        and safe["md5_checksum"] == verified_baseline.md5_checksum
        and safe["safe_name"] == selection.safe_name == source_item.safe_name
        and safe["folder_role"] == selection.folder_role.value
        and safe["selection_position"] == selection.selection_position
        and safe["image_role"] == selection.image_role.value
        and safe["source_mime_type"] == source_item.mime_type == verified_baseline.source_mime_type
        and safe["size_bytes"] == source_item.size_bytes == verified_baseline.size_bytes
        and safe["image_width"] == source_item.image_width == verified_baseline.image_width
        and safe["image_height"] == source_item.image_height == verified_baseline.image_height
        and safe["warnings"] == list(expected_warnings)
        and not any(provider_id in value for value in _public_strings(safe))
    )


def create_secure_selected_media_handle(
    selection_item: image_selection_policy.ImageSelectionItem,
    baseline_identity: SelectedMediaBaselineIdentity,
    source_manifest: nested_core.GoogleDriveNestedFolderManifest | depth2_core.GoogleDriveDepth2FolderManifest,
) -> SecureSelectedMediaHandle:
    """Create a capability from exact selected and fresh manifest provenance."""
    selection = _validate_selection_item(selection_item)
    baseline = _validate_baseline_identity(baseline_identity, selection)
    kind, depth, folder, parent = _validate_manifest(selection, source_manifest)
    if (
        selection.sku != source_manifest.sku
        or selection.product_source != source_manifest.product_source
        or selection.source_manifest_kind != kind or selection.depth != depth
        or selection.safe_folder_name != folder
        or selection.parent_safe_folder_name != parent
    ):
        raise SecureSelectedMediaHandleError("selected_media_provenance_mismatch")
    source_item = _exact_source_item(selection, source_manifest)
    provider_id = source_item.provider_file_id
    if type(source_item.file_id_fingerprint) is not str or root_core._SHA256_PATTERN.fullmatch(source_item.file_id_fingerprint) is None:
        raise SecureSelectedMediaHandleError("fresh_selected_media_fingerprint_missing")
    if (
        not root_core.is_valid_drive_id(provider_id)
        or root_core.fingerprint_drive_id(provider_id) != source_item.file_id_fingerprint
    ):
        raise SecureSelectedMediaHandleError("invalid_provider_file_identity")
    if source_item.file_id_fingerprint != baseline.file_id_fingerprint:
        raise SecureSelectedMediaHandleError("selected_media_file_identity_changed")
    if type(source_item.md5_checksum) is not str or root_core._MD5_PATTERN.fullmatch(source_item.md5_checksum) is None:
        raise SecureSelectedMediaHandleError("fresh_selected_media_checksum_missing")
    if source_item.md5_checksum != baseline.md5_checksum:
        raise SecureSelectedMediaHandleError("selected_media_content_changed")
    if (
        source_item.mime_type != baseline.source_mime_type
        or source_item.size_bytes != baseline.size_bytes
        or source_item.image_width != baseline.image_width
        or source_item.image_height != baseline.image_height
    ):
        raise SecureSelectedMediaHandleError("selected_media_metadata_changed")
    handle = SecureSelectedMediaHandle(
        _HANDLE_CAPABILITY, selection_item=selection,
        baseline_identity=baseline, source_manifest=source_manifest,
        source_item=source_item,
    )
    if not _valid_handle(handle):
        raise SecureSelectedMediaHandleError("invalid_secure_selected_media_handle")
    return handle


def _provider_file_id_for_download(handle: SecureSelectedMediaHandle) -> str:
    """Package-private raw identity access for a future download Core only."""
    if not _valid_handle(handle):
        raise SecureSelectedMediaHandleError("invalid_secure_selected_media_handle")
    source_item = object.__getattribute__(handle, "_SecureSelectedMediaHandle__source_item")
    return source_item.provider_file_id
