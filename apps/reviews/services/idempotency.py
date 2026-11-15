"""写动作幂等（D3 第 1 节）：数据库唯一约束 + 请求指纹，不靠进程内缓存。

用法（P09 提交、P10 决定/退回共用）：

    replay = begin(user, ACTION, key, payload)   # 已有记录 → 重放首次响应；内容不同 → 409
    if replay is not None:
        return replay
    ... 真正执行 ...
    store(user, ACTION, key, payload, response)

``payload`` 必须能 ``model_dump(mode="json")`` 或是可 JSON 序列化的 dict。
"""
import hashlib
import json

from apps.accounts.errors import ApiError

from ..models import IdempotencyRecord


def request_hash(payload) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def begin(user, action: str, key: str | None, payload) -> dict | None:
    """返回需重放的首次响应；无 key 或无记录返回 None。同 key 不同内容 → 409。"""
    if not key:
        return None
    record = IdempotencyRecord.objects.filter(user=user, action=action, key=key).first()
    if record is None:
        return None
    if record.request_hash != request_hash(payload):
        raise ApiError(
            409, "idempotency_conflict",
            "同一幂等键对应了不同的请求内容，请检查请求或更换幂等键",
            {"action": action},
        )
    response = dict(record.response or {})
    response["idempotent_replay"] = True
    return response


def store(user, action: str, key: str | None, payload, response: dict) -> None:
    """保存首次响应。键冲突时以数据库里已有的记录为准（唯一约束兜底）。"""
    if not key:
        return
    IdempotencyRecord.objects.get_or_create(
        user=user, action=action, key=key,
        defaults={"request_hash": request_hash(payload), "response": response},
    )
