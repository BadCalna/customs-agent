"""写动作幂等：先在业务事务内预占唯一键，再写副作用与首次响应。

请求身份包含操作者、动作、目标对象和请求内容：同一幂等键被用于**另一个对象**
时返回 409，而不是把上一个对象的响应重放给新对象。
调用方必须在同一 ``transaction.atomic`` 内调用 ``begin`` / ``store``。

升级前创建的记录使用纯 payload 哈希。这里保留旧指纹读取，并在事务写入路径
升级为带 target_id 的新指纹；旧记录没有目标列，因此只有请求或响应能证明目标
一致时才接受纯 payload 指纹，避免兼容逻辑重新引入跨对象重放。
"""
import hashlib
import json
from collections.abc import Mapping

from django.db import transaction

from apps.accounts.errors import ApiError

from ..models import IdempotencyRecord

_TARGET_KEYS = {"target_id", "task_id", "submission_id"}


def _json_payload(payload):
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload


def _hash_payload(payload) -> str:
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def legacy_request_hash(payload) -> str:
    """升级前的纯 payload 指纹；仅用于读取/迁移既有记录。"""
    return _hash_payload(_json_payload(payload))


def request_hash(payload, target_id: str) -> str:
    return _hash_payload({"target_id": str(target_id), "payload": _json_payload(payload)})


def _conflict(action):
    return ApiError(
        409, "idempotency_conflict",
        "同一幂等键对应了不同的请求对象或内容，请检查请求或更换幂等键",
        {"action": action},
    )


def _mentions_target(value, target_id: str) -> bool:
    """旧记录没有 target_id 列，只能从原请求/响应中核对其原有目标。"""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _TARGET_KEYS and str(item) == target_id:
                return True
            if _mentions_target(item, target_id):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_mentions_target(item, target_id) for item in value)
    return False


def _matches(record, payload, target_id: str) -> bool:
    normalized = _json_payload(payload)
    if record.request_hash == request_hash(normalized, target_id):
        return True

    # main 的 P06 集成版曾把 task_id 合入提交 payload 后再做纯 payload 哈希。
    if target_id and isinstance(normalized, Mapping) and "task_id" not in normalized:
        bound_legacy = legacy_request_hash({"task_id": target_id, **normalized})
        if record.request_hash == bound_legacy:
            return True

    if record.request_hash != legacy_request_hash(normalized):
        return False
    # 创建动作尚无既有目标（target_id=""）时可按原请求重放；其它动作必须能
    # 从旧请求或已保存响应证明仍是同一个对象，否则要求换新幂等键。
    return not target_id or _mentions_target(normalized, target_id) or _mentions_target(
        record.response, target_id
    )


def _replay(record, payload, target_id: str, *, migrate: bool) -> dict:
    if not _matches(record, payload, target_id):
        raise _conflict(record.action)
    if migrate and record.request_hash != request_hash(payload, target_id):
        record.request_hash = request_hash(payload, target_id)
        record.save(update_fields=["request_hash"])
    if not record.response:
        # 竞争者不会读到未提交的预占记录；读到空响应说明是重入或历史未完成记录。
        raise ApiError(409, "idempotency_in_progress", "该幂等请求尚未完成，请稍后重试")
    return {**record.response, "idempotent_replay": True}


def _validate_key(key: str) -> None:
    if len(key) > 128:
        raise ApiError(422, "invalid_idempotency_key", "幂等键不能超过 128 个字符")


def get_replay(user, action: str, key: str | None, payload, *, target_id: str) -> dict | None:
    """只读预检：已有完成响应则重放；绝不创建预占，可在事务外调用。"""
    if not key:
        return None
    _validate_key(key)
    record = IdempotencyRecord.objects.filter(user=user, action=action, key=key).first()
    if record is None:
        return None
    return _replay(record, payload, target_id, migrate=False)


def begin(user, action: str, key: str | None, payload, *, target_id: str) -> dict | None:
    """预占键位：已完成则重放首次响应；必须早于任何业务副作用调用。"""
    if not key:
        return None
    _validate_key(key)
    fingerprint = request_hash(payload, target_id)
    record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
        user=user, action=action, key=key,
        defaults={"request_hash": fingerprint, "response": {}},
    )
    if created:
        return None
    return _replay(record, payload, target_id, migrate=True)


def store(user, action: str, key: str | None, payload, response: dict, *, target_id: str) -> None:
    if not key:
        return
    _validate_key(key)
    fingerprint = request_hash(payload, target_id)
    # 保留旧 store 可独立使用的契约；正常写动作仍应先 begin 再 store。
    # 独立调用时自开事务，select_for_update 在 PostgreSQL 上不能裸用在自动提交中。
    with transaction.atomic():
        record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
            user=user, action=action, key=key,
            defaults={"request_hash": fingerprint, "response": response},
        )
        if created:
            return
        if not _matches(record, payload, target_id):
            raise _conflict(action)
        if record.response and record.response != response:
            raise _conflict(action)
        record.request_hash = fingerprint
        record.response = response
        record.save(update_fields=["request_hash", "response"])
