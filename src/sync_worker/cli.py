"""Command-line entry points for sync_worker."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from .config import load_config
from .doctor import DoctorRunner, redactor_for_settings
from .http_client import ReadOnlyHttpClient
from .report import DoctorReportWriter
from .sanitization import Redactor


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configure_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return logging.getLogger("sync_worker.doctor")


def _log_failure(logger: logging.Logger, error: BaseException) -> None:
    summary = Redactor().exception(error)
    logger.error(
        json.dumps(
            {"event": "doctor_aborted", "error": summary},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _run_doctor(logger: logging.Logger) -> int:
    try:
        settings = load_config()
    except Exception as error:
        _log_failure(logger, error)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sync_worker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "doctor", help="Run read-only staging connection diagnostics"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    logger = _configure_logging()
    if arguments.command == "doctor":
        return _run_doctor(logger)
    raise AssertionError("Unhandled command")
