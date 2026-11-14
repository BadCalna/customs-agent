"""执行查询端点（P02 只读部分）。

重跑、重试与真实队列由 P13 实现；本文件不提供任何推进执行的入口。
"""
from django.http import Http404
from ninja import Router

from apps.accounts.auth import session_auth
from apps.accounts.permissions import READ, require_action

from .models import Run
from .schemas import RunDetail
from .serializers import run_to_detail

router = Router(tags=["分析执行"])


@router.get("/runs/{run_id}", response=RunDetail, auth=session_auth)
def get_run(request, run_id: str):
    try:
        run = Run.objects.select_related("task", "version").get(pk=run_id)
    except (Run.DoesNotExist, ValueError, TypeError) as exc:
        raise Http404("执行不存在或不在可见范围") from exc
    require_action(request.auth, READ, run)
    return run_to_detail(run)
