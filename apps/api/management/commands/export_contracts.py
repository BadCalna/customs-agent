"""导出契约 v1：JSON Schema、OpenAPI 与 FIX 固定样例。

用法：
    make contracts            # schema + OpenAPI；已 seed-contracts 时同时刷新样例
样例来自真实 ORM 序列化结果，不是手写的理想化数据，因此与 live 响应同构。
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from adapters.engines import protocols
from apps.api.fixtures import DemoSet, FixtureDoc
from apps.api.schemas import (
    CONTRACT_VERSION,
    ErrorBody,
    ErrorEnvelope,
    EvidenceEntry,
    ExpertCheckRef,
    ExpertFinding,
    ImageEntry,
    IssueEntry,
    RunProgress,
    SourceLocation,
    StageValue,
)
from apps.reviews.management.commands.seed_contracts import MAIN_TITLE, RECODE_TITLE
from apps.reviews.models import Task
from adapters.sources.history_search import (
    HistoryCandidate,
    HistoryDatasetInfo,
    HistoryQuery,
    HistoryRecord,
    HistorySearchResult,
    HistorySourceRef,
)
from apps.reviews.schemas import (
    ConfirmationIn,
    ConfirmationOut,
    ItemCard,
    ItemFacts,
    ItemListEntry,
    ItemPage,
    SubmissionIn,
    SubmissionLineIn,
    SubmissionLineOut,
    DecisionIn,
    DecisionLineIn,
    DecisionLineOut,
    DecisionOut,
    HistoryVersion,
    SubmissionOut,
    TaskHistoryOut,
    TaskReturnIn,
    TaskReturnOut,
    WithdrawalOut,
    TaskDetail,
    TaskListEntry,
    TaskPage,
    VersionEntry,
)
from apps.reviews.serializers import item_to_card, item_to_list_entry, task_to_detail
from apps.runs.models import Run
from apps.runs.schemas import AttemptEntry, PrecheckIn, RetryIn, RunCreated, RunDetail, RunItemEntry
from apps.runs.serializers import run_to_detail

from apps.reviews.import_schemas import ImportOut, ImportDetail, ClaimableEntry, ClaimablePage, ClaimOut

SCHEMAS = {
    "imports": [ImportOut, ImportDetail, ClaimableEntry, ClaimablePage, ClaimOut],
    "common": [ErrorEnvelope, SourceLocation, StageValue, IssueEntry, EvidenceEntry, ImageEntry, RunProgress],
    "expert": [ExpertFinding, ExpertCheckRef],
    "tasks": [VersionEntry, TaskListEntry, TaskPage, TaskDetail],
    "items": [ItemListEntry, ItemPage, ItemFacts, ItemCard],
    "runs": [RunItemEntry, RunDetail, AttemptEntry, PrecheckIn, RetryIn, RunCreated],
    "confirmations": [ConfirmationIn, ConfirmationOut],
    "submissions": [SubmissionIn, SubmissionLineIn, SubmissionLineOut, SubmissionOut, WithdrawalOut],
    "decisions": [DecisionIn, DecisionLineIn, DecisionLineOut, DecisionOut, TaskReturnIn, TaskReturnOut],
    "history": [
        TaskHistoryOut,
        HistoryVersion,
        HistoryRecord,
        HistoryQuery,
        HistoryCandidate,
        HistorySearchResult,
            HistorySourceRef,
        HistoryDatasetInfo,
    ],
    "engine": [
        protocols.ExecutionContext,
        protocols.RecommendInput,
        protocols.RecommendOutput,
        protocols.CompareInput,
        protocols.CompareOutput,
        protocols.PrecheckOutput,
        protocols.ExpertCheckInput,
        protocols.ExpertCheckOutput,
    ],
    "fixtures": [FixtureDoc],
}


def dump(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Command(BaseCommand):
    help = "导出契约 v1 的 JSON Schema、OpenAPI 与 FIX 固定样例。"

    def handle(self, *args, **options):
        root = Path(settings.BASE_DIR) / "contracts"
        count = 0
        for group, models in SCHEMAS.items():
            for model in models:
                dump(root / "schemas" / group / f"{model.__name__}.json", model.model_json_schema())
                count += 1
        from config.urls import api_v1

        dump(root / "openapi" / "v1.json", api_v1.get_openapi_schema())
        self.stdout.write(f"已导出 {count} 个 schema 与 OpenAPI（契约 {CONTRACT_VERSION}）")

        main = Task.objects.filter(title=MAIN_TITLE).first()
        recode = Task.objects.filter(title=RECODE_TITLE).first()
        if main is None or recode is None:
            self.stdout.write("未找到契约演示数据，跳过样例导出；需要时先执行 make seed-contracts。")
            return
        fixtures = build_fixtures(main, recode)
        for scenario, doc in fixtures.items():
            # 只去掉未使用的顶层字段；卡片内部的 null 必须保留，
            # “未产生的阶段值为 null”是契约语义，不能在样例里丢。
            payload = {
                key: value
                for key, value in doc.model_dump(mode="json").items()
                if value is not None and value != []
            }
            dump(root / "fixtures" / f"{scenario}.json", payload)
        self.stdout.write(self.style.SUCCESS(f"已导出 {len(fixtures)} 份 FIX 样例到 contracts/fixtures/"))

        demo = build_demo_set(main, recode)
        dump(root / "fixtures" / "DEMO-SET.json", demo.model_dump(mode="json"))
        self.stdout.write(self.style.SUCCESS("已导出 fixture 模式数据集 contracts/fixtures/DEMO-SET.json"))


def build_demo_set(main: Task, recode: Task) -> DemoSet:
    """把演示数据组装成 fixture 模式的完整数据集，形状与 live 端点逐字段一致。"""
    tasks, items, cards, runs = [], {}, {}, {}
    for task in (main, recode):
        user = task.assignments.get(role="operator").user
        tasks.append(task_to_detail(user, task))
        for version in task.versions.all():
            rows = list(version.items.all())
            items[str(version.pk)] = [item_to_list_entry(item) for item in rows]
            for item in rows:
                cards[str(item.pk)] = item_to_card(user, item)
            for run in Run.objects.filter(version=version, is_current=True):
                runs[str(run.pk)] = run_to_detail(run)
    return DemoSet(tasks=tasks, items=items, cards=cards, runs=runs)


def _error(status_code: str, message: str, scenario: str) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=ErrorBody(code=status_code, message=message, details={}),
        request_id=f"fixture-{scenario.lower()}",
    )


def build_fixtures(main: Task, recode: Task) -> dict[str, FixtureDoc]:
    """从演示数据生成样例；各场景含义见 D3 第 4 节。"""

    operator = main.assignments.get(role="operator").user
    v2 = main.versions.get(number=2)
    v1 = main.versions.get(number=1)
    rows = {item.source_row: item for item in v2.items.all()}
    card = lambda row: item_to_card(operator, rows[row])  # noqa: E731 - 局部简写
    run_v2 = Run.objects.get(version=v2, kind="precheck", is_current=True)
    run_v1 = Run.objects.get(version=v1, kind="precheck", is_current=True)
    recode_version = recode.versions.get(number=1)
    recode_card = item_to_card(operator, recode_version.items.get())
    expert_run = Run.objects.get(version=recode_version, kind="expert_check", is_current=True)

    return {
        "FIX-01": FixtureDoc(
            scenario="FIX-01", title="正常建议",
            notes="原值、建议、来源状态可见；result_mode=mock，不自动审批。related_cards 为建议与原码不一致的对照。",
            card=card(5), related_cards=[card(6)],
        ),
        "FIX-02": FixtureDoc(
            scenario="FIX-02", title="缺关键事实",
            notes="执行成功但业务待确认；suggested_hs 为 null，issues 指出缺什么、由谁补充。",
            card=card(7),
        ),
        "FIX-03": FixtureDoc(
            scenario="FIX-03", title="图片失败",
            notes="read_failed 与 absent 分开，保留来源单元格与失败原因；related_cards 为确无图片的对照。",
            card=card(8), related_cards=[card(6)],
        ),
        "FIX-04": FixtureDoc(
            scenario="FIX-04", title="原码异常与未填",
            notes="空白与异常原文都保留，不用建议值回填；supplier_missing 与 supplier_invalid 是两类结论。",
            card=card(9), related_cards=[card(10)],
        ),
        "FIX-05": FixtureDoc(
            scenario="FIX-05", title="部分运行失败",
            notes="其他行成功、失败行带技术错误原因；等待补料不计入 progress.failed。rq 模式正式重试入口由服务端 retry_allowed 授权；本固定演示无操作人，不授予重试。",
            card=card(11), run=run_to_detail(run_v2),
        ),
        "FIX-06": FixtureDoc(
            scenario="FIX-06", title="查询失败",
            notes="evidence_status=query_failed，置信度为 null，不生成已核实结论。",
            card=card(12),
        ),
        "FIX-07": FixtureDoc(
            scenario="FIX-07", title="空列表与无权",
            notes="空列表是 200 空集合；越权读取是 404，不泄露对象是否存在。",
            task_page=TaskPage(items=[], next_cursor=None),
            error=_error("not_found", "对象不存在或不在可见范围", "FIX-07"),
        ),
        "FIX-08": FixtureDoc(
            scenario="FIX-08", title="修订冲突",
            notes="expected_revision 与 Idempotency-Key 由正式写端点校验；本样例固定 409 响应形状。",
            error=_error("conflict", "商品修订已变化，请刷新后重试", "FIX-08"),
        ),
        "FIX-09": FixtureDoc(
            scenario="FIX-09", title="V1 延迟返回",
            notes="V1 执行在其版本内仍可回查；前端按 version_id 丢弃晚到的旧响应，不覆盖 V2 卡片。",
            card=card(5), run=run_to_detail(run_v1),
        ),
        "FIX-10": FixtureDoc(
            scenario="FIX-10", title="专家改码",
            notes="submitted_hs、expert_final_hs、改码原因分别展示；latest_expert_check.target_hs 仍是原提交码。",
            card=recode_card, run=run_to_detail(expert_run),
        ),
    }
