"""Central redaction helpers for logs, exceptions, URLs, and reports."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTH_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^,;\r\n]+"
)
_COOKIE_PATTERN = re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*[^;\r\n]+")
_SENSITIVE_PARAMETER_PATTERN = re.compile(
    r"(?i)\b(consumer_key|consumer_secret|app_password|password|token|_wpnonce)"
    r"\s*=\s*[^&\s,;]+"
)
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\b"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA )?PRIVATE KEY-----",
    re.DOTALL,
)
_SENSITIVE_JSON_FIELD_PATTERN = re.compile(
    r'(?i)"(private_key|private_key_id|client_email|token_uri|access_token|'
    r'refresh_token)"\s*:\s*"[^"]*"'
)
REPORT_SECRET_SCAN_PATTERN_TEXT = (
    r"(?<![A-Za-z0-9])(?:ck|cs)_[A-Za-z0-9]{20,}"
    r"|Authorization|Cookie|WP_APP_PASSWORD"
)
REPORT_SECRET_SCAN_PATTERN = re.compile(REPORT_SECRET_SCAN_PATTERN_TEXT)


def mask_hostname(hostname: str | None) -> str:
    """Mask host-specific labels while retaining the registrable-looking suffix."""
    if not hostname:
        return "***"
    labels = hostname.lower().rstrip(".").split(".")
    if len(labels) >= 2:
        return "***." + ".".join(labels[-2:])
    return "***"


def sanitize_url(url: str) -> str:
    """Remove user info, query parameters, fragments, ports, and host labels."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[REDACTED_URL]"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[REDACTED_URL]"
    path = parsed.path if parsed.path.startswith("/") else ""
    return f"{parsed.scheme.lower()}://{mask_hostname(parsed.hostname)}{path}"


@dataclass(frozen=True, slots=True)
class Redactor:
    """Redact known secrets plus common authentication material."""

    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_values(cls, values: Iterable[str | None]) -> Redactor:
        secrets = tuple(
            sorted({value for value in values if value}, key=len, reverse=True)
        )
        return cls(secrets=secrets)

    def text(self, value: object, *, limit: int = 500) -> str:
        redacted = str(value)
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = _PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", redacted)
        redacted = _SENSITIVE_JSON_FIELD_PATTERN.sub(
            lambda match: f'"{match.group(1)}":"[REDACTED]"', redacted
        )
        redacted = _AUTH_HEADER_PATTERN.sub(r"\1: [REDACTED]", redacted)
        redacted = _COOKIE_PATTERN.sub(r"\1: [REDACTED]", redacted)
        redacted = _SENSITIVE_PARAMETER_PATTERN.sub(
            r"\1=[REDACTED]", redacted
        )
        redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)
        redacted = _URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), redacted)
        if len(redacted) > limit:
            return redacted[: limit - 3] + "..."
        return redacted

    def exception(self, error: BaseException) -> str:
        return self.text(f"{type(error).__name__}: {error}")

    def value(self, value: Any) -> Any:
        """Recursively redact JSON-compatible structures."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.text(value)
