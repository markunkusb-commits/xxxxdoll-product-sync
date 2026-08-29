"""Pure deterministic image selection planning for quality-qualified sources.

Quality is a gate, never a ranking score.  This policy groups safe candidates
by SKU, prioritizes storefront photos, fills from factory photos only when
needed, and produces a bounded primary/gallery plan.  It performs no I/O,
media inspection, conversion, provider access or upload operation.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from . import folder_role_policy, image_quality_policy
from .image_mapping import ProductSourceRange
from .unified_image_eligibility_policy import (
    UnifiedImageEligibilityPolicyError,
    _safe_issues,
)


POLICY_VERSION = "xxxxdoll-image-selection-v1"
MAX_IMAGES_PER_SKU = 12


class ImageSelectionRole(StrEnum):
    PRIMARY = "primary"
    GALLERY = "gallery"
    NOT_SELECTED = "not_selected"


class ImageSelectionReason(StrEnum):
    SELECTED_STOREFRONT_PRIMARY = "selected_storefront_primary"
    SELECTED_STOREFRONT_GALLERY = "selected_storefront_gallery"
    SELECTED_FACTORY_PRIMARY_FALLBACK = "selected_factory_primary_fallback"
    SELECTED_FACTORY_GALLERY_FILL = "selected_factory_gallery_fill"
    NOT_SELECTED_IMAGE_LIMIT = "not_selected_image_limit"
    NOT_SELECTED_QUALITY_INELIGIBLE = "not_selected_quality_ineligible"
    INVALID_SELECTION_CANDIDATE = "invalid_selection_candidate"


class ImageSelectionPolicyError(ValueError):
    """Fixed safe codes only; never candidate reprs or input values."""


@dataclass(frozen=True, slots=True)
class ImageSelectionCandidate:
    sku: str
    folder_role: folder_role_policy.FolderRole
    safe_name: str
    source_manifest_kind: str
    depth: int
    safe_folder_name: str | None
    parent_safe_folder_name: str | None
    quality_result: image_quality_policy.ImageQualityPolicyResult
    product_source: ProductSourceRange | None = None
    requires_deeper_inventory: bool = False


@dataclass(frozen=True, slots=True)
class ImageSelectionItem:
    sku: str
    folder_role: folder_role_policy.FolderRole | None
    safe_name: str
    source_manifest_kind: str
    depth: int
    safe_folder_name: str | None
    parent_safe_folder_name: str | None
    product_source: ProductSourceRange | None
    requires_deeper_inventory: bool
    quality_eligible: bool
    selected: bool
    selection_position: int | None
    image_role: ImageSelectionRole
    selection_reason: ImageSelectionReason
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "folder_role": self.folder_role.value if self.folder_role is not None else None,
            "safe_name": self.safe_name,
            "source_manifest_kind": self.source_manifest_kind,
            "depth": self.depth,
            "safe_folder_name": self.safe_folder_name,
            "parent_safe_folder_name": self.parent_safe_folder_name,
            "product_source": self.product_source.to_dict() if self.product_source is not None else None,
            "requires_deeper_inventory": self.requires_deeper_inventory,
            "quality_eligible": self.quality_eligible,
            "selected": self.selected,
            "selection_position": self.selection_position,
            "image_role": self.image_role.value,
            "selection_reason": self.selection_reason.value,
            "policy_version": self.policy_version,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


@dataclass(frozen=True, slots=True)
class ImageSelectionBatchResult:
    sku: str
    total_candidates: int
    quality_candidates: int
    storefront_candidates: int
    factory_candidates: int
    selected_count: int
    selected_storefront: int
    selected_factory: int
    primary_count: int
    gallery_count: int
    items: tuple[ImageSelectionItem, ...]
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "total_candidates": self.total_candidates,
            "quality_candidates": self.quality_candidates,
            "storefront_candidates": self.storefront_candidates,
            "factory_candidates": self.factory_candidates,
            "selected_count": self.selected_count,
            "selected_storefront": self.selected_storefront,
            "selected_factory": self.selected_factory,
            "primary_count": self.primary_count,
            "gallery_count": self.gallery_count,
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class _ValidatedCandidate:
    candidate: ImageSelectionCandidate
    index: int
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    invalid: bool

    @property
    def quality_pass(self) -> bool:
        quality = self.candidate.quality_result
        return (
            not self.invalid
            and quality.quality_eligible is True
            and quality.quality_reason is image_quality_policy.ImageQualityReason.QUALITY_PASS
        )


_KIND_DEPTH = {"root": 0, "nested": 1, "depth2": 2}
_SELECTABLE_ROLES = frozenset({
    folder_role_policy.FolderRole.STOREFRONT_PHOTOS,
    folder_role_policy.FolderRole.FACTORY_PHOTOS,
})
_PATH_CHARS = re.compile(r"[\\/:]")


def _safe_text(value: object, field_name: str, *, basename: bool = False, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 500:
        raise ImageSelectionPolicyError(f"invalid_{field_name}")
    try:
        folder_role_policy._validate_safe_text(value, field_name)
    except folder_role_policy.FolderRolePolicyError:
        raise ImageSelectionPolicyError(f"unsafe_{field_name}") from None
    if basename and (_PATH_CHARS.search(value) or value in {".", ".."}):
        raise ImageSelectionPolicyError(f"unsafe_{field_name}")
    return value


def natural_safe_name_key(value: str) -> tuple[tuple[int, object, int], ...]:
    """NFKC/casefold natural numeric tokens; no semantic interpretation."""
    _safe_text(value, "safe_name", basename=True)
    normalized = unicodedata.normalize("NFKC", value).casefold()
    parts = re.split(r"(\d+)", normalized)
    return tuple(
        (1, int(part), len(part)) if part.isdigit() else (0, part, len(part))
        for part in parts if part
    )


def _source_key(value: ProductSourceRange | None) -> tuple[int, int]:
    return (-1, -1) if value is None else (value.start_row, value.end_row)


def _hierarchy_key(value: _ValidatedCandidate) -> tuple[object, ...]:
    candidate = value.candidate
    normalized_name = unicodedata.normalize("NFKC", candidate.safe_name)
    return (
        natural_safe_name_key(candidate.safe_name),
        candidate.source_manifest_kind,
        candidate.depth,
        unicodedata.normalize("NFKC", candidate.safe_folder_name or "").casefold(),
        unicodedata.normalize("NFKC", candidate.parent_safe_folder_name or "").casefold(),
        *_source_key(candidate.product_source),
        normalized_name.casefold(),
        normalized_name,
        value.index,
    )


def _validate_candidate(candidate: object, index: int, expected_sku: str | None = None) -> _ValidatedCandidate:
    if type(candidate) is not ImageSelectionCandidate:
        raise ImageSelectionPolicyError("image_selection_candidate_required")
    sku = _safe_text(candidate.sku, "sku", basename=True)
    if expected_sku is not None and sku != expected_sku:
        raise ImageSelectionPolicyError("candidate_sku_mismatch")
    _safe_text(candidate.safe_name, "safe_name", basename=True)
    kind = candidate.source_manifest_kind
    if type(kind) is not str or kind not in _KIND_DEPTH or type(candidate.depth) is not int or candidate.depth != _KIND_DEPTH[kind]:
        raise ImageSelectionPolicyError("invalid_source_hierarchy")
    folder = _safe_text(candidate.safe_folder_name, "safe_folder_name", optional=kind == "root")
    parent = _safe_text(candidate.parent_safe_folder_name, "parent_safe_folder_name", optional=kind != "depth2")
    if (
        (kind == "root" and (folder is not None or parent is not None))
        or (kind == "nested" and (folder is None or parent is not None))
        or (kind == "depth2" and (folder is None or parent is None))
    ):
        raise ImageSelectionPolicyError("invalid_source_hierarchy")
    source = candidate.product_source
    if source is not None and (
        type(source) is not ProductSourceRange
        or type(source.start_row) is not int or type(source.end_row) is not int
        or source.start_row <= 0 or source.end_row < source.start_row
    ):
        raise ImageSelectionPolicyError("invalid_product_source")
    if kind != "root" and source is None:
        raise ImageSelectionPolicyError("invalid_product_source")
    if type(candidate.requires_deeper_inventory) is not bool:
        raise ImageSelectionPolicyError("invalid_deeper_inventory_flag")
    quality = candidate.quality_result
    if type(quality) is not image_quality_policy.ImageQualityPolicyResult:
        raise ImageSelectionPolicyError("image_quality_policy_result_required")

    invalid = (
        type(candidate.folder_role) is not folder_role_policy.FolderRole
        or candidate.folder_role not in _SELECTABLE_ROLES
        or type(quality.policy_version) is not str
        or quality.policy_version != image_quality_policy.POLICY_VERSION
        or type(quality.quality_eligible) is not bool
        or type(quality.quality_reason) is not image_quality_policy.ImageQualityReason
        or quality.quality_eligible != (
            quality.quality_reason is image_quality_policy.ImageQualityReason.QUALITY_PASS
        )
    )
    try:
        warnings, unsafe_warning = _safe_issues(quality.warnings, "quality", "warning")
        blockers, unsafe_blocker = _safe_issues(quality.blocking_issues, "quality", "blocker")
    except UnifiedImageEligibilityPolicyError:
        warnings, blockers = (), ()
        unsafe_warning = unsafe_blocker = True
    if unsafe_warning or unsafe_blocker:
        invalid = True
        blockers = tuple(dict.fromkeys((*blockers, "unsafe_upstream_audit")))
    if quality.quality_eligible is True and blockers:
        invalid = True
    if invalid:
        blockers = tuple(dict.fromkeys((*blockers, "invalid_selection_candidate")))
    return _ValidatedCandidate(candidate, index, warnings, blockers, invalid)


def _item(
    value: _ValidatedCandidate,
    *,
    position: int | None,
    duplicate: bool,
    factory_fallback: bool,
) -> ImageSelectionItem:
    candidate = value.candidate
    selected = position is not None
    warnings = value.warnings
    if duplicate:
        warnings = tuple(dict.fromkeys((*warnings, "duplicate_selection_name")))
    if factory_fallback and position == 0:
        warnings = tuple(dict.fromkeys((*warnings, "primary_from_factory_fallback")))

    if value.invalid:
        image_role = ImageSelectionRole.NOT_SELECTED
        reason = ImageSelectionReason.INVALID_SELECTION_CANDIDATE
    elif not value.quality_pass:
        image_role = ImageSelectionRole.NOT_SELECTED
        reason = ImageSelectionReason.NOT_SELECTED_QUALITY_INELIGIBLE
    elif not selected:
        image_role = ImageSelectionRole.NOT_SELECTED
        reason = ImageSelectionReason.NOT_SELECTED_IMAGE_LIMIT
    elif position == 0 and candidate.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS:
        image_role = ImageSelectionRole.PRIMARY
        reason = ImageSelectionReason.SELECTED_STOREFRONT_PRIMARY
    elif position == 0:
        image_role = ImageSelectionRole.PRIMARY
        reason = ImageSelectionReason.SELECTED_FACTORY_PRIMARY_FALLBACK
    elif candidate.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS:
        image_role = ImageSelectionRole.GALLERY
        reason = ImageSelectionReason.SELECTED_STOREFRONT_GALLERY
    else:
        image_role = ImageSelectionRole.GALLERY
        reason = ImageSelectionReason.SELECTED_FACTORY_GALLERY_FILL

    return ImageSelectionItem(
        sku=candidate.sku,
        folder_role=candidate.folder_role if type(candidate.folder_role) is folder_role_policy.FolderRole else None,
        safe_name=candidate.safe_name,
        source_manifest_kind=candidate.source_manifest_kind,
        depth=candidate.depth,
        safe_folder_name=candidate.safe_folder_name,
        parent_safe_folder_name=candidate.parent_safe_folder_name,
        product_source=candidate.product_source,
        requires_deeper_inventory=candidate.requires_deeper_inventory,
        quality_eligible=candidate.quality_result.quality_eligible is True,
        selected=selected and not value.invalid and value.quality_pass,
        selection_position=position if selected and not value.invalid and value.quality_pass else None,
        image_role=image_role,
        selection_reason=reason,
        warnings=warnings,
        blocking_issues=value.blockers,
    )


def select_images_for_sku(
    sku: str,
    candidates: Sequence[ImageSelectionCandidate],
) -> ImageSelectionBatchResult:
    """Select at most twelve candidates for one exact safe SKU."""
    safe_sku = _safe_text(sku, "sku", basename=True)
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
        raise ImageSelectionPolicyError("selection_candidate_sequence_required")
    validated = tuple(_validate_candidate(candidate, index, safe_sku) for index, candidate in enumerate(candidates))
    storefront = sorted(
        (item for item in validated if item.quality_pass and item.candidate.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS),
        key=_hierarchy_key,
    )
    factory = sorted(
        (item for item in validated if item.quality_pass and item.candidate.folder_role is folder_role_policy.FolderRole.FACTORY_PHOTOS),
        key=_hierarchy_key,
    )
    selected_values = [*storefront[:MAX_IMAGES_PER_SKU]]
    selected_values.extend(factory[: MAX_IMAGES_PER_SKU - len(selected_values)])
    position_by_index = {item.index: position for position, item in enumerate(selected_values)}
    selected_indices = set(position_by_index)
    remainder = sorted(
        (item for item in validated if item.index not in selected_indices),
        key=lambda item: (
            0 if item.candidate.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS else
            1 if item.candidate.folder_role is folder_role_policy.FolderRole.FACTORY_PHOTOS else 2,
            _hierarchy_key(item),
        ),
    )
    ordered = (*selected_values, *remainder)
    duplicate_names = {name for name, count in Counter(item.candidate.safe_name for item in validated).items() if count > 1}
    factory_fallback = bool(selected_values) and not storefront
    items = tuple(
        _item(
            item,
            position=position_by_index.get(item.index),
            duplicate=item.candidate.safe_name in duplicate_names,
            factory_fallback=factory_fallback,
        )
        for item in ordered
    )
    selected_items = tuple(item for item in items if item.selected)
    batch_warnings = []
    if not any(item.quality_pass for item in validated):
        batch_warnings.append("no_quality_images_available")
    if duplicate_names:
        batch_warnings.append("duplicate_selection_name")
    if factory_fallback:
        batch_warnings.append("primary_from_factory_fallback")
    batch_blockers = tuple(dict.fromkeys(
        code for item in items for code in item.blocking_issues
    ))
    primary_count = sum(item.image_role is ImageSelectionRole.PRIMARY for item in selected_items)
    gallery_count = sum(item.image_role is ImageSelectionRole.GALLERY for item in selected_items)
    # Construction guarantees one primary for every non-empty selection.
    if len(selected_items) > MAX_IMAGES_PER_SKU or primary_count > 1 or (selected_items and primary_count != 1):
        raise ImageSelectionPolicyError("selection_invariant_violation")
    return ImageSelectionBatchResult(
        sku=safe_sku,
        total_candidates=len(validated),
        quality_candidates=sum(item.quality_pass for item in validated),
        storefront_candidates=len(storefront),
        factory_candidates=len(factory),
        selected_count=len(selected_items),
        selected_storefront=sum(
            item.folder_role is folder_role_policy.FolderRole.STOREFRONT_PHOTOS for item in selected_items
        ),
        selected_factory=sum(
            item.folder_role is folder_role_policy.FolderRole.FACTORY_PHOTOS for item in selected_items
        ),
        primary_count=primary_count,
        gallery_count=gallery_count,
        items=items,
        warnings=tuple(batch_warnings),
        blocking_issues=batch_blockers,
    )


def select_images(
    candidates: Sequence[ImageSelectionCandidate],
) -> tuple[ImageSelectionBatchResult, ...]:
    """Group an immutable input sequence by exact SKU and select each group."""
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
        raise ImageSelectionPolicyError("selection_candidate_sequence_required")
    groups: defaultdict[str, list[ImageSelectionCandidate]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        validated = _validate_candidate(candidate, index)
        groups[validated.candidate.sku].append(validated.candidate)
    return tuple(
        select_images_for_sku(sku, groups[sku])
        for sku in sorted(groups, key=natural_safe_name_key)
    )
