"""不透明游标分页。

游标只是排序键的编码，不承载权限；每次翻页仍重新校验可见范围。
"""
import base64
import json

from apps.accounts.errors import ApiError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "分页游标无法解析") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_cursor", "分页游标格式不正确")
    return payload


def normalize_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiError(400, "invalid_limit", f"limit 须在 1～{MAX_LIMIT} 之间")
    return limit
