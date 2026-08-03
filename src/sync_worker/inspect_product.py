"""Safe, read-only extraction of one reference WooCommerce product."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .config import Settings
from .http_client import ReadOnlyHttpClient
from .sanitization import Redactor
from .security import (
    assert_safe_staging_runtime,
    basic_auth_headers,
    redactor_for_settings,
)


_SKU_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WHITELISTED_META_KEYS = frozenset(
    {
        "_product_addons_exclude_global",
        "original_folder",
        "local_image_folder",
    }
)
_BRAND_FIELDS = ("brands", "brand", "product_brand", "product_brands")
_PRODUCT_TAXONOMY_FIELDS = ("tags", "product_tags", "product_taxonomy_terms")


def validate_sku(sku: str) -> str:
    normalized = sku.strip()
    if not _SKU_PATTERN.fullmatch(normalized):
        raise ValueError("SKU must use 1-128 letters, numbers, dots, dashes, or underscores")
    return normalized


def reference_product_report_filename(sku: str) -> str:
    return f"reference-product-{validate_sku(sku)}.json"


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_string(value: object, *, limit: int = 300) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _file_name(reference: object) -> str:
    if not isinstance(reference, str) or not reference:
        return ""
    try:
        path = urlsplit(reference).path
    except ValueError:
        return ""
    name = PurePosixPath(unquote(path).replace("\\", "/")).name
    return name[:255]


class _DescriptionImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_file_names: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "img":
            return
        attributes = {name.lower(): value for name, value in attrs}
        source = next(
            (
                attributes.get(name)
                for name in ("src", "data-src", "data-lazy-src")
                if attributes.get(name)
            ),
            "",
        )
        self.image_file_names.append(_file_name(source))


def _description_summary(product: Mapping[str, object]) -> dict[str, object]:
    short_description = _safe_string(
        product.get("short_description"), limit=2_000_000
    )
    description = _safe_string(product.get("description"), limit=2_000_000)
    parser = _DescriptionImageParser()
    parser.feed(description)
    parser.close()
    ordered_names = parser.image_file_names[:500]
    return {
        "short_description_characters": len(short_description),
        "description_characters": len(description),
        "description_image_count": len(parser.image_file_names),
        "description_image_file_names": ordered_names,
        "contains_spec_webp": any(
            name.lower() == "spec.webp" for name in parser.image_file_names
        ),
    }


def _term(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    term_id = _safe_int(value.get("term_id"))
    if term_id is None:
        term_id = _safe_int(value.get("id"))
    return {
        "term_id": term_id,
        "name": _safe_string(value.get("name")),
        "slug": _safe_string(value.get("slug")),
    }


def _terms(value: object, *, limit: int = 100) -> list[dict[str, object]]:
    candidates: list[object]
    if isinstance(value, list):
        candidates = value[:limit]
    elif isinstance(value, Mapping):
        candidates = [value]
    else:
        candidates = []
    results: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for candidate in candidates:
        item = _term(candidate)
        if item is None:
            continue
        identity = (item["term_id"], item["name"], item["slug"])
        if identity not in seen:
            seen.add(identity)
            results.append(item)
    return results


def _categories(product: Mapping[str, object]) -> list[dict[str, object]]:
    values = product.get("categories")
    if not isinstance(values, list):
        return []
    results = []
    for value in values[:100]:
        if isinstance(value, Mapping):
            results.append(
                {
                    "id": _safe_int(value.get("id")),
                    "name": _safe_string(value.get("name")),
                    "slug": _safe_string(value.get("slug")),
                }
            )
    return results


def _taxonomy_terms(
    product: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    brand_terms: list[dict[str, object]] = []
    product_terms: list[dict[str, object]] = []
    for field in _BRAND_FIELDS:
        brand_terms.extend(_terms(product.get(field)))
    for field in _PRODUCT_TAXONOMY_FIELDS:
        product_terms.extend(_terms(product.get(field)))

    taxonomies = product.get("taxonomies")
    if isinstance(taxonomies, Mapping):
        for taxonomy_name, values in list(taxonomies.items())[:100]:
            destination = (
                brand_terms
                if "brand" in str(taxonomy_name).lower()
                else product_terms
            )
            if isinstance(values, list):
                destination.extend(_terms(values))
            else:
                destination.extend(_terms(values))
    return _deduplicate_terms(brand_terms), _deduplicate_terms(product_terms)


def _deduplicate_terms(
    terms: list[dict[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for item in terms[:200]:
        identity = (item.get("term_id"), item.get("name"), item.get("slug"))
        if identity not in seen:
            seen.add(identity)
            results.append(item)
    return results


def _attributes(product: Mapping[str, object]) -> list[dict[str, object]]:
    values = product.get("attributes")
    if not isinstance(values, list):
        return []
    results = []
    for value in values[:100]:
        if not isinstance(value, Mapping):
            continue
        options = value.get("options")
        safe_options = (
            [_safe_string(option) for option in options[:100]]
            if isinstance(options, list)
            else []
        )
        results.append(
            {
                "id": _safe_int(value.get("id")),
                "name": _safe_string(value.get("name")),
                "slug": _safe_string(value.get("slug")),
                "position": _safe_int(value.get("position")),
                "visible": _safe_bool(value.get("visible")),
                "variation": _safe_bool(value.get("variation")),
                "options": safe_options,
            }
        )
    return results


def _image_manifest(product: Mapping[str, object]) -> list[dict[str, object]]:
    images = product.get("images")
    if not isinstance(images, list):
        return []
    manifest = []
    for position, image in enumerate(images[:100]):
        if not isinstance(image, Mapping):
            continue
        file_name = _file_name(image.get("src")) or _file_name(image.get("name"))
        manifest.append(
            {
                "attachment_id": _safe_int(image.get("id")),
                "position": position,
                "file_name": file_name,
                "alt": _safe_string(image.get("alt")),
                "is_primary": position == 0,
            }
        )
    return manifest


def _safe_folder_value(value: object, redactor: Redactor) -> object:
    if not isinstance(value, str):
        return value if value is None or isinstance(value, (bool, int, float)) else None
    normalized = value.replace("\\", "/").strip()
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in parts
    ):
        return "[REDACTED_PATH]"
    return redactor.text(normalized, limit=300)


def _meta_summary(
    product: Mapping[str, object], redactor: Redactor
) -> tuple[dict[str, object], list[dict[str, object]], bool | None]:
    metadata = product.get("meta_data")
    if not isinstance(metadata, list):
        return {}, [], None

    whitelisted: dict[str, object] = {}
    unknown: list[dict[str, object]] = []
    seen_unknown: set[str] = set()
    for item in metadata[:1000]:
        if not isinstance(item, Mapping):
            continue
        key = item.get("key")
        if not isinstance(key, str):
            continue
        if key in _WHITELISTED_META_KEYS:
            if key == "_product_addons_exclude_global":
                whitelisted[key] = _flag_value(item.get("value"))
            else:
                whitelisted[key] = _safe_folder_value(item.get("value"), redactor)
        elif key not in seen_unknown and len(unknown) < 500:
            seen_unknown.add(key)
            unknown.append(
                {"key": redactor.text(key, limit=200), "review_required": True}
            )

    excluded = _flag_value(whitelisted.get("_product_addons_exclude_global"))
    return whitelisted, unknown, excluded


def _flag_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 0:
            return False
        if value == 1:
            return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return None


def _yith_inheritance(
    categories: list[dict[str, object]], excluded: bool | None
) -> str:
    belongs_to_md_dolls = any(
        str(category.get("name", "")).strip().casefold() == "md dolls"
        or str(category.get("slug", "")).strip().casefold() == "md-dolls"
        for category in categories
    )
    if excluded is True:
        return "likely_disabled"
    if excluded is False and belongs_to_md_dolls:
        return "likely_enabled"
    return "unable_to_confirm"


def _empty_report(
    settings: Settings, sku: str, timestamp: datetime
) -> dict[str, object]:
    return {
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
        "staging_hostname": settings.masked_hostname(),
        "requested_sku": sku,
        "status": "pending",
        "http_status": None,
        "product_found": False,
        "product_id": None,
        "product": {},
        "categories": [],
        "brand_terms": [],
        "product_taxonomy_terms": [],
        "attributes": [],
        "image_manifest": [],
        "description_summary": {
            "short_description_characters": 0,
            "description_characters": 0,
            "description_image_count": 0,
            "description_image_file_names": [],
            "contains_spec_webp": False,
        },
        "whitelisted_meta": {},
        "unknown_meta_keys": [],
        "global_addons_excluded": None,
        "yith_global_options_inheritance": "unable_to_confirm",
        "get_requests": 0,
        "head_requests": 0,
        "write_requests_performed": 0,
        "sanitized_errors": [],
    }


class ReferenceProductInspector:
    """Query one exact SKU and return an allowlisted structural report."""

    def __init__(
        self,
        settings: Settings,
        client: ReadOnlyHttpClient,
        *,
        redactor: Redactor | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._redactor = redactor or redactor_for_settings(settings)
        self._logger = logger or logging.getLogger("sync_worker.inspect_product")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _log(self, event: str, **fields: object) -> None:
        payload = self._redactor.value({"event": event, **fields})
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _finalize(self, report: dict[str, object]) -> dict[str, object]:
        report["get_requests"] = self._client.counters.get_requests
        report["head_requests"] = self._client.counters.head_requests
        report["write_requests_performed"] = 0
        sanitized = self._redactor.value(report)
        if not isinstance(sanitized, dict):
            raise TypeError("Reference product report sanitization failed")
        self._log(
            "inspect_product_complete",
            status=sanitized.get("status"),
            product_found=sanitized.get("product_found"),
            get_requests=self._client.counters.get_requests,
            head_requests=self._client.counters.head_requests,
            write_requests_performed=0,
        )
        return sanitized

    def run(self, sku: str) -> dict[str, object]:
        assert_safe_staging_runtime(self._settings, self._client)
        safe_sku = validate_sku(sku)
        report = _empty_report(self._settings, safe_sku, self._clock())
        self._log("inspect_product_started")

        headers = basic_auth_headers(
            self._settings.wc_consumer_key, self._settings.wc_consumer_secret
        )
        try:
            response = self._client.get(
                "/wp-json/wc/v3/products",
                query={"sku": safe_sku},
                headers=headers,
            )
        except Exception as error:
            report["status"] = "request_failed"
            report["sanitized_errors"] = [self._redactor.exception(error)]
            return self._finalize(report)

        report["http_status"] = response.status_code
        if not 200 <= response.status_code < 300:
            report["status"] = "request_failed"
            report["sanitized_errors"] = [f"HTTP status {response.status_code}"]
            return self._finalize(report)

        try:
            payload = response.json()
        except Exception as error:
            report["status"] = "invalid_response"
            report["sanitized_errors"] = [self._redactor.exception(error)]
            return self._finalize(report)
        if not isinstance(payload, list):
            report["status"] = "invalid_response"
            report["sanitized_errors"] = ["Expected a product list response"]
            return self._finalize(report)

        matching_products = [
            product
            for product in payload
            if isinstance(product, Mapping) and product.get("sku") == safe_sku
        ]
        if len(matching_products) == 0:
            report["status"] = "product_not_found"
            return self._finalize(report)
        if len(matching_products) > 1:
            report["status"] = "duplicate_sku_error"
            return self._finalize(report)

        product = matching_products[0]
        categories = _categories(product)
        brand_terms, product_terms = _taxonomy_terms(product)
        whitelisted_meta, unknown_meta, addons_excluded = _meta_summary(
            product, self._redactor
        )
        product_id = _safe_int(product.get("id"))
        report.update(
            {
                "status": "ok",
                "product_found": True,
                "product_id": product_id,
                "product": {
                    "id": product_id,
                    "name": _safe_string(product.get("name")),
                    "slug": _safe_string(product.get("slug")),
                    "sku": _safe_string(product.get("sku")),
                    "type": _safe_string(product.get("type")),
                    "status": _safe_string(product.get("status")),
                    "regular_price": _safe_string(product.get("regular_price")),
                    "sale_price": _safe_string(product.get("sale_price")),
                    "stock_status": _safe_string(product.get("stock_status")),
                    "catalog_visibility": _safe_string(
                        product.get("catalog_visibility")
                    ),
                    "featured": _safe_bool(product.get("featured")),
                    "virtual": _safe_bool(product.get("virtual")),
                    "downloadable": _safe_bool(product.get("downloadable")),
                },
                "categories": categories,
                "brand_terms": brand_terms,
                "product_taxonomy_terms": product_terms,
                "attributes": _attributes(product),
                "image_manifest": _image_manifest(product),
                "description_summary": _description_summary(product),
                "whitelisted_meta": whitelisted_meta,
                "unknown_meta_keys": unknown_meta,
                "global_addons_excluded": addons_excluded,
                "yith_global_options_inheritance": _yith_inheritance(
                    categories, addons_excluded
                ),
            }
        )
        return self._finalize(report)
