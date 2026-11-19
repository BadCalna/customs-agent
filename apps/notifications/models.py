"""站内消息：只属于收件人本人（R10、D3）。

消息读取按 recipient 过滤；他人消息不可访问。钉钉 DWS 外发由 P19 实现，
与站内消息记录互不替代。
"""
import uuid

from django.conf import settings
from django.db import models


class Message(models.Model):
    fixed_input_fields = ("recipient", "task", "version", "submission", "decision", "event_key", "kind", "title", "body")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages", verbose_name="收件人",
    )
    task = models.ForeignKey(
        "reviews.Task", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="messages", verbose_name="关联任务",
    )
    kind = models.CharField("消息类型", max_length=32, blank=True, default="")
    version = models.ForeignKey("reviews.FileVersion", null=True, blank=True, on_delete=models.SET_NULL)
    submission = models.ForeignKey("reviews.Submission", null=True, blank=True, on_delete=models.SET_NULL)
    decision = models.ForeignKey("reviews.Decision", null=True, blank=True, on_delete=models.SET_NULL)
    event_key = models.CharField(max_length=160, blank=True, default="")
    title = models.CharField("标题", max_length=200)
    body = models.TextField("正文", blank=True, default="")
    is_read = models.BooleanField("已读", default=False)
    read_at = models.DateTimeField("已读时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "站内消息"
        verbose_name_plural = "站内消息"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read"], name="idx_message_recipient_read"),
        ]
        constraints = [models.UniqueConstraint(fields=["recipient", "event_key"],
            condition=~models.Q(event_key=""), name="uniq_message_recipient_event")]

    def __str__(self):
        return self.title

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.version_id and self.version.task_id != self.task_id:
            raise ValidationError("消息版本必须属于同一任务")
        if self.submission_id and (self.submission.task_id != self.task_id or self.submission.version_id != self.version_id):
            raise ValidationError("消息提交必须属于指定任务和版本")
        if self.decision_id and self.decision.submission_id != self.submission_id:
            raise ValidationError("消息决定必须属于指定提交")

    def save(self, *args, **kwargs):
        # API只可更新已读；普通保存也拒绝重绑历史目标。保留SET_NULL父级清理语义。
        from django.core.exceptions import ValidationError
        fields = [self._meta.get_field(name).attname for name in self.fixed_input_fields]
        stored = type(self).objects.filter(pk=self.pk).values_list(*fields).first() if self.pk else None
        if stored is not None and tuple(getattr(self, name) for name in fields) != stored:
            raise ValidationError("消息事件、收件人和目标已经固定")
        self.clean()
        return super().save(*args, **kwargs)


class NotificationDelivery(models.Model):
    """持久 mock DWS outbox；mocked 与真实送达严格分开。"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.OneToOneField(Message, on_delete=models.CASCADE, related_name="delivery")
    status = models.CharField(max_length=16, default="pending", choices=[
        (s, s) for s in ("pending", "processing", "mocked", "exhausted", "suppressed")])
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    receipt = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="idx_notify_due")]
        constraints = [models.CheckConstraint(condition=models.Q(attempts__lte=3), name="notify_attempt_limit")]


class DeliveryAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(NotificationDelivery, on_delete=models.CASCADE, related_name="history")
    number = models.PositiveSmallIntegerField()
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, default="started")
    error_code = models.CharField(max_length=64, blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["delivery", "number"], name="uniq_notify_attempt")]
