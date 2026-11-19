from typing import Literal
from ninja import Schema


class MessageOut(Schema):
    message_id: str
    task_id: str | None
    version_id: str | None
    submission_id: str | None
    decision_id: str | None
    kind: str
    title: str
    body: str
    is_read: bool
    read_at: str | None
    created_at: str
    target_url: str | None


class MessagePage(Schema):
    items: list[MessageOut]
    next_cursor: str | None = None
    unread_count: int = 0


class MessageReadIn(Schema):
    is_read: Literal[True]

    model_config = {"extra": "forbid"}
