"""账号、三角色与任务显式授权。

角色只有三类业务角色（D1 R10）：供应商、业务员、专家。本地管理员等非业务账号
把 role 留空，不参与业务接口的可见范围和动作矩阵。
"""
import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    SUPPLIER = "supplier", "供应商"
    OPERATOR = "operator", "业务员"
    EXPERT = "expert", "专家"


class User(AbstractUser):
    """业务账号。ID 为不透明 UUID（D3 第 1 节）。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(
        "业务角色", max_length=16, choices=Role.choices, blank=True, default="",
        help_text="留空表示非业务账号（如本地管理员），不进入业务接口可见范围。",
    )
    display_name = models.CharField("显示名", max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "账号"
        verbose_name_plural = "账号"

    def __str__(self):
        return self.display_name or self.username

    @property
    def role_label(self):
        return Role(self.role).label if self.role else ""


class TaskAssignment(models.Model):
    """任务显式授权：业务员/专家能看到哪些任务，由本表决定。

    首期真实业务员与任务的归属口径待 Jay 确认（D3 第 5 节）；开发样例必须显式
    授权，不能因为角色是业务员就放开全部任务。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        "reviews.Task", on_delete=models.CASCADE, related_name="assignments",
        verbose_name="审核任务",
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="task_assignments",
        verbose_name="被授权账号",
    )
    role = models.CharField("授权角色", max_length=16, choices=Role.choices)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "任务授权"
        verbose_name_plural = "任务授权"
        ordering = ["task_id", "user_id"]
        constraints = [
            models.UniqueConstraint(fields=["task", "user"], name="uniq_task_assignment_user"),
            models.CheckConstraint(
                condition=models.Q(role__in=[Role.OPERATOR, Role.EXPERT]),
                name="task_assignment_business_role",
            ),
        ]

    def __str__(self):
        return f"{self.task_id} → {self.user_id}（{self.role}）"
