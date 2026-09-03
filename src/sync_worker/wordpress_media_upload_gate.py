"""Pure-local staging gate for future WordPress media upload transport.

This module does not perform HTTP, create an HTTP client, read configuration
files, or own the lifecycle of converted WebP files.  It accepts only live
``VerifiedWebPArtifact`` capabilities, revalidates every artifact through the
Conversion Core, and issues non-serializable upload intents only when the
entire canonical batch passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from . import google_drive_folder_manifest as drive_manifest_core
from . import verified_webp_conversion as conversion_core
from .config import ConfigError, Settings
from .report import sanitize_report_data
from .sanitization import Redactor


POLICY_VERSION: Final = "xxxxdoll-wordpress-media-upload-gate-v1"
WORDPRESS_RESOURCE: Final = "media"
WORDPRESS_MEDIA_ENDPOINT: Final = "/wp-json/wp/v2/media"
MAX_UPLOAD_FILENAME_LENGTH: Final = 128
UPLOAD_FILENAME_SHA_PREFIX_LENGTH: Final = 16
MEDIA_IDENTITY_VERSION: Final = "xxxxdoll-media-identity-v1"
TARGET_BINDING_VERSION: Final = "xxxxdoll-wp-target-binding-v1"
MAX_ARTIFACTS_PER_BATCH: Final = conversion_core.MAX_ARTIFACTS_PER_BATCH

_INTENT_CAPABILITY = object()
_ASCII_FILENAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*\.webp\Z", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MEDIA_IDENTITY_PATTERN = re.compile(
    rf"{re.escape(MEDIA_IDENTITY_VERSION)}:[0-9a-f]{{64}}\Z", re.ASCII
)
_TARGET_BINDING_PATTERN = re.compile(
    rf"{re.escape(TARGET_BINDING_VERSION)}:[0-9a-f]{{64}}\Z", re.ASCII
)
_ZERO_ACTIVITY = {
    "network_requests_performed": 0,
    "wordpress_upload_requests_performed": 0,
    "woocommerce_requests_performed": 0,
    "external_write_requests_performed": 0,
    "write_requests_performed": 0,
}


class WordPressMediaUploadGateError(ValueError):
    """Fixed-code Gate error; never includes a URL, path, or credential."""


class WordPressMediaUploadIntent:
    """Immutable capability proving one WebP passed the staging upload Gate."""

    __slots__ = (
        "__capability",
        "__artifact",
        "__target_binding",
        "_sku",
        "_selection_position",
        "_image_role",
        "_folder_role",
        "_source_safe_name",
        "_output_size_bytes",
        "_output_sha256",
        "_image_width",
        "_image_height",
        "_upload_filename",
        "_media_identity",
        "_warnings",
    )

    def __init__(
        self,
        capability: object,
        *,
        artifact: conversion_core.VerifiedWebPArtifact,
        target_binding: str,
        upload_filename: str,
        media_identity: str,
    ) -> None:
        if capability is not _INTENT_CAPABILITY:
            raise WordPressMediaUploadGateError(
                "wordpress_media_upload_intent_factory_required"
            )
        if type(artifact) is not conversion_core.VerifiedWebPArtifact:
            raise WordPressMediaUploadGateError(
                "wordpress_media_requires_verified_webp"
            )
        if _TARGET_BINDING_PATTERN.fullmatch(target_binding) is None:
            raise WordPressMediaUploadGateError(
                "wordpress_media_target_binding_invalid"
            )
        if (
            _ASCII_FILENAME_PATTERN.fullmatch(upload_filename) is None
            or len(upload_filename) > MAX_UPLOAD_FILENAME_LENGTH
            or ".." in upload_filename
        ):
            raise WordPressMediaUploadGateError(
                "wordpress_media_upload_filename_invalid"
            )
        if _MEDIA_IDENTITY_PATTERN.fullmatch(media_identity) is None:
            raise WordPressMediaUploadGateError(
                "wordpress_media_identity_invalid"
            )
        object.__setattr__(self, "_WordPressMediaUploadIntent__capability", capability)
        object.__setattr__(self, "_WordPressMediaUploadIntent__artifact", artifact)
        object.__setattr__(
            self, "_WordPressMediaUploadIntent__target_binding", target_binding
        )
        object.__setattr__(self, "_sku", artifact.sku)
        object.__setattr__(self, "_selection_position", artifact.selection_position)
        object.__setattr__(self, "_image_role", artifact.image_role)
        object.__setattr__(self, "_folder_role", artifact.folder_role)
        object.__setattr__(self, "_source_safe_name", artifact.safe_name)
        object.__setattr__(self, "_output_size_bytes", artifact.output_size_bytes)
        object.__setattr__(self, "_output_sha256", artifact.output_sha256)
        object.__setattr__(self, "_image_width", artifact.image_width)
        object.__setattr__(self, "_image_height", artifact.image_height)
        object.__setattr__(self, "_upload_filename", upload_filename)
        object.__setattr__(self, "_media_identity", media_identity)
        object.__setattr__(self, "_warnings", tuple(artifact.warnings))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("wordpress_media_upload_intent_is_immutable")

    @property
    def policy_version(self) -> str:
        return POLICY_VERSION

    @property
    def sku(self) -> str:
        return self._sku

    @property
    def selection_position(self) -> int:
        return self._selection_position

    @property
    def image_role(self) -> str:
        return self._image_role

    @property
    def folder_role(self) -> str:
        return self._folder_role

    @property
    def source_safe_name(self) -> str:
        return self._source_safe_name

    @property
    def output_mime_type(self) -> str:
        return "image/webp"

    @property
    def output_extension(self) -> str:
        return ".webp"

    @property
    def output_size_bytes(self) -> int:
        return self._output_size_bytes

    @property
    def output_sha256(self) -> str:
        return self._output_sha256

    @property
    def image_width(self) -> int:
        return self._image_width

    @property
    def image_height(self) -> int:
        return self._image_height

    @property
    def upload_filename(self) -> str:
        return self._upload_filename

    @property
    def media_identity(self) -> str:
        return self._media_identity

    @property
    def target_host_fingerprint(self) -> str:
        return object.__getattribute__(
            self, "_WordPressMediaUploadIntent__target_binding"
        )

    @property
    def target_is_staging(self) -> bool:
        return True

    @property
    def wordpress_resource(self) -> str:
        return WORDPRESS_RESOURCE

    @property
    def upload_gate_passed(self) -> bool:
        return True

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @property
    def blocking_issues(self) -> tuple[str, ...]:
        return ()

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "sku": self.sku,
            "selection_position": self.selection_position,
            "image_role": self.image_role,
            "folder_role": self.folder_role,
            "source_safe_name": self.source_safe_name,
            "output_mime_type": self.output_mime_type,
            "output_extension": self.output_extension,
            "output_size_bytes": self.output_size_bytes,
            "output_sha256": self.output_sha256,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "upload_filename": self.upload_filename,
            "media_identity": self.media_identity,
            "target_host_fingerprint": self.target_host_fingerprint,
            "target_is_staging": self.target_is_staging,
            "wordpress_resource": self.wordpress_resource,
            "upload_gate_passed": self.upload_gate_passed,
            "alt_text": None,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }

    def __repr__(self) -> str:
        return f"WordPressMediaUploadIntent({self.to_safe_dict()!r})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("wordpress_media_upload_intent_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("wordpress_media_upload_intent_not_serializable")


class _WordPressMediaUploadMaterial:
    """Private, same-process transport material; never safe for projection."""

    __slots__ = (
        "__local_path",
        "upload_filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "target_host_fingerprint",
    )

    def __init__(
        self,
        *,
        local_path: Path,
        upload_filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        target_host_fingerprint: str,
    ) -> None:
        object.__setattr__(self, "_WordPressMediaUploadMaterial__local_path", local_path)
        object.__setattr__(self, "upload_filename", upload_filename)
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(self, "size_bytes", size_bytes)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(
            self, "target_host_fingerprint", target_host_fingerprint
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("wordpress_media_upload_material_is_immutable")


class WordPressMediaUploadGateBatchResult:
    """Safe batch audit plus all-or-nothing in-memory intent capabilities."""

    __slots__ = ("_status", "_summary", "_results", "_intents", "_blocking_issues")

    def __init__(
        self,
        *,
        status: str,
        summary: Mapping[str, int],
        results: tuple[Mapping[str, object], ...],
        intents: tuple[WordPressMediaUploadIntent, ...],
        blocking_issues: tuple[str, ...],
    ) -> None:
        self._status = status
        self._summary = dict(summary)
        self._results = tuple(dict(item) for item in results)
        self._intents = intents if status == "ok" else ()
        self._blocking_issues = blocking_issues

    @property
    def status(self) -> str:
        return self._status

    @property
    def intents(self) -> tuple[WordPressMediaUploadIntent, ...]:
        return self._intents

    def to_safe_dict(self) -> dict[str, object]:
        report = {
            "status": self.status,
            "policy_version": POLICY_VERSION,
            "summary": dict(self._summary),
            "results": [dict(item) for item in self._results],
            "warnings": [],
            "blocking_issues": list(self._blocking_issues),
            **_ZERO_ACTIVITY,
        }
        safe = sanitize_report_data(report, Redactor())
        drive_manifest_core._assert_report_safe(safe)
        return json.loads(json.dumps(safe, ensure_ascii=False))

    def __repr__(self) -> str:
        return (
            "WordPressMediaUploadGateBatchResult("
            f"status={self.status!r}, intents_count={len(self.intents)})"
        )

    def __reduce__(self):
        raise TypeError("wordpress_media_upload_gate_result_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("wordpress_media_upload_gate_result_not_serializable")


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _target_binding(settings: Settings) -> str:
    """Validate staging-only settings and return a non-sensitive target binding."""

    if type(settings) is not Settings:
        raise WordPressMediaUploadGateError("wordpress_media_settings_required")
    try:
        parsed = urlsplit(settings.wp_base_url)
        hostname = parsed.hostname.casefold() if parsed.hostname else ""
        port = parsed.port
    except (TypeError, ValueError):
        raise WordPressMediaUploadGateError("wordpress_media_target_url_invalid") from None

    if _matches_domain(hostname, "xxxxdoll.com"):
        raise WordPressMediaUploadGateError(
            "wordpress_media_production_host_forbidden"
        )
    if parsed.scheme.casefold() != "https":
        raise WordPressMediaUploadGateError("wordpress_media_https_required")
    if not _matches_domain(hostname, "wpcomstaging.com"):
        raise WordPressMediaUploadGateError(
            "wordpress_media_staging_host_required"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WordPressMediaUploadGateError("wordpress_media_target_url_invalid")
    if settings.sync_environment != "staging":
        raise WordPressMediaUploadGateError(
            "wordpress_media_staging_environment_required"
        )
    if settings.dry_run is not True:
        raise WordPressMediaUploadGateError("wordpress_media_dry_run_required")
    if settings.default_product_status != "draft":
        raise WordPressMediaUploadGateError(
            "wordpress_media_draft_status_required"
        )
    if settings.allow_delete is not False:
        raise WordPressMediaUploadGateError(
            "wordpress_media_delete_must_remain_disabled"
        )
    try:
        settings.validate()
    except ConfigError:
        raise WordPressMediaUploadGateError(
            "wordpress_media_staging_safety_failed"
        ) from None
    if not settings.staging_safety_checks().all_passed:
        raise WordPressMediaUploadGateError(
            "wordpress_media_staging_safety_failed"
        )
    normalized_path = parsed.path.rstrip("/")
    port_text = "" if port is None else f":{port}"
    digest = hashlib.sha256(
        f"{TARGET_BINDING_VERSION}\0{hostname}{port_text}{normalized_path}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{TARGET_BINDING_VERSION}:{digest}"


def _normalize_sku_for_filename(sku: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", sku).encode(
        "ascii", "ignore"
    ).decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").casefold()
    return normalized or "media"


def _upload_filename(artifact: conversion_core.VerifiedWebPArtifact) -> str:
    position = str(artifact.selection_position).zfill(2)
    suffix = f"-{position}-{artifact.output_sha256[:UPLOAD_FILENAME_SHA_PREFIX_LENGTH]}.webp"
    maximum_sku_length = MAX_UPLOAD_FILENAME_LENGTH - len(suffix)
    normalized_sku = _normalize_sku_for_filename(artifact.sku)[:maximum_sku_length]
    normalized_sku = normalized_sku.rstrip("-") or "media"
    filename = normalized_sku + suffix
    if (
        len(filename) > MAX_UPLOAD_FILENAME_LENGTH
        or _ASCII_FILENAME_PATTERN.fullmatch(filename) is None
        or "/" in filename
        or "\\" in filename
        or ".." in filename
    ):
        raise WordPressMediaUploadGateError(
            "wordpress_media_upload_filename_invalid"
        )
    return filename


def _media_identity(artifact: conversion_core.VerifiedWebPArtifact) -> str:
    value = (
        f"{MEDIA_IDENTITY_VERSION}\0{artifact.sku}\0"
        f"{artifact.selection_position}\0{artifact.output_sha256}"
    )
    return f"{MEDIA_IDENTITY_VERSION}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _normalize_artifacts(
    value: (
        conversion_core.VerifiedWebPArtifact
        | tuple[conversion_core.VerifiedWebPArtifact, ...]
    ),
) -> tuple[conversion_core.VerifiedWebPArtifact, ...]:
    if type(value) is conversion_core.VerifiedWebPArtifact:
        artifacts = (value,)
    elif type(value) is tuple:
        artifacts = value
    else:
        raise WordPressMediaUploadGateError(
            "verified_webp_artifacts_required"
        )
    if not artifacts or len(artifacts) > MAX_ARTIFACTS_PER_BATCH:
        raise WordPressMediaUploadGateError("verified_webp_artifacts_required")
    if any(type(item) is not conversion_core.VerifiedWebPArtifact for item in artifacts):
        raise WordPressMediaUploadGateError("verified_webp_artifacts_required")
    try:
        if any(
            type(item.sku) is not str
            or not item.sku
            or type(item.selection_position) is not int
            or item.selection_position < 0
            for item in artifacts
        ):
            raise ValueError
        keys = tuple((item.sku, item.selection_position) for item in artifacts)
        canonical = tuple(sorted(keys))
    except (AttributeError, TypeError, ValueError):
        raise WordPressMediaUploadGateError(
            "verified_webp_artifacts_required"
        ) from None
    if keys != canonical or len(keys) != len(set(keys)):
        raise WordPressMediaUploadGateError(
            "wordpress_media_artifacts_not_canonical"
        )
    return artifacts


def _safe_blocked_item(
    artifact: conversion_core.VerifiedWebPArtifact,
    *,
    status: str,
    blocker: str,
) -> dict[str, object]:
    return {
        "sku": artifact.sku,
        "selection_position": artifact.selection_position,
        "image_role": artifact.image_role,
        "folder_role": artifact.folder_role,
        "source_safe_name": artifact.safe_name,
        "output_mime_type": artifact.output_mime_type,
        "output_extension": artifact.output_extension,
        "output_size_bytes": artifact.output_size_bytes,
        "output_sha256": artifact.output_sha256,
        "image_width": artifact.image_width,
        "image_height": artifact.image_height,
        "upload_filename": None,
        "media_identity": None,
        "target_host_fingerprint": None,
        "target_is_staging": False,
        "wordpress_resource": WORDPRESS_RESOURCE,
        "upload_gate_passed": False,
        "alt_text": None,
        "gate_status": status,
        "warnings": list(artifact.warnings),
        "blocking_issues": [blocker],
    }


def _blocked_batch(
    artifacts: tuple[conversion_core.VerifiedWebPArtifact, ...],
    code: str,
) -> WordPressMediaUploadGateBatchResult:
    results = tuple(
        _safe_blocked_item(
            artifact, status="not_attempted", blocker=code
        )
        for artifact in artifacts
    )
    return WordPressMediaUploadGateBatchResult(
        status="blocked",
        summary={
            "artifacts_received": len(artifacts),
            "gate_passed": 0,
            "gate_blocked": len(artifacts),
            "intents_created": 0,
        },
        results=results,
        intents=(),
        blocking_issues=(code,),
    )


def create_wordpress_media_upload_intents(
    artifacts_value: (
        conversion_core.VerifiedWebPArtifact
        | tuple[conversion_core.VerifiedWebPArtifact, ...]
    ),
    settings: Settings,
) -> WordPressMediaUploadGateBatchResult:
    """Revalidate a canonical WebP batch and issue intents all-or-nothing."""

    artifacts = _normalize_artifacts(artifacts_value)
    try:
        target_binding = _target_binding(settings)
    except WordPressMediaUploadGateError as error:
        return _blocked_batch(artifacts, str(error))

    intents: list[WordPressMediaUploadIntent] = []
    results: list[dict[str, object]] = []
    blocked = False
    for artifact in artifacts:
        if blocked:
            results.append(_safe_blocked_item(
                artifact,
                status="not_attempted",
                blocker="wordpress_media_batch_aborted_after_failure",
            ))
            continue
        try:
            if (
                artifact.output_mime_type != "image/webp"
                or artifact.output_extension != ".webp"
                or artifact.webp_verified is not True
            ):
                raise WordPressMediaUploadGateError(
                    "wordpress_media_requires_verified_webp"
                )
            conversion_core._local_webp_path_for_upload(artifact)
            intent = WordPressMediaUploadIntent(
                _INTENT_CAPABILITY,
                artifact=artifact,
                target_binding=target_binding,
                upload_filename=_upload_filename(artifact),
                media_identity=_media_identity(artifact),
            )
        except (conversion_core.VerifiedWebPConversionError, OSError):
            blocker = "wordpress_media_verified_webp_revalidation_failed"
        except WordPressMediaUploadGateError as error:
            blocker = str(error)
        else:
            intents.append(intent)
            safe_item = intent.to_safe_dict()
            safe_item["gate_status"] = "gate_passed"
            results.append(safe_item)
            continue
        blocked = True
        results.append(_safe_blocked_item(
            artifact, status="gate_blocked", blocker=blocker
        ))

    if blocked:
        blockers = tuple(dict.fromkeys(
            issue
            for item in results
            for issue in item["blocking_issues"]
            if issue != "wordpress_media_batch_aborted_after_failure"
        ))
        return WordPressMediaUploadGateBatchResult(
            status="blocked",
            summary={
                "artifacts_received": len(artifacts),
                "gate_passed": len(intents),
                "gate_blocked": len(artifacts) - len(intents),
                "intents_created": 0,
            },
            results=tuple(results),
            intents=(),
            blocking_issues=blockers,
        )
    return WordPressMediaUploadGateBatchResult(
        status="ok",
        summary={
            "artifacts_received": len(artifacts),
            "gate_passed": len(intents),
            "gate_blocked": 0,
            "intents_created": len(intents),
        },
        results=tuple(results),
        intents=tuple(intents),
        blocking_issues=(),
    )


def _valid_intent(intent: object) -> bool:
    if type(intent) is not WordPressMediaUploadIntent:
        return False
    try:
        capability = object.__getattribute__(
            intent, "_WordPressMediaUploadIntent__capability"
        )
        artifact = object.__getattribute__(
            intent, "_WordPressMediaUploadIntent__artifact"
        )
        target_binding = object.__getattribute__(
            intent, "_WordPressMediaUploadIntent__target_binding"
        )
    except AttributeError:
        return False
    try:
        return (
            capability is _INTENT_CAPABILITY
            and type(artifact) is conversion_core.VerifiedWebPArtifact
            and _TARGET_BINDING_PATTERN.fullmatch(target_binding) is not None
            and intent.output_mime_type == "image/webp"
            and intent.output_extension == ".webp"
            and intent.upload_gate_passed is True
            and intent.wordpress_resource == WORDPRESS_RESOURCE
            and _SHA256_PATTERN.fullmatch(intent.output_sha256) is not None
            and _MEDIA_IDENTITY_PATTERN.fullmatch(intent.media_identity) is not None
            and _ASCII_FILENAME_PATTERN.fullmatch(intent.upload_filename) is not None
            and len(intent.upload_filename) <= MAX_UPLOAD_FILENAME_LENGTH
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _upload_material_for_transport(
    intent: WordPressMediaUploadIntent,
) -> _WordPressMediaUploadMaterial:
    """Privately revalidate an intent and its WebP immediately before transport."""

    if not _valid_intent(intent):
        raise WordPressMediaUploadGateError(
            "valid_wordpress_media_upload_intent_required"
        )
    artifact = object.__getattribute__(
        intent, "_WordPressMediaUploadIntent__artifact"
    )
    try:
        local_path = conversion_core._local_webp_path_for_upload(artifact)
    except conversion_core.VerifiedWebPConversionError:
        raise WordPressMediaUploadGateError(
            "wordpress_media_verified_webp_revalidation_failed"
        ) from None
    if (
        artifact.output_sha256 != intent.output_sha256
        or artifact.output_size_bytes != intent.output_size_bytes
        or artifact.sku != intent.sku
        or artifact.selection_position != intent.selection_position
    ):
        raise WordPressMediaUploadGateError(
            "wordpress_media_upload_intent_artifact_mismatch"
        )
    return _WordPressMediaUploadMaterial(
        local_path=local_path,
        upload_filename=intent.upload_filename,
        mime_type=intent.output_mime_type,
        size_bytes=intent.output_size_bytes,
        sha256=intent.output_sha256,
        target_host_fingerprint=intent.target_host_fingerprint,
    )


def _local_upload_path_for_transport(material: _WordPressMediaUploadMaterial) -> Path:
    """Narrow private accessor reserved for the future transport implementation."""

    if type(material) is not _WordPressMediaUploadMaterial:
        raise WordPressMediaUploadGateError(
            "wordpress_media_upload_material_required"
        )
    return object.__getattribute__(
        material, "_WordPressMediaUploadMaterial__local_path"
    )
