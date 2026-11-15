"""P09 业务员确认端点：PATCH /versions/{version_id}/items/{item_id}/confirmation。

权限与 CSRF 由公共认证器和 ``require_action`` 统一处理；本层只做输入输出。
"""
from ninja import Router

from apps.accounts.auth import session_auth

from ..schemas import ConfirmationIn, ConfirmationOut
from ..serializers import iso
from ..services.confirmations import write_confirmation

router = Router(tags=["业务员确认"])


@router.patch(
    "/versions/{version_id}/items/{item_id}/confirmation",
    response=ConfirmationOut, auth=session_auth,
)
def confirm_item(request, version_id: str, item_id: str, payload: ConfirmationIn):
    confirmation, item, task, replay = write_confirmation(request.auth, version_id, item_id, payload)
    return ConfirmationOut(
        confirmation_id=str(confirmation.pk),
        task_id=str(task.pk),
        version_id=str(item.version_id),
        item_id=str(item.pk),
        revision=item.revision,
        task_revision=task.revision,
        previous_hs=confirmation.previous_hs,
        confirmed_hs=confirmation.confirmed_hs,
        reason=confirmation.reason,
        created_by=confirmation.created_by.display_name or confirmation.created_by.username,
        created_at=iso(confirmation.created_at),
        task_stage=task.stage,
        idempotent_replay=replay,
    )
