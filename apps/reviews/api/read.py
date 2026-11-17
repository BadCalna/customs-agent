"""P02 只读查询端点：任务、商品列表与商品卡片。

- 全部 ``auth=session_auth``，匿名 401、越权 404，由公共认证器与权限服务统一处理；
- GET 无副作用：不创建执行、不推进任务阶段、不触发 mock 计算；
- 分页形状固定为 ``{items, next_cursor}``。
"""
import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils.dateparse import parse_datetime
from ninja import Router

from apps.accounts.auth import session_auth
from apps.accounts.errors import ApiError
from apps.accounts.permissions import READ, get_visible_task, require_action, visible_tasks
from apps.api.not_implemented import pending_view
from apps.api.pagination import decode_cursor, encode_cursor, normalize_limit

from ..models import FileVersion, Item, Task
from ..schemas import ItemCard, ItemPage, TaskDetail, TaskPage
from ..serializers import iso, item_to_card, item_to_list_entry, task_to_detail, task_to_entry

router = Router(tags=["审核查询"])


def _get_or_404(queryset, **lookup):
    try:
        return queryset.get(**lookup)
    except (queryset.model.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        # ValidationError 来自非法 UUID 等取值转换；对外与不存在同形，避免 500。
        raise Http404("对象不存在或不在可见范围") from exc


def _task_cursor(cursor: str | None) -> dict | None:
    """校验任务列表游标：时间必须可解析为带时区的 ISO 8601，ID 必须是 UUID。

    游标对外是不透明字符串，但仍可被篡改；类型不对时返回 400，不能让取值异常
    变成 500，也不能把伪造游标当成分页参数直接拼进查询。
    """

    position = decode_cursor(cursor)
    if not position:
        return None
    created_at, task_id = position.get("created_at"), position.get("id")
    if not isinstance(created_at, str) or not isinstance(task_id, str):
        raise ApiError(400, "invalid_cursor", "分页游标格式不正确")
    parsed = parse_datetime(created_at)
    if parsed is None or parsed.tzinfo is None:
        raise ApiError(400, "invalid_cursor", "分页游标中的时间格式不正确")
    try:
        uuid.UUID(task_id)
    except ValueError as exc:
        raise ApiError(400, "invalid_cursor", "分页游标中的任务标识不正确") from exc
    return {"created_at": parsed, "id": task_id}


def _item_cursor(cursor: str | None) -> dict | None:
    position = decode_cursor(cursor)
    if not position:
        return None
    sheet, row = position.get("sheet"), position.get("row")
    if not isinstance(sheet, str) or not isinstance(row, int) or isinstance(row, bool) or row < 1:
        raise ApiError(400, "invalid_cursor", "分页游标格式不正确")
    return {"sheet": sheet, "row": row}


@router.get("/tasks", response=TaskPage, auth=session_auth)
def list_tasks(request, stage: str | None = None, cursor: str | None = None, limit: int | None = None):
    limit = normalize_limit(limit)
    queryset = visible_tasks(request.auth).select_related("supplier").order_by("-created_at", "-id")
    if stage:
        if stage not in Task.Stage.values:
            raise ApiError(400, "invalid_stage", "未知的任务阶段", {"allowed": list(Task.Stage.values)})
        queryset = queryset.filter(stage=stage)
    position = _task_cursor(cursor)
    if position:
        queryset = queryset.filter(created_at__lte=position["created_at"])
        queryset = queryset.exclude(created_at=position["created_at"], id__gte=position["id"])
    rows = list(queryset[: limit + 1])
    page, rows = rows[:limit], rows[limit:]
    next_cursor = None
    if rows and page:
        last = page[-1]
        next_cursor = encode_cursor({"created_at": iso(last.created_at), "id": str(last.pk)})
    return TaskPage(items=[task_to_entry(request.auth, task) for task in page], next_cursor=next_cursor)


@router.get("/tasks/{task_id}", response=TaskDetail, auth=session_auth)
def get_task(request, task_id: str):
    task = get_visible_task(request.auth, task_id)
    return task_to_detail(request.auth, task)


@router.get("/versions/{version_id}/items", response=ItemPage, auth=session_auth)
def list_items(request, version_id: str, cursor: str | None = None, limit: int | None = None):
    limit = normalize_limit(limit)
    version = _get_or_404(FileVersion.objects.select_related("task"), pk=version_id)
    require_action(request.auth, READ, version)
    queryset = version.items.order_by("source_sheet", "source_row")
    position = _item_cursor(cursor)
    if position:
        queryset = queryset.filter(
            source_sheet__gte=position["sheet"],
        ).exclude(
            source_sheet=position["sheet"], source_row__lte=position["row"],
        )
    rows = list(queryset[: limit + 1])
    page, rows = rows[:limit], rows[limit:]
    next_cursor = None
    if rows and page:
        last = page[-1]
        next_cursor = encode_cursor({"sheet": last.source_sheet, "row": last.source_row})
    return ItemPage(items=[item_to_list_entry(item) for item in page], next_cursor=next_cursor)


@router.get("/versions/{version_id}/items/{item_id}", response=ItemCard, auth=session_auth)
def get_item_card(request, version_id: str, item_id: str):
    version = _get_or_404(FileVersion.objects.select_related("task"), pk=version_id)
    require_action(request.auth, READ, version)
    item = _get_or_404(Item.objects.select_related("version__task"), pk=item_id, version=version)
    return item_to_card(request.auth, item)


# 与 GET /tasks 共用 router，避免 Ninja 跨 router 同路径返回 405。
from .imports import create_task
from ..import_schemas import ImportOut
router.post("/tasks", response=ImportOut, auth=session_auth)(create_task)
