"""P10 专家处置与业务员退回端点。

- ``POST /submissions/{submission_id}/decisions``：专家整批通过或退回；
- ``POST /tasks/{task_id}/returns``：业务员把问题退回供应商补料。
两者都支持 ``Idempotency-Key``，已处置提交返回 409，不重复产生有效决定。
"""
from ninja import Router

from apps.accounts.auth import session_auth

from ..schemas import DecisionIn, DecisionOut, TaskReturnIn, TaskReturnOut
from ..services.decisions import decide
from ..services.returns import return_version

router = Router(tags=["专家处置与退回"])


@router.post("/submissions/{submission_id}/decisions", response=DecisionOut, auth=session_auth)
def decide_submission(request, submission_id: str, payload: DecisionIn):
    return decide(request.auth, submission_id, payload, request.headers.get("Idempotency-Key") or None)


@router.post("/tasks/{task_id}/returns", response=TaskReturnOut, auth=session_auth)
def return_task_version(request, task_id: str, payload: TaskReturnIn):
    return return_version(request.auth, task_id, payload, request.headers.get("Idempotency-Key") or None)
