"""Pure metadata-only image quality floor for unified image candidates.

The policy consumes an existing UnifiedImageEligibilityResult plus three
integer metadata values.  It performs no file, URL, provider, media or network
I/O.  Passing this policy is only permission to continue to a future media
pipeline; it is never image selection or upload authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from . import unified_image_eligibility_policy as unified_policy


POLICY_VERSION = "xxxxdoll-image-quality-v1"
MIN_SHORT_EDGE_PX = 1600
MIN_MEGAPIXELS = 3.0

# Input-validation ceilings only.  These are deliberately far above practical
# product imagery and are not maximum-quality/resolution thresholds.
MAX_SAFE_DIMENSION_PX = 1_000_000
MAX_SAFE_SIZE_BYTES = 1_000_000_000_000


class ImageOrientation(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"
    SQUARE = "square"


class ImageQualityReason(StrEnum):
    QUALITY_PASS = "quality_pass"
    SHORT_EDGE_BELOW_MINIMUM = "short_edge_below_minimum"
    MEGAPIXELS_BELOW_MINIMUM = "megapixels_below_minimum"
    QUALITY_METADATA_MISSING = "quality_metadata_missing"
    QUALITY_METADATA_INVALID = "quality_metadata_invalid"
    UPSTREAM_IMAGE_INELIGIBLE = "upstream_image_ineligible"
    INVALID_POLICY_INPUT = "invalid_policy_input"


class ImageQualityPolicyError(ValueError):
    """Fixed safe error codes only; never caller values or object reprs."""


@dataclass(frozen=True, slots=True)
class ImageQualityPolicyResult:
    quality_eligible: bool
    quality_reason: ImageQualityReason
    image_width: int | None
    image_height: int | None
    short_edge: int | None
    long_edge: int | None
    pixel_count: int | None
    megapixels: float | None
    size_bytes: int | None
    orientation: ImageOrientation | None
    min_short_edge_px: int = field(default=MIN_SHORT_EDGE_PX, init=False)
    min_megapixels: float = field(default=MIN_MEGAPIXELS, init=False)
    warnings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    policy_version: str = field(default=POLICY_VERSION, init=False)

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic metadata audit without upload semantics."""
        return {
            "policy_version": self.policy_version,
            "quality_eligible": self.quality_eligible,
            "quality_reason": self.quality_reason.value,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "short_edge": self.short_edge,
            "long_edge": self.long_edge,
            "pixel_count": self.pixel_count,
            "megapixels": self.megapixels,
            "size_bytes": self.size_bytes,
            "orientation": self.orientation.value if self.orientation is not None else None,
            "min_short_edge_px": self.min_short_edge_px,
            "min_megapixels": self.min_megapixels,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }


def _audit_integer(value: object, ceiling: int) -> int | None:
    """Retain only bounded exact ints; bool and unsafe huge values disappear."""
    if type(value) is int and -ceiling <= value <= ceiling:
        return value
    return None


def _metadata_state(
    image_width: object,
    image_height: object,
    size_bytes: object,
) -> tuple[bool, bool]:
    values = (image_width, image_height, size_bytes)
    missing = any(value is None for value in values)
    invalid = not missing and (
        type(image_width) is not int
        or type(image_height) is not int
        or type(size_bytes) is not int
        or image_width <= 0
        or image_height <= 0
        or size_bytes <= 0
        or image_width > MAX_SAFE_DIMENSION_PX
        or image_height > MAX_SAFE_DIMENSION_PX
        or size_bytes > MAX_SAFE_SIZE_BYTES
    )
    return missing, invalid


def evaluate_image_quality(
    unified_result: unified_policy.UnifiedImageEligibilityResult,
    *,
    image_width: int | None = None,
    image_height: int | None = None,
    size_bytes: int | None = None,
) -> ImageQualityPolicyResult:
    """Evaluate one already-unified candidate from safe integer metadata.

    Stable reason precedence is: invalid upstream policy input, upstream image
    ineligible, metadata missing, metadata invalid, short edge, megapixels,
    pass.  Orientation, source byte size and deeper-inventory state never alter
    the two fixed quality gates.
    """
    if type(unified_result) is not unified_policy.UnifiedImageEligibilityResult:
        raise ImageQualityPolicyError("unified_image_eligibility_result_required")

    invalid_upstream = (
        type(unified_result.policy_version) is not str
        or unified_result.policy_version != unified_policy.POLICY_VERSION
        or type(unified_result.unified_image_eligible) is not bool
    )
    try:
        warnings, unsafe_warning = unified_policy._safe_issues(
            unified_result.warnings, "unified", "warning",
        )
        blockers, unsafe_blocker = unified_policy._safe_issues(
            unified_result.blocking_issues, "unified", "blocker",
        )
    except unified_policy.UnifiedImageEligibilityPolicyError:
        warnings, blockers = (), ()
        unsafe_warning = unsafe_blocker = True
    if unsafe_warning or unsafe_blocker:
        invalid_upstream = True
        blockers = tuple(dict.fromkeys((*blockers, "unsafe_upstream_audit")))
    # A formally eligible upstream result cannot simultaneously be blocked.
    if unified_result.unified_image_eligible is True and blockers:
        invalid_upstream = True

    missing, invalid_metadata = _metadata_state(image_width, image_height, size_bytes)
    width_audit = _audit_integer(image_width, MAX_SAFE_DIMENSION_PX)
    height_audit = _audit_integer(image_height, MAX_SAFE_DIMENSION_PX)
    size_audit = _audit_integer(size_bytes, MAX_SAFE_SIZE_BYTES)

    short_edge = long_edge = pixel_count = None
    megapixels = None
    orientation = None
    if not missing and not invalid_metadata:
        short_edge = min(image_width, image_height)
        long_edge = max(image_width, image_height)
        pixel_count = image_width * image_height
        megapixels = pixel_count / 1_000_000
        if image_width == image_height:
            orientation = ImageOrientation.SQUARE
        elif image_width < image_height:
            orientation = ImageOrientation.PORTRAIT
        else:
            orientation = ImageOrientation.LANDSCAPE

    if invalid_upstream:
        reason = ImageQualityReason.INVALID_POLICY_INPUT
    elif not unified_result.unified_image_eligible:
        reason = ImageQualityReason.UPSTREAM_IMAGE_INELIGIBLE
    elif missing:
        reason = ImageQualityReason.QUALITY_METADATA_MISSING
    elif invalid_metadata:
        reason = ImageQualityReason.QUALITY_METADATA_INVALID
    elif short_edge < MIN_SHORT_EDGE_PX:
        reason = ImageQualityReason.SHORT_EDGE_BELOW_MINIMUM
    elif megapixels < MIN_MEGAPIXELS:
        reason = ImageQualityReason.MEGAPIXELS_BELOW_MINIMUM
    else:
        reason = ImageQualityReason.QUALITY_PASS

    if reason in {
        ImageQualityReason.INVALID_POLICY_INPUT,
        ImageQualityReason.QUALITY_METADATA_MISSING,
        ImageQualityReason.QUALITY_METADATA_INVALID,
    }:
        blockers = tuple(dict.fromkeys((*blockers, reason.value)))

    return ImageQualityPolicyResult(
        quality_eligible=reason is ImageQualityReason.QUALITY_PASS,
        quality_reason=reason,
        image_width=width_audit,
        image_height=height_audit,
        short_edge=short_edge,
        long_edge=long_edge,
        pixel_count=pixel_count,
        megapixels=megapixels,
        size_bytes=size_audit,
        orientation=orientation,
        warnings=warnings,
        blocking_issues=blockers,
    )
