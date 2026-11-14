"""P10 只读历史：按版本/提交/决定分组，附跨版本候选对照与审计事件。

GET 无副作用；跨版本对照只是候选展示，不推断同一商品身份（D2）。
"""
from ninja import Router

from apps.accounts.auth import session_auth

from ..schemas import TaskHistoryOut
from ..services.versions import task_history

router = Router(tags=["版本历史"])


@router.get("/tasks/{task_id}/history", response=TaskHistoryOut, auth=session_auth)
def get_task_history(request, task_id: str):
    return task_history(request.auth, task_id)
