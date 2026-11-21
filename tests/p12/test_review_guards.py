"""PR30 regressions for FK ordering, target metadata and trust listeners."""
import hashlib
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
from tests.p12.shell import bash_command, run_database_script, shell_environment


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
    def test_sourced_environment_preserves_explicit_paths_and_repository_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = root / '附件目录 with spaces'
            files.mkdir()
            data, socket = root / 'never-created-data', root / 'never-created-socket'
            binaries = root / 'no binaries'
            env = dict(os.environ, CUSTOMS_PG_HOST='127.42.1.1', CUSTOMS_PG_PORT='55499',
                       CUSTOMS_PG_NAME='p12_environment_probe', CUSTOMS_PG_USER='p12_environment_probe',
                       CUSTOMS_PGDATA=str(data), CUSTOMS_PG_SOCKET=str(socket), CUSTOMS_PG_BIN=str(binaries),
                       CUSTOMS_FILE_ROOT=str(files))
            inspect = (
                "import json, os, sys; print(json.dumps({"
                "'repo_root':sys.argv[1], 'repo_key':sys.argv[2], 'pgdata':sys.argv[3], "
                "'pg_bin':sys.argv[4], 'pgsocket':sys.argv[5], "
                "'file_root':os.environ['CUSTOMS_FILE_ROOT'], 'host':os.environ['CUSTOMS_PG_HOST'], "
                "'port':os.environ['CUSTOMS_PG_PORT'], 'name':os.environ['CUSTOMS_PG_NAME'], "
                "'user':os.environ['CUSTOMS_PG_USER']}))"
            )
            # Source the actual pg_env.sh; printf preserves the literal Bash
            # root bytes so the key is checked independently of path conversion.
            command = '. "$1"; printf \'%s\\n\' "$REPO_ROOT"; "$PYTHON" -c "$2" "$REPO_ROOT" "$repo_key" "$PGDATA" "$PG_BIN" "$PGSOCKET"'
            result = subprocess.run(
                [*bash_command(), '-c', command, 'p12-environment-probe',
                 (settings.BASE_DIR / 'scripts/database/pg_env.sh').as_posix(), inspect],
                env=shell_environment(env), capture_output=True, text=True, encoding='utf-8', timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            literal_root, encoded = result.stdout.splitlines()
            actual = json.loads(encoded)
            self.assertEqual(Path(actual['repo_root']).resolve(), settings.BASE_DIR.resolve())
            self.assertEqual(actual['repo_key'], hashlib.sha256(literal_root.encode('utf-8')).hexdigest()[:12])
            for key, expected in (('pgdata', data), ('pg_bin', binaries), ('pgsocket', socket), ('file_root', files)):
                self.assertEqual(Path(actual[key]).resolve(), expected.resolve())
            self.assertEqual((actual['host'], actual['port'], actual['name'], actual['user']),
                             ('127.42.1.1', '55499', 'p12_environment_probe', 'p12_environment_probe'))
            self.assertFalse(data.exists())
            self.assertFalse(socket.exists())
            self.assertFalse(binaries.exists())

    def test_unsafe_bind_fails_before_any_cluster_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for host in ('0.0.0.0', '::', '*', '192.168.1.10', 'localhost', '127.0.0.1,0.0.0.0', ' 127.0.0.1'):
                for action in ('init', 'start', 'restart'):
                    with self.subTest(host=host, action=action):
                        env = dict(os.environ, PYTHON=sys.executable, CUSTOMS_PG_HOST=host,
                                   CUSTOMS_PGDATA=str(root / 'never-created'), CUSTOMS_PG_BIN=str(root / 'no-bin'))
                        result = run_database_script(settings.BASE_DIR / 'scripts/database/pg_ctl.sh', action, env=env)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('仅允许单个回环 IP', result.stderr)
                        self.assertFalse((root / 'never-created').exists())

    def test_loopback_reaches_binary_check(self):
        with tempfile.TemporaryDirectory() as directory:
            for host in ('127.0.0.1', '127.42.1.1', '::1'):
                env = dict(os.environ, PYTHON=sys.executable, CUSTOMS_PG_HOST=host,
                           CUSTOMS_PG_BIN=directory, CUSTOMS_PGDATA=directory + '/never-created')
                result = run_database_script(settings.BASE_DIR / 'scripts/database/pg_ctl.sh', 'init', env=env)
                self.assertEqual(result.returncode, 2)
                self.assertIn('找不到', result.stderr)
                self.assertNotIn('仅允许单个回环 IP', result.stderr)
