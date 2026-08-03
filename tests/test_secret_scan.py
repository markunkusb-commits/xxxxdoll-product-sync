from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.sanitization import (  # noqa: E402
    REPORT_SECRET_SCAN_PATTERN,
    REPORT_SECRET_SCAN_PATTERN_TEXT,
)


class ReportSecretScanTests(unittest.TestCase):
    def test_stock_status_does_not_match_consumer_key_pattern(self) -> None:
        self.assertIsNone(REPORT_SECRET_SCAN_PATTERN.search('"stock_status": "instock"'))

    def test_short_consumer_key_does_not_match(self) -> None:
        self.assertIsNone(REPORT_SECRET_SCAN_PATTERN.search("ck_short"))

    def test_long_consumer_key_matches(self) -> None:
        self.assertIsNotNone(
            REPORT_SECRET_SCAN_PATTERN.search("ck_12345678901234567890")
        )

    def test_long_consumer_secret_matches(self) -> None:
        self.assertIsNotNone(
            REPORT_SECRET_SCAN_PATTERN.search("cs_ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        )

    def test_readme_uses_the_boundary_safe_scan_pattern(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(REPORT_SECRET_SCAN_PATTERN_TEXT, readme)


if __name__ == "__main__":
    unittest.main()
