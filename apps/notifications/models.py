"""站内消息：只属于收件人本人（R10、D3）。

消息读取按 recipient 过滤；他人消息不可访问。钉钉 DWS 外发由 P19 实现，
与站内消息记录互不替代。
"""
import uuid

from django.conf import settings
from django.db import models


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages", verbose_name="收件人",
    )
    task = models.ForeignKey(
        "reviews.Task", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="messages", verbose_name="关联任务",
    )
    kind = models.CharField("消息类型", max_length=32, blank=True, default="")
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

    def __str__(self):
        return self.title
