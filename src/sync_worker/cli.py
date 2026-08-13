"""Command-line entry points for sync_worker."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from .config import load_config, load_google_config
from .doctor import DoctorRunner
from .http_client import ReadOnlyHttpClient
from .google_api import OfficialGoogleClientFactory, google_redactor_for_settings
from .google_doctor import GoogleDoctorRunner
from .inspect_product import (
    ReferenceProductInspector,
    reference_product_report_filename,
)
from .report import (
    DoctorReportWriter,
    ReferenceProductReportWriter,
    SafeJsonReportWriter,
)
from .sanitization import Redactor
from .security import redactor_for_settings
from .supplier_inventory import SupplierInventoryRunner, validate_max_depth


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configure_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return logging.getLogger("sync_worker")


def _log_failure(
    logger: logging.Logger, error: BaseException, *, event: str
) -> None:
    summary = Redactor().exception(error)
    logger.error(
        json.dumps(
            {"event": event, "error": summary},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _run_doctor(logger: logging.Logger) -> int:
    try:
        settings = load_config()
    except Exception as error:
        _log_failure(logger, error, event="doctor_aborted")
        return 2

    redactor = redactor_for_settings(settings)
    try:
        client = ReadOnlyHttpClient(
            settings.wp_base_url,
            redactor=redactor,
        )
        report = DoctorRunner(
            settings,
            client,
            redactor=redactor,
            logger=logger,
        ).run()
        report_path = PROJECT_ROOT / "reports" / "doctor-report.json"
        DoctorReportWriter(report_path, redactor).write(report)
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "doctor_aborted",
                    "error": redactor.exception(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    logger.info(
        json.dumps(
            {
                "event": "doctor_report_written",
                "path": "reports/doctor-report.json",
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if not report.get("errors") else 1


def _run_inspect_product(logger: logging.Logger, sku: str) -> int:
    try:
        settings = load_config()
    except Exception as error:
        _log_failure(logger, error, event="inspect_product_aborted")
        return 2

    redactor = redactor_for_settings(settings)
    try:
        report_name = reference_product_report_filename(sku)
        client = ReadOnlyHttpClient(settings.wp_base_url, redactor=redactor)
        report = ReferenceProductInspector(
            settings,
            client,
            redactor=redactor,
            logger=logger,
        ).run(sku)
        report_path = PROJECT_ROOT / "reports" / report_name
        ReferenceProductReportWriter(report_path, redactor).write(report)
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "inspect_product_aborted",
                    "error": redactor.exception(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    logger.info(
        json.dumps(
            {
                "event": "reference_product_report_written",
                "path": f"reports/{report_name}",
                "status": report.get("status"),
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if report.get("status") == "ok":
        return 0
    if report.get("status") in {"product_not_found", "duplicate_sku_error"}:
        return 3
    return 1


def _run_google_doctor(logger: logging.Logger) -> int:
    try:
        settings = load_google_config()
    except Exception as error:
        _log_failure(logger, error, event="google_doctor_aborted")
        return 2

    redactor = google_redactor_for_settings(settings)
    try:
        report = GoogleDoctorRunner(
            settings,
            OfficialGoogleClientFactory(),
            redactor=redactor,
            logger=logger,
        ).run()
        report_path = PROJECT_ROOT / "reports" / "google-doctor-report.json"
        SafeJsonReportWriter(report_path, redactor).write(report)
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "google_doctor_aborted",
                    "error": redactor.exception(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    logger.info(
        json.dumps(
            {
                "event": "google_doctor_report_written",
                "path": "reports/google-doctor-report.json",
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return (
        0
        if report.get("drive_api_status") and report.get("sheets_api_status")
        else 1
    )


def _run_supplier_inventory(logger: logging.Logger, max_depth: int) -> int:
    try:
        settings = load_google_config()
    except Exception as error:
        _log_failure(logger, error, event="supplier_inventory_aborted")
        return 2

    redactor = google_redactor_for_settings(settings)
    try:
        report = SupplierInventoryRunner(
            settings,
            OfficialGoogleClientFactory(),
            max_depth=max_depth,
            redactor=redactor,
            logger=logger,
        ).run()
        report_path = PROJECT_ROOT / "reports" / "supplier-inventory.json"
        SafeJsonReportWriter(report_path, redactor).write(report)
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "supplier_inventory_aborted",
                    "error": redactor.exception(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    logger.info(
        json.dumps(
            {
                "event": "supplier_inventory_report_written",
                "path": "reports/supplier-inventory.json",
                "status": report.get("status"),
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _max_depth_argument(value: str) -> int:
    try:
        return validate_max_depth(int(value))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sync_worker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "doctor", help="Run read-only staging connection diagnostics"
    )
    inspect_product = subcommands.add_parser(
        "inspect-product", help="Inspect one reference product by exact SKU"
    )
    inspect_product.add_argument("--sku", required=True, help="Exact product SKU")
    subcommands.add_parser(
        "google-doctor", help="Run read-only Google Drive and Sheets diagnostics"
    )
    supplier_inventory = subcommands.add_parser(
        "supplier-inventory",
        help="Build a bounded read-only supplier Drive and Sheets inventory",
    )
    supplier_inventory.add_argument(
        "--max-depth",
        type=_max_depth_argument,
        default=4,
        help="Maximum Drive folder depth (1-6; default: 4)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    logger = _configure_logging()
    if arguments.command == "doctor":
        return _run_doctor(logger)
    if arguments.command == "inspect-product":
        return _run_inspect_product(logger, arguments.sku)
    if arguments.command == "google-doctor":
        return _run_google_doctor(logger)
    if arguments.command == "supplier-inventory":
        return _run_supplier_inventory(logger, arguments.max_depth)
    raise AssertionError("Unhandled command")
