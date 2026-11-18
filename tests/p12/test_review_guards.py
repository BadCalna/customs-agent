"""PR30 regressions for FK ordering, target metadata and trust listeners."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Role, TaskAssignment, User
from apps.reviews.models import Task
from scripts.database import dumpdata_full, model_inventory


class DependencyFixtureTests(TestCase):
    def test_assignment_round_trip_uses_parent_first_fixture_and_real_loaddata(self):
        supplier = User.objects.create(username='source-supplier', role=Role.SUPPLIER)
        operator = User.objects.create(username='source-operator', role=Role.OPERATOR)
        task = Task.objects.create(title='fixture dependency', supplier=supplier)
        assignment = TaskAssignment.objects.create(task=task, user=operator, role=Role.OPERATOR)
        models = model_inventory.migration_models()
        self.assertLess(models.index(Task), models.index(TaskAssignment))
        self.assertLess(models.index(User), models.index(TaskAssignment))
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'copy.json'
            with patch.object(sys, 'argv', ['dumpdata_full.py', str(fixture)]):
                self.assertEqual(dumpdata_full.main(), 0)
            entries = json.loads(fixture.read_text())
            labels = [entry['model'] for entry in entries]
            self.assertLess(labels.index('reviews.task'), labels.index('accounts.taskassignment'))
            task_id, assignment_id = task.pk, assignment.pk
            task.delete()
            User.objects.filter(pk__in=[supplier.pk, operator.pk]).delete()
            self.assertFalse(TaskAssignment.objects.exists())
            call_command('loaddata', str(fixture), verbosity=0)
            restored = TaskAssignment.objects.select_related('task', 'user').get(pk=assignment_id)
            self.assertEqual(restored.task_id, task_id)
            self.assertEqual(restored.task.title, 'fixture dependency')
            self.assertEqual(restored.user.username, 'source-operator')
            connection.check_constraints()


class TargetMetadataTests(TestCase):
    def test_clean_baseline_is_accepted(self):
        model_inventory.validate_target_metadata(connection)

    def test_custom_or_stale_content_type_is_rejected(self):
        ContentType.objects.create(app_label='old_branch', model='evidence')
        with self.assertRaisesRegex(ValueError, '目标生成元数据'):
            model_inventory.validate_target_metadata(connection)

    def test_unreferenced_custom_permission_is_rejected(self):
        content_type = ContentType.objects.get_for_model(Task)
        Permission.objects.create(content_type=content_type, codename='legacy_action', name='Keep this')
        with self.assertRaisesRegex(ValueError, '目标生成元数据'):
            model_inventory.validate_target_metadata(connection)

    def test_renamed_or_missing_permission_is_rejected(self):
        permission = Permission.objects.first()
        permission.name = 'customized permission display name'
        permission.save(update_fields=['name'])
        with self.assertRaisesRegex(ValueError, '目标生成元数据'):
            model_inventory.validate_target_metadata(connection)
        permission.delete()
        with self.assertRaisesRegex(ValueError, '目标生成元数据'):
            model_inventory.validate_target_metadata(connection)


class LocalTrustGuardTests(SimpleTestCase):
    def test_unsafe_bind_fails_before_any_cluster_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for host in ('0.0.0.0', '::', '*', '192.168.1.10', 'localhost', '127.0.0.1,0.0.0.0', ' 127.0.0.1'):
                for action in ('init', 'start', 'restart'):
                    with self.subTest(host=host, action=action):
                        env = dict(os.environ, PYTHON=sys.executable, CUSTOMS_PG_HOST=host,
                                   CUSTOMS_PGDATA=str(root / 'never-created'), CUSTOMS_PG_BIN=str(root / 'no-bin'))
                        result = subprocess.run([str(settings.BASE_DIR / 'scripts/database/pg_ctl.sh'), action],
                                                env=env, text=True, capture_output=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('仅允许单个回环 IP', result.stderr)
                        self.assertFalse((root / 'never-created').exists())

    def test_loopback_reaches_binary_check(self):
        with tempfile.TemporaryDirectory() as directory:
            for host in ('127.0.0.1', '127.42.1.1', '::1'):
                env = dict(os.environ, PYTHON=sys.executable, CUSTOMS_PG_HOST=host,
                           CUSTOMS_PG_BIN=directory, CUSTOMS_PGDATA=directory + '/never-created')
                result = subprocess.run([str(settings.BASE_DIR / 'scripts/database/pg_ctl.sh'), 'init'],
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('找不到', result.stderr)
                self.assertNotIn('仅允许单个回环 IP', result.stderr)
