"""Safe local workspace publication for frozen Woo batch plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import single_product_staging_package_dry_run as package_io
from . import woocommerce_target_snapshot as target_snapshot


BATCH_REPORT_FILENAME = "woo-batch-plan.json"
ITEMS_DIRECTORY = "items"
AUTHORITIES_DIRECTORY = "authorities"
REPORTS_DIRECTORY = "reports"
PLAN_FILENAME = "woo-apply-plan.json"
TEMP_PREFIX = ".woo-batch-build-"
PUBLISH_RESERVATION_PREFIX = ".woo-batch-publish-"
MAX_BATCH_REPORT_BYTES = 16 * 1024 * 1024

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]+:", re.IGNORECASE)
class WooBatchWorkspaceError(ValueError):
    """Fixed-code local batch workspace failure."""


@dataclass(frozen=True, slots=True)
class BatchPlanCopy:
    """One exact Single Product Plan copy bound to its batch sequence."""

    sequence: int
    raw_bytes: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class BatchWorkspaceResult:
    """Published or safely reused batch workspace."""

    path: Path
    reused: bool
    persisted_report: dict[str, object]


def item_directory_name(sequence: int) -> str:
    if type(sequence) is not int or sequence <= 0 or sequence > 999_999:
        raise WooBatchWorkspaceError("woo_batch_workspace_sequence_invalid")
    return f"{sequence:06d}"


def _safe_output_root(path: Path) -> Path:
    try:
        candidate = Path(path)
        text = str(candidate).replace("\\", "/")
        if text.startswith("//") or _SCHEME_PATTERN.match(text):
            raise WooBatchWorkspaceError("woo_batch_output_root_invalid")
        absolute = Path(os.path.abspath(candidate))
        if str(absolute).replace("\\", "/").startswith("//"):
            raise WooBatchWorkspaceError("woo_batch_output_root_invalid")
        if package_io._has_link_or_reparse(absolute):
            raise WooBatchWorkspaceError("woo_batch_output_root_linked")
        absolute.mkdir(parents=True, exist_ok=True)
        if not absolute.is_dir() or package_io._has_link_or_reparse(absolute):
            raise WooBatchWorkspaceError("woo_batch_output_root_invalid")
        return absolute
    except WooBatchWorkspaceError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise WooBatchWorkspaceError("woo_batch_output_root_invalid") from None


def batch_workspace_path(output_root: Path, batch_hash: str) -> Path:
    if type(batch_hash) is not str or _HASH_PATTERN.fullmatch(batch_hash) is None:
        raise WooBatchWorkspaceError("woo_batch_hash_invalid")
    root = _safe_output_root(output_root)
    return root / batch_hash


def _write_exclusive_bytes(path: Path, data: bytes) -> None:
    if not isinstance(data, bytes) or not data:
        raise WooBatchWorkspaceError("woo_batch_workspace_bytes_invalid")
    descriptor: int | None = None
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, flags, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
    except FileExistsError:
        raise WooBatchWorkspaceError("woo_batch_workspace_file_exists") from None
    except OSError:
        raise WooBatchWorkspaceError("woo_batch_workspace_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _report_bytes(report: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise WooBatchWorkspaceError("woo_batch_report_invalid") from None


def _read_report(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        local = package_io._local_path(path, require_file=True)
        size = local.stat().st_size
        if size <= 0 or size > MAX_BATCH_REPORT_BYTES:
            raise WooBatchWorkspaceError("woo_batch_report_size_invalid")
        raw = local.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=target_snapshot._json_object_no_duplicates,
        )
    except WooBatchWorkspaceError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        package_io.SingleProductStagingPackageInputError,
        target_snapshot.WooTargetSnapshotInputError,
    ):
        raise WooBatchWorkspaceError("woo_batch_report_invalid") from None
    if not isinstance(value, dict):
        raise WooBatchWorkspaceError("woo_batch_report_invalid")
    return value, raw


def _safe_fingerprint(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"basename", "sha256"}:
        return False
    basename = value.get("basename")
    digest = value.get("sha256")
    return (
        type(basename) is str
        and bool(basename)
        and Path(basename).name == basename
        and Path(basename).suffix.casefold() == ".json"
        and package_io._FORBIDDEN_BASENAME.search(basename) is None
        and type(digest) is str
        and _HASH_PATTERN.fullmatch(digest) is not None
    )


def _reports_match(
    existing: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if not _safe_fingerprint(existing.get("source_manifest")):
        return False
    existing_semantic = dict(existing)
    expected_semantic = dict(expected)
    existing_semantic.pop("source_manifest", None)
    expected_semantic.pop("source_manifest", None)
    return existing_semantic == expected_semantic


def _regular_unlinked_file(path: Path) -> bool:
    try:
        stat_result = path.stat()
        return (
            path.is_file()
            and not package_io._has_link_or_reparse(path)
            and getattr(stat_result, "st_nlink", 1) == 1
        )
    except (OSError, RuntimeError):
        return False


def _validate_existing_workspace(
    root: Path,
    expected_report: Mapping[str, object],
    copies: Sequence[BatchPlanCopy],
) -> dict[str, object]:
    try:
        if (
            not root.is_dir()
            or package_io._has_link_or_reparse(root)
            or {entry.name for entry in root.iterdir()}
            != {BATCH_REPORT_FILENAME, ITEMS_DIRECTORY}
        ):
            raise WooBatchWorkspaceError("woo_batch_workspace_mismatch")
        existing_report, _ = _read_report(root / BATCH_REPORT_FILENAME)
        if not _reports_match(existing_report, expected_report):
            raise WooBatchWorkspaceError("woo_batch_workspace_mismatch")

        items_root = root / ITEMS_DIRECTORY
        expected_names = {item_directory_name(copy.sequence) for copy in copies}
        if (
            not items_root.is_dir()
            or package_io._has_link_or_reparse(items_root)
            or {entry.name for entry in items_root.iterdir()} != expected_names
        ):
            raise WooBatchWorkspaceError("woo_batch_workspace_mismatch")

        for copy in copies:
            item_root = items_root / item_directory_name(copy.sequence)
            if (
                not item_root.is_dir()
                or package_io._has_link_or_reparse(item_root)
                or {entry.name for entry in item_root.iterdir()}
                != {AUTHORITIES_DIRECTORY, REPORTS_DIRECTORY}
            ):
                raise WooBatchWorkspaceError("woo_batch_workspace_mismatch")
            authority_root = item_root / AUTHORITIES_DIRECTORY
            reports_root = item_root / REPORTS_DIRECTORY
            plan_path = authority_root / PLAN_FILENAME
            if (
                not authority_root.is_dir()
                or {entry.name for entry in authority_root.iterdir()}
                != {PLAN_FILENAME}
                or not _regular_unlinked_file(plan_path)
                or plan_path.read_bytes() != copy.raw_bytes
                or hashlib.sha256(copy.raw_bytes).hexdigest() != copy.sha256
                or not reports_root.is_dir()
                or package_io._has_link_or_reparse(reports_root)
            ):
                raise WooBatchWorkspaceError("woo_batch_workspace_mismatch")
            if any(reports_root.iterdir()):
                raise WooBatchWorkspaceError("woo_batch_workspace_mismatch")
        return existing_report
    except WooBatchWorkspaceError:
        raise
    except (OSError, RuntimeError):
        raise WooBatchWorkspaceError("woo_batch_workspace_mismatch") from None


def _build_temporary_workspace(
    temporary_root: Path,
    report: Mapping[str, object],
    copies: Sequence[BatchPlanCopy],
) -> None:
    _write_exclusive_bytes(
        temporary_root / BATCH_REPORT_FILENAME,
        _report_bytes(report),
    )
    for copy in copies:
        if (
            type(copy.sequence) is not int
            or not isinstance(copy.raw_bytes, bytes)
            or not copy.raw_bytes
            or type(copy.sha256) is not str
            or _HASH_PATTERN.fullmatch(copy.sha256) is None
            or hashlib.sha256(copy.raw_bytes).hexdigest() != copy.sha256
        ):
            raise WooBatchWorkspaceError("woo_batch_plan_copy_invalid")
        item_root = (
            temporary_root / ITEMS_DIRECTORY / item_directory_name(copy.sequence)
        )
        authority_root = item_root / AUTHORITIES_DIRECTORY
        reports_root = item_root / REPORTS_DIRECTORY
        authority_root.mkdir(parents=True, exist_ok=False)
        reports_root.mkdir(parents=True, exist_ok=False)
        copied_path = authority_root / PLAN_FILENAME
        _write_exclusive_bytes(copied_path, copy.raw_bytes)
        copied = copied_path.read_bytes()
        if (
            copied != copy.raw_bytes
            or hashlib.sha256(copied).hexdigest() != copy.sha256
            or not _regular_unlinked_file(copied_path)
        ):
            raise WooBatchWorkspaceError("woo_batch_plan_copy_mismatch")


def _cleanup_temporary(path: Path, output_root: Path) -> None:
    try:
        if path.parent == output_root and path.name.startswith(TEMP_PREFIX):
            shutil.rmtree(path)
    except OSError:
        pass


def publish_batch_workspace(
    output_root: Path,
    batch_hash: str,
    report: Mapping[str, object],
    copies: Sequence[BatchPlanCopy],
) -> BatchWorkspaceResult:
    """Publish one exact batch workspace without overwriting an existing one."""

    final_path = batch_workspace_path(output_root, batch_hash)
    root = final_path.parent
    copies_tuple = tuple(copies)
    if not copies_tuple:
        raise WooBatchWorkspaceError("woo_batch_plan_copies_empty")

    temporary = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=root))
    publish_reservation = root / (
        f"{PUBLISH_RESERVATION_PREFIX}{batch_hash}.reservation"
    )
    reservation_owned = False
    try:
        _build_temporary_workspace(temporary, report, copies_tuple)
        _validate_existing_workspace(temporary, report, copies_tuple)
        try:
            _write_exclusive_bytes(
                publish_reservation,
                batch_hash.encode("ascii"),
            )
            reservation_owned = True
        except WooBatchWorkspaceError as error:
            if str(error) == "woo_batch_workspace_file_exists":
                raise WooBatchWorkspaceError("woo_batch_publish_in_progress") from None
            raise

        if final_path.exists():
            persisted = _validate_existing_workspace(
                final_path,
                report,
                copies_tuple,
            )
            return BatchWorkspaceResult(final_path, True, persisted)
        try:
            os.rename(temporary, final_path)
        except OSError:
            if final_path.exists():
                persisted = _validate_existing_workspace(
                    final_path,
                    report,
                    copies_tuple,
                )
                return BatchWorkspaceResult(final_path, True, persisted)
            raise WooBatchWorkspaceError("woo_batch_publish_failed") from None
        temporary = Path()
        return BatchWorkspaceResult(final_path, False, dict(report))
    finally:
        if reservation_owned:
            try:
                publish_reservation.unlink()
            except OSError:
                pass
        if temporary != Path() and temporary.exists():
            _cleanup_temporary(temporary, root)
