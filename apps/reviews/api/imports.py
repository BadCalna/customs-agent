"""真实 multipart 上传、认领和受控下载。所有写动作都使用 session+CSRF。"""
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from ninja import File, Form, Router, UploadedFile

from adapters.files import storage
from apps.accounts.auth import session_auth
from apps.accounts.errors import ApiError
from apps.accounts.permissions import READ, require_action
from apps.api.pagination import normalize_limit
from apps.reviews.import_schemas import ClaimablePage, ClaimOut, ImportDetail, ImportOut
from apps.reviews.models import FileVersion, ImportJob, ItemImage
from apps.reviews.services import files, imports
from apps.reviews.services.claims import claim_task, claimable_tasks

router = Router(tags=["上传与认领"])


def _upload(request, file, **kwargs):
    # 在读取上传体之前校验业务权限；限长读取，拒绝超限。
    imports.authorize(request.auth, kwargs.get("task_id"))
    data = file.read(files.max_original_bytes() + 1)
    try:
        return imports.upload(request.auth, filename=file.name, data=data,
                              key=request.headers.get("Idempotency-Key"), **kwargs)
    except storage.StorageError as exc:
        files._raise_storage_error(exc)
    except ApiError as exc:
        if exc.code == "storage_unavailable":
            raise ApiError(500, exc.code, "文件存储不可用，请检查空间和目录权限") from exc
        raise


def create_task(request, file: File[UploadedFile]):
    return _upload(request, file)


@router.post("/tasks/{task_id}/versions", response=ImportOut, auth=session_auth)
def create_version(request, task_id: str, file: File[UploadedFile],
                   expected_current_version_id: Form[str], confirm_duplicate: Form[bool] = False):
    return _upload(request, file, task_id=task_id, expected_current_version_id=expected_current_version_id,
                   confirm_duplicate=confirm_duplicate)


@router.get("/tasks/claimable", response=ClaimablePage, auth=session_auth)
def list_claimable(request, cursor: str | None = None, limit: int | None = None):
    from .read import _task_cursor
    from apps.api.pagination import encode_cursor
    size = normalize_limit(limit)
    queryset = claimable_tasks(request.auth).order_by("-created_at", "-id")
    position = _task_cursor(cursor)
    if position:
        queryset = queryset.filter(created_at__lte=position["created_at"]).exclude(
            created_at=position["created_at"], id__gte=position["id"])
    rows = list(queryset[:size + 1])
    page = rows[:size]
    next_cursor = encode_cursor({"created_at": page[-1].created_at.isoformat(), "id": str(page[-1].pk)}) if len(rows) > size else None
    return {"items": [{"task_id": str(t.pk), "created_at": t.created_at.isoformat()} for t in page],
            "next_cursor": next_cursor}


@router.post("/tasks/{task_id}/claim", response=ClaimOut, auth=session_auth)
def claim(request, task_id: str):
    return claim_task(request.auth, task_id, request.headers.get("Idempotency-Key"))


@router.get("/versions/{version_id}/import", response=ImportDetail, auth=session_auth)
def get_import(request, version_id: str):
    version = files._get_version(version_id)
    require_action(request.auth, READ, version)
    job = ImportJob.objects.filter(version=version).first()
    if job is None:
        raise Http404("该版本没有上传解析记录")
    return {**imports.import_response(job), "report": job.report, "error": job.error}


@router.get("/files/{file_id}", auth=session_auth)
def download(request, file_id: str):
    # 文件 ID 为版本 UUID 或图片 UUID；绝不接受任意相对路径。
    try:
        is_version = FileVersion.objects.filter(pk=file_id).exists()
        is_image = ItemImage.objects.filter(pk=file_id).exists()
    except (ValueError, TypeError, ValidationError):
        raise Http404("文件不存在或不在可见范围")
    try:
        if is_version:
            result = files.read_version_original(request.auth, file_id)
        elif is_image:
            result = files.read_item_image(request.auth, file_id)
        else:
            raise Http404("文件不存在或不在可见范围")
    except ApiError as exc:
        if exc.code in {"file_content_missing", "storage_unavailable"}:
            raise ApiError(exc.status, exc.code, "受控文件暂时不可读取，请联系维护人员") from exc
        raise
    response = HttpResponse(result.content, content_type=result.content_type)
    response["Content-Disposition"] = content_disposition_header(True, result.filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
