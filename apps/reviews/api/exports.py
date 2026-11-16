"""P20 导出入口。由主控移除 501 占位后挂载 router；不在本包修改全局路由。

第一批直接返回下载文件，不登记虚构的文件 ID、不自动退回或发送通知。
原件读取复用 P05；结果模式只看选定执行的持久化来源，不随当前引擎配置切换。
"""
from __future__ import annotations

import io
from typing import Literal

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from ninja import Router, Schema

from apps.accounts.auth import session_auth
from apps.accounts.errors import ApiError
from apps.accounts.models import Role
from apps.accounts.permissions import READ, require_action
from apps.api.schemas import IssueEntry
from apps.runs.models import Run
from adapters.files.export_workbook import ExportError, ExportLine, ExportSelection, export_precheck
from adapters.files.export_issues import XLSX_MIME, export_issues
from adapters.files.ooxml import OoxmlError
from experiments.excel_fidelity.workbook import FidelityError
from ..models import Decision, FileVersion

router = Router(tags=["结果导出"])


class ExportIn(Schema):
    kind: Literal["precheck", "issues"]
    run_id: str
    submission_id: str | None = None
    decision_id: str | None = None
    excel2016_images: bool = True


def _read_original(user, version_id):
    try:
        from ..services.files import read_version_original
    except ModuleNotFoundError as exc:
        if exc.name == "fcntl":
            raise ApiError(503, "storage_platform_unsupported", "当前文件存储模块尚未支持此运行平台，导出模块可先做离线验证") from exc
        raise
    return read_version_original(user, version_id)


def build_export(user, version_id: str, payload: ExportIn):
    try:
        version = FileVersion.objects.select_related("task").get(pk=version_id)
    except (FileVersion.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("文件版本不存在或不在可见范围") from exc
    require_action(user, READ, version)
    if user.role not in (Role.OPERATOR, Role.EXPERT):
        raise PermissionDenied("审核结果导出仅供该任务的业务员或专家使用")
    try:
        run = Run.objects.select_related("submission").get(pk=payload.run_id, version=version, task=version.task)
    except (Run.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ApiError(422, "result_reference_mismatch", "执行结果不属于请求的任务和文件版本") from exc
    if run.status in (Run.Status.QUEUED, Run.Status.RUNNING):
        raise ApiError(409, "run_not_finished", "执行尚未结束，请完成后导出")
    if run.status == Run.Status.SUPERSEDED:
        raise ApiError(409, "run_superseded", "该执行已作废，不能作为有效审核结果导出")
    if run.submission_id and (run.submission.task_id != version.task_id or run.submission.version_id != version.pk):
        raise ApiError(422, "result_reference_mismatch", "执行引用的提交快照与任务或文件版本不符")
    if run.kind == Run.Kind.PRECHECK and (payload.submission_id or payload.decision_id):
        raise ApiError(422, "result_reference_mismatch", "预审导出不能混入专家提交或决定")
    if run.kind == Run.Kind.EXPERT_CHECK and str(run.submission_id) != payload.submission_id:
        raise ApiError(422, "result_reference_mismatch", "专家导出必须明确引用本次核查对应的提交快照")
    if payload.decision_id:
        try:
            decision = Decision.objects.get(pk=payload.decision_id)
        except (Decision.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
            raise ApiError(422, "result_reference_mismatch", "指定决定不存在") from exc
        if decision.submission_id != run.submission_id:
            raise ApiError(422, "result_reference_mismatch", "专家决定不属于选定的提交快照")
    items = list(version.items.order_by("source_sheet", "source_row"))
    results = {row.item_id: row for row in run.items.all()}
    if set(results) != {item.pk for item in items} or not items:
        raise ApiError(409, "incomplete_result_set", "该执行没有覆盖本文件全部商品，不能自行拼入其他轮次结果")
    if any(row.status in ("queued", "running") for row in results.values()):
        raise ApiError(409, "run_not_finished", "仍有商品尚未完成核查，请完成后导出")
    lines, modes = [], set()
    try:
        for item in items:
            row = results[item.pk]
            data = row.result or {}
            if not isinstance(data, dict):
                raise ExportError("invalid_result", "执行完整结果必须是对象")
            # 旧 mock 执行未持久化 result_mode；缺失来源保守标为 mock，绝不读当前配置猜 live。
            mode = data.get("result_mode", "mock")
            if "mock_scenario" in data or "mock" in str(data.get("engine_version", "")).lower():
                mode = "mock"
            if mode not in ("mock", "live"):
                raise ExportError("invalid_result_mode", "选定执行的结果来源模式未知")
            modes.add(mode)
            for key, value in (("suggested_hs", row.suggested_hs), ("comparison_status", row.comparison_status or None),
                               ("confidence", row.confidence)):
                if key in data and data[key] != value:
                    raise ExportError("result_value_conflict", "执行摘要与完整结果中的编码、结论或置信度冲突")
            lines.append(ExportLine(
                item_id=str(item.pk), version_id=str(version.pk), run_id=str(run.pk),
                sheet=item.source_sheet, row=item.source_row, suggested_hs=row.suggested_hs,
                comparison_status=row.comparison_status or None, reason=data.get("comparison_reason", ""),
                confidence=row.confidence, confidence_label=data.get("confidence_label", ""),
                confidence_note=data.get("confidence_note", ""), status=row.status,
                error_message=row.error_message,
                issues=tuple(IssueEntry.model_validate(issue) for issue in data.get("issues", [])),
            ))
        original = _read_original(user, version.pk)
        selection = ExportSelection(
            task_id=str(version.task_id), version_id=str(version.pk), run_id=str(run.pk),
            source_sha256=original.sha256, lines=tuple(lines),
            result_mode="live" if modes == {"live"} else "mock", run_kind=run.kind,
            submission_id=payload.submission_id, decision_id=payload.decision_id,
            version_number=version.number,
        )
        artifact = (export_precheck(original.content, selection, excel2016_images=payload.excel2016_images)
                    if payload.kind == "precheck" else export_issues(original.content, selection))
        return artifact
    except (ExportError, OoxmlError, FidelityError, ValidationError, ValueError) as exc:
        if isinstance(exc, ApiError):
            raise
        raise ApiError(422, getattr(exc, "code", "export_validation_failed"), str(exc)) from exc


@router.post("/versions/{version_id}/exports", auth=session_auth)
def create_export(request, version_id: str, payload: ExportIn):
    artifact = build_export(request.auth, version_id, payload)
    response = FileResponse(io.BytesIO(artifact.content), as_attachment=True,
                            filename=artifact.filename, content_type=XLSX_MIME)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    response["X-Export-Run"] = artifact.manifest["run_id"]
    response["X-Result-Mode"] = artifact.manifest["result_mode"]
    return response
