"""Capability-gated, bounded downloads for selected Google Drive media.

The core accepts only in-memory ``SecureSelectedMediaHandle`` capabilities.
It streams one source at a time into an OS-temporary workspace, verifies the
downloaded bytes, and exposes local paths only through a package-private,
revalidating helper intended for the future conversion core.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from . import google_drive_folder_manifest as drive_manifest_core
from . import secure_selected_media_handle as handle_core
from .google_api import (
    GoogleDriveContentDownloadError,
    GoogleDriveContentDownloadReceipt,
    GoogleDriveContentSinkError,
)
from .report import sanitize_report_data
from .sanitization import Redactor


POLICY_VERSION = "xxxxdoll-secure-media-download-v1"
DOWNLOAD_CHUNK_SIZE = 256 * 1024
MAX_HANDLES_PER_BATCH = 200
MAX_SOURCE_FILE_BYTES = 100 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS = 3
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_CAPABILITY = object()
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_TEMP_SOURCE_NAME = re.compile(
    r"source-[0-9]{3}-[0-9]+\.source\.(?:jpg|png|webp)\Z",
    re.ASCII,
)
_ZERO_EXTERNAL_COUNTERS = {
    "conversion_requests_performed": 0,
    "wordpress_upload_requests_performed": 0,
    "external_write_requests_performed": 0,
}


class SecureMediaDownloadError(ValueError):
    """Fixed safe error codes only; never paths or provider identities."""


class DriveContentDownloadGateway(Protocol):
    def download_file(
        self,
        provider_file_id: str,
        sink: object,
        *,
        chunk_size: int,
    ) -> GoogleDriveContentDownloadReceipt: ...


DownloadProgressCallback = Callable[[Mapping[str, object]], None]


class _BoundedHashingSink:
    """Write-only sink that hashes incrementally and rejects oversized chunks."""

    __slots__ = ("_stream", "_hash", "_prefix", "_max_bytes", "bytes_written")

    def __init__(self, stream: object, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self.reset()

    def reset(self) -> None:
        try:
            self._stream.seek(0)
            self._stream.truncate(0)
        except OSError:
            raise GoogleDriveContentSinkError("download_source_write_failed") from None
        self._hash = hashlib.md5(usedforsecurity=False)
        self._prefix = bytearray()
        self.bytes_written = 0

    def write(self, value: object) -> int:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise GoogleDriveContentSinkError("download_chunk_must_be_bytes")
        size = len(value)
        if size > DOWNLOAD_CHUNK_SIZE:
            raise GoogleDriveContentSinkError("download_chunk_limit_exceeded")
        if self.bytes_written + size > self._max_bytes:
            raise GoogleDriveContentSinkError("download_source_file_too_large")
        if len(self._prefix) < 12:
            self._prefix.extend(bytes(value[: 12 - len(self._prefix)]))
        self._hash.update(value)
        try:
            written = self._stream.write(value)
        except OSError:
            raise GoogleDriveContentSinkError("download_source_write_failed") from None
        if written != size:
            raise GoogleDriveContentSinkError("download_source_write_incomplete")
        self.bytes_written += written
        return written

    @property
    def md5_checksum(self) -> str:
        return self._hash.hexdigest()

    @property
    def signature_prefix(self) -> bytes:
        return bytes(self._prefix)


class VerifiedDownloadedMediaArtifact:
    """Immutable, non-serializable authority over one verified local source."""

    __slots__ = (
        "__capability", "__local_source_path", "__workspace_root",
        "_sku", "_selection_position", "_image_role", "_folder_role",
        "_safe_name", "_source_mime_type", "_source_extension",
        "_expected_size_bytes", "_actual_size_bytes", "_expected_md5_checksum",
        "_actual_md5_checksum", "_file_id_fingerprint", "_expected_image_width",
        "_expected_image_height", "_source_verified", "_warnings",
        "_blocking_issues",
    )

    def __init__(
        self,
        capability: object,
        *,
        handle: handle_core.SecureSelectedMediaHandle,
        local_source_path: Path,
        workspace_root: Path,
        source_extension: str,
        actual_size_bytes: int,
        actual_md5_checksum: str,
    ) -> None:
        if capability is not _ARTIFACT_CAPABILITY or not handle_core._valid_handle(handle):
            raise SecureMediaDownloadError("verified_download_artifact_factory_required")
        object.__setattr__(self, "_VerifiedDownloadedMediaArtifact__capability", capability)
        object.__setattr__(self, "_VerifiedDownloadedMediaArtifact__local_source_path", local_source_path)
        object.__setattr__(self, "_VerifiedDownloadedMediaArtifact__workspace_root", workspace_root)
        object.__setattr__(self, "_sku", handle.sku)
        object.__setattr__(self, "_selection_position", handle.selection_position)
        object.__setattr__(self, "_image_role", handle.image_role.value)
        object.__setattr__(self, "_folder_role", handle.folder_role.value)
        object.__setattr__(self, "_safe_name", handle.safe_name)
        object.__setattr__(self, "_source_mime_type", handle.source_mime_type)
        object.__setattr__(self, "_source_extension", source_extension)
        object.__setattr__(self, "_expected_size_bytes", handle.size_bytes)
        object.__setattr__(self, "_actual_size_bytes", actual_size_bytes)
        object.__setattr__(self, "_expected_md5_checksum", handle.md5_checksum)
        object.__setattr__(self, "_actual_md5_checksum", actual_md5_checksum)
        object.__setattr__(self, "_file_id_fingerprint", handle.file_id_fingerprint)
        object.__setattr__(self, "_expected_image_width", handle.image_width)
        object.__setattr__(self, "_expected_image_height", handle.image_height)
        object.__setattr__(self, "_source_verified", True)
        object.__setattr__(self, "_warnings", tuple(handle.warnings))
        object.__setattr__(self, "_blocking_issues", ())

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("verified_downloaded_media_artifact_is_immutable")

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
    def source_mime_type(self) -> str:
        return self._source_mime_type

    @property
    def source_extension(self) -> str:
        return self._source_extension

    @property
    def expected_size_bytes(self) -> int | None:
        return self._expected_size_bytes

    @property
    def actual_size_bytes(self) -> int:
        return self._actual_size_bytes

    @property
    def expected_md5_checksum(self) -> str:
        return self._expected_md5_checksum

    @property
    def actual_md5_checksum(self) -> str:
        return self._actual_md5_checksum

    @property
    def file_id_fingerprint(self) -> str:
        return self._file_id_fingerprint

    @property
    def expected_image_width(self) -> int | None:
        return self._expected_image_width

    @property
    def expected_image_height(self) -> int | None:
        return self._expected_image_height

    @property
    def source_verified(self) -> bool:
        return self._source_verified

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @property
    def blocking_issues(self) -> tuple[str, ...]:
        return self._blocking_issues

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "policy_version": POLICY_VERSION,
            "sku": self.sku,
            "selection_position": self.selection_position,
            "image_role": self.image_role,
            "folder_role": self.folder_role,
            "safe_name": self.safe_name,
            "source_mime_type": self.source_mime_type,
            "source_extension": self.source_extension,
            "expected_size_bytes": self.expected_size_bytes,
            "actual_size_bytes": self.actual_size_bytes,
            "expected_md5_checksum": self.expected_md5_checksum,
            "actual_md5_checksum": self.actual_md5_checksum,
            "file_id_fingerprint": self.file_id_fingerprint,
            "expected_image_width": self.expected_image_width,
            "expected_image_height": self.expected_image_height,
            "source_verified": self.source_verified,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }

    def __repr__(self) -> str:
        return f"VerifiedDownloadedMediaArtifact({self.to_safe_dict()!r})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("verified_downloaded_media_artifact_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("verified_downloaded_media_artifact_not_serializable")


def _artifact_paths(
    artifact: VerifiedDownloadedMediaArtifact,
) -> tuple[Path, Path]:
    if type(artifact) is not VerifiedDownloadedMediaArtifact:
        raise SecureMediaDownloadError("verified_downloaded_media_artifact_required")
    try:
        capability = object.__getattribute__(
            artifact, "_VerifiedDownloadedMediaArtifact__capability"
        )
        path = object.__getattribute__(
            artifact, "_VerifiedDownloadedMediaArtifact__local_source_path"
        )
        workspace = object.__getattribute__(
            artifact, "_VerifiedDownloadedMediaArtifact__workspace_root"
        )
    except AttributeError:
        raise SecureMediaDownloadError("invalid_verified_downloaded_media_artifact") from None
    if (
        capability is not _ARTIFACT_CAPABILITY
        or not isinstance(path, Path)
        or not isinstance(workspace, Path)
        or artifact.source_verified is not True
        or artifact.source_extension != _MIME_EXTENSIONS.get(artifact.source_mime_type)
        or not _valid_md5(artifact.expected_md5_checksum)
        or not _valid_md5(artifact.actual_md5_checksum)
        or type(artifact.actual_size_bytes) is not int
        or not 0 <= artifact.actual_size_bytes <= MAX_SOURCE_FILE_BYTES
        or type(artifact.selection_position) is not int
        or artifact.selection_position < 0
        or _TEMP_SOURCE_NAME.fullmatch(path.name) is None
        or not workspace.name.startswith("xxxxdoll-secure-media-")
    ):
        raise SecureMediaDownloadError("invalid_verified_downloaded_media_artifact")
    return path, workspace


def _valid_md5(value: object) -> bool:
    return type(value) is str and drive_manifest_core._MD5_PATTERN.fullmatch(value) is not None


def _local_source_path_for_conversion(
    artifact: VerifiedDownloadedMediaArtifact,
) -> Path:
    """Reverify local bytes immediately before granting conversion access."""

    path, workspace = _artifact_paths(artifact)
    try:
        if path.parent != workspace or not path.is_relative_to(workspace):
            raise SecureMediaDownloadError("downloaded_artifact_path_invalid")
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SecureMediaDownloadError("downloaded_artifact_path_invalid")
        digest = hashlib.md5(usedforsecurity=False)
        total = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SOURCE_FILE_BYTES:
                    raise SecureMediaDownloadError("downloaded_artifact_local_content_changed")
                digest.update(chunk)
    except SecureMediaDownloadError:
        raise
    except OSError:
        raise SecureMediaDownloadError("downloaded_artifact_local_file_unavailable") from None
    if total != artifact.actual_size_bytes or digest.hexdigest() != artifact.actual_md5_checksum:
        raise SecureMediaDownloadError("downloaded_artifact_local_content_changed")
    return path


def _safe_item(handle: handle_core.SecureSelectedMediaHandle) -> dict[str, object]:
    return {
        "sku": handle.sku,
        "selection_position": handle.selection_position,
        "image_role": handle.image_role.value,
        "folder_role": handle.folder_role.value,
        "safe_name": handle.safe_name,
        "source_mime_type": handle.source_mime_type,
        "source_extension": _MIME_EXTENSIONS.get(handle.source_mime_type),
        "expected_size_bytes": handle.size_bytes,
        "actual_size_bytes": None,
        "expected_md5_checksum": handle.md5_checksum,
        "actual_md5_checksum": None,
        "file_id_fingerprint": handle.file_id_fingerprint,
        "expected_image_width": handle.image_width,
        "expected_image_height": handle.image_height,
        "source_verified": False,
        "download_status": "not_attempted",
        "download_attempts": 0,
        "warnings": list(handle.warnings),
        "blocking_issues": [],
    }


def _signature_matches(mime_type: str, prefix: bytes) -> bool:
    if mime_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    return False


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
                raise SecureMediaDownloadError("download_workspace_parent_invalid")
        parent = absolute.resolve(strict=True)
        info = parent.lstat()
    except SecureMediaDownloadError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SecureMediaDownloadError("download_workspace_parent_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or parent == _PROJECT_ROOT
        or _PROJECT_ROOT in parent.parents
    ):
        raise SecureMediaDownloadError("download_workspace_parent_invalid")
    return parent


def _cleanup_files(paths: tuple[Path, ...], workspace: Path | None) -> int:
    cleaned = 0
    for path in paths:
        try:
            path.unlink(missing_ok=False)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        else:
            cleaned += 1
    if workspace is not None:
        try:
            workspace.rmdir()
        except OSError:
            pass
    return cleaned


class SecureMediaDownloadBatchResult:
    """Safe audit plus lifecycle-managed, memory-only verified artifacts."""

    __slots__ = (
        "_status", "_summary", "_results", "_artifacts", "_paths",
        "_workspace", "_cleaned", "_cleaned_count",
    )

    def __init__(
        self,
        *,
        status: str,
        summary: Mapping[str, int],
        results: tuple[Mapping[str, object], ...],
        artifacts: tuple[VerifiedDownloadedMediaArtifact, ...],
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
    def artifacts(self) -> tuple[VerifiedDownloadedMediaArtifact, ...]:
        return () if self._cleaned else self._artifacts

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned_count += _cleanup_files(self._paths, self._workspace)
        self._cleaned = True

    def __enter__(self) -> SecureMediaDownloadBatchResult:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()

    def to_safe_report_dict(self) -> dict[str, object]:
        summary = {
            **self._summary,
            "source_files_cleaned": self._cleaned_count,
            "authoritative_artifacts": len(self.artifacts),
        }
        report = {
            "status": self.status,
            "policy_version": POLICY_VERSION,
            "summary": summary,
            "results": [dict(item) for item in self._results],
            **_ZERO_EXTERNAL_COUNTERS,
        }
        safe = sanitize_report_data(report, Redactor())
        drive_manifest_core._assert_report_safe(safe)
        return json.loads(json.dumps(safe, ensure_ascii=False))

    def __repr__(self) -> str:
        return (
            f"SecureMediaDownloadBatchResult(status={self.status!r}, "
            f"artifacts_count={len(self.artifacts)})"
        )

    def __reduce__(self):
        raise TypeError("secure_media_download_batch_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("secure_media_download_batch_not_serializable")


def _normalized_handles(
    value: handle_core.SecureSelectedMediaHandle | tuple[handle_core.SecureSelectedMediaHandle, ...],
) -> tuple[handle_core.SecureSelectedMediaHandle, ...]:
    if type(value) is handle_core.SecureSelectedMediaHandle:
        handles = (value,)
    elif type(value) is tuple:
        handles = value
    else:
        raise SecureMediaDownloadError("secure_selected_media_handles_required")
    if len(handles) > MAX_HANDLES_PER_BATCH:
        raise SecureMediaDownloadError("secure_media_download_batch_limit_exceeded")
    if not handles:
        raise SecureMediaDownloadError("secure_selected_media_handles_required")
    if any(type(item) is not handle_core.SecureSelectedMediaHandle or not handle_core._valid_handle(item) for item in handles):
        raise SecureMediaDownloadError("secure_selected_media_handles_required")
    keys = tuple((item.sku, item.selection_position) for item in handles)
    if keys != tuple(sorted(keys)):
        raise SecureMediaDownloadError("download_handles_not_canonical_order")
    if len(set(keys)) != len(keys):
        raise SecureMediaDownloadError("duplicate_download_handle_identity")
    return handles


class _DownloadWorkspaceLifecycle:
    __slots__ = ("workspace", "paths")

    def __init__(self) -> None:
        self.workspace: Path | None = None
        self.paths: list[Path] = []

    def cleanup(self) -> int:
        return _cleanup_files(tuple(self.paths), self.workspace)


def _emit_download_progress(
    callback: DownloadProgressCallback | None,
    handle: handle_core.SecureSelectedMediaHandle,
    *,
    current_index: int,
    total_items: int,
    status: str,
) -> None:
    if callback is None:
        return
    event = {
        "current_index": current_index,
        "total_items": total_items,
        "sku": handle.sku,
        "selection_position": handle.selection_position,
        "status": status,
    }
    try:
        callback(event)
    except Exception:
        raise SecureMediaDownloadError("download_progress_callback_failed") from None


def _download_secure_media_impl(
    handles: handle_core.SecureSelectedMediaHandle | tuple[handle_core.SecureSelectedMediaHandle, ...],
    gateway: DriveContentDownloadGateway,
    *,
    workspace_parent: Path | None = None,
    progress_callback: DownloadProgressCallback | None = None,
    lifecycle: _DownloadWorkspaceLifecycle,
) -> SecureMediaDownloadBatchResult:
    """Unprotected implementation; the public wrapper owns lifecycle cleanup."""

    selected = _normalized_handles(handles)
    parent = _safe_workspace_parent(workspace_parent)
    try:
        workspace = Path(tempfile.mkdtemp(
            prefix="xxxxdoll-secure-media-",
            dir=None if parent is None else str(parent),
        ))
        lifecycle.workspace = workspace
    except OSError:
        raise SecureMediaDownloadError("download_workspace_creation_failed") from None

    audits: list[dict[str, object]] = []
    artifacts: list[VerifiedDownloadedMediaArtifact] = []
    paths = lifecycle.paths
    counters = {
        "handles_received": len(selected),
        "downloads_attempted": 0,
        "downloads_verified": 0,
        "downloads_failed": 0,
        "checksum_verified": 0,
        "checksum_mismatch": 0,
        "size_verified": 0,
        "size_mismatch": 0,
        "signature_verified": 0,
        "signature_mismatch": 0,
        "source_files_created": 0,
        "download_requests_performed": 0,
        "bytes_downloaded": 0,
        **_ZERO_EXTERNAL_COUNTERS,
    }
    blocked = False
    for index, handle in enumerate(selected):
        audit = _safe_item(handle)
        if blocked:
            audit["download_status"] = "not_attempted"
            audit["blocking_issues"] = ["batch_aborted_after_failure"]
            audits.append(audit)
            continue
        _emit_download_progress(
            progress_callback,
            handle,
            current_index=index + 1,
            total_items=len(selected),
            status="download_started",
        )
        extension = _MIME_EXTENSIONS.get(handle.source_mime_type)
        blocker: str | None = None
        if extension is None:
            blocker = "download_source_mime_not_allowed"
        elif handle.size_bytes is not None and handle.size_bytes > MAX_SOURCE_FILE_BYTES:
            blocker = "download_source_file_too_large"
        if blocker is not None:
            audit["download_status"] = "download_blocked"
            audit["blocking_issues"] = [blocker]
            counters["downloads_failed"] += 1
            audits.append(audit)
            blocked = True
            _emit_download_progress(
                progress_callback,
                handle,
                current_index=index + 1,
                total_items=len(selected),
                status="download_blocked",
            )
            continue
        source_path = workspace / (
            f"source-{index:03d}-{handle.selection_position:03d}.source{extension}"
        )
        paths.append(source_path)
        try:
            stream = source_path.open("xb")
        except OSError:
            audit["download_status"] = "download_blocked"
            audit["blocking_issues"] = ["download_source_file_creation_failed"]
            counters["downloads_failed"] += 1
            audits.append(audit)
            blocked = True
            _emit_download_progress(
                progress_callback,
                handle,
                current_index=index + 1,
                total_items=len(selected),
                status="download_blocked",
            )
            continue
        counters["source_files_created"] += 1
        try:
            sink = _BoundedHashingSink(stream, MAX_SOURCE_FILE_BYTES)
        except GoogleDriveContentSinkError:
            stream.close()
            audit["download_status"] = "download_blocked"
            audit["blocking_issues"] = ["download_source_write_failed"]
            counters["downloads_failed"] += 1
            audits.append(audit)
            blocked = True
            _emit_download_progress(
                progress_callback,
                handle,
                current_index=index + 1,
                total_items=len(selected),
                status="download_blocked",
            )
            continue
        counters["downloads_attempted"] += 1
        try:
            provider_file_id = handle_core._provider_file_id_for_download(handle)
            for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
                audit["download_attempts"] = attempt
                if attempt > 1:
                    try:
                        sink.reset()
                    except GoogleDriveContentSinkError:
                        blocker = "download_source_write_failed"
                        break
                try:
                    receipt = gateway.download_file(
                        provider_file_id,
                        sink,
                        chunk_size=DOWNLOAD_CHUNK_SIZE,
                    )
                except GoogleDriveContentSinkError as error:
                    counters["bytes_downloaded"] += sink.bytes_written
                    counters["download_requests_performed"] += 1
                    blocker = str(error) if str(error) in {
                        "download_chunk_limit_exceeded",
                        "download_chunk_must_be_bytes",
                        "download_source_file_too_large",
                        "download_source_write_incomplete",
                        "download_source_write_failed",
                    } else "download_stream_rejected"
                    break
                except GoogleDriveContentDownloadError as error:
                    counters["bytes_downloaded"] += sink.bytes_written
                    counters["download_requests_performed"] += max(0, error.requests_performed)
                    if error.transient and attempt < MAX_DOWNLOAD_ATTEMPTS:
                        continue
                    blocker = error.code if error.code in {
                        "drive_download_forbidden", "drive_download_not_found",
                        "drive_download_transient_error", "drive_download_provider_error",
                        "drive_download_sink_contract_violation",
                    } else "drive_download_provider_error"
                    break
                except Exception:
                    counters["bytes_downloaded"] += sink.bytes_written
                    counters["download_requests_performed"] += 1
                    blocker = "drive_download_provider_error"
                    break
                if type(receipt) is not GoogleDriveContentDownloadReceipt:
                    blocker = "download_gateway_contract_violation"
                    counters["download_requests_performed"] += 1
                    counters["bytes_downloaded"] += sink.bytes_written
                    break
                if (
                    type(receipt.requests_performed) is not int
                    or receipt.requests_performed < 1
                    or type(receipt.bytes_written) is not int
                    or receipt.bytes_written != sink.bytes_written
                ):
                    blocker = "download_gateway_contract_violation"
                    counters["download_requests_performed"] += (
                        receipt.requests_performed
                        if type(receipt.requests_performed) is int
                        and receipt.requests_performed > 0
                        else 1
                    )
                    counters["bytes_downloaded"] += sink.bytes_written
                    break
                counters["download_requests_performed"] += receipt.requests_performed
                counters["bytes_downloaded"] += sink.bytes_written
                break
        except handle_core.SecureSelectedMediaHandleError:
            blocker = "secure_selected_media_handle_invalid"
        finally:
            stream.close()
        actual_md5 = sink.md5_checksum
        actual_size = sink.bytes_written
        audit["actual_md5_checksum"] = actual_md5
        audit["actual_size_bytes"] = actual_size
        if blocker is None and actual_md5.casefold() != handle.md5_checksum.casefold():
            blocker = "downloaded_content_checksum_mismatch"
            counters["checksum_mismatch"] += 1
        elif blocker is None:
            counters["checksum_verified"] += 1
        if blocker is None and handle.size_bytes is not None and actual_size != handle.size_bytes:
            blocker = "downloaded_content_size_mismatch"
            counters["size_mismatch"] += 1
        elif blocker is None and handle.size_bytes is not None:
            counters["size_verified"] += 1
        if blocker is None and not _signature_matches(handle.source_mime_type, sink.signature_prefix):
            blocker = "downloaded_content_signature_mismatch"
            counters["signature_mismatch"] += 1
        elif blocker is None:
            counters["signature_verified"] += 1
        if blocker is None:
            try:
                artifact = VerifiedDownloadedMediaArtifact(
                    _ARTIFACT_CAPABILITY,
                    handle=handle,
                    local_source_path=source_path,
                    workspace_root=workspace,
                    source_extension=extension,
                    actual_size_bytes=actual_size,
                    actual_md5_checksum=actual_md5,
                )
            except SecureMediaDownloadError:
                blocker = "verified_download_artifact_creation_failed"
            else:
                artifacts.append(artifact)
                counters["downloads_verified"] += 1
                audit.update(artifact.to_safe_dict())
                audit["download_status"] = "downloaded_verified"
        if blocker is not None:
            counters["downloads_failed"] += 1
            audit["download_status"] = "download_blocked"
            audit["blocking_issues"] = [blocker]
            blocked = True
            progress_status = "download_blocked"
        else:
            progress_status = "download_verified"
        audits.append(audit)
        _emit_download_progress(
            progress_callback,
            handle,
            current_index=index + 1,
            total_items=len(selected),
            status=progress_status,
        )

    if blocked:
        cleaned = _cleanup_files(tuple(paths), workspace)
        return SecureMediaDownloadBatchResult(
            status="blocked", summary=counters, results=tuple(audits),
            artifacts=(), paths=(), workspace=None, cleaned_count=cleaned,
        )
    return SecureMediaDownloadBatchResult(
        status="ok", summary=counters, results=tuple(audits),
        artifacts=tuple(artifacts), paths=tuple(paths), workspace=workspace,
    )


def download_secure_media(
    handles: handle_core.SecureSelectedMediaHandle | tuple[handle_core.SecureSelectedMediaHandle, ...],
    gateway: DriveContentDownloadGateway,
    *,
    workspace_parent: Path | None = None,
    progress_callback: DownloadProgressCallback | None = None,
) -> SecureMediaDownloadBatchResult:
    """Stream one batch and clean every tracked path on any BaseException."""

    if progress_callback is not None and not callable(progress_callback):
        raise SecureMediaDownloadError("invalid_download_progress_callback")
    lifecycle = _DownloadWorkspaceLifecycle()
    try:
        return _download_secure_media_impl(
            handles,
            gateway,
            workspace_parent=workspace_parent,
            progress_callback=progress_callback,
            lifecycle=lifecycle,
        )
    except BaseException:
        lifecycle.cleanup()
        raise
