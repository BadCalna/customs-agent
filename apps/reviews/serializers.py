"""ORM → 契约 v1 的序列化。

取值规则（与 D2/D3 一致）：
- 供应商原值只读 ``Item.supplier_*_raw``，任何阶段结果都不回写；
- 预审建议取当前有效预审执行（``Run.is_current`` 且 kind=precheck）的行结果；
- 业务员确认取该商品最大 revision 的 ``Confirmation``；
- 提交值取最新一条包含该商品的 ``SubmissionItem`` 快照；
- 专家终值取最新 ``DecisionItem``；
- 任一阶段没有值就是 ``None``，不用其他阶段补位。
"""
from django.conf import settings

from apps.accounts.permissions import ROLE_ACTIONS
from apps.api.schemas import (
    Action,
    ComparisonStatus,
    EvidenceEntry,
    ExpertCheckRef,
    ExpertFinding,
    ImageEntry,
    ImageStatus,
    IssueEntry,
    ResultMode,
    SourceLocation,
    StageValue,
)
from apps.runs.models import Run, RunItem

from .models import Confirmation, DecisionItem, SubmissionItem
from .schemas import ItemCard, ItemFacts, ItemListEntry, TaskDetail, TaskListEntry, VersionEntry


def iso(value):
    return value.isoformat() if value else None


def result_mode() -> ResultMode:
    return ResultMode(settings.ENGINE_MODE)


def source_location(item) -> SourceLocation:
    return SourceLocation(
        sheet_name=item.source_sheet, row_number=item.source_row, cells=item.source_cells,
    )


def allowed_actions(user) -> list[Action]:
    """只按角色矩阵给出展示用动作；服务端每次仍重新校验，不附带阶段判断。"""

    return [Action(value) for value in sorted(ROLE_ACTIONS.get(user.role, frozenset()))]


def stage_value(value, source_id=None) -> StageValue:
    """值为 null 时仍保留来源引用：执行过但没有建议，与尚未执行是两回事。"""

    if source_id is None and (value is None or value == ""):
        return StageValue(value=None, source_id=None)
    return StageValue(value=value or None, source_id=str(source_id) if source_id else None)


def current_confirmation(item):
    return Confirmation.objects.filter(item=item).order_by("-revision").first()


def current_submission_item(item):
    return (
        SubmissionItem.objects.filter(item=item)
        .select_related("submission")
        .order_by("-submission__created_at", "-submission__revision")
        .first()
    )


def current_decision_item(item):
    return (
        DecisionItem.objects.filter(item=item)
        .select_related("decision")
        .order_by("-decision__created_at")
        .first()
    )


def current_run_item(item, kind=Run.Kind.PRECHECK):
    return (
        RunItem.objects.filter(item=item, run__kind=kind, run__is_current=True)
        .select_related("run")
        .order_by("-run__created_at")
        .first()
    )


def latest_run(task, kind=None):
    queryset = task.runs.all()
    if kind:
        queryset = queryset.filter(kind=kind)
    return queryset.order_by("-created_at").first()


def summarize_image_status(images) -> ImageStatus:
    """任一读取失败即整体标 read_failed；绝不让读不出被当成无图片。"""

    statuses = {image.status for image in images}
    if not statuses:
        return ImageStatus.ABSENT
    if "read_failed" in statuses:
        return ImageStatus.READ_FAILED
    if "available" in statuses:
        return ImageStatus.AVAILABLE
    return ImageStatus.ABSENT


def image_entries(item) -> list[ImageEntry]:
    return [
        ImageEntry(
            image_id=str(image.pk),
            status=ImageStatus(image.status),
            source_sheet=image.source_sheet,
            source_cell=image.source_cell,
            dispimg_id=image.dispimg_id,
            download_url=f"/api/v1/files/{image.pk}" if image.status == "available" and image.storage_path else None,
            error_message=image.error_message,
        )
        for image in item.images.all()
    ]


def _issues_from_result(result: dict) -> list[IssueEntry]:
    return [IssueEntry(**entry) for entry in result.get("issues", [])]


def _evidence_from_result(result: dict) -> list[EvidenceEntry]:
    return [EvidenceEntry(**entry) for entry in result.get("evidence", [])]


def _expert_check_ref(run_item) -> ExpertCheckRef | None:
    if run_item is None or run_item.run.submission_id is None:
        return None
    result = run_item.result or {}
    submission_item = current_submission_item(run_item.item)
    target_hs = result.get("target_hs") or (submission_item.submitted_hs if submission_item else "")
    if not target_hs:
        return None
    return ExpertCheckRef(
        run_id=str(run_item.run_id),
        submission_id=str(run_item.run.submission_id),
        target_hs=target_hs,
        findings=[ExpertFinding(**finding) for finding in result.get("findings", [])],
    )


def _stages(item):
    """五个编码阶段的当前值与来源引用。"""

    precheck = current_run_item(item, Run.Kind.PRECHECK)
    confirmation = current_confirmation(item)
    submission_item = current_submission_item(item)
    decision_item = current_decision_item(item)
    expert = current_run_item(item, Run.Kind.EXPERT_CHECK)
    # 专家核查建议落在 RunItem.suggested_hs；读 result 内字段会永远拿到 null。
    expert_suggested = expert.suggested_hs if expert else None
    return {
        "precheck": precheck,
        "expert": expert,
        "precheck_suggested_hs": stage_value(
            precheck.suggested_hs if precheck else None, precheck.run_id if precheck else None,
        ),
        "business_confirmed_hs": stage_value(
            confirmation.confirmed_hs if confirmation else None, confirmation.pk if confirmation else None,
        ),
        "submitted_hs": stage_value(
            submission_item.submitted_hs if submission_item else None,
            submission_item.submission_id if submission_item else None,
        ),
        "expert_suggested_hs": stage_value(expert_suggested, expert.run_id if expert else None),
        "expert_final_hs": stage_value(
            decision_item.final_hs if decision_item else None, decision_item.decision_id if decision_item else None,
        ),
    }


def item_to_list_entry(item) -> ItemListEntry:
    stages = _stages(item)
    precheck = stages["precheck"]
    return ItemListEntry(
        task_id=str(item.version.task_id),
        version_id=str(item.version_id),
        item_id=str(item.pk),
        revision=item.revision,
        source_location=source_location(item),
        name=item.name,
        supplier_hs_raw=item.supplier_hs_raw,
        precheck_suggested_hs=stages["precheck_suggested_hs"].value,
        business_confirmed_hs=stages["business_confirmed_hs"].value,
        submitted_hs=stages["submitted_hs"].value,
        expert_final_hs=stages["expert_final_hs"].value,
        comparison_status=ComparisonStatus(precheck.comparison_status) if precheck and precheck.comparison_status else None,
        image_status=summarize_image_status(list(item.images.all())),
        latest_run_id=str(precheck.run_id) if precheck else None,
        result_mode=result_mode(),
    )


def item_to_card(user, item) -> ItemCard:
    task = item.version.task
    stages = _stages(item)
    precheck = stages["precheck"]
    result = (precheck.result or {}) if precheck else {}
    submission_item = current_submission_item(item)
    images = list(item.images.all())
    return ItemCard(
        task_id=str(task.pk),
        version_id=str(item.version_id),
        item_id=str(item.pk),
        revision=item.revision,
        source_location=source_location(item),
        facts=ItemFacts(
            name=item.name,
            attributes=item.attributes or {},
            supplier_hs_raw=item.supplier_hs_raw,
            supplier_elements_raw=item.supplier_elements_raw,
            supplier_tax_rate_raw=item.supplier_tax_rate_raw,
        ),
        images=image_entries(item),
        image_status=summarize_image_status(images),
        precheck_suggested_hs=stages["precheck_suggested_hs"],
        business_confirmed_hs=stages["business_confirmed_hs"],
        submitted_hs=stages["submitted_hs"],
        expert_suggested_hs=stages["expert_suggested_hs"],
        expert_final_hs=stages["expert_final_hs"],
        comparison_status=ComparisonStatus(precheck.comparison_status) if precheck and precheck.comparison_status else None,
        issues=_issues_from_result(result),
        evidence_status=result.get("evidence_status"),
        evidence=_evidence_from_result(result),
        confidence=precheck.confidence if precheck else None,
        latest_run_id=str(precheck.run_id) if precheck else None,
        latest_expert_check=_expert_check_ref(stages["expert"]),
        result_mode=result_mode(),
        task_stage=task.stage,
        submission_status=submission_item.submission.status if submission_item else None,
        allowed_actions=allowed_actions(user),
    )


def _version_entry(version, current_id) -> VersionEntry:
    return VersionEntry(
        version_id=str(version.pk),
        number=version.number,
        original_filename=version.original_filename,
        uploaded_at=iso(version.uploaded_at),
        is_current=version.pk == current_id,
    )


def task_to_entry(user, task) -> TaskListEntry:
    current_version = task.versions.order_by("-number").first()
    run = Run.objects.filter(version=current_version).order_by("-created_at").first() if current_version else None
    return TaskListEntry(
        task_id=str(task.pk),
        title=task.title,
        stage=task.stage,
        supplier=task.supplier.display_name or task.supplier.username,
        revision=task.revision,
        current_version_id=str(current_version.pk) if current_version else None,
        current_version_number=current_version.number if current_version else None,
        latest_run_id=str(run.pk) if run else None,
        latest_run_kind=run.kind if run else None,
        latest_run_status=run.status if run else None,
        allowed_actions=allowed_actions(user),
        result_mode=result_mode(),
        created_at=iso(task.created_at),
        updated_at=iso(task.updated_at),
    )


def task_to_detail(user, task) -> TaskDetail:
    from apps.runs.services import precheck_capability
    entry = task_to_entry(user, task)
    current_version = task.versions.order_by("-number").first()
    submission = task.submissions.filter(version=current_version).order_by("-created_at").first()
    return TaskDetail(
        **entry.model_dump(),
        precheck_allowed=precheck_capability(user, task),
        versions=[_version_entry(v, current_version.pk if current_version else None) for v in task.versions.all()],
        current_submission_id=str(submission.pk) if submission else None,
        current_submission_status=submission.status if submission else None,
    )
