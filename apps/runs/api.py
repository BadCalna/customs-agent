"""执行查询端点（P02 只读部分）。

重跑、重试与真实队列由 P13 实现；本文件不提供任何推进执行的入口。
"""
from django.core.exceptions import ValidationError
from django.http import Http404
from ninja import Router

from apps.accounts.auth import session_auth
from apps.accounts.permissions import READ, require_action

from .models import Run
from .schemas import PrecheckIn, RetryIn, RunCreated, RunDetail
from .serializers import run_to_detail

router = Router(tags=["分析执行"])


@router.get("/runs/{run_id}", response=RunDetail, auth=session_auth)
def get_run(request, run_id: str):
    try:
        run = Run.objects.select_related("task", "version").get(pk=run_id)
    except (Run.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        # 非法 UUID 与不存在同形返回 404，不把取值错误暴露成 500。
        raise Http404("执行不存在或不在可见范围") from exc
    require_action(request.auth, READ, run)
    return run_to_detail(run, user=request.auth)


@router.post("/versions/{version_id}/prechecks", response=RunCreated, auth=session_auth)
def create_precheck(request, version_id: str, payload: PrecheckIn):
    from .services import start_precheck
    from apps.reviews.services.claims import validate_key
    key = request.headers.get("Idempotency-Key")
    validate_key(key)
    return start_precheck(request.auth, version_id, payload.revision, key)


@router.post("/runs/{run_id}/retries", response=RunCreated, auth=session_auth)
def create_retry(request, run_id: str, payload: RetryIn):
    from .services import retry_run
    from apps.reviews.services.claims import validate_key
    key = request.headers.get("Idempotency-Key")
    validate_key(key)
    return retry_run(request.auth, run_id, payload.item_ids, key)
