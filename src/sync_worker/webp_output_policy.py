"""Pure WebP pipeline planning; a policy result is never upload authority.

Source eligibility permits future conversion/verification only. Even source
WebP must pass that pipeline. A future upload gate must require a separately
verified WebP artifact, not this result, its dictionary, or an upstream flag.
This module neither creates that artifact nor performs any I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from .image_asset_type_policy import AssetClass, ImageAssetTypeResult
from .sanitization import REPORT_SECRET_SCAN_PATTERN_TEXT, Redactor


POLICY_VERSION = "xxxxdoll-webp-output-v1"


class WebPAction(StrEnum):
    CONVERT_TO_WEBP = "convert_to_webp"
    VALIDATE_EXISTING_WEBP = "validate_existing_webp"
    NOT_ALLOWED = "not_allowed"


class WebPOutputPolicyError(ValueError):
    """Fixed validation codes only; never echo an input object or value."""


# These are pipeline actions, not MIME/extension classification rules. They are
# consulted only after the upstream web_image + MIME + eligibility checks pass.
_SOURCE_ACTIONS = {
    "image/jpeg": WebPAction.CONVERT_TO_WEBP,
    "image/png": WebPAction.CONVERT_TO_WEBP,
    "image/webp": WebPAction.VALIDATE_EXISTING_WEBP,
}
_INELIGIBLE_CLASSES = {
    AssetClass.DESIGN_SOURCE: "design_source_not_storefront_asset",
    AssetClass.VIDEO: "video_not_storefront_asset",
    AssetClass.OTHER_MEDIA: "other_media_not_storefront_asset",
    AssetClass.UNSUPPORTED: "unsupported_asset_not_allowed",
    AssetClass.UNKNOWN: "unknown_asset_not_allowed",
}
_ISSUE_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_SECRET_PATTERN = re.compile(REPORT_SECRET_SCAN_PATTERN_TEXT, re.IGNORECASE)
_UNSAFE_MIME_AUDIT_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://|\bdrive\.google\.com\b|\bwww\."
    r"|^[\\/]|[\\]|^[a-z]:|-----BEGIN|[\x00-\x1f\x7f]",
    re.IGNORECASE,
)


def _safe_issues(value: tuple[str, ...], kind: str) -> tuple[tuple[str, ...], bool]:
    """Retain safe upstream machine codes; replace unsafe text, never echo it."""
    if type(value) is not tuple or any(not isinstance(code, str) for code in value):
        raise WebPOutputPolicyError(f"invalid_upstream_{kind}")
    codes = []
    unsafe = False
    for code in value:
        if (
            not _ISSUE_CODE_PATTERN.fullmatch(code)
            or _SECRET_PATTERN.search(code)
            or Redactor().text(code, limit=len(code) + 1) != code
        ):
            codes.append(f"unsafe_upstream_{kind}_redacted")
            unsafe = True
        else:
            codes.append(code)
    return tuple(dict.fromkeys(codes)), unsafe


@dataclass(frozen=True, slots=True)
class WebPOutputPolicyResult:
    source_asset_class: AssetClass
    source_mime_type: str | None
    source_asset_eligible: bool
    requires_webp_pipeline: bool
    webp_action: WebPAction
    reason: str | None = None
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    @property
    def target_mime_type(self) -> Literal["image/webp"]:
        return "image/webp"

    @property
    def target_extension(self) -> Literal[".webp"]:
        return ".webp"

    @property
    def wordpress_upload_ready(self) -> Literal[False]:
        # Not an init field: constructors/replace cannot promote a policy to an
        # artifact. No setter, mark-ready method or caller-supplied override.
        return False

    def to_dict(self) -> dict[str, object]:
        """Return a safe, deterministic plan, never a verified media artifact."""
        return {
            "policy_version": self.policy_version,
            "source_asset_class": self.source_asset_class.value,
            "source_mime_type": self.source_mime_type,
            "source_asset_eligible": self.source_asset_eligible,
            "requires_webp_pipeline": self.requires_webp_pipeline,
            "webp_action": self.webp_action.value,
            "target_mime_type": self.target_mime_type,
            "target_extension": self.target_extension,
            "wordpress_upload_ready": self.wordpress_upload_ready,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


def evaluate_webp_output_policy(asset: ImageAssetTypeResult) -> WebPOutputPolicyResult:
    """Plan from upstream decisions without inspecting names, files or bytes.

    Wrong object types or malformed consumed fields raise fixed safe errors.
    Unknown/unsupported decisions fail closed. Unsafe issue text is replaced by
    safe audit markers and blocks planning. All results remain upload-ineligible.
    """
    if type(asset) is not ImageAssetTypeResult:
        raise WebPOutputPolicyError("image_asset_type_result_required")
    if not isinstance(asset.asset_class, AssetClass):
        raise WebPOutputPolicyError("invalid_upstream_asset_class")
    if type(asset.storefront_eligible) is not bool:
        raise WebPOutputPolicyError("invalid_upstream_storefront_eligible")
    if type(asset.classification_source) is not str or asset.classification_source not in {"mime", "extension_fallback", "unknown"}:
        raise WebPOutputPolicyError("invalid_upstream_classification_source")
    mime = asset.normalized_mime_type
    if mime is not None:
        if not isinstance(mime, str):
            raise WebPOutputPolicyError("invalid_upstream_mime_type")
        if (
            _UNSAFE_MIME_AUDIT_PATTERN.search(mime)
            or _SECRET_PATTERN.search(mime)
            or Redactor().text(mime, limit=len(mime) + 1) != mime
        ):
            raise WebPOutputPolicyError("unsafe_upstream_mime_type")

    warnings, unsafe_warning = _safe_issues(asset.warnings, "warning")
    blockers, unsafe_blocker = _safe_issues(asset.blocking_issues, "blocker")
    if unsafe_warning or unsafe_blocker:
        blockers = tuple(dict.fromkeys((*blockers, "unsafe_upstream_audit")))

    action = WebPAction.NOT_ALLOWED
    if blockers:
        reason = "upstream_asset_blocked"
    elif asset.asset_class in _INELIGIBLE_CLASSES:
        reason = _INELIGIBLE_CLASSES[asset.asset_class]
    elif asset.classification_source != "mime":
        reason = "mime_classification_required"
    elif not asset.storefront_eligible:
        reason = "upstream_storefront_ineligible"
    else:
        action = _SOURCE_ACTIONS.get(mime, WebPAction.NOT_ALLOWED)
        reason = None if action is not WebPAction.NOT_ALLOWED else "webp_source_mime_not_supported"

    eligible = action is not WebPAction.NOT_ALLOWED
    return WebPOutputPolicyResult(
        source_asset_class=asset.asset_class, source_mime_type=mime,
        source_asset_eligible=eligible, requires_webp_pipeline=eligible,
        webp_action=action, reason=reason, warnings=warnings, blocking_issues=blockers,
    )
