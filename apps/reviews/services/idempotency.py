"""写动作幂等：先在业务事务内预占唯一键，再写副作用与首次响应。

请求身份包含操作者、动作、目标对象和请求内容：同一幂等键被用于**另一个对象**
时返回 409，而不是把上一个对象的响应重放给新对象。
调用方必须在同一 ``transaction.atomic`` 内调用 ``begin`` / ``store``。
"""
import hashlib
import json

from apps.accounts.errors import ApiError

from ..models import IdempotencyRecord


def request_hash(payload, target_id: str) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    raw = json.dumps(
        {"target_id": str(target_id), "payload": payload},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _conflict(action):
    return ApiError(
        409, "idempotency_conflict",
        "同一幂等键对应了不同的请求对象或内容，请检查请求或更换幂等键",
        {"action": action},
    )


def begin(user, action: str, key: str | None, payload, *, target_id: str) -> dict | None:
    """预占键位：已完成则重放首次响应；必须早于任何业务副作用调用。"""
    if not key:
        return None
    if len(key) > 128:
        raise ApiError(422, "invalid_idempotency_key", "幂等键不能超过 128 个字符")
    fingerprint = request_hash(payload, target_id)
    record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
        user=user, action=action, key=key,
        defaults={"request_hash": fingerprint, "response": {}},
    )
    if created:
        return None
    if record.request_hash != fingerprint:
        raise _conflict(action)
    if not record.response:
        # 竞争者不会读到未提交的预占记录；读到空响应说明是重入或历史未完成记录。
        raise ApiError(409, "idempotency_in_progress", "该幂等请求尚未完成，请稍后重试")
    return {**record.response, "idempotent_replay": True}


def store(user, action: str, key: str | None, payload, response: dict, *, target_id: str) -> None:
    if not key:
        return
    record = IdempotencyRecord.objects.select_for_update().get(user=user, action=action, key=key)
    if record.request_hash != request_hash(payload, target_id):
        raise _conflict(action)
    if record.response and record.response != response:
        raise _conflict(action)
    record.response = response
    record.save(update_fields=["response"])
