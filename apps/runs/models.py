"""分析执行：预审（precheck）与专家核查（expert_check）各是独立执行记录。

执行状态属于技术维度（queued/running/succeeded/failed），业务结论属于比较结论
与问题记录（D4、B09）；两者不合并成单一状态字段。
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.model_guards import FixedInputModel, protect_from_direct_delete


def _related_or_none(obj, name):
    """安全读取外键关联；关联未设置或指向不存在对象时返回 None。"""
    if getattr(obj, f"{name}_id", None) is None:
        return None
    try:
        return getattr(obj, name)
    except models.ObjectDoesNotExist:
        return None


class Run(FixedInputModel):
    fixed_input_fields = ("task", "version", "kind", "submission", "parent_run")

    class Kind(models.TextChoices):
        PRECHECK = "precheck", "业务员预审"
        EXPERT_CHECK = "expert_check", "专家核查"

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        SUCCEEDED = "succeeded", "执行成功"
        FAILED = "failed", "执行失败"
        SUPERSEDED = "superseded", "已作废"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey("reviews.Task", on_delete=models.CASCADE, related_name="runs", verbose_name="审核任务")
    version = models.ForeignKey(
        "reviews.FileVersion", on_delete=models.PROTECT, related_name="runs", verbose_name="文件版本",
    )
    submission = models.ForeignKey(
        "reviews.Submission", on_delete=models.PROTECT, null=True, blank=True,
        related_name="runs", verbose_name="关联提交快照",
        help_text="专家核查绑定业务员提交快照；预审为空。",
    )
    kind = models.CharField("执行类型", max_length=16, choices=Kind.choices)
    status = models.CharField("执行状态", max_length=16, choices=Status.choices, default=Status.QUEUED)
    is_current = models.BooleanField(
        "当前有效执行", default=False,
        help_text="按文件版本与执行类型各保留一轮当前执行；同版本重跑转为历史，旧版本执行仍可回查。",
    )
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="triggered_runs", verbose_name="触发人",
        help_text="自动触发为空，手动重新预审/重试记录操作人。",
    )
    parent_run = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="retry_runs", verbose_name="重试来源执行")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta(FixedInputModel.Meta):
        verbose_name = "分析执行"
        verbose_name_plural = "分析执行"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["version", "kind", "is_current"], name="idx_run_version_kind_current"),
        ]

    def clean(self):
        parent = _related_or_none(self, "parent_run")
        if parent and (parent.task_id != self.task_id or parent.version_id != self.version_id
                       or parent.kind != self.kind or parent.submission_id != self.submission_id):
            raise ValidationError("重试来源必须属于同任务、同版本、同类型和同提交。")
        version = _related_or_none(self, "version")
        if version is not None and self.task_id and version.task_id != self.task_id:
            raise ValidationError("分析执行的文件版本必须属于该任务，不能跨任务引用。")
        submission = _related_or_none(self, "submission")
        # 执行类型与提交的绑定关系是强制的：专家核查必须能回答“核查的是哪次提交”，
        # 普通预审则不允许绑提交，避免把两种执行混成一种语义。
        if self.kind == self.Kind.EXPERT_CHECK and submission is None:
            raise ValidationError("专家核查必须绑定业务员的提交快照，不能凭空核查。")
        if self.kind == self.Kind.PRECHECK and submission is not None:
            raise ValidationError("普通预审不能绑定提交快照；专家核查才绑定提交。")
        if submission is not None:
            if submission.task_id != self.task_id or submission.version_id != self.version_id:
                raise ValidationError("专家核查必须绑定同任务、同文件版本的提交快照。")


class RunItem(FixedInputModel):
    fixed_input_fields = ("run", "item")

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        SUCCEEDED = "succeeded", "执行成功"
        FAILED = "failed", "执行失败"

    class ComparisonStatus(models.TextChoices):
        # 取值与优先级严格对齐 D4 第 2 节：关联异常 → 原码缺失/异常 → 待确认 → 一致/不一致。
        # 原码未填（supplier_missing）与原码异常（supplier_invalid）必须分开，不能合并。
        ASSOCIATION_ERROR = "association_error", "关联异常"
        SUPPLIER_MISSING = "supplier_missing", "原码缺失"
        SUPPLIER_INVALID = "supplier_invalid", "原码异常"
        NEEDS_CONFIRMATION = "needs_confirmation", "待确认"
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

    class Meta(FixedInputModel.Meta):
        verbose_name = "执行行结果"
        verbose_name_plural = "执行行结果"
        ordering = ["run_id", "item_id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "item"], name="uniq_run_item"),
        ]

    def clean(self):
        run = _related_or_none(self, "run")
        item = _related_or_none(self, "item")
        if run is None or item is None:
            return
        if item.version_id != run.version_id:
            raise ValidationError("执行行的商品必须属于该执行的文件版本，不能把旧版本商品挂到新执行上。")
        if run.submission_id and not run.submission.items.filter(item_id=item.pk).exists():
            raise ValidationError("专家核查的执行行商品必须属于所引用的提交快照。")


# 执行记录是历史：结果行禁止单独删除，执行本身也禁止单独删除；
# 随任务级联清理仍允许，避免整库/整任务清理完全不可用。
for _model in (Run, RunItem):
    protect_from_direct_delete(_model)


class RunWork(models.Model):
    """事务内持久待运行凭据。Redis 只负责通知，数据库租约与 token 决定写入权。"""

    run = models.OneToOneField(Run, on_delete=models.CASCADE, primary_key=True, related_name="work")
    state = models.CharField(max_length=16, default="pending")
    token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    import_attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "available_at"], name="idx_work_state_due")]


class RunAttempt(FixedInputModel):
    """每次引擎调用或导入解析尝试；失败、中断与过期输出分别保留。"""

    fixed_input_fields = ("run", "row", "number", "token")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, null=True, blank=True, related_name="attempts")
    row = models.ForeignKey(RunItem, on_delete=models.CASCADE, null=True, blank=True, related_name="attempt_records")
    number = models.PositiveIntegerField()
    token = models.UUIDField()
    status = models.CharField(max_length=16, default="running")
    input_hash = models.CharField(max_length=64, blank=True)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta(FixedInputModel.Meta):
        ordering = ["row_id", "number"]
        constraints = [models.UniqueConstraint(fields=["row", "number"], name="uniq_row_attempt_number")]

    def clean(self):
        if self.run_id is None and self.row_id is None:
            raise ValidationError("执行尝试必须绑定 Run 或商品行")
        if self.row_id is not None:
            row = _related_or_none(self, "row")
            if row is not None and self.run_id is not None and row.run_id != self.run_id:
                raise ValidationError("执行尝试的 Run 必须与商品行所属 Run 一致")


protect_from_direct_delete(RunAttempt)
