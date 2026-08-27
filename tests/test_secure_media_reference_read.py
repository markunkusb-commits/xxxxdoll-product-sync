from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.google_api import (  # noqa: E402
    GoogleOperationBlocked,
    ReadOnlySheetsGateway,
    SHEETS_LINK_CELL_FIELDS,
    ensure_google_operation_allowed,
)
from sync_worker.config import (  # noqa: E402
    GOOGLE_DRIVE_METADATA_READONLY_SCOPE,
    GOOGLE_SHEETS_READONLY_SCOPE,
)
from sync_worker.image_mapping import (  # noqa: E402
    REDACTED_REFERENCE,
    create_supplier_media_source_reference,
)
from sync_worker.media_source_discovery import discover_media_source  # noqa: E402
from sync_worker.secure_media_reference_read import (  # noqa: E402
    SecureMediaReferenceInputError,
    SecureMediaReferenceResponseError,
    SecureMediaReferenceReader,
    validate_mapping_report,
    validate_single_cell_coordinate,
)


SHEET = "RMB Price List"
RAW_URL = "https://drive.google.com/drive/folders/FOLDER_ID_PRIVATE"


def fingerprint(raw: str, coordinate: str = "I16") -> str:
    return create_supplier_media_source_reference(
        source_coordinate=coordinate,
        marker_coordinate="B15",
        marker_text="Photo download link",
        raw_reference=raw,
    ).reference_fingerprint


def media_item(
    *,
    coordinate: str = "I16",
    marker: str = "B15",
    raw: str = RAW_URL,
    status: str = "available",
    match_status: str = "exact_source_range_match",
    match_method: str = "source_range",
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    reference_fingerprint = (
        fingerprint(REDACTED_REFERENCE, coordinate)
        if status == "redacted"
        else fingerprint(raw, coordinate)
    )
    return {
        "match_status": match_status,
        "match_method": match_method,
        "ambiguous": match_status == "ambiguous_media_source",
        "marker_coordinate": marker,
        "reference_coordinate": coordinate,
        "reference_status": status,
        "reference_fingerprint": reference_fingerprint,
        "warnings": list(warnings or []),
        "blocking_issues": list(blockers or []),
        "download_ready": False,
    }


def product_result(
    *sources: dict[str, object],
    start_row: int = 10,
    end_row: int = 20,
    model: str = "VICA",
    series: str = "ultra",
    blockers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": "mapped",
        "product_identity": {
            "series": series,
            "model": model,
            "raw_model": model,
            "raw_series_title": f"CLM {series.title()}",
        },
        "series": series,
        "product_source": {"start_row": start_row, "end_row": end_row},
        "media_sources": list(sources),
        "warnings": [],
        "blocking_issues": list(blockers or []),
    }


def mapping_report(
    *products: dict[str, object], status: str = "ok"
) -> dict[str, object]:
    return {"status": status, "results": list(products)}


def _coordinate_indices(coordinate: str) -> tuple[int, int]:
    letters = "".join(character for character in coordinate if character.isalpha())
    row = int(coordinate[len(letters) :]) - 1
    column = 0
    for character in letters:
        column = column * 26 + ord(character) - ord("A") + 1
    return row, column - 1


def value_range(
    coordinate: str,
    value: object = RAW_URL,
    *,
    hyperlink: object = None,
    rich_links: list[object] | None = None,
    chip_runs: list[object] | None = None,
    cell_level_link: object = None,
    formula: object = None,
) -> dict[str, object]:
    row, column = _coordinate_indices(coordinate)
    cell: dict[str, object] = {"formattedValue": value}
    if hyperlink is not None:
        cell["hyperlink"] = hyperlink
    if rich_links is not None:
        cell["textFormatRuns"] = [
            {"format": {"link": {"uri": uri}}} for uri in rich_links
        ]
    if chip_runs is not None:
        cell["chipRuns"] = chip_runs
    if cell_level_link is not None:
        cell["userEnteredFormat"] = {
            "textFormat": {"link": {"uri": cell_level_link}}
        }
    if formula is not None:
        cell["userEnteredValue"] = {"formulaValue": formula}
    return {
        "startRow": row,
        "startColumn": column,
        "rowData": [{"values": [cell]}],
    }


def metadata_response(*grids: dict[str, object]) -> dict[str, object]:
    return {"sheets": [{"data": list(grids)}]}


class FakeRequest:
    def __init__(self, response: object) -> None:
        self.response = response

    def execute(self) -> object:
        return self.response


class FakeValues:
    def __init__(self, response: object) -> None:
        self.response = response
        self.batch_get_calls: list[dict[str, object]] = []
        self.write_calls = 0

    def batchGet(self, **kwargs: object) -> FakeRequest:
        raise AssertionError("values.batchGet must not read hyperlink cells")

    def update(self, **kwargs: object) -> None:
        self.write_calls += 1
        raise AssertionError("Sheets write forbidden")


class FakeSpreadsheets:
    def __init__(self, values: FakeValues) -> None:
        self._values = values

    def values(self) -> FakeValues:
        return self._values

    def get(self, **kwargs: object) -> FakeRequest:
        self._values.batch_get_calls.append(dict(kwargs))
        return FakeRequest(self._values.response)


class FakeSheets:
    def __init__(self, values: FakeValues) -> None:
        self._spreadsheets = FakeSpreadsheets(values)

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


class FakeSettings:
    clm_spreadsheet_id = "mock-spreadsheet-id"
    clm_drive_folder_id = ""
    md_drive_folder_id = ""
    drive_scope = GOOGLE_DRIVE_METADATA_READONLY_SCOPE
    sheets_scope = GOOGLE_SHEETS_READONLY_SCOPE

    def __init__(self) -> None:
        self.validate_calls = 0

    def validate_sheets_readonly(self) -> None:
        self.validate_calls += 1


class FakeFactory:
    def __init__(self, response: object) -> None:
        self.values = FakeValues(response)
        self.calls = 0
        self.full_create_calls = 0
        self.drive_client_created = False

    def create_sheets_readonly(self, settings: object) -> object:
        self.calls += 1
        return FakeSheets(self.values)

    def create(self, settings: object) -> object:
        self.full_create_calls += 1
        raise AssertionError("full Google client factory must not be used")


def run_reader(
    report: dict[str, object] | None = None,
    response: object | None = None,
):
    active_report = report or mapping_report(product_result(media_item()))
    active_response = response or metadata_response(value_range("I16"))
    settings = FakeSettings()
    factory = FakeFactory(active_response)
    batch = SecureMediaReferenceReader(settings, factory).run(
        active_report, sheet_title=SHEET
    )
    return batch, settings, factory


class SecureMediaReferenceReadTests(unittest.TestCase):
    def test_01_mapping_status_must_be_ok(self) -> None:
        settings = FakeSettings()
        factory = FakeFactory(metadata_response())
        with self.assertRaisesRegex(
            SecureMediaReferenceInputError, "mapping_report_not_ok"
        ):
            SecureMediaReferenceReader(settings, factory).run(
                mapping_report(status="error"), sheet_title=SHEET
            )
        self.assertEqual(factory.calls, 0)

    def test_02_only_exact_source_range_match_is_selected(self) -> None:
        report = mapping_report(
            product_result(
                media_item(),
                media_item(
                    coordinate="I17", match_status="unmatched_media_source"
                ),
            )
        )
        self.assertEqual(len(validate_mapping_report(report)), 1)

    def test_03_unmatched_source_is_excluded(self) -> None:
        report = mapping_report(
            product_result(media_item(match_status="unmatched_media_source"))
        )
        self.assertEqual(validate_mapping_report(report), ())

    def test_04_ambiguous_source_is_excluded(self) -> None:
        report = mapping_report(
            product_result(media_item(match_status="ambiguous_media_source"))
        )
        self.assertEqual(validate_mapping_report(report), ())

    def test_05_blocking_product_is_excluded(self) -> None:
        report = mapping_report(
            product_result(media_item(), blockers=["ambiguous_media_source"])
        )
        self.assertEqual(validate_mapping_report(report), ())

    def test_06_blocking_media_source_is_excluded(self) -> None:
        report = mapping_report(
            product_result(media_item(blockers=["unsafe_source"]))
        )
        self.assertEqual(validate_mapping_report(report), ())

    def test_07_safe_single_cell_coordinate(self) -> None:
        self.assertEqual(validate_single_cell_coordinate("AZ565"), "AZ565")

    def test_08_range_coordinate_is_rejected(self) -> None:
        with self.assertRaises(SecureMediaReferenceInputError):
            validate_single_cell_coordinate("I1:I100")

    def test_09_sheet_injection_coordinate_is_rejected(self) -> None:
        with self.assertRaises(SecureMediaReferenceInputError):
            validate_single_cell_coordinate("Sheet1!I16")

    def test_10_formula_coordinate_is_rejected(self) -> None:
        with self.assertRaises(SecureMediaReferenceInputError):
            validate_single_cell_coordinate("=IMPORTXML(A1)")

    def test_11_lowercase_coordinate_is_rejected(self) -> None:
        with self.assertRaises(SecureMediaReferenceInputError):
            validate_single_cell_coordinate("i16")

    def test_12_coordinate_is_deduplicated(self) -> None:
        item = media_item()
        report = mapping_report(product_result(item, copy.deepcopy(item)))
        self.assertEqual(len(validate_mapping_report(report)), 1)

    def test_13_conflicting_duplicate_coordinate_is_rejected(self) -> None:
        report = mapping_report(
            product_result(media_item()),
            product_result(media_item(), start_row=30, end_row=40, model="OTHER"),
        )
        with self.assertRaisesRegex(
            SecureMediaReferenceInputError,
            "duplicate_media_reference_coordinate_conflict",
        ):
            validate_mapping_report(report)

    def test_14_more_than_100_coordinates_is_rejected(self) -> None:
        sources = [
            media_item(
                coordinate=f"I{index}",
                marker=f"B{index - 1}",
            )
            for index in range(2, 103)
        ]
        with self.assertRaisesRegex(
            SecureMediaReferenceInputError, "too_many_media_references"
        ):
            validate_mapping_report(
                mapping_report(product_result(*sources, start_row=1, end_row=200))
            )

    def test_15_batch_get_is_used(self) -> None:
        _, _, factory = run_reader()
        self.assertEqual(len(factory.values.batch_get_calls), 1)

    def test_16_one_google_read_request(self) -> None:
        batch, _, _ = run_reader()
        self.assertEqual(batch.read_requests_performed, 1)

    def test_17_no_per_cell_request_loop(self) -> None:
        report = mapping_report(
            product_result(
                media_item(coordinate="I16"),
                media_item(coordinate="I17", marker="B16"),
            )
        )
        response = metadata_response(value_range("I16"), value_range("I17"))
        batch, _, factory = run_reader(report, response)
        self.assertEqual(batch.coordinates_requested, 2)
        self.assertEqual(len(factory.values.batch_get_calls), 1)

    def test_18_batch_ranges_are_exact_single_cells(self) -> None:
        _, _, factory = run_reader()
        call = factory.values.batch_get_calls[0]
        self.assertEqual(call["ranges"], [f"'{SHEET}'!I16"])

    def test_19_returned_range_matches_by_coordinate(self) -> None:
        batch, _, _ = run_reader()
        self.assertEqual(batch.results[0].mapped_source.reference_coordinate, "I16")
        self.assertEqual(batch.results[0].read_status, "read")

    def test_20_response_order_is_irrelevant(self) -> None:
        report = mapping_report(
            product_result(
                media_item(coordinate="I16"),
                media_item(coordinate="I17", marker="B16"),
            )
        )
        response = metadata_response(
            value_range("I17", "https://example.test/b.jpg"),
            value_range("I16", "https://example.test/a.jpg"),
        )
        batch, _, _ = run_reader(report, response)
        self.assertEqual(
            [item.mapped_source.reference_coordinate for item in batch.results],
            ["I16", "I17"],
        )

    def test_21_missing_response_is_blocked(self) -> None:
        batch, _, _ = run_reader(response=metadata_response())
        self.assertEqual(
            batch.results[0].read_status,
            "media_reference_response_missing",
        )

    def test_22_empty_cell_is_blocked(self) -> None:
        batch, _, _ = run_reader(
            response=metadata_response(value_range("I16", ""))
        )
        self.assertEqual(batch.results[0].read_status, "empty_media_reference")

    def test_23_missing_cell_values_are_blocked(self) -> None:
        response = metadata_response(
            {"startRow": 15, "startColumn": 8, "rowData": []}
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status, "media_reference_cell_missing"
        )

    def test_24_multiple_cell_values_are_rejected(self) -> None:
        response = metadata_response(
            {
                "startRow": 15,
                "startColumn": 8,
                "rowData": [
                    {
                        "values": [
                            {"formattedValue": "one"},
                            {"formattedValue": "two"},
                        ]
                    }
                ],
            }
        )
        with self.assertRaises(SecureMediaReferenceResponseError):
            run_reader(response=response)

    def test_25_raw_reference_is_hidden_from_repr(self) -> None:
        batch, _, _ = run_reader()
        self.assertNotIn(RAW_URL, repr(batch.results[0]))

    def test_26_raw_reference_is_not_in_safe_object_serialization(self) -> None:
        batch, _, _ = run_reader()
        safe = {
            "status": batch.results[0].read_status,
            "fingerprint": batch.results[0].fresh_reference_fingerprint,
        }
        self.assertNotIn(RAW_URL, json.dumps(safe))

    def test_27_fresh_fingerprint_uses_existing_helper(self) -> None:
        batch, _, _ = run_reader()
        self.assertEqual(
            batch.results[0].fresh_reference_fingerprint,
            fingerprint(RAW_URL),
        )

    def test_28_equal_fingerprint_is_verified(self) -> None:
        batch, _, _ = run_reader()
        self.assertEqual(
            batch.results[0].reference_verification, "verified_unchanged"
        )

    def test_29_changed_fingerprint_is_blocked(self) -> None:
        response = metadata_response(
            value_range("I16", "https://example.test/changed")
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(
            result.reference_verification, "reference_changed_since_mapping"
        )
        self.assertIn("reference_changed_since_mapping", result.blocking_issues)

    def test_30_redacted_mapping_accepts_real_cell_read(self) -> None:
        report = mapping_report(
            product_result(media_item(status="redacted"))
        )
        batch, _, _ = run_reader(report=report)
        self.assertEqual(batch.results[0].read_status, "read")
        self.assertEqual(
            batch.results[0].reference_verification,
            "mapping_reference_redacted",
        )
        self.assertFalse(batch.results[0].blocking_issues)

    def test_31_supplier_reference_can_be_passed_to_discovery(self) -> None:
        batch, _, _ = run_reader()
        supplier = batch.results[0].to_supplier_reference()
        self.assertIsNotNone(supplier)
        self.assertEqual(supplier.reference_status, "available")

    def test_32_settings_are_validated_before_factory(self) -> None:
        _, settings, factory = run_reader()
        self.assertEqual(settings.validate_calls, 1)
        self.assertEqual(factory.calls, 1)

    def test_33_no_selected_sources_skips_google_client(self) -> None:
        report = mapping_report(
            product_result(media_item(match_status="unmatched_media_source"))
        )
        batch, settings, factory = run_reader(
            report=report, response=metadata_response()
        )
        self.assertEqual(batch.coordinates_requested, 0)
        self.assertEqual(settings.validate_calls, 0)
        self.assertEqual(factory.calls, 0)

    def test_34_mapping_input_is_not_mutated(self) -> None:
        report = mapping_report(product_result(media_item()))
        before = copy.deepcopy(report)
        run_reader(report=report)
        self.assertEqual(report, before)

    def test_35_write_count_is_always_zero(self) -> None:
        batch, _, factory = run_reader()
        self.assertEqual(batch.write_requests_performed, 0)
        self.assertEqual(factory.values.write_calls, 0)

    def test_36_all_sheets_write_operations_remain_blocked(self) -> None:
        for operation in (
            "sheets.values.update",
            "sheets.values.append",
            "sheets.values.batchUpdate",
            "sheets.spreadsheets.batchUpdate",
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(GoogleOperationBlocked):
                    ensure_google_operation_allowed(operation)

    def test_37_cell_data_hyperlink_is_extracted(self) -> None:
        response = metadata_response(
            value_range("I16", "Download", hyperlink=RAW_URL)
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_38_hyperlink_wins_over_display_text(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "https://example.test/not-the-target",
                hyperlink=RAW_URL,
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_39_one_rich_text_link_is_extracted(self) -> None:
        response = metadata_response(
            value_range("I16", "Click here", rich_links=[RAW_URL])
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_40_duplicate_rich_text_link_is_deduplicated(self) -> None:
        response = metadata_response(
            value_range("I16", "Link", rich_links=[RAW_URL, RAW_URL])
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].read_status, "read")
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_41_multiple_rich_text_links_are_ambiguous(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Two links",
                rich_links=[RAW_URL, "https://example.test/other"],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status, "ambiguous_media_hyperlink"
        )
        self.assertIsNone(batch.results[0].raw_reference)

    def test_42_direct_https_formatted_value_fallback(self) -> None:
        batch, _, _ = run_reader(
            response=metadata_response(value_range("I16", RAW_URL))
        )
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_43_direct_http_formatted_value_fallback(self) -> None:
        raw = "http://127.0.0.1/media"
        report = mapping_report(product_result(media_item(status="redacted")))
        batch, _, _ = run_reader(
            report=report,
            response=metadata_response(value_range("I16", raw)),
        )
        self.assertEqual(batch.results[0].raw_reference, raw)

    def test_44_plain_display_text_has_link_missing_status(self) -> None:
        batch, _, _ = run_reader(
            response=metadata_response(value_range("I16", "Google Drive"))
        )
        self.assertEqual(
            batch.results[0].read_status, "media_reference_link_missing"
        )
        self.assertIsNone(batch.results[0].raw_reference)

    def test_45_grid_response_order_does_not_map_by_array_position(self) -> None:
        report = mapping_report(
            product_result(
                media_item(coordinate="I16", status="redacted"),
                media_item(coordinate="J17", marker="B16", status="redacted"),
            )
        )
        response = metadata_response(
            value_range("J17", "Second", hyperlink="https://example.test/b"),
            value_range("I16", "First", hyperlink="https://example.test/a"),
        )
        batch, _, _ = run_reader(report=report, response=response)
        self.assertEqual(
            [item.raw_reference for item in batch.results],
            ["https://example.test/a", "https://example.test/b"],
        )

    def test_46_start_row_and_column_restore_actual_coordinate(self) -> None:
        report = mapping_report(
            product_result(media_item(coordinate="AZ565", marker="B564", status="redacted"), start_row=560, end_row=570)
        )
        response = metadata_response(
            value_range("AZ565", "Download", hyperlink=RAW_URL)
        )
        batch, _, _ = run_reader(report=report, response=response)
        self.assertEqual(
            batch.results[0].mapped_source.reference_coordinate, "AZ565"
        )

    def test_47_request_contains_only_exact_single_cell_ranges(self) -> None:
        report = mapping_report(
            product_result(
                media_item(coordinate="I16"),
                media_item(coordinate="J17", marker="B16", status="redacted"),
            )
        )
        response = metadata_response(value_range("I16"), value_range("J17"))
        _, _, factory = run_reader(report=report, response=response)
        self.assertEqual(
            factory.values.batch_get_calls[0]["ranges"],
            [f"'{SHEET}'!I16", f"'{SHEET}'!J17"],
        )

    def test_48_eight_cells_use_one_metadata_request(self) -> None:
        sources = [
            media_item(
                coordinate=f"I{row}",
                marker=f"B{row - 1}",
                status="redacted",
            )
            for row in range(16, 24)
        ]
        report = mapping_report(product_result(*sources, start_row=10, end_row=30))
        response = metadata_response(
            *(value_range(f"I{row}", RAW_URL) for row in range(16, 24))
        )
        batch, _, factory = run_reader(report=report, response=response)
        self.assertEqual(batch.coordinates_requested, 8)
        self.assertEqual(batch.read_requests_performed, 1)
        self.assertEqual(len(factory.values.batch_get_calls), 1)

    def test_49_metadata_request_uses_spreadsheets_get_options(self) -> None:
        _, _, factory = run_reader()
        call = factory.values.batch_get_calls[0]
        self.assertTrue(call["includeGridData"])
        self.assertEqual(call["fields"], SHEETS_LINK_CELL_FIELDS)

    def test_50_fields_allowlist_contains_only_link_cell_metadata(self) -> None:
        self.assertIn("formattedValue", SHEETS_LINK_CELL_FIELDS)
        self.assertIn("hyperlink", SHEETS_LINK_CELL_FIELDS)
        self.assertIn("textFormatRuns", SHEETS_LINK_CELL_FIELDS)
        self.assertIn("link(uri)", SHEETS_LINK_CELL_FIELDS)

    def test_51_fields_request_only_approved_cell_link_and_formula_metadata(self) -> None:
        self.assertIn("userEnteredFormat(textFormat(link(uri)))", SHEETS_LINK_CELL_FIELDS)
        self.assertIn("userEnteredValue(formulaValue)", SHEETS_LINK_CELL_FIELDS)

    def test_52_fields_do_not_request_notes_or_permissions(self) -> None:
        for forbidden in ("note", "comment", "permission", "owner"):
            self.assertNotIn(forbidden, SHEETS_LINK_CELL_FIELDS.casefold())

    def test_53_hyperlink_raw_target_is_hidden_from_repr(self) -> None:
        batch, _, _ = run_reader(
            response=metadata_response(
                value_range("I16", "Download", hyperlink=RAW_URL)
            )
        )
        self.assertNotIn(RAW_URL, repr(batch.results[0]))

    def test_54_hyperlink_raw_target_is_not_in_safe_serialization(self) -> None:
        batch, _, _ = run_reader(
            response=metadata_response(
                value_range("I16", "Download", hyperlink=RAW_URL)
            )
        )
        safe = {
            "status": batch.results[0].read_status,
            "fingerprint": batch.results[0].fresh_reference_fingerprint,
        }
        self.assertNotIn(RAW_URL, json.dumps(safe))

    def test_55_formula_display_text_is_not_parsed_or_retained(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}","Download")'
        batch, _, _ = run_reader(
            response=metadata_response(value_range("I16", formula))
        )
        self.assertEqual(
            batch.results[0].read_status, "media_reference_link_missing"
        )
        self.assertIsNone(batch.results[0].raw_reference)
        self.assertNotIn(formula, repr(batch.results[0]))

    def test_56_direct_hyperlink_wins_over_ambiguous_rich_links(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Mixed",
                hyperlink=RAW_URL,
                rich_links=[
                    "https://example.test/a",
                    "https://example.test/b",
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_57_invalid_hyperlink_allows_direct_url_fallback(self) -> None:
        response = metadata_response(
            value_range("I16", RAW_URL, hyperlink="not a uri")
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_58_fields_include_only_approved_smart_chip_metadata(self) -> None:
        self.assertIn("chipRuns", SHEETS_LINK_CELL_FIELDS)
        self.assertIn("startIndex", SHEETS_LINK_CELL_FIELDS)
        self.assertIn("richLinkProperties(uri,mimeType)", SHEETS_LINK_CELL_FIELDS)
        self.assertNotIn("personProperties", SHEETS_LINK_CELL_FIELDS)

    def test_59_drive_folder_smart_chip_uri_is_extracted(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Drive folder",
                chip_runs=[
                    {
                        "startIndex": 0,
                        "chip": {
                            "richLinkProperties": {
                                "uri": RAW_URL,
                                "mimeType": "application/vnd.google-apps.folder",
                            }
                        },
                    }
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_60_one_unique_smart_chip_uri_is_read(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Chip",
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].read_status, "read")

    def test_61_duplicate_smart_chip_uri_is_deduplicated(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Duplicate chips",
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}},
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}},
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(result.raw_reference, RAW_URL)
        self.assertEqual(result.smart_chip_rich_link_count, 2)
        self.assertTrue(result.smart_chip_unique_uri)

    def test_62_multiple_smart_chip_uris_are_ambiguous(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Multiple chips",
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}},
                    {
                        "chip": {
                            "richLinkProperties": {
                                "uri": "https://example.test/other"
                            }
                        }
                    },
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status, "ambiguous_media_smart_chip"
        )
        self.assertIsNone(batch.results[0].raw_reference)

    def test_63_person_chip_only_is_ignored_and_link_missing(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Supplier contact",
                chip_runs=[
                    {
                        "chip": {
                            "personProperties": {
                                "email": "mock-person@example.invalid"
                            }
                        }
                    }
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(result.read_status, "media_reference_link_missing")
        self.assertTrue(result.smart_chip_present)
        self.assertEqual(result.smart_chip_rich_link_count, 0)

    def test_64_person_chip_does_not_hide_rich_link_chip(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Mixed chips",
                chip_runs=[
                    {"chip": {"personProperties": {"email": "mock@example.invalid"}}},
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}},
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_65_cell_hyperlink_wins_over_smart_chip(self) -> None:
        hyperlink = "https://example.test/hyperlink-wins"
        response = metadata_response(
            value_range(
                "I16",
                "Mixed",
                hyperlink=hyperlink,
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, hyperlink)

    def test_66_text_format_link_wins_over_smart_chip(self) -> None:
        rich_text = "https://example.test/text-format-wins"
        response = metadata_response(
            value_range(
                "I16",
                "Mixed",
                rich_links=[rich_text],
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, rich_text)

    def test_67_smart_chip_wins_over_formatted_url_fallback(self) -> None:
        fallback = "https://example.test/formatted-fallback"
        response = metadata_response(
            value_range(
                "I16",
                fallback,
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_68_formatted_direct_url_fallback_remains_supported(self) -> None:
        batch, _, _ = run_reader(
            response=metadata_response(value_range("I16", RAW_URL))
        )
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_69_smart_chip_uri_is_hidden_from_repr(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Drive folder",
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertNotIn(RAW_URL, repr(batch.results[0]))

    def test_70_smart_chip_raw_id_is_not_in_safe_serialization(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Drive folder",
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        safe = {
            "status": batch.results[0].read_status,
            "fingerprint": batch.results[0].fresh_reference_fingerprint,
            "smart_chip_present": batch.results[0].smart_chip_present,
        }
        self.assertNotIn("FOLDER_ID_PRIVATE", json.dumps(safe))

    def test_71_smart_chip_mime_does_not_bypass_discovery_core(self) -> None:
        non_drive_uri = "https://example.test/photo.jpg"
        response = metadata_response(
            value_range(
                "I16",
                "Looks like folder",
                chip_runs=[
                    {
                        "chip": {
                            "richLinkProperties": {
                                "uri": non_drive_uri,
                                "mimeType": "application/vnd.google-apps.folder",
                            }
                        }
                    }
                ],
            )
        )
        report = mapping_report(product_result(media_item(status="redacted")))
        batch, _, _ = run_reader(report=report, response=response)
        discovery = discover_media_source(batch.results[0].to_supplier_reference())
        self.assertEqual(discovery.provider, "direct_web")
        self.assertEqual(discovery.resource_kind, "direct_image_candidate")

    def test_72_eight_smart_chips_use_one_metadata_request(self) -> None:
        sources = [
            media_item(coordinate=f"I{row}", marker=f"B{row - 1}", status="redacted")
            for row in range(16, 24)
        ]
        report = mapping_report(product_result(*sources, start_row=10, end_row=30))
        response = metadata_response(
            *(
                value_range(
                    f"I{row}",
                    "Drive folder",
                    chip_runs=[
                        {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                    ],
                )
                for row in range(16, 24)
            )
        )
        batch, _, factory = run_reader(report=report, response=response)
        self.assertEqual(batch.coordinates_requested, 8)
        self.assertEqual(batch.read_requests_performed, 1)
        self.assertEqual(len(factory.values.batch_get_calls), 1)

    def test_73_smart_chip_grid_order_is_irrelevant(self) -> None:
        report = mapping_report(
            product_result(
                media_item(coordinate="I16", status="redacted"),
                media_item(coordinate="J17", marker="B16", status="redacted"),
            )
        )
        first = "https://example.test/first"
        second = "https://example.test/second"
        response = metadata_response(
            value_range("J17", "Second", chip_runs=[{"chip": {"richLinkProperties": {"uri": second}}}]),
            value_range("I16", "First", chip_runs=[{"chip": {"richLinkProperties": {"uri": first}}}]),
        )
        batch, _, _ = run_reader(report=report, response=response)
        self.assertEqual(
            [item.raw_reference for item in batch.results], [first, second]
        )

    def test_74_smart_chip_safe_diagnostic_fields_are_recorded(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Drive folder",
                chip_runs=[
                    {"chip": {"richLinkProperties": {"uri": RAW_URL}}}
                ],
            )
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertTrue(result.smart_chip_present)
        self.assertEqual(result.smart_chip_rich_link_count, 1)
        self.assertTrue(result.smart_chip_unique_uri)

    def test_75_whole_cell_link_is_extracted(self) -> None:
        response = metadata_response(
            value_range("I16", "Download", cell_level_link=RAW_URL)
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(result.raw_reference, RAW_URL)
        self.assertTrue(result.cell_level_link_present)

    def test_76_cell_level_link_is_memory_only(self) -> None:
        response = metadata_response(
            value_range("I16", "Download", cell_level_link=RAW_URL)
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        safe = {
            "status": result.read_status,
            "cell_level_link_present": result.cell_level_link_present,
            "fingerprint": result.fresh_reference_fingerprint,
        }
        self.assertNotIn(RAW_URL, repr(result))
        self.assertNotIn(RAW_URL, json.dumps(safe))

    def test_77_run_level_link_overrides_cell_level_link(self) -> None:
        run_link = "https://example.test/run-level"
        response = metadata_response(
            value_range(
                "I16",
                "Download",
                rich_links=[run_link],
                cell_level_link=RAW_URL,
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, run_link)
        self.assertTrue(batch.results[0].cell_level_link_present)

    def test_78_ambiguous_run_links_do_not_fall_back_to_cell_level(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Download",
                rich_links=[RAW_URL, "https://example.test/second"],
                cell_level_link="https://example.test/cell-level",
            )
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(result.read_status, "ambiguous_media_hyperlink")
        self.assertIsNone(result.raw_reference)
        self.assertTrue(result.cell_level_link_present)

    def test_79_formula_present_diagnostic(self) -> None:
        response = metadata_response(
            value_range("I16", "Result", formula="=SUM(A1:A2)")
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertTrue(result.formula_present)
        self.assertEqual(result.formula_function, "OTHER")
        self.assertFalse(result.formula_is_hyperlink)

    def test_80_hyperlink_formula_function_is_detected(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}","Download")'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertTrue(result.formula_present)
        self.assertEqual(result.formula_function, "HYPERLINK")
        self.assertTrue(result.formula_is_hyperlink)

    def test_81_image_formula_function_is_detected_without_execution(self) -> None:
        formula = f'=IMAGE("{RAW_URL}")'
        response = metadata_response(value_range("I16", "Image", formula=formula))
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(result.formula_function, "IMAGE")
        self.assertFalse(result.formula_is_hyperlink)
        self.assertIsNone(result.raw_reference)

    def test_82_other_formula_is_safely_classified(self) -> None:
        response = metadata_response(
            value_range("I16", "Result", formula=" = concat(A1, B1)")
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(result.formula_function, "OTHER")
        self.assertEqual(result.read_status, "media_reference_link_missing")

    def test_83_literal_hyperlink_formula_extracts_first_argument(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}")'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_84_escaped_quote_in_formula_label_is_handled(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}","Download ""secure""")'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_85_comma_in_formula_label_is_not_treated_as_an_argument(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}","Download, secure")'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_86_dynamic_hyperlink_first_argument_is_unsupported(self) -> None:
        response = metadata_response(
            value_range(
                "I16",
                "Download",
                formula='=HYPERLINK(CONCAT("https://example.test/", A1), "Open")',
            )
        )
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        self.assertEqual(
            result.read_status, "dynamic_hyperlink_formula_unsupported"
        )
        self.assertIsNone(result.raw_reference)

    def test_87_cell_reference_hyperlink_first_argument_is_unsupported(self) -> None:
        response = metadata_response(
            value_range("I16", "Download", formula="=HYPERLINK(A1, \"Open\")")
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status,
            "dynamic_hyperlink_formula_unsupported",
        )

    def test_88_malformed_hyperlink_formula_is_unsupported(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}","Unclosed label)'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status, "unsupported_hyperlink_formula"
        )
        self.assertIsNone(batch.results[0].raw_reference)

    def test_89_raw_formula_is_never_retained_or_serialized(self) -> None:
        secret_formula_fragment = "FORMULA_PRIVATE_FRAGMENT"
        formula = f'=HYPERLINK("{RAW_URL}","{secret_formula_fragment}")'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        safe = {
            "formula_present": result.formula_present,
            "formula_function": result.formula_function,
            "formula_is_hyperlink": result.formula_is_hyperlink,
        }
        self.assertNotIn(formula, repr(result))
        self.assertNotIn(secret_formula_fragment, repr(result))
        self.assertNotIn(secret_formula_fragment, json.dumps(safe))

    def test_90_formula_raw_uri_is_never_serialized(self) -> None:
        formula = f'=HYPERLINK("{RAW_URL}","Download")'
        response = metadata_response(value_range("I16", "Download", formula=formula))
        batch, _, _ = run_reader(response=response)
        result = batch.results[0]
        safe = {
            "status": result.read_status,
            "formula_present": result.formula_present,
            "formula_function": result.formula_function,
            "formula_is_hyperlink": result.formula_is_hyperlink,
            "fingerprint": result.fresh_reference_fingerprint,
        }
        self.assertNotIn(RAW_URL, repr(result))
        self.assertNotIn(RAW_URL, json.dumps(safe))

    def test_91_cell_level_link_precedes_smart_chip_and_formula(self) -> None:
        cell_link = "https://example.test/cell-level"
        formula = '=HYPERLINK("https://example.test/formula","Open")'
        response = metadata_response(
            value_range(
                "I16",
                "Download",
                cell_level_link=cell_link,
                chip_runs=[{"chip": {"richLinkProperties": {"uri": RAW_URL}}}],
                formula=formula,
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, cell_link)

    def test_92_smart_chip_precedes_hyperlink_formula(self) -> None:
        formula = '=HYPERLINK("https://example.test/formula","Open")'
        response = metadata_response(
            value_range(
                "I16",
                "Download",
                chip_runs=[{"chip": {"richLinkProperties": {"uri": RAW_URL}}}],
                formula=formula,
            )
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_93_eight_mixed_cells_still_use_one_metadata_request(self) -> None:
        sources = [
            media_item(coordinate=f"I{row}", marker=f"B{row - 1}", status="redacted")
            for row in range(16, 24)
        ]
        report = mapping_report(product_result(*sources, start_row=10, end_row=30))
        response = metadata_response(
            *(
                value_range(
                    f"I{row}",
                    "Download",
                    cell_level_link=RAW_URL if row % 2 == 0 else None,
                    formula=(
                        f'=HYPERLINK("{RAW_URL}","Open")'
                        if row % 2
                        else None
                    ),
                )
                for row in range(16, 24)
            )
        )
        batch, _, factory = run_reader(report=report, response=response)
        self.assertEqual(batch.read_requests_performed, 1)
        self.assertEqual(len(factory.values.batch_get_calls), 1)
        self.assertEqual(factory.values.write_calls, 0)

    def test_94_non_hyperlink_formula_allows_direct_url_fallback(self) -> None:
        response = metadata_response(
            value_range("I16", RAW_URL, formula="=CONCAT(A1, B1)")
        )
        batch, _, _ = run_reader(response=response)
        self.assertEqual(batch.results[0].raw_reference, RAW_URL)

    def test_95_metadata_drive_scope_and_missing_folder_ids_are_accepted(self) -> None:
        batch, settings, factory = run_reader()
        self.assertEqual(batch.results[0].read_status, "read")
        self.assertEqual(settings.drive_scope, GOOGLE_DRIVE_METADATA_READONLY_SCOPE)
        self.assertEqual(settings.clm_drive_folder_id, "")
        self.assertEqual(settings.md_drive_folder_id, "")
        self.assertEqual(factory.calls, 1)

    def test_96_full_factory_and_drive_client_are_never_used(self) -> None:
        batch, _, factory = run_reader()
        self.assertEqual(batch.read_requests_performed, 1)
        self.assertEqual(factory.full_create_calls, 0)
        self.assertFalse(factory.drive_client_created)

    def test_97_sheets_gateway_exposes_no_drive_operations(self) -> None:
        gateway = ReadOnlySheetsGateway(FakeSheets(FakeValues(metadata_response())))
        self.assertFalse(hasattr(gateway, "get_folder"))
        self.assertFalse(hasattr(gateway, "list_folder_children"))

    def test_98_current_eight_exact_coordinates_use_one_sheets_request(self) -> None:
        current_ranges = (
            (479, 489, "I488"),
            (490, 500, "I499"),
            (501, 511, "I510"),
            (512, 522, "I521"),
            (523, 533, "I532"),
            (534, 544, "I543"),
            (545, 555, "I554"),
            (556, 565, "I565"),
        )
        products = tuple(
            product_result(
                media_item(
                    coordinate=coordinate,
                    marker=f"B{int(coordinate[1:]) - 1}",
                    status="redacted",
                ),
                start_row=start_row,
                end_row=end_row,
                model=f"MODEL-{index}",
            )
            for index, (start_row, end_row, coordinate) in enumerate(
                current_ranges, start=1
            )
        )
        response = metadata_response(
            *(
                value_range(coordinate, RAW_URL)
                for _, _, coordinate in current_ranges
            )
        )
        batch, _, factory = run_reader(
            report=mapping_report(*products), response=response
        )
        self.assertEqual(
            [item.mapped_source.reference_coordinate for item in batch.results],
            [coordinate for _, _, coordinate in current_ranges],
        )
        self.assertEqual(batch.read_requests_performed, 1)
        self.assertEqual(len(factory.values.batch_get_calls), 1)


if __name__ == "__main__":
    unittest.main()
