"""P09 业务员确认 HS：追加式确认 + 修订冲突校验 + 修改留痕（D2、D3、R02）。

- 只新增 ``Confirmation`` 行，不改写供应商原值与 AI 建议；
- ``Item.revision`` / ``Task.revision`` 在事务内递增，供提交与后续编辑做 409 校验；
- 相同值、相同修订的重复提交是幂等重放，不产生新的确认行；
- 已通过的任务关闭编辑；已提交后的修改只影响下一次提交，旧快照不动。
"""
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from apps.accounts.errors import ApiError
from apps.accounts.permissions import CONFIRM, require_action

from ..models import AuditEvent, Confirmation, Item, Task

HS_PATTERN = re.compile(r"^\d{10}$")


def normalize_hs(value: str) -> str:
    """确认值必须是 10 位数字编码；空白与异常原值只属于供应商原文字段。"""
    hs = (value or "").strip()
    if not HS_PATTERN.match(hs):
        raise ApiError(
            422, "invalid_hs",
            "确认 HS 必须是 10 位数字编码；无法确定时请留空并退回补料，不要填占位值",
            {"field": "confirmed_hs"},
        )
    return hs


def get_item_in_version(version_id: str, item_id: str) -> Item:
    try:
        return Item.objects.select_related("version__task").get(pk=item_id, version_id=version_id)
    except (Item.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        # 商品不属于该版本、或 ID 非法，都按不可见处理，不泄露存在性。
        raise Http404("商品不存在或不在可见范围") from exc


def current_confirmation(item) -> Confirmation | None:
    return Confirmation.objects.filter(item=item).order_by("-revision").first()


@transaction.atomic
def write_confirmation(user, version_id: str, item_id: str, payload) -> tuple[Confirmation, Item, Task, bool]:
    """写入一次确认。返回 ``(确认记录, 商品, 任务, 是否幂等重放)``。"""
    item = get_item_in_version(version_id, item_id)
    task = require_action(user, CONFIRM, item)
    # 事务内重读并锁定：冲突判断必须基于最新修订（SQLite 上行锁为空操作，PG 由 P12 验证）。
    item = Item.objects.select_for_update().select_related("version__task").get(pk=item.pk)
    task = Task.objects.select_for_update().get(pk=task.pk)

    if task.stage == Task.Stage.APPROVED:
        raise ApiError(409, "task_closed", "任务已通过，不能再修改确认值；如需变更请走补料重传")
    if payload.expected_revision != item.revision:
        current = current_confirmation(item)
        raise ApiError(
            409, "stale_revision",
            "商品已被修改，请刷新后基于最新修订再提交",
            {
                "current_revision": item.revision,
                "current_confirmed_hs": current.confirmed_hs if current else None,
            },
        )

    confirmed_hs = normalize_hs(payload.confirmed_hs)
    current = current_confirmation(item)
    if current is not None and current.confirmed_hs == confirmed_hs:
        # 同值同修订：不新增确认行、不推进修订，避免重复点击制造假历史。
        return current, item, task, True

    confirmation = Confirmation(
        item=item,
        revision=item.revision + 1,
        previous_hs=current.confirmed_hs if current else "",
        confirmed_hs=confirmed_hs,
        reason=payload.reason or "",
        created_by=user,
    )
    confirmation.save()

    item.revision = confirmation.revision
    item.save(update_fields=["revision"])
    task.revision += 1
    task.stage = Task.Stage.AWAITING_CONFIRMATION
    task.save(update_fields=["revision", "stage", "updated_at"])

    AuditEvent.objects.create(
        task=task, version=item.version, item=item, actor=user,
        action="confirmation.updated", stage="business_confirmation",
        previous_value=confirmation.previous_hs, new_value=confirmation.confirmed_hs,
        reason=confirmation.reason,
        details={"confirmation_id": str(confirmation.pk), "item_revision": confirmation.revision},
    )
    return confirmation, item, task, False
