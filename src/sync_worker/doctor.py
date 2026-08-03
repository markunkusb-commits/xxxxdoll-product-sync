"""Read-only WordPress and WooCommerce connection diagnostics."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .http_client import HttpResponse, ReadOnlyHttpClient
from .sanitization import Redactor
from .security import (
    assert_safe_staging_runtime,
    basic_auth_headers,
    redactor_for_settings,
)


def _success(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_string(value: object) -> str:
    return value[:200] if isinstance(value, str) else ""


def _discovery_strings(payload: object) -> set[str]:
    if not isinstance(payload, Mapping):
        return set()
    discovered: set[str] = set()
    namespaces = payload.get("namespaces")
    if isinstance(namespaces, list):
        discovered.update(
            item.lower() for item in namespaces if isinstance(item, str)
        )
    routes = payload.get("routes")
    if isinstance(routes, Mapping):
        discovered.update(str(route).lower() for route in routes)
    return discovered


def _detect_woocommerce(payload: object) -> str:
    discovered = _discovery_strings(payload)
    return "detected" if any("wc/v" in item for item in discovered) else "not_detected"


def _detect_yith(payload: object) -> str:
    discovered = _discovery_strings(payload)
    markers = (
        "addon",
        "add-on",
        "add_ons",
        "product-add-ons",
        "extra-options",
        "wapo",
    )
    if any("yith" in item and any(marker in item for marker in markers) for item in discovered):
        return "detected"
    return "requires_wordpress_bridge"


@dataclass(slots=True)
class _FetchResult:
    status_code: int | None
    payload: object | None


class DoctorRunner:
    """Run allowlisted diagnostics and retain only explicitly safe response fields."""

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
        self._logger = logger or logging.getLogger("sync_worker.doctor")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._errors: list[dict[str, str]] = []

    def _log(self, event: str, **fields: object) -> None:
        payload = self._redactor.value({"event": event, **fields})
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _assert_safe_before_network(self) -> None:
        assert_safe_staging_runtime(self._settings, self._client)

    def _fetch_json(
        self,
        check: str,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _FetchResult:
        try:
            response: HttpResponse = self._client.get(
                path, query=query, headers=headers
            )
        except Exception as error:
            summary = self._redactor.exception(error)
            self._errors.append({"check": check, "summary": summary})
            self._log("doctor_check_failed", check=check, error=summary)
            return _FetchResult(status_code=None, payload=None)

        if not _success(response.status_code):
            self._errors.append(
                {"check": check, "summary": f"HTTP status {response.status_code}"}
            )
            self._log(
                "doctor_check_complete",
                check=check,
                ok=False,
                http_status=response.status_code,
            )
            return _FetchResult(status_code=response.status_code, payload=None)

        try:
            payload = response.json()
        except Exception as error:
            summary = self._redactor.exception(error)
            self._errors.append({"check": check, "summary": summary})
            self._log("doctor_check_failed", check=check, error=summary)
            return _FetchResult(status_code=response.status_code, payload=None)

        self._log(
            "doctor_check_complete",
            check=check,
            ok=True,
            http_status=response.status_code,
        )
        return _FetchResult(status_code=response.status_code, payload=payload)

    @staticmethod
    def _status(status_code: int | None, payload_valid: bool) -> dict[str, object]:
        return {"ok": _success(status_code) and payload_valid, "http_status": status_code}

    def run(self) -> dict[str, object]:
        self._assert_safe_before_network()
        self._log("doctor_started", hostname=self._settings.masked_hostname())

        wordpress_auth = basic_auth_headers(
            self._settings.wp_username, self._settings.wp_app_password
        )
        woocommerce_auth = basic_auth_headers(
            self._settings.wc_consumer_key, self._settings.wc_consumer_secret
        )

        rest_root = self._fetch_json("wordpress_rest", "/wp-json/")
        current_user = self._fetch_json(
            "wordpress_auth",
            "/wp-json/wp/v2/users/me",
            query={"context": "view"},
            headers=wordpress_auth,
        )
        media = self._fetch_json(
            "media_auth",
            "/wp-json/wp/v2/media",
            query={"context": "view", "per_page": 1},
            headers=wordpress_auth,
        )
        products = self._fetch_json(
            "woocommerce_auth",
            "/wp-json/wc/v3/products",
            query={"context": "view", "per_page": 5},
            headers=woocommerce_auth,
        )
        categories = self._fetch_json(
            "product_categories",
            "/wp-json/wc/v3/products/categories",
            query={"per_page": 100},
            headers=woocommerce_auth,
        )
        attributes = self._fetch_json(
            "product_attributes",
            "/wp-json/wc/v3/products/attributes",
            query={"per_page": 100},
            headers=woocommerce_auth,
        )
        taxonomies = self._fetch_json(
            "product_taxonomies", "/wp-json/wp/v2/taxonomies"
        )

        safe_products = []
        if isinstance(products.payload, list):
            for product in products.payload[:5]:
                if isinstance(product, Mapping):
                    safe_products.append(
                        {
                            "id": _safe_int(product.get("id")),
                            "sku": _safe_string(product.get("sku")),
                            "status": _safe_string(product.get("status")),
                            "type": _safe_string(product.get("type")),
                        }
                    )

        safe_categories = []
        if isinstance(categories.payload, list):
            for category in categories.payload:
                if isinstance(category, Mapping):
                    safe_categories.append(
                        {
                            "id": _safe_int(category.get("id")),
                            "name": _safe_string(category.get("name")),
                            "parent_id": _safe_int(category.get("parent")),
                        }
                    )

        safe_attributes = []
        if isinstance(attributes.payload, list):
            for attribute in attributes.payload:
                if isinstance(attribute, Mapping):
                    safe_attributes.append(
                        {
                            "id": _safe_int(attribute.get("id")),
                            "name": _safe_string(attribute.get("name")),
                            "slug": _safe_string(attribute.get("slug")),
                        }
                    )

        safe_taxonomies = []
        if isinstance(taxonomies.payload, Mapping):
            for key, taxonomy in taxonomies.payload.items():
                if not isinstance(taxonomy, Mapping):
                    continue
                slug = _safe_string(taxonomy.get("slug")) or _safe_string(key)
                object_types = taxonomy.get("types", taxonomy.get("object_type", []))
                relates_to_product = (
                    isinstance(object_types, list) and "product" in object_types
                ) or slug in {"product_cat", "product_tag"} or slug.startswith("pa_")
                if relates_to_product:
                    safe_taxonomies.append(
                        {
                            "slug": slug,
                            "name": _safe_string(taxonomy.get("name")),
                            "rest_base": _safe_string(taxonomy.get("rest_base")),
                        }
                    )

        user_valid = isinstance(current_user.payload, Mapping) and _safe_int(
            current_user.payload.get("id")
        ) is not None
        media_valid = isinstance(media.payload, list)
        products_valid = isinstance(products.payload, list)
        categories_valid = isinstance(categories.payload, list)
        attributes_valid = isinstance(attributes.payload, list)
        taxonomies_valid = isinstance(taxonomies.payload, Mapping)
        root_valid = isinstance(rest_root.payload, Mapping)

        report: dict[str, object] = {
            "timestamp": self._clock().astimezone(timezone.utc).isoformat(),
            "staging_hostname": self._settings.masked_hostname(),
            "wordpress_rest": self._status(rest_root.status_code, root_valid),
            "wordpress_auth": self._status(current_user.status_code, user_valid),
            "media_auth": self._status(media.status_code, media_valid),
            "woocommerce_auth": self._status(products.status_code, products_valid),
            "woocommerce_detection": _detect_woocommerce(rest_root.payload),
            "products": safe_products,
            "product_categories_status": self._status(
                categories.status_code, categories_valid
            ),
            "product_categories": safe_categories,
            "product_attributes_status": self._status(
                attributes.status_code, attributes_valid
            ),
            "product_attributes": safe_attributes,
            "product_taxonomies_status": self._status(
                taxonomies.status_code, taxonomies_valid
            ),
            "product_taxonomies": safe_taxonomies,
            "yith_detection": _detect_yith(rest_root.payload),
            "get_requests": self._client.counters.get_requests,
            "head_requests": self._client.counters.head_requests,
            "write_requests_performed": 0,
            "errors": self._errors,
        }
        self._log(
            "doctor_complete",
            get_requests=self._client.counters.get_requests,
            head_requests=self._client.counters.head_requests,
            write_requests_performed=0,
        )
        sanitized_report = self._redactor.value(report)
        if not isinstance(sanitized_report, dict):
            raise TypeError("Doctor report sanitization failed")
        return sanitized_report
