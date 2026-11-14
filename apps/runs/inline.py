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
from apps.reviews.models import Item

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


def _build_recommend_input(run, item, scenario) -> RecommendInput:
    context = _context(run, item, scenario)
    payload = RecommendInput(
        context=context,
        name=item.name,
        attributes={str(k): str(v) for k, v in (item.attributes or {}).items()},
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
def execute_precheck(version, scenarios: dict[str, str] | None = None, triggered_by=None) -> Run:
    """对指定版本的全部商品执行一次 mock 预审，旧执行转为历史。"""

    scenarios = scenarios or {}
    engine = get_engine()
    run = _start_run(version.task, version, Run.Kind.PRECHECK, triggered_by=triggered_by)
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
        row.save()
    return _finish_run(run)


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
        Run.objects.filter(version=run.version, kind=run.kind, is_current=True).exclude(pk=run.pk).update(is_current=False)
        run.status = Run.Status.RUNNING
        run.is_current = True
        run.started_at = timezone.now()
        run.save(update_fields=["status", "is_current", "started_at"])
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
