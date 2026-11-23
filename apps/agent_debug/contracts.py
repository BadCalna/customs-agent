from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from adapters.engines.precheck import FACT_FIELDS


class Strict(Schema):
    model_config = ConfigDict(extra="forbid")


Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60,
                                        pattern=r"^[\w .-]+$")]
AttributeValue = Annotated[str, StringConstraints(max_length=200)]


class ConnectionIn(Strict):
    alias: Alias
    endpoint_id: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=160)
    auth_mode: Literal["none", "bearer"]
    api_key: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    copy_secret_from: UUID | None = None

    @model_validator(mode="after")
    def secrets(self):
        if self.auth_mode == "none" and (self.api_key or self.copy_secret_from):
            raise ValueError("credential not allowed")
        if self.auth_mode == "bearer" and bool(self.api_key) == bool(self.copy_secret_from):
            raise ValueError("one credential source required")
        return self


class DefaultIn(Strict):
    expected_default_version: int = Field(ge=0, le=2**63 - 1, strict=True)


class Facts(Strict):
    name: str = Field(min_length=1, max_length=200)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict, max_length=20)
    elements_text: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def nonblank(cls, v):
        if not v.strip():
            raise ValueError("blank name")
        return v

    @field_validator("attributes")
    @classmethod
    def allowed_attributes(cls, v):
        if not set(v) <= FACT_FIELDS:
            raise ValueError("attribute not allowed")
        return v


class Basis(Strict):
    declaration_date: date | None = None
    tariff_version: str | None = Field(default=None, min_length=1, max_length=24,
                                        pattern=r"^[0-9]{4}[A-Za-z]?$" )


class PreviewIn(Strict):
    mode: Literal["mock", "live"]
    sample_id: str | None = Field(default=None, min_length=1, max_length=32)
    facts: Facts | None = None
    query_basis: Basis | None = None
    parent_run_id: UUID | None = None

    @model_validator(mode="after")
    def one_source(self):
        if sum(v is not None for v in (self.sample_id, self.facts, self.parent_run_id)) != 1:
            raise ValueError("one input source required")
        if self.facts is None and self.query_basis is not None:
            raise ValueError("cannot modify pinned sample basis")
        return self


class RunIn(Strict):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    confirm_external: StrictBool = False
    parent_run_id: UUID | None = None


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def input_hash(value) -> str:
    return digest(json_bytes(value))


def idempotency_hash(key):
    from .errors import DebugError

    if not isinstance(key, str) or not re.fullmatch(r"[!-~]{16,128}", key):
        raise DebugError("INVALID_INPUT", 422)
    return digest(key)
