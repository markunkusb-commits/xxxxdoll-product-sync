"""Pure source-candidate eligibility, never media verification or upload authority.

Consume the existing FolderRoleClassification (the folder-role domain result)
and WebPOutputPolicyResult. Names, MIME typing, quality, selection and traversal
remain outside this layer. No I/O or upstream policy evaluation is performed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from .folder_role_policy import (
    POLICY_VERSION as FOLDER_POLICY_VERSION,
    FolderRole,
    FolderRoleClassification,
)
from .sanitization import REPORT_SECRET_SCAN_PATTERN_TEXT, Redactor
from .webp_output_policy import (
    POLICY_VERSION as WEBP_POLICY_VERSION,
    WebPAction,
    WebPOutputPolicyResult,
)


POLICY_VERSION = "xxxxdoll-unified-image-eligibility-v1"


class EligibilityReason(StrEnum):
    ELIGIBLE_STOREFRONT_PHOTO = "eligible_storefront_photo"
    ELIGIBLE_FACTORY_PHOTO = "eligible_factory_photo"
    FOLDER_ROLE_NOT_GALLERY_ELIGIBLE = "folder_role_not_gallery_eligible"
    FOLDER_ROLE_UNKNOWN = "folder_role_unknown"
    SOURCE_ASSET_NOT_WEBP_ELIGIBLE = "source_asset_not_webp_eligible"
    WEBP_PIPELINE_NOT_REQUIRED = "webp_pipeline_not_required"
    INVALID_WEBP_ACTION = "invalid_webp_action"
    INVALID_WEBP_TARGET = "invalid_webp_target"
    UPSTREAM_BLOCKED = "upstream_blocked"
    MISSING_FOLDER_ROLE = "missing_folder_role"
    INVALID_POLICY_INPUT = "invalid_policy_input"


class UnifiedImageEligibilityPolicyError(ValueError):
    """Fixed codes only, without echoing untrusted objects, values or paths."""


# An explicit business-role allowlist, not folder-name classification rules.
_ELIGIBLE_ROLES = {
    FolderRole.STOREFRONT_PHOTOS: EligibilityReason.ELIGIBLE_STOREFRONT_PHOTO,
    FolderRole.FACTORY_PHOTOS: EligibilityReason.ELIGIBLE_FACTORY_PHOTO,
}
_PIPELINE_ACTIONS = frozenset({WebPAction.CONVERT_TO_WEBP, WebPAction.VALIDATE_EXISTING_WEBP})
_ISSUE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}", re.ASCII)
_SECRET_PATTERN = re.compile(REPORT_SECRET_SCAN_PATTERN_TEXT, re.IGNORECASE)
_CREDENTIAL_CODE = re.compile(
    r"(?:^|_)(?:token|secret|password|credential|credentials|private_key|private_key_id|client_email)(?:_|$)"
)


def _safe_issues(value: tuple[str, ...], source: str, kind: str) -> tuple[tuple[str, ...], bool]:
    """Keep safe machine codes; redact unsafe text and flag a blocking audit."""
    if type(value) is not tuple or any(type(code) is not str for code in value):
        raise UnifiedImageEligibilityPolicyError("invalid_policy_input")
    result = []
    unsafe = False
    for code in value:
        if (
            not _ISSUE_CODE.fullmatch(code)
            or _SECRET_PATTERN.search(code)
            or _CREDENTIAL_CODE.search(code)
            or Redactor().text(code, limit=len(code) + 1) != code
        ):
            result.append(f"unsafe_{source}_{kind}_redacted")
            unsafe = True
        else:
            result.append(code)
    return tuple(dict.fromkeys(result)), unsafe


@dataclass(frozen=True, slots=True)
class UnifiedImageEligibilityResult:
    folder_role: FolderRole | None
    folder_role_policy_version: str | None
    webp_policy_version: str | None
    folder_gallery_eligible: bool
    source_asset_eligible: bool
    requires_webp_pipeline: bool
    webp_action: WebPAction | None
    unified_image_eligible: bool
    eligibility_reason: EligibilityReason
    requires_deeper_inventory: bool
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    @property
    def target_mime_type(self) -> Literal["image/webp"]:
        return "image/webp"

    @property
    def target_extension(self) -> Literal[".webp"]:
        return ".webp"

    def to_dict(self) -> dict[str, object]:
        """Safe candidate audit only; deliberately no upload-authority field."""
        return {
            "policy_version": self.policy_version,
            "folder_role": self.folder_role.value if self.folder_role is not None else None,
            "folder_role_policy_version": self.folder_role_policy_version,
            "webp_policy_version": self.webp_policy_version,
            "folder_gallery_eligible": self.folder_gallery_eligible,
            "source_asset_eligible": self.source_asset_eligible,
            "requires_webp_pipeline": self.requires_webp_pipeline,
            "webp_action": self.webp_action.value if self.webp_action is not None else None,
            "target_mime_type": self.target_mime_type,
            "target_extension": self.target_extension,
            "unified_image_eligible": self.unified_image_eligible,
            "eligibility_reason": self.eligibility_reason.value,
            "requires_deeper_inventory": self.requires_deeper_inventory,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


def evaluate_unified_image_eligibility(
    folder_role: FolderRoleClassification | None,
    webp_result: WebPOutputPolicyResult,
) -> UnifiedImageEligibilityResult:
    """Intersect the explicit gallery-role and WebP source decisions.

    Wrong domain object types raise fixed safe errors. Missing folder context
    and malformed consumed fields fail closed in the result. Only known policy
    versions are accepted. Unused names, MIME, dimensions and upload readiness
    are neither inspected nor projected. Upstream booleans remain audit values:
    a Banner may retain source_asset_eligible=True while unified eligibility is
    False. Deeper inventory is informational and never triggers traversal.
    """
    if folder_role is not None and type(folder_role) is not FolderRoleClassification:
        raise UnifiedImageEligibilityPolicyError("folder_role_result_required")
    if type(webp_result) is not WebPOutputPolicyResult:
        raise UnifiedImageEligibilityPolicyError("webp_output_policy_result_required")

    invalid_input = False
    role = None
    folder_version = None
    gallery = False
    deeper = False
    warnings: list[str] = []
    blockers: list[str] = []
    sources = [("webp", webp_result)]
    if folder_role is not None:
        role = folder_role.role if type(folder_role.role) is FolderRole else None
        folder_version = FOLDER_POLICY_VERSION if type(folder_role.policy_version) is str and folder_role.policy_version == FOLDER_POLICY_VERSION else None
        invalid_input = role is None or folder_version is None or any(
            type(flag) is not bool for flag in (folder_role.gallery_eligible, folder_role.requires_deeper_inventory)
        )
        gallery = folder_role.gallery_eligible is True
        deeper = folder_role.requires_deeper_inventory is True
        sources.insert(0, ("folder", folder_role))

    webp_version = WEBP_POLICY_VERSION if type(webp_result.policy_version) is str and webp_result.policy_version == WEBP_POLICY_VERSION else None
    invalid_input = invalid_input or webp_version is None or any(
        type(flag) is not bool for flag in (webp_result.source_asset_eligible, webp_result.requires_webp_pipeline)
    )
    source_eligible = webp_result.source_asset_eligible is True
    pipeline = webp_result.requires_webp_pipeline is True
    action = webp_result.webp_action if type(webp_result.webp_action) is WebPAction else None
    target_valid = (
        type(webp_result.target_mime_type) is str and webp_result.target_mime_type == "image/webp"
        and type(webp_result.target_extension) is str and webp_result.target_extension == ".webp"
    )
    for label, upstream in sources:
        for kind, values, destination in (
            ("warning", upstream.warnings, warnings),
            ("blocker", upstream.blocking_issues, blockers),
        ):
            try:
                codes, unsafe = _safe_issues(values, label, kind)
            except UnifiedImageEligibilityPolicyError:
                invalid_input = True
                continue
            destination.extend(codes)
            if unsafe:
                blockers.append("unsafe_upstream_audit")

    if deeper:
        warnings.append("folder_inventory_incomplete")
    if role is FolderRole.UNKNOWN:
        warnings.append("folder_role_unknown")

    # Stable precedence for simultaneous failures; no dynamic reason strings.
    if invalid_input:
        reason = EligibilityReason.INVALID_POLICY_INPUT
    elif folder_role is None:
        reason = EligibilityReason.MISSING_FOLDER_ROLE
    elif blockers:
        reason = EligibilityReason.UPSTREAM_BLOCKED
    elif role is FolderRole.UNKNOWN:
        reason = EligibilityReason.FOLDER_ROLE_UNKNOWN
    elif not gallery or role not in _ELIGIBLE_ROLES:
        reason = EligibilityReason.FOLDER_ROLE_NOT_GALLERY_ELIGIBLE
    elif not source_eligible:
        reason = EligibilityReason.SOURCE_ASSET_NOT_WEBP_ELIGIBLE
    elif not pipeline:
        reason = EligibilityReason.WEBP_PIPELINE_NOT_REQUIRED
    elif action not in _PIPELINE_ACTIONS:
        reason = EligibilityReason.INVALID_WEBP_ACTION
    elif not target_valid:
        reason = EligibilityReason.INVALID_WEBP_TARGET
    else:
        reason = _ELIGIBLE_ROLES[role]
    if reason in {EligibilityReason.INVALID_POLICY_INPUT, EligibilityReason.INVALID_WEBP_ACTION, EligibilityReason.INVALID_WEBP_TARGET}:
        blockers.append(reason.value)

    return UnifiedImageEligibilityResult(
        folder_role=role, folder_role_policy_version=folder_version,
        webp_policy_version=webp_version, folder_gallery_eligible=gallery,
        source_asset_eligible=source_eligible, requires_webp_pipeline=pipeline,
        webp_action=action, unified_image_eligible=reason in _ELIGIBLE_ROLES.values(),
        eligibility_reason=reason, requires_deeper_inventory=deeper,
        warnings=tuple(dict.fromkeys(warnings)), blocking_issues=tuple(dict.fromkeys(blockers)),
    )
