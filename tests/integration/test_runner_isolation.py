"""The acceptance entry point must not inherit a developer's live configuration."""
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from tests.integration.run import environment


class RunnerIsolationTests(TestCase):
    def test_sqlite_run_discards_live_settings_and_uses_temporary_storage(self):
        with patch.dict(os.environ, {
            "CUSTOMS_ENGINE_MODE": "openai", "CUSTOMS_AI_API_KEY": "must-not-be-inherited",
            "CUSTOMS_DB_BACKEND": "postgres", "CUSTOMS_PG_NAME": "production",
            "CUSTOMS_FILE_ROOT": "/production/files", "CUSTOMS_TARIFF_PATH": "/private/source.json",
            "DJANGO_SETTINGS_MODULE": "deploy.settings_cloud", "DJANGO_SECRET_KEY": "live-secret",
        }):
            child = environment("sqlite", Path("p21-isolated-root"))
        self.assertEqual(child["CUSTOMS_DB_BACKEND"], "sqlite")
        self.assertEqual(child["CUSTOMS_ENGINE_MODE"], "mock")
        self.assertEqual(child["CUSTOMS_EXECUTOR_MODE"], "inline")
        self.assertEqual(child["CUSTOMS_NOTIFICATION_MODE"], "mock")
        self.assertEqual(child["DJANGO_SETTINGS_MODULE"], "config.settings")
        self.assertEqual(child["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(Path(child["CUSTOMS_FILE_ROOT"]), Path("p21-isolated-root/files"))
        self.assertEqual(Path(child["CUSTOMS_SQLITE_PATH"]), Path("p21-isolated-root/acceptance.sqlite3"))
        self.assertNotEqual(child["DJANGO_SECRET_KEY"], "live-secret")
        for name in ("CUSTOMS_AI_API_KEY", "CUSTOMS_PG_NAME", "CUSTOMS_TARIFF_PATH"):
            self.assertNotIn(name, child)

    def test_postgres_refuses_unrelated_database_and_remote_host(self):
        for name, host in (("production", "127.0.0.1"), ("customs_p21", "example.com")):
            with self.subTest(name=name, host=host), patch.dict(os.environ, {
                "CUSTOMS_PG_NAME": name, "CUSTOMS_PG_HOST": host, "CUSTOMS_PG_USER": "p21",
            }):
                with self.assertRaises(ValueError):
                    environment("postgres", Path("p21-isolated-root"))

    def test_explicit_loopback_postgres_connection_is_preserved(self):
        with patch.dict(os.environ, {
            "CUSTOMS_PG_NAME": "customs_p21", "CUSTOMS_PG_HOST": "127.0.0.1",
            "CUSTOMS_PG_USER": "p21", "CUSTOMS_PG_PORT": "55421",
            "CUSTOMS_PG_PASSWORD": "synthetic-test-value",
        }):
            child = environment("postgres", Path("p21-isolated-root"))
        self.assertEqual(child["CUSTOMS_PG_NAME"], "customs_p21")
        self.assertEqual(child["CUSTOMS_PG_HOST"], "127.0.0.1")
        self.assertEqual(child["CUSTOMS_PG_PORT"], "55421")
        self.assertEqual(child["CUSTOMS_DB_BACKEND"], "postgres")
        self.assertEqual(child["CUSTOMS_EXECUTOR_MODE"], "inline")
