"""分析执行：预审（precheck）与专家核查（expert_check）各是独立执行记录。

执行状态属于技术维度（queued/running/succeeded/failed），业务结论属于比较结论
与问题记录（D4、B09）；两者不合并成单一状态字段。
"""
import uuid

from django.conf import settings
from django.db import models


class Run(models.Model):
    class Kind(models.TextChoices):
        PRECHECK = "precheck", "业务员预审"
        EXPERT_CHECK = "expert_check", "专家核查"

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        SUCCEEDED = "succeeded", "执行成功"
        FAILED = "failed", "执行失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey("reviews.Task", on_delete=models.CASCADE, related_name="runs", verbose_name="审核任务")
    version = models.ForeignKey(
        "reviews.FileVersion", on_delete=models.PROTECT, related_name="runs", verbose_name="文件版本",
    )
    submission = models.ForeignKey(
        "reviews.Submission", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs", verbose_name="关联提交快照",
        help_text="专家核查绑定业务员提交快照；预审为空。",
    )
    kind = models.CharField("执行类型", max_length=16, choices=Kind.choices)
    status = models.CharField("执行状态", max_length=16, choices=Status.choices, default=Status.QUEUED)
    is_current = models.BooleanField(
        "当前有效执行", default=False,
        help_text="由 P06/P13 维护；旧执行保留为历史，不参与当前卡片结果。",
    )
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="triggered_runs", verbose_name="触发人",
        help_text="自动触发为空，手动重新预审/重试记录操作人。",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        verbose_name = "分析执行"
        verbose_name_plural = "分析执行"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["task", "kind", "is_current"], name="idx_run_task_kind_current"),
        ]


class RunItem(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        SUCCEEDED = "succeeded", "执行成功"
        FAILED = "failed", "执行失败"

    class ComparisonStatus(models.TextChoices):
        RELATED_ANOMALY = "related_anomaly", "关联异常"
        INVALID_ORIGINAL = "invalid_original", "原码异常"
        INSUFFICIENT_EVIDENCE = "insufficient_evidence", "证据不足"
        CONSISTENT = "consistent", "一致"
        INCONSISTENT = "inconsistent", "不一致"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="items", verbose_name="分析执行")
    item = models.ForeignKey(
        "reviews.Item", on_delete=models.PROTECT, related_name="run_items", verbose_name="商品行",
    )
    status = models.CharField("行状态", max_length=16, choices=Status.choices, default=Status.QUEUED)
    suggested_hs = models.CharField(
        "建议 HS", max_length=32, null=True, blank=True,
        help_text="未生成建议为 null；与供应商原 HS、业务员确认值分开保存。",
    )
    comparison_status = models.CharField(
        "比较结论", max_length=32, choices=ComparisonStatus.choices, blank=True, default="",
        help_text="空表示尚未得出结论；取值口径见 D4，优先级由 P02/P16 实现。",
    )
    confidence = models.FloatField("置信度", null=True, blank=True)
    result = models.JSONField("完整结果", default=dict, blank=True, help_text="候选、原因、证据与查询状态。")
    error_message = models.TextField("技术失败原因", blank=True, default="")
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "执行行结果"
        verbose_name_plural = "执行行结果"
        ordering = ["run_id", "item_id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "item"], name="uniq_run_item"),
        ]
