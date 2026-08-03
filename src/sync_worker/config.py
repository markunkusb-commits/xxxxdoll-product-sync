"""Read and validate worker configuration without making network requests."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOUBLE_QUOTED_ESCAPE_PATTERN = re.compile(r'\\([\\"nrt])')
DEFAULT_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _decode_double_quoted(value: str) -> str:
    escapes = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
    return _DOUBLE_QUOTED_ESCAPE_PATTERN.sub(
        lambda match: escapes[match.group(1)], value
    )


def _parse_dotenv_value(raw_value: str, line_number: int) -> str:
    value = raw_value.strip()
    if not value or value[0] not in {"'", '"'}:
        return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()

    quote = value[0]
    escaped = False
    closing_index: int | None = None
    for index, character in enumerate(value[1:], start=1):
        if character == quote and not escaped:
            closing_index = index
            break
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False

    if closing_index is None:
        raise ConfigError(f"Invalid quoted value in .env at line {line_number}")

    trailing = value[closing_index + 1 :].strip()
    if trailing and not trailing.startswith("#"):
        raise ConfigError(f"Unexpected content in .env at line {line_number}")

    unquoted = value[1:closing_index]
    if quote == '"':
        return _decode_double_quoted(unquoted)
    return unquoted.replace("\\'", "'").replace("\\\\", "\\")


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read a dotenv file without changing process environment variables."""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ConfigError(f"Invalid .env entry at line {line_number}")

        name, raw_value = line.split("=", maxsplit=1)
        name = name.strip()
        if not _ENV_NAME_PATTERN.fullmatch(name):
            raise ConfigError(f"Invalid variable name in .env at line {line_number}")
        values[name] = _parse_dotenv_value(raw_value, line_number)
    return values


def _read_text(environ: Mapping[str, str], name: str, default: str = "") -> str:
    return environ.get(name, default).strip()


def _read_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = environ.get(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def _hostname(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


def _url_scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _matches_domain(hostname: str | None, domain: str) -> bool:
    return bool(hostname) and (
        hostname == domain or hostname.endswith(f".{domain}")
    )


@dataclass(frozen=True, slots=True)
class StagingSafetyChecks:
    """Boolean-only results for staging configuration safety checks."""

    uses_https: bool
    host_is_wpcomstaging: bool
    host_is_not_xxxxdoll_production: bool
    environment_is_staging: bool
    dry_run_enabled: bool
    product_status_is_draft: bool
    delete_disabled: bool

    @property
    def all_passed(self) -> bool:
        return all(
            (
                self.uses_https,
                self.host_is_wpcomstaging,
                self.host_is_not_xxxxdoll_production,
                self.environment_is_staging,
                self.dry_run_enabled,
                self.product_status_is_draft,
                self.delete_disabled,
            )
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated environment-based configuration for the worker."""

    wp_base_url: str = field(default="", repr=False)
    wp_username: str = field(default="", repr=False)
    wp_app_password: str = field(default="", repr=False)
    wc_consumer_key: str = field(default="", repr=False)
    wc_consumer_secret: str = field(default="", repr=False)
    sync_environment: str = "staging"
    dry_run: bool = True
    default_product_status: str = "draft"
    allow_delete: bool = False

    def configured_status(self) -> dict[str, bool]:
        """Return presence flags without exposing configuration values."""
        return {
            "WP_BASE_URL": bool(self.wp_base_url),
            "WP_USERNAME": bool(self.wp_username),
            "WP_APP_PASSWORD": bool(self.wp_app_password),
            "WC_CONSUMER_KEY": bool(self.wc_consumer_key),
            "WC_CONSUMER_SECRET": bool(self.wc_consumer_secret),
        }

    def masked_hostname(self) -> str | None:
        """Return only a masked hostname derived from WP_BASE_URL."""
        hostname = _hostname(self.wp_base_url)
        if hostname is None:
            return None
        labels = hostname.split(".")
        if len(labels) >= 2:
            return "***." + ".".join(labels[-2:])
        return "***"

    def staging_safety_checks(self) -> StagingSafetyChecks:
        """Evaluate the required staging safeguards without returning secrets."""
        hostname = _hostname(self.wp_base_url)
        return StagingSafetyChecks(
            uses_https=_url_scheme(self.wp_base_url) == "https" and hostname is not None,
            host_is_wpcomstaging=_matches_domain(hostname, "wpcomstaging.com"),
            host_is_not_xxxxdoll_production=not _matches_domain(
                hostname, "xxxxdoll.com"
            ),
            environment_is_staging=self.sync_environment == "staging",
            dry_run_enabled=self.dry_run,
            product_status_is_draft=self.default_product_status == "draft",
            delete_disabled=not self.allow_delete,
        )

    def validate(self) -> None:
        """Fail closed when settings could permit an unsafe synchronization."""
        if self.sync_environment != "staging":
            raise ConfigError("SYNC_ENVIRONMENT must remain staging")
        if not self.dry_run:
            raise ConfigError("DRY_RUN must remain true")
        if self.default_product_status != "draft":
            raise ConfigError("DEFAULT_PRODUCT_STATUS must remain draft")
        if self.allow_delete:
            raise ConfigError("ALLOW_DELETE must remain false")

        if self.wp_base_url:
            checks = self.staging_safety_checks()
            if not checks.uses_https:
                raise ConfigError("WP_BASE_URL must use HTTPS")
            if not checks.host_is_wpcomstaging:
                raise ConfigError("WP_BASE_URL must target wpcomstaging.com")
            if not checks.host_is_not_xxxxdoll_production:
                raise ConfigError("WP_BASE_URL must not target xxxxdoll.com")


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
) -> Settings:
    """Load root .env values, then overlay process environment variables.

    Passing ``environ`` explicitly skips automatic dotenv loading, which keeps tests
    and callers deterministic. No environment values are logged by this module.
    """
    if environ is None:
        path = DEFAULT_DOTENV_PATH if dotenv_path is None else Path(dotenv_path)
        source: dict[str, str] = _read_dotenv(path)
        source.update(os.environ)
    else:
        source = dict(environ)
    settings = Settings(
        wp_base_url=_read_text(source, "WP_BASE_URL"),
        wp_username=_read_text(source, "WP_USERNAME"),
        wp_app_password=_read_text(source, "WP_APP_PASSWORD"),
        wc_consumer_key=_read_text(source, "WC_CONSUMER_KEY"),
        wc_consumer_secret=_read_text(source, "WC_CONSUMER_SECRET"),
        sync_environment=_read_text(source, "SYNC_ENVIRONMENT", "staging").lower(),
        dry_run=_read_bool(source, "DRY_RUN", True),
        default_product_status=_read_text(
            source, "DEFAULT_PRODUCT_STATUS", "draft"
        ).lower(),
        allow_delete=_read_bool(source, "ALLOW_DELETE", False),
    )
    settings.validate()
    return settings
