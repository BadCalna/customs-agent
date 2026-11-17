"""真实 XLSX -> 独立版本 -> P04/P05 -> inline mock。

事务 A 持久登记暂存 token、版本、唯一预审；事务 B 锁任务、发布文件、解析落库并执行。
文件系统不参与数据库回滚。B 中断后 A 仍在，同 key 或恢复命令重用 token/版本/run。
"""
import hashlib
import logging
from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from adapters.files import storage
from adapters.files.declarations import SemanticLayoutError
from adapters.files.ooxml import OoxmlError, Workbook
from adapters.files.packing_links import map_workbook
from apps.accounts.errors import ApiError
from apps.accounts.models import Role, User
from apps.accounts.permissions import UPLOAD_VERSION, _require_authenticated, get_visible_task, require_action
from apps.reviews.models import AuditEvent, FileVersion, ImportJob, Item, ItemImage, ItemMember, Submission, Task
from apps.runs.inline import execute_precheck
from apps.runs.models import Run
from . import files, idempotency
from .claims import validate_key

logger = logging.getLogger(__name__)
ACTION = "file.import"


def authorize(user, task_id=None):
    _require_authenticated(user)
    if task_id is not None:
        task = get_visible_task(user, task_id)
        require_action(user, UPLOAD_VERSION, task)
        return task
    if user.role != Role.SUPPLIER:
        raise PermissionDenied("只有供应商可以上传新任务")
    return None


def parse(data, filename):
    try:
        mapping = map_workbook(Workbook(data, source_name=filename))
    except (OoxmlError, SemanticLayoutError) as exc:
        issue = {"code": exc.code, "message": str(exc), "sheet": getattr(exc, "sheet", "") or "",
                 "cell": getattr(exc, "cell", "") or "", "row": 0, "blocking": True,
                 "details": getattr(exc, "details", {})}
        raise ApiError(422, "parse_blocked", "模板结构无法导入", {"issues": [issue]}) from exc
    issues = [x.as_dict() for x in mapping.blocking_issues]
    # P04 的关联争抢仍以 warning 返回；P06 导入不得静默丢弃成员。
    for link in mapping.links:
        if link.conflicting_declaration_rows or any(m.ownership_ambiguous for m in link.members):
            issues.append({"code": "ambiguous_member_ownership", "message": "装箱成员被多条申报行争抢",
                           "sheet": link.sheet, "row": link.row, "cell": link.record.facts["item"].cell,
                           "blocking": True})
        if not link.record.value("name").strip():
            issues.append({"code": "missing_name", "message": "商品品名为空，无法识别商品",
                           "sheet": link.sheet, "row": link.row, "cell": link.record.facts["name"].cell,
                           "blocking": True})
        for field, value in link.item_values().items():
            limit = getattr(Item._meta.get_field(field), "max_length", None)
            if limit and len(str(value)) > limit:
                issues.append({"code": "field_too_long", "message": f"{field} 超过存储长度 {limit}",
                               "sheet": link.sheet, "row": link.row, "cell": link.record.cell("name"), "blocking": True})
        for member in link.confirmed_members:
            for field, value in (("contract_no", member.contract_no), ("item_no", member.item_no)):
                limit = ItemMember._meta.get_field(field).max_length
                if len(value) > limit:
                    source = member.facts.get("contract" if field == "contract_no" else "item")
                    issues.append({"code": "field_too_long", "message": f"{field} 超过存储长度 {limit}",
                                   "sheet": member.packing_sheet, "row": member.packing_row,
                                   "cell": source.cell if source else "", "blocking": True})
    if not mapping.links:
        issues.append({"code": "no_declarations", "message": "申报页没有可导入商品", "sheet": mapping.declaration.layout.sheet,
                       "cell": f"A{mapping.declaration.layout.header_row}", "row": mapping.declaration.layout.header_row,
                       "blocking": True})
    if issues:
        raise ApiError(422, "parse_blocked", "模板含阻断问题，请按来源位置修正", {"issues": issues})
    return mapping


def upload(user, *, filename, data, key, task_id=None, expected_current_version_id=None, confirm_duplicate=False):
    authorize(user, task_id)
    validate_key(key)
    if not isinstance(data, bytes) or not data or len(data) > files.max_original_bytes():
        raise ApiError(422, "file_too_large" if data else "empty_file", "文件为空或超过上传上限")
    filename = storage.sanitize_filename(filename, fallback="原件.xlsx", allowed_suffixes=storage.ORIGINAL_SUFFIXES)
    digest = hashlib.sha256(data).hexdigest()
    payload = {"task_id": str(task_id) if task_id else None, "filename": filename, "sha256": digest,
               "expected_current_version_id": expected_current_version_id, "confirm_duplicate": confirm_duplicate}
    # 重试已登记的版本不受后来版本变化影响，但每次先校验归属。
    replay = idempotency.begin(user, ACTION, key, payload)
    if replay is not None:
        return finish_import(replay["import_id"], replay=True)
    mapping = parse(data, filename)
    staged = files.stage_version_original(data, filename=filename)
    reserved = False
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            replay = idempotency.begin(user, ACTION, key, payload)
            if replay is not None:
                job_id = replay["import_id"]
            else:
                task = authorize(user, task_id)
                if task is not None:
                    task = Task.objects.select_for_update().get(pk=task.pk)
                    latest = task.versions.order_by("-number").first()
                    if latest is None:
                        raise ApiError(409, "no_current_version", "该任务没有已上传版本，请使用首次上传入口")
                    if not expected_current_version_id or str(latest.pk) != expected_current_version_id:
                        raise ApiError(409, "stale_version", "当前版本已变化，请刷新后上传")
                    if ImportJob.objects.filter(version__task=task).exclude(status=ImportJob.Status.SUCCEEDED).exists():
                        raise ApiError(409, "import_incomplete", "已有未完成导入，请先重试原请求或恢复该导入")
                    duplicate = ImportJob.objects.filter(version__task=task, sha256=digest).order_by("-version__number").first()
                    if duplicate and not confirm_duplicate:
                        raise ApiError(409, "duplicate_file", "文件与已有版本相同，确认后可以创建新版本", {
                            "version_id": str(duplicate.version_id), "version_number": duplicate.version.number,
                            "sha256": digest, "requires_confirmation": True})
                    number = latest.number + 1
                    task.revision += 1
                    task.stage = Task.Stage.UPLOADED
                    task.save(update_fields=["revision", "stage", "updated_at"])
                else:
                    task = Task.objects.create(supplier=user, title=Path(filename).stem[:200])
                    number = 1
                version = FileVersion.objects.create(task=task, number=number, uploaded_by=user, original_filename=filename)
                run = Run.objects.create(task=task, version=version, kind=Run.Kind.PRECHECK, status=Run.Status.QUEUED)
                job = ImportJob.objects.create(version=version, precheck=run, sha256=digest, stage_token=staged.token)
                job_id = str(job.pk)
                AuditEvent.objects.create(task=task, version=version, actor=user, action="version.created",
                                          details={"number": number, "sha256": digest, "import_id": job_id, "result_mode": "mock"})
                idempotency.store(user, ACTION, key, payload, {"import_id": job_id})
                reserved = True
    finally:
        if not reserved:
            storage.discard_staged(files.storage_root(), staged.token)
    return finish_import(job_id, replay=replay is not None, mapping=mapping)


def finish_import(job_id, *, replay=False, mapping=None):
    """系统入口。调用方负责授权；CLI 仅供本机恢复。锁序 Task -> ImportJob -> Run。"""
    job = ImportJob.objects.select_related("version__task", "precheck").get(pk=job_id)
    try:
        with transaction.atomic():
            task = Task.objects.select_for_update().get(pk=job.version.task_id)
            job = ImportJob.objects.select_for_update().select_related("version", "precheck").get(pk=job.pk)
            if job.status != ImportJob.Status.SUCCEEDED:
                if task.versions.filter(number__gt=job.version.number).exists():
                    raise ApiError(409, "stale_version", "旧版本导入不能覆盖当前版本")
                version = files.publish_staged_version_original(job.version, job.stage_token, filename=job.version.original_filename)
                if mapping is None:
                    data = storage.read_bytes(files.storage_root(), version.storage_path, max_bytes=files.max_read_bytes())
                    mapping = parse(data, version.original_filename)
                # B 完整事务成功前不会有商品或行结果；失败回滚后复用同一版本/run。
                for link in mapping.links:
                    values = link.item_values()
                    values["attributes"]["excel_fields"] = {name: fact.as_dict() for name, fact in link.record.facts.items()}
                    item = Item.objects.create(version=version, **values)
                    ItemMember.objects.bulk_create([ItemMember(item=item, **v) for v in link.member_values()])
                layout = mapping.declaration.layout
                if "image" not in layout.columns:
                    # 图片列缺失/歧义无法证明无图，逐商品标记 read_failed，保留商品。
                    for item in version.items.all():
                        ItemImage.objects.create(item=item, source_sheet=item.source_sheet, source_cell=f"row:{item.source_row}",
                            status=ItemImage.Status.READ_FAILED, error_message="image_column_unresolved：附图列缺失或歧义，无法判定图片")
                else:
                    try:
                        files.extract_version_images_for_version(version, sheet_name=layout.sheet,
                            rows=[link.row for link in mapping.links], column=layout.columns["image"], header_row=layout.header_row)
                    except ApiError as exc:
                        if exc.status != 422 or not exc.code.startswith(("workbook_", "image_column_", "dispimg_")):
                            raise
                        # 图片部件整体损坏时仍保留商品，未知的逐行状态全部明确标读取失败。
                        for item in version.items.all():
                            ItemImage.objects.create(item=item, source_sheet=item.source_sheet,
                                source_cell=f"{layout.columns['image']}{item.source_row}",
                                status=ItemImage.Status.READ_FAILED, error_message=f"{exc.code}：图片资源读取失败，需核对原件")
                # 旧提交仅更新有效状态；快照、确认与决定本身不改动。
                Submission.objects.filter(task=task, status=Submission.Status.PENDING).exclude(version=version).update(status=Submission.Status.SUPERSEDED)
                job.precheck.status = Run.Status.QUEUED
                job.precheck.save(update_fields=["status"])
                run = execute_precheck(version, triggered_by=version.uploaded_by, run=job.precheck)
                job.report = mapping.as_dict()
                job.error = {}
                job.status = ImportJob.Status.SUCCEEDED
                job.save(update_fields=["report", "error", "status", "updated_at"])
                task.stage = Task.Stage.AWAITING_CONFIRMATION
                task.save(update_fields=["stage", "updated_at"])
                AuditEvent.objects.create(task=task, version=version, actor=version.uploaded_by, action="import.succeeded",
                                          details={"import_id": str(job.pk), "precheck_id": str(run.pk), "result_mode": "mock"})
    except Exception as exc:
        logger.exception("P06 import failed: %s", job_id)
        error = {"code": exc.code if isinstance(exc, ApiError) else "import_processing_failed",
                 "message": "导入处理失败，请重试原请求或联系维护人员",
                 "details": exc.details if isinstance(exc, ApiError) and exc.code == "parse_blocked" else {"exception_type": type(exc).__name__}}
        # 不把别的请求刚完成的成功状态回写为失败。
        with transaction.atomic():
            Task.objects.select_for_update().get(pk=job.version.task_id)
            current = ImportJob.objects.select_for_update().get(pk=job_id)
            if current.status == ImportJob.Status.SUCCEEDED:
                return import_response(current, replay=True)
            current.status, current.error = ImportJob.Status.FAILED, error
            current.save(update_fields=["status", "error", "updated_at"])
            Run.objects.filter(pk=current.precheck_id).update(status=Run.Status.FAILED, is_current=False, finished_at=timezone.now())
            AuditEvent.objects.create(task_id=job.version.task_id, version_id=job.version_id,
                                      actor_id=job.version.uploaded_by_id, action="import.failed",
                                      details={"import_id": str(job_id), "code": error["code"]})
        raise ApiError(500, "import_failed", "导入未完成，未标记解析成功；请用原幂等键重试", {
            "import_id": str(job_id), "task_id": str(job.version.task_id), "version_id": str(job.version_id),
            "status": "failed", "cause": error}) from exc
    # 成功提交后再清理回执；崩溃时重试/恢复也会到这里。
    try:
        storage.discard_staged(files.storage_root(), job.stage_token)
    except storage.StorageError as exc:
        if exc.code != "stage_not_found":
            logger.exception("P06 staging cleanup deferred: %s", job_id)
    return import_response(job, replay=replay)


def import_response(job, *, replay=False):
    version = job.version
    task = version.task
    images = ItemImage.objects.filter(item__version=version)
    return {"import_id": str(job.pk), "task_id": str(task.pk), "version_id": str(version.pk),
            "version_number": version.number, "parse_status": job.status, "sha256": job.sha256,
            "item_count": version.items.count(), "member_count": ItemMember.objects.filter(item__version=version).count(),
            "image_counts": {s: images.filter(status=s).count() for s in ItemImage.Status.values},
            "precheck_run_id": str(job.precheck_id), "precheck_status": Run.objects.get(pk=job.precheck_id).status,
            "result_mode": "mock", "executor_mode": "inline",
            "assignment_status": "claimed" if task.assignments.filter(role=Role.OPERATOR).exists() else "unclaimed",
            "original_download_url": f"/api/v1/files/{version.pk}", "idempotent_replay": replay}
