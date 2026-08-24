from __future__ import annotations

import json
import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.category_mapping import CategoryRegistry  # noqa: E402
from sync_worker.cli import (  # noqa: E402
    _run_discover_woo_categories,
    build_parser,
    main,
)
from sync_worker.woocommerce_category_discovery import (  # noqa: E402
    API_RESOURCE,
    API_VERSION,
    CATEGORY_ENDPOINT,
    DEFAULT_PER_PAGE,
    REPORT_FILENAME,
    StdlibWooCategoryTransport,
    WooCategoryConfigurationError,
    WooCategoryCredentialError,
    WooCategoryCredentials,
    WooCategoryDataError,
    WooCategoryDiscovery,
    WooCategoryPage,
    WooCategoryPaginationError,
    WooCategoryRetryableError,
    load_woo_category_credentials,
    normalize_woo_base_url,
    redactor_for_woo_category_credentials,
    run_woo_category_discovery,
)


def category(
    category_id: int,
    name: str,
    *,
    slug: str | None = None,
    parent: object = 0,
    count: object = 0,
) -> dict[str, object]:
    return {
        "id": category_id,
        "name": name,
        "slug": slug or name.casefold().replace(" ", "-"),
        "parent": parent,
        "count": count,
        "description": f"Description for {name}",
        "display": "default",
    }


class FakeTransport:
    def __init__(
        self,
        responses: dict[int, WooCategoryPage | Exception | list[object]],
        *,
        base_url: str = "https://shop.example.com",
        count_requests: bool = True,
    ) -> None:
        self.base_url = base_url
        self.responses = responses
        self.calls: list[tuple[int, int]] = []
        self._network_requests = 0
        self._count_requests = count_requests

    @property
    def network_requests_performed(self) -> int:
        return self._network_requests

    @property
    def write_requests_performed(self) -> int:
        return 0

    def get_categories(self, *, page: int, per_page: int = 100) -> WooCategoryPage:
        self.calls.append((page, per_page))
        if self._count_requests:
            self._network_requests += 1
        response = self.responses[page]
        if isinstance(response, list):
            if not response:
                raise AssertionError("response sequence was exhausted")
            current = response.pop(0)
        else:
            current = response
        if isinstance(current, Exception):
            raise current
        return current


class FakeSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class FakeHttpResponse:
    def __init__(
        self,
        items: object,
        *,
        status: int = 200,
        total: str | None = "1",
        total_pages: str | None = "1",
    ) -> None:
        self.status = status
        self._body = json.dumps(items).encode("utf-8")
        self._headers = {
            "X-WP-Total": total,
            "X-WP-TotalPages": total_pages,
        }

    def read(self, _: int) -> bytes:
        return self._body

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)


class FakeConnection:
    def __init__(
        self,
        response: FakeHttpResponse,
        *,
        connect_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.connect_error = connect_error
        self.sock = FakeSocket()
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> FakeHttpResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def page(
    items: list[dict[str, object]],
    *,
    total_pages: int | None = 1,
    total: int | None = None,
) -> WooCategoryPage:
    return WooCategoryPage(
        items=items,
        total=len(items) if total is None else total,
        total_pages=total_pages,
    )


def discovered(transport: FakeTransport) -> dict[str, object]:
    return WooCategoryDiscovery(transport, sleeper=lambda _: None).build_report()


class WooCategoryDiscoveryTests(unittest.TestCase):
    def test_01_cli_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            ["discover-woo-categories", "--base-url", "https://shop.example.com"]
        )
        self.assertEqual(arguments.command, "discover-woo-categories")
        self.assertEqual(arguments.base_url, "https://shop.example.com")

    def test_02_base_url_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["discover-woo-categories"])
        self.assertEqual(caught.exception.code, 2)

    def test_03_cli_has_no_credential_arguments(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "discover-woo-categories",
                    "--base-url",
                    "https://shop.example.com",
                    "--consumer-key",
                    "forbidden",
                ]
            )

    def test_04_https_is_required(self) -> None:
        with self.assertRaises(WooCategoryConfigurationError):
            normalize_woo_base_url("http://shop.example.com")

    def test_05_localhost_http_is_allowed_for_tests(self) -> None:
        self.assertEqual(
            normalize_woo_base_url("http://localhost:8765"),
            "http://localhost:8765",
        )

    def test_06_loopback_http_is_allowed_for_tests(self) -> None:
        self.assertEqual(
            normalize_woo_base_url("http://127.0.0.1:8765/"),
            "http://127.0.0.1:8765",
        )

    def test_07_base_url_rejects_embedded_authentication(self) -> None:
        with self.assertRaises(WooCategoryConfigurationError):
            normalize_woo_base_url("https://user:password@shop.example.com")

    def test_08_base_url_rejects_query_credentials(self) -> None:
        with self.assertRaises(WooCategoryConfigurationError):
            normalize_woo_base_url("https://shop.example.com?consumer_key=secret")

    def test_09_credentials_are_read_from_process_environment_mapping(self) -> None:
        credentials = load_woo_category_credentials(
            {"WC_CONSUMER_KEY": "ck_test", "WC_CONSUMER_SECRET": "cs_test"}
        )
        self.assertEqual(credentials.consumer_key, "ck_test")
        self.assertEqual(credentials.consumer_secret, "cs_test")

    def test_10_missing_consumer_key_is_rejected(self) -> None:
        with self.assertRaises(WooCategoryCredentialError):
            load_woo_category_credentials({"WC_CONSUMER_SECRET": "cs_test"})

    def test_11_missing_consumer_secret_is_rejected(self) -> None:
        with self.assertRaises(WooCategoryCredentialError):
            load_woo_category_credentials({"WC_CONSUMER_KEY": "ck_test"})

    def test_12_credential_loader_does_not_read_dotenv(self) -> None:
        with patch.object(Path, "read_text", side_effect=AssertionError(".env forbidden")):
            credentials = load_woo_category_credentials(
                {"WC_CONSUMER_KEY": "ck_test", "WC_CONSUMER_SECRET": "cs_test"}
            )
        self.assertEqual(credentials.consumer_key, "ck_test")

    def test_13_credentials_repr_is_secret_free(self) -> None:
        credentials = WooCategoryCredentials("ck_private_value", "cs_private_value")
        self.assertNotIn("ck_private_value", repr(credentials))
        self.assertNotIn("cs_private_value", repr(credentials))

    def test_14_credentials_are_redacted_from_cli_errors(self) -> None:
        credentials = WooCategoryCredentials("ck_private_value", "cs_private_value")
        logger = Mock(spec=logging.Logger)
        with patch(
            "sync_worker.cli.load_woo_category_credentials",
            return_value=credentials,
        ), patch(
            "sync_worker.cli.run_woo_category_discovery",
            side_effect=RuntimeError("ck_private_value cs_private_value"),
        ):
            status = _run_discover_woo_categories(logger, "https://shop.example.com")
        message = logger.error.call_args.args[0]
        self.assertEqual(status, 2)
        self.assertNotIn("ck_private_value", message)
        self.assertNotIn("cs_private_value", message)

    def test_15_transport_uses_get(self) -> None:
        connection = FakeConnection(FakeHttpResponse([category(1, "Root")]))
        credentials = WooCategoryCredentials("ck_test", "cs_test")
        with patch(
            "sync_worker.woocommerce_category_discovery.http.client.HTTPSConnection",
            return_value=connection,
        ):
            StdlibWooCategoryTransport(
                "https://shop.example.com", credentials
            ).get_categories(page=1)
        self.assertEqual(connection.requests[0][0], "GET")

    def test_16_transport_uses_wc_v3_category_endpoint(self) -> None:
        connection = FakeConnection(FakeHttpResponse([category(1, "Root")]))
        with patch(
            "sync_worker.woocommerce_category_discovery.http.client.HTTPSConnection",
            return_value=connection,
        ):
            StdlibWooCategoryTransport(
                "https://shop.example.com", WooCategoryCredentials("ck", "cs")
            ).get_categories(page=1)
        self.assertTrue(connection.requests[0][1].startswith(CATEGORY_ENDPOINT + "?"))

    def test_17_transport_uses_per_page_100(self) -> None:
        transport = FakeTransport({1: page([category(1, "Root")])})
        discovered(transport)
        self.assertEqual(transport.calls, [(1, DEFAULT_PER_PAGE)])

    def test_18_one_page_terminates(self) -> None:
        transport = FakeTransport({1: page([category(1, "Root")])})
        report = discovered(transport)
        self.assertEqual(report["summary"]["pages_read"], 1)

    def test_19_multiple_pages_are_read(self) -> None:
        transport = FakeTransport(
            {
                1: page([category(1, "Root")], total_pages=2, total=2),
                2: page([category(2, "Child", parent=1)], total_pages=2, total=2),
            }
        )
        report = discovered(transport)
        self.assertEqual(report["summary"]["pages_read"], 2)
        self.assertEqual(transport.calls, [(1, 100), (2, 100)])

    def test_20_page_results_are_sorted_by_id(self) -> None:
        transport = FakeTransport(
            {
                1: page([category(9, "Nine")], total_pages=2),
                2: page([category(2, "Two")], total_pages=2),
            }
        )
        ids = [item["id"] for item in discovered(transport)["categories"]]
        self.assertEqual(ids, [2, 9])

    def test_21_short_page_terminates_without_headers(self) -> None:
        transport = FakeTransport(
            {1: page([category(1, "Root")], total_pages=None)}
        )
        discovered(transport)
        self.assertEqual(transport.calls, [(1, 100)])

    def test_22_full_page_without_headers_reads_next_page(self) -> None:
        first_page = [category(index, f"Category {index}") for index in range(1, 101)]
        transport = FakeTransport(
            {
                1: page(first_page, total_pages=None),
                2: page([], total_pages=None),
            }
        )
        report = discovered(transport)
        self.assertEqual(report["summary"]["pages_read"], 2)

    def test_23_repeated_page_is_blocked(self) -> None:
        repeated = [category(1, "Root")]
        transport = FakeTransport(
            {
                1: page(repeated, total_pages=2),
                2: page(repeated, total_pages=2),
            }
        )
        with self.assertRaises(WooCategoryPaginationError):
            discovered(transport)

    def test_24_maximum_page_guard(self) -> None:
        full_page = [category(index, f"Category {index}") for index in range(1, 101)]
        transport = FakeTransport({1: page(full_page, total_pages=None)})
        with self.assertRaises(WooCategoryPaginationError):
            WooCategoryDiscovery(transport, max_pages=1).build_report()

    def test_25_category_id_is_preserved(self) -> None:
        report = discovered(FakeTransport({1: page([category(123, "Root")])}))
        self.assertEqual(report["categories"][0]["id"], 123)

    def test_26_category_name_is_preserved(self) -> None:
        report = discovered(FakeTransport({1: page([category(1, "Silicone Dolls")])}))
        self.assertEqual(report["categories"][0]["name"], "Silicone Dolls")

    def test_27_category_slug_is_preserved(self) -> None:
        report = discovered(FakeTransport({1: page([category(1, "Root", slug="root-slug")])}))
        self.assertEqual(report["categories"][0]["slug"], "root-slug")

    def test_28_category_parent_is_preserved(self) -> None:
        report = discovered(
            FakeTransport(
                {1: page([category(1, "Root"), category(2, "Child", parent=1)])}
            )
        )
        self.assertEqual(report["categories"][1]["parent"], 1)

    def test_29_category_count_is_preserved(self) -> None:
        report = discovered(FakeTransport({1: page([category(1, "Root", count=46)])}))
        self.assertEqual(report["categories"][0]["count"], 46)

    def test_30_root_category_summary(self) -> None:
        report = discovered(FakeTransport({1: page([category(1, "Root")])}))
        self.assertEqual(report["summary"]["root_categories"], 1)

    def test_31_child_category_summary(self) -> None:
        report = discovered(
            FakeTransport(
                {1: page([category(1, "Root"), category(2, "Child", parent=1)])}
            )
        )
        self.assertEqual(report["summary"]["child_categories"], 1)

    def test_32_category_path_is_built_locally(self) -> None:
        report = discovered(
            FakeTransport(
                {
                    1: page(
                        [
                            category(1, "Sex Dolls"),
                            category(2, "Silicone Dolls", parent=1),
                        ]
                    )
                }
            )
        )
        child = report["categories"][1]
        self.assertEqual(child["parent_name"], "Sex Dolls")
        self.assertEqual(child["category_path"], "Sex Dolls > Silicone Dolls")

    def test_33_missing_id_is_invalid(self) -> None:
        invalid = category(1, "Root")
        invalid.pop("id")
        with self.assertRaises(WooCategoryDataError):
            discovered(FakeTransport({1: page([invalid])}))

    def test_34_zero_id_is_invalid(self) -> None:
        with self.assertRaises(WooCategoryDataError):
            discovered(FakeTransport({1: page([category(0, "Root")])}))

    def test_35_negative_id_is_invalid(self) -> None:
        with self.assertRaises(WooCategoryDataError):
            discovered(FakeTransport({1: page([category(-1, "Root")])}))

    def test_36_duplicate_id_is_discovery_error(self) -> None:
        with self.assertRaises(WooCategoryDataError):
            discovered(
                FakeTransport(
                    {1: page([category(1, "First"), category(1, "Second")])}
                )
            )

    def test_37_invalid_parent_value_has_warning(self) -> None:
        report = discovered(FakeTransport({1: page([category(1, "Root", parent="bad")])}))
        record = report["categories"][0]
        self.assertIsNone(record["parent"])
        self.assertIn("invalid_parent_value", record["warnings"])

    def test_38_missing_parent_reference_has_warning(self) -> None:
        report = discovered(FakeTransport({1: page([category(2, "Child", parent=999)])}))
        self.assertIn("invalid_parent_reference", report["categories"][0]["warnings"])

    def test_39_total_categories_summary(self) -> None:
        report = discovered(
            FakeTransport({1: page([category(1, "One"), category(2, "Two")])})
        )
        self.assertEqual(report["summary"]["total_categories"], 2)

    def test_40_empty_and_nonempty_summaries(self) -> None:
        report = discovered(
            FakeTransport(
                {
                    1: page(
                        [category(1, "Empty", count=0), category(2, "Used", count=3)]
                    )
                }
            )
        )
        self.assertEqual(report["summary"]["empty_categories"], 1)
        self.assertEqual(report["summary"]["categories_with_products"], 1)

    def test_41_transport_exposes_no_post_put_patch_delete(self) -> None:
        transport = StdlibWooCategoryTransport(
            "https://shop.example.com", WooCategoryCredentials("ck", "cs")
        )
        for method in ("post", "put", "patch", "delete", "request"):
            with self.subTest(method=method):
                self.assertFalse(hasattr(transport, method))

    def test_42_write_requests_are_always_zero(self) -> None:
        transport = FakeTransport({1: page([category(1, "Root")])})
        report = discovered(transport)
        self.assertEqual(transport.write_requests_performed, 0)
        self.assertEqual(report["write_requests_performed"], 0)

    def test_43_consumer_key_is_absent_from_report(self) -> None:
        credentials = WooCategoryCredentials(
            "ck_ABCDEFGHIJKLMNOPQRSTUVWXYZ", "cs_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )
        report = WooCategoryDiscovery(
            FakeTransport({1: page([category(1, "Root")])}),
            redactor=redactor_for_woo_category_credentials(credentials),
        ).build_report()
        self.assertNotIn(credentials.consumer_key, json.dumps(report))

    def test_44_consumer_secret_is_absent_from_report(self) -> None:
        credentials = WooCategoryCredentials(
            "ck_ABCDEFGHIJKLMNOPQRSTUVWXYZ", "cs_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )
        report = WooCategoryDiscovery(
            FakeTransport({1: page([category(1, "Root")])}),
            redactor=redactor_for_woo_category_credentials(credentials),
        ).build_report()
        self.assertNotIn(credentials.consumer_secret, json.dumps(report))

    def test_45_auth_cookie_password_token_are_absent_from_report(self) -> None:
        sensitive = category(1, "Authorization: Basic abc")
        sensitive["description"] = "Cookie=session password=abc token=xyz"
        serialized = json.dumps(
            discovered(FakeTransport({1: page([sensitive])})),
            ensure_ascii=False,
        ).casefold()
        for forbidden in ("authorization", "cookie", "password", "token"):
            self.assertNotIn(forbidden, serialized)

    def test_46_limited_retry_succeeds(self) -> None:
        transport = FakeTransport(
            {
                1: [
                    WooCategoryRetryableError("TimeoutError"),
                    page([category(1, "Root")]),
                ]
            }
        )
        report = WooCategoryDiscovery(transport, sleeper=lambda _: None).build_report()
        self.assertEqual(report["summary"]["total_categories"], 1)
        self.assertEqual(len(transport.calls), 2)

    def test_47_retry_is_bounded(self) -> None:
        transport = FakeTransport(
            {
                1: [
                    WooCategoryRetryableError("reset"),
                    WooCategoryRetryableError("reset"),
                    WooCategoryRetryableError("reset"),
                ]
            }
        )
        with self.assertRaises(WooCategoryRetryableError):
            WooCategoryDiscovery(transport, sleeper=lambda _: None).build_report()
        self.assertEqual(len(transport.calls), 3)

    def test_48_timeout_is_retryable(self) -> None:
        connection = FakeConnection(
            FakeHttpResponse([]),
            connect_error=TimeoutError("private endpoint detail"),
        )
        with patch(
            "sync_worker.woocommerce_category_discovery.http.client.HTTPSConnection",
            return_value=connection,
        ):
            transport = StdlibWooCategoryTransport(
                "https://shop.example.com", WooCategoryCredentials("ck", "cs")
            )
            with self.assertRaises(WooCategoryRetryableError) as caught:
                transport.get_categories(page=1)
        self.assertNotIn("private endpoint detail", str(caught.exception))

    def test_49_connection_reset_is_retryable(self) -> None:
        connection = FakeConnection(
            FakeHttpResponse([]),
            connect_error=ConnectionResetError("private endpoint detail"),
        )
        with patch(
            "sync_worker.woocommerce_category_discovery.http.client.HTTPSConnection",
            return_value=connection,
        ):
            transport = StdlibWooCategoryTransport(
                "https://shop.example.com", WooCategoryCredentials("ck", "cs")
            )
            with self.assertRaises(WooCategoryRetryableError):
                transport.get_categories(page=1)

    def test_50_output_is_deterministic(self) -> None:
        first = discovered(
            FakeTransport({1: page([category(2, "Two"), category(1, "One")])})
        )
        second = discovered(
            FakeTransport({1: page([category(2, "Two"), category(1, "One")])})
        )
        self.assertEqual(first, second)

    def test_51_api_metadata_is_read_only(self) -> None:
        report = discovered(FakeTransport({1: page([])}))
        self.assertEqual(
            report["api"],
            {"version": API_VERSION, "resource": API_RESOURCE, "read_only": True},
        )

    def test_52_source_metadata_uses_fixed_endpoint(self) -> None:
        source = discovered(
            FakeTransport({1: page([category(1, "Root")])})
        )["categories"][0]["source"]
        self.assertEqual(source["api_version"], API_VERSION)
        self.assertEqual(source["endpoint"], CATEGORY_ENDPOINT)

    def test_53_discovery_does_not_construct_category_bindings(self) -> None:
        with patch.object(
            CategoryRegistry,
            "map_product",
            side_effect=AssertionError("binding forbidden"),
        ):
            report = discovered(FakeTransport({1: page([category(1, "CLM Ultra")])}))
        serialized = json.dumps(report)
        self.assertNotIn("category_key", serialized)
        self.assertNotIn("woo_category_id", serialized)

    def test_54_category_name_does_not_trigger_fuzzy_mapping(self) -> None:
        record = discovered(
            FakeTransport({1: page([category(1, "Pro Ultra")])})
        )["categories"][0]
        self.assertEqual(record["name"], "Pro Ultra")
        self.assertNotIn("clm-", json.dumps(record))

    def test_55_mock_development_run_opens_no_socket(self) -> None:
        transport = FakeTransport(
            {1: page([category(1, "Root")])},
            count_requests=False,
        )
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network forbidden"),
        ), patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("network forbidden"),
        ):
            report = discovered(transport)
        self.assertEqual(report["network_requests_performed"], 0)

    def test_56_runner_writes_only_expected_local_report(self) -> None:
        credentials = WooCategoryCredentials("ck_test", "cs_test")
        transport = FakeTransport({1: page([category(1, "Root")])})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, output_path = run_woo_category_discovery(
                "https://shop.example.com",
                credentials,
                project_root=root,
                transport=transport,
            )
            files = sorted(path.name for path in (root / "reports").iterdir())
        self.assertEqual(report["write_requests_performed"], 0)
        self.assertEqual(output_path.name, REPORT_FILENAME)
        self.assertEqual(files, [REPORT_FILENAME])

    def test_57_cli_calls_environment_loader_and_runner(self) -> None:
        credentials = WooCategoryCredentials("ck_test", "cs_test")
        mock_report = {
            "status": "ok",
            "summary": {},
            "network_requests_performed": 1,
            "write_requests_performed": 0,
        }
        with patch(
            "sync_worker.cli.load_woo_category_credentials",
            return_value=credentials,
        ) as loader, patch(
            "sync_worker.cli.run_woo_category_discovery",
            return_value=(mock_report, Path(REPORT_FILENAME)),
        ) as runner:
            status = main(
                ["discover-woo-categories", "--base-url", "https://shop.example.com"]
            )
        self.assertEqual(status, 0)
        loader.assert_called_once_with()
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
