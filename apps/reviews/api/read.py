"""P02 只读查询端点：任务、商品列表与商品卡片。

- 全部 ``auth=session_auth``，匿名 401、越权 404，由公共认证器与权限服务统一处理；
- GET 无副作用：不创建执行、不推进任务阶段、不触发 mock 计算；
- 分页形状固定为 ``{items, next_cursor}``。
"""
from django.http import Http404
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
    except (queryset.model.DoesNotExist, ValueError, TypeError) as exc:
        raise Http404("对象不存在或不在可见范围") from exc


@router.get("/tasks", response=TaskPage, auth=session_auth)
def list_tasks(request, stage: str | None = None, cursor: str | None = None, limit: int | None = None):
    limit = normalize_limit(limit)
    queryset = visible_tasks(request.auth).select_related("supplier").order_by("-created_at", "-id")
    if stage:
        if stage not in Task.Stage.values:
            raise ApiError(400, "invalid_stage", "未知的任务阶段", {"allowed": list(Task.Stage.values)})
        queryset = queryset.filter(stage=stage)
    position = decode_cursor(cursor)
    if position:
        queryset = queryset.filter(created_at__lte=position.get("created_at"))
        queryset = queryset.exclude(created_at=position.get("created_at"), id__gte=position.get("id"))
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
    position = decode_cursor(cursor)
    if position:
        queryset = queryset.filter(
            source_sheet__gte=position.get("sheet", ""),
        ).exclude(
            source_sheet=position.get("sheet", ""), source_row__lte=position.get("row", 0),
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


# 首次上传由 P06 实现。它与 GET /tasks 同路径，必须挂在同一 router，
# 否则 Ninja 先注册的路径视图会把 POST 判为 405 而不是显式 501。
router.api_operation(["POST"], "/tasks", auth=session_auth)(pending_view("POST", "/tasks", "P06"))
