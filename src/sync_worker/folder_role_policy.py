"""Pure, versioned folder-name semantics, without traversal or image selection.

Only safe names enter this API. Parent, depth, SKU and source are audit metadata;
they never supply or override a role. A deeper-inventory flag reports incomplete
inventory, not permission to traverse or a request to create a depth-three job.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from .image_mapping import ProductSourceRange
from .sanitization import REPORT_SECRET_SCAN_PATTERN_TEXT, Redactor


POLICY_VERSION = "xxxxdoll-folder-role-v1"


class FolderRole(StrEnum):
    STOREFRONT_PHOTOS = "storefront_photos"
    FACTORY_PHOTOS = "factory_photos"
    BANNER = "banner"
    VIDEO = "video"
    EYE_OPTIONS = "eye_options"
    PROMO_ASSETS = "promo_assets"
    OTHER_SKIN_TONE = "other_skin_tone"
    UNKNOWN = "unknown"


class FolderRolePolicyError(ValueError):
    """Fixed validation codes only; never echo a caller's input value."""


# Whole words/phrases only, in descending priority. The final photos rule is a
# prefix rule: an isolated word later in an unrelated name is not sufficient.
_RULES = (
    (FolderRole.OTHER_SKIN_TONE, "other_skin_tone_phrase", re.compile(r"(?<!\w)other skin tone(?!\w)")),
    (FolderRole.EYE_OPTIONS, "eye_options_phrase", re.compile(r"(?<!\w)eye options?(?!\w)")),
    (FolderRole.PROMO_ASSETS, "promo_assets_phrase", re.compile(r"(?<!\w)(?:promo assets?|promotional assets)(?!\w)")),
    (FolderRole.BANNER, "banner_word", re.compile(r"(?<!\w)banner(?!\w)")),
    (FolderRole.VIDEO, "video_word", re.compile(r"(?<!\w)videos?(?!\w)")),
    (FolderRole.FACTORY_PHOTOS, "factory_photos_phrase", re.compile(r"(?<!\w)factory photos?(?!\w)")),
    (FolderRole.STOREFRONT_PHOTOS, "storefront_photos_prefix", re.compile(r"^photos?(?:\s|$)")),
)
ROLE_PRIORITY = tuple(role for role, _, _ in _RULES) + (FolderRole.UNKNOWN,)
_GALLERY_ROLES = frozenset({FolderRole.STOREFRONT_PHOTOS, FolderRole.FACTORY_PHOTOS})
_SECRET_PATTERN = re.compile(REPORT_SECRET_SCAN_PATTERN_TEXT, re.IGNORECASE)
_UNSAFE_TEXT_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://|\bdrive\.google\.com\b|\bwww\."
    r"|-----BEGIN [^-]*PRIVATE KEY-----"
    r"|\b(?:private[_ ]key(?:[_ ]id)?|client[_ ]email|token[_ ]uri|"
    r"access[_ ]token|refresh[_ ]token|token|password|resource[_ ]?key)\s*[:=]",
    re.IGNORECASE,
)


def _validate_safe_text(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise FolderRolePolicyError(f"invalid_{field_name}")
    canonical = unicodedata.normalize("NFKC", value)
    if (
        _UNSAFE_TEXT_PATTERN.search(canonical)
        or _SECRET_PATTERN.search(canonical)
        or Redactor().text(canonical, limit=len(canonical) + 1) != canonical
        or any(unicodedata.category(char) == "Cc" and not char.isspace() for char in canonical)
    ):
        raise FolderRolePolicyError(f"unsafe_{field_name}")


def normalize_folder_name(safe_folder_name: str) -> str:
    """NFKC, casefold, underscore/hyphen separators and whitespace only."""

    _validate_safe_text(safe_folder_name, "safe_folder_name")
    normalized = unicodedata.normalize("NFKC", safe_folder_name).casefold()
    return " ".join(normalized.replace("_", " ").replace("-", " ").split())


@dataclass(frozen=True, slots=True)
class FolderRoleClassification:
    role: FolderRole
    normalized_folder_name: str
    matched_rule: str | None
    depth: int | None
    parent_safe_folder_name: str | None
    sku: str | None
    product_source: ProductSourceRange | None
    gallery_eligible: bool
    requires_deeper_inventory: bool
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    @property
    def storefront_gallery_eligible(self) -> bool:
        """Eligibility alias; never identifies or selects individual images."""
        return self.gallery_eligible

    def to_dict(self) -> dict[str, object]:
        """JSON-safe audit projection; no provider metadata or media fields."""
        return {
            "role": self.role.value,
            "policy_version": self.policy_version,
            "normalized_folder_name": self.normalized_folder_name,
            "matched_rule": self.matched_rule,
            "depth": self.depth,
            "parent_safe_folder_name": self.parent_safe_folder_name,
            "sku": self.sku,
            "product_source": self.product_source.to_dict() if self.product_source is not None else None,
            "gallery_eligible": self.gallery_eligible,
            "requires_deeper_inventory": self.requires_deeper_inventory,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


def classify_folder_role(
    safe_folder_name: str,
    *,
    parent_safe_folder_name: str | None = None,
    depth: int | None = None,
    sku: str | None = None,
    product_source: ProductSourceRange | None = None,
    has_depth_limit_children: bool = False,
) -> FolderRoleClassification:
    """Classify one safe folder name with no I/O or inherited role assumptions.

    No manifest, raw ID, fingerprint, image count, MIME, modified time or client
    parameter is accepted. Unknown names are nonblocking and never gallery-
    eligible. For every role, requires_deeper_inventory mirrors only the supplied
    boolean: it cannot authorize traversal, select files or schedule work.
    """

    normalized = normalize_folder_name(safe_folder_name)
    if parent_safe_folder_name is not None:
        _validate_safe_text(parent_safe_folder_name, "parent_safe_folder_name")
    if sku is not None:
        _validate_safe_text(sku, "sku")
    if depth is not None and (type(depth) is not int or depth < 0):
        raise FolderRolePolicyError("invalid_depth")
    if type(has_depth_limit_children) is not bool:
        raise FolderRolePolicyError("invalid_depth_limit_children_flag")
    if product_source is not None and (
        not isinstance(product_source, ProductSourceRange)
        or type(product_source.start_row) is not int
        or type(product_source.end_row) is not int
        or product_source.start_row <= 0
        or product_source.end_row < product_source.start_row
    ):
        raise FolderRolePolicyError("invalid_product_source")

    role = FolderRole.UNKNOWN
    matched_rule = None
    for candidate, rule_name, pattern in _RULES:
        if pattern.search(normalized):
            role, matched_rule = candidate, rule_name
            break
    return FolderRoleClassification(
        role=role, normalized_folder_name=normalized, matched_rule=matched_rule,
        depth=depth, parent_safe_folder_name=parent_safe_folder_name, sku=sku,
        product_source=product_source, gallery_eligible=role in _GALLERY_ROLES,
        requires_deeper_inventory=has_depth_limit_children,
        warnings=("folder_role_unknown",) if role is FolderRole.UNKNOWN else (),
    )
