"""建立契约 v1 的演示数据，覆盖 D3 第 4 节 FIX-01～FIX-10 的持久化部分。

- 幂等：已存在同名演示任务时跳过，不重置任何业务资料；
- 全部结果由 inline mock 引擎真实落库，刷新页面或重启服务后仍可读取；
- 所有编码与依据都是合成样例，不构成真实归类结论；
- FIX-07（空列表/越权）与 FIX-08（修订冲突）属于请求层行为，由 tests/p02 覆盖。
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from adapters.engines.mock import SAMPLE_HS, SAMPLE_HS_ALT
from apps.accounts.models import Role, TaskAssignment, User
from apps.runs.inline import execute_expert_check, execute_precheck

from ...models import (
    Confirmation,
    Decision,
    DecisionItem,
    FileVersion,
    Item,
    ItemImage,
    Submission,
    SubmissionItem,
    Task,
)

MAIN_TITLE = "契约演示-主流程"
RECODE_TITLE = "契约演示-专家改码"
SHEET = "申报要素"

# (行号, 品名, 供应商原 HS, mock 场景, 附图状态)
MAIN_V2_ITEMS = [
    (5, "不锈钢制无缝管（一致样例）", SAMPLE_HS, "normal", "available"),
    (6, "不锈钢制焊接管（不一致样例）", "7306290000", "normal", "absent"),
    (7, "镍合金板（缺用途）", SAMPLE_HS, "missing_facts", "absent"),
    (8, "镍合金带（附图读取失败）", SAMPLE_HS, "image_failed", "read_failed"),
    (9, "钢制紧固件（原码未填）", "", "normal", "absent"),
    (10, "钢制法兰（原码异常）", "ABC", "supplier_invalid", "absent"),
    (11, "铜制管件（技术失败行）", SAMPLE_HS, "tech_failure", "absent"),
    (12, "铝制型材（查询失败）", SAMPLE_HS, "query_failed", "absent"),
]


class Command(BaseCommand):
    help = "建立契约 v1 演示任务、版本、商品与 mock 执行结果（幂等）。"

    @transaction.atomic
    def handle(self, *args, **options):
        supplier = User.objects.get(username="supplier_a")
        operator = User.objects.get(username="operator_a")
        expert = User.objects.get(username="expert")
        created = []
        if not Task.objects.filter(title=MAIN_TITLE).exists():
            self._build_main_task(supplier, operator, expert)
            created.append(MAIN_TITLE)
        if not Task.objects.filter(title=RECODE_TITLE).exists():
            self._build_recode_task(supplier, operator, expert)
            created.append(RECODE_TITLE)
        if created:
            self.stdout.write(f"新建演示任务：{', '.join(created)}")
        else:
            self.stdout.write("演示任务已存在，未重复创建。")
        self.stdout.write(self.style.SUCCESS(
            "seed-contracts 完成；查询 /api/v1/tasks 即可读取真实保存的 mock 结果。"
        ))

    def _grant(self, task, operator, expert):
        TaskAssignment.objects.get_or_create(task=task, user=operator, defaults={"role": Role.OPERATOR})
        TaskAssignment.objects.get_or_create(task=task, user=expert, defaults={"role": Role.EXPERT})

    def _version(self, task, number, uploaded_by, filename) -> FileVersion:
        return FileVersion.objects.create(
            task=task, number=number, original_filename=filename, uploaded_by=uploaded_by,
        )

    def _item(self, version, row, name, raw_hs, image_status) -> Item:
        item = Item.objects.create(
            version=version,
            source_sheet=SHEET,
            source_row=row,
            source_cells=f"A{row}:H{row}",
            name=name,
            attributes={"品名": name, "规格型号": f"SPEC-{row}"},
            supplier_hs_raw=raw_hs,
            supplier_elements_raw=f"品名:{name}|规格型号:SPEC-{row}",
            supplier_tax_rate_raw="",
        )
        if image_status != "absent":
            ItemImage.objects.create(
                item=item,
                source_sheet=SHEET,
                source_cell=f"H{row}",
                dispimg_id=f"DISPIMG_{row}",
                status=image_status,
                error_message="mock：DISPIMG 嵌入资源缺失" if image_status == "read_failed" else "",
            )
        return item

    def _build_main_task(self, supplier, operator, expert):
        task = Task.objects.create(title=MAIN_TITLE, supplier=supplier)
        self._grant(task, operator, expert)

        # V1：两条商品，用于 FIX-09 的旧版本历史与晚到响应对照。
        v1 = self._version(task, 1, supplier, "契约演示-V1.xlsx")
        self._item(v1, 5, "不锈钢制无缝管（一致样例）", SAMPLE_HS, "available")
        self._item(v1, 6, "不锈钢制焊接管（不一致样例）", "7306290000", "absent")
        # V1 的预审执行在该版本内仍为当前有效，供 FIX-09 历史回查。
        execute_precheck(v1, triggered_by=None)

        # V2：补料重排后的整表重审，覆盖 FIX-01～FIX-06。
        v2 = self._version(task, 2, supplier, "契约演示-V2.xlsx")
        scenarios = {}
        for row, name, raw_hs, scenario, image_status in MAIN_V2_ITEMS:
            item = self._item(v2, row, name, raw_hs, image_status)
            scenarios[str(item.pk)] = scenario
        execute_precheck(v2, scenarios=scenarios)
        task.stage = Task.Stage.AWAITING_CONFIRMATION
        task.save(update_fields=["stage"])

    def _build_recode_task(self, supplier, operator, expert):
        """FIX-10：提交码、专家终值、改码原因与旧核查对象分别可查。"""

        task = Task.objects.create(title=RECODE_TITLE, supplier=supplier)
        self._grant(task, operator, expert)
        version = self._version(task, 1, supplier, "契约演示-改码.xlsx")
        item = self._item(version, 3, "不锈钢制无缝管（改码样例）", SAMPLE_HS, "available")
        execute_precheck(version)

        confirmation = Confirmation.objects.create(
            item=item, revision=1, previous_hs="", confirmed_hs=SAMPLE_HS,
            reason="业务员按预审建议确认", created_by=operator,
        )
        submission = Submission.objects.create(
            task=task, version=version, revision=task.revision, created_by=operator,
        )
        SubmissionItem.objects.create(
            submission=submission, item=item, confirmation=confirmation,
            submitted_hs=confirmation.confirmed_hs, item_revision=confirmation.revision,
        )
        task.stage = Task.Stage.SUBMITTED
        task.save(update_fields=["stage"])
        execute_expert_check(submission, scenarios={str(item.pk): "expert_conflict"})

        decision = Decision.objects.create(
            submission=submission, action=Decision.Action.APPROVE,
            reason="专家改码后整批通过（演示数据）", created_by=expert,
        )
        DecisionItem.objects.create(
            decision=decision, item=item,
            previous_hs=confirmation.confirmed_hs, final_hs=SAMPLE_HS_ALT,
            change_reason="mock：要素核查冲突，专家改码",
        )
        submission.status = Submission.Status.APPROVED
        submission.save(update_fields=["status"])
        task.stage = Task.Stage.APPROVED
        task.save(update_fields=["stage"])
