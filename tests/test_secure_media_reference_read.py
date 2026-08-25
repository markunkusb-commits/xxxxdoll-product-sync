from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.google_api import (  # noqa: E402
    GoogleClients,
    GoogleOperationBlocked,
    ensure_google_operation_allowed,
)
from sync_worker.image_mapping import (  # noqa: E402
    REDACTED_REFERENCE,
    create_supplier_media_source_reference,
)
from sync_worker.secure_media_reference_read import (  # noqa: E402
    SecureMediaReferenceInputError,
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


def value_range(coordinate: str, value: object = RAW_URL) -> dict[str, object]:
    return {"range": f"'{SHEET}'!{coordinate}", "values": [[value]]}


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
        self.batch_get_calls.append(dict(kwargs))
        return FakeRequest(self.response)

    def update(self, **kwargs: object) -> None:
        self.write_calls += 1
        raise AssertionError("Sheets write forbidden")


class FakeSpreadsheets:
    def __init__(self, values: FakeValues) -> None:
        self._values = values

    def values(self) -> FakeValues:
        return self._values


class FakeSheets:
    def __init__(self, values: FakeValues) -> None:
        self._spreadsheets = FakeSpreadsheets(values)

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


class FakeSettings:
    clm_spreadsheet_id = "mock-spreadsheet-id"

    def __init__(self) -> None:
        self.validate_calls = 0

    def validate(self) -> None:
        self.validate_calls += 1


class FakeFactory:
    def __init__(self, response: object) -> None:
        self.values = FakeValues(response)
        self.calls = 0

    def create(self, settings: object) -> GoogleClients:
        self.calls += 1
        return GoogleClients(drive=object(), sheets=FakeSheets(self.values))


def run_reader(
    report: dict[str, object] | None = None,
    response: object | None = None,
):
    active_report = report or mapping_report(product_result(media_item()))
    active_response = response or {"valueRanges": [value_range("I16")]}
    settings = FakeSettings()
    factory = FakeFactory(active_response)
    batch = SecureMediaReferenceReader(settings, factory).run(
        active_report, sheet_title=SHEET
    )
    return batch, settings, factory


class SecureMediaReferenceReadTests(unittest.TestCase):
    def test_01_mapping_status_must_be_ok(self) -> None:
        settings = FakeSettings()
        factory = FakeFactory({"valueRanges": []})
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
        response = {
            "valueRanges": [value_range("I16"), value_range("I17")]
        }
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
        response = {
            "valueRanges": [
                value_range("I17", "https://example.test/b.jpg"),
                value_range("I16", "https://example.test/a.jpg"),
            ]
        }
        batch, _, _ = run_reader(report, response)
        self.assertEqual(
            [item.mapped_source.reference_coordinate for item in batch.results],
            ["I16", "I17"],
        )

    def test_21_missing_response_is_blocked(self) -> None:
        batch, _, _ = run_reader(response={"valueRanges": []})
        self.assertEqual(
            batch.results[0].read_status,
            "media_reference_response_missing",
        )

    def test_22_empty_cell_is_blocked(self) -> None:
        batch, _, _ = run_reader(
            response={"valueRanges": [value_range("I16", "")]}
        )
        self.assertEqual(batch.results[0].read_status, "empty_media_reference")

    def test_23_missing_cell_values_are_blocked(self) -> None:
        response = {"valueRanges": [{"range": f"'{SHEET}'!I16"}]}
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status, "media_reference_cell_missing"
        )

    def test_24_multiple_cell_values_are_rejected(self) -> None:
        response = {
            "valueRanges": [
                {"range": f"'{SHEET}'!I16", "values": [["one", "two"]]}
            ]
        }
        batch, _, _ = run_reader(response=response)
        self.assertEqual(
            batch.results[0].read_status, "invalid_media_reference_cell"
        )

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
        response = {
            "valueRanges": [value_range("I16", "https://example.test/changed")]
        }
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
            report=report, response={"valueRanges": []}
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


if __name__ == "__main__":
    unittest.main()
