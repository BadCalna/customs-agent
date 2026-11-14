"""审核域最小模型：任务、文件版本、商品行、附图、确认、提交、决定、问题与审计。

分层原则（D2、D3、B09）：
- 供应商原值只写一次，任何阶段结果不覆盖它；
- 预审建议在 ``runs.RunItem``，业务员确认追加到 ``Confirmation``，提交值冗余进
  ``SubmissionItem`` 快照，专家终值在 ``DecisionItem``；
- 业务阶段（``Task.stage``）、执行状态（``Run.status``）、比较结论、图片状态、
  问题状态各自独立，不用一个字段混合表达。
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.model_guards import FixedInputModel, protect_from_direct_delete


def _related_or_none(obj, name):
    """安全读取外键关联；关联未设置或指向不存在对象时返回 None。"""
    if getattr(obj, f"{name}_id", None) is None:
        return None
    try:
        return getattr(obj, name)
    except models.ObjectDoesNotExist:
        return None


class Task(models.Model):
    """一个上传的 Excel 文件对应一个审核任务（R01）。"""

    class Stage(models.TextChoices):
        UPLOADED = "uploaded", "已上传"
        PRECHECKING = "prechecking", "预审中"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "待业务员确认"
        SUBMITTED = "submitted", "已提交专家"
        APPROVED = "approved", "已通过"
        RETURNED = "returned", "已退回"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField("任务名称", max_length=200, blank=True, default="")
    supplier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="supplied_tasks",
        verbose_name="供应商",
    )
    stage = models.CharField("业务阶段", max_length=24, choices=Stage.choices, default=Stage.UPLOADED)
    revision = models.PositiveIntegerField(
        "任务修订", default=1,
        help_text="业务写动作在事务中递增；提交与审核决定携带它做冲突校验（D3 第 1 节）。",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "审核任务"
        verbose_name_plural = "审核任务"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or str(self.pk)


class FileVersion(models.Model):
    """同一任务下的一次上传，V1/V2 区分补料版本（R07、B08）。

    原文件不可变：不在本模型提供更新路径，重传只会创建新的版本行。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="versions", verbose_name="审核任务")
    number = models.PositiveIntegerField("版本号", help_text="V1 为 1；同任务内唯一。")
    original_filename = models.CharField("原文件名", max_length=255)
    storage_path = models.CharField(
        "存储路径", max_length=500, blank=True, default="",
        help_text="受控文件存储由 P05 接入；不保存服务器绝对路径到对外接口。",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_versions",
        verbose_name="上传人",
    )
    uploaded_at = models.DateTimeField("上传时间", auto_now_add=True)

    class Meta:
        verbose_name = "文件版本"
        verbose_name_plural = "文件版本"
        ordering = ["task_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["task", "number"], name="uniq_file_version_number"),
        ]

    def __str__(self):
        return f"{self.task_id} V{self.number}"


class Item(models.Model):
    """商品行：按申报要素页签的一行建立（R05），同时保存与原 Excel 的位置关系。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(FileVersion, on_delete=models.CASCADE, related_name="items", verbose_name="文件版本")
    source_sheet = models.CharField("来源页签", max_length=128)
    source_row = models.PositiveIntegerField("Excel 行号", help_text="1 起始，对应申报要素页签。")
    source_cells = models.CharField("单元格范围", max_length=64, blank=True, default="")
    name = models.CharField("品名", max_length=255, blank=True, default="")
    attributes = models.JSONField("归类事实", default=dict, blank=True)
    supplier_hs_raw = models.CharField(
        "供应商原 HS", max_length=32, blank=True, default="",
        help_text="原文保留，空白即空白；任何阶段结果不得写入本字段。",
    )
    supplier_elements_raw = models.TextField("供应商申报要素原文", blank=True, default="")
    supplier_tax_rate_raw = models.CharField("供应商退税率原文", max_length=32, blank=True, default="")
    revision = models.PositiveIntegerField("商品修订", default=1, help_text="业务员每次确认修改递增；与 Confirmation.revision 同步。")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "商品行"
        verbose_name_plural = "商品行"
        ordering = ["version_id", "source_row"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "source_sheet", "source_row"], name="uniq_item_source_row",
            ),
        ]

    def __str__(self):
        return self.name or f"{self.version_id}#{self.source_row}"


class ItemMember(models.Model):
    """装箱明细成员行：同一申报条目关联多条装箱记录时保存成员关系（R05、D2）。"""

    class RelationType(models.TextChoices):
        PACKING = "packing", "装箱成员"
        DECLARATION = "declaration", "申报成员"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="members", verbose_name="商品行")
    relation_type = models.CharField(
        "关联类型", max_length=16, choices=RelationType.choices, default=RelationType.PACKING,
    )
    source_sheet = models.CharField("来源页签", max_length=128)
    source_row = models.PositiveIntegerField("Excel 行号")
    contract_no = models.CharField("合同号", max_length=64, blank=True, default="")
    item_no = models.CharField("合同项号", max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "商品成员行"
        verbose_name_plural = "商品成员行"
        ordering = ["item_id", "source_row"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "relation_type", "source_sheet", "source_row"],
                name="uniq_item_member_source_row",
            ),
        ]


class ItemImage(models.Model):
    """申报要素行的附图（DISPIMG）。读取失败必须与无图片分开（R11、D3）。"""

    class Status(models.TextChoices):
        AVAILABLE = "available", "已提取"
        ABSENT = "absent", "无图片"
        READ_FAILED = "read_failed", "读取失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="images", verbose_name="商品行")
    source_sheet = models.CharField("来源页签", max_length=128)
    source_cell = models.CharField("来源单元格", max_length=64)
    dispimg_id = models.CharField("DISPIMG 资源标识", max_length=128, blank=True, default="")
    storage_path = models.CharField("存储路径", max_length=500, blank=True, default="")
    status = models.CharField("提取状态", max_length=16, choices=Status.choices, default=Status.ABSENT)
    error_message = models.TextField("失败原因", blank=True, default="")
    extracted_at = models.DateTimeField("提取时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "商品附图"
        verbose_name_plural = "商品附图"
        ordering = ["item_id", "source_cell"]
        constraints = [
            models.UniqueConstraint(fields=["item", "source_cell"], name="uniq_item_image_cell"),
        ]


class Confirmation(FixedInputModel):
    """业务员确认 HS 的追加式记录；当前确认值取该商品最大 revision（D2）。

    一次确认落库后内容固定：新确认只能追加新行，不能改写或删除旧行。
    """

    fixed_input_fields = ("item", "revision", "previous_hs", "confirmed_hs", "reason", "created_by")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="confirmations", verbose_name="商品行")
    revision = models.PositiveIntegerField("确认修订", help_text="与 Item.revision 同步递增；同商品内唯一。")
    previous_hs = models.CharField("修改前确认值", max_length=32, blank=True, default="")
    confirmed_hs = models.CharField("确认值", max_length=32)
    reason = models.TextField("修改原因", blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="confirmations",
        verbose_name="操作人",
    )
    created_at = models.DateTimeField("确认时间", auto_now_add=True)

    class Meta(FixedInputModel.Meta):
        verbose_name = "业务员确认"
        verbose_name_plural = "业务员确认"
        ordering = ["item_id", "revision"]
        constraints = [
            models.UniqueConstraint(fields=["item", "revision"], name="uniq_confirmation_revision"),
        ]


class Submission(FixedInputModel):
    """业务员整批提交给专家的不可变快照（D2、R09：整批提交，不做部分通过）。"""

    fixed_input_fields = ("task", "version", "revision", "created_by")

    class Status(models.TextChoices):
        PENDING = "pending", "待专家审核"
        APPROVED = "approved", "已通过"
        RETURNED = "returned", "已退回"
        SUPERSEDED = "superseded", "已被新提交取代"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="submissions", verbose_name="审核任务")
    version = models.ForeignKey(
        FileVersion, on_delete=models.PROTECT, related_name="submissions", verbose_name="文件版本",
    )
    revision = models.PositiveIntegerField("提交时任务修订", help_text="与 Task.revision 对应，用于冲突校验。")
    status = models.CharField("提交状态", max_length=16, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submissions",
        verbose_name="提交人",
    )
    created_at = models.DateTimeField("提交时间", auto_now_add=True)

    class Meta(FixedInputModel.Meta):
        verbose_name = "提交快照"
        verbose_name_plural = "提交快照"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["task", "version", "revision"], name="uniq_submission_task_version_revision",
            ),
        ]

    def clean(self):
        # 快照内部一致性：提交的文件版本必须属于该任务。
        version = _related_or_none(self, "version")
        if version is not None and self.task_id and version.task_id != self.task_id:
            raise ValidationError("提交的文件版本必须属于该任务，不能跨任务引用。")


class SubmissionItem(FixedInputModel):
    """提交快照行：固定提交时的商品修订和待审 HS，后续编辑不得静默改变。"""

    fixed_input_fields = ("submission", "item", "confirmation", "item_revision", "submitted_hs")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="items", verbose_name="提交快照",
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="submission_items", verbose_name="商品行")
    confirmation = models.ForeignKey(
        Confirmation, on_delete=models.PROTECT, related_name="submission_items", verbose_name="确认记录",
    )
    submitted_hs = models.CharField("提交时 HS", max_length=32)
    item_revision = models.PositiveIntegerField("提交时商品修订")

    class Meta(FixedInputModel.Meta):
        verbose_name = "提交快照行"
        verbose_name_plural = "提交快照行"
        ordering = ["submission_id", "item_id"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "item"], name="uniq_submission_item"),
        ]

    def clean(self):
        """快照内部一致性：版本、商品、确认记录、修订与 HS 必须互相对应。

        只保证“这一份快照自洽”，不保证“提交用的是最新确认”——后者是提交事务的
        时效校验（409），由 P09 负责；旧快照在商品后续被修改后仍应原样可查。
        """
        submission = _related_or_none(self, "submission")
        item = _related_or_none(self, "item")
        confirmation = _related_or_none(self, "confirmation")
        if submission is None or item is None or confirmation is None:
            return
        if item.version_id != submission.version_id:
            raise ValidationError("提交快照行的商品必须属于该提交的文件版本，不能把旧版本商品放进新版本提交。")
        if confirmation.item_id != item.pk:
            raise ValidationError("提交快照行引用的确认记录必须属于同一商品。")
        if self.item_revision != confirmation.revision:
            raise ValidationError("提交快照行的商品修订必须与所引用确认记录的修订一致。")
        if self.submitted_hs != confirmation.confirmed_hs:
            raise ValidationError("提交快照行的 HS 必须取自所引用的确认记录，不能另填。")


class Decision(FixedInputModel):
    """专家人工决定：整批批准或退回（R09）；专家可直接改码后通过（R08）。

    决定是历史结论：落库后动作、对象、原因和审核人固定，不能被事后改写或删除。
    """

    fixed_input_fields = ("submission", "action", "return_to", "reason", "created_by")

    class Action(models.TextChoices):
        APPROVE = "approve", "通过"
        RETURN = "return"

    class ReturnTo(models.TextChoices):
        SUPPLIER = "supplier", "供应商"
        OPERATOR = "operator", "业务员"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        Submission, on_delete=models.PROTECT, related_name="decisions", verbose_name="提交快照",
    )
    action = models.CharField("决定", max_length=16, choices=Action.choices)
    return_to = models.CharField(
        "退回对象", max_length=16, choices=ReturnTo.choices, blank=True, default="",
        help_text="仅退回决定使用；通过时留空。",
    )
    reason = models.TextField("决定说明", blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="decisions",
        verbose_name="审核人",
    )
    created_at = models.DateTimeField("决定时间", auto_now_add=True)

    class Meta(FixedInputModel.Meta):
        verbose_name = "专家决定"
        verbose_name_plural = "专家决定"
        ordering = ["-created_at"]


class DecisionItem(FixedInputModel):
    """专家逐行终值；改码必须保存前后值与原因（R08），不改码时最终值等于提交值。

    逐行终值是历史结论：落库后固定，如需更正只能产生新的专家决定记录。
    """

    fixed_input_fields = ("decision", "item", "previous_hs", "final_hs", "change_reason")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.ForeignKey(
        Decision, on_delete=models.CASCADE, related_name="items", verbose_name="专家决定",
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="decision_items", verbose_name="商品行")
    previous_hs = models.CharField("提交待审 HS", max_length=32, blank=True, default="")
    final_hs = models.CharField("专家终值", max_length=32, blank=True, default="")
    change_reason = models.TextField("改码原因", blank=True, default="")

    class Meta(FixedInputModel.Meta):
        verbose_name = "专家逐行终值"
        verbose_name_plural = "专家逐行终值"
        ordering = ["decision_id", "item_id"]
        constraints = [
            models.UniqueConstraint(fields=["decision", "item"], name="uniq_decision_item"),
        ]

    def clean(self):
        decision = _related_or_none(self, "decision")
        if decision is None or self.item_id is None:
            return
        submission = _related_or_none(decision, "submission")
        if submission is None:
            return
        row = submission.items.filter(item_id=self.item_id).first()
        if row is None:
            raise ValidationError("专家逐行终值只能针对该提交快照中的商品。")
        changed = self.final_hs != row.submitted_hs
        if changed:
            # R08：改码必须留痕，原码取提交快照，不允许空白或另填。
            if self.previous_hs != row.submitted_hs:
                raise ValidationError("专家改码时的原码必须取自提交快照中的待审 HS，不能留空或另填。")
            if not (self.change_reason or "").strip():
                raise ValidationError("专家改码必须填写改码原因。")
        elif self.previous_hs and self.previous_hs != row.submitted_hs:
            raise ValidationError("专家终值未改变时，前值若填写也必须等于快照中的待审 HS。")

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class Issue(models.Model):
    """问题记录：可按商品或任务级；技术失败、证据不足、业务待确认分开记录（B09）。"""

    class Status(models.TextChoices):
        OPEN = "open", "未解决"
        RESOLVED = "resolved", "已解决"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="issues", verbose_name="审核任务")
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True, related_name="issues", verbose_name="商品行",
    )
    run = models.ForeignKey(
        "runs.Run", on_delete=models.SET_NULL, null=True, blank=True, related_name="issues", verbose_name="执行记录",
    )
    kind = models.CharField("问题类型", max_length=32, blank=True, default="")
    message = models.TextField("问题描述")
    status = models.CharField("处理状态", max_length=16, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="issues", verbose_name="记录人",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "问题"
        verbose_name_plural = "问题"
        ordering = ["-created_at"]


class AuditEvent(models.Model):
    """追加式审计日志：记录谁在什么时候把什么值改成了什么（D2 修改日志）。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="audit_events", verbose_name="审核任务")
    version = models.ForeignKey(
        FileVersion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_events", verbose_name="文件版本",
    )
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_events", verbose_name="商品行",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_events", verbose_name="操作人",
    )
    action = models.CharField("动作", max_length=64, help_text="如 confirmation.updated、decision.approved。")
    stage = models.CharField("环节", max_length=32, blank=True, default="")
    previous_value = models.TextField("前值", blank=True, default="")
    new_value = models.TextField("后值", blank=True, default="")
    reason = models.TextField("原因", blank=True, default="")
    details = models.JSONField("附加信息", default=dict, blank=True)
    created_at = models.DateTimeField("发生时间", auto_now_add=True)

    class Meta:
        verbose_name = "审计事件"
        verbose_name_plural = "审计事件"
        ordering = ["-created_at"]


# 历史记录：内容固定（fixed_input_fields）且禁止单独删除；随父记录级联删除仍允许。
for _model in (SubmissionItem, Confirmation, Decision, DecisionItem):
    protect_from_direct_delete(_model)
