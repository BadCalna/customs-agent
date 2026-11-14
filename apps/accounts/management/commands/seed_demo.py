"""幂等建立三角色测试账号与显式归属的演示任务（D6 ``make seed-demo``）。

约定：
- 只补缺失数据，不重置已有业务资料（任务阶段、商品、提交、决定都不动）；
- 密码只在新建账号时设置；确需重置演示账号密码时显式加 ``--reset-passwords``；
- 演示账号密码仅用于本地开发，不是生产凭据，不要写入真实试用环境。
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Role, TaskAssignment, User
from apps.reviews.models import Task

DEMO_PASSWORD = "customs-demo-only"

ACCOUNTS = [
    # username, display_name, role
    ("supplier_a", "供应商甲", Role.SUPPLIER),
    ("supplier_b", "供应商乙", Role.SUPPLIER),
    ("operator_a", "业务员甲", Role.OPERATOR),
    ("operator_b", "业务员乙", Role.OPERATOR),
    ("expert", "专家", Role.EXPERT),
]

DEMO_TASKS = [
    # title, supplier username, 需要显式授权的 (username, role)
    ("演示任务-供应商甲", "supplier_a", [("operator_a", Role.OPERATOR), ("expert", Role.EXPERT)]),
    ("演示任务-供应商乙", "supplier_b", [("operator_b", Role.OPERATOR), ("expert", Role.EXPERT)]),
]


class Command(BaseCommand):
    help = "幂等建立三角色测试账号与显式归属的演示任务；不重置已有业务资料。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="重置已存在演示账号的密码（默认不动）。",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        reset_passwords = options["reset_passwords"]
        users = {}
        created_accounts = []
        reset_accounts = []

        for username, display_name, role in ACCOUNTS:
            user = User.objects.filter(username=username).first()
            if user is None:
                user = User(
                    username=username,
                    display_name=display_name,
                    role=role,
                    first_name=display_name,
                )
                user.set_password(DEMO_PASSWORD)
                user.save()
                created_accounts.append(username)
            elif reset_passwords:
                user.set_password(DEMO_PASSWORD)
                user.save(update_fields=["password"])
                reset_accounts.append(username)
            users[username] = user

        created_tasks = []
        for title, supplier_username, assignments in DEMO_TASKS:
            supplier = users[supplier_username]
            task = Task.objects.filter(supplier=supplier, title=title).first()
            if task is None:
                task = Task.objects.create(supplier=supplier, title=title)
                created_tasks.append(title)
            for username, role in assignments:
                TaskAssignment.objects.get_or_create(
                    task=task, user=users[username], defaults={"role": role},
                )

        self.stdout.write(f"演示账号：新建 {len(created_accounts)} 个" + (
            f"（{', '.join(created_accounts)}）" if created_accounts else ""
        ))
        if reset_accounts:
            self.stdout.write(f"已重置密码：{', '.join(reset_accounts)}")
        self.stdout.write(f"演示任务：新建 {len(created_tasks)} 个" + (
            f"（{', '.join(created_tasks)}）" if created_tasks else ""
        ))
        if created_accounts:
            self.stdout.write(f"新建账号的统一密码：{DEMO_PASSWORD}（仅本地开发使用）")
        self.stdout.write(self.style.SUCCESS("seed-demo 完成；重复执行不会产生重复账号、任务或授权。"))
