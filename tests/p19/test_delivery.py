"""P19 mock DWS outbox: durable retry, lease fencing and no live network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from adapters.notifications.dws import MockDwsTransport, MockReceipt
from apps.accounts.models import Role, TaskAssignment
from apps.notifications.models import DeliveryAttempt, Message, NotificationDelivery
from apps.notifications.services import deliver_one, dispatch_due, record_message
from tests.p01.factories import assign, make_task, make_user, make_version


class FailingTransport:
    def __init__(self):
        self.calls = []

    def send(self, **payload):
        self.calls.append(payload)
        raise RuntimeError("private-token=never-persist https://private.invalid secret business detail")


class RecordingTransport(MockDwsTransport):
    def __init__(self):
        self.calls = []

    def send(self, **payload):
        self.calls.append(payload)
        return super().send(**payload)


class DeliveryFixture:
    def setUp(self):
        self.supplier = make_user("delivery-supplier", Role.SUPPLIER)
        self.operator = make_user("delivery-operator", Role.OPERATOR)
        self.task = make_task(self.supplier)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, self.supplier)

    def new_delivery(self, key="delivery-fixture"):
        message = Message.objects.create(
            recipient=self.operator, task=self.task, version=self.version,
            event_key=key, kind="submission.created", title="合成通知", body="合成消息正文",
        )
        return NotificationDelivery.objects.create(message=message)


@override_settings(NOTIFICATION_MODE="mock", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MockDeliveryTests(DeliveryFixture, TestCase):
    def test_mock_success_has_explicit_receipt_and_no_network(self):
        delivery = self.new_delivery()
        transport = RecordingTransport()
        with patch("socket.create_connection", side_effect=AssertionError("mock must stay offline")):
            self.assertTrue(deliver_one(delivery.pk, transport=transport))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "mocked")
        self.assertEqual(delivery.receipt, f"mock:{delivery.pk}")
        self.assertEqual(delivery.attempts, 1)
        self.assertIsNone(delivery.lease_token)
        self.assertIsNone(delivery.lease_until)
        self.assertIsNone(delivery.next_attempt_at)
        attempt = delivery.history.get()
        self.assertEqual(attempt.status, "mocked")
        self.assertIsNotNone(attempt.finished_at)
        self.assertEqual(transport.calls[0]["delivery_id"], str(delivery.pk))
        self.assertIn(str(self.version.pk), transport.calls[0]["target_url"])

    def test_terminal_delivery_is_not_dispatched_twice(self):
        delivery = self.new_delivery()
        transport = RecordingTransport()
        self.assertTrue(deliver_one(delivery.pk, transport=transport))
        self.assertFalse(deliver_one(delivery.pk, transport=transport))
        self.assertEqual(dispatch_due(transport=transport), 0)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(delivery.history.count(), 1)

    def test_failures_retry_at_due_time_then_stop_after_three_attempts(self):
        delivery = self.new_delivery()
        transport = FailingTransport()
        now = timezone.now()
        for attempt_number in (1, 2, 3):
            self.assertTrue(deliver_one(delivery.pk, transport=transport, now=now))
            delivery.refresh_from_db()
            self.assertEqual(delivery.attempts, attempt_number)
            self.assertEqual(delivery.last_error_code, "transport_failed")
            self.assertEqual(delivery.history.get(number=attempt_number).status, "failed")
            if attempt_number < 3:
                self.assertEqual(delivery.status, "pending")
                self.assertEqual(delivery.next_attempt_at, now + timedelta(seconds=30 * attempt_number))
                self.assertFalse(deliver_one(delivery.pk, transport=transport, now=delivery.next_attempt_at - timedelta(microseconds=1)))
                now = delivery.next_attempt_at
        self.assertEqual(delivery.status, "exhausted")
        self.assertIsNone(delivery.next_attempt_at)
        self.assertFalse(deliver_one(delivery.pk, transport=transport, now=now + timedelta(days=1)))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual({p["delivery_id"] for p in transport.calls}, {str(delivery.pk)})

    def test_failure_details_are_not_persisted(self):
        delivery = self.new_delivery()
        deliver_one(delivery.pk, transport=FailingTransport())
        delivery.refresh_from_db()
        persisted = repr(list(NotificationDelivery.objects.values())) + repr(list(DeliveryAttempt.objects.values()))
        for secret in ("never-persist", "private.invalid", "secret business detail"):
            self.assertNotIn(secret, persisted)
        self.assertEqual(delivery.last_error_code, "transport_failed")

    def test_pending_future_time_and_live_lease_are_not_claimed(self):
        now = timezone.now()
        delivery = self.new_delivery()
        NotificationDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=now + timedelta(minutes=1))
        transport = RecordingTransport()
        self.assertFalse(deliver_one(delivery.pk, transport=transport, now=now))
        self.assertEqual(dispatch_due(now=now, transport=transport), 0)
        NotificationDelivery.objects.filter(pk=delivery.pk).update(
            status="processing", next_attempt_at=None, lease_until=now + timedelta(minutes=1),
        )
        self.assertFalse(deliver_one(delivery.pk, transport=transport, now=now))
        self.assertEqual(dispatch_due(now=now, transport=transport), 0)
        self.assertEqual(transport.calls, [])

    def test_expired_attempt_is_preserved_and_scan_recovers(self):
        now = timezone.now()
        delivery = self.new_delivery()
        old = DeliveryAttempt.objects.create(delivery=delivery, number=1)
        NotificationDelivery.objects.filter(pk=delivery.pk).update(
            status="processing", attempts=1, lease_token=old.token, lease_until=now - timedelta(seconds=1),
        )
        transport = RecordingTransport()
        self.assertEqual(dispatch_due(now=now, transport=transport), 1)
        delivery.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(delivery.status, "mocked")
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(old.status, "interrupted")
        self.assertEqual(old.error_code, "lease_expired")
        self.assertIsNotNone(old.finished_at)
        self.assertEqual(delivery.history.get(number=2).status, "mocked")

    def test_expired_third_attempt_exhausts_without_fourth_send(self):
        now = timezone.now()
        delivery = self.new_delivery()
        old = DeliveryAttempt.objects.create(delivery=delivery, number=3)
        NotificationDelivery.objects.filter(pk=delivery.pk).update(
            status="processing", attempts=3, lease_token=old.token, lease_until=now - timedelta(seconds=1),
        )
        transport = RecordingTransport()
        dispatch_due(now=now, transport=transport)
        delivery.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(delivery.status, "exhausted")
        self.assertEqual(delivery.attempts, 3)
        self.assertEqual(old.status, "interrupted")
        self.assertEqual(transport.calls, [])

    def test_reentrant_claim_during_valid_lease_sends_once(self):
        delivery = self.new_delivery()
        now = timezone.now()
        nested = RecordingTransport()

        class ReentrantTransport(MockDwsTransport):
            def send(inner, **payload):
                self.assertFalse(deliver_one(delivery.pk, transport=nested, now=now))
                return super().send(**payload)

        self.assertTrue(deliver_one(delivery.pk, transport=ReentrantTransport(), now=now))
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(nested.calls, [])

    def test_stale_owner_cannot_replace_newer_mock_result(self):
        delivery = self.new_delivery()
        now = timezone.now()
        successor = RecordingTransport()

        class SupersededTransport(MockDwsTransport):
            def send(inner, **payload):
                self.assertTrue(deliver_one(delivery.pk, transport=successor, now=now + timedelta(seconds=61)))
                raise RuntimeError("old owner fails after successor succeeds")

        self.assertFalse(deliver_one(delivery.pk, transport=SupersededTransport(), now=now))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "mocked")
        self.assertEqual(delivery.last_error_code, "")
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(delivery.history.get(number=1).status, "interrupted")
        self.assertEqual(delivery.history.get(number=2).status, "mocked")

    def test_owner_that_finishes_after_lease_expiry_cannot_publish(self):
        delivery = self.new_delivery()
        now = timezone.now()
        # Time moves past the lease while the unlocked transport is in flight;
        # even without a successor yet, the expired owner cannot publish.
        with patch("apps.notifications.services.timezone.now", return_value=now + timedelta(seconds=61)):
            self.assertFalse(deliver_one(delivery.pk, now=now))
        delivery.refresh_from_db()
        self.assertNotEqual(delivery.status, "mocked")
        self.assertEqual(delivery.receipt, "")
        self.assertEqual(dispatch_due(now=now + timedelta(seconds=62)), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "mocked")
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(delivery.history.get(number=1).status, "interrupted")

    def test_processing_record_without_lease_can_be_recovered(self):
        delivery = self.new_delivery()
        old = DeliveryAttempt.objects.create(delivery=delivery, number=1)
        NotificationDelivery.objects.filter(pk=delivery.pk).update(
            status="processing", attempts=1, lease_token=old.token, lease_until=None,
        )
        self.assertEqual(dispatch_due(), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "mocked")
        self.assertEqual(delivery.attempts, 2)
        old.refresh_from_db()
        self.assertEqual(old.status, "interrupted")

    @override_settings(NOTIFICATION_MODE="disabled")
    def test_disabled_mode_never_calls_transport(self):
        delivery = self.new_delivery()
        transport = RecordingTransport()
        self.assertTrue(deliver_one(delivery.pk, transport=transport))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "suppressed")
        self.assertEqual(delivery.receipt, "")
        self.assertEqual(transport.calls, [])

    def test_inactive_or_revoked_recipient_never_calls_transport(self):
        transport = RecordingTransport()
        for mode in ("inactive", "revoked"):
            with self.subTest(mode=mode):
                delivery = self.new_delivery(key=mode)
                if mode == "inactive":
                    type(self.operator).objects.filter(pk=self.operator.pk).update(is_active=False)
                else:
                    type(self.operator).objects.filter(pk=self.operator.pk).update(is_active=True)
                    TaskAssignment.objects.filter(task=self.task, user=self.operator).delete()
                self.assertTrue(deliver_one(delivery.pk, transport=transport))
                delivery.refresh_from_db()
                self.assertEqual(delivery.status, "suppressed")
                self.assertEqual(delivery.last_error_code, "recipient_unavailable")
        self.assertEqual(transport.calls, [])

    def test_invalid_or_live_receipt_is_not_accepted_as_mock_success(self):
        delivery = self.new_delivery()

        class InvalidTransport:
            def send(inner, **payload):
                return MockReceipt(delivery_id=payload["delivery_id"], receipt="claimed-live", mode="live")

        self.assertTrue(deliver_one(delivery.pk, transport=InvalidTransport()))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "pending")
        self.assertEqual(delivery.last_error_code, "transport_failed")
        self.assertEqual(delivery.receipt, "")

    def test_batch_limit_and_failure_do_not_block_other_messages(self):
        first = self.new_delivery("first")
        second = self.new_delivery("second")
        with patch("apps.notifications.services.deliver_one", side_effect=[RuntimeError("do not leak"), True]) as send:
            with self.assertLogs("apps.notifications.services", level="ERROR") as logs:
                self.assertEqual(dispatch_due(limit=2), 1)
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("do not leak", " ".join(logs.output))
        self.assertIn(str(first.pk), " ".join(logs.output))
        self.assertEqual(NotificationDelivery.objects.count(), 2)

    def test_outbox_callback_waits_for_commit_and_rolls_back_with_business(self):
        with patch("apps.notifications.services.deliver_one") as send:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                message = record_message(recipient=self.operator, task=self.task, version=self.version,
                                         kind="submission.created", event_key="commit-test", title="合成待审提醒")
                self.assertEqual(NotificationDelivery.objects.filter(message=message, status="pending").count(), 1)
                send.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            send.assert_called_once_with(str(message.delivery.pk))
        with self.captureOnCommitCallbacks(execute=True) as rolled_back_callbacks:
            try:
                with transaction.atomic():
                    record_message(recipient=self.operator, task=self.task, version=self.version,
                                   kind="submission.created", event_key="rollback-test", title="应回滚")
                    raise RuntimeError("business transaction rolled back")
            except RuntimeError:
                pass
        self.assertEqual(rolled_back_callbacks, [])
        self.assertFalse(Message.objects.filter(event_key="rollback-test").exists())


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks and independent connections")
@override_settings(NOTIFICATION_MODE="mock", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PostgreSQLDeliveryConcurrencyTests(DeliveryFixture, TransactionTestCase):
    def test_two_connections_cannot_send_same_active_lease(self):
        delivery = self.new_delivery()
        entered = Event()
        release = Event()
        calls = []

        class BlockingTransport(MockDwsTransport):
            def send(inner, **payload):
                calls.append(payload["delivery_id"])
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("test release timeout")
                return super().send(**payload)

        def first_owner():
            close_old_connections()
            try:
                return deliver_one(delivery.pk, transport=BlockingTransport())
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first_owner)
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertFalse(deliver_one(delivery.pk, transport=RecordingTransport()))
            finally:
                release.set()
            self.assertTrue(future.result(timeout=10))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "mocked")
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(calls, [str(delivery.pk)])
