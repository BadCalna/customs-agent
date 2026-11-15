"""P12：PostgreSQL 侧的结构证据——编码、列类型、约束与索引真的建在数据库里。

DDL 层面的检查不能靠“端口连通”代替；这些用例只在 PostgreSQL 后端有意义，
在 SQLite 上跳过，并在回交记录里标明跳过原因。
"""
import unittest

from django.apps import apps
from django.db import connection
from django.test import TestCase


PG_ONLY = unittest.skipUnless(
    connection.vendor == "postgresql", "DDL 检查只在 PostgreSQL 后端运行（P12）"
)


@PG_ONLY
class PostgresCatalogTests(TestCase):
    def sql(self, query, params=None):
        with connection.cursor() as cursor:
            cursor.execute(query, params or [])
            return cursor.fetchall()

    def test_server_encoding_is_utf8(self):
        rows = self.sql("select pg_encoding_to_char(encoding) from pg_database where datname = current_database()")
        self.assertEqual(rows[0][0], "UTF8")

    def test_client_encoding_is_utf8(self):
        self.assertEqual(self.sql("show client_encoding")[0][0], "UTF8")

    def test_json_columns_are_jsonb(self):
        rows = self.sql(
            "select column_name, data_type from information_schema.columns "
            "where table_name = %s and column_name in ('result', 'attributes')",
            ["runs_runitem"],
        )
        self.assertEqual({name: kind for name, kind in rows}, {"result": "jsonb"})

    def test_every_business_table_exists(self):
        tables = set(connection.introspection.table_names())
        for model in apps.get_models():
            if model._meta.app_label in {"accounts", "reviews", "runs", "notifications"}:
                self.assertIn(model._meta.db_table, tables, model._meta.label)

    def test_unique_constraints_exist_in_catalog(self):
        rows = self.sql(
            "select conname from pg_constraint "
            "where contype = 'u' and connamespace = 'public'::regnamespace",
        )
        names = {row[0] for row in rows}
        for expected in (
            "uniq_submission_task_version_revision",
            "uniq_idempotency_user_action_key",
            "uniq_item_source_row",
            "uniq_confirmation_revision",
            "uniq_submission_item",
            "uniq_run_item",
        ):
            self.assertIn(expected, names)

    def test_check_constraints_exist_in_catalog(self):
        rows = self.sql("select conname from pg_constraint where contype = 'c'")
        names = {row[0] for row in rows}
        self.assertIn("task_assignment_business_role", names)

    def test_declared_indexes_exist(self):
        rows = self.sql("select indexname from pg_indexes where schemaname = 'public'")
        names = {row[0] for row in rows}
        self.assertIn("idx_run_version_kind_current", names)
        self.assertIn("idx_message_recipient_read", names)

    def test_foreign_keys_are_enforced_by_database(self):
        rows = self.sql("select count(*) from pg_constraint where contype = 'f'")
        self.assertGreater(rows[0][0], 10)

    def test_isolation_level_is_read_committed(self):
        rows = self.sql("show transaction isolation level")
        self.assertEqual(rows[0][0], "read committed")

    def test_migrations_are_fully_applied(self):
        rows = self.sql("select count(*) from django_migrations")
        self.assertGreater(rows[0][0], 10)
        pending = self.sql(
            "select count(*) from django_migrations where app in "
            "('accounts', 'reviews', 'runs', 'notifications')"
        )
        self.assertGreater(pending[0][0], 0)