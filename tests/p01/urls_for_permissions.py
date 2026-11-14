"""测试专用 URLConf：用生产权限服务和统一错误处理器挂两个探针端点。

P01 不发布业务读写端点（属 P02 范围），但需要在 HTTP 层证明：
直接改 ID、跨租户读取、业务员越权批准都会得到 D3 约定的 JSON 错误，
而不是 500 或页面跳转。探针只在测试中使用，不进入生产路由。
"""
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.urls import path
from ninja import NinjaAPI

from apps.accounts.api import install_error_handlers
from apps.accounts.auth import session_auth
from apps.accounts.permissions import DECIDE, READ, require_action
from apps.reviews.models import Submission, Task

api = NinjaAPI(title="P01 权限探针", urls_namespace="perm-probe")
install_error_handlers(api)


def _get_or_404(model, object_id):
    try:
        obj = model.objects.filter(pk=object_id).first()
    except (ValidationError, ValueError) as exc:
        raise Http404("对象不存在或不在可见范围") from exc
    if obj is None:
        raise Http404("对象不存在或不在可见范围")
    return obj


@api.get("/perm/tasks/{task_id}", auth=session_auth)
def read_task(request, task_id: str):
    task = _get_or_404(Task, task_id)
    require_action(request.auth, READ, task)
    return JsonResponse({"task_id": str(task.pk), "title": task.title})


@api.post("/perm/submissions/{submission_id}/decision", auth=session_auth)
def decide(request, submission_id: str):
    submission = _get_or_404(Submission, submission_id)
    require_action(request.auth, DECIDE, submission)
    return JsonResponse({"submission_id": str(submission.pk), "decided": True})


urlpatterns = [path("probe/", api.urls)]
