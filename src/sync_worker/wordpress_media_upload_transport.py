"""Narrow staging-only WordPress media upload transport core.

The core accepts only live upload intents issued by the Media Upload Gate.  It
uses a separate write permit and an injected narrow HTTP transport with exactly
two operations: exact-slug media lookup and raw WebP upload.  No CLI, report
writer, environment loading, WooCommerce operation, rollback, or delete exists
in this module.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import ssl
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import unquote, urlencode, urlsplit

from . import google_drive_folder_manifest as drive_manifest_core
from . import wordpress_media_upload_gate as gate_core
from .config import Settings
from .report import sanitize_report_data
from .sanitization import Redactor


POLICY_VERSION: Final = "xxxxdoll-wordpress-media-upload-transport-v1"
WORDPRESS_MEDIA_ENDPOINT: Final = gate_core.WORDPRESS_MEDIA_ENDPOINT
WORDPRESS_RESOURCE: Final = gate_core.WORDPRESS_RESOURCE
MEDIA_SLUG_PREFIX: Final = "xxxxdoll-media-"
MAX_MEDIA_SLUG_LENGTH: Final = 96
MAX_RESPONSE_BYTES: Final = 1_000_000
CONNECT_TIMEOUT_SECONDS: Final = 10.0
READ_TIMEOUT_SECONDS: Final = 30.0
LOOKUP_PER_PAGE: Final = 2

_CREDENTIAL_CAPABILITY = object()
_WRITE_PERMIT_CAPABILITY = object()
_REFERENCE_CAPABILITY = object()
_SLUG_PATTERN = re.compile(r"xxxxdoll-media-[0-9a-f]{64}\Z", re.ASCII)
_BINDING_PATTERN = gate_core._TARGET_BINDING_PATTERN
_SAFE_UPLOAD_STATUSES = frozenset({"created", "reused", "created_reconciled"})
_DETERMINISTIC_POST_ERRORS = {
    400: "wordpress_media_upload_bad_request",
    401: "wordpress_media_upload_unauthorized",
    403: "wordpress_media_upload_forbidden",
    404: "wordpress_media_upload_endpoint_not_found",
    413: "wordpress_media_upload_too_large",
    415: "wordpress_media_upload_unsupported_media_type",
}


class WordPressMediaUploadTransportError(ValueError):
    """Fixed-code transport failure with no request or credential context."""


class WordPressMediaTransportNetworkError(Exception):
    """Fixed internal marker for an uncertain network outcome."""

    __slots__ = ("rate_limited",)

    def __init__(self, *, rate_limited: bool = False) -> None:
        super().__init__("wordpress_media_upload_outcome_uncertain")
        self.rate_limited = rate_limited


class WordPressApplicationPasswordCredentials:
    """Non-serializable, redacted Application Password credentials."""

    __slots__ = ("__capability", "__username", "__application_password")

    def __init__(
        self,
        capability: object,
        *,
        username: str,
        application_password: str,
    ) -> None:
        if capability is not _CREDENTIAL_CAPABILITY:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_credentials_factory_required"
            )
        if not _safe_username(username) or not _safe_application_password(
            application_password
        ):
            raise WordPressMediaUploadTransportError(
                "wordpress_media_credentials_invalid"
            )
        object.__setattr__(
            self,
            "_WordPressApplicationPasswordCredentials__capability",
            capability,
        )
        object.__setattr__(
            self, "_WordPressApplicationPasswordCredentials__username", username
        )
        object.__setattr__(
            self,
            "_WordPressApplicationPasswordCredentials__application_password",
            application_password,
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("wordpress_media_credentials_are_immutable")

    def __repr__(self) -> str:
        return "WordPressApplicationPasswordCredentials([REDACTED])"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("wordpress_media_credentials_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("wordpress_media_credentials_not_serializable")


class WordPressMediaWritePermit:
    """Explicit, target-bound capability authorizing this narrow write core."""

    __slots__ = ("__capability", "__target_binding")

    def __init__(self, capability: object, *, target_binding: str) -> None:
        if capability is not _WRITE_PERMIT_CAPABILITY:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_write_permit_factory_required"
            )
        if type(target_binding) is not str or _BINDING_PATTERN.fullmatch(
            target_binding
        ) is None:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_write_permit_invalid"
            )
        object.__setattr__(
            self, "_WordPressMediaWritePermit__capability", capability
        )
        object.__setattr__(
            self, "_WordPressMediaWritePermit__target_binding", target_binding
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("wordpress_media_write_permit_is_immutable")

    def __repr__(self) -> str:
        return "WordPressMediaWritePermit(target=[BOUND])"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("wordpress_media_write_permit_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("wordpress_media_write_permit_not_serializable")


class WordPressMediaHttpResponse:
    """Bounded response envelope used by narrow real and mock transports."""

    __slots__ = ("status_code", "body")

    def __init__(self, status_code: int, body: bytes) -> None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_http_response_invalid"
            )
        if type(body) is not bytes:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_http_response_invalid"
            )
        self.status_code = status_code
        self.body = body

    def __repr__(self) -> str:
        return (
            "WordPressMediaHttpResponse("
            f"status_code={self.status_code}, body_bytes={len(self.body)})"
        )


class WordPressMediaHttpTransport(Protocol):
    """No arbitrary method/path surface: media lookup and upload only."""

    def lookup_media(
        self, *, slug: str, authorization: str
    ) -> WordPressMediaHttpResponse: ...

    def upload_media(
        self,
        *,
        slug: str,
        upload_filename: str,
        body: bytes,
        authorization: str,
    ) -> WordPressMediaHttpResponse: ...


class StdlibWordPressMediaHttpTransport:
    """Exact-host TLS transport with no redirect or generic request method."""

    __slots__ = ("_hostname", "_port", "_base_path", "_ssl_context")

    def __init__(self, settings: Settings) -> None:
        gate_core._target_binding(settings)
        parsed = urlsplit(settings.wp_base_url)
        self._hostname = parsed.hostname.casefold()
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._ssl_context = ssl.create_default_context()

    def _send_exact(
        self,
        method: str,
        target: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> WordPressMediaHttpResponse:
        connection = http.client.HTTPSConnection(
            self._hostname,
            self._port,
            timeout=CONNECT_TIMEOUT_SECONDS,
            context=self._ssl_context,
        )
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            if connection.sock is not None:
                connection.sock.settimeout(READ_TIMEOUT_SECONDS)
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError):
                    raise WordPressMediaTransportNetworkError() from None
                if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                    raise WordPressMediaTransportNetworkError()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise WordPressMediaTransportNetworkError()
            return WordPressMediaHttpResponse(response.status, response_body)
        except WordPressMediaTransportNetworkError:
            raise
        except Exception:
            raise WordPressMediaTransportNetworkError() from None
        finally:
            connection.close()

    def lookup_media(
        self, *, slug: str, authorization: str
    ) -> WordPressMediaHttpResponse:
        if _SLUG_PATTERN.fullmatch(slug) is None:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_slug_invalid"
            )
        query = urlencode({"slug": slug, "per_page": LOOKUP_PER_PAGE})
        return self._send_exact(
            "GET",
            f"{self._base_path}{WORDPRESS_MEDIA_ENDPOINT}?{query}",
            body=None,
            headers={
                "Authorization": authorization,
                "Accept": "application/json",
            },
        )

    def upload_media(
        self,
        *,
        slug: str,
        upload_filename: str,
        body: bytes,
        authorization: str,
    ) -> WordPressMediaHttpResponse:
        if (
            _SLUG_PATTERN.fullmatch(slug) is None
            or gate_core._ASCII_FILENAME_PATTERN.fullmatch(upload_filename) is None
            or type(body) is not bytes
            or not body
            or len(body) > gate_core.conversion_core.MAX_WEBP_OUTPUT_FILE_BYTES
        ):
            raise WordPressMediaUploadTransportError(
                "wordpress_media_upload_request_invalid"
            )
        return self._send_exact(
            "POST",
            self._base_path + WORDPRESS_MEDIA_ENDPOINT,
            body=body,
            headers={
                "Authorization": authorization,
                "Accept": "application/json",
                "Content-Type": "image/webp",
                "Content-Disposition": (
                    f'attachment; filename="{upload_filename}"'
                ),
                "Content-Length": str(len(body)),
                "X-WP-Media-Slug": slug,
            },
        )


class VerifiedWordPressMediaReference:
    """Immutable, capability-gated verified WordPress media reference."""

    __slots__ = (
        "__capability",
        "__source_location_fingerprint",
        "_sku",
        "_selection_position",
        "_image_role",
        "_media_identity",
        "_upload_filename",
        "_wordpress_media_id",
        "_wordpress_slug",
        "_upload_status",
        "_target_fingerprint",
        "_warnings",
    )

    def __init__(
        self,
        capability: object,
        *,
        intent: gate_core.WordPressMediaUploadIntent,
        wordpress_media_id: int,
        wordpress_slug: str,
        upload_status: str,
        source_location_fingerprint: str,
    ) -> None:
        if capability is not _REFERENCE_CAPABILITY:
            raise WordPressMediaUploadTransportError(
                "wordpress_media_reference_factory_required"
            )
        if not gate_core._valid_intent(intent):
            raise WordPressMediaUploadTransportError(
                "valid_wordpress_media_upload_intent_required"
            )
        if (
            type(wordpress_media_id) is not int
            or wordpress_media_id <= 0
            or _SLUG_PATTERN.fullmatch(wordpress_slug) is None
            or upload_status not in _SAFE_UPLOAD_STATUSES
            or not _valid_source_location_fingerprint(source_location_fingerprint)
        ):
            raise WordPressMediaUploadTransportError(
                "wordpress_media_reference_invalid"
            )
        object.__setattr__(
            self, "_VerifiedWordPressMediaReference__capability", capability
        )
        object.__setattr__(
            self,
            "_VerifiedWordPressMediaReference__source_location_fingerprint",
            source_location_fingerprint,
        )
        object.__setattr__(self, "_sku", intent.sku)
        object.__setattr__(self, "_selection_position", intent.selection_position)
        object.__setattr__(self, "_image_role", intent.image_role)
        object.__setattr__(self, "_media_identity", intent.media_identity)
        object.__setattr__(self, "_upload_filename", intent.upload_filename)
        object.__setattr__(self, "_wordpress_media_id", wordpress_media_id)
        object.__setattr__(self, "_wordpress_slug", wordpress_slug)
        object.__setattr__(self, "_upload_status", upload_status)
        object.__setattr__(
            self, "_target_fingerprint", intent.target_host_fingerprint
        )
        object.__setattr__(self, "_warnings", tuple(intent.warnings))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("verified_wordpress_media_reference_is_immutable")

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
    def media_identity(self) -> str:
        return self._media_identity

    @property
    def upload_filename(self) -> str:
        return self._upload_filename

    @property
    def wordpress_media_id(self) -> int:
        return self._wordpress_media_id

    @property
    def wordpress_slug(self) -> str:
        return self._wordpress_slug

    @property
    def upload_status(self) -> str:
        return self._upload_status

    @property
    def target_fingerprint(self) -> str:
        return self._target_fingerprint

    @property
    def mime_type(self) -> str:
        return "image/webp"

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @property
    def blocking_issues(self) -> tuple[str, ...]:
        return ()

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "sku": self.sku,
            "selection_position": self.selection_position,
            "image_role": self.image_role,
            "media_identity": self.media_identity,
            "upload_filename": self.upload_filename,
            "wordpress_media_id": self.wordpress_media_id,
            "wordpress_slug": self.wordpress_slug,
            "upload_status": self.upload_status,
            "target_fingerprint": self.target_fingerprint,
            "mime_type": self.mime_type,
            "warnings": list(self.warnings),
            "blocking_issues": list(self.blocking_issues),
        }

    def __repr__(self) -> str:
        return f"VerifiedWordPressMediaReference({self.to_safe_dict()!r})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("verified_wordpress_media_reference_not_serializable")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("verified_wordpress_media_reference_not_serializable")


class WordPressMediaUploadTransportBatchResult:
    """Fail-stop remote audit; successful earlier references remain authoritative."""

    __slots__ = (
        "_status",
        "_summary",
        "_results",
        "_references",
        "_warnings",
        "_blocking_issues",
    )

    def __init__(
        self,
        *,
        status: str,
        summary: Mapping[str, int | None],
        results: tuple[Mapping[str, object], ...],
        references: tuple[VerifiedWordPressMediaReference, ...],
        warnings: tuple[str, ...],
        blocking_issues: tuple[str, ...],
    ) -> None:
        self._status = status
        self._summary = dict(summary)
        self._results = tuple(dict(item) for item in results)
        self._references = references
        self._warnings = warnings
        self._blocking_issues = blocking_issues

    @property
    def status(self) -> str:
        return self._status

    @property
    def references(self) -> tuple[VerifiedWordPressMediaReference, ...]:
        return self._references

    def to_safe_dict(self) -> dict[str, object]:
        report = {
            "status": self.status,
            "policy_version": POLICY_VERSION,
            "summary": dict(self._summary),
            "results": [dict(item) for item in self._results],
            "warnings": list(self._warnings),
            "blocking_issues": list(self._blocking_issues),
        }
        safe = sanitize_report_data(report, Redactor())
        drive_manifest_core._assert_report_safe(safe)
        return json.loads(json.dumps(safe, ensure_ascii=False))

    def __repr__(self) -> str:
        return (
            "WordPressMediaUploadTransportBatchResult("
            f"status={self.status!r}, references_count={len(self.references)})"
        )


ProgressCallback = Callable[[Mapping[str, object]], None]


def _safe_username(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _safe_application_password(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and "\r" not in value
        and "\n" not in value
        and "\x00" not in value
    )


def _create_test_application_password_credentials(
    username: str, application_password: str
) -> WordPressApplicationPasswordCredentials:
    """Test-only factory; real creation belongs to a future approved canary."""

    return WordPressApplicationPasswordCredentials(
        _CREDENTIAL_CAPABILITY,
        username=username,
        application_password=application_password,
    )


def _create_test_media_write_permit(settings: Settings) -> WordPressMediaWritePermit:
    """Test-only explicit write authorization bound to one validated target."""

    return WordPressMediaWritePermit(
        _WRITE_PERMIT_CAPABILITY,
        target_binding=gate_core._target_binding(settings),
    )


def _credentials_values(
    credentials: WordPressApplicationPasswordCredentials,
) -> tuple[str, str]:
    if type(credentials) is not WordPressApplicationPasswordCredentials:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_credentials_required"
        )
    try:
        capability = object.__getattribute__(
            credentials,
            "_WordPressApplicationPasswordCredentials__capability",
        )
        username = object.__getattribute__(
            credentials,
            "_WordPressApplicationPasswordCredentials__username",
        )
        password = object.__getattribute__(
            credentials,
            "_WordPressApplicationPasswordCredentials__application_password",
        )
    except AttributeError:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_credentials_required"
        ) from None
    if (
        capability is not _CREDENTIAL_CAPABILITY
        or not _safe_username(username)
        or not _safe_application_password(password)
    ):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_credentials_required"
        )
    return username, password


def _permit_binding(permit: WordPressMediaWritePermit) -> str:
    if type(permit) is not WordPressMediaWritePermit:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_write_permit_required"
        )
    try:
        capability = object.__getattribute__(
            permit, "_WordPressMediaWritePermit__capability"
        )
        binding = object.__getattribute__(
            permit, "_WordPressMediaWritePermit__target_binding"
        )
    except AttributeError:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_write_permit_required"
        ) from None
    if (
        capability is not _WRITE_PERMIT_CAPABILITY
        or type(binding) is not str
        or _BINDING_PATTERN.fullmatch(binding) is None
    ):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_write_permit_required"
        )
    return binding


def _authorization(credentials: WordPressApplicationPasswordCredentials) -> str:
    username, password = _credentials_values(credentials)
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )
    return f"Basic {token}"


def _normalize_intents(
    value: (
        gate_core.WordPressMediaUploadIntent
        | tuple[gate_core.WordPressMediaUploadIntent, ...]
    ),
) -> tuple[gate_core.WordPressMediaUploadIntent, ...]:
    if type(value) is gate_core.WordPressMediaUploadIntent:
        intents = (value,)
    elif type(value) is tuple:
        intents = value
    else:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_upload_intents_required"
        )
    if not intents or len(intents) > gate_core.MAX_ARTIFACTS_PER_BATCH:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_upload_intents_required"
        )
    if any(not gate_core._valid_intent(intent) for intent in intents):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_upload_intents_required"
        )
    keys = tuple((intent.sku, intent.selection_position) for intent in intents)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_upload_intents_not_canonical"
        )
    return intents


def _media_slug(intent: gate_core.WordPressMediaUploadIntent) -> str:
    try:
        _, digest = intent.media_identity.split(":", 1)
    except (AttributeError, ValueError):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_identity_invalid"
        ) from None
    slug = MEDIA_SLUG_PREFIX + digest
    if len(slug) > MAX_MEDIA_SLUG_LENGTH or _SLUG_PATTERN.fullmatch(slug) is None:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_identity_invalid"
        )
    return slug


def _emit_progress(
    callback: ProgressCallback | None,
    intent: gate_core.WordPressMediaUploadIntent,
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
        "sku": intent.sku,
        "selection_position": intent.selection_position,
        "stage": "wordpress_media",
        "status": status,
    }
    try:
        callback(event)
    except Exception:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_progress_callback_failed"
        ) from None


def _validated_response(response: object) -> WordPressMediaHttpResponse:
    if type(response) is not WordPressMediaHttpResponse:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_http_response_invalid"
        )
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_too_large"
        )
    return response


def _json_value(response: WordPressMediaHttpResponse) -> object:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_decode_failed"
        ) from None


def _source_location(
    source_url: object,
    *,
    fresh_settings: Settings,
    upload_filename: str,
) -> str:
    if type(source_url) is not str:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_invalid"
        )
    try:
        parsed_source = urlsplit(source_url)
        source_host = parsed_source.hostname.casefold() if parsed_source.hostname else ""
        target = urlsplit(fresh_settings.wp_base_url)
        target_host = target.hostname.casefold() if target.hostname else ""
    except (TypeError, ValueError):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_invalid"
        ) from None
    if gate_core._matches_domain(source_host, "xxxxdoll.com"):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_production_source_forbidden"
        )
    if (
        parsed_source.scheme.casefold() != "https"
        or source_host != target_host
        or not gate_core._matches_domain(source_host, "wpcomstaging.com")
        or parsed_source.username
        or parsed_source.password
        or parsed_source.query
        or parsed_source.fragment
        or unquote(Path(parsed_source.path).name) != upload_filename
    ):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_invalid"
        )
    digest = hashlib.sha256(
        f"wordpress-media-source-v1\0{source_host}\0{parsed_source.path}".encode(
            "utf-8"
        )
    ).hexdigest()
    return "wordpress-media-source-v1:" + digest


def _valid_source_location_fingerprint(value: object) -> bool:
    return type(value) is str and re.fullmatch(
        r"wordpress-media-source-v1:[0-9a-f]{64}", value, re.ASCII
    ) is not None


def _reference_from_record(
    raw: object,
    *,
    intent: gate_core.WordPressMediaUploadIntent,
    expected_slug: str,
    fresh_settings: Settings,
    upload_status: str,
) -> VerifiedWordPressMediaReference:
    if not isinstance(raw, Mapping):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_invalid"
        )
    media_id = raw.get("id")
    slug = raw.get("slug")
    mime_type = raw.get("mime_type")
    if (
        type(media_id) is not int
        or media_id <= 0
        or slug != expected_slug
        or mime_type != "image/webp"
    ):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_response_invalid"
        )
    location = _source_location(
        raw.get("source_url"),
        fresh_settings=fresh_settings,
        upload_filename=intent.upload_filename,
    )
    details = raw.get("media_details")
    if details is not None:
        if not isinstance(details, Mapping):
            raise WordPressMediaUploadTransportError(
                "wordpress_media_response_invalid"
            )
        remote_file = details.get("file")
        if remote_file is not None and (
            type(remote_file) is not str
            or unquote(Path(remote_file).name) != intent.upload_filename
        ):
            raise WordPressMediaUploadTransportError(
                "wordpress_media_response_invalid"
            )
    return VerifiedWordPressMediaReference(
        _REFERENCE_CAPABILITY,
        intent=intent,
        wordpress_media_id=media_id,
        wordpress_slug=slug,
        upload_status=upload_status,
        source_location_fingerprint=location,
    )


def _lookup(
    transport: WordPressMediaHttpTransport,
    *,
    intent: gate_core.WordPressMediaUploadIntent,
    slug: str,
    authorization: str,
    fresh_settings: Settings,
) -> VerifiedWordPressMediaReference | None:
    try:
        raw_response = transport.lookup_media(
            slug=slug, authorization=authorization
        )
    except Exception:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_lookup_failed"
        ) from None
    response = _validated_response(raw_response)
    if 300 <= response.status_code <= 399:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_redirect_forbidden"
        )
    if response.status_code != 200:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_lookup_failed"
        )
    value = _json_value(response)
    if not isinstance(value, list):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_lookup_response_invalid"
        )
    if len(value) > 1:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_identity_ambiguous"
        )
    if not value:
        return None
    return _reference_from_record(
        value[0],
        intent=intent,
        expected_slug=slug,
        fresh_settings=fresh_settings,
        upload_status="reused",
    )


def _read_verified_body(
    material: gate_core._WordPressMediaUploadMaterial,
) -> bytes:
    path = gate_core._local_upload_path_for_transport(material)
    try:
        body = path.read_bytes()
    except OSError:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_local_webp_unavailable"
        ) from None
    if (
        len(body) != material.size_bytes
        or hashlib.sha256(body).hexdigest() != material.sha256
        or material.mime_type != "image/webp"
    ):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_local_webp_changed"
        )
    return body


def _post(
    transport: WordPressMediaHttpTransport,
    *,
    intent: gate_core.WordPressMediaUploadIntent,
    material: gate_core._WordPressMediaUploadMaterial,
    slug: str,
    authorization: str,
    fresh_settings: Settings,
) -> VerifiedWordPressMediaReference:
    body = _read_verified_body(material)
    try:
        raw_response = transport.upload_media(
            slug=slug,
            upload_filename=material.upload_filename,
            body=body,
            authorization=authorization,
        )
    except WordPressMediaTransportNetworkError:
        raise
    except Exception:
        raise WordPressMediaTransportNetworkError() from None
    try:
        response = _validated_response(raw_response)
    except WordPressMediaUploadTransportError:
        raise WordPressMediaTransportNetworkError() from None
    if 300 <= response.status_code <= 399:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_redirect_forbidden"
        )
    if response.status_code in _DETERMINISTIC_POST_ERRORS:
        raise WordPressMediaUploadTransportError(
            _DETERMINISTIC_POST_ERRORS[response.status_code]
        )
    if response.status_code == 429 or 500 <= response.status_code <= 599:
        raise WordPressMediaTransportNetworkError(
            rate_limited=response.status_code == 429
        )
    if response.status_code != 201:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_upload_http_status_invalid"
        )
    try:
        value = _json_value(response)
    except WordPressMediaUploadTransportError as error:
        if str(error) in {
            "wordpress_media_response_decode_failed",
            "wordpress_media_response_too_large",
        }:
            raise WordPressMediaTransportNetworkError() from None
        raise
    return _reference_from_record(
        value,
        intent=intent,
        expected_slug=slug,
        fresh_settings=fresh_settings,
        upload_status="created",
    )


def _empty_item(
    intent: gate_core.WordPressMediaUploadIntent,
    *,
    status: str,
    blocker: str | None = None,
) -> dict[str, object]:
    return {
        "sku": intent.sku,
        "selection_position": intent.selection_position,
        "image_role": intent.image_role,
        "media_identity": intent.media_identity,
        "upload_filename": intent.upload_filename,
        "wordpress_media_id": None,
        "wordpress_slug": _media_slug(intent),
        "upload_status": status,
        "target_fingerprint": intent.target_host_fingerprint,
        "mime_type": "image/webp",
        "warnings": list(intent.warnings),
        "blocking_issues": [] if blocker is None else [blocker],
    }


def _summary(
    *,
    intents_received: int,
    lookup_requests: int,
    upload_requests: int,
    reconciliation_requests: int,
    created: int,
    reused: int,
    reconciled: int,
    failed_at_index: int | None,
) -> dict[str, int | None]:
    return {
        "intents_received": intents_received,
        "lookup_requests_performed": lookup_requests,
        "reconciliation_requests_performed": reconciliation_requests,
        "network_requests_performed": (
            lookup_requests + upload_requests + reconciliation_requests
        ),
        "wordpress_upload_requests_performed": upload_requests,
        "external_write_requests_performed": upload_requests,
        "write_requests_performed": upload_requests,
        "remote_media_created": created + reconciled,
        "remote_media_reused": reused,
        "created": created,
        "reused": reused,
        "created_reconciled": reconciled,
        "failed_at_index": failed_at_index,
        "references_created": created + reused + reconciled,
        "delete_requests_performed": 0,
        "rollback_requests_performed": 0,
    }


def execute_wordpress_media_uploads(
    intents_value: (
        gate_core.WordPressMediaUploadIntent
        | tuple[gate_core.WordPressMediaUploadIntent, ...]
    ),
    fresh_settings: Settings,
    credentials: WordPressApplicationPasswordCredentials,
    write_permit: WordPressMediaWritePermit,
    transport: WordPressMediaHttpTransport,
    *,
    progress_callback: ProgressCallback | None = None,
) -> WordPressMediaUploadTransportBatchResult:
    """Sequentially upload/reuse media with fail-stop, never rollback semantics."""

    intents = _normalize_intents(intents_value)
    if progress_callback is not None and not callable(progress_callback):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_progress_callback_invalid"
        )
    if not callable(getattr(transport, "lookup_media", None)) or not callable(
        getattr(transport, "upload_media", None)
    ):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_http_transport_required"
        )
    try:
        fresh_binding = gate_core._target_binding(fresh_settings)
    except gate_core.WordPressMediaUploadGateError as error:
        raise WordPressMediaUploadTransportError(str(error)) from None
    if any(intent.target_host_fingerprint != fresh_binding for intent in intents):
        raise WordPressMediaUploadTransportError(
            "wordpress_media_target_binding_changed"
        )
    if _permit_binding(write_permit) != fresh_binding:
        raise WordPressMediaUploadTransportError(
            "wordpress_media_write_permit_target_mismatch"
        )
    authorization = _authorization(credentials)

    references: list[VerifiedWordPressMediaReference] = []
    results: list[dict[str, object]] = []
    lookup_requests = 0
    upload_requests = 0
    reconciliation_requests = 0
    created = 0
    reused = 0
    reconciled = 0
    failed_at_index: int | None = None
    blocker: str | None = None

    for index, intent in enumerate(intents, start=1):
        if failed_at_index is not None:
            results.append(_empty_item(
                intent,
                status="not_attempted",
                blocker="wordpress_media_batch_stopped_after_failure",
            ))
            continue

        slug = _media_slug(intent)
        material: gate_core._WordPressMediaUploadMaterial | None = None
        reference: VerifiedWordPressMediaReference | None = None
        progress_status: str | None = None
        try:
            material = gate_core._upload_material_for_transport(intent)
            if material.target_host_fingerprint != fresh_binding:
                raise WordPressMediaUploadTransportError(
                    "wordpress_media_target_binding_changed"
                )

            _emit_progress(
                progress_callback,
                intent,
                current_index=index,
                total_items=len(intents),
                status="lookup_started",
            )
            lookup_requests += 1
            existing = _lookup(
                transport,
                intent=intent,
                slug=slug,
                authorization=authorization,
                fresh_settings=fresh_settings,
            )
            if existing is not None:
                reference = existing
                reused += 1
                progress_status = "lookup_reused"
            else:
                _emit_progress(
                    progress_callback,
                    intent,
                    current_index=index,
                    total_items=len(intents),
                    status="upload_started",
                )
                upload_requests += 1
                try:
                    reference = _post(
                        transport,
                        intent=intent,
                        material=material,
                        slug=slug,
                        authorization=authorization,
                        fresh_settings=fresh_settings,
                    )
                except WordPressMediaTransportNetworkError as uncertain:
                    reconciliation_requests += 1
                    try:
                        found_after_post = _lookup(
                            transport,
                            intent=intent,
                            slug=slug,
                            authorization=authorization,
                            fresh_settings=fresh_settings,
                        )
                    except WordPressMediaUploadTransportError as error:
                        if str(error) == "wordpress_media_identity_ambiguous":
                            raise
                        raise WordPressMediaUploadTransportError(
                            "wordpress_media_upload_outcome_unknown"
                        ) from None
                    if found_after_post is None:
                        raise WordPressMediaUploadTransportError(
                            "wordpress_media_upload_rate_limited"
                            if uncertain.rate_limited
                            else "wordpress_media_upload_outcome_unknown"
                        )
                    reference = VerifiedWordPressMediaReference(
                        _REFERENCE_CAPABILITY,
                        intent=intent,
                        wordpress_media_id=found_after_post.wordpress_media_id,
                        wordpress_slug=found_after_post.wordpress_slug,
                        upload_status="created_reconciled",
                        source_location_fingerprint=object.__getattribute__(
                            found_after_post,
                            "_VerifiedWordPressMediaReference__source_location_fingerprint",
                        ),
                    )
                    reconciled += 1
                    progress_status = "upload_reconciled"
                else:
                    created += 1
                    progress_status = "upload_created"

            if reference is None or progress_status is None:
                raise WordPressMediaUploadTransportError(
                    "wordpress_media_reference_invalid"
                )

            references.append(reference)
            item = reference.to_safe_dict()
            try:
                _emit_progress(
                    progress_callback,
                    intent,
                    current_index=index,
                    total_items=len(intents),
                    status=progress_status,
                )
            except WordPressMediaUploadTransportError:
                blocker = "wordpress_media_progress_callback_failed"
                item["blocking_issues"] = [blocker]
                results.append(item)
                failed_at_index = index
                continue
            results.append(item)
            continue
        except gate_core.WordPressMediaUploadGateError:
            blocker = "wordpress_media_upload_material_invalid"
        except WordPressMediaUploadTransportError as error:
            blocker = str(error)
        except Exception:
            blocker = "wordpress_media_lookup_failed"

        failed_at_index = index
        results.append(_empty_item(intent, status="blocked", blocker=blocker))
        try:
            _emit_progress(
                progress_callback,
                intent,
                current_index=index,
                total_items=len(intents),
                status="upload_blocked",
            )
        except WordPressMediaUploadTransportError:
            blocker = "wordpress_media_progress_callback_failed"
            results[-1] = _empty_item(intent, status="blocked", blocker=blocker)

    summary = _summary(
        intents_received=len(intents),
        lookup_requests=lookup_requests,
        upload_requests=upload_requests,
        reconciliation_requests=reconciliation_requests,
        created=created,
        reused=reused,
        reconciled=reconciled,
        failed_at_index=failed_at_index,
    )
    return WordPressMediaUploadTransportBatchResult(
        status="ok" if failed_at_index is None else "blocked",
        summary=summary,
        results=tuple(results),
        references=tuple(references),
        warnings=(),
        blocking_issues=() if blocker is None else (blocker,),
    )
