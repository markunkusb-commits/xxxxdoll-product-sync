"""Environment-scoped, human-approved Woo category bindings.

Staging Woo IDs live in this profile layer, never in the environment-neutral
internal Category Registry.  Verification is pure local and requires an exact
environment, host, category ID, and approved discovery name match.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

from .category_mapping import (
    CATEGORY_REGISTRY_VERSION,
    INTERNAL_CATEGORY_DEFINITIONS,
)
from .woocommerce_category_discovery import WooCategoryRecord


STAGING_BINDING_PROFILE_VERSION = "xxxxdoll-staging-category-bind-v1"
STAGING_ENVIRONMENT = "staging"
STAGING_EXPECTED_HOST = "staging-1d07-owenau512-iqjhz.wpcomstaging.com"

BindingStatus = Literal[
    "bound_verified",
    "unbound_category",
    "binding_target_missing",
    "binding_target_changed",
]
ProfileVerificationStatus = Literal["verified", "blocked"]

_INTERNAL_CATEGORY_KEYS = tuple(
    definition.category_key for definition in INTERNAL_CATEGORY_DEFINITIONS
)
_APPROVED_INTERNAL_CATEGORY_KEYS = frozenset(_INTERNAL_CATEGORY_KEYS)


class WooCategoryBindingProfileError(ValueError):
    """Raised for an invalid approved profile definition or discovery input."""


class InvalidProfileWooCategoryIdError(WooCategoryBindingProfileError):
    """Raised when a profile Woo ID is not a positive integer."""


class ProfileBindingConflictError(WooCategoryBindingProfileError):
    """Raised when one internal category is assigned conflicting targets."""


class DiscoveryRecordConflictError(WooCategoryBindingProfileError):
    """Raised when verification input contains duplicate Woo IDs."""


@dataclass(frozen=True, slots=True)
class ApprovedWooCategoryBinding:
    internal_category_key: str
    woo_category_id: int
    expected_name: str


@dataclass(frozen=True, slots=True)
class WooCategoryBindingProfile:
    profile_version: str
    environment: str
    expected_host: str
    bindings: tuple[ApprovedWooCategoryBinding, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_version or not isinstance(self.profile_version, str):
            raise WooCategoryBindingProfileError("profile_version must be text")
        if not self.environment or not isinstance(self.environment, str):
            raise WooCategoryBindingProfileError("environment must be text")
        if not self.expected_host or not isinstance(self.expected_host, str):
            raise WooCategoryBindingProfileError("expected_host must be text")
        if not isinstance(self.bindings, tuple):
            raise WooCategoryBindingProfileError("bindings must be a tuple")

        by_internal_key: dict[str, ApprovedWooCategoryBinding] = {}
        woo_id_to_keys: dict[int, list[str]] = {}
        stable_bindings: list[ApprovedWooCategoryBinding] = []
        profile_warnings = list(self.warnings)
        for binding in self.bindings:
            if not isinstance(binding, ApprovedWooCategoryBinding):
                raise WooCategoryBindingProfileError(
                    "bindings must contain ApprovedWooCategoryBinding values"
                )
            if binding.internal_category_key not in _APPROVED_INTERNAL_CATEGORY_KEYS:
                raise WooCategoryBindingProfileError(
                    "binding references an unknown internal category"
                )
            if type(binding.woo_category_id) is not int or binding.woo_category_id <= 0:
                raise InvalidProfileWooCategoryIdError(
                    "woo_category_id must be a positive integer"
                )
            if not isinstance(binding.expected_name, str) or not binding.expected_name:
                raise WooCategoryBindingProfileError(
                    "expected_name must be non-empty text"
                )
            existing = by_internal_key.get(binding.internal_category_key)
            if existing is not None and existing != binding:
                raise ProfileBindingConflictError(
                    "one internal category cannot bind to different Woo targets"
                )
            if existing is None:
                by_internal_key[binding.internal_category_key] = binding
                stable_bindings.append(binding)
                woo_id_to_keys.setdefault(binding.woo_category_id, []).append(
                    binding.internal_category_key
                )

        if any(len(keys) > 1 for keys in woo_id_to_keys.values()):
            profile_warnings.append("shared_woo_category_id_requires_review")
        object.__setattr__(self, "bindings", tuple(stable_bindings))
        object.__setattr__(
            self,
            "warnings",
            tuple(dict.fromkeys(profile_warnings)),
        )

    def binding_for(
        self, internal_category_key: str
    ) -> ApprovedWooCategoryBinding | None:
        return next(
            (
                binding
                for binding in self.bindings
                if binding.internal_category_key == internal_category_key
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class WooCategoryBindingResult:
    internal_category_key: str
    status: BindingStatus
    woo_category_id: int | None
    expected_name: str | None
    discovered_name: str | None
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WooCategoryBindingSummary:
    total_internal_categories: int
    bound_verified: int
    unbound_categories: int
    missing_targets: int
    changed_targets: int
    blocking_bindings: int


@dataclass(frozen=True, slots=True)
class WooCategoryBindingVerification:
    status: ProfileVerificationStatus
    profile_version: str
    registry_version: str
    environment: str
    hostname: str
    expected_host: str
    results: tuple[WooCategoryBindingResult, ...]
    summary: WooCategoryBindingSummary
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    network_requests_performed: int = 0
    write_requests_performed: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def staging_category_binding_profile() -> WooCategoryBindingProfile:
    """Return the explicitly approved staging-only binding profile."""

    return WooCategoryBindingProfile(
        profile_version=STAGING_BINDING_PROFILE_VERSION,
        environment=STAGING_ENVIRONMENT,
        expected_host=STAGING_EXPECTED_HOST,
        bindings=(
            ApprovedWooCategoryBinding(
                internal_category_key="clm-pro",
                woo_category_id=1431,
                expected_name="Realistic sex dolls",
            ),
            ApprovedWooCategoryBinding(
                internal_category_key="clm-ultra",
                woo_category_id=1432,
                expected_name="Silicone sex dolls",
            ),
        ),
    )


def _normalize_hostname(host_or_url: str) -> str:
    if not isinstance(host_or_url, str) or not host_or_url.strip():
        return ""
    candidate = host_or_url.strip()
    if "://" in candidate:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return ""
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return ""
        hostname = parsed.hostname or ""
    else:
        hostname = candidate.rstrip(".")
        if "/" in hostname or ":" in hostname:
            return ""
    return hostname.casefold().rstrip(".")


def _empty_summary() -> WooCategoryBindingSummary:
    return WooCategoryBindingSummary(
        total_internal_categories=len(_INTERNAL_CATEGORY_KEYS),
        bound_verified=0,
        unbound_categories=0,
        missing_targets=0,
        changed_targets=0,
        blocking_bindings=0,
    )


def _environment_mismatch_verification(
    profile: WooCategoryBindingProfile,
    *,
    environment: str,
    hostname: str,
) -> WooCategoryBindingVerification:
    return WooCategoryBindingVerification(
        status="blocked",
        profile_version=profile.profile_version,
        registry_version=CATEGORY_REGISTRY_VERSION,
        environment=environment,
        hostname=hostname,
        expected_host=profile.expected_host,
        results=(),
        summary=_empty_summary(),
        warnings=profile.warnings,
        blocking_issues=("category_binding_environment_mismatch",),
    )


def verify_woo_category_bindings(
    profile: WooCategoryBindingProfile,
    *,
    environment: str,
    host: str,
    discovery_records: Sequence[WooCategoryRecord],
) -> WooCategoryBindingVerification:
    """Verify an approved profile against local discovery records only."""

    if not isinstance(profile, WooCategoryBindingProfile):
        raise TypeError("profile must be a WooCategoryBindingProfile")
    normalized_environment = environment.strip().casefold() if isinstance(
        environment, str
    ) else ""
    normalized_host = _normalize_hostname(host)
    if (
        normalized_environment != profile.environment.casefold()
        or normalized_host != profile.expected_host.casefold()
    ):
        return _environment_mismatch_verification(
            profile,
            environment=normalized_environment,
            hostname=normalized_host,
        )

    if isinstance(discovery_records, (str, bytes)) or not isinstance(
        discovery_records, Sequence
    ):
        raise TypeError("discovery_records must be a sequence")
    discovery_by_id: dict[int, WooCategoryRecord] = {}
    for record in discovery_records:
        if not isinstance(record, WooCategoryRecord):
            raise TypeError("discovery_records must contain WooCategoryRecord values")
        if record.id in discovery_by_id:
            raise DiscoveryRecordConflictError(
                "discovery records contain a duplicate Woo category id"
            )
        discovery_by_id[record.id] = record

    results: list[WooCategoryBindingResult] = []
    for internal_category_key in _INTERNAL_CATEGORY_KEYS:
        approved = profile.binding_for(internal_category_key)
        if approved is None:
            results.append(
                WooCategoryBindingResult(
                    internal_category_key=internal_category_key,
                    status="unbound_category",
                    woo_category_id=None,
                    expected_name=None,
                    discovered_name=None,
                    warnings=(),
                    blocking_issues=(),
                )
            )
            continue
        discovered = discovery_by_id.get(approved.woo_category_id)
        if discovered is None:
            results.append(
                WooCategoryBindingResult(
                    internal_category_key=internal_category_key,
                    status="binding_target_missing",
                    woo_category_id=approved.woo_category_id,
                    expected_name=approved.expected_name,
                    discovered_name=None,
                    warnings=(),
                    blocking_issues=("binding_target_missing",),
                )
            )
        elif discovered.name != approved.expected_name:
            results.append(
                WooCategoryBindingResult(
                    internal_category_key=internal_category_key,
                    status="binding_target_changed",
                    woo_category_id=approved.woo_category_id,
                    expected_name=approved.expected_name,
                    discovered_name=discovered.name,
                    warnings=(),
                    blocking_issues=("binding_target_changed",),
                )
            )
        else:
            results.append(
                WooCategoryBindingResult(
                    internal_category_key=internal_category_key,
                    status="bound_verified",
                    woo_category_id=approved.woo_category_id,
                    expected_name=approved.expected_name,
                    discovered_name=discovered.name,
                    warnings=(),
                    blocking_issues=(),
                )
            )

    summary = WooCategoryBindingSummary(
        total_internal_categories=len(results),
        bound_verified=sum(result.status == "bound_verified" for result in results),
        unbound_categories=sum(
            result.status == "unbound_category" for result in results
        ),
        missing_targets=sum(
            result.status == "binding_target_missing" for result in results
        ),
        changed_targets=sum(
            result.status == "binding_target_changed" for result in results
        ),
        blocking_bindings=sum(bool(result.blocking_issues) for result in results),
    )
    blocking_issues = tuple(
        dict.fromkeys(
            issue for result in results for issue in result.blocking_issues
        )
    )
    return WooCategoryBindingVerification(
        status="blocked" if blocking_issues else "verified",
        profile_version=profile.profile_version,
        registry_version=CATEGORY_REGISTRY_VERSION,
        environment=normalized_environment,
        hostname=normalized_host,
        expected_host=profile.expected_host,
        results=tuple(results),
        summary=summary,
        warnings=profile.warnings,
        blocking_issues=blocking_issues,
    )
