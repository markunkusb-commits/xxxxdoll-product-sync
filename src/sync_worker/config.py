"""Read and validate worker configuration without making network requests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_ALLOWED_ENVIRONMENTS = frozenset({"staging", "production"})
_ALLOWED_PRODUCT_STATUSES = frozenset({"draft", "pending", "private", "publish"})


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


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated environment-based configuration for the worker."""

    wp_base_url: str = ""
    wp_username: str = ""
    wp_app_password: str = field(default="", repr=False)
    wc_consumer_key: str = field(default="", repr=False)
    wc_consumer_secret: str = field(default="", repr=False)
    sync_environment: str = "staging"
    dry_run: bool = True
    default_product_status: str = "draft"
    allow_delete: bool = False

    def validate(self) -> None:
        """Fail closed when settings could permit an unsafe synchronization."""
        if self.sync_environment not in _ALLOWED_ENVIRONMENTS:
            raise ConfigError(
                "SYNC_ENVIRONMENT must be either 'staging' or 'production'"
            )
        if self.default_product_status not in _ALLOWED_PRODUCT_STATUSES:
            raise ConfigError("DEFAULT_PRODUCT_STATUS is not supported")
        if self.allow_delete:
            raise ConfigError("ALLOW_DELETE must remain false")

        if not self.dry_run:
            required_values = {
                "WP_BASE_URL": self.wp_base_url,
                "WP_USERNAME": self.wp_username,
                "WP_APP_PASSWORD": self.wp_app_password,
                "WC_CONSUMER_KEY": self.wc_consumer_key,
                "WC_CONSUMER_SECRET": self.wc_consumer_secret,
            }
            missing = [name for name, value in required_values.items() if not value]
            if missing:
                raise ConfigError(
                    "Non-dry-run configuration is missing: " + ", ".join(missing)
                )


def load_config(environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings from a supplied mapping or the current process environment."""
    source = os.environ if environ is None else environ
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
