from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sync_worker.config import ConfigError, load_config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_safe_defaults_do_not_require_credentials(self) -> None:
        settings = load_config({})

        self.assertEqual(settings.sync_environment, "staging")
        self.assertTrue(settings.dry_run)
        self.assertEqual(settings.default_product_status, "draft")
        self.assertFalse(settings.allow_delete)

    def test_delete_permission_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "ALLOW_DELETE"):
            load_config({"ALLOW_DELETE": "true"})

    def test_non_dry_run_requires_connection_settings(self) -> None:
        with self.assertRaisesRegex(ConfigError, "WP_BASE_URL"):
            load_config({"DRY_RUN": "false"})

    def test_invalid_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "DRY_RUN"):
            load_config({"DRY_RUN": "sometimes"})

    def test_secret_values_are_not_exposed_by_repr(self) -> None:
        secret = "do-not-print-this-secret"
        settings = load_config(
            {
                "WP_APP_PASSWORD": secret,
                "WC_CONSUMER_KEY": secret,
                "WC_CONSUMER_SECRET": secret,
            }
        )

        self.assertNotIn(secret, repr(settings))

    def test_example_environment_file_contains_only_safe_placeholders(self) -> None:
        values = dict(
            line.split("=", maxsplit=1)
            for line in (PROJECT_ROOT / ".env.example").read_text(
                encoding="utf-8"
            ).splitlines()
        )

        self.assertEqual(
            values,
            {
                "WP_BASE_URL": "",
                "WP_USERNAME": "",
                "WP_APP_PASSWORD": "",
                "WC_CONSUMER_KEY": "",
                "WC_CONSUMER_SECRET": "",
                "SYNC_ENVIRONMENT": "staging",
                "DRY_RUN": "true",
                "DEFAULT_PRODUCT_STATUS": "draft",
                "ALLOW_DELETE": "false",
            },
        )


if __name__ == "__main__":
    unittest.main()
