"""Command-line entry points for sync_worker."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from .additional_option_dry_run import run_additional_option_dry_run
from .category_mapping_dry_run import run_category_mapping_dry_run
from .config import (
    load_config,
    load_google_config,
    load_google_drive_metadata_config,
    load_google_sheets_readonly_config,
)
from .clm_price_dry_run import run_clm_parser_dry_run
from .doctor import DoctorRunner
from .folder_role_dry_run import run_folder_role_dry_run
from .http_client import ReadOnlyHttpClient
from .google_api import OfficialGoogleClientFactory, google_redactor_for_settings
from .google_doctor import GoogleDoctorRunner
from .google_drive_folder_manifest_dry_run import (
    run_drive_folder_manifest_dry_run,
)
from .google_drive_nested_folder_manifest_dry_run import (
    run_nested_drive_folder_manifest_dry_run,
)
from .google_drive_depth2_folder_manifest_dry_run import (
    run_depth2_drive_folder_manifest_dry_run,
)
from .inspect_product import (
    ReferenceProductInspector,
    reference_product_report_filename,
)
from .image_mapping_dry_run import run_image_mapping_dry_run
from .image_asset_type_dry_run import run_image_asset_type_dry_run
from .webp_output_policy_dry_run import (
    WebPOutputPolicyDryRunInputError,
    run_webp_output_policy_dry_run,
)
from .unified_image_eligibility_dry_run import (
    UnifiedImageEligibilityDryRunInputError,
    run_unified_image_eligibility_dry_run,
)
from .image_quality_dry_run import (
    ImageQualityDryRunInputError,
    run_image_quality_dry_run,
)
from .image_selection_dry_run import (
    ImageSelectionDryRunInputError,
    run_image_selection_dry_run,
)
from .selected_media_baseline_snapshot import (
    SelectedMediaBaselineSnapshotError,
    run_selected_media_baseline_snapshot,
)
from .selected_media_handle_preparation import (
    SelectedMediaHandlePreparationError,
    run_selected_media_handle_preparation,
)
from .secure_media_download_canary import (
    SecureMediaDownloadCanaryError,
    run_secure_media_download_canary,
)
from .verified_webp_conversion_canary import (
    VerifiedWebPConversionCanaryError,
    run_verified_webp_conversion_canary,
)
from .secure_media_download_execution import (
    SecureMediaDownloadExecutionError,
    run_secure_media_download_execution,
)
from .media_source_discovery_dry_run import (
    run_media_source_discovery_dry_run,
)
from .option_pricing_dry_run import (
    OptionPricingDryRunInputError,
    parse_rmb_to_usd_rate,
    run_option_pricing_dry_run,
)
from .option_mapping_registry import REGISTRY_VERSION
from .product_size_enrichment_dry_run import (
    run_product_size_enrichment_dry_run,
)
from .product_option_linking_dry_run import (
    run_product_option_linking_dry_run,
)
from .product_option_pricing_dry_run import (
    run_product_option_pricing_dry_run,
)
from .product_option_presentation_dry_run import (
    run_product_option_presentation_dry_run,
)
from .woocommerce_payload_dry_run import run_woocommerce_payload_dry_run
from .woo_category_binding import STAGING_BINDING_PROFILE_VERSION
from .woocommerce_category_discovery import (
    load_woo_category_credentials,
    normalize_woo_base_url,
    redactor_for_woo_category_credentials,
    run_woo_category_discovery,
)
from .report import (
    DoctorReportWriter,
    ReferenceProductReportWriter,
    SafeJsonReportWriter,
)
from .sanitization import Redactor
from .security import redactor_for_settings
from .size_list_dry_run import run_size_list_dry_run
from .sku_dry_run import run_sku_dry_run
from .sheet_layout import (
    SheetLayoutInspector,
    parse_a1_range,
    safe_sheet_report_filename,
    validate_sheet_title,
)
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


def _sheet_title_argument(value: str) -> str:
    try:
        return validate_sheet_title(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _selection_position_argument(value: str) -> int:
    try:
        position = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("position_must_be_nonnegative_integer") from None
    if position < 0:
        raise argparse.ArgumentTypeError("position_must_be_nonnegative_integer")
    return position


def _sheet_range_argument(value: str) -> str:
    try:
        return parse_a1_range(value).a1
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _rmb_to_usd_argument(value: str) -> Decimal:
    try:
        return parse_rmb_to_usd_rate(value)
    except OptionPricingDryRunInputError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _woo_base_url_argument(value: str) -> str:
    try:
        return normalize_woo_base_url(value)
    except Exception as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _category_binding_profile_argument(value: str) -> str:
    if value != STAGING_BINDING_PROFILE_VERSION:
        raise argparse.ArgumentTypeError("unknown_category_binding_profile")
    return value


def _run_inspect_sheet_layout(
    logger: logging.Logger,
    sheet_title: str,
    a1_range: str,
) -> int:
    try:
        validated_sheet = validate_sheet_title(sheet_title)
        validated_range = parse_a1_range(a1_range).a1
        settings = load_google_sheets_readonly_config()
    except Exception as error:
        _log_failure(logger, error, event="inspect_sheet_layout_aborted")
        return 2

    redactor = google_redactor_for_settings(settings)
    try:
        report = SheetLayoutInspector(
            settings,
            OfficialGoogleClientFactory(),
            sheet_title=validated_sheet,
            a1_range=validated_range,
            redactor=redactor,
            logger=logger,
        ).run()
        report_name = safe_sheet_report_filename(validated_sheet)
        report_path = PROJECT_ROOT / "reports" / report_name
        SafeJsonReportWriter(report_path, redactor).write(report)
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "inspect_sheet_layout_aborted",
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
                "event": "sheet_layout_report_written",
                "path": f"reports/{report_name}",
                "status": report.get("status"),
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_parse_clm_price_list(logger: logging.Logger, input_path: Path) -> int:
    try:
        report, _ = run_clm_parser_dry_run(
            input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="parse_clm_price_list_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "clm_parser_dry_run_report_written",
                "path": "reports/clm-parser-dry-run.json",
                "status": report.get("status"),
                "detected_product_count": report.get(
                    "detected_product_count", 0
                ),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_parse_additional_option(
    logger: logging.Logger, input_path: Path
) -> int:
    try:
        report, _ = run_additional_option_dry_run(
            input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="parse_additional_option_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "additional_option_dry_run_report_written",
                "path": "reports/additional-option-dry-run.json",
                "status": report.get("status"),
                "detected_option_count": report.get(
                    "detected_option_count", 0
                ),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_price_additional_options(
    logger: logging.Logger,
    input_path: Path,
    rmb_to_usd_rate: Decimal,
) -> int:
    try:
        report, _ = run_option_pricing_dry_run(
            input_path,
            rmb_to_usd_rate=rmb_to_usd_rate,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="price_additional_options_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "option_pricing_dry_run_report_written",
                "path": "reports/option-pricing-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_parse_size_list(logger: logging.Logger, input_path: Path) -> int:
    try:
        report, _ = run_size_list_dry_run(
            input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="parse_size_list_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "size_list_dry_run_report_written",
                "path": "reports/size-list-dry-run.json",
                "status": report.get("status"),
                "detected_record_count": report.get(
                    "detected_record_count", 0
                ),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_enrich_product_size(
    logger: logging.Logger,
    product_input_path: Path,
    size_input_path: Path,
) -> int:
    try:
        report, _ = run_product_size_enrichment_dry_run(
            product_input_path,
            size_input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="enrich_product_size_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "product_size_enrichment_dry_run_report_written",
                "path": "reports/product-size-enrichment-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_map_product_images(
    logger: logging.Logger,
    product_input_path: Path,
    layout_input_path: Path,
) -> int:
    try:
        report, _ = run_image_mapping_dry_run(
            product_input_path,
            layout_input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="map_product_images_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "image_mapping_dry_run_report_written",
                "path": "reports/image-mapping-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_discover_mapped_media_sources(
    logger: logging.Logger,
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path | None,
) -> int:
    try:
        settings = load_google_sheets_readonly_config()
    except Exception as error:
        _log_failure(
            logger,
            error,
            event="discover_mapped_media_sources_aborted",
        )
        return 2
    redactor = google_redactor_for_settings(settings)
    try:
        report, _ = run_media_source_discovery_dry_run(
            mapping_input_path,
            sheet_title,
            settings,
            OfficialGoogleClientFactory(),
            project_root=PROJECT_ROOT,
            sku_report_input_path=sku_report_input_path,
            redactor=redactor,
        )
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "discover_mapped_media_sources_aborted",
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
                "event": "media_source_discovery_dry_run_report_written",
                "path": "reports/media-source-discovery-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "read_requests_performed": report.get(
                    "read_requests_performed", 0
                ),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_build_drive_folder_manifests(
    logger: logging.Logger,
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path,
) -> int:
    try:
        settings = load_google_drive_metadata_config()
    except Exception as error:
        _log_failure(
            logger, error, event="drive_folder_manifest_dry_run_aborted"
        )
        return 2
    redactor = google_redactor_for_settings(settings)
    try:
        report, _ = run_drive_folder_manifest_dry_run(
            mapping_input_path,
            sheet_title,
            sku_report_input_path,
            settings,
            OfficialGoogleClientFactory(),
            project_root=PROJECT_ROOT,
            redactor=redactor,
        )
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "drive_folder_manifest_dry_run_aborted",
                    "error": redactor.exception(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    summary = report.get("summary", {})
    logger.info(
        json.dumps(
            {
                "event": "drive_folder_manifest_dry_run_report_written",
                "path": "reports/google-drive-folder-manifest-dry-run.json",
                "status": report.get("status"),
                "summary": summary,
                "download_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_build_nested_drive_folder_manifests(
    logger: logging.Logger,
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path,
) -> int:
    try:
        settings = load_google_drive_metadata_config()
    except Exception as error:
        _log_failure(logger, error, event="nested_drive_folder_manifest_dry_run_aborted")
        return 2
    redactor = google_redactor_for_settings(settings)
    try:
        report, _ = run_nested_drive_folder_manifest_dry_run(
            mapping_input_path, sheet_title, sku_report_input_path,
            settings, OfficialGoogleClientFactory(),
            project_root=PROJECT_ROOT, redactor=redactor,
        )
    except Exception as error:
        logger.error(json.dumps({
            "event": "nested_drive_folder_manifest_dry_run_aborted",
            "error": redactor.exception(error),
        }, ensure_ascii=False, sort_keys=True))
        return 2
    logger.info(json.dumps({
        "event": "nested_drive_folder_manifest_dry_run_report_written",
        "path": "reports/google-drive-nested-folder-manifest-dry-run.json",
        "status": report.get("status"),
        "summary": report.get("summary", {}),
        "download_requests_performed": 0,
        "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


def _run_build_depth2_drive_folder_manifests(
    logger: logging.Logger,
    mapping_input_path: Path,
    sheet_title: str,
    sku_report_input_path: Path,
) -> int:
    try:
        settings = load_google_drive_metadata_config()
    except Exception as error:
        _log_failure(logger, error, event="depth2_drive_folder_manifest_dry_run_aborted")
        return 2
    redactor = google_redactor_for_settings(settings)
    try:
        report, _ = run_depth2_drive_folder_manifest_dry_run(
            mapping_input_path, sheet_title, sku_report_input_path,
            settings, OfficialGoogleClientFactory(),
            project_root=PROJECT_ROOT, redactor=redactor,
        )
    except Exception as error:
        logger.error(json.dumps({
            "event": "depth2_drive_folder_manifest_dry_run_aborted",
            "error": redactor.exception(error),
        }, ensure_ascii=False, sort_keys=True))
        return 2
    logger.info(json.dumps({
        "event": "depth2_drive_folder_manifest_dry_run_report_written",
        "path": "reports/google-drive-depth2-folder-manifest-dry-run.json",
        "status": report.get("status"), "summary": report.get("summary", {}),
        "download_requests_performed": 0, "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


def _run_classify_folder_roles(
    logger: logging.Logger,
    nested_manifest_path: Path,
    depth2_manifest_path: Path,
) -> int:
    try:
        report, _ = run_folder_role_dry_run(
            nested_manifest_path, depth2_manifest_path, project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="folder_role_dry_run_aborted")
        return 2
    logger.info(json.dumps({
        "event": "folder_role_dry_run_report_written",
        "path": "reports/folder-role-dry-run.json",
        "status": report.get("status"), "summary": report.get("summary", {}),
        "network_requests_performed": 0,
        "download_requests_performed": 0, "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


def _run_classify_image_asset_types(
    logger: logging.Logger,
    root_manifest_path: Path,
    nested_manifest_path: Path,
    depth2_manifest_path: Path,
) -> int:
    try:
        report, _ = run_image_asset_type_dry_run(
            root_manifest_path, nested_manifest_path, depth2_manifest_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="image_asset_type_dry_run_aborted")
        return 2
    logger.info(json.dumps({
        "event": "image_asset_type_dry_run_report_written",
        "path": "reports/image-asset-type-dry-run.json",
        "status": report.get("status"), "summary": report.get("summary", {}),
        "network_requests_performed": 0,
        "download_requests_performed": 0, "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


def _run_plan_webp_output(logger: logging.Logger, asset_report_path: Path) -> int:
    try:
        report, _ = run_webp_output_policy_dry_run(
            asset_report_path, project_root=PROJECT_ROOT,
        )
    except WebPOutputPolicyDryRunInputError as error:
        _log_failure(logger, error, event="webp_output_policy_dry_run_aborted")
        return 2
    except Exception:
        # Never expose unexpected exceptions, input paths, requests or values.
        _log_failure(
            logger, ValueError("webp_output_policy_dry_run_failed"),
            event="webp_output_policy_dry_run_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "webp_output_policy_dry_run_report_written",
        "status": report["status"], "summary": report["summary"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def _run_evaluate_image_eligibility(
    logger: logging.Logger,
    folder_role_report_path: Path,
    webp_report_path: Path,
) -> int:
    try:
        report, _ = run_unified_image_eligibility_dry_run(
            folder_role_report_path, webp_report_path, project_root=PROJECT_ROOT,
        )
    except UnifiedImageEligibilityDryRunInputError as error:
        _log_failure(logger, error, event="unified_image_eligibility_dry_run_aborted")
        return 2
    except Exception:
        _log_failure(
            logger, ValueError("unified_image_eligibility_dry_run_failed"),
            event="unified_image_eligibility_dry_run_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "unified_image_eligibility_dry_run_report_written",
        "status": report["status"], "summary": report["summary"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def _run_evaluate_image_quality(
    logger: logging.Logger,
    unified_report_path: Path,
    asset_report_path: Path,
) -> int:
    try:
        report, _ = run_image_quality_dry_run(
            unified_report_path, asset_report_path, project_root=PROJECT_ROOT,
        )
    except ImageQualityDryRunInputError as error:
        _log_failure(logger, error, event="image_quality_dry_run_aborted")
        return 2
    except Exception:
        _log_failure(
            logger, ValueError("image_quality_dry_run_failed"),
            event="image_quality_dry_run_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "image_quality_dry_run_report_written",
        "status": report["status"], "summary": report["summary"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def _run_select_product_images(
    logger: logging.Logger,
    quality_report_path: Path,
) -> int:
    try:
        report, _ = run_image_selection_dry_run(
            quality_report_path, project_root=PROJECT_ROOT,
        )
    except ImageSelectionDryRunInputError as error:
        _log_failure(logger, error, event="image_selection_dry_run_aborted")
        return 2
    except Exception:
        _log_failure(
            logger, ValueError("image_selection_dry_run_failed"),
            event="image_selection_dry_run_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "image_selection_dry_run_report_written",
        "status": report["status"], "summary": report["summary"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def _run_freeze_selected_media_baseline(
    logger: logging.Logger,
    selection_report_path: Path,
    nested_baseline_path: Path,
    depth2_baseline_path: Path,
) -> int:
    try:
        report, _ = run_selected_media_baseline_snapshot(
            selection_report_path,
            nested_baseline_path,
            depth2_baseline_path,
            project_root=PROJECT_ROOT,
        )
    except SelectedMediaBaselineSnapshotError as error:
        _log_failure(
            logger, error, event="selected_media_baseline_snapshot_aborted"
        )
        return 2
    except Exception:
        _log_failure(
            logger,
            ValueError("selected_media_baseline_snapshot_failed"),
            event="selected_media_baseline_snapshot_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "selected_media_baseline_snapshot_written",
        "path": "reports/selected-media-baseline-snapshot.json",
        "status": report["status"],
        "summary": report["summary"],
        "network_requests_performed": 0,
        "drive_read_requests_performed": 0,
        "download_requests_performed": 0,
        "media_read_requests_performed": 0,
        "conversion_requests_performed": 0,
        "wordpress_upload_requests_performed": 0,
        "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


def _run_prepare_selected_media_handles(
    logger: logging.Logger,
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
) -> int:
    try:
        settings = load_google_drive_metadata_config()
        result, _ = run_selected_media_handle_preparation(
            selection_report_path, baseline_snapshot_path, mapping_path,
            sheet_title, sku_report_path, settings,
            OfficialGoogleClientFactory(), project_root=PROJECT_ROOT,
        )
    except SelectedMediaHandlePreparationError as error:
        _log_failure(logger, error, event="selected_media_handle_preparation_aborted")
        return 2
    except Exception:
        _log_failure(
            logger, ValueError("selected_media_handle_preparation_failed"),
            event="selected_media_handle_preparation_aborted",
        )
        return 2
    report = result.to_safe_report_dict()
    logger.info(json.dumps({
        "event": "selected_media_handle_preparation_report_written",
        "path": "reports/selected-media-handle-preparation.json",
        "status": result.status, "summary": report["summary"],
        "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "ok" else 1


def _run_secure_media_download_canary(
    logger: logging.Logger,
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    sku: str,
    position: int,
) -> int:
    try:
        settings = load_google_drive_metadata_config()
        report, _ = run_secure_media_download_canary(
            selection_report_path, baseline_snapshot_path, mapping_path,
            sheet_title, sku_report_path, sku, position,
            settings, OfficialGoogleClientFactory(), project_root=PROJECT_ROOT,
        )
    except SecureMediaDownloadCanaryError as error:
        _log_failure(logger, error, event="secure_media_download_canary_aborted")
        return 2
    except Exception:
        _log_failure(
            logger, ValueError("secure_media_download_canary_failed"),
            event="secure_media_download_canary_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "secure_media_download_canary_report_written",
        "path": "reports/secure-media-download-canary.json",
        "status": report["status"],
        "cleanup_completed": report["cleanup_completed"],
        "source_files_remaining": report["source_files_remaining"],
        "download_requests_performed": report["download_requests_performed"],
        "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    if report["status"] == "ok":
        return 0
    return 2 if report["status"] == "failed" else 1


def _run_verified_webp_conversion_canary(
    logger: logging.Logger,
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
    sku: str,
    position: int,
) -> int:
    try:
        settings = load_google_drive_metadata_config()
        report, _ = run_verified_webp_conversion_canary(
            selection_report_path,
            baseline_snapshot_path,
            mapping_path,
            sheet_title,
            sku_report_path,
            sku,
            position,
            settings,
            OfficialGoogleClientFactory(),
            project_root=PROJECT_ROOT,
        )
    except VerifiedWebPConversionCanaryError as error:
        _log_failure(
            logger,
            error,
            event="verified_webp_conversion_canary_aborted",
        )
        return 2
    except Exception:
        _log_failure(
            logger,
            ValueError("verified_webp_conversion_canary_failed"),
            event="verified_webp_conversion_canary_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "verified_webp_conversion_canary_report_written",
        "path": "reports/verified-webp-conversion-canary.json",
        "status": report["status"],
        "source_cleanup_completed": report["source_cleanup_completed"],
        "source_files_remaining": report["source_files_remaining"],
        "webp_cleanup_completed": report["webp_cleanup_completed"],
        "webp_files_remaining": report["webp_files_remaining"],
        "retained_download_artifacts": report["retained_download_artifacts"],
        "retained_webp_artifacts": report["retained_webp_artifacts"],
        "wordpress_upload_requests_performed": 0,
        "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    if report["status"] == "ok":
        return 0
    return 2 if report["status"] == "failed" else 1


def _run_secure_media_download_execution(
    logger: logging.Logger,
    selection_report_path: Path,
    baseline_snapshot_path: Path,
    mapping_path: Path,
    sheet_title: str,
    sku_report_path: Path,
) -> int:
    def safe_progress(event: object) -> None:
        if not isinstance(event, dict):
            raise ValueError("invalid_download_progress_event")
        safe_event = {
            key: event.get(key)
            for key in (
                "current_index", "total_items", "sku",
                "selection_position", "status",
            )
        }
        logger.info(json.dumps({
            "event": "secure_media_download_progress",
            **safe_event,
        }, ensure_ascii=False, sort_keys=True))

    try:
        settings = load_google_drive_metadata_config()
        report, _ = run_secure_media_download_execution(
            selection_report_path, baseline_snapshot_path, mapping_path,
            sheet_title, sku_report_path, settings,
            OfficialGoogleClientFactory(), project_root=PROJECT_ROOT,
            progress_callback=safe_progress,
        )
    except SecureMediaDownloadExecutionError as error:
        _log_failure(logger, error, event="secure_media_download_execution_aborted")
        return 2
    except Exception:
        _log_failure(
            logger, ValueError("secure_media_download_execution_failed"),
            event="secure_media_download_execution_aborted",
        )
        return 2
    logger.info(json.dumps({
        "event": "secure_media_download_execution_report_written",
        "path": "reports/secure-media-download-execution.json",
        "status": report["status"],
        "selected_items": report["selected_items"],
        "downloads_verified": report["downloads_verified"],
        "cleanup_completed": report["cleanup_completed"],
        "source_files_remaining": report["source_files_remaining"],
        "download_requests_performed": report["download_requests_performed"],
        "write_requests_performed": 0,
    }, ensure_ascii=False, sort_keys=True))
    if report["status"] == "ok":
        return 0
    return 2 if report["status"] == "failed" else 1


def _run_link_product_options(
    logger: logging.Logger,
    product_input_path: Path,
    option_input_path: Path,
    mapping_registry_version: str | None,
) -> int:
    try:
        report, _ = run_product_option_linking_dry_run(
            product_input_path,
            option_input_path,
            project_root=PROJECT_ROOT,
            mapping_registry_version=mapping_registry_version,
        )
    except Exception as error:
        _log_failure(logger, error, event="link_product_options_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "product_option_linking_dry_run_report_written",
                "path": "reports/product-option-linking-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_price_linked_product_options(
    logger: logging.Logger,
    input_path: Path,
    rmb_to_usd_rate: Decimal,
) -> int:
    try:
        report, _ = run_product_option_pricing_dry_run(
            input_path,
            rmb_to_usd_rate=rmb_to_usd_rate,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="price_linked_product_options_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "product_option_pricing_dry_run_report_written",
                "path": "reports/product-option-pricing-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_present_product_option_prices(
    logger: logging.Logger,
    input_path: Path,
) -> int:
    try:
        report, _ = run_product_option_presentation_dry_run(
            input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="present_product_option_prices_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "product_option_presentation_dry_run_report_written",
                "path": "reports/product-option-presentation-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_build_woocommerce_payloads(
    logger: logging.Logger,
    product_input_path: Path,
    size_input_path: Path,
    presented_option_input_path: Path,
    category_binding_profile_version: str | None = None,
    woo_category_discovery_path: Path | None = None,
    target_base_url: str | None = None,
) -> int:
    try:
        report, _ = run_woocommerce_payload_dry_run(
            product_input_path,
            size_input_path,
            presented_option_input_path,
            project_root=PROJECT_ROOT,
            category_binding_profile_version=category_binding_profile_version,
            woo_category_discovery_path=woo_category_discovery_path,
            target_base_url=target_base_url,
        )
    except Exception as error:
        _log_failure(logger, error, event="woocommerce_payload_dry_run_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "woocommerce_payload_dry_run_report_written",
                "path": "reports/woocommerce-payload-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_generate_sku_dry_run(
    logger: logging.Logger,
    product_input_path: Path,
) -> int:
    try:
        report, _ = run_sku_dry_run(
            product_input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="sku_dry_run_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "sku_dry_run_report_written",
                "path": "reports/sku-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_category_mapping_dry_run(
    logger: logging.Logger,
    product_input_path: Path,
) -> int:
    try:
        report, _ = run_category_mapping_dry_run(
            product_input_path,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        _log_failure(logger, error, event="category_mapping_dry_run_aborted")
        return 2

    logger.info(
        json.dumps(
            {
                "event": "category_mapping_dry_run_report_written",
                "path": "reports/category-mapping-dry-run.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": 0,
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


def _run_discover_woo_categories(
    logger: logging.Logger,
    base_url: str,
) -> int:
    redactor = Redactor()
    try:
        credentials = load_woo_category_credentials()
        redactor = redactor_for_woo_category_credentials(credentials)
        report, _ = run_woo_category_discovery(
            base_url,
            credentials,
            project_root=PROJECT_ROOT,
        )
    except Exception as error:
        logger.error(
            json.dumps(
                {
                    "event": "woo_category_discovery_aborted",
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
                "event": "woo_category_discovery_report_written",
                "path": "reports/woo-category-discovery.json",
                "status": report.get("status"),
                "summary": report.get("summary", {}),
                "network_requests_performed": report.get(
                    "network_requests_performed", 0
                ),
                "write_requests_performed": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("status") == "ok" else 1


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
    inspect_sheet_layout = subcommands.add_parser(
        "inspect-sheet-layout",
        help="Inspect one bounded Sheet grid region without formulas or writes",
    )
    inspect_sheet_layout.add_argument(
        "--sheet",
        required=True,
        type=_sheet_title_argument,
        help="Exact Sheet title (1-150 characters)",
    )
    inspect_sheet_layout.add_argument(
        "--range",
        required=True,
        dest="a1_range",
        type=_sheet_range_argument,
        help="Single bounded A1 range, up to AZ, 100 rows, and 5200 cells",
    )
    parse_clm_price_list = subcommands.add_parser(
        "parse-clm-price-list",
        help="Parse one local sheet-layout JSON into a sanitized dry-run report",
    )
    parse_clm_price_list.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Local sheet-layout JSON file",
    )
    parse_additional_option = subcommands.add_parser(
        "parse-additional-option",
        help="Parse a local Additional Option layout into a dry-run report",
    )
    parse_additional_option.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Local Additional Option sheet-layout JSON file",
    )
    price_additional_options = subcommands.add_parser(
        "price-additional-options",
        help="Price a local Additional Option dry-run report with injected FX",
    )
    price_additional_options.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Local Additional Option dry-run JSON file",
    )
    price_additional_options.add_argument(
        "--rmb-to-usd",
        required=True,
        type=_rmb_to_usd_argument,
        dest="rmb_to_usd_rate",
        help="Explicit positive RMB-to-USD Decimal rate",
    )
    parse_size_list = subcommands.add_parser(
        "parse-size-list",
        help="Parse a local Size List layout into a sanitized dry-run report",
    )
    parse_size_list.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Local Size List sheet-layout JSON file",
    )
    enrich_product_size = subcommands.add_parser(
        "enrich-product-size",
        help="Join local CLM and Size dry-run reports without external access",
    )
    enrich_product_size.add_argument(
        "--products",
        required=True,
        type=Path,
        dest="product_input_path",
        help="Local CLM parser dry-run JSON file",
    )
    enrich_product_size.add_argument(
        "--sizes",
        required=True,
        type=Path,
        dest="size_input_path",
        help="Local Size List dry-run JSON file",
    )
    map_product_images = subcommands.add_parser(
        "map-product-images",
        help="Map local Product records to local Sheet media provenance",
    )
    map_product_images.add_argument(
        "--products",
        required=True,
        type=Path,
        dest="product_input_path",
        help="Local CLM parser dry-run JSON file",
    )
    map_product_images.add_argument(
        "--layout",
        required=True,
        type=Path,
        dest="layout_input_path",
        help="Corresponding local Sheet layout JSON file",
    )
    discover_mapped_media_sources = subcommands.add_parser(
        "discover-mapped-media-sources",
        help="Securely classify exact mapped media reference cells",
    )
    discover_mapped_media_sources.add_argument(
        "--mapping",
        required=True,
        type=Path,
        dest="mapping_input_path",
        help="Local Image Mapping dry-run JSON file",
    )
    discover_mapped_media_sources.add_argument(
        "--sheet",
        required=True,
        type=_sheet_title_argument,
        dest="sheet_title",
        help="Exact Google Sheet title containing the mapped cells",
    )
    discover_mapped_media_sources.add_argument(
        "--sku-report",
        type=Path,
        dest="sku_report_input_path",
        help="Optional regenerated SKU dry-run JSON file",
    )
    build_drive_folder_manifests = subcommands.add_parser(
        "build-drive-folder-manifests",
        help="Build metadata-only manifests for exact mapped Drive folders",
    )
    build_drive_folder_manifests.add_argument(
        "--mapping",
        required=True,
        type=Path,
        dest="mapping_input_path",
        help="Local Image Mapping dry-run JSON file",
    )
    build_drive_folder_manifests.add_argument(
        "--sheet",
        required=True,
        type=_sheet_title_argument,
        dest="sheet_title",
        help="Exact Google Sheet title containing mapped reference cells",
    )
    build_drive_folder_manifests.add_argument(
        "--sku-report",
        required=True,
        type=Path,
        dest="sku_report_input_path",
        help="Local verified SKU dry-run JSON file",
    )
    build_nested_drive_folder_manifests = subcommands.add_parser(
        "build-nested-drive-folder-manifests",
        help="Read fresh Root and depth-one Nested Drive metadata manifests",
    )
    build_nested_drive_folder_manifests.add_argument(
        "--mapping", required=True, type=Path, dest="mapping_input_path",
        help="Local Image Mapping dry-run JSON file (not a Root manifest report)",
    )
    build_nested_drive_folder_manifests.add_argument(
        "--sheet", required=True, type=_sheet_title_argument, dest="sheet_title",
        help="Exact Google Sheet title containing mapped reference cells",
    )
    build_nested_drive_folder_manifests.add_argument(
        "--sku-report", required=True, type=Path, dest="sku_report_input_path",
        help="Local verified SKU dry-run JSON file from the same product snapshot",
    )
    build_depth2_drive_folder_manifests = subcommands.add_parser(
        "build-depth2-drive-folder-manifests",
        help="Read fresh Root, depth-one and depth-two Drive metadata manifests",
    )
    build_depth2_drive_folder_manifests.add_argument(
        "--mapping", required=True, type=Path, dest="mapping_input_path",
        help="Local Image Mapping dry-run JSON file (not a Drive manifest report)",
    )
    build_depth2_drive_folder_manifests.add_argument(
        "--sheet", required=True, type=_sheet_title_argument, dest="sheet_title",
        help="Exact Google Sheet title containing mapped reference cells",
    )
    build_depth2_drive_folder_manifests.add_argument(
        "--sku-report", required=True, type=Path, dest="sku_report_input_path",
        help="Local verified SKU dry-run JSON file from the same product snapshot",
    )
    classify_folder_roles = subcommands.add_parser(
        "classify-folder-roles",
        help="Classify safe local depth-one/two folder manifests without network access",
    )
    classify_folder_roles.add_argument(
        "--nested-manifest", required=True, type=Path, dest="nested_manifest_path",
        help="Local safe Nested Folder Manifest dry-run JSON with status=ok",
    )
    classify_folder_roles.add_argument(
        "--depth2-manifest", required=True, type=Path, dest="depth2_manifest_path",
        help="Local safe Depth-2 Folder Manifest dry-run JSON with status=ok",
    )
    classify_image_asset_types = subcommands.add_parser(
        "classify-image-asset-types",
        help="Classify file metadata from three safe local manifests without network access",
    )
    for flag, dest in (
        ("--root-manifest", "root_manifest_path"),
        ("--nested-manifest", "nested_manifest_path"),
        ("--depth2-manifest", "depth2_manifest_path"),
    ):
        classify_image_asset_types.add_argument(
            flag, required=True, type=Path, dest=dest,
            help="Local safe manifest dry-run JSON with status=ok",
        )
    plan_webp_output = subcommands.add_parser(
        "plan-webp-output",
        help="Plan WebP processing from a safe local asset-type report; no media I/O",
    )
    plan_webp_output.add_argument(
        "--asset-report", required=True, type=Path, dest="asset_report_path",
        help="Local Image Asset Type dry-run JSON with status=ok",
    )
    evaluate_image_eligibility = subcommands.add_parser(
        "evaluate-image-eligibility",
        help="Exact-join local Folder Role and WebP reports through Unified Core",
    )
    evaluate_image_eligibility.add_argument(
        "--folder-role-report", required=True, type=Path,
        dest="folder_role_report_path",
        help="Local Folder Role dry-run JSON with status=ok",
    )
    evaluate_image_eligibility.add_argument(
        "--webp-report", required=True, type=Path, dest="webp_report_path",
        help="Local WebP Output Policy dry-run JSON with status=ok",
    )
    evaluate_image_quality = subcommands.add_parser(
        "evaluate-image-quality",
        help="Exact-join local Unified and Asset Type reports through Quality Core",
    )
    evaluate_image_quality.add_argument(
        "--unified-report", required=True, type=Path, dest="unified_report_path",
        help="Local Unified Image Eligibility dry-run JSON with status=ok",
    )
    evaluate_image_quality.add_argument(
        "--asset-report", required=True, type=Path, dest="asset_report_path",
        help="Local Image Asset Type dry-run JSON with status=ok",
    )
    select_product_images = subcommands.add_parser(
        "select-product-images",
        help="Build a deterministic image plan from a local Quality report",
    )
    select_product_images.add_argument(
        "--quality-report", required=True, type=Path, dest="quality_report_path",
        help="Local Image Quality dry-run JSON with status=ok",
    )
    freeze_selected_media_baseline = subcommands.add_parser(
        "freeze-selected-media-baseline",
        help="Freeze selected images against historical safe manifest snapshots",
    )
    freeze_selected_media_baseline.add_argument(
        "--selection-report", required=True, type=Path,
        dest="selection_report_path",
        help="Local image-selection-dry-run.json report with status=ok",
    )
    freeze_selected_media_baseline.add_argument(
        "--nested-baseline", required=True, type=Path,
        dest="nested_baseline_path",
        help="Historical nested Drive manifest dry-run JSON report",
    )
    freeze_selected_media_baseline.add_argument(
        "--depth2-baseline", required=True, type=Path,
        dest="depth2_baseline_path",
        help="Historical depth-two Drive manifest dry-run JSON report",
    )
    prepare_selected_media_handles = subcommands.add_parser(
        "prepare-selected-media-handles",
        help="Prepare all selected media handles from frozen identity and fresh metadata",
    )
    for flag, dest, help_text in (
        ("--selection-report", "selection_report_path", "Current Image Selection report"),
        ("--baseline-snapshot", "baseline_snapshot_path", "Frozen selected media baseline snapshot"),
        ("--mapping", "mapping_path", "Current Image Mapping report"),
        ("--sku-report", "sku_report_path", "Current verified SKU report"),
    ):
        prepare_selected_media_handles.add_argument(
            flag, required=True, type=Path, dest=dest, help=help_text,
        )
    prepare_selected_media_handles.add_argument(
        "--sheet", required=True, type=_sheet_title_argument,
        dest="sheet_title", help="Exact Google Sheet title",
    )
    download_selected_media_canary = subcommands.add_parser(
        "download-selected-media-canary",
        help="Prepare all handles and verify one exact Drive media download",
    )
    for flag, dest, help_text in (
        ("--selection-report", "selection_report_path", "Current Image Selection report"),
        ("--baseline-snapshot", "baseline_snapshot_path", "Frozen selected media baseline snapshot"),
        ("--mapping", "mapping_path", "Current Image Mapping report"),
        ("--sku-report", "sku_report_path", "Current verified SKU report"),
    ):
        download_selected_media_canary.add_argument(
            flag, required=True, type=Path, dest=dest, help=help_text,
        )
    download_selected_media_canary.add_argument(
        "--sheet", required=True, type=_sheet_title_argument,
        dest="sheet_title", help="Exact Google Sheet title",
    )
    download_selected_media_canary.add_argument(
        "--sku", required=True, help="Exact selected product SKU",
    )
    download_selected_media_canary.add_argument(
        "--position", required=True, type=_selection_position_argument,
        help="Exact zero-based selected image position",
    )
    convert_selected_media_canary = subcommands.add_parser(
        "convert-selected-media-canary",
        help="Fresh-prepare all handles, download one exact source, and verify WebP",
    )
    for flag, dest, help_text in (
        ("--selection-report", "selection_report_path", "Current Image Selection report"),
        ("--baseline-snapshot", "baseline_snapshot_path", "Frozen selected media baseline snapshot"),
        ("--mapping", "mapping_path", "Current Image Mapping report"),
        ("--sku-report", "sku_report_path", "Current verified SKU report"),
    ):
        convert_selected_media_canary.add_argument(
            flag, required=True, type=Path, dest=dest, help=help_text,
        )
    convert_selected_media_canary.add_argument(
        "--sheet", required=True, type=_sheet_title_argument,
        dest="sheet_title", help="Exact Google Sheet title",
    )
    convert_selected_media_canary.add_argument(
        "--sku", required=True, help="Exact selected product SKU",
    )
    convert_selected_media_canary.add_argument(
        "--position", required=True, type=_selection_position_argument,
        help="Exact zero-based selected image position",
    )
    download_selected_media_batch = subcommands.add_parser(
        "download-selected-media-batch",
        help="Prepare and verify the full approved Drive media batch",
    )
    for flag, dest, help_text in (
        ("--selection-report", "selection_report_path", "Current Image Selection report"),
        ("--baseline-snapshot", "baseline_snapshot_path", "Frozen selected media baseline snapshot"),
        ("--mapping", "mapping_path", "Current Image Mapping report"),
        ("--sku-report", "sku_report_path", "Current verified SKU report"),
    ):
        download_selected_media_batch.add_argument(
            flag, required=True, type=Path, dest=dest, help=help_text,
        )
    download_selected_media_batch.add_argument(
        "--sheet", required=True, type=_sheet_title_argument,
        dest="sheet_title", help="Exact Google Sheet title",
    )
    link_product_options = subcommands.add_parser(
        "link-product-options",
        help="Link local Product and Additional Option dry-run reports",
    )
    link_product_options.add_argument(
        "--products",
        required=True,
        type=Path,
        dest="product_input_path",
        help="Local CLM parser dry-run JSON file",
    )
    link_product_options.add_argument(
        "--options",
        required=True,
        type=Path,
        dest="option_input_path",
        help="Local Additional Option dry-run JSON file",
    )
    link_product_options.add_argument(
        "--mapping-registry",
        choices=(REGISTRY_VERSION,),
        default=None,
        dest="mapping_registry_version",
        help="Explicit approved option mapping registry version",
    )
    price_linked_options = subcommands.add_parser(
        "price-linked-product-options",
        help="Price a local Product Option Linking report with injected FX",
    )
    price_linked_options.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Local Product Option Linking dry-run JSON file",
    )
    price_linked_options.add_argument(
        "--rmb-to-usd",
        required=True,
        type=_rmb_to_usd_argument,
        dest="rmb_to_usd_rate",
        help="Explicit positive RMB-to-USD Decimal rate",
    )
    present_product_option_prices = subcommands.add_parser(
        "present-product-option-prices",
        help="Present prices from a local Product Option Pricing report",
    )
    present_product_option_prices.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Local Product Option Pricing dry-run JSON file",
    )
    build_woocommerce_payloads = subcommands.add_parser(
        "build-woocommerce-payloads",
        help="Build write-disabled WooCommerce payloads from local reports",
    )
    build_woocommerce_payloads.add_argument(
        "--products",
        required=True,
        type=Path,
        dest="product_input_path",
        help="Local CLM parser dry-run JSON file",
    )
    build_woocommerce_payloads.add_argument(
        "--sizes",
        required=True,
        type=Path,
        dest="size_input_path",
        help="Local Size List dry-run JSON file",
    )
    build_woocommerce_payloads.add_argument(
        "--presented-options",
        required=True,
        type=Path,
        dest="presented_option_input_path",
        help="Local Product Option Presentation dry-run JSON file",
    )
    build_woocommerce_payloads.add_argument(
        "--category-binding-profile",
        type=_category_binding_profile_argument,
        dest="category_binding_profile_version",
        help="Explicit approved Woo category binding profile version",
    )
    build_woocommerce_payloads.add_argument(
        "--woo-category-discovery",
        type=Path,
        dest="woo_category_discovery_path",
        help="Local Woo Category Discovery JSON report",
    )
    build_woocommerce_payloads.add_argument(
        "--target-base-url",
        type=_woo_base_url_argument,
        dest="target_base_url",
        help="Explicit target WooCommerce base URL used for host validation",
    )
    generate_sku_dry_run = subcommands.add_parser(
        "generate-sku-dry-run",
        help="Generate stable SKU candidates from a local CLM parser report",
    )
    generate_sku_dry_run.add_argument(
        "--products",
        required=True,
        type=Path,
        dest="product_input_path",
        help="Local CLM parser dry-run JSON file",
    )
    map_categories_dry_run = subcommands.add_parser(
        "map-categories-dry-run",
        help="Map categories from a local CLM parser report",
    )
    map_categories_dry_run.add_argument(
        "--products",
        required=True,
        type=Path,
        dest="product_input_path",
        help="Local CLM parser dry-run JSON file",
    )
    discover_woo_categories = subcommands.add_parser(
        "discover-woo-categories",
        help="Discover WooCommerce product categories using read-only GETs",
    )
    discover_woo_categories.add_argument(
        "--base-url",
        required=True,
        type=_woo_base_url_argument,
        dest="base_url",
        help="HTTPS WooCommerce site base URL",
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
    if arguments.command == "inspect-sheet-layout":
        return _run_inspect_sheet_layout(
            logger, arguments.sheet, arguments.a1_range
        )
    if arguments.command == "parse-clm-price-list":
        return _run_parse_clm_price_list(logger, arguments.input_path)
    if arguments.command == "parse-additional-option":
        return _run_parse_additional_option(logger, arguments.input_path)
    if arguments.command == "price-additional-options":
        return _run_price_additional_options(
            logger,
            arguments.input_path,
            arguments.rmb_to_usd_rate,
        )
    if arguments.command == "parse-size-list":
        return _run_parse_size_list(logger, arguments.input_path)
    if arguments.command == "enrich-product-size":
        return _run_enrich_product_size(
            logger,
            arguments.product_input_path,
            arguments.size_input_path,
        )
    if arguments.command == "map-product-images":
        return _run_map_product_images(
            logger,
            arguments.product_input_path,
            arguments.layout_input_path,
        )
    if arguments.command == "discover-mapped-media-sources":
        return _run_discover_mapped_media_sources(
            logger,
            arguments.mapping_input_path,
            arguments.sheet_title,
            arguments.sku_report_input_path,
        )
    if arguments.command == "build-drive-folder-manifests":
        return _run_build_drive_folder_manifests(
            logger,
            arguments.mapping_input_path,
            arguments.sheet_title,
            arguments.sku_report_input_path,
        )
    if arguments.command == "build-nested-drive-folder-manifests":
        return _run_build_nested_drive_folder_manifests(
            logger,
            arguments.mapping_input_path,
            arguments.sheet_title,
            arguments.sku_report_input_path,
        )
    if arguments.command == "build-depth2-drive-folder-manifests":
        return _run_build_depth2_drive_folder_manifests(
            logger,
            arguments.mapping_input_path,
            arguments.sheet_title,
            arguments.sku_report_input_path,
        )
    if arguments.command == "classify-folder-roles":
        return _run_classify_folder_roles(
            logger, arguments.nested_manifest_path, arguments.depth2_manifest_path,
        )
    if arguments.command == "classify-image-asset-types":
        return _run_classify_image_asset_types(
            logger, arguments.root_manifest_path,
            arguments.nested_manifest_path, arguments.depth2_manifest_path,
        )
    if arguments.command == "plan-webp-output":
        return _run_plan_webp_output(logger, arguments.asset_report_path)
    if arguments.command == "evaluate-image-eligibility":
        return _run_evaluate_image_eligibility(
            logger, arguments.folder_role_report_path, arguments.webp_report_path,
        )
    if arguments.command == "evaluate-image-quality":
        return _run_evaluate_image_quality(
            logger, arguments.unified_report_path, arguments.asset_report_path,
        )
    if arguments.command == "select-product-images":
        return _run_select_product_images(logger, arguments.quality_report_path)
    if arguments.command == "freeze-selected-media-baseline":
        return _run_freeze_selected_media_baseline(
            logger,
            arguments.selection_report_path,
            arguments.nested_baseline_path,
            arguments.depth2_baseline_path,
        )
    if arguments.command == "prepare-selected-media-handles":
        return _run_prepare_selected_media_handles(
            logger, arguments.selection_report_path,
            arguments.baseline_snapshot_path, arguments.mapping_path,
            arguments.sheet_title, arguments.sku_report_path,
        )
    if arguments.command == "download-selected-media-canary":
        return _run_secure_media_download_canary(
            logger, arguments.selection_report_path,
            arguments.baseline_snapshot_path, arguments.mapping_path,
            arguments.sheet_title, arguments.sku_report_path,
            arguments.sku, arguments.position,
        )
    if arguments.command == "convert-selected-media-canary":
        return _run_verified_webp_conversion_canary(
            logger,
            arguments.selection_report_path,
            arguments.baseline_snapshot_path,
            arguments.mapping_path,
            arguments.sheet_title,
            arguments.sku_report_path,
            arguments.sku,
            arguments.position,
        )
    if arguments.command == "download-selected-media-batch":
        return _run_secure_media_download_execution(
            logger, arguments.selection_report_path,
            arguments.baseline_snapshot_path, arguments.mapping_path,
            arguments.sheet_title, arguments.sku_report_path,
        )
    if arguments.command == "link-product-options":
        return _run_link_product_options(
            logger,
            arguments.product_input_path,
            arguments.option_input_path,
            arguments.mapping_registry_version,
        )
    if arguments.command == "price-linked-product-options":
        return _run_price_linked_product_options(
            logger,
            arguments.input_path,
            arguments.rmb_to_usd_rate,
        )
    if arguments.command == "present-product-option-prices":
        return _run_present_product_option_prices(
            logger,
            arguments.input_path,
        )
    if arguments.command == "build-woocommerce-payloads":
        return _run_build_woocommerce_payloads(
            logger,
            arguments.product_input_path,
            arguments.size_input_path,
            arguments.presented_option_input_path,
            arguments.category_binding_profile_version,
            arguments.woo_category_discovery_path,
            arguments.target_base_url,
        )
    if arguments.command == "generate-sku-dry-run":
        return _run_generate_sku_dry_run(
            logger,
            arguments.product_input_path,
        )
    if arguments.command == "map-categories-dry-run":
        return _run_category_mapping_dry_run(
            logger,
            arguments.product_input_path,
        )
    if arguments.command == "discover-woo-categories":
        return _run_discover_woo_categories(logger, arguments.base_url)
    raise AssertionError("Unhandled command")
