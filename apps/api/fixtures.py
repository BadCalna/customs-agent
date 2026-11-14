"""固定样例文档结构（D3 第 4 节 FIX-01～FIX-10）。

每份样例自描述：场景号、契约版本、说明，以及该场景涉及的卡片、执行、
分页或错误响应。前端 fixture 模式直接读取这些文件，与 live 模式同一 schema。
"""
from ninja import Schema
from pydantic import Field

from apps.reviews.schemas import ItemCard, ItemListEntry, TaskDetail, TaskPage
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


class DemoSet(Schema):
    """fixture 模式的完整数据集：与 live 端点返回同构，供 web 演示壳离线使用。

    ``items`` 按版本 ID 索引，``cards`` 按商品 ID 索引，``runs`` 按执行 ID 索引；
    不保存生成时间，使同一份演示数据重复导出字节一致（漂移检查依赖这一点）。
    """

    contract_version: str = CONTRACT_VERSION
    tasks: list[TaskDetail] = Field(default_factory=list)
    items: dict[str, list[ItemListEntry]] = Field(default_factory=dict)
    cards: dict[str, ItemCard] = Field(default_factory=dict)
    runs: dict[str, RunDetail] = Field(default_factory=dict)
