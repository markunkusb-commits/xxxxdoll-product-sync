"""Capability-gated, all-or-nothing verified WebP conversion.

Only in-memory :class:`VerifiedDownloadedMediaArtifact` authorities are
accepted.  Source paths are obtained through the Download Core's private,
revalidating helper.  Converted or byte-preserved outputs remain private and
can be granted to a future upload core only through
``_local_webp_path_for_upload`` after another integrity check.

This module is deliberately local-only: it has no CLI, report writer, network,
Google, WooCommerce, or WordPress integration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from PIL import Image, UnidentifiedImageError

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_media_download as download_core
from .report import sanitize_report_data
from .sanitization import Redactor


POLICY_VERSION: Final = "xxxxdoll-verified-webp-conversion-v1"
ENCODER_PROFILE_VERSION: Final = "xxxxdoll-pillow-webp-q85-m6-v1"
EXISTING_WEBP_PROFILE_VERSION: Final = "xxxxdoll-existing-webp-byte-copy-v1"
WEBP_QUALITY: Final = 85
WEBP_METHOD: Final = 6
MAX_DECODE_PIXELS: Final = 100_000_000
MAX_ARTIFACTS_PER_BATCH: Final = download_core.MAX_HANDLES_PER_BATCH
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WEBP_ARTIFACT_CAPABILITY = object()
_WEBP_NAME_PATTERN = re.compile(r"webp-[0-9]{3}-[0-9]+\.webp\Z", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SOURCE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_CONVERTIBLE_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
_CHUNK_SIZE = 256 * 1024
_ZERO_EXTERNAL_COUNTERS = {
    "wordpress_upload_requests_performed": 0,
    "external_write_requests_performed": 0,
}


class VerifiedWebPConversionError(ValueError):
    """Fixed safe error codes only; never paths or source authority values."""


class _ConversionBlocked(Exception):
    __slots__ = ("code", "decoded_width", "decoded_height")

    def __init__(
        self,
        code: str,
        *,
        decoded_width: int | None = None,
        decoded_height: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.decoded_width = decoded_width
        self.decoded_height = decoded_height


class VerifiedWebPArtifact:
    """Immutable and non-serializable authority over one verified WebP file."""

    __slots__ = (
        "__capability",
        "__local_webp_path",
        "__workspace_root",
        "_sku",
        "_selection_position",
        "_image_role",
        "_folder_role",
        "_safe_name",
        "_file_id_fingerprint",
        "_source_mime_type",
        "_source_size_bytes",
        "_source_md5_checksum",
        "_output_size_bytes",
        "_output_sha256",
        "_image_width",
        "_image_height",
        "_conversion_action",
        "_encoder_profile_version",
        "_webp_verified",
        "_warnings",
        "_blocking_issues",
    )

    def __init__(
        self,
        capability: object,
        *,
        source: download_core.VerifiedDownloadedMediaArtifact,
        local_webp_path: Path,
        workspace_root: Path,
        output_size_bytes: int,
        output_sha256: str,
        image_width: int,
        image_height: int,
        conversion_action: str,
        encoder_profile_version: str,
    ) -> None:
        if capability is not _WEBP_ARTIFACT_CAPABILITY:
            raise VerifiedWebPConversionError("verified_webp_artifact_factory_required")
        if type(source) is not download_core.VerifiedDownloadedMediaArtifact:
            raise VerifiedWebPConversionError("verified_downloaded_media_artifact_required")
        object.__setattr__(self, "_VerifiedWebPArtifact__capability", capability)
        object.__setattr__(self, "_VerifiedWebPArtifact__local_webp_path", local_webp_path)
        object.__setattr__(self, "_VerifiedWebPArtifact__workspace_root", workspace_root)
        object.__setattr__(self, "_sku", source.sku)
        object.__setattr__(self, "_selection_position", source.selection_position)
        object.__setattr__(self, "_image_role", source.image_role)
        object.__setattr__(self, "_folder_role", source.folder_role)
        object.__setattr__(self, "_safe_name", source.safe_name)
        object.__setattr__(self, "_file_id_fingerprint", source.file_id_fingerprint)
        object.__setattr__(self, "_source_mime_type", source.source_mime_type)
        object.__setattr__(self, "_source_size_bytes", source.actual_size_bytes)
        object.__setattr__(self, "_source_md5_checksum", source.actual_md5_checksum)
        object.__setattr__(self, "_output_size_bytes", output_size_bytes)
        object.__setattr__(self, "_output_sha256", output_sha256)
        object.__setattr__(self, "_image_width", image_width)
        object.__setattr__(self, "_image_height", image_height)
        object.__setattr__(self, "_conversion_action", conversion_action)
        object.__setattr__(self, "_encoder_profile_version", encoder_profile_version)
        object.__setattr__(self, "_webp_verified", True)
        object.__setattr__(self, "_warnings", tuple(source.warnings))
        object.__setattr__(self, "_blocking_issues", ())

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("verified_webp_artifact_is_immutable")

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
    def safe_name(self) -> str:
        return self._safe_name

    @property
    def file_id_fingerprint(self) -> str:
        return self._file_id_fingerprint

    @property
    def source_mime_type(self) -> str:
        return self._source_mime_type

    @property
    def source_size_bytes(self) -> int:
        return self._source_size_bytes

    @property
    def source_md5_checksum(self) -> str:
        return self._source_md5_checksum

    @property
    def output_mime_type(self) -> Literal["image/webp"]:
        return "image/webp"

    @property
    def output_extension(self) -> Literal[".webp"]:
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
    def conversion_action(self) -> str:
        return self._conversion_action

    @property
    def encoder_profile_version(self) -> str:
        return self._encoder_profile_version

    @property
    def webp_verified(self) -> bool:
        return self._webp_verified

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @property
    def blocking_issues(self) -> tuple[str, ...]:
        return self._blocking_issues

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "sku": self.sku,
            "selection_position": self.selection_position,
            "image_role": self.image_role,
            "folder_role": self.folder_role,
            "safe_name": self.safe_name,
            "file_id_fingerprint": self.file_id_fingerprint,
            "source_mime_type": self.source_mime_type,
            "source_size_bytes": self.source_size_bytes,
            "source_md5_checksum": self.source_md5_checksum,
            "output_mime_type": self.output_mime_type,
            "output_extension": self.output_extension,
            "output_size_bytes": self.output_size_bytes,
            "output_sha256": self.output_sha256,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "conversion_action": self.conversion_action,
            "encoder_profile_version": self.encoder_profile_version,
            "webp_verified": self.webp_verified,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }

    def __repr__(self) -> str:
        return f"VerifiedWebPArtifact({self.to_safe_dict()!r})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("verified_webp_artifact_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("verified_webp_artifact_not_serializable")


def _valid_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_PATTERN.fullmatch(value) is not None


def _webp_artifact_paths(artifact: VerifiedWebPArtifact) -> tuple[Path, Path]:
    if type(artifact) is not VerifiedWebPArtifact:
        raise VerifiedWebPConversionError("verified_webp_artifact_required")
    try:
        capability = object.__getattribute__(
            artifact, "_VerifiedWebPArtifact__capability"
        )
        path = object.__getattribute__(
            artifact, "_VerifiedWebPArtifact__local_webp_path"
        )
        workspace = object.__getattribute__(
            artifact, "_VerifiedWebPArtifact__workspace_root"
        )
    except AttributeError:
        raise VerifiedWebPConversionError("invalid_verified_webp_artifact") from None
    if (
        capability is not _WEBP_ARTIFACT_CAPABILITY
        or not isinstance(path, Path)
        or not isinstance(workspace, Path)
        or path.parent != workspace
        or not path.is_relative_to(workspace)
        or not workspace.name.startswith("xxxxdoll-webp-")
        or _WEBP_NAME_PATTERN.fullmatch(path.name) is None
        or path.suffix.casefold() != ".webp"
        or artifact.webp_verified is not True
        or artifact.output_mime_type != "image/webp"
        or artifact.output_extension != ".webp"
        or type(artifact.output_size_bytes) is not int
        or artifact.output_size_bytes <= 0
        or not _valid_sha256(artifact.output_sha256)
        or type(artifact.image_width) is not int
        or artifact.image_width <= 0
        or type(artifact.image_height) is not int
        or artifact.image_height <= 0
    ):
        raise VerifiedWebPConversionError("invalid_verified_webp_artifact")
    return path, workspace


def _read_size_sha256_and_magic(path: Path) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if len(prefix) < 12:
                    prefix.extend(chunk[: 12 - len(prefix)])
                total += len(chunk)
                digest.update(chunk)
    except OSError:
        raise VerifiedWebPConversionError("verified_webp_file_unavailable") from None
    return total, digest.hexdigest(), bytes(prefix)


def _webp_magic_matches(prefix: bytes) -> bool:
    return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"


def _local_webp_path_for_upload(artifact: VerifiedWebPArtifact) -> Path:
    """Reverify final bytes before granting private upload-path authority."""

    path, workspace = _webp_artifact_paths(artifact)
    try:
        info = path.lstat()
    except OSError:
        raise VerifiedWebPConversionError("verified_webp_file_unavailable") from None
    if (
        path.parent != workspace
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise VerifiedWebPConversionError("verified_webp_path_invalid")
    total, digest, prefix = _read_size_sha256_and_magic(path)
    if not _webp_magic_matches(prefix):
        raise VerifiedWebPConversionError("verified_webp_magic_mismatch")
    if total != artifact.output_size_bytes or digest != artifact.output_sha256:
        raise VerifiedWebPConversionError("verified_webp_local_content_changed")
    return path


def _normalize_sources(
    value: (
        download_core.VerifiedDownloadedMediaArtifact
        | tuple[download_core.VerifiedDownloadedMediaArtifact, ...]
    ),
) -> tuple[download_core.VerifiedDownloadedMediaArtifact, ...]:
    if type(value) is download_core.VerifiedDownloadedMediaArtifact:
        sources = (value,)
    elif type(value) is tuple:
        sources = value
    else:
        raise VerifiedWebPConversionError("verified_downloaded_media_artifacts_required")
    if not sources or len(sources) > MAX_ARTIFACTS_PER_BATCH:
        raise VerifiedWebPConversionError("verified_downloaded_media_artifacts_required")
    if any(type(item) is not download_core.VerifiedDownloadedMediaArtifact for item in sources):
        raise VerifiedWebPConversionError("verified_downloaded_media_artifacts_required")
    try:
        safe_sources = tuple(item.to_safe_dict() for item in sources)
        for item, safe_source in zip(sources, safe_sources, strict=True):
            if (
                type(item.sku) is not str
                or not item.sku
                or type(item.selection_position) is not int
                or item.selection_position < 0
                or type(item.image_role) is not str
                or type(item.folder_role) is not str
                or type(item.safe_name) is not str
                or type(item.file_id_fingerprint) is not str
                or type(item.source_mime_type) is not str
                or type(item.actual_size_bytes) is not int
                or item.actual_size_bytes <= 0
                or not download_core._valid_md5(item.actual_md5_checksum)
                or item.source_verified is not True
                or type(item.warnings) is not tuple
                or type(item.blocking_issues) is not tuple
                or item.blocking_issues
            ):
                raise TypeError
            drive_manifest_core._assert_report_safe(safe_source)
        identities = tuple((item.sku, item.selection_position) for item in sources)
    except (
        AttributeError,
        TypeError,
        drive_manifest_core.GoogleDriveFolderManifestError,
    ):
        raise VerifiedWebPConversionError(
            "verified_downloaded_media_artifacts_required"
        ) from None
    if len(set(identities)) != len(identities):
        raise VerifiedWebPConversionError("duplicate_webp_conversion_identity")
    return sources


def _safe_workspace_parent(workspace_parent: Path | None) -> Path | None:
    if workspace_parent is None:
        return None
    try:
        absolute = Path(os.path.abspath(Path(workspace_parent)))
        for component in (absolute, *absolute.parents):
            info = component.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise VerifiedWebPConversionError("webp_workspace_parent_invalid")
        parent = absolute.resolve(strict=True)
        info = parent.lstat()
    except VerifiedWebPConversionError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise VerifiedWebPConversionError("webp_workspace_parent_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or parent == _PROJECT_ROOT
        or _PROJECT_ROOT in parent.parents
    ):
        raise VerifiedWebPConversionError("webp_workspace_parent_invalid")
    return parent


def _create_workspace(parent: Path | None) -> Path:
    try:
        workspace = Path(
            tempfile.mkdtemp(
                prefix="xxxxdoll-webp-",
                dir=None if parent is None else str(parent),
            )
        ).resolve(strict=True)
    except (OSError, RuntimeError):
        raise VerifiedWebPConversionError("webp_workspace_creation_failed") from None
    if workspace == _PROJECT_ROOT or _PROJECT_ROOT in workspace.parents:
        try:
            workspace.rmdir()
        except OSError:
            pass
        raise VerifiedWebPConversionError("webp_workspace_location_forbidden")
    return workspace


def _cleanup_files(paths: tuple[Path, ...], workspace: Path | None) -> int:
    cleaned = 0
    if workspace is not None:
        for path in paths:
            if path.parent != workspace or _WEBP_NAME_PATTERN.fullmatch(path.name) is None:
                continue
            try:
                path.unlink(missing_ok=False)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            else:
                cleaned += 1
        try:
            workspace.rmdir()
        except OSError:
            pass
    return cleaned


class _WebPWorkspaceLifecycle:
    __slots__ = ("workspace", "paths")

    def __init__(self) -> None:
        self.workspace: Path | None = None
        self.paths: list[Path] = []

    def cleanup(self) -> int:
        return _cleanup_files(tuple(self.paths), self.workspace)


def _safe_item(source: download_core.VerifiedDownloadedMediaArtifact) -> dict[str, object]:
    if source.source_mime_type == "image/webp":
        action = "validate_existing_webp"
    elif source.source_mime_type in _CONVERTIBLE_MIME_TYPES:
        action = "convert_to_webp"
    else:
        action = "not_allowed"
    return {
        "sku": source.sku,
        "selection_position": source.selection_position,
        "image_role": source.image_role,
        "folder_role": source.folder_role,
        "safe_name": source.safe_name,
        "source_mime_type": source.source_mime_type,
        "source_size_bytes": source.actual_size_bytes,
        "conversion_action": action,
        "expected_width": source.expected_image_width,
        "expected_height": source.expected_image_height,
        "decoded_width": None,
        "decoded_height": None,
        "output_mime_type": "image/webp",
        "output_extension": ".webp",
        "output_size_bytes": None,
        "output_sha256": None,
        "webp_verified": False,
        "conversion_status": "not_attempted",
        "warnings": list(source.warnings),
        "blocking_issues": [],
    }


def _expected_dimensions(
    source: download_core.VerifiedDownloadedMediaArtifact,
) -> tuple[int, int]:
    width = source.expected_image_width
    height = source.expected_image_height
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height > MAX_DECODE_PIXELS
    ):
        raise _ConversionBlocked("webp_expected_dimensions_unsafe")
    return width, height


def _open_and_load_source(
    path: Path,
    *,
    mime_type: str,
    expected_width: int,
    expected_height: int,
) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                decoded_width, decoded_height = opened.size
                if (
                    type(decoded_width) is not int
                    or type(decoded_height) is not int
                    or decoded_width <= 0
                    or decoded_height <= 0
                    or decoded_width * decoded_height > MAX_DECODE_PIXELS
                ):
                    raise _ConversionBlocked(
                        "webp_decode_dimensions_unsafe",
                        decoded_width=decoded_width,
                        decoded_height=decoded_height,
                    )
                if opened.format != _SOURCE_FORMATS[mime_type]:
                    raise _ConversionBlocked(
                        "source_decoded_format_mismatch",
                        decoded_width=decoded_width,
                        decoded_height=decoded_height,
                    )
                if (decoded_width, decoded_height) != (expected_width, expected_height):
                    raise _ConversionBlocked(
                        "source_decoded_dimensions_mismatch",
                        decoded_width=decoded_width,
                        decoded_height=decoded_height,
                    )
                opened.load()
                if opened.size != (expected_width, expected_height):
                    raise _ConversionBlocked(
                        "source_decoded_dimensions_mismatch",
                        decoded_width=opened.size[0],
                        decoded_height=opened.size[1],
                    )
                return opened.copy()
    except _ConversionBlocked:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _ConversionBlocked("webp_decode_dimensions_unsafe") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise _ConversionBlocked("source_image_decode_failed") from None


def _normalized_pixel_image(source: Image.Image) -> Image.Image:
    has_alpha = "A" in source.getbands() or (
        source.mode == "P" and "transparency" in source.info
    )
    return source.convert("RGBA" if has_alpha else "RGB")


def _encode_webp(source: Image.Image, target: Path) -> None:
    normalized = _normalized_pixel_image(source)
    try:
        with target.open("xb") as output:
            normalized.save(
                output,
                format="WEBP",
                quality=WEBP_QUALITY,
                method=WEBP_METHOD,
            )
    except OSError:
        raise _ConversionBlocked("webp_output_write_failed") from None
    finally:
        normalized.close()


def _copy_existing_webp(source: Path, target: Path) -> None:
    try:
        with source.open("rb") as source_stream, target.open("xb") as output_stream:
            shutil.copyfileobj(source_stream, output_stream, length=_CHUNK_SIZE)
    except OSError:
        raise _ConversionBlocked("webp_output_write_failed") from None


def _verify_final_webp(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
) -> tuple[int, str]:
    try:
        info = path.lstat()
    except OSError:
        raise _ConversionBlocked("webp_output_file_unavailable") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or path.suffix.casefold() != ".webp"
    ):
        raise _ConversionBlocked("webp_output_path_invalid")
    try:
        size, digest, prefix = _read_size_sha256_and_magic(path)
    except VerifiedWebPConversionError:
        raise _ConversionBlocked("webp_output_file_unavailable") from None
    if size <= 0 or not _webp_magic_matches(prefix):
        raise _ConversionBlocked("webp_output_signature_mismatch")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as output:
                if output.format != "WEBP":
                    raise _ConversionBlocked("webp_output_decode_failed")
                width, height = output.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > MAX_DECODE_PIXELS
                ):
                    raise _ConversionBlocked("webp_output_dimensions_unsafe")
                if (width, height) != (expected_width, expected_height):
                    raise _ConversionBlocked("webp_output_dimensions_mismatch")
                output.load()
                if output.size != (expected_width, expected_height):
                    raise _ConversionBlocked("webp_output_dimensions_mismatch")
    except _ConversionBlocked:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _ConversionBlocked("webp_output_dimensions_unsafe") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise _ConversionBlocked("webp_output_decode_failed") from None
    return size, digest


def _increment_failure_counters(counters: dict[str, int], code: str) -> None:
    counters["conversion_failed"] += 1
    if code in {
        "source_image_decode_failed",
        "source_decoded_format_mismatch",
        "webp_decode_dimensions_unsafe",
    }:
        counters["decode_failed"] += 1
    if code == "source_decoded_dimensions_mismatch":
        counters["dimension_mismatch"] += 1
    if code in {"source_webp_signature_mismatch", "webp_output_signature_mismatch"}:
        counters["webp_signature_mismatch"] += 1
    if code in {
        "webp_output_decode_failed",
        "webp_output_dimensions_mismatch",
        "webp_output_dimensions_unsafe",
    }:
        counters["webp_decode_failed"] += 1
    if code == "webp_output_dimensions_mismatch":
        counters["dimension_mismatch"] += 1


class VerifiedWebPConversionBatchResult:
    """Safe audit plus lifecycle-managed, memory-only WebP authorities."""

    __slots__ = (
        "_status",
        "_summary",
        "_results",
        "_artifacts",
        "_paths",
        "_workspace",
        "_cleaned",
        "_cleaned_count",
    )

    def __init__(
        self,
        *,
        status: str,
        summary: Mapping[str, int],
        results: tuple[Mapping[str, object], ...],
        artifacts: tuple[VerifiedWebPArtifact, ...],
        paths: tuple[Path, ...],
        workspace: Path | None,
        cleaned_count: int = 0,
    ) -> None:
        self._status = status
        self._summary = dict(summary)
        self._results = tuple(dict(item) for item in results)
        self._artifacts = artifacts
        self._paths = paths
        self._workspace = workspace
        self._cleaned = status != "ok"
        self._cleaned_count = cleaned_count

    @property
    def status(self) -> str:
        return self._status

    @property
    def artifacts(self) -> tuple[VerifiedWebPArtifact, ...]:
        return () if self._cleaned else self._artifacts

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned_count += _cleanup_files(self._paths, self._workspace)
        self._cleaned = True

    def __enter__(self) -> VerifiedWebPConversionBatchResult:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()

    def to_safe_report_dict(self) -> dict[str, object]:
        summary = {
            **self._summary,
            "output_files_cleaned": self._cleaned_count,
            "authoritative_webp_artifacts": len(self.artifacts),
        }
        report = {
            "status": self.status,
            "policy_version": POLICY_VERSION,
            "encoder_profile_version": ENCODER_PROFILE_VERSION,
            "summary": summary,
            "results": [dict(item) for item in self._results],
            **_ZERO_EXTERNAL_COUNTERS,
        }
        safe = sanitize_report_data(report, Redactor())
        drive_manifest_core._assert_report_safe(safe)
        return json.loads(json.dumps(safe, ensure_ascii=False))

    def __repr__(self) -> str:
        return (
            f"VerifiedWebPConversionBatchResult(status={self.status!r}, "
            f"artifacts_count={len(self.artifacts)})"
        )

    def __reduce__(self):
        raise TypeError("verified_webp_conversion_batch_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("verified_webp_conversion_batch_not_serializable")


def _convert_verified_media_to_webp_impl(
    sources_value: (
        download_core.VerifiedDownloadedMediaArtifact
        | tuple[download_core.VerifiedDownloadedMediaArtifact, ...]
    ),
    *,
    workspace_parent: Path | None,
    lifecycle: _WebPWorkspaceLifecycle,
) -> VerifiedWebPConversionBatchResult:
    sources = _normalize_sources(sources_value)
    parent = _safe_workspace_parent(workspace_parent)
    workspace = _create_workspace(parent)
    lifecycle.workspace = workspace
    paths = lifecycle.paths
    counters = {
        "source_artifacts_received": len(sources),
        "conversion_attempted": 0,
        "conversion_verified": 0,
        "conversion_failed": 0,
        "converted_from_jpeg": 0,
        "converted_from_png": 0,
        "validated_existing_webp": 0,
        "decode_verified": 0,
        "decode_failed": 0,
        "dimension_verified": 0,
        "dimension_mismatch": 0,
        "webp_signature_verified": 0,
        "webp_signature_mismatch": 0,
        "webp_decode_verified": 0,
        "webp_decode_failed": 0,
        "output_files_created": 0,
        "output_files_cleaned": 0,
        "source_total_bytes": sum(item.actual_size_bytes for item in sources),
        "output_total_bytes": 0,
        "authoritative_webp_artifacts": 0,
        "conversion_requests_performed": 0,
        **_ZERO_EXTERNAL_COUNTERS,
    }
    audits: list[dict[str, object]] = []
    artifacts: list[VerifiedWebPArtifact] = []
    blocked = False

    for index, source in enumerate(sources):
        audit = _safe_item(source)
        if blocked:
            audit["blocking_issues"] = ["batch_aborted_after_failure"]
            audits.append(audit)
            continue
        counters["conversion_attempted"] += 1
        target = workspace / f"webp-{index:03d}-{source.selection_position:03d}.webp"
        blocker: _ConversionBlocked | None = None
        decoded: Image.Image | None = None
        target_created_counted = False
        try:
            source_path = download_core._local_source_path_for_conversion(source)
            mime_type = source.source_mime_type
            if mime_type not in _SOURCE_FORMATS:
                raise _ConversionBlocked("webp_source_mime_not_allowed")
            expected_width, expected_height = _expected_dimensions(source)
            if mime_type == "image/webp":
                _, _, source_prefix = _read_size_sha256_and_magic(source_path)
                if not _webp_magic_matches(source_prefix):
                    raise _ConversionBlocked("source_webp_signature_mismatch")
            decoded = _open_and_load_source(
                source_path,
                mime_type=mime_type,
                expected_width=expected_width,
                expected_height=expected_height,
            )
            audit["decoded_width"] = decoded.size[0]
            audit["decoded_height"] = decoded.size[1]
            counters["decode_verified"] += 1
            counters["dimension_verified"] += 1
            paths.append(target)
            if mime_type in _CONVERTIBLE_MIME_TYPES:
                counters["conversion_requests_performed"] += 1
                _encode_webp(decoded, target)
                if mime_type == "image/jpeg":
                    counters["converted_from_jpeg"] += 1
                else:
                    counters["converted_from_png"] += 1
                profile = ENCODER_PROFILE_VERSION
            else:
                _copy_existing_webp(source_path, target)
                counters["validated_existing_webp"] += 1
                profile = EXISTING_WEBP_PROFILE_VERSION
            counters["output_files_created"] += 1
            target_created_counted = True
            output_size, output_sha256 = _verify_final_webp(
                target,
                expected_width=expected_width,
                expected_height=expected_height,
            )
            counters["webp_signature_verified"] += 1
            counters["webp_decode_verified"] += 1
            artifact = VerifiedWebPArtifact(
                _WEBP_ARTIFACT_CAPABILITY,
                source=source,
                local_webp_path=target,
                workspace_root=workspace,
                output_size_bytes=output_size,
                output_sha256=output_sha256,
                image_width=expected_width,
                image_height=expected_height,
                conversion_action=str(audit["conversion_action"]),
                encoder_profile_version=profile,
            )
            artifacts.append(artifact)
            counters["conversion_verified"] += 1
            counters["output_total_bytes"] += output_size
            audit.update(artifact.to_safe_dict())
            audit["expected_width"] = expected_width
            audit["expected_height"] = expected_height
            audit["decoded_width"] = decoded.size[0]
            audit["decoded_height"] = decoded.size[1]
            audit["conversion_status"] = "webp_verified"
        except download_core.SecureMediaDownloadError:
            blocker = _ConversionBlocked("source_authority_revalidation_failed")
        except VerifiedWebPConversionError:
            blocker = _ConversionBlocked("webp_source_verification_failed")
        except _ConversionBlocked as error:
            blocker = error
        except Exception:
            blocker = _ConversionBlocked("webp_conversion_failed")
        finally:
            if decoded is not None:
                decoded.close()
            if not target_created_counted:
                try:
                    target_info = target.lstat()
                except OSError:
                    pass
                else:
                    if stat.S_ISREG(target_info.st_mode):
                        counters["output_files_created"] += 1
        if blocker is not None:
            audit["decoded_width"] = blocker.decoded_width
            audit["decoded_height"] = blocker.decoded_height
            audit["conversion_status"] = "conversion_blocked"
            audit["blocking_issues"] = [blocker.code]
            _increment_failure_counters(counters, blocker.code)
            blocked = True
        audits.append(audit)

    if blocked:
        cleaned = lifecycle.cleanup()
        counters["output_files_cleaned"] = cleaned
        counters["authoritative_webp_artifacts"] = 0
        return VerifiedWebPConversionBatchResult(
            status="blocked",
            summary=counters,
            results=tuple(audits),
            artifacts=(),
            paths=(),
            workspace=None,
            cleaned_count=cleaned,
        )
    counters["authoritative_webp_artifacts"] = len(artifacts)
    return VerifiedWebPConversionBatchResult(
        status="ok",
        summary=counters,
        results=tuple(audits),
        artifacts=tuple(artifacts),
        paths=tuple(paths),
        workspace=workspace,
    )


def convert_verified_media_to_webp(
    sources: (
        download_core.VerifiedDownloadedMediaArtifact
        | tuple[download_core.VerifiedDownloadedMediaArtifact, ...]
    ),
    *,
    workspace_parent: Path | None = None,
) -> VerifiedWebPConversionBatchResult:
    """Convert/validate one authoritative batch and clean on interruption."""

    lifecycle = _WebPWorkspaceLifecycle()
    try:
        return _convert_verified_media_to_webp_impl(
            sources,
            workspace_parent=workspace_parent,
            lifecycle=lifecycle,
        )
    except BaseException:
        lifecycle.cleanup()
        raise
