"""Defensive HTTP transport used only by the agent debug worker."""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any

import httpx

from .access import setting
from .errors import DebugError
from .security import Endpoint, serialize_request

_MAX_RESPONSE = 64 * 1024
_DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class TransportResult:
    text: str
    http_status: int
    model: str | None = None
    provider_request_id: str | None = None
    usage: dict[str, int] | None = None


def _timeout() -> float:
    try:
        value = float(setting("OPS_DEBUG_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return max(0.1, min(value, _DEFAULT_TIMEOUT)) if math.isfinite(value) else _DEFAULT_TIMEOUT


def _safe_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10**12:
            result[key] = item
    return result or None


def _decode_envelope(raw: bytes, *, expected_model: str, http_status: int) -> tuple[str, str, dict[str, int] | None]:
    def unique_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    try:
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, TypeError, RecursionError):
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, http_status=http_status, received=True) from None
    if not isinstance(envelope, dict):
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, http_status=http_status, received=True) from None
    response_model = envelope.get("model")
    if response_model != expected_model:
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, http_status=http_status, received=True) from None
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, http_status=http_status, received=True) from None
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, http_status=http_status, received=True) from None
    return message["content"], response_model, _safe_usage(envelope.get("usage"))


def send(endpoint: Endpoint, body: bytes, key: str | None = None) -> TransportResult:
    """Send exactly one fixed-protocol request; never follows redirects or proxies."""
    if not isinstance(endpoint, Endpoint) or not isinstance(body, bytes):
        raise DebugError("INVALID_INPUT", 422) from None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        raise DebugError("INVALID_INPUT", 422) from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("model"), str):
        raise DebugError("INVALID_INPUT", 422) from None
    if parsed["model"] not in endpoint.models:
        raise DebugError("ENDPOINT_NOT_ALLOWED", 409) from None
    # Re-run canonical serialization to ensure callers cannot inject a mutable packet.
    canonical = serialize_request(parsed["model"], parsed)
    if canonical != body:
        raise DebugError("INVALID_INPUT", 422) from None
    if endpoint.auth_mode == "bearer" and (not isinstance(key, str) or not key):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    if endpoint.auth_mode == "none" and key is not None:
        raise DebugError("ENDPOINT_NOT_ALLOWED", 409) from None

    headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}
    if endpoint.auth_mode == "bearer":
        headers["Authorization"] = "Bearer " + key  # key never returned or logged
    return asyncio.run(_send_once(endpoint, body, headers, parsed["model"]))


async def _send_once(endpoint, body, headers, expected_model):
    timeout = _timeout()
    status = None
    try:
        # A wall-clock deadline bounds connect, headers and slow streaming
        # together; a per-read timeout alone lets a trickling peer hold a slot.
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, http2=False) as client:
                async with client.stream("POST", endpoint.url, content=body, headers=headers) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise DebugError("AUTH_REJECTED", 502, http_status=status, received=True)
                    if status == 429:
                        raise DebugError("PROVIDER_LIMIT", 429, http_status=status, received=True)
                    if not 200 <= status < 300:
                        raise DebugError("PROVIDER_REJECTED", 502, http_status=status, received=True)
                    encoding = response.headers.get("content-encoding")
                    if encoding and encoding.lower() != "identity":
                        raise DebugError("PROVIDER_REJECTED", 502, http_status=status, received=True)
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > _MAX_RESPONSE:
                            raise DebugError("RESPONSE_TOO_LARGE", 502, http_status=status, received=True, truncated=True)
                        raw.extend(chunk)
        text, model, usage = _decode_envelope(raw, expected_model=expected_model, http_status=status)
        # Do not trust arbitrary response headers to be identifiers: a provider
        # could echo an authorization value in an otherwise plausible ID.
        return TransportResult(text=text, http_status=status, model=model, usage=usage)
    except (httpx.TimeoutException, TimeoutError):
        raise DebugError("TIMEOUT", 504, http_status=status, received=status is not None) from None
    except (httpx.HTTPError, OSError):
        raise DebugError("ENDPOINT_UNREACHABLE", 502, http_status=status, received=status is not None) from None
