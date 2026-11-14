"""固定样例文档结构（D3 第 4 节 FIX-01～FIX-10）。

每份样例自描述：场景号、契约版本、说明，以及该场景涉及的卡片、执行、
分页或错误响应。前端 fixture 模式直接读取这些文件，与 live 模式同一 schema。
"""
from ninja import Schema
from pydantic import Field

from apps.reviews.schemas import ItemCard, TaskPage
from apps.runs.schemas import RunDetail

from .schemas import CONTRACT_VERSION, ErrorEnvelope


class FixtureDoc(Schema):
    scenario: str = Field(pattern=r"^FIX-\d{2}$")
    title: str
    contract_version: str = CONTRACT_VERSION
    notes: str = ""
    card: ItemCard | None = None
    related_cards: list[ItemCard] = Field(default_factory=list)
    run: RunDetail | None = None
    task_page: TaskPage | None = None
    error: ErrorEnvelope | None = None
