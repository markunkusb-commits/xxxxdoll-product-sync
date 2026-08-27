"""Read and validate worker configuration without making network requests."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GOOGLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_PROXY_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_PORT_PATTERN = re.compile(r"^[0-9]+$")
_DOUBLE_QUOTED_ESCAPE_PATTERN = re.compile(r'\\([\\"nrt])')
DEFAULT_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"
PROJECT_ROOT = DEFAULT_DOTENV_PATH.parent
GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DRIVE_METADATA_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/drive.metadata.readonly"
)
GOOGLE_SHEETS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/spreadsheets.readonly"
)


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


def _read_strict_bool(
    environ: Mapping[str, str], name: str, default: bool
) -> bool:
    raw_value = environ.get(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigError(f"{name} must be exactly true or false")


def _read_optional_port(environ: Mapping[str, str], name: str) -> int | None:
    raw_value = environ.get(name)
    if raw_value is None or not raw_value.strip():
        return None
    normalized = raw_value.strip()
    if not _PORT_PATTERN.fullmatch(normalized):
        raise ConfigError(f"{name} must be an integer from 1 to 65535")
    value = int(normalized)
    if not 1 <= value <= 65535:
        raise ConfigError(f"{name} must be an integer from 1 to 65535")
    return value


def _valid_proxy_host(value: str) -> bool:
    if not value or any(character.isspace() for character in value):
        return False
    try:
        ip_address(value)
    except ValueError:
        return bool(_PROXY_HOSTNAME_PATTERN.fullmatch(value))
    return True


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


def mask_identifier(value: str) -> str:
    """Return an identifier-safe display form without revealing the full ID."""
    return "***" + value[-4:] if len(value) >= 4 else "***"


@dataclass(frozen=True, slots=True)
class GoogleSettings:
    """Validated configuration for read-only Google Drive and Sheets checks."""

    service_account_file: str = field(default="", repr=False)
    clm_spreadsheet_id: str = field(default="", repr=False)
    clm_drive_folder_id: str = field(default="", repr=False)
    md_drive_folder_id: str = field(default="", repr=False)
    drive_scope: str = field(default="", repr=False)
    sheets_scope: str = field(default="", repr=False)
    google_proxy_mode: str = field(default="none", repr=False)
    google_proxy_host: str = field(default="", repr=False)
    google_proxy_port: int | None = field(default=None, repr=False)
    google_proxy_rdns: bool = field(default=True, repr=False)

    @property
    def resolved_service_account_file(self) -> Path:
        return Path(self.service_account_file).expanduser().resolve(strict=False)

    def configured_status(self) -> dict[str, bool]:
        return {
            "GOOGLE_SERVICE_ACCOUNT_FILE": bool(self.service_account_file),
            "CLM_SPREADSHEET_ID": bool(self.clm_spreadsheet_id),
            "CLM_DRIVE_FOLDER_ID": bool(self.clm_drive_folder_id),
            "MD_DRIVE_FOLDER_ID": bool(self.md_drive_folder_id),
            "GOOGLE_DRIVE_SCOPE": bool(self.drive_scope),
            "GOOGLE_SHEETS_SCOPE": bool(self.sheets_scope),
        }

    def masked_identifiers(self) -> dict[str, str]:
        return {
            "CLM_SPREADSHEET_ID": mask_identifier(self.clm_spreadsheet_id),
            "CLM_DRIVE_FOLDER_ID": mask_identifier(self.clm_drive_folder_id),
            "MD_DRIVE_FOLDER_ID": mask_identifier(self.md_drive_folder_id),
        }

    def _validate_proxy(self) -> None:
        if self.google_proxy_mode not in {"none", "socks5"}:
            raise ConfigError("GOOGLE_PROXY_MODE must be none or socks5")
        if not isinstance(self.google_proxy_rdns, bool):
            raise ConfigError("GOOGLE_PROXY_RDNS must be exactly true or false")
        if self.google_proxy_mode == "socks5":
            if not _valid_proxy_host(self.google_proxy_host):
                raise ConfigError(
                    "GOOGLE_PROXY_HOST must be a valid host in socks5 mode"
                )
            if (
                not isinstance(self.google_proxy_port, int)
                or isinstance(self.google_proxy_port, bool)
                or not 1 <= self.google_proxy_port <= 65535
            ):
                raise ConfigError(
                    "GOOGLE_PROXY_PORT must be an integer from 1 to 65535"
                )

    def _validate_service_account_file(
        self, *, project_root: Path = PROJECT_ROOT
    ) -> None:
        if not self.service_account_file:
            raise ConfigError(
                "Missing Google configuration: GOOGLE_SERVICE_ACCOUNT_FILE"
            )
        credentials_path = self.resolved_service_account_file
        resolved_project_root = project_root.resolve(strict=False)
        if credentials_path.is_relative_to(resolved_project_root):
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_FILE must be outside the project")
        if credentials_path.suffix.lower() != ".json":
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_FILE must use a .json extension")
        if not credentials_path.is_file():
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_FILE does not exist")

    def validate(self, *, project_root: Path = PROJECT_ROOT) -> None:
        self._validate_proxy()

        missing = [
            name
            for name, configured in self.configured_status().items()
            if not configured
        ]
        if missing:
            raise ConfigError("Missing Google configuration: " + ", ".join(missing))
        for name, value in (
            ("CLM_SPREADSHEET_ID", self.clm_spreadsheet_id),
            ("CLM_DRIVE_FOLDER_ID", self.clm_drive_folder_id),
            ("MD_DRIVE_FOLDER_ID", self.md_drive_folder_id),
        ):
            if not _GOOGLE_ID_PATTERN.fullmatch(value):
                raise ConfigError(f"{name} has an invalid identifier format")

        self._validate_service_account_file(project_root=project_root)
        if self.drive_scope != GOOGLE_DRIVE_READONLY_SCOPE:
            raise ConfigError("GOOGLE_DRIVE_SCOPE must be the exact read-only scope")
        if self.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE:
            raise ConfigError("GOOGLE_SHEETS_SCOPE must be the exact read-only scope")

    def validate_drive_metadata(
        self, *, project_root: Path = PROJECT_ROOT
    ) -> None:
        """Validate only the configuration needed for metadata-only Drive reads."""

        self._validate_proxy()
        self._validate_service_account_file(project_root=project_root)
        if self.drive_scope != GOOGLE_DRIVE_METADATA_READONLY_SCOPE:
            raise ConfigError("drive_metadata_scope_unavailable")

    def validate_drive_metadata_with_sheets(
        self, *, project_root: Path = PROJECT_ROOT
    ) -> None:
        """Validate the exact scopes needed by the manifest reality check."""

        self.validate_drive_metadata(project_root=project_root)
        if self.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE:
            raise ConfigError("drive_metadata_scope_unavailable")
        if not _GOOGLE_ID_PATTERN.fullmatch(self.clm_spreadsheet_id):
            raise ConfigError("CLM_SPREADSHEET_ID has an invalid identifier format")

    def validate_sheets_readonly(
        self, *, project_root: Path = PROJECT_ROOT
    ) -> None:
        """Validate only configuration required for read-only Sheets access."""

        self._validate_proxy()
        self._validate_service_account_file(project_root=project_root)
        if not self.clm_spreadsheet_id:
            raise ConfigError("Missing Google configuration: CLM_SPREADSHEET_ID")
        if not _GOOGLE_ID_PATTERN.fullmatch(self.clm_spreadsheet_id):
            raise ConfigError("CLM_SPREADSHEET_ID has an invalid identifier format")
        if self.sheets_scope != GOOGLE_SHEETS_READONLY_SCOPE:
            raise ConfigError("GOOGLE_SHEETS_SCOPE must be the exact read-only scope")


def _configuration_source(
    environ: Mapping[str, str] | None,
    dotenv_path: str | Path | None,
) -> dict[str, str]:
    if environ is not None:
        return dict(environ)
    path = DEFAULT_DOTENV_PATH if dotenv_path is None else Path(dotenv_path)
    source = _read_dotenv(path)
    source.update(os.environ)
    return source


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
) -> Settings:
    """Load root .env values, then overlay process environment variables.

    Passing ``environ`` explicitly skips automatic dotenv loading, which keeps tests
    and callers deterministic. No environment values are logged by this module.
    """
    source = _configuration_source(environ, dotenv_path)
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


def load_google_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> GoogleSettings:
    """Load and validate Google settings without reading credential JSON content."""
    source = _configuration_source(environ, dotenv_path)
    settings = _google_settings_from_source(source)
    settings.validate(project_root=project_root)
    return settings


def _google_settings_from_source(
    source: Mapping[str, str],
) -> GoogleSettings:
    proxy_mode = _read_text(source, "GOOGLE_PROXY_MODE", "none").lower()
    if not proxy_mode:
        proxy_mode = "none"
    return GoogleSettings(
        service_account_file=_read_text(source, "GOOGLE_SERVICE_ACCOUNT_FILE"),
        clm_spreadsheet_id=_read_text(source, "CLM_SPREADSHEET_ID"),
        clm_drive_folder_id=_read_text(source, "CLM_DRIVE_FOLDER_ID"),
        md_drive_folder_id=_read_text(source, "MD_DRIVE_FOLDER_ID"),
        drive_scope=_read_text(source, "GOOGLE_DRIVE_SCOPE"),
        sheets_scope=_read_text(source, "GOOGLE_SHEETS_SCOPE"),
        google_proxy_mode=proxy_mode,
        google_proxy_host=_read_text(source, "GOOGLE_PROXY_HOST"),
        google_proxy_port=_read_optional_port(source, "GOOGLE_PROXY_PORT"),
        google_proxy_rdns=_read_strict_bool(
            source, "GOOGLE_PROXY_RDNS", True
        ),
    )


def load_google_drive_metadata_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> GoogleSettings:
    """Load the shared Google/proxy config for metadata-only Drive access."""

    source = _configuration_source(environ, dotenv_path)
    settings = _google_settings_from_source(source)
    settings.validate_drive_metadata(project_root=project_root)
    return settings


def load_google_sheets_readonly_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> GoogleSettings:
    """Load only the shared proxy and read-only Sheets configuration."""

    source = _configuration_source(environ, dotenv_path)
    settings = _google_settings_from_source(source)
    settings.validate_sheets_readonly(project_root=project_root)
    return settings
