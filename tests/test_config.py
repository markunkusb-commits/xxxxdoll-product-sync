from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import sync_worker.config as config_module  # noqa: E402
from sync_worker.config import ConfigError, load_config  # noqa: E402


SAFE_CONFIG = {
    "WP_BASE_URL": "https://sandbox.wpcomstaging.com",
    "WP_USERNAME": "fake-user",
    "WP_APP_PASSWORD": "fake-app-password",
    "WC_CONSUMER_KEY": "fake-consumer-key",
    "WC_CONSUMER_SECRET": "fake-consumer-secret",
    "SYNC_ENVIRONMENT": "staging",
    "DRY_RUN": "true",
    "DEFAULT_PRODUCT_STATUS": "draft",
    "ALLOW_DELETE": "false",
}


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

    def test_non_dry_run_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "DRY_RUN"):
            load_config({"DRY_RUN": "false"})

    def test_invalid_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "DRY_RUN"):
            load_config({"DRY_RUN": "sometimes"})

    def test_secret_values_are_not_exposed_by_repr(self) -> None:
        secret = "do-not-print-this-secret"
        settings = load_config(
            {
                "WP_BASE_URL": f"https://{secret}.wpcomstaging.com",
                "WP_USERNAME": secret,
                "WP_APP_PASSWORD": secret,
                "WC_CONSUMER_KEY": secret,
                "WC_CONSUMER_SECRET": secret,
            }
        )

        self.assertNotIn(secret, repr(settings))

    def test_loads_dotenv_from_default_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dotenv_path = Path(temporary_directory) / ".env"
            dotenv_path.write_text(
                "\n".join(f"{name}={value}" for name, value in SAFE_CONFIG.items()),
                encoding="utf-8",
            )

            with (
                patch.object(config_module, "DEFAULT_DOTENV_PATH", dotenv_path),
                patch.dict(os.environ, {}, clear=True),
            ):
                settings = load_config()

        self.assertTrue(all(settings.configured_status().values()))
        self.assertTrue(settings.staging_safety_checks().all_passed)

    def test_process_environment_overrides_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            dotenv_path = Path(temporary_directory) / ".env"
            dotenv_path.write_text("WP_USERNAME=from-file\n", encoding="utf-8")

            with patch.dict(
                os.environ, {"WP_USERNAME": "from-process"}, clear=True
            ):
                settings = load_config(dotenv_path=dotenv_path)

        self.assertEqual(settings.wp_username, "from-process")

    def test_staging_safety_checks_pass_for_safe_target(self) -> None:
        checks = load_config(SAFE_CONFIG).staging_safety_checks()

        self.assertTrue(checks.uses_https)
        self.assertTrue(checks.host_is_wpcomstaging)
        self.assertTrue(checks.host_is_not_xxxxdoll_production)
        self.assertTrue(checks.environment_is_staging)
        self.assertTrue(checks.dry_run_enabled)
        self.assertTrue(checks.product_status_is_draft)
        self.assertTrue(checks.delete_disabled)
        self.assertTrue(checks.all_passed)

    def test_unsafe_staging_controls_are_rejected(self) -> None:
        unsafe_cases = (
            ({**SAFE_CONFIG, "WP_BASE_URL": "http://sandbox.wpcomstaging.com"}, "HTTPS"),
            ({**SAFE_CONFIG, "WP_BASE_URL": "https://[invalid"}, "HTTPS"),
            ({**SAFE_CONFIG, "WP_BASE_URL": "https://xxxxdoll.com"}, "wpcomstaging"),
            ({**SAFE_CONFIG, "SYNC_ENVIRONMENT": "production"}, "SYNC_ENVIRONMENT"),
            ({**SAFE_CONFIG, "DRY_RUN": "false"}, "DRY_RUN"),
            ({**SAFE_CONFIG, "DEFAULT_PRODUCT_STATUS": "publish"}, "DEFAULT_PRODUCT_STATUS"),
            ({**SAFE_CONFIG, "ALLOW_DELETE": "true"}, "ALLOW_DELETE"),
        )

        for values, expected_error in unsafe_cases:
            with self.subTest(expected_error=expected_error):
                with self.assertRaisesRegex(ConfigError, expected_error):
                    load_config(values)

    def test_hostname_is_masked(self) -> None:
        settings = load_config(SAFE_CONFIG)

        self.assertEqual(settings.masked_hostname(), "***.wpcomstaging.com")
        self.assertNotIn("sandbox", settings.masked_hostname() or "")

    def test_gitignore_protects_local_dotenv_files(self) -> None:
        rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        self.assertIn(".env", rules)
        self.assertIn(".env.*", rules)
        self.assertIn("!.env.example", rules)

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
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "CLM_SPREADSHEET_ID": "",
                "CLM_DRIVE_FOLDER_ID": "",
                "MD_DRIVE_FOLDER_ID": "",
                "GOOGLE_DRIVE_SCOPE": "https://www.googleapis.com/auth/drive.readonly",
                "GOOGLE_SHEETS_SCOPE": "https://www.googleapis.com/auth/spreadsheets.readonly",
                "GOOGLE_PROXY_MODE": "",
                "GOOGLE_PROXY_HOST": "",
                "GOOGLE_PROXY_PORT": "",
                "GOOGLE_PROXY_RDNS": "true",
            },
        )


if __name__ == "__main__":
    unittest.main()
