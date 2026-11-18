"""P06 HTTP 契约，生成物与既有 v1 契约共同发布。"""
from typing import Any, Literal
from ninja import Schema
from pydantic import Field


class ImportOut(Schema):
    import_id: str
    task_id: str
    version_id: str
    version_number: int
    parse_status: Literal["staged", "succeeded", "failed", "superseded"]
    sha256: str
    item_count: int
    member_count: int
    image_counts: dict[str, int]
    precheck_run_id: str
    precheck_status: str
    result_mode: Literal["mock"]
    executor_mode: Literal["inline", "rq"]
    assignment_status: Literal["claimed", "unclaimed"]
    original_download_url: str
    idempotent_replay: bool = False


class ImportDetail(ImportOut):
    error: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] = Field(default_factory=dict)


class ClaimableEntry(Schema):
    task_id: str
    created_at: str
    assignment_status: Literal["unclaimed"] = "unclaimed"
    # 故意不包含供应商、原件名、标题、商品、图片或下载 URL。


class ClaimablePage(Schema):
    items: list[ClaimableEntry]
    next_cursor: str | None = None


class ClaimOut(Schema):
    task_id: str
    operator_id: str
    claimed_at: str
    assignment_status: Literal["claimed"]
    idempotent_replay: bool = False
