"""Allowlisted, redacted JSON report persistence for read-only commands."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .sanitization import Redactor


_FORBIDDEN_EXACT_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set_cookie",
        "headers",
        "request_headers",
        "response_headers",
        "url",
        "full_url",
        "wp_base_url",
        "wp_username",
        "username",
        "wp_app_password",
        "app_password",
        "private_key",
        "private_key_id",
        "client_email",
        "token_uri",
        "access_token",
        "refresh_token",
        "wc_consumer_key",
        "wc_consumer_secret",
        "consumer_key",
        "consumer_secret",
        "password",
        "secret",
        "token",
        "spreadsheet_id",
        "file_id",
        "folder_id",
        "image_url",
        "download_url",
        "download_link",
        "web_content_link",
        "users",
        "user",
        "customers",
        "customer",
        "orders",
        "order",
        "payments",
        "payment",
        "coupons",
        "coupon",
    }
)


def _forbidden_report_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return (
        normalized in _FORBIDDEN_EXACT_KEYS
        or normalized.startswith(
            ("customer_", "order_", "payment_", "coupon_", "user_")
        )
        or normalized.endswith(
            ("_password", "_secret", "_token", "_cookie", "_headers", "_url")
        )
    )


def sanitize_report_data(value: Any, redactor: Redactor) -> Any:
    """Drop forbidden fields and redact every retained string recursively."""
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_report_data(item, redactor)
            for key, item in value.items()
            if not _forbidden_report_key(key)
        }
    if isinstance(value, list):
        return [sanitize_report_data(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [sanitize_report_data(item, redactor) for item in value]
    return redactor.value(value)


class SafeJsonReportWriter:
    """Write a sanitized read-only report atomically."""

    def __init__(self, path: Path, redactor: Redactor) -> None:
        self._path = path
        self._redactor = redactor

    def write(self, report: Mapping[str, object]) -> Path:
        sanitized = sanitize_report_data(report, self._redactor)
        if not isinstance(sanitized, dict):
            raise TypeError("Safe report must be a mapping")
        sanitized["write_requests_performed"] = 0

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_name(self._path.name + ".tmp")
        temporary_path.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self._path)
        return self._path


class DoctorReportWriter(SafeJsonReportWriter):
    """Backward-compatible writer name for doctor reports."""


class ReferenceProductReportWriter(SafeJsonReportWriter):
    """Writer for reference product inspection reports."""
