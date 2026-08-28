"""Pure, metadata-only asset typing, independent of discovery and selection.

Storefront eligibility here is only a MIME policy decision, not verification of
file contents, site media support, image quality or final gallery eligibility.
Folder roles, SKU, size and dimensions are audit-only. No I/O is performed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from .sanitization import REPORT_SECRET_SCAN_PATTERN_TEXT, Redactor


POLICY_VERSION = "xxxxdoll-image-asset-type-v1"


class AssetClass(StrEnum):
    WEB_IMAGE = "web_image"
    DESIGN_SOURCE = "design_source"
    VIDEO = "video"
    OTHER_MEDIA = "other_media"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


ClassificationSource = Literal["mime", "extension_fallback", "unknown"]
ClassificationStatus = Literal[
    "metadata_web_image", "metadata_classified", "extension_fallback_candidate", "unknown",
]


class ImageAssetTypePolicyError(ValueError):
    """Fixed validation codes only, without echoing input metadata."""


_STOREFRONT_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_PLATFORM_VERIFICATION_MIMES = frozenset({"image/gif", "image/avif"})
_DESIGN_SOURCE_MIMES = frozenset({"image/vnd.adobe.photoshop", "application/postscript"})
_GENERIC_MIME = "application/octet-stream"
# Strict, concrete ASCII type/subtype only. No parameters, wildcard, fuzzy
# spelling repair, Unicode lookalike conversion or whitespace inside the MIME.
_MIME_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.ASCII)
_EXTENSION_PATTERN = re.compile(r"[a-z0-9]{1,12}", re.ASCII)
# Explicit audit pairs; do not consult OS mimetypes registries or file contents.
_EXTENSION_MIMES = {
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".png": frozenset({"image/png"}),
    ".webp": frozenset({"image/webp"}),
    ".gif": frozenset({"image/gif"}),
    ".avif": frozenset({"image/avif"}),
    ".psd": frozenset({"image/vnd.adobe.photoshop"}),
    ".eps": frozenset({"application/postscript"}),
    ".ps": frozenset({"application/postscript"}),
    ".mp4": frozenset({"video/mp4", "audio/mp4"}),
    ".webm": frozenset({"video/webm", "audio/webm"}),
    ".pdf": frozenset({"application/pdf"}),
}
_EXTENSION_FALLBACK_CLASSES = {
    ".jpg": AssetClass.WEB_IMAGE,
    ".jpeg": AssetClass.WEB_IMAGE,
    ".png": AssetClass.WEB_IMAGE,
    ".webp": AssetClass.WEB_IMAGE,
    ".psd": AssetClass.DESIGN_SOURCE,
    ".mp4": AssetClass.VIDEO,
}
_SECRET_PATTERN = re.compile(REPORT_SECRET_SCAN_PATTERN_TEXT, re.IGNORECASE)
_UNSAFE_TEXT_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://|\bdrive\.google\.com\b|\bwww\."
    r"|-----BEGIN [^-]*PRIVATE KEY-----"
    r"|\b(?:resource[_ ]?key|provider[_ ]file[_ ]id|raw[_ ](?:file|folder)[_ ]id)\s*[:=]",
    re.IGNORECASE,
)


def _validate_safe_text(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ImageAssetTypePolicyError(f"invalid_{field_name}")
    canonical = unicodedata.normalize("NFKC", value)
    if (
        _SECRET_PATTERN.search(canonical)
        or _UNSAFE_TEXT_PATTERN.search(canonical)
        or Redactor().text(canonical, limit=len(canonical) + 1) != canonical
        or any(unicodedata.category(char) == "Cc" and not char.isspace() for char in canonical)
    ):
        raise ImageAssetTypePolicyError(f"unsafe_{field_name}")


def _normalize_mime_type(mime_type: str | None) -> str | None:
    # Malformed values are nonblocking unknowns, not extension fallback input.
    # Do not retain invalid raw MIME strings, which may contain unsafe text.
    if not isinstance(mime_type, str):
        return None
    candidate = mime_type.strip().lower()
    if not _MIME_PATTERN.fullmatch(candidate):
        return None
    _validate_safe_text(candidate, "mime_type")
    return candidate


def _safe_extension(safe_name: str) -> str | None:
    # The input is a safe basename, never a path or URL to resolve/open.
    canonical = unicodedata.normalize("NFKC", safe_name)
    if any(char in canonical for char in ("/", "\\", ":")):
        raise ImageAssetTypePolicyError("unsafe_safe_name")
    stem, dot, suffix = safe_name.strip().rpartition(".")
    if not stem or not dot or not _EXTENSION_PATTERN.fullmatch(suffix.lower()):
        return None
    return "." + suffix.lower()


def _validate_audit_number(value: int | None, field_name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ImageAssetTypePolicyError(f"invalid_{field_name}")


@dataclass(frozen=True, slots=True)
class ImageAssetTypeResult:
    asset_class: AssetClass
    normalized_mime_type: str | None
    safe_extension: str | None
    storefront_eligible: bool
    classification_source: ClassificationSource
    status: ClassificationStatus
    safe_name: str
    size_bytes: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    sku: str | None = None
    folder_role: str | None = None
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    def to_dict(self) -> dict[str, object]:
        """Deterministic, JSON-safe metadata audit; not a media verification."""
        return {
            "asset_class": self.asset_class.value,
            "policy_version": self.policy_version,
            "normalized_mime_type": self.normalized_mime_type,
            "safe_extension": self.safe_extension,
            "storefront_eligible": self.storefront_eligible,
            "classification_source": self.classification_source,
            "status": self.status,
            "safe_name": self.safe_name,
            "size_bytes": self.size_bytes,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "sku": self.sku,
            "folder_role": self.folder_role,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


def classify_image_asset_type(
    mime_type: str | None,
    safe_name: str,
    *,
    size_bytes: int | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    sku: str | None = None,
    folder_role: str | None = None,
) -> ImageAssetTypeResult:
    """Classify allowlisted metadata without accessing a provider or file.

    Only JPEG/PNG/WebP MIME metadata grants type-level storefront eligibility.
    Missing MIME and octet-stream allow a closed extension fallback, never an
    approval. Malformed MIME does not enable fallback. All audit dimensions,
    including zero, leave classification and eligibility unchanged. No raw ID,
    fingerprint, URL, path, content, gateway or client argument is accepted.
    """
    _validate_safe_text(safe_name, "safe_name")
    for field_name, value in (("sku", sku), ("folder_role", folder_role)):
        if value is not None:
            _validate_safe_text(value, field_name)
    for field_name, value in (("size_bytes", size_bytes), ("image_width", image_width), ("image_height", image_height)):
        _validate_audit_number(value, field_name)
    extension = _safe_extension(safe_name)
    normalized = _normalize_mime_type(mime_type)
    missing = mime_type is None or (isinstance(mime_type, str) and not mime_type.strip())
    warnings = []
    asset_class = AssetClass.UNKNOWN
    source: ClassificationSource = "unknown"
    status: ClassificationStatus = "unknown"
    eligible = False

    if normalized is None:
        warnings.append("asset_mime_unknown")
    else:
        source, status = "mime", "metadata_classified"
        if normalized in _STOREFRONT_MIMES | _PLATFORM_VERIFICATION_MIMES:
            asset_class, status = AssetClass.WEB_IMAGE, "metadata_web_image"
            eligible = normalized in _STOREFRONT_MIMES
            if normalized in _PLATFORM_VERIFICATION_MIMES:
                warnings.append("web_image_format_requires_platform_verification")
        elif normalized in _DESIGN_SOURCE_MIMES:
            asset_class = AssetClass.DESIGN_SOURCE
        elif normalized.startswith("video/"):
            asset_class = AssetClass.VIDEO
        elif normalized.startswith("audio/") or normalized == "application/pdf":
            asset_class = AssetClass.OTHER_MEDIA
        else:
            asset_class = AssetClass.UNSUPPORTED

    if (missing or normalized == _GENERIC_MIME) and extension in _EXTENSION_FALLBACK_CLASSES:
        asset_class = _EXTENSION_FALLBACK_CLASSES[extension]
        source, status = "extension_fallback", "extension_fallback_candidate"
        eligible = False
        warnings.append("mime_verification_required")
    elif asset_class == AssetClass.UNSUPPORTED:
        warnings.append("asset_type_unsupported")

    expected_mimes = _EXTENSION_MIMES.get(extension)
    if normalized is not None and normalized != _GENERIC_MIME and expected_mimes and normalized not in expected_mimes:
        warnings.append("asset_extension_mime_mismatch")

    return ImageAssetTypeResult(
        asset_class=asset_class, normalized_mime_type=normalized,
        safe_extension=extension, storefront_eligible=eligible,
        classification_source=source, status=status, safe_name=safe_name,
        size_bytes=size_bytes, image_width=image_width, image_height=image_height,
        sku=sku, folder_role=folder_role, warnings=tuple(warnings),
    )
