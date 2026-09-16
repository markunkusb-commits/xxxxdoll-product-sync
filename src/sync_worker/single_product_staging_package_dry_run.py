"""Local-only I/O adapter for the single-product staging package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from .report import SafeJsonReportWriter
from .sanitization import Redactor
from .single_product_staging_package import (
    REPORT_FILENAME,
    SOURCE_ROLES,
    SingleProductStagingPackageError,
    build_single_product_staging_package,
)


MAX_INPUT_REPORT_BYTES = 64 * 1024 * 1024
_FORBIDDEN_BASENAME = re.compile(
    r"(?i)(?:^\.env(?:\.|$)|authorization|cookie|credential|password|secret|"
    r"private.?key|access.?token|refresh.?token|service.?account|client.?secret|"
    r"token\.json)"
)


class SingleProductStagingPackageInputError(ValueError):
    """Fixed-code local path, JSON, or report-contract error."""


def _has_link_or_reparse(path: Path) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            return True
    return False


def _local_path(path: Path, *, require_file: bool) -> Path:
    try:
        candidate = Path(path)
        text = str(candidate).replace("\\", "/")
        if (
            text.startswith("//")
            or re.match(r"^[a-z][a-z0-9+.-]+:", text, re.IGNORECASE)
            or candidate.suffix.casefold() != ".json"
            or _FORBIDDEN_BASENAME.search(candidate.name)
        ):
            raise SingleProductStagingPackageInputError(
                "single_product_local_json_required"
            )
        absolute = Path(os.path.abspath(candidate))
        if str(absolute).replace("\\", "/").startswith("//"):
            raise SingleProductStagingPackageInputError(
                "single_product_local_json_required"
            )
        if _has_link_or_reparse(absolute):
            raise SingleProductStagingPackageInputError(
                "single_product_linked_path_not_allowed"
            )
        if require_file and not absolute.is_file():
            raise SingleProductStagingPackageInputError(
                "single_product_local_json_required"
            )
        return absolute
    except SingleProductStagingPackageInputError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SingleProductStagingPackageInputError(
            "single_product_local_json_required"
        ) from None


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SingleProductStagingPackageInputError(
                "single_product_duplicate_json_key"
            )
        result[key] = value
    return result


def _read_report(path: Path) -> tuple[Mapping[str, object], str]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_INPUT_REPORT_BYTES:
            raise SingleProductStagingPackageInputError(
                "single_product_input_size_invalid"
            )
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_no_duplicates
        )
    except SingleProductStagingPackageInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise SingleProductStagingPackageInputError(
            "single_product_input_json_invalid"
        ) from None
    if not isinstance(value, Mapping):
        raise SingleProductStagingPackageInputError(
            "single_product_input_root_invalid"
        )
    if value.get("status") != "ok":
        raise SingleProductStagingPackageInputError(
            "single_product_input_status_not_ok"
        )
    return value, hashlib.sha256(raw).hexdigest()


def run_single_product_staging_package_dry_run(
    payload_report_path: Path,
    sku_report_path: Path,
    selection_report_path: Path,
    media_report_path: Path,
    category_discovery_path: Path,
    target_sku: str,
    *,
    project_root: Path,
    redactor: Redactor | None = None,
) -> tuple[dict[str, object], Path]:
    """Read exactly five local reports and atomically write one safe package."""

    inputs = tuple(
        _local_path(Path(value), require_file=True)
        for value in (
            payload_report_path,
            sku_report_path,
            selection_report_path,
            media_report_path,
            category_discovery_path,
        )
    )
    output = _local_path(
        Path(project_root) / "reports" / REPORT_FILENAME,
        require_file=False,
    )
    if len(set(inputs)) != len(inputs) or output in inputs:
        raise SingleProductStagingPackageInputError(
            "single_product_input_path_collision"
        )
    loaded = tuple(_read_report(path) for path in inputs)
    fingerprints = {
        role: {"basename": path.name, "sha256": loaded[index][1]}
        for index, (role, path) in enumerate(zip(SOURCE_ROLES, inputs, strict=True))
    }
    try:
        report = build_single_product_staging_package(
            *(item[0] for item in loaded),
            target_sku=target_sku,
            source_fingerprints=fingerprints,
            redactor=redactor,
        )
    except SingleProductStagingPackageError as error:
        raise SingleProductStagingPackageInputError(str(error)) from None
    active_redactor = redactor or Redactor()
    try:
        SafeJsonReportWriter(output, active_redactor).write(report)
    except (OSError, TypeError, ValueError):
        raise SingleProductStagingPackageInputError(
            "single_product_package_write_failed"
        ) from None
    return report, output
