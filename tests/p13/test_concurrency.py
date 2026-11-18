from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import Role
from apps.runs import dispatch, services
from apps.runs.jobs import perform_work
from apps.runs.models import Run, RunAttempt
from apps.reviews.models import Task
from tests.p01.factories import assign, make_item, make_task, make_user, make_version


@unittest.skipUnless(connection.vendor == "postgresql", "requires actual PostgreSQL multi-connection locks")
@override_settings(EXECUTOR_MODE="rq", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConcurrentWorkerTests(TransactionTestCase):
    def setUp(self):
        supplier = make_user("pg_supplier", Role.SUPPLIER)
        self.operator = make_user("pg_operator", Role.OPERATOR)
        self.task = make_task(supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, supplier)
        for row in (5, 6):
            make_item(self.version, row=row)
        self.run = Run.objects.create(task=self.task, version=self.version, kind="precheck", is_current=True)
        services.register_run(self.run)

    def test_two_real_connections_delivering_same_run(self):
        barrier = Barrier(2)
        def execute():
            try:
                barrier.wait(timeout=10)
                perform_work(str(self.run.pk))
            finally:
                connections.close_all()
        original = services.evaluate_row
        def slow(run, item):
            time.sleep(0.05)
            return original(run,item)
        with patch.object(services, "evaluate_row", side_effect=slow):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [pool.submit(execute) for _ in range(2)]
                for result in results:
                    result.result(timeout=15)
        self.assertEqual(RunAttempt.objects.filter(status="succeeded").count(), 2)
        self.assertEqual(self.run.items.filter(status="succeeded").count(), 2)

    def test_two_dispatchers_enqueue_pending_run_once(self):
        barrier = Barrier(2)
        def scan():
            try:
                barrier.wait(timeout=10)
                return dispatch.dispatch_pending(run_id=str(self.run.pk))
            finally:
                connections.close_all()
        with patch.object(dispatch, "_enqueue", side_effect=lambda _: time.sleep(0.05)) as enqueue:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: scan(), range(2)))
        self.assertEqual(sum(results), 1)
        enqueue.assert_called_once_with(self.run.pk)

    def test_model_call_releases_task_lock_for_new_version(self):
        entered, release = Event(), Event()
        original = services.evaluate_row
        def blocked(run,item):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release model call")
            return original(run,item)
        def execute():
            try:
                perform_work(str(self.run.pk))
            finally:
                connections.close_all()
        with patch.object(services, "evaluate_row", side_effect=blocked), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute)
            self.assertTrue(entered.wait(timeout=10))
            try:
                from django.db import transaction
                with transaction.atomic():
                    connection.cursor().execute("SET LOCAL lock_timeout = '2000ms'")
                    task = Task.objects.select_for_update().get(pk=self.task.pk)
                    make_version(task, task.supplier, number=2)
                    task.stage = Task.Stage.UPLOADED
                    task.save(update_fields=["stage"])
            finally:
                release.set()
            future.result(timeout=15)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "superseded")
        self.assertFalse(self.run.items.filter(status="succeeded").exists())

    def test_two_concurrent_reruns_with_same_revision_only_one_wins(self):
        barrier = Barrier(2)
        def execute(index):
            try:
                barrier.wait(timeout=10)
                from apps.accounts.models import User
                user = User.objects.get(pk=self.operator.pk)
                try:
                    return services.start_precheck(user,str(self.version.pk),self.task.revision,f"concurrent-{index}")["run_id"]
                except Exception as exc:
                    return getattr(exc,"code",type(exc).__name__)
            finally:
                connections.close_all()
        with patch("apps.runs.dispatch.safe_dispatch", return_value=0), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute,range(2)))
        self.assertEqual(results.count("stale_revision"), 1, results)
        self.assertEqual(Run.objects.filter(version=self.version,is_current=True).count(), 1)
