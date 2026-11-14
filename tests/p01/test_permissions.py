"""P01 对象级权限：可见范围、角色动作矩阵、越权与改 ID 拒绝。"""
import uuid

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import override_settings

from apps.accounts import permissions
from apps.accounts.errors import AuthenticationRequired
from apps.accounts.models import Role
from apps.notifications.models import Message
from apps.reviews.models import SubmissionItem

from .factories import assign, make_user
from .permission_fixture import PermissionFixture


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class VisibilityTests(PermissionFixture):
    def test_anonymous_is_rejected_with_401_semantics(self):
        anonymous = type("Anonymous", (), {"is_authenticated": False, "is_active": False})()
        with self.assertRaises(AuthenticationRequired):
            permissions.visible_tasks(anonymous)
        with self.assertRaises(AuthenticationRequired):
            permissions.require_action(anonymous, permissions.READ, self.task_a)

    def test_supplier_sees_only_own_tasks(self):
        self.assertEqual(list(permissions.visible_tasks(self.supplier_a)), [self.task_a])
        self.assertEqual(permissions.get_visible_task(self.supplier_a, self.task_a.pk), self.task_a)

    def test_operator_and_expert_see_assigned_tasks_only(self):
        self.assertEqual(list(permissions.visible_tasks(self.operator_a)), [self.task_a])
        self.assertEqual(list(permissions.visible_tasks(self.expert)), [self.task_a])
        self.assertFalse(permissions.visible_tasks(self.operator_b).filter(pk=self.task_a.pk).exists())

    def test_role_mismatch_assignment_grants_nothing(self):
        # 角色为业务员却只被授予专家角色：不进入可见范围，也不能按专家动作。
        user = make_user("operator_as_expert", Role.OPERATOR)
        assign(self.task_a, user, Role.EXPERT)
        self.assertFalse(permissions.visible_tasks(user).filter(pk=self.task_a.pk).exists())
        with self.assertRaises(Http404):
            permissions.require_action(user, permissions.READ, self.task_a)

    def test_account_without_role_sees_nothing(self):
        staff = make_user("local_admin", "")
        self.assertFalse(permissions.visible_tasks(staff).exists())
        with self.assertRaises(Http404):
            permissions.get_visible_task(staff, self.task_a.pk)

    def test_cross_tenant_reads_are_404(self):
        with self.assertRaises(Http404):
            permissions.get_visible_task(self.supplier_a, self.task_b.pk)
        for obj in (self.version_b, self.item_b, self.run_b):
            with self.assertRaises(Http404):
                permissions.require_action(self.supplier_a, permissions.READ, obj)

    def test_direct_id_tampering_is_404_not_500(self):
        with self.assertRaises(Http404):
            permissions.get_visible_task(self.supplier_a, uuid.uuid4())
        with self.assertRaises(Http404):
            permissions.get_visible_task(self.supplier_a, "not-a-uuid")
        with self.assertRaises(Http404):
            permissions.require_action(
                self.supplier_b, permissions.READ, self.submission_a,
            )

    def test_all_business_objects_resolve_through_task(self):
        for obj in (self.version_a, self.item_a, self.image_a, self.run_a, self.run_item_a,
                    self.confirmation_a, self.submission_a, self.decision_a):
            with self.subTest(model=obj._meta.object_name):
                self.assertEqual(permissions.task_of(obj), self.task_a)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ActionMatrixTests(PermissionFixture):
    def test_supplier_can_upload_to_own_task_only(self):
        permissions.require_action(self.supplier_a, permissions.UPLOAD_VERSION, self.task_a)
        with self.assertRaises(Http404):
            permissions.require_action(self.supplier_a, permissions.UPLOAD_VERSION, self.task_b)

    def test_supplier_cannot_confirm_or_decide(self):
        for action in (permissions.CONFIRM, permissions.SUBMIT, permissions.DECIDE):
            with self.assertRaises(PermissionDenied):
                permissions.require_action(self.supplier_a, action, self.task_a)

    def test_operator_can_confirm_and_submit(self):
        permissions.require_action(self.operator_a, permissions.CONFIRM, self.item_a)
        permissions.require_action(self.operator_a, permissions.SUBMIT, self.submission_a)

    def test_operator_cannot_approve(self):
        with self.assertRaises(PermissionDenied):
            permissions.require_action(self.operator_a, permissions.DECIDE, self.submission_a)

    def test_expert_can_decide_but_not_confirm(self):
        permissions.require_action(self.expert, permissions.DECIDE, self.submission_a)
        with self.assertRaises(PermissionDenied):
            permissions.require_action(self.expert, permissions.CONFIRM, self.item_a)

    def test_read_is_allowed_for_every_visible_role(self):
        for user in (self.supplier_a, self.operator_a, self.expert):
            with self.subTest(role=user.role):
                self.assertEqual(permissions.require_action(user, permissions.READ, self.task_a), self.task_a)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MessagePermissionTests(PermissionFixture):
    def test_own_message_is_visible(self):
        message = Message.objects.create(recipient=self.operator_a, title="待确认")
        self.assertEqual(permissions.get_visible_message(self.operator_a, message.pk), message)

    def test_other_users_message_is_404(self):
        message = Message.objects.create(recipient=self.operator_a, title="待确认")
        with self.assertRaises(Http404):
            permissions.get_visible_message(self.operator_b, message.pk)
