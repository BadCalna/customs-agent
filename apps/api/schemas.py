"""契约 v1 公共结构：枚举、分页、错误、源位置、阶段值、问题与证据。

对应 D3 第 1/3 节、D4 第 2/3 节。本模块是这些字段的唯一代码来源，
``contracts/schemas/*.json`` 由它导出，执行 Agent 不得另写一套枚举。

约定：
- 未产生的阶段值一律为 ``null``，不用其他阶段的值补位；
- ``comparison_status`` 为 ``null`` 表示尚未得出结论，与六类结论区分；
- 时间字段为带时区的 ISO 8601 字符串。
"""
from enum import Enum
from typing import Any

from ninja import Schema
from pydantic import Field

CONTRACT_VERSION = "1.0.0"


# --- 枚举：与数据库模型保持一致的取值由 tests/p02 做防漂移校验 ---

class ResultMode(str, Enum):
    """结果来源模式。mock 结果必须在页面上可识别，不冒充真实引擎结论。"""

    MOCK = "mock"
    LIVE = "live"


class TaskStage(str, Enum):
    UPLOADED = "uploaded"
    PRECHECKING = "prechecking"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    RETURNED = "returned"


class SubmissionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    RETURNED = "returned"
    SUPERSEDED = "superseded"


class RunKind(str, Enum):
    PRECHECK = "precheck"
    EXPERT_CHECK = "expert_check"


class RunStatus(str, Enum):
    """技术执行状态。部分行失败由 ``progress.failed`` 表达，不改写本状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ComparisonStatus(str, Enum):
    """D4 第 2 节六类比较结论，优先级按声明顺序递减。"""

    ASSOCIATION_ERROR = "association_error"
    SUPPLIER_MISSING = "supplier_missing"
    SUPPLIER_INVALID = "supplier_invalid"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"


class EvidenceStatus(str, Enum):
    """逐来源的证据取得状态；查询失败与确实未查得必须分开。"""

    OBTAINED = "obtained"
    NOT_FOUND = "not_found"
    QUERY_FAILED = "query_failed"
    VERSION_UNVERIFIED = "version_unverified"


class ImageStatus(str, Enum):
    AVAILABLE = "available"
    ABSENT = "absent"
    READ_FAILED = "read_failed"


class ExpertVerdict(str, Enum):
    """专家分项核查结论；unknown 不等于无风险。"""

    MATCHED = "matched"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class Action(str, Enum):
    """``allowed_actions`` 的取值，与服务层权限常量一一对应。"""

    READ = "read"
    VERSION_CREATE = "version.create"
    CONFIRMATION_WRITE = "confirmation.write"
    SUBMISSION_CREATE = "submission.create"
    DECISION_RESOLVE = "decision.resolve"


# --- 错误与分页 ---

class ErrorBody(Schema):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(Schema):
    """D3 第 1 节统一错误体；HTTP 状态码由响应头承载。"""

    error: ErrorBody
    request_id: str


class NotImplementedOut(Schema):
    """未实现端点的显式应答，绝不伪造成功。"""

    error: ErrorBody
    request_id: str


# --- 卡片公共片段 ---

class SourceLocation(Schema):
    """商品行在原 Excel 中的位置；行号为 1 起始。"""

    sheet_name: str
    row_number: int = Field(ge=1)
    cells: str = ""


class StageValue(Schema):
    """某一编码阶段的值及其来源引用。未产生时两个字段都为 null。"""

    value: str | None = None
    source_id: str | None = Field(
        default=None, description="产生该值的 run / submission / decision 标识"
    )


class IssueEntry(Schema):
    """补料或异常问题，定位到具体商品与字段。"""

    code: str
    message: str
    field: str | None = None
    missing: str | None = Field(default=None, description="缺少什么事实或资料")
    suggested_role: str | None = Field(
        default=None, description="建议由哪个角色补充；最终责任人由人工决定"
    )


class EvidenceEntry(Schema):
    """一条可定位的依据。查询无结果时不生成虚构条款。"""

    evidence_id: str
    source_type: str
    title: str
    status: EvidenceStatus
    url: str | None = None
    document: str | None = None
    locator: str | None = Field(default=None, description="条款、页码或章节定位")
    excerpt: str | None = None
    retrieved_at: str | None = None
    issued_at: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    scope: str | None = None
    query: dict[str, Any] | None = None
    note: str | None = None


class ImageEntry(Schema):
    """商品附图；读取失败与确无图片分开，并保留来源单元格。"""

    image_id: str
    status: ImageStatus
    source_sheet: str
    source_cell: str
    dispimg_id: str = ""
    download_url: str | None = Field(
        default=None, description="受控下载路由，不暴露服务器绝对路径"
    )
    error_message: str = ""


class ExpertFinding(Schema):
    """专家核查的一个分项意见，针对的是业务员提交的那个 HS。"""

    aspect: str = Field(description="elements / images / tax / regulatory")
    verdict: ExpertVerdict
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ExpertCheckRef(Schema):
    """卡片上标注最近一次专家核查针对的是哪个提交码。"""

    run_id: str
    submission_id: str
    target_hs: str
    findings: list[ExpertFinding] = Field(default_factory=list)


class RunProgress(Schema):
    """执行内各商品的当前状态计数；等待补料不计入 failed。"""

    total: int = Field(ge=0)
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
