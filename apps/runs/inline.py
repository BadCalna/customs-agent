"""inline 执行编排：真实 ORM 落库 + mock 引擎边界。

用途仅限于 M0/M1 的顺序流程与演示数据：
- 由命令或测试显式调用，GET 查询绝不触发；
- 输入在调用时从版本化 ORM 数据现场构造，不另存“AI 专用数据”；
- 逐行记录技术失败，业务缺事实不算技术失败；
- 真实队列、重试、过期保护由 P13 接管，本模块不提供恢复保证。
"""
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from adapters.engines.mock import EngineTechnicalError
from adapters.engines.protocols import (
    CompareInput,
    ExecutionContext,
    ExpertCheckInput,
    ImageFact,
    RecommendInput,
    get_engine,
)
from apps.api.schemas import ResultMode
from apps.reviews.models import Item, Submission, Task
from apps.reviews.services.history_reference import precheck_history_reference

from .models import Run, RunItem


def _input_hash(payload) -> str:
    return hashlib.sha256(payload.model_dump_json().encode()).hexdigest()


def _merge_issues(*groups) -> list[dict]:
    """合并推荐与比较两个阶段的问题，按完整内容去重并保持首次出现顺序。

    ``compare_original`` 在“资料不足”分支会原样复用 ``recommendation.gaps``，
    直接拼接会让同一个问题在商品卡片上出现两次。
    """
    merged, seen = [], set()
    for group in groups:
        for issue in group:
            payload = issue.model_dump()
            key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            merged.append(payload)
    return merged


def _context(run, item, scenario) -> ExecutionContext:
    engine = get_engine()
    return ExecutionContext(
        task_id=str(run.task_id),
        version_id=str(run.version_id),
        item_id=str(item.pk),
        run_id=str(run.pk),
        input_revision=item.revision,
        engine_version=getattr(engine, "engine_version", ""),
        prompt_version=getattr(engine, "prompt_version", ""),
        rules_version=getattr(engine, "rules_version", ""),
        result_mode=ResultMode.MOCK,
        mock_scenario=scenario,
    )


def _image_facts(item) -> list[ImageFact]:
    return [
        ImageFact(
            image_id=str(image.pk),
            status=image.status,
            source_cell=image.source_cell,
            error_message=image.error_message,
        )
        for image in item.images.all()
    ]


# 独立推荐只取明确的商品事实。来源快照、原码、旧意见和旧阶段结果仍留库供审计。
_RECOMMEND_FACT_FIELDS = frozenset({
    "name", "primary_name", "alias", "specification", "manufacturer_model", "material",
    "usage", "brand", "model", "quantity", "unit", "process", "composition", "function",
    "品名", "规格型号", "材质", "用途", "品牌", "型号", "成分", "工艺", "功能", "数量", "单位",
})


def _recommend_attributes(attributes) -> dict[str, str]:
    # 只允许标量，不能把嵌套历史/字段对象以字符串形式藏回白名单字段。
    return {str(key): str(value) for key, value in (attributes or {}).items()
            if key in _RECOMMEND_FACT_FIELDS and isinstance(value, (str, int, float, bool))}


def _build_recommend_input(run, item, scenario) -> RecommendInput:
    context = _context(run, item, scenario)
    payload = RecommendInput(
        context=context,
        name=item.name,
        attributes=_recommend_attributes(item.attributes),
        elements_text=item.supplier_elements_raw,
        # 申报要素原文可能自然出现编码；只记录风险，不声称已彻底消除锚定。
        anchoring_notes=["elements_text 为供应商原文，可能含编码字样"],
        images=_image_facts(item),
        query_basis={},
    )
    payload.context.input_hash = _input_hash(payload)
    return payload


def _scenario_for(scenarios, item) -> str:
    return scenarios.get(str(item.pk)) or scenarios.get("*") or "normal"


def _start_run(task, version, kind, submission=None, triggered_by=None) -> Run:
    # is_current 按“文件版本 + 执行类型”维护：同版本重跑只留最新一轮为当前，
    # 旧版本（V1）的当前执行仍然有效，供历史页面回查，不被 V2 顶掉。
    Run.objects.filter(version=version, kind=kind, is_current=True).update(is_current=False)
    run = Run(
        task=task,
        version=version,
        kind=kind,
        submission=submission,
        status=Run.Status.RUNNING,
        is_current=True,
        triggered_by=triggered_by,
        started_at=timezone.now(),
    )
    run.save()
    return run


def _finish_run(run):
    rows = list(run.items.all())
    run.status = Run.Status.FAILED if rows and all(r.status == RunItem.Status.FAILED for r in rows) else Run.Status.SUCCEEDED
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at"])
    return run


@transaction.atomic
def execute_precheck(version, scenarios: dict[str, str] | None = None, triggered_by=None, run=None) -> Run:
    """对指定版本的全部商品执行一次 mock 预审，旧执行转为历史。"""

    scenarios = scenarios or {}
    engine = get_engine()
    if run is None:
        run = _start_run(version.task, version, Run.Kind.PRECHECK, triggered_by=triggered_by)
    else:
        Task.objects.select_for_update().get(pk=version.task_id)
        run = Run.objects.select_for_update().get(pk=run.pk)
        if run.version_id != version.pk or run.kind != Run.Kind.PRECHECK or run.submission_id:
            raise ValueError("预登记执行与预审版本不一致")
        if run.status != Run.Status.QUEUED:
            return run
        Run.objects.filter(version=version, kind=run.kind, is_current=True).exclude(pk=run.pk).update(is_current=False)
        run.status, run.is_current, run.started_at = Run.Status.RUNNING, True, timezone.now()
        run.finished_at = None
        run.save(update_fields=["status", "is_current", "started_at", "finished_at"])
    for item in Item.objects.filter(version=version).order_by("source_sheet", "source_row"):
        scenario = _scenario_for(scenarios, item)
        row = RunItem(run=run, item=item, status=RunItem.Status.RUNNING)
        row.save()
        recommend_input = _build_recommend_input(run, item, scenario)
        try:
            recommendation = engine.recommend(recommend_input)
            comparison = engine.compare(CompareInput(
                context=recommend_input.context,
                recommendation=recommendation,
                supplier_hs_raw=item.supplier_hs_raw,
                association_ok=scenario != "association_error",
                association_note="mock：装箱项号关联歧义" if scenario == "association_error" else "",
                supplier_hs_format_valid=scenario != "supplier_invalid",
                supplier_hs_validity_note="mock：原 HS 格式异常" if scenario == "supplier_invalid" else "",
            ))
        except EngineTechnicalError as exc:
            row.status = RunItem.Status.FAILED
            row.error_message = str(exc)
            row.result = {"issues": [], "evidence": [], "attempts": 1}
            row.save()
            continue
        row.status = RunItem.Status.SUCCEEDED
        row.suggested_hs = recommendation.suggested_hs
        row.comparison_status = comparison.comparison_status.value
        row.confidence = recommendation.confidence
        row.result = {
            "candidates": [c.model_dump() for c in recommendation.candidates],
            "comparison_reason": comparison.reason,
            "confidence_note": recommendation.confidence_note,
            "evidence_status": recommendation.evidence_status.value if recommendation.evidence_status else None,
            "evidence": [e.model_dump() for e in recommendation.evidence],
            "issues": _merge_issues(recommendation.gaps, comparison.issues),
            "attempts": 1,
            "engine_version": recommend_input.context.engine_version,
            "prompt_version": recommend_input.context.prompt_version,
            "rules_version": recommend_input.context.rules_version,
            "input_hash": recommend_input.context.input_hash,
            "mock_scenario": scenario,
        }
        # 历史源仅追加参考证据；不进独立推荐输入，也不回写建议HS或比较结论。
        history_reference = precheck_history_reference(item)
        if history_reference is not None:
            # 历史参考只持久化给内部审计；不追加到通用 evidence，避免供应商
            # 通过 item/run 只读序列化看到跨供应商合同/报关来源。
            row.result["history_reference"] = history_reference.model_dump(mode="json")
        row.save()
    return _finish_run(run)


def _claim_preregistered_run(run) -> Run | None:
    """在 Task → Submission → Run 的锁顺序内重查并激活预登记执行。

    与 P09 撤回、P10 处置共用同一把任务锁，因此"检查通过"之后不可能再插入
    撤回/重新提交；拿不到锁就等，不会读到旧状态。返回 None 表示本轮作废。
    调用方（``execute_expert_check``）本身在 ``transaction.atomic`` 内，
    锁会一直持有到本轮行结果写完，避免激活后又被撤回穿插。
    """
    Task.objects.select_for_update().get(pk=run.task_id)
    submission = Submission.objects.select_for_update().get(pk=run.submission_id)
    run = Run.objects.select_for_update().get(pk=run.pk)
    if run.status != Run.Status.QUEUED:
        return None
    newer_exists = Run.objects.filter(
        version_id=run.version_id, kind=run.kind, created_at__gt=run.created_at,
    ).exists()
    if submission.status != Submission.Status.PENDING or newer_exists:
        run.status = Run.Status.SUPERSEDED
        run.is_current = False
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "is_current", "finished_at"])
        return None
    Run.objects.filter(version=run.version, kind=run.kind, is_current=True).exclude(pk=run.pk).update(is_current=False)
    run.status = Run.Status.RUNNING
    run.is_current = True
    run.started_at = timezone.now()
    run.save(update_fields=["status", "is_current", "started_at"])
    return run


@transaction.atomic
def execute_expert_check(submission, scenarios: dict[str, str] | None = None, triggered_by=None, run=None) -> Run:
    """对提交快照执行 mock 专家核查；核查对象固定为快照中的提交码。

    ``run`` 由 P09 在提交事务内预先登记（queued），此处复用而不是新建，
    保证"提交快照 + 待运行记录"是同一个事务的产物。
    """

    scenarios = scenarios or {}
    engine = get_engine()
    if run is None:
        run = _start_run(
            submission.task, submission.version, Run.Kind.EXPERT_CHECK,
            submission=submission, triggered_by=triggered_by,
        )
    else:
        # 迟到执行（P13 队列延后启动）不得抢占当前。重查与激活必须在**同一把任务锁**内：
        # 先查后激活之间若允许撤回/改码/重新提交插入，旧执行仍会拿到"检查通过"的结论。
        claimed = _claim_preregistered_run(run)
        if claimed is None:
            return run  # 已置 superseded，本轮不写任何行结果
        run = claimed
    for line in submission.items.select_related("item"):
        item = line.item
        scenario = _scenario_for(scenarios, item)
        row = RunItem(run=run, item=item, status=RunItem.Status.RUNNING)
        row.save()
        context = _context(run, item, scenario)
        payload = ExpertCheckInput(
            context=context,
            submission_id=str(submission.pk),
            submitted_hs=line.submitted_hs,
            name=item.name,
            attributes={str(k): str(v) for k, v in (item.attributes or {}).items()},
            elements_text=item.supplier_elements_raw,
            supplier_tax_rate_raw=item.supplier_tax_rate_raw,
            images=_image_facts(item),
        )
        context.input_hash = _input_hash(payload)
        try:
            output = engine.expert_check(payload)
        except EngineTechnicalError as exc:
            row.status = RunItem.Status.FAILED
            row.error_message = str(exc)
            row.result = {"issues": [], "evidence": [], "attempts": 1}
            row.save()
            continue
        row.status = RunItem.Status.SUCCEEDED
        row.suggested_hs = output.suggested_hs
        row.result = {
            "target_hs": output.target_hs,
            "findings": [f.model_dump() for f in output.findings],
            "evidence_status": output.evidence_status.value if output.evidence_status else None,
            "evidence": [e.model_dump() for e in output.evidence],
            "issues": [i.model_dump() for i in output.issues],
            "attempts": 1,
            "engine_version": context.engine_version,
            "prompt_version": context.prompt_version,
            "rules_version": context.rules_version,
            "input_hash": context.input_hash,
            "mock_scenario": scenario,
        }
        row.save()
    return _finish_run(run)
