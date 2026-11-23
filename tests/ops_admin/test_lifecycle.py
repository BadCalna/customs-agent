from concurrent.futures import ThreadPoolExecutor
from json import dumps, loads
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import DatabaseError, close_old_connections, connection, connections, transaction
from django.db.models import Q
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.errors import ApiError
from apps.accounts.models import TaskAssignment, User
from apps.ops_admin.catalog import sync_catalog
from apps.ops_admin.lifecycle import services, tickets
from apps.ops_admin.lifecycle.models import AccountLifecycle, LifecycleRateBucket, SecretTicket
from apps.ops_admin.models import AuditRecord, MutationReceipt
from apps.ops_admin.mutations import lock_ops_state, mutate
from apps.ops_admin.security import digest
from apps.reviews.models import Task


class LifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(username="system_actor", password="AdminSeed_Safe:912")
        cls.actor.user_permissions.set(Permission.objects.filter(
            content_type__app_label="ops_admin", content_type__model="permissioncatalog",
        ))

    def request(self, user=None, *, key="lifecycle-tests-0001", **extras):
        req = RequestFactory().post("/api/ops/admin/", HTTP_IDEMPOTENCY_KEY=key, **extras)
        req.user = self.actor if user is None else user
        req.auth = req.user
        return req

    def run_mutation(self, operation, *args, actor=None, **kwargs):
        with transaction.atomic():
            lock_ops_state()
            return operation(self.request(actor), *args, **kwargs)

    def permission(self, code):
        return Permission.objects.get(content_type__app_label="ops_admin", content_type__model="permissioncatalog", codename=code)

    def user(self, name, *grants, **fields):
        user = User.objects.create_user(username=name, password="OperatorSeed_Safe:912", **fields)
        if grants:
            user.user_permissions.add(*(self.permission(code) for code in grants))
        return user

    def session(self, user):
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        session.save()
        return session.session_key

    def pending(self, name="pending_user", **fields):
        result = self.run_mutation(services.create_user, {"username": name, **fields})
        self.assertEqual(result["status"], "pending_secret")
        user = User.objects.get(pk=result["id"])
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        return user

    def issue(self, user):
        return self.run_mutation(services.issue_ticket, user.pk, delivery_mode="offline")["ticket"]

    def consume(self, raw, password="Fresh_passphrase:8675309"):
        return self.run_mutation(services.consume_ticket, ticket=raw, new_password=password, actor=AnonymousUser())

    def denied(self, code, func, *args, **kwargs):
        with self.assertRaises(ApiError) as error:
            self.run_mutation(func, *args, **kwargs)
        self.assertEqual(error.exception.code, code)

    def test_create_first_setup_and_reissue_is_one_time(self):
        user = self.pending()
        first = self.issue(user)
        second = self.issue(user)
        self.assertNotEqual(first, second)
        persisted = dumps(list(SecretTicket.objects.values()), default=str)
        for sensitive in (first, second, self.request().headers["Idempotency-Key"]):
            self.assertNotIn(sensitive, persisted)
        self.assertEqual(SecretTicket.objects.filter(revoked_at__isnull=False).count(), 1)
        self.assertEqual(self.consume(second, "123")[1]["error"]["code"], "invalid_password")
        self.assertEqual(self.consume(second), {"status": "accepted"})
        self.assertEqual(self.consume(first), self.consume(second))
        self.assertEqual(self.consume(second), self.consume("A" * 43))
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("Fresh_passphrase:8675309"))
        self.assertEqual(user.ops_lifecycle.status, AccountLifecycle.Status.ACTIVE)
        kinds = set(AuditRecord.objects.values_list("event_type", flat=True))
        self.assertTrue({"ops.secret.issued", "ops.secret.revoked", "ops.secret.consumed", "ops.secret.offline_delivered"} <= kinds)

    def test_reset_revokes_sessions_and_native_changes_invalidate_ticket(self):
        user = self.user("legacy", email="before@example.test")
        other = self.user("other")
        key, unrelated = self.session(user), self.session(other)
        self.assertEqual(services.session_counts([user, other]), {str(user.pk): 1, str(other.pk): 1})
        raw = self.issue(user)
        user.email = "after@example.test"
        user.save(update_fields=["email"])
        self.assertEqual(self.consume(raw)[1]["error"]["code"], "invalid_ticket")
        self.assertTrue(Session.objects.filter(pk=key).exists())
        self.assertEqual(self.consume(self.issue(user)), {"status": "accepted"})
        self.assertFalse(Session.objects.filter(pk=key).exists())
        self.assertTrue(Session.objects.filter(pk=unrelated).exists())
        user.refresh_from_db()
        info = services.user_summary(user, detail=True)
        self.assertEqual(info["email_masked"], "a***@e***")
        self.assertNotIn(user.email, dumps(info))
        self.assertNotIn(key, dumps(info))

    def test_disable_revokes_and_never_allows_ticket_to_reenable(self):
        user = self.user("disable_target")
        raw, key = self.issue(user), self.session(user)
        response = self.run_mutation(services.set_user_active, user.pk, active=False, reason="offboarding")
        self.assertEqual(response["status"], "disabled")
        self.assertFalse(Session.objects.filter(pk=key).exists())
        self.denied("account_disabled", services.issue_ticket, user.pk)
        self.assertEqual(self.consume(raw)[1]["error"]["code"], "invalid_ticket")
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertNotIn("offboarding", dumps(list(AuditRecord.objects.values_list("payload", flat=True))))

    def test_expiry_is_terminal_and_scheduled_expiry_is_idempotent(self):
        with override_settings(OPS_ADMIN_SECRET_TTL_SECONDS=2):
            raw = self.issue(self.pending())
        with patch("apps.ops_admin.lifecycle.tickets.timezone.now", return_value=timezone.now() + timezone.timedelta(seconds=10)):
            self.assertEqual(services.maintain_lifecycle()["expired_tickets"], 1)
            self.assertEqual(services.maintain_lifecycle()["expired_tickets"], 0)
            self.assertEqual(self.consume(raw), self.consume("Z" * 43))
        self.assertEqual(AuditRecord.objects.filter(event_type="ops.secret.expired").count(), 1)
        self.assertIsNotNone(SecretTicket.objects.get().expired_at)

    def test_reserved_users_self_operations_and_dedicated_business_role(self):
        target = self.user("staff_emergency", is_staff=True, is_superuser=True)
        self.denied("forbidden", services.create_user, {"username": "forged"}, actor=target)
        self.denied("reserved_account", services.issue_ticket, target.pk)
        self.denied("self_disable", services.set_user_active, self.actor.pk, active=False, reason="self")
        self.denied("self_delete", services.delete_user, self.actor.pk, confirm=True, reason="self")
        scoped = self.user("scoped", "change_user", "reset_user_password", "add_user")
        regular = self.user("business", role="supplier")
        self.denied("forbidden", services.update_user, regular.pk, {"role": "expert", "confirm": True}, actor=scoped)
        self.denied("forbidden", services.issue_ticket, regular.pk, actor=scoped)
        self.denied("forbidden", services.create_user, {"username": "cannot_add_role", "role": "supplier"}, actor=scoped)
        self.denied("self_escalation", services.update_user, self.actor.pk, {"role": "expert", "confirm": True})
        self.denied("confirmation_required", services.update_user, regular.pk, {"role": "expert"})
        self.run_mutation(services.update_user, regular.pk, {"role": "expert", "confirm": True})
        self.assertEqual(AuditRecord.objects.get(event_type="ops.user.changed").payload,
                         {"previous_role": "supplier", "new_role": "expert"})

    def test_group_assignment_scope_confirmation_and_last_admin(self):
        mgr = self.user("manager", "change_user", "change_group")
        target = self.user("target")
        group = Group.objects.create(name="registrar")
        cap = self.permission("manage_module_registry")
        group.permissions.add(cap)
        self.denied("delegation_denied", services.update_user, target.pk, {"groups": [group.pk], "confirm": True}, actor=mgr)
        self.denied("confirmation_required", services.update_user, target.pk, {"groups": [group.pk]})
        self.denied("self_escalation", services.update_user, self.actor.pk, {"groups": [group.pk], "confirm": True})
        self.run_mutation(services.update_user, target.pk, {"groups": [group.pk], "confirm": True})
        self.actor.user_permissions.remove(cap)
        self.actor.groups.add(group)
        target.groups.remove(group)
        self.denied("last_active_admin", services.update_group, group.pk, permissions=[], confirm=True)
        self.assertEqual(set(group.permissions.all()), {cap})
        self.denied("last_active_admin", services.update_user, self.actor.pk, {"groups": [], "confirm": True})
        self.assertTrue(self.actor.groups.filter(pk=group.pk).exists())

    def test_invalid_permission_input_and_unmanaged_permission_preserved(self):
        group = Group.objects.create(name="mixed")
        native = Permission.objects.filter(content_type__app_label="auth", codename="delete_group").get()
        group.permissions.add(native)
        self.denied("permission_not_assignable", services.update_group, group.pk, permissions=["auth.delete_group"], confirm=True)
        self.denied("group_not_assignable", services.update_user, self.pending().pk, {"groups": [group.pk], "confirm": True})
        response = self.run_mutation(services.update_group, group.pk, permissions=["ops_admin.view_user"], confirm=True)
        self.assertTrue(group.permissions.filter(pk=native.pk).exists())
        self.assertEqual(response["permissions"], ["ops_admin.view_user"])

    def test_delete_only_unestablished_managed_user_others_are_disabled(self):
        user = self.pending()
        self.issue(user)
        self.denied("confirmation_required", services.delete_user, user.pk, reason="typo", confirm=False)
        self.assertEqual(self.run_mutation(services.delete_user, user.pk, reason="typo", confirm=True), {"status": "deleted"})
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        legacy = self.user("legacy_account")
        key = self.session(legacy)
        status, result = self.run_mutation(services.delete_user, legacy.pk, reason="no longer needed", confirm=True)
        self.assertEqual((status, result["error"]["code"]), (409, "account_not_deletable"))
        self.assertFalse(User.objects.get(pk=legacy.pk).is_active)
        self.assertFalse(Session.objects.filter(pk=key).exists())

    def test_direct_business_relations_prevent_physical_delete_of_pending_account(self):
        supplier = self.user("supplier", role="supplier")
        task = Task.objects.create(supplier=supplier)
        pending = self.pending()
        grant = TaskAssignment.objects.create(task=task, user=pending, role="operator")
        status, result = self.run_mutation(services.delete_user, pending.pk, reason="typo", confirm=True)
        self.assertEqual((status, result["error"]["code"]), (409, "account_not_deletable"))
        self.assertEqual(User.objects.get(pk=pending.pk).ops_lifecycle.status, "disabled")
        self.assertTrue(TaskAssignment.objects.filter(pk=grant.pk).exists())

    def test_native_unusable_account_is_not_a_viable_last_admin(self):
        cap = self.permission("manage_module_registry")
        unusable = User(username="unusable_native", is_active=True)
        unusable.set_unusable_password()
        unusable.save()
        unusable.user_permissions.add(cap)
        group = Group.objects.create(name="only_viable_registrar")
        group.permissions.add(cap)
        self.actor.groups.add(group)
        self.actor.user_permissions.remove(cap)
        self.denied("last_active_admin", services.update_group, group.pk, permissions=[], confirm=True)

    def test_failed_critical_audit_rolls_back_consume_and_session_revoke(self):
        user = self.user("reset_failure")
        key, old_hash = self.session(user), user.password
        raw = self.issue(user)
        with patch("apps.ops_admin.lifecycle.tickets.audit", side_effect=DatabaseError("audit down")):
            with self.assertRaises(DatabaseError):
                self.consume(raw)
        user.refresh_from_db()
        self.assertEqual(user.password, old_hash)
        self.assertTrue(Session.objects.filter(pk=key).exists())
        self.assertIsNone(SecretTicket.objects.get().consumed_at)
        self.assertEqual(self.consume(raw), {"status": "accepted"})

    @override_settings(OPS_ADMIN_RATE_LIMITS={"consume": (900, 1, None, 2)})
    def test_rate_limits_unlinkable_subject_and_ip_and_do_not_trust_forwarded_ip(self):
        req = self.request(AnonymousUser(), REMOTE_ADDR="192.0.2.7", HTTP_X_FORWARDED_FOR="192.0.2.44")
        services.rate_limit(req, "consume", subject="subject-secret")
        with self.assertRaises(ApiError) as error:
            services.rate_limit(req, "consume", subject="subject-secret")
        self.assertEqual(error.exception.status, 429)
        req = self.request(AnonymousUser(), REMOTE_ADDR="192.0.2.7", HTTP_X_FORWARDED_FOR="192.0.2.45")
        with self.assertRaises(ApiError) as error:
            services.rate_limit(req, "consume", subject="another-secret")
        self.assertEqual(error.exception.status, 429)
        keys = list(LifecycleRateBucket.objects.values_list("key", flat=True))
        self.assertEqual(len(keys), 3)
        for key in keys:
            self.assertRegex(key, r"^[a-f0-9]{64}$")
            self.assertNotIn("192.0.2", key)

    def test_idempotent_replay_never_contains_offline_secret(self):
        user = self.pending()
        def call():
            request = self.request()
            request.session = SessionStore(session_key=self.session(self.actor))
            return mutate(request, action="secret.issue", target=user.pk, body={"delivery_mode": "offline"},
                          secret=True, work=lambda: services.issue_ticket(request, user.pk, delivery_mode="offline"))
        first, second = (loads(call().content) for _ in range(2))
        self.assertRegex(first["ticket"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(second, {"status": "already_processed", "request_id": first["request_id"]})
        self.assertNotIn(first["ticket"], dumps(MutationReceipt.objects.get().response))
        self.assertEqual(SecretTicket.objects.count(), 1)

    @override_settings(OPS_ADMIN_TRUSTED_EMAIL_ENABLED=True, OPS_ADMIN_SECRET_PUBLIC_URL="https://ops.example.test/ops/admin/secret/",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_only_after_commit_with_safe_link_and_no_raw_return(self):
        user = self.pending(email="trusted@example.test")
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            result = self.run_mutation(services.issue_ticket, user.pk, delivery_mode="trusted_email")
            self.assertNotIn("ticket", result)
            self.assertEqual(len(getattr(mail, "outbox", [])), 0)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        link, raw = msg.body.split("#ticket=")
        self.assertEqual(link, "https://ops.example.test/ops/admin/secret/")
        self.assertEqual(msg.to, [user.email])
        self.assertNotIn(raw, dumps(result))
        self.assertEqual(SecretTicket.objects.get().email_message_id, msg.extra_headers["Message-ID"])
        self.assertEqual(self.consume(raw), {"status": "accepted"})

    @override_settings(OPS_ADMIN_TRUSTED_EMAIL_ENABLED=True, OPS_ADMIN_SECRET_PUBLIC_URL="https://ops.example.test/ops/admin/secret/")
    def test_no_email_transport_on_failed_send_attempt_audit(self):
        user = self.pending(email="trusted@example.test")
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            result = self.run_mutation(services.issue_ticket, user.pk, delivery_mode="trusted_email")
        self.assertEqual(len(callbacks), 1)
        # Fault only the send-attempt audit; the issuance already succeeded.
        with patch("apps.ops_admin.lifecycle.tickets.audit", side_effect=DatabaseError("audit down")), \
                patch.object(tickets.EmailMessage, "send") as send, self.assertLogs("customs.ops.lifecycle", "ERROR"):
            callbacks[0]()
            send.assert_not_called()
        self.assertEqual(result, {"status": "accepted", "delivery_status": "unavailable"})

    @override_settings(OPS_ADMIN_TRUSTED_EMAIL_ENABLED=True, OPS_ADMIN_SECRET_PUBLIC_URL="https://ops.example.test/ops/admin/secret/",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_no_dispatch_on_rollback_and_offline_fallback_replaces_bearer(self):
        user = self.pending(email="trusted@example.test")
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.run_mutation(services.issue_ticket, user.pk, delivery_mode="trusted_email")
                raise DatabaseError("rollback before dispatch")
        self.assertEqual(callbacks, [])
        self.assertFalse(SecretTicket.objects.exists())
        attempted = []
        def fail_send(message, **_):
            attempted.append(message.body.split("#ticket=")[1])
            raise OSError("transport failed")
        with patch.object(tickets.EmailMessage, "send", autospec=True, side_effect=fail_send):
            with self.captureOnCommitCallbacks(execute=True):
                result = self.run_mutation(services.issue_ticket, user.pk, delivery_mode="trusted_email")
        self.assertEqual(result["delivery_mode"], "offline")
        self.assertNotEqual(result["ticket"], attempted[0])
        self.assertEqual(self.consume(attempted[0])[1]["error"]["code"], "invalid_ticket")
        self.assertEqual(self.consume(result["ticket"]), {"status": "accepted"})
        self.assertTrue(AuditRecord.objects.filter(event_type="ops.secret.delivery_failed", summary_code="send_failed").exists())


@skipUnless(connection.vendor == "postgresql", "Tests PostgreSQL row locks, not SQLite write locks")
class LifecycleConcurrencyTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase flushes the catalog's migration-seeded rows after each test.
        sync_catalog({})

    def user(self, name, *grants):
        user = User.objects.create_user(username=name, password="Seed_password:8972130")
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="ops_admin",
                                                            content_type__model="permissioncatalog", codename__in=grants))
        return user

    def session(self, user):
        store = SessionStore()
        store[SESSION_KEY] = str(user.pk)
        store[HASH_SESSION_KEY] = user.get_session_auth_hash()
        store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        store.save()
        return store.session_key

    def request(self, key, *, user=None, session_key=None):
        request = RequestFactory().post("/api/ops/admin/", HTTP_IDEMPOTENCY_KEY=key)
        request.user = user if user is not None else AnonymousUser()
        request.auth = user
        request.session = SessionStore(session_key=session_key)
        return request

    def parallel(self, *calls):
        start = Barrier(len(calls))

        def run(call):
            close_old_connections()
            try:
                start.wait(timeout=5)
                return call()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(run, call) for call in calls]
            return [future.result(timeout=12) for future in futures]

    def issue(self, actor, session_key, target, key):
        request = self.request(key, user=actor, session_key=session_key)
        return mutate(request, action="secret.issue", target=target.pk, secret=True, body={"delivery_mode": "offline"},
                      work=lambda: services.issue_ticket(request, target.pk, delivery_mode="offline"))

    def consume(self, ticket, key):
        request = self.request(key)
        password = "Concurrent_Reset:3590227"
        return mutate(request, action="secret.consume", secret=True, body={"ticket": ticket, "new_password": password},
                      work=lambda: services.consume_ticket(request, ticket=ticket, new_password=password))

    def test_concurrent_same_key_issue_and_consume(self):
        actor = self.user("issuer", "reset_user_password")
        session_key = self.session(actor)
        target = self.user("recipient")
        issue = lambda: self.issue(actor, session_key, target, "same-key-issue-0001")
        responses = self.parallel(issue, issue)
        self.assertEqual([response.status_code for response in responses], [200, 200])
        first, repeat = sorted((loads(response.content) for response in responses), key=lambda row: row["status"])
        self.assertEqual(repeat, {"status": "already_processed", "request_id": first["request_id"]})
        self.assertEqual(SecretTicket.objects.count(), 1)
        consume = lambda: self.consume(first["ticket"], "same-key-consume-0001")
        responses = self.parallel(consume, consume)
        self.assertEqual([response.status_code for response in responses], [200, 200])
        first, repeat = sorted((loads(response.content) for response in responses), key=lambda row: row["status"])
        self.assertEqual(repeat, {"status": "already_processed", "request_id": first["request_id"]})
        self.assertEqual(AuditRecord.objects.filter(event_type="ops.secret.consumed").count(), 1)

    def test_concurrent_distinct_keys_revoke_predecessor_and_consume_once(self):
        actor = self.user("issuer", "reset_user_password")
        session_key = self.session(actor)
        target = self.user("recipient")
        responses = self.parallel(*(lambda key=key: self.issue(actor, session_key, target, key)
                                    for key in ("distinct-issue-0001", "distinct-issue-0002")))
        self.assertEqual([response.status_code for response in responses], [200, 200])
        raw = [loads(response.content)["ticket"] for response in responses]
        self.assertNotEqual(raw[0], raw[1])
        self.assertEqual(SecretTicket.objects.filter(revoked_at__isnull=False).count(), 1)
        open_ticket = SecretTicket.objects.filter(revoked_at__isnull=True).get()
        valid = next(value for value in raw if digest("secret.ticket", value) == open_ticket.ticket_hash)
        responses = self.parallel(*(lambda key=key: self.consume(valid, key)
                                    for key in ("distinct-use-0001", "distinct-use-0002")))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 422])
        self.assertEqual(AuditRecord.objects.filter(event_type="ops.secret.consumed").count(), 1)

    def test_concurrent_group_edit_and_disable_cannot_remove_last_capability(self):
        # Both actors delegate a grant they hold at request start; one attempts to disable
        # the other, while the other attempts to remove the grant from the first's group.
        admin_a = self.user("group_admin", "disable_user")
        admin_b = self.user("direct_admin", "manage_module_registry", "change_group")
        group = Group.objects.create(name="registry_group")
        group.permissions.add(Permission.objects.get(content_type__app_label="ops_admin",
                                                     content_type__model="permissioncatalog", codename="manage_module_registry"))
        admin_a.groups.add(group)
        admin_a.user_permissions.add(Permission.objects.get(content_type__app_label="ops_admin",
                                                             content_type__model="permissioncatalog", codename="change_group"))
        a_session, b_session = self.session(admin_a), self.session(admin_b)

        def disable():
            request = self.request("disable-admin-b-0001", user=admin_a, session_key=a_session)
            return mutate(request, action="user.disable", target=admin_b.pk, body={"reason": "transfer"},
                          work=lambda: services.set_user_active(request, admin_b.pk, active=False, reason="transfer"))

        def change_group():
            request = self.request("remove-admin-a-0001", user=admin_b, session_key=b_session)
            return mutate(request, action="group.update", target=group.pk, body={"permissions": [], "confirm": True},
                          work=lambda: services.update_group(request, group.pk, permissions=[], confirm=True))

        def attempt(call):
            try:
                result = call()
            except ApiError as exc:
                return exc.status, exc.code
            return result.status_code, loads(result.content).get("error", {}).get("code")

        statuses = self.parallel(lambda: attempt(disable), lambda: attempt(change_group))
        self.assertIn((200, None), statuses)
        # A losing actor either becomes unauthenticated after disable/revoke, or loses
        # permission to manage the other when their own registry grant is removed.
        self.assertTrue(set(statuses) & {(401, "unauthorized"), (403, "delegation_denied"), (409, "last_active_admin")})
        active_registry_admins = User.objects.filter(is_active=True).filter(
            Q(user_permissions__content_type__model="permissioncatalog", user_permissions__codename="manage_module_registry")
            | Q(groups__permissions__content_type__model="permissioncatalog", groups__permissions__codename="manage_module_registry")
        ).distinct()
        self.assertEqual(active_registry_admins.count(), 1)
