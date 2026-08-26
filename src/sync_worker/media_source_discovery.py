"""Pure-local classification of mapped supplier media references.

V1 parses URL structure only.  It never opens a URL, probes a resource,
downloads content, or creates a provider/API client.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from urllib.parse import parse_qs, urlsplit

from .image_mapping import (
    REDACTED_REFERENCE,
    MediaSourceMappingResult,
    SupplierMediaSourceReference,
)
from .sku_policy import MAX_SKU_LENGTH, SkuGenerationResult


DiscoveryStatus = Literal[
    "classified",
    "redacted_reference",
    "missing_reference",
    "invalid_reference",
    "insecure_scheme",
    "unsupported_scheme",
    "embedded_credentials",
]
MediaProvider = Literal[
    "google_drive",
    "dropbox",
    "onedrive",
    "sharepoint",
    "direct_web",
    "unknown",
]
DiscoveredResourceKind = Literal[
    "folder",
    "file",
    "workspace_resource",
    "direct_image_candidate",
    "archive_candidate",
    "unknown",
]
DiscoverySource: TypeAlias = (
    SupplierMediaSourceReference | MediaSourceMappingResult
)

_SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
_COORDINATE_PATTERN = re.compile(r"^[A-Z]+[1-9][0-9]*$")
_SAFE_SKU_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_SAFE_HOST_PATTERN = re.compile(r"^(?:[a-z0-9.-]+|[0-9a-f:]+)$")
_GOOGLE_FOLDER_PATTERN = re.compile(
    r"^/drive/folders/([^/]+)(?:/|$)", re.IGNORECASE
)
_GOOGLE_FILE_PATTERN = re.compile(
    r"^/file/d/([^/]+)(?:/|$)", re.IGNORECASE
)
_GOOGLE_WORKSPACE_PATTERN = re.compile(
    r"^/(?:document|spreadsheets|presentation|forms)/d/([^/]+)(?:/|$)",
    re.IGNORECASE,
)
_DROPBOX_FOLDER_PATTERN = re.compile(
    r"^/scl/fo/([^/]+)(?:/|$)", re.IGNORECASE
)
_DROPBOX_FILE_PATTERN = re.compile(
    r"^/scl/fi/([^/]+)(?:/|$)", re.IGNORECASE
)
_DIRECT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
_ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_UNSAFE_SERIALIZED_PATTERN = re.compile(
    r"(?i)https?://|user:[^\s@]+@|"
    r"(?:access_token|token|signature|auth|key|password)\s*=|"
    r"\b(?:authorization|cookie)\b"
)


class MediaSourceDiscoveryError(ValueError):
    """Safe validation error without raw supplier reference content."""


@dataclass(frozen=True, slots=True)
class MediaSourceDiscoveryResult:
    discovery_status: DiscoveryStatus
    provider: MediaProvider
    resource_kind: DiscoveredResourceKind
    scheme: str | None
    safe_host: str | None
    safe_path_hint: str | None
    reference_coordinate: str
    reference_fingerprint: str | None
    resource_id_fingerprint: str | None
    requires_provider_api: bool
    requires_http_probe: bool
    download_ready: Literal[False]
    sku: str | None
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    provider_resource_id: str | None = field(default=None, repr=False)
    resource_key: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, object]:
        report = {
            "discovery_status": self.discovery_status,
            "provider": self.provider,
            "resource_kind": self.resource_kind,
            "scheme": self.scheme,
            "safe_host": self.safe_host,
            "safe_path_hint": self.safe_path_hint,
            "reference_coordinate": self.reference_coordinate,
            "reference_fingerprint": self.reference_fingerprint,
            "resource_id_fingerprint": self.resource_id_fingerprint,
            "requires_provider_api": self.requires_provider_api,
            "requires_http_probe": self.requires_http_probe,
            "download_ready": False,
            "sku": self.sku,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }
        _assert_report_safe(report)
        return report


@dataclass(frozen=True, slots=True)
class MediaSourceDiscoverySummary:
    total_sources: int
    classified_sources: int
    redacted_sources: int
    missing_sources: int
    invalid_sources: int
    google_drive_sources: int
    dropbox_sources: int
    onedrive_sources: int
    sharepoint_sources: int
    direct_web_sources: int
    unknown_sources: int
    folder_candidates: int
    file_candidates: int
    workspace_resources: int
    direct_image_candidates: int
    archive_candidates: int
    insecure_sources: int
    unsupported_scheme_sources: int
    credential_blocked_sources: int

    def to_dict(self) -> dict[str, int]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class MediaSourceDiscoveryBatchResult:
    results: tuple[MediaSourceDiscoveryResult, ...]
    summary: MediaSourceDiscoverySummary
    network_requests_performed: Literal[0] = 0
    write_requests_performed: Literal[0] = 0

    def to_report_dict(self) -> dict[str, object]:
        report = {
            "status": "ok",
            "summary": self.summary.to_dict(),
            "network_requests_performed": 0,
            "write_requests_performed": 0,
            "results": [item.to_dict() for item in self.results],
        }
        _assert_report_safe(report)
        return report


@dataclass(frozen=True, slots=True)
class _SourceView:
    raw_reference: str | None = field(repr=False)
    reference_status: str
    reference_coordinate: str
    reference_fingerprint: str | None
    raw_reference_available: bool


@dataclass(frozen=True, slots=True)
class _UrlClassification:
    provider: MediaProvider
    resource_kind: DiscoveredResourceKind
    safe_path_hint: str
    resource_id: str | None
    requires_provider_api: bool
    requires_http_probe: bool
    warnings: tuple[str, ...] = ()
    resource_key: str | None = field(default=None, repr=False)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _assert_report_safe(report: Mapping[str, object]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if "raw_reference" in serialized or _UNSAFE_SERIALIZED_PATTERN.search(serialized):
        raise MediaSourceDiscoveryError("unsafe_media_discovery_output")


def _source_view(source: DiscoverySource) -> _SourceView:
    if isinstance(source, SupplierMediaSourceReference):
        return _SourceView(
            raw_reference=source.raw_reference,
            reference_status=source.reference_status,
            reference_coordinate=source.source_coordinate,
            reference_fingerprint=source.reference_fingerprint,
            raw_reference_available=True,
        )
    if isinstance(source, MediaSourceMappingResult):
        return _SourceView(
            raw_reference=source.safe_reference,
            reference_status=source.reference_status,
            reference_coordinate=source.reference_coordinate,
            reference_fingerprint=source.reference_fingerprint,
            raw_reference_available=False,
        )
    raise TypeError(
        "source must be SupplierMediaSourceReference or MediaSourceMappingResult"
    )


def _safe_coordinate(value: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if _COORDINATE_PATTERN.fullmatch(normalized):
            return normalized, ()
    return "[INVALID_COORDINATE]", ("invalid_reference_coordinate",)


def _safe_reference_fingerprint(
    value: object,
) -> tuple[str | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    if isinstance(value, str) and _SHA256_PATTERN.fullmatch(value):
        return value, ()
    return None, ("invalid_reference_fingerprint",)


def _resource_fingerprint(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    return hashlib.sha256(resource_id.encode("utf-8")).hexdigest()


def _safe_sku(
    sku_result: SkuGenerationResult | None,
) -> tuple[str | None, tuple[str, ...]]:
    if sku_result is None:
        return None, ()
    if not isinstance(sku_result, SkuGenerationResult):
        raise TypeError("sku_result must be a SkuGenerationResult or null")
    sku = sku_result.sku
    if (
        sku_result.status == "ok"
        and isinstance(sku, str)
        and 1 <= len(sku) <= MAX_SKU_LENGTH
        and _SAFE_SKU_PATTERN.fullmatch(sku)
    ):
        return sku, ()
    return None, ("sku_not_verified",)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _google_resource_key(query: str) -> str | None:
    values = [value for value in parse_qs(query).get("resourcekey", []) if value]
    return values[0] if len(values) == 1 else None


def _classify_path(host: str, path: str, query: str) -> _UrlClassification:
    if host == "drive.google.com":
        resource_key = _google_resource_key(query)
        if match := _GOOGLE_FOLDER_PATTERN.match(path):
            return _UrlClassification(
                "google_drive",
                "folder",
                "/drive/folders/[RESOURCE_ID]",
                match.group(1),
                True,
                False,
                resource_key=resource_key,
            )
        if match := _GOOGLE_FILE_PATTERN.match(path):
            return _UrlClassification(
                "google_drive",
                "file",
                "/file/d/[RESOURCE_ID]",
                match.group(1),
                True,
                False,
                resource_key=resource_key,
            )
        resource_id = None
        if path.rstrip("/").casefold() == "/open":
            identifiers = [value for value in parse_qs(query).get("id", []) if value]
            if len(identifiers) == 1:
                resource_id = identifiers[0]
        return _UrlClassification(
            "google_drive",
            "unknown",
            "/open" if path.rstrip("/").casefold() == "/open" else "/[PATH]",
            resource_id,
            True,
            False,
            resource_key=resource_key,
        )
    if host == "docs.google.com":
        match = _GOOGLE_WORKSPACE_PATTERN.match(path)
        return _UrlClassification(
            "google_drive",
            "workspace_resource",
            "/workspace/d/[RESOURCE_ID]" if match else "/workspace/[PATH]",
            match.group(1) if match else None,
            True,
            False,
            resource_key=_google_resource_key(query),
        )
    if _host_matches(host, "dropbox.com"):
        if match := _DROPBOX_FOLDER_PATTERN.match(path):
            return _UrlClassification(
                "dropbox",
                "folder",
                "/scl/fo/[RESOURCE_ID]",
                match.group(1),
                True,
                False,
            )
        if match := _DROPBOX_FILE_PATTERN.match(path):
            return _UrlClassification(
                "dropbox",
                "file",
                "/scl/fi/[RESOURCE_ID]",
                match.group(1),
                True,
                False,
            )
        return _UrlClassification(
            "dropbox", "unknown", "/[PATH]", None, True, False
        )
    if host == "1drv.ms" or _host_matches(host, "onedrive.live.com"):
        return _UrlClassification(
            "onedrive", "unknown", "/[PATH]", None, True, False
        )
    if _host_matches(host, "sharepoint.com"):
        return _UrlClassification(
            "sharepoint", "unknown", "/[PATH]", None, True, False
        )

    lowered_path = path.casefold()
    for extension in _DIRECT_IMAGE_EXTENSIONS:
        if lowered_path.endswith(extension):
            return _UrlClassification(
                "direct_web",
                "direct_image_candidate",
                f"/[PATH]{extension}",
                None,
                False,
                True,
                ("resource_not_verified",),
            )
    for extension in _ARCHIVE_EXTENSIONS:
        if lowered_path.endswith(extension):
            return _UrlClassification(
                "direct_web",
                "archive_candidate",
                f"/[PATH]{extension}",
                None,
                False,
                True,
                ("resource_not_verified",),
            )
    return _UrlClassification(
        "direct_web", "unknown", "/[PATH]" if path != "/" else "/", None, False, True
    )


def _base_result(
    *,
    status: DiscoveryStatus,
    coordinate: str,
    fingerprint: str | None,
    sku: str | None,
    scheme: str | None = None,
    safe_host: str | None = None,
    warnings: Sequence[str] = (),
    blockers: Sequence[str] = (),
) -> MediaSourceDiscoveryResult:
    return MediaSourceDiscoveryResult(
        discovery_status=status,
        provider="unknown",
        resource_kind="unknown",
        scheme=scheme,
        safe_host=safe_host,
        safe_path_hint=None,
        reference_coordinate=coordinate,
        reference_fingerprint=fingerprint,
        resource_id_fingerprint=None,
        requires_provider_api=False,
        requires_http_probe=False,
        download_ready=False,
        sku=sku,
        warnings=_unique(tuple(warnings)),
        blocking_issues=_unique(tuple(blockers)),
    )


def discover_media_source(
    source: DiscoverySource,
    *,
    sku_result: SkuGenerationResult | None = None,
) -> MediaSourceDiscoveryResult:
    """Classify one source entirely in memory without accessing it."""

    view = _source_view(source)
    coordinate, coordinate_warnings = _safe_coordinate(view.reference_coordinate)
    fingerprint, fingerprint_warnings = _safe_reference_fingerprint(
        view.reference_fingerprint
    )
    sku, sku_warnings = _safe_sku(sku_result)
    warnings = [
        *coordinate_warnings,
        *fingerprint_warnings,
        *sku_warnings,
    ]
    if not view.raw_reference_available:
        warnings.append("classification_limited_to_safe_reference")

    raw_reference = view.raw_reference
    if raw_reference is None or (
        isinstance(raw_reference, str) and not raw_reference.strip()
    ):
        status: DiscoveryStatus = (
            "missing_reference"
            if view.reference_status == "missing" or raw_reference is None
            else "invalid_reference"
        )
        issue = (
            "missing_reference"
            if status == "missing_reference"
            else "invalid_reference"
        )
        return _base_result(
            status=status,
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            warnings=(*warnings, issue),
            blockers=(issue,),
        )
    if not isinstance(raw_reference, str):
        return _base_result(
            status="invalid_reference",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            warnings=(*warnings, "invalid_reference"),
            blockers=("invalid_reference",),
        )
    stripped_reference = raw_reference.strip()
    if (
        view.reference_status == "redacted"
        or stripped_reference.casefold() == REDACTED_REFERENCE.casefold()
    ):
        return _base_result(
            status="redacted_reference",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            warnings=(*warnings, "reference_content_redacted"),
        )

    try:
        parsed = urlsplit(stripped_reference)
        scheme = parsed.scheme.casefold()
        host = parsed.hostname.casefold().rstrip(".") if parsed.hostname else None
        _ = parsed.port
        has_credentials = parsed.username is not None or parsed.password is not None
    except (TypeError, ValueError, UnicodeError):
        return _base_result(
            status="invalid_reference",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            warnings=(*warnings, "invalid_reference"),
            blockers=("invalid_reference",),
        )

    if has_credentials:
        return _base_result(
            status="embedded_credentials",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            scheme=scheme or None,
            safe_host=host,
            warnings=(*warnings, "media_source_credentials_blocked"),
            blockers=("embedded_credentials",),
        )
    if not scheme:
        return _base_result(
            status="invalid_reference",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            warnings=(*warnings, "invalid_reference"),
            blockers=("invalid_reference",),
        )
    if scheme not in {"http", "https"}:
        return _base_result(
            status="unsupported_scheme",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            scheme=scheme or None,
            safe_host=host,
            warnings=(*warnings, "unsupported_media_scheme"),
            blockers=("unsupported_media_scheme",),
        )
    if host is None or _SAFE_HOST_PATTERN.fullmatch(host) is None:
        return _base_result(
            status="invalid_reference",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            scheme=scheme,
            warnings=(*warnings, "invalid_reference"),
            blockers=("invalid_reference",),
        )
    if scheme == "http" and host not in _LOCAL_HOSTS:
        return _base_result(
            status="insecure_scheme",
            coordinate=coordinate,
            fingerprint=fingerprint,
            sku=sku,
            scheme=scheme,
            safe_host=host,
            warnings=(*warnings, "insecure_media_source"),
            blockers=("insecure_media_source",),
        )

    classification = _classify_path(host, parsed.path or "/", parsed.query)
    result = MediaSourceDiscoveryResult(
        discovery_status="classified",
        provider=classification.provider,
        resource_kind=classification.resource_kind,
        scheme=scheme,
        safe_host=host,
        safe_path_hint=classification.safe_path_hint,
        reference_coordinate=coordinate,
        reference_fingerprint=fingerprint,
        resource_id_fingerprint=_resource_fingerprint(
            classification.resource_id
        ),
        requires_provider_api=classification.requires_provider_api,
        requires_http_probe=classification.requires_http_probe,
        download_ready=False,
        sku=sku,
        warnings=_unique((*warnings, *classification.warnings)),
        blocking_issues=(),
        provider_resource_id=classification.resource_id,
        resource_key=classification.resource_key,
    )
    result.to_dict()
    return result


def summarize_media_source_discovery(
    results: Sequence[MediaSourceDiscoveryResult],
) -> MediaSourceDiscoverySummary:
    """Build deterministic provider, kind, and blocker counters."""

    stable_results = tuple(results)
    if any(not isinstance(item, MediaSourceDiscoveryResult) for item in stable_results):
        raise TypeError("results must contain MediaSourceDiscoveryResult values")
    return MediaSourceDiscoverySummary(
        total_sources=len(stable_results),
        classified_sources=sum(
            item.discovery_status == "classified" for item in stable_results
        ),
        redacted_sources=sum(
            item.discovery_status == "redacted_reference" for item in stable_results
        ),
        missing_sources=sum(
            item.discovery_status == "missing_reference" for item in stable_results
        ),
        invalid_sources=sum(
            item.discovery_status == "invalid_reference" for item in stable_results
        ),
        google_drive_sources=sum(
            item.provider == "google_drive" for item in stable_results
        ),
        dropbox_sources=sum(item.provider == "dropbox" for item in stable_results),
        onedrive_sources=sum(item.provider == "onedrive" for item in stable_results),
        sharepoint_sources=sum(
            item.provider == "sharepoint" for item in stable_results
        ),
        direct_web_sources=sum(
            item.provider == "direct_web" for item in stable_results
        ),
        unknown_sources=sum(item.provider == "unknown" for item in stable_results),
        folder_candidates=sum(
            item.resource_kind == "folder" for item in stable_results
        ),
        file_candidates=sum(item.resource_kind == "file" for item in stable_results),
        workspace_resources=sum(
            item.resource_kind == "workspace_resource" for item in stable_results
        ),
        direct_image_candidates=sum(
            item.resource_kind == "direct_image_candidate"
            for item in stable_results
        ),
        archive_candidates=sum(
            item.resource_kind == "archive_candidate" for item in stable_results
        ),
        insecure_sources=sum(
            item.discovery_status == "insecure_scheme" for item in stable_results
        ),
        unsupported_scheme_sources=sum(
            item.discovery_status == "unsupported_scheme"
            for item in stable_results
        ),
        credential_blocked_sources=sum(
            item.discovery_status == "embedded_credentials"
            for item in stable_results
        ),
    )


def _result_sort_key(result: MediaSourceDiscoveryResult) -> tuple[object, ...]:
    return (
        result.reference_coordinate,
        result.reference_fingerprint or "",
        result.provider,
        result.resource_kind,
        result.sku or "",
    )


def discover_media_sources(
    sources: Sequence[DiscoverySource],
    *,
    sku_results_by_coordinate: Mapping[str, SkuGenerationResult] | None = None,
) -> MediaSourceDiscoveryBatchResult:
    """Classify a batch; optional SKU data is audit metadata only."""

    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence")
    sku_lookup = sku_results_by_coordinate or {}
    discovered = tuple(
        sorted(
            (
                discover_media_source(
                    source,
                    sku_result=sku_lookup.get(
                        _source_view(source).reference_coordinate
                    ),
                )
                for source in sources
            ),
            key=_result_sort_key,
        )
    )
    summary = summarize_media_source_discovery(discovered)
    return MediaSourceDiscoveryBatchResult(
        results=discovered,
        summary=summary,
        network_requests_performed=0,
        write_requests_performed=0,
    )
