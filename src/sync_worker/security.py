"""Shared runtime safety, authentication, and credential-redaction helpers."""

from __future__ import annotations

import base64
from urllib.parse import urlsplit

from .config import ConfigError, Settings
from .http_client import ReadOnlyHttpClient
from .sanitization import Redactor


def _basic_token(username: str, password: str) -> str:
    if not username or not password:
        return ""
    return base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )


def basic_auth_headers(username: str, password: str) -> dict[str, str]:
    """Build an in-memory Basic header that must never be logged or reported."""
    token = _basic_token(username, password)
    return {"Authorization": f"Basic {token}"} if token else {}


def redactor_for_settings(settings: Settings) -> Redactor:
    """Cover raw and derived authentication values."""
    return Redactor.from_values(
        (
            settings.wp_base_url,
            settings.wp_username,
            settings.wp_app_password,
            settings.wc_consumer_key,
            settings.wc_consumer_secret,
            _basic_token(settings.wp_username, settings.wp_app_password),
            _basic_token(settings.wc_consumer_key, settings.wc_consumer_secret),
        )
    )


def assert_safe_staging_runtime(
    settings: Settings, client: ReadOnlyHttpClient
) -> None:
    """Fail before transport unless every staging read-only safeguard passes."""
    settings.validate()
    if not settings.staging_safety_checks().all_passed:
        raise ConfigError("Staging runtime safety checks failed")
    configured_hostname = urlsplit(settings.wp_base_url).hostname
    if client.hostname != configured_hostname:
        raise ConfigError("HTTP client target does not match configuration")
