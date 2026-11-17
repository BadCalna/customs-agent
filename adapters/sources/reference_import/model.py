"""官方参考数据导入流水线的公共模型（P23）。

定位
----
本模块只定义“导入过程中的事实与状态”，不读文件、不转换业务字段、不写输出。
调用方通过这里的枚举区分：**来源未提供 / 无法解析 / 记录被拒绝 / 记录待核 /
记录成功导入**，避免把“没读到”“读坏了”“被拒绝了”“导入成功”混成一个布尔值。

诚实边界
--------
- 退出码、报告状态、记录状态是三套独立的表达，不允许只靠一句日志文字下结论；
- 真实批次与合成批次用 ``DatasetKind`` 分开，合成批次不允许发布；
- 所有哈希都是对**原件字节**计算的，导入过程只读不写原件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: 转换器版本。业务输出指纹包含它：只要转换语义变化就必须改这个值，
#: 否则“输入相同 → 输出相同”的承诺不再成立。
CONVERTER_VERSION = "p23-import-2026.11.3"

#: 来源清单与复核文件的结构版本。
SOURCES_SCHEMA_VERSION = "p23.sources.1"
REVIEW_SCHEMA_VERSION = "p23.review.1"
SNAPSHOT_SCHEMA_VERSION = "p23.snapshot.1"
RELEASE_SCHEMA_VERSION = "p23.release.1"

#: 品目注释（税则子目解释说明）独立快照的结构版本。
COMMENTARY_SCHEMA_VERSION = "p23.commentary.1"


class DatasetKind(str, Enum):
    """批次性质。合成批次只用于测试与联调，不允许发布成业务来源。"""

    REAL = "real"
    SYNTHETIC = "synthetic"


class DatasetStatus(str, Enum):
    """一份数据类别在本批次的可用状态；直接对应“未提供/未支持”的明示要求。"""

    PROVIDED = "provided"
    """来源已提供并被本流水线消费。"""

    NOT_PROVIDED = "not_provided"
    """用户尚未提供该类官方资料；不是解析失败，也不能用空白补齐。"""

    UNSUPPORTED = "unsupported"
    """来源存在结构性问题或本流水线不支持该形态（必须写清原因）。"""

    INPUT_MISSING = "input_missing"
    """清单里登记了原件，但本机取不到（例如原件不在 Git 中）。"""


class RecordStatus(str, Enum):
    """单条业务记录的处理结果。"""

    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    """与已导入记录同业务键且内容一致：不产生重复记录。"""

    CONFLICT = "conflict"
    """同业务键但内容不同：报告冲突，保留先出现的一条并转待核。"""

    PENDING = "pending"
    """字段有缺口/来源待核，记录保留但带明确限制说明。"""

    REJECTED = "rejected"
    """记录不得进入快照（编码失真、字段非法、结构未知等）。"""

    NOT_IMPORTED = "not_imported"
    """来源声明了但本流水线不导入（含“未支持”说明）。"""


class ExitCode(int, Enum):
    """CLI 退出码。成功 / 部分拒绝 / 失败必须能从退出码区分。"""

    SUCCESS = 0
    PARTIAL = 1
    FAILED = 2


class InputKind(str, Enum):
    """输入原件的形态。"""

    HS_ELEMENT_WORKBOOK = "hs_element_mapping_workbook"
    TAX_RATE_WORKBOOK = "hs_element_tax_rate_workbook"
    COMMENTARY_JSON = "commentary_json"
    COMMENTARY_WORKBOOK = "commentary_workbook"


class DatasetId(str, Enum):
    """本流水线产出的数据类别；与 P14/P17 的存储文件一一对应。"""

    P14_TARIFF = "p14_tariff"
    P14_ELEMENTS = "p14_elements"
    P17_TAX_RATES = "p17_tax_rates"
    P17_REGULATORY = "p17_regulatory"
    TARIFF_COMMENTARY = "tariff_commentary"
    REGULATORY_CODE_LEDGER = "regulatory_code_ledger"
    """只有监管条件代码、没有方向/措施/有效期时的原始台账（不进入 P17）。"""

    @property
    def filename(self) -> str:
        return {
            DatasetId.P14_TARIFF: "tariff.json",
            DatasetId.P14_ELEMENTS: "elements.json",
            DatasetId.P17_TAX_RATES: "tax_rates.json",
            DatasetId.P17_REGULATORY: "regulatory.json",
            DatasetId.TARIFF_COMMENTARY: "commentary.json",
            DatasetId.REGULATORY_CODE_LEDGER: "regulatory_codes.json",
        }[self]


#: 写入快照的数据类别顺序固定，保证报告与指纹可复现。
DATASET_ORDER: tuple[DatasetId, ...] = (
    DatasetId.P14_TARIFF,
    DatasetId.P14_ELEMENTS,
    DatasetId.P17_TAX_RATES,
    DatasetId.REGULATORY_CODE_LEDGER,
    DatasetId.P17_REGULATORY,
    DatasetId.TARIFF_COMMENTARY,
)

#: 用于“无法定位来源就拒绝”的不可见字符清理（与 P14/P17 同口径）。
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Zs", "Zl", "Zp", "Mn", "Mc", "Me"})
_VARIATION = re.compile("[\ufe00-\ufe0f\U000e0100-\U000e01ef]")
_HANGUL_FILLER = {0x115F, 0x1160, 0x3164, 0xFFA0}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now(zone_offset_hours: int = 8) -> str:
    """本机演示环境使用 +08；只用于报告时间，不参与业务内容。"""

    from datetime import timedelta, timezone as _tz

    return datetime.now(_tz(timedelta(hours=zone_offset_hours))).isoformat(timespec="seconds")


def strip_invisible(value: Any) -> str:
    """去掉不可见字符后返回文本；用于判断来源定位是否真的写了东西。"""

    text = "" if value is None else str(value)
    kept: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        if char in _HANGUL_FILLER:
            continue
        if _VARIATION.match(char):
            continue
        if unicodedata.category(char) in _INVISIBLE_CATEGORIES and char != " ":
            continue
        kept.append(char)
    return "".join(kept).strip()


def visible_text(value: Any) -> str:
    """保留正常空白、去不可见字符的文本；用于与 P14/P17 的定位校验对齐。"""

    text = "" if value is None else str(value)
    kept = [char for char in text if not _VARIATION.match(char) and char not in _HANGUL_FILLER]
    kept = [
        char
        for char in kept
        if char == " " or unicodedata.category(char) not in _INVISIBLE_CATEGORIES
    ]
    return "".join(kept).strip()


def clean_source_text(value: Any) -> str:
    """业务文本清洗：只去不可见字符，保留换行/制表与全部可见内容。

    与 :func:`visible_text` 的区别：那个用于“定位是否真的写了字”，会把换行也去掉；
    业务字段（商品名称、要素名、条件原文）必须保留换行，否则等于改写来源。
    """

    text = "" if value is None else str(value)
    kept: list[str] = []
    for char in text:
        if char in "\n\t":
            kept.append(char)
            continue
        if _VARIATION.match(char) or char in _HANGUL_FILLER:
            continue
        category = unicodedata.category(char)
        if category in {"Cf", "Cc", "Cs", "Co", "Cn", "Zl", "Zp"}:
            continue
        if char == "\u00a0":
            kept.append(" ")
            continue
        kept.append(char)
    return "".join(kept).strip()


def parse_iso_date(value: Any) -> date | None:
    """只接受 ISO 日期/时间；解析失败返回 ``None``，绝不截断后猜测。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00")).date()
    except ValueError:
        return None


def is_iso_datetime(value: Any) -> bool:
    """校验带时区的 ISO 时间（复核人、取得时间等要求可核对的时刻）。"""

    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return False
    return moment.tzinfo is not None


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    """稳定的 JSON 序列化：排序键、紧凑分隔符、保留中文原文。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> Any:  # pragma: no cover - 仅防御未知类型
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.name
    raise TypeError(f"无法序列化 {type(value).__name__}")


def fingerprint(payload: Any) -> str:
    return sha256_text(canonical_json(payload))


def write_json_atomic(path: str | Path, payload: Any) -> Path:
    """临时文件 + ``os.replace``：写到一半失败不会损坏既有文件。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{id(payload)}")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    return target


def write_text_atomic(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{id(text)}")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    return target


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class RecordIssue:
    """一条记录级问题：必须能被报告复述，不需要读日志。"""

    code: str
    message: str
    field: str = ""
    severity: str = "error"
    """error=记录被拒绝；pending=记录保留但待核；info=仅提示。"""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        payload["severity"] = self.severity
        return payload


@dataclass
class RecordOutcome:
    """单条业务记录的处理结果，供报告逐条列出原因。"""

    dataset: DatasetId
    status: RecordStatus
    business_key: str
    locator: str
    reason: str = ""
    issues: list[RecordIssue] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset": self.dataset.value,
            "status": self.status.value,
            "business_key": self.business_key,
            "locator": self.locator,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.issues:
            payload["issues"] = [issue.as_dict() for issue in self.issues]
        return payload


@dataclass
class DatasetOutcome:
    """一份数据类别的汇总：数量必须能与报告、快照自洽核对。"""

    dataset: DatasetId
    status: DatasetStatus
    reason: str = ""
    input_unit: str = ""
    """输入计数的单位（税号行 / 要素行 / 退税率行 / 注释条目），报告里必须说明，
    否则“总数”无法与“输出记录数”对账。"""
    input_total: int = 0
    imported: int = 0
    duplicate: int = 0
    conflict: int = 0
    pending: int = 0
    rejected: int = 0
    not_imported: int = 0
    output_records: int = 0
    fingerprint: str = ""
    assumptions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def count(self, status: RecordStatus, amount: int = 1) -> None:
        if status is RecordStatus.IMPORTED:
            self.imported += amount
        elif status is RecordStatus.DUPLICATE:
            self.duplicate += amount
        elif status is RecordStatus.CONFLICT:
            self.conflict += amount
        elif status is RecordStatus.PENDING:
            self.pending += amount
        elif status is RecordStatus.REJECTED:
            self.rejected += amount
        elif status is RecordStatus.NOT_IMPORTED:
            self.not_imported += amount
        else:  # pragma: no cover - 枚举已覆盖
            raise ValueError(f"未知记录状态：{status}")

    @property
    def ok(self) -> bool:
        """没有拒绝、没有冲突、没有缺失输入才算完全成功。"""

        return (
            self.status is DatasetStatus.PROVIDED
            and not self.rejected
            and not self.conflict
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset": self.dataset.value,
            "status": self.status.value,
            "input_unit": self.input_unit,
            "counts": {
                "input_total": self.input_total,
                "imported": self.imported,
                "duplicate": self.duplicate,
                "conflict": self.conflict,
                "pending": self.pending,
                "rejected": self.rejected,
                "not_imported": self.not_imported,
                "output_records": self.output_records,
            },
            "fingerprint": self.fingerprint,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.assumptions:
            payload["assumptions"] = list(self.assumptions)
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


@dataclass(frozen=True)
class SourceFile:
    """一份已登记的原件。``sha256``/``bytes`` 在读取前后都要核对。"""

    source_id: str
    kind: InputKind
    path: Path
    sha256: str
    size: int
    roles: tuple[str, ...] = ()
    declared_sha256: str | None = None
    declared_bytes: int | None = None
    retrieved_at: str | None = None
    recorded_source: Mapping[str, Any] = field(default_factory=dict)
    original_filename: str = ""
    notes: str = ""
    in_git: bool = True
    declared_path: str = ""
    """清单里登记的仓库相对路径；报告用它，不写本机绝对路径。"""
    local_path_hint: str | None = None

    @property
    def hash_verified(self) -> bool:
        """清单声明了哈希就必须逐字相符；没声明时只记录实测值。"""

        return self.declared_sha256 is None or self.declared_sha256 == self.sha256

    @property
    def size_verified(self) -> bool:
        return self.declared_bytes is None or self.declared_bytes == self.size

    @property
    def verified(self) -> bool:
        return self.hash_verified and self.size_verified

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "repository_path": (self.declared_path or self.path.name) if self.in_git else None,
            "local_path": None if self.in_git else self.path.name,
            "sha256": self.sha256,
            "bytes": self.size,
            "declared_sha256": self.declared_sha256,
            "hash_verified": self.hash_verified,
            "size_verified": self.size_verified,
            "roles": list(self.roles),
            "retrieved_at": self.retrieved_at,
            "original_filename": self.original_filename or self.path.name,
            "notes": self.notes,
        }
        if self.local_path_hint:
            payload["local_path_hint"] = self.local_path_hint
        if self.recorded_source:
            payload["recorded_source"] = dict(self.recorded_source)
        return payload


def summarize_counts(outcomes: Iterable[DatasetOutcome]) -> dict[str, int]:
    """把多份数据类别的计数合并成报告顶部的总数。"""

    totals = {
        "input_total": 0,
        "imported": 0,
        "duplicate": 0,
        "conflict": 0,
        "pending": 0,
        "rejected": 0,
        "not_imported": 0,
        "output_records": 0,
    }
    for outcome in outcomes:
        totals["input_total"] += outcome.input_total
        totals["imported"] += outcome.imported
        totals["duplicate"] += outcome.duplicate
        totals["conflict"] += outcome.conflict
        totals["pending"] += outcome.pending
        totals["rejected"] += outcome.rejected
        totals["not_imported"] += outcome.not_imported
        totals["output_records"] += outcome.output_records
    return totals


def exit_code_for(
    outcomes: Sequence[DatasetOutcome], *, failed: bool = False
) -> ExitCode:
    """成功 / 部分拒绝 / 失败三态：拒绝或冲突或缺失输入都不算成功。"""

    if failed:
        return ExitCode.FAILED
    if not outcomes:
        return ExitCode.FAILED
    partial = False
    for outcome in outcomes:
        if outcome.status in (DatasetStatus.INPUT_MISSING, DatasetStatus.UNSUPPORTED):
            partial = True
        if outcome.rejected or outcome.conflict or outcome.not_imported:
            partial = True
    if partial:
        return ExitCode.PARTIAL
    return ExitCode.SUCCESS


def render_counts_line(totals: Mapping[str, int]) -> str:
    """报告摘要行；数量必须能与逐条明细对账。"""

    return (
        "input={input_total} imported={imported} duplicate={duplicate} conflict={conflict} "
        "pending={pending} rejected={rejected} not_imported={not_imported} "
        "output={output_records}".format(**totals)
    )


__all__ = [
    "COMMENTARY_SCHEMA_VERSION",
    "CONVERTER_VERSION",
    "DATASET_ORDER",
    "DatasetId",
    "DatasetKind",
    "DatasetOutcome",
    "DatasetStatus",
    "ExitCode",
    "InputKind",
    "RELEASE_SCHEMA_VERSION",
    "REVIEW_SCHEMA_VERSION",
    "RecordIssue",
    "RecordOutcome",
    "RecordStatus",
    "SNAPSHOT_SCHEMA_VERSION",
    "SOURCES_SCHEMA_VERSION",
    "SourceFile",
    "canonical_json",
    "clean_source_text",
    "exit_code_for",
    "fingerprint",
    "is_iso_datetime",
    "local_now",
    "parse_iso_date",
    "read_json",
    "render_counts_line",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "strip_invisible",
    "summarize_counts",
    "utc_now",
    "visible_text",
    "write_json_atomic",
    "write_text_atomic",
]
