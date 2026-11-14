"""P01 测试账号初始化：幂等、角色与显式归属、不重置已有业务资料。"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.accounts.management.commands.seed_demo import ACCOUNTS, DEMO_PASSWORD, DEMO_TASKS
from apps.accounts.models import Role, TaskAssignment, User
from apps.reviews.models import Task


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SeedDemoTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("seed_demo", *args, stdout=out)
        return out.getvalue()

    def test_seed_creates_roles_and_explicit_assignments(self):
        self._run()
        self.assertEqual(User.objects.count(), len(ACCOUNTS))
        self.assertEqual(Task.objects.count(), len(DEMO_TASKS))
        for username, display_name, role in ACCOUNTS:
            user = User.objects.get(username=username)
            self.assertEqual(user.role, role)
            self.assertEqual(user.display_name, display_name)
            self.assertTrue(user.check_password(DEMO_PASSWORD))
        assigned = {
            (a.task.title, a.user.username, a.role) for a in TaskAssignment.objects.select_related("task", "user")
        }
        self.assertEqual(assigned, {
            ("演示任务-供应商甲", "operator_a", Role.OPERATOR),
            ("演示任务-供应商甲", "expert", Role.EXPERT),
            ("演示任务-供应商乙", "operator_b", Role.OPERATOR),
            ("演示任务-供应商乙", "expert", Role.EXPERT),
        })

    def test_second_run_is_idempotent(self):
        self._run()
        counts = (User.objects.count(), Task.objects.count(), TaskAssignment.objects.count())
        output = self._run()
        self.assertEqual(
            (User.objects.count(), Task.objects.count(), TaskAssignment.objects.count()), counts,
        )
        self.assertIn("新建 0 个", output)

    def test_existing_business_data_is_not_reset(self):
        self._run()
        task = Task.objects.get(title="演示任务-供应商甲")
        task.stage = Task.Stage.SUBMITTED
        task.title = "演示任务-供应商甲"
        task.save(update_fields=["stage"])
        self._run()
        task.refresh_from_db()
        self.assertEqual(task.stage, Task.Stage.SUBMITTED)
        self.assertEqual(Task.objects.filter(title="演示任务-供应商甲").count(), 1)

    def test_password_is_kept_unless_reset_requested(self):
        self._run()
        user = User.objects.get(username="supplier_a")
        user.set_password("changed-by-dev")
        user.save()
        self._run()
        user.refresh_from_db()
        self.assertTrue(user.check_password("changed-by-dev"))
        self._run("--reset-passwords")
        user.refresh_from_db()
        self.assertTrue(user.check_password(DEMO_PASSWORD))
