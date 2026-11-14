"""P09 整批提交端点：POST /tasks/{task_id}/submissions。

- ``Idempotency-Key`` 头持久化去重：同 key 同内容重放首次响应，同 key 不同内容 409；
- 提交成功后在同事务内登记专家核查待运行记录（R13），inline 执行器随后执行。
"""
from ninja import Router

from apps.accounts.auth import session_auth

from ..schemas import SubmissionIn, SubmissionOut
from ..services.submissions import create_submission

router = Router(tags=["业务员提交"])


@router.post("/tasks/{task_id}/submissions", response=SubmissionOut, auth=session_auth)
def submit_task(request, task_id: str, payload: SubmissionIn):
    key = request.headers.get("Idempotency-Key") or None
    return create_submission(request.auth, task_id, payload, key)
