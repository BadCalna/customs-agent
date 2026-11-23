"""Security primitives and fixed endpoint policy for the agent debug transport.

This module deliberately has no dependency on Django models.  Connection secrets may be
wrapped with the application master key, but durable secret references are kept in a
separate local secret-store directory (never in the project/DB sync tree).  Trace/data
ciphertexts use an independent data key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import DebugError


_ALLOWED_AUTH = frozenset({"none", "bearer"})
_ALLOWED_TARGET = frozenset({"local", "external"})
_LOOPBACK = frozenset({"127.0.0.1", "::1"})
_REF_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SECRET_FILE_LIMIT = 8192


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: str
    url: str
    models: tuple[str, ...]
    auth_mode: str
    target: str
    label: str

    def __post_init__(self) -> None:
        if not self.id or not self.label or not self.models:
            raise ValueError("invalid endpoint definition")
        if self.auth_mode not in _ALLOWED_AUTH or self.target not in _ALLOWED_TARGET:
            raise ValueError("invalid endpoint policy")
        _validate_url(self.url, self.target)
        if urlsplit(self.url).scheme == "http" and self.auth_mode != "none":
            raise ValueError("HTTP loopback endpoints cannot use bearer authentication")
        if any(not isinstance(m, str) or not m or len(m) > 200 for m in self.models):
            raise ValueError("invalid endpoint model")

    @property
    def fingerprint(self) -> str:
        policy = {"id": self.id, "url": self.url, "models": self.models,
                  "auth_mode": self.auth_mode, "target": self.target, "label": self.label}
        return hashlib.sha256(_json(policy)).hexdigest()


def _setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        return value.strip() or default
    try:
        from django.conf import settings
        value = getattr(settings, name, None)
        return str(value).strip() if value is not None and str(value).strip() else default
    except Exception:
        return default


def _raw_setting(name: str, default: Any = None) -> Any:
    if name in os.environ:
        return os.environ[name]
    try:
        from django.conf import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_url(url: str, target: str) -> None:
    try:
        p = urlsplit(url)
    except Exception as exc:
        raise ValueError("invalid endpoint URL") from exc
    host = p.hostname
    if (not host or p.username is not None or p.password is not None or p.query or p.fragment
            or p.scheme not in {"http", "https"} or not p.path.startswith("/")
            or any(not char.isprintable() or char.isspace() or char == "\\" for char in url)):
        raise ValueError("endpoint URL must be an absolute URL without credentials/query")
    # Access port here to reject malformed/out-of-range configuration early.
    if p.port == 0:
        raise ValueError("invalid endpoint port")
    is_literal_loopback = host in _LOOPBACK
    if p.scheme == "http" and (not is_literal_loopback or target != "local"):
        raise ValueError("HTTP is permitted only for literal loopback endpoints")
    if (target == "local") != is_literal_loopback:
        raise ValueError("only local endpoints may use literal loopback")


def _parse_endpoints() -> dict[str, Endpoint]:
    raw = _raw_setting("OPS_DEBUG_ENDPOINTS")
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(value, Mapping):
            value = [dict(item, id=key) for key, item in value.items()]
        if not isinstance(value, list):
            raise ValueError
        result = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError
            ep = Endpoint(id=str(item["id"]), url=str(item["url"]),
                          models=tuple(str(x) for x in item["models"]),
                          auth_mode=str(item.get("auth_mode", "none")),
                          target=str(item["target"]), label=str(item.get("label", item["id"])))
            if ep.id in result:
                raise ValueError
            result[ep.id] = ep
        return result
    except Exception as exc:
        raise DebugError("ENDPOINT_NOT_ALLOWED", 409) from None


def endpoints() -> dict[str, Endpoint]:
    """Return the immutable, administrator-provided endpoint registry."""
    return dict(_parse_endpoints())


def get_endpoint(endpoint_id: str, model: str | None = None, auth_mode: str | None = None) -> Endpoint:
    ep = endpoints().get(endpoint_id)
    if ep is None or (model is not None and model not in ep.models) or (auth_mode is not None and auth_mode != ep.auth_mode):
        raise DebugError("ENDPOINT_NOT_ALLOWED", 409)
    return ep


def _decode_key(name: str) -> bytes | None:
    raw = _setting(name)
    if not raw:
        return None
    try:
        if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
            return bytes.fromhex(raw)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", raw):
            return None
        key = base64.b64decode(raw.rstrip("=") + "=", altchars=b"-_", validate=True)
        return key if len(key) == 32 else None
    except ValueError:
        return None


def _key_pair() -> tuple[bytes | None, bytes | None]:
    master = _decode_key("OPS_DEBUG_MASTER_KEY")
    data = _decode_key("OPS_DEBUG_DATA_KEY")
    if master is not None and data is not None and secrets.compare_digest(master, data):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    return master, data


def _aead_seal(value: bytes, key: bytes | None, aad: bytes) -> str:
    if key is None:
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, value, aad)
    return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")


def _aead_open(ciphertext: str, key: bytes | None, aad: bytes, unavailable: str) -> bytes:
    if key is None:
        raise DebugError(unavailable, 409) from None
    if not isinstance(ciphertext, str) or not ciphertext.startswith("v1."):
        raise DebugError(unavailable, 409) from None
    try:
        raw = base64.urlsafe_b64decode(ciphertext[3:] + "=" * (-len(ciphertext[3:]) % 4))
        if len(raw) < 28:
            raise ValueError
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    except (ValueError, TypeError, InvalidTag):
        raise DebugError(unavailable, 409) from None


def seal_secret(value: str, revision: str) -> str:
    try:
        rev = str(uuid.UUID(str(revision)))
    except (ValueError, AttributeError, TypeError):
        raise DebugError("INVALID_INPUT", 422) from None
    if not isinstance(value, str) or len(value) > 16384:
        raise DebugError("INVALID_INPUT", 422) from None
    return _aead_seal(value.encode("utf-8"), _key_pair()[0], ("conn:" + rev).encode())


def open_secret(ciphertext: str, revision: str) -> str:
    try:
        rev = str(uuid.UUID(str(revision)))
    except (ValueError, AttributeError, TypeError):
        raise DebugError("INVALID_INPUT", 422) from None
    try:
        return _aead_open(ciphertext, _key_pair()[0], ("conn:" + rev).encode(), "SECRET_UNAVAILABLE").decode("utf-8")
    except UnicodeDecodeError:
        raise DebugError("SECRET_UNAVAILABLE", 409) from None


def seal_data(value: Any, context: str) -> str:
    if not isinstance(context, str) or not context or len(context) > 300:
        raise DebugError("INVALID_INPUT", 422) from None
    try:
        encoded = _json(value)
    except (TypeError, ValueError, OverflowError):
        raise DebugError("INVALID_INPUT", 422) from None
    if len(encoded) > 2 * 1024 * 1024:
        raise DebugError("INVALID_INPUT", 422) from None
    return _aead_seal(encoded, _key_pair()[1], ("data:" + context).encode())


def open_data(ciphertext: str, context: str) -> Any:
    if not isinstance(context, str) or not context or len(context) > 300:
        raise DebugError("INVALID_INPUT", 422) from None
    raw = _aead_open(ciphertext, _key_pair()[1], ("data:" + context).encode(), "DATA_UNAVAILABLE")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        raise DebugError("DATA_UNAVAILABLE", 409) from None


def _secret_root() -> Path:
    raw = _setting("OPS_DEBUG_SECRET_STORE")
    if not raw:
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    path = configured.resolve()
    text = str(path)
    try:
        from django.conf import settings
        base_dir = Path(getattr(settings, "BASE_DIR")).resolve()
        if path == base_dir or base_dir in path.parents:
            raise DebugError("SECRET_UNAVAILABLE", 409) from None
    except DebugError:
        raise
    except Exception:
        pass
    forbidden = ("/OneDrive", "/CloudStorage/", "/.git/", "/worktrees/")
    if any(marker in text for marker in forbidden):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        access = path.stat()
        if hasattr(os, "geteuid") and (access.st_uid != os.geteuid() or access.st_mode & 0o077):
            raise DebugError("SECRET_UNAVAILABLE", 409)
    except OSError:
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    return path


def persist_secret(ciphertext: str) -> str:
    """Persist an already sealed connection ciphertext outside the project tree."""
    if not isinstance(ciphertext, str) or not ciphertext.startswith("v1."):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    root = _secret_root()
    ref = secrets.token_urlsafe(24)
    path = root / ref
    try:
        fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=str(root))
    except OSError:
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(ciphertext)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    return ref


def load_secret(ref: str) -> str:
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    try:
        path = _secret_root() / ref
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="ascii") as fh:
            access = os.fstat(fh.fileno())
            if (not stat.S_ISREG(access.st_mode) or access.st_mode & 0o077
                    or access.st_uid != os.geteuid()):
                raise DebugError("SECRET_UNAVAILABLE", 409)
            value = fh.read(_SECRET_FILE_LIMIT + 1)
    except (OSError, UnicodeError):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    if len(value) > _SECRET_FILE_LIMIT or not value.startswith("v1."):
        raise DebugError("SECRET_UNAVAILABLE", 409) from None
    return value


def serialize_request(model: str, packet: Mapping[str, Any]) -> bytes:
    """Canonicalize the adapter's OpenAI-compatible packet for preview and send."""
    if not isinstance(model, str) or not model or not isinstance(packet, Mapping):
        raise DebugError("INVALID_INPUT", 422) from None
    try:
        body = json.loads(json.dumps(packet, ensure_ascii=False))
    except (TypeError, ValueError):
        raise DebugError("INVALID_INPUT", 422) from None
    if not isinstance(body, dict):
        raise DebugError("INVALID_INPUT", 422) from None
    supplied = body.get("model")
    if supplied is not None and supplied != model:
        raise DebugError("ENDPOINT_NOT_ALLOWED", 409) from None
    body["model"] = model
    response_format = body.get("response_format")
    if response_format is not None and response_format != {"type": "json_object"}:
        raise DebugError("PROVIDER_REJECTED", 422) from None
    body["response_format"] = {"type": "json_object"}
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise DebugError("INVALID_INPUT", 422) from None
    return _json(body)
