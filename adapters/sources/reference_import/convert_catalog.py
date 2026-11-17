"""HS 目录、申报要素与监管条件代码台账的转换（P23）。

输入是用户已取得的「HS 编码—申报要素映射」工作簿；输出是 P14 的
``p14.tariff.1`` / ``p14.elements.1`` 两份 JSON，以及一份**不进入 P17** 的
监管条件代码台账（来源只有字母代码，没有方向、措施、证件与有效期）。

字段映射的硬规则
----------------
1. **编码按字符串处理**：只接受文本单元格，或长度无损的整数单元格；
   ``0101210010`` 的前导零必须原样保留。9 位数字、科学计数法、浮点尾数
   一律拒绝，不补零、不猜位。
2. **8 位与 10 位分开**：8 位是税目/子目，10 位是申报编码；不按前缀补成 10 位。
3. **必填标记只认 1/0**：``必填标记（1/0）`` 是来源对必填性的声明，1→required、
   0→optional；出现其它值整份清单拒绝，不猜成“条件必填”。
4. **没有的东西保持没有**：来源没有条款顺序字段时，``order`` 只用行序并标注；
   来源没有条件说明时，不写 ``condition``、不伪造 ``[]``。
5. **监管条件代码不解释**：只保存原文串与定位，不拆码、不补方向、不写
   ``measures=[]``，也不把空串当成“无监管”。

版本口径
--------
来源没有声明税则公告的生效日/失效日（批次说明明确不把归档日补成生效日），
所以版本窗口的起点使用**来源文件内记录的采集时间**（换算到本机时区后的日期），
仅表示“这批数据从该日之后可核对”，并在 ``label`` 与报告假设里写清楚。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from adapters.sources.reference_import.model import (
    DatasetId,
    DatasetKind,
    DatasetOutcome,
    DatasetStatus,
    InputKind,
    RecordIssue,
    RecordOutcome,
    RecordStatus,
    SourceFile,
    clean_source_text,
    fingerprint,
    parse_iso_date,
    visible_text,
)
from adapters.sources.reference_import.sources import (
    HeaderLayout,
    HeaderSpec,
    LayoutError,
    SourceManifest,
    WorkbookAccess,
    normalize_header,
    open_workbook,
)

#: 允许的必填标记取值 → P14 的 requirement。
REQUIREMENT_BY_FLAG = {"1": "required", "0": "optional"}

#: 编码长度 → 含义。8 位是税目/子目，10 位是申报编码，其它长度一律拒绝。
CODE_LENGTHS = {8: "subheading", 10: "declaration"}

INDEX_SPEC = HeaderSpec(
    label="HS 编码索引",
    required=("HS编码", "商品名称", "适用前缀"),
    optional=(
        "第一计量单位",
        "第二计量单位",
        "监管条件代码",
        "申报要素数",
        "必填数",
        "非必填数",
        "匹配状态",
    ),
)

ELEMENT_SPEC = HeaderSpec(
    label="申报要素明细",
    required=("HS编码", "商品名称", "适用前缀", "申报要素"),
    optional=("HS来源页", "要素来源页"),
)

#: 申报要素明细的必填标记列，表头可能带“（1/0）”后缀，按前缀匹配。
FLAG_HEADER_PREFIX = "必填标记"

_ELEMENT_ORDER_NOTE = "order 取来源行序，仅用于稳定排序与定位，不代表官方申报顺序"


@dataclass(frozen=True)
class Versioning:
    """版本口径：默认值可被来源清单的 ``defaults`` 覆盖，全部写进报告假设。"""

    tariff_version: str = "2026"
    tariff_effective_from: str = "2026-11-16"
    elements_version: str = "2026"
    elements_effective_from: str = "2026-11-16"
    tax_version: str = "2026C"
    commentary_version: str = "2026-11-17T10-20"
    dataset_prefix: str = "customs-reference"

    @classmethod
    def from_manifest(cls, manifest: SourceManifest) -> "Versioning":
        defaults = manifest.defaults
        base = cls()
        versioning = cls(
            tariff_version=defaults.get("tariff_version", base.tariff_version),
            tariff_effective_from=defaults.get(
                "tariff_effective_from", base.tariff_effective_from
            ),
            elements_version=defaults.get("elements_version", base.elements_version),
            elements_effective_from=defaults.get(
                "elements_effective_from", base.elements_effective_from
            ),
            tax_version=defaults.get("tax_version", base.tax_version),
            commentary_version=defaults.get(
                "commentary_version", base.commentary_version
            ),
            dataset_prefix=defaults.get("dataset_prefix", base.dataset_prefix),
        )
        for field_name, value in (
            ("tariff_effective_from", versioning.tariff_effective_from),
            ("elements_effective_from", versioning.elements_effective_from),
        ):
            if parse_iso_date(value) is None:
                raise ValueError(f"版本起始日期不是 ISO 日期：{field_name}={value!r}")
        for field_name, value in (
            ("tariff_version", versioning.tariff_version),
            ("elements_version", versioning.elements_version),
            ("tax_version", versioning.tax_version),
        ):
            if not value:
                raise ValueError(f"版本号不能为空：{field_name}")
        if versioning.dataset_prefix.endswith("-"):
            raise ValueError("dataset_prefix 不能以 '-' 结尾")
        return versioning

    def dataset_id(self, dataset: DatasetId) -> str:
        return f"{self.dataset_prefix}-{dataset.value.replace('_', '-')}"


# ---------------------------------------------------------------------------
# 编码与单元格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeRead:
    """一次编码读取结果；``issue`` 为 ``None`` 表示可用。"""

    code: str = ""
    level: str = ""
    issue: RecordIssue | None = None
    recovered_from_numeric: bool = False
    raw_text: str = ""

    @property
    def ok(self) -> bool:
        return self.issue is None and bool(self.code)


def read_hs_code(value: str, kind: str) -> CodeRead:
    """把单元格读成编码字符串；不做任何补零/纠位/截断。

    - 文本单元格：NFKC 后去空白，必须全数字且长度 8 或 10；
    - 数值单元格：只有十进制整数串长度正好 8/10 时才采用（此时不存在前导零
      丢失的可能），其余一律按“精度已损坏”拒绝。
    """

    text = visible_text(unicodedata.normalize("NFKC", value or ""))
    if not text:
        return CodeRead(issue=RecordIssue("HS_CODE_EMPTY", "编码单元格为空", "HS编码"))
    if kind in ("inlineStr", "s", "str"):
        if not text.isdigit():
            return CodeRead(
                issue=RecordIssue(
                    "HS_CODE_NONDIGIT",
                    f"编码不是纯数字：{text!r}（保留原文，不修补）",
                    "HS编码",
                ),
                raw_text=text,
            )
        level = CODE_LENGTHS.get(len(text))
        if level is None:
            return CodeRead(
                issue=RecordIssue(
                    "HS_CODE_WRONG_LENGTH",
                    f"编码长度 {len(text)} 位，本流水线只接受 8 位子目或 10 位申报编码：{text!r}",
                    "HS编码",
                ),
                raw_text=text,
            )
        return CodeRead(code=text, level=level, raw_text=text)
    if kind == "n":
        if not text.isdigit():
            return CodeRead(
                issue=RecordIssue(
                    "HS_CELL_NUMERIC_PRECISION_LOST",
                    f"编码存成了数值 {text!r}（含小数/指数），精度可能已损坏，不猜测修复",
                    "HS编码",
                ),
                raw_text=text,
            )
        level = CODE_LENGTHS.get(len(text))
        if level is None:
            return CodeRead(
                issue=RecordIssue(
                    "HS_CELL_NUMERIC_PRECISION_LOST",
                    f"编码存成了 {len(text)} 位数值 {text!r}：前导零可能已丢失，不补零",
                    "HS编码",
                ),
                raw_text=text,
            )
        return CodeRead(code=text, level=level, recovered_from_numeric=True, raw_text=text)
    return CodeRead(
        issue=RecordIssue(
            "HS_CELL_TYPE_UNSUPPORTED", f"编码单元格类型 {kind!r} 不受支持", "HS编码"
        ),
        raw_text=text,
    )


def code_parts(code: str) -> tuple[str, str, str]:
    """按 HS 层级位置切出章/品目/子目；不做任何补位。"""

    return code[:2], code[:4], code[:6]


def source_ref(
    source: SourceFile,
    *,
    sheet: str,
    locator: str,
    system: str,
    url: str | None = None,
) -> dict[str, Any]:
    """构造一条可回查的来源引用；没有 URL 就留空，不编造。"""

    recorded = source.recorded_source or {}
    url = visible_text(url) or visible_text(recorded.get("url")) or None
    cell = locator.split("!")[-1]
    payload: dict[str, Any] = {
        "system": system,
        "document": source.original_filename or source.path.name,
        "locator": f"{sheet}!{cell}",
        "publisher": visible_text(recorded.get("publisher")),
        "retrieved_at": source.retrieved_at,
    }
    if url:
        payload["url"] = url
    return payload


def system_name(source: SourceFile, *keys: str) -> str:
    recorded = source.recorded_source or {}
    for key in keys:
        value = visible_text(recorded.get(key))
        if value:
            return value
    return ""


def dataset_descriptor(
    *,
    dataset: DatasetId,
    description: str,
    document: str,
    url: str | None,
    retrieved_at: str | None,
    notes: str,
    kind: DatasetKind = DatasetKind.REAL,
) -> dict[str, Any]:
    """构造 P14/P17 的 ``dataset`` 段。

    真实批次：官方来源 + ``authority=authoritative`` + ``synthetic=false``，
    但**未复核**（``review_status=unreviewed``），因此下游仍不可作依据；
    合成批次：``source_type/authority/synthetic`` 三个字段必须同时改成合成，
    否则夹具数据会被 P14/P17 读成“官方来源”。
    """

    official_kind = {
        DatasetId.P14_TARIFF: "official_tariff",
        DatasetId.P14_ELEMENTS: "official_declaration",
        DatasetId.P17_TAX_RATES: "official_tax",
        DatasetId.P17_REGULATORY: "official_regulatory",
    }.get(dataset, "official_tariff")
    if kind is DatasetKind.SYNTHETIC:
        source_type = "synthetic"
        authority = "synthetic"
        synthetic = True
        notes = "本批次是合成夹具（dataset_kind=synthetic），不是真实官方数据；" + notes
    else:
        source_type = official_kind
        authority = "authoritative"
        synthetic = False
    return {
        "dataset_id": "",
        "source_type": source_type,
        "authority": authority,
        "synthetic": synthetic,
        "description": description,
        "document": document,
        "url": url,
        "retrieved_at": retrieved_at,
        "review_status": "unreviewed",
        "reviewed_by": "",
        "reviewed_at": None,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 表头与行访问
# ---------------------------------------------------------------------------


def index_layout(access: WorkbookAccess) -> HeaderLayout:
    try:
        return access.locate_header(INDEX_SPEC)
    except LayoutError as exc:
        raise LayoutError(exc.code, f"{access.source.source_id}: {exc}") from exc


def element_layout(access: WorkbookAccess) -> HeaderLayout:
    try:
        layout = access.locate_header(ELEMENT_SPEC)
    except LayoutError as exc:
        raise LayoutError(exc.code, f"{access.source.source_id}: {exc}") from exc
    candidates = {
        name: column
        for name, column in layout.extras.items()
        if name.startswith(normalize_header(FLAG_HEADER_PREFIX))
    }
    if not candidates:
        raise LayoutError(
            "header_not_found", f"{access.source.source_id}: 申报要素明细缺少“必填标记”列"
        )
    if len(candidates) > 1:
        raise LayoutError(
            "header_ambiguous",
            f"{access.source.source_id}: 有多个“必填标记”列：{'、'.join(sorted(candidates))}",
        )
    (flag_name, flag_column), = candidates.items()
    return HeaderLayout(
        sheet=layout.sheet,
        header_row=layout.header_row,
        columns={**layout.columns, "必填标记": flag_column},
        extras={name: column for name, column in layout.extras.items() if name != flag_name},
    )


def data_rows(access: WorkbookAccess, sheet, *, start_row: int) -> Iterable[int]:
    """按行号产出数据行；整行为空的行跳过，行号不重排。"""

    for row_number in access.row_numbers(sheet):
        if row_number <= start_row:
            continue
        row = access.row_cells(sheet, row_number)
        if not any(visible_text(cell.value) for cell in row.values()):
            continue
        yield row_number


def _text(
    access: WorkbookAccess, sheet, row: int, columns: Mapping[str, int], name: str
) -> str:
    """业务字段文本：保留换行，只去不可见字符（不改写来源正文）。"""

    return clean_source_text(access.text(sheet, row, columns.get(name)))


# ---------------------------------------------------------------------------
# 转换产物
# ---------------------------------------------------------------------------


@dataclass
class CatalogBuild:
    """一轮目录转换的全部产物；由快照层决定哪些写盘。"""

    tariff_payload: dict[str, Any] | None = None
    elements_payload: dict[str, Any] | None = None
    regulatory_ledger_payload: dict[str, Any] | None = None
    tariff_outcome: DatasetOutcome | None = None
    elements_outcome: DatasetOutcome | None = None
    ledger_outcome: DatasetOutcome | None = None
    outcomes: list[RecordOutcome] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass
class _IndexRow:
    locator: str
    match_status: str
    element_count: int | None
    required_count: int | None


def _parse_optional_count(raw: str) -> int | None:
    text = visible_text(raw)
    if not text:
        return None
    if not text.isdigit():
        return None
    return int(text)


def build_catalog(
    manifest: SourceManifest,
    *,
    versioning: Versioning,
    sources: Iterable[SourceFile],
) -> CatalogBuild:
    """把来源清单里的目录工作簿转换成 P14 两份 JSON + 监管代码台账。

    多个来源文件覆盖同一数据类别时：内容相同 → duplicate（不产生重复记录），
    内容不同 → conflict（保留先出现的一条并转待核，绝不静默覆盖）。
    """

    build = CatalogBuild()
    tariff = DatasetOutcome(
        dataset=DatasetId.P14_TARIFF, status=DatasetStatus.PROVIDED, input_unit="税号行"
    )
    elements = DatasetOutcome(
        dataset=DatasetId.P14_ELEMENTS,
        status=DatasetStatus.PROVIDED,
        input_unit="要素明细行 + 目录中应有但缺失的清单（输出单位为编码的要素清单）",
    )
    ledger = DatasetOutcome(
        dataset=DatasetId.REGULATORY_CODE_LEDGER,
        status=DatasetStatus.PROVIDED,
        input_unit="税号行",
    )
    tariff_seen: dict[tuple[str, str], dict[str, Any]] = {}
    element_seen: dict[tuple[str, str], dict[str, Any]] = {}
    ledger_seen: dict[tuple[str, str], dict[str, Any]] = {}
    index_rows: dict[str, _IndexRow] = {}
    element_codes: dict[str, str] = {}
    numeric_cells: list[str] = []
    documents: dict[str, list[str]] = {}
    urls: dict[str, list[str]] = {}
    retrieved: dict[str, list[str]] = {}
    parse_failures: list[RecordOutcome] = []

    # 先解析表头、缓存工作簿：目录（含统计列）必须先转换完，要素清单才能核对统计数。
    accesses: dict[str, WorkbookAccess] = {}
    layouts_index: dict[str, HeaderLayout] = {}
    layouts_elements: dict[str, HeaderLayout] = {}
    for source in sources:
        if source.kind is InputKind.COMMENTARY_JSON:
            continue
        access = open_workbook(source)
        accesses[source.source_id] = access
        try:
            layouts_index[source.source_id] = index_layout(access)
        except LayoutError:
            pass
        try:
            layouts_elements[source.source_id] = element_layout(access)
        except LayoutError:
            pass
        if (
            source.source_id not in layouts_index
            and source.source_id not in layouts_elements
        ):
            raise LayoutError(
                "header_not_found",
                f"{source.source_id}: 既找不到 HS 编码索引表头，也找不到申报要素明细表头；"
                "未知结构不允许静默跳过",
            )

    for source in sources:
        layout = layouts_index.get(source.source_id)
        access = accesses.get(source.source_id)
        if access is None or layout is None:
            continue
        document = source.original_filename or source.path.name
        documents.setdefault("tariff", []).append(document)
        documents.setdefault("ledger", []).append(document)
        url = system_name(source, "hs_url", "url")
        if url:
            urls.setdefault("tariff", []).append(url)
        if source.retrieved_at:
            retrieved.setdefault("tariff", []).append(source.retrieved_at)
        _convert_index(
            access,
            source,
            layout,
            versioning=versioning,
            tariff=tariff,
            ledger=ledger,
            tariff_seen=tariff_seen,
            ledger_seen=ledger_seen,
            index_rows=index_rows,
            outcomes=build.outcomes,
            numeric_cells=numeric_cells,
        )

    for source in sources:
        layout = layouts_elements.get(source.source_id)
        access = accesses.get(source.source_id)
        if access is None or layout is None:
            continue
        document = source.original_filename or source.path.name
        documents.setdefault("elements", []).append(document)
        url = system_name(source, "elements_url", "url")
        if url:
            urls.setdefault("elements", []).append(url)
        if source.retrieved_at:
            retrieved.setdefault("elements", []).append(source.retrieved_at)
        _convert_elements(
            access,
            source,
            layout,
            versioning=versioning,
            elements=elements,
            element_seen=element_seen,
            element_codes=element_codes,
            index_rows=index_rows,
            outcomes=build.outcomes,
            numeric_cells=numeric_cells,
        )

    # ---- 目录与要素清单互相覆盖核对：缺口单独记 pending，不生成空清单
    for code, info in index_rows.items():
        if code in element_codes:
            continue
        if info.match_status and not info.match_status.startswith("已匹配"):
            reason = (
                f"来源声明“{info.match_status}”，未提供该编码的申报要素清单；"
                "未匹配不代表无需申报要素"
            )
            code_name = "ELEMENT_SCHEMA_NOT_MATCHED"
        else:
            reason = "来源标注为已匹配，但申报要素明细里没有该编码的行；需回来源补清单"
            code_name = "ELEMENT_SCHEMA_MISSING"
        # “目录里应有清单但明细缺失”也占一个输入条目：否则待核数无法与输入数对账。
        elements.input_total += 1
        elements.count(RecordStatus.PENDING)
        build.outcomes.append(
            RecordOutcome(
                dataset=DatasetId.P14_ELEMENTS,
                status=RecordStatus.PENDING,
                business_key=code,
                locator=info.locator,
                reason=reason,
                issues=[RecordIssue(code_name, reason, "HS编码", severity="pending")],
            )
        )
    _check_declared_counts_placeholder = None

    # ---- 组装 dataset 与 versions
    build.tariff_payload = _tariff_payload(
        versioning, tariff_seen, documents.get("tariff", []), urls.get("tariff", []),
        retrieved.get("tariff", []), manifest.kind,
    )
    if tariff_seen:
        build.tariff_outcome = tariff
    else:
        tariff.status = DatasetStatus.INPUT_MISSING
        tariff.reason = "没有可解析的税则编码索引工作簿"
        build.tariff_outcome = tariff

    build.elements_payload = _elements_payload(
        versioning, element_seen, documents.get("elements", []),
        urls.get("elements", []), retrieved.get("elements", []), manifest.kind,
    )
    if element_seen:
        build.elements_outcome = elements
    else:
        elements.status = DatasetStatus.INPUT_MISSING
        elements.reason = "没有可解析的申报要素明细工作簿"
        build.elements_outcome = elements

    if ledger_seen:
        build.regulatory_ledger_payload = {
            "schema_version": "p23.regulatory-codes.1",
            "dataset": {
                "dataset_id": versioning.dataset_id(DatasetId.REGULATORY_CODE_LEDGER),
                "source_type": (
                    "synthetic"
                    if manifest.kind is DatasetKind.SYNTHETIC
                    else "official_regulatory_codes_only"
                ),
                "authority": (
                    "synthetic"
                    if manifest.kind is DatasetKind.SYNTHETIC
                    else "authoritative"
                ),
                "synthetic": manifest.kind is DatasetKind.SYNTHETIC,
                "description": (
                    "监管条件代码原文台账：只有代码字符串与编码，没有进出口方向、措施名称、"
                    "主管机关、证件与生效/失效日期。"
                ),
                "document": "、".join(sorted(set(documents.get("ledger", [])))),
                "url": (urls.get("tariff") or [None])[0],
                "retrieved_at": (retrieved.get("tariff") or [None])[0],
                "review_status": "unreviewed",
                "reviewed_by": "",
                "reviewed_at": None,
                "notes": (
                    "**不进入 P17 监管条件库**：P17 的 RegulatoryRecord 要求方向与措施，"
                    "本来源均未声明；按方向/含义推断会凭空造出监管结论。"
                    "本文件只用于证明“代码存在且可回查”。"
                ),
            },
            "integration": {
                "p17_regulatory_store": "not_generated",
                "reason": "来源只有代码串，缺方向/措施/证件/有效期；不得由代码猜含义",
            },
            "records": [entry for entry in ledger_seen.values()],
        }
        build.ledger_outcome = ledger
    else:
        ledger.status = DatasetStatus.NOT_PROVIDED
        ledger.reason = "来源未提供监管条件代码列（或全部为空）"
        build.ledger_outcome = ledger

    build.assumptions.extend(_assumptions(versioning, numeric_cells))
    if parse_failures:  # pragma: no cover - 防御：解析失败必须显式出现
        build.outcomes.extend(parse_failures)
    return build


def _tariff_payload(
    versioning: Versioning,
    seen: Mapping[tuple[str, str], Mapping[str, Any]],
    documents: list[str],
    urls: list[str],
    retrieved: list[str],
    kind: DatasetKind,
) -> dict[str, Any] | None:
    if not seen:
        return None
    records = [entry["payload"] for entry in seen.values()]
    dataset = dataset_descriptor(
        dataset=DatasetId.P14_TARIFF,
        description=(
            "用户导出的海关现行税则 10 位编码目录（HS编码索引页签），只含编码、名称、"
            "计量单位代码与监管条件代码；不含进口关税税率、增值税与单位名称。"
        ),
        document="、".join(sorted(set(documents))),
        url=urls[0] if urls else None,
        retrieved_at=retrieved[0] if retrieved else None,
        notes=(
            "由 P23 导入流水线生成；未复核。编码状态按“来源为现行税则查询结果”记 active，"
            "属待复核假设。来源未声明税则公告生效日/失效日，版本窗口起点取采集日期，"
            "只表示该批次从该日之后可核对。"
        ),
        kind=kind,
    )
    dataset["dataset_id"] = versioning.dataset_id(DatasetId.P14_TARIFF)
    return {
        "schema_version": "p14.tariff.1",
        "dataset": dataset,
        "versions": [
            {
                "version": versioning.tariff_version,
                "effective_from": versioning.tariff_effective_from,
                "effective_to": None,
                "label": (
                    f"{versioning.tariff_version} 税则年度（批次采集 "
                    f"{versioning.tariff_effective_from}；来源未声明公告生效日/失效日）"
                ),
            }
        ],
        "codes": records,
    }


def _elements_payload(
    versioning: Versioning,
    seen: Mapping[tuple[str, str], Mapping[str, Any]],
    documents: list[str],
    urls: list[str],
    retrieved: list[str],
    kind: DatasetKind,
) -> dict[str, Any] | None:
    if not seen:
        return None
    schemas = [entry["payload"] for entry in seen.values()]
    dataset = dataset_descriptor(
        dataset=DatasetId.P14_ELEMENTS,
        description=(
            "用户导出的规范申报要素清单（申报要素明细页签）；必填标记 1/0 原样映射为 "
            "required/optional，条件必填口径、取值枚举与官方申报顺序未取得。"
        ),
        document="、".join(sorted(set(documents))),
        url=urls[0] if urls else None,
        retrieved_at=retrieved[0] if retrieved else None,
        notes=(
            "由 P23 导入流水线生成；未复核。要素 order 取来源行序，不是官方申报顺序；"
            "条件必填、取值枚举未在来源中给出，不得据此判“无条件必填”。"
        ),
        kind=kind,
    )
    dataset["dataset_id"] = versioning.dataset_id(DatasetId.P14_ELEMENTS)
    return {
        "schema_version": "p14.elements.1",
        "dataset": dataset,
        "versions": [
            {
                "version": versioning.elements_version,
                "effective_from": versioning.elements_effective_from,
                "effective_to": None,
                "label": (
                    f"{versioning.elements_version} 规范申报目录（批次采集 "
                    f"{versioning.elements_effective_from}；来源未声明公告生效日/失效日）"
                ),
            }
        ],
        "schemas": schemas,
    }


def _assumptions(versioning: Versioning, numeric_cells: list[str]) -> list[str]:
    assumptions = [
        "code_state_active：来源为海关现行税则查询结果，编码未逐条声明作废/并码；"
        "导入按 active 记录，需复核确认后才能作为“编码有效”的依据",
        "tariff_version：版本号 "
        f"{versioning.tariff_version} 取自文件名/批次与项目 D-09 口径，来源未声明公告生效日；"
        f"版本窗口起点 {versioning.tariff_effective_from} 只是“该批次可核对起点”，不是政策生效日",
        "element_order_from_file：来源没有官方申报顺序字段，order 取明细行序，"
        "仅用于稳定排序与定位；位置式原文解析在顺序未被专家确认前不得作依据",
        "element_requirement_from_flag：必填标记 1/0 映射为 required/optional；"
        "条件必填口径未取得，0 不必然表示任何情况下都非必填",
        "element_value_type_unknown：来源未给取值类型与枚举，全部按 text 处理，不猜枚举",
        "no_tariff_rate：本批不含进口关税（最惠国/暂定/普通）与增值税，未导入、未推断",
    ]
    if numeric_cells:
        shown = "、".join(numeric_cells[:5])
        assumptions.append(
            f"numeric_cells：{len(numeric_cells)} 个编码/要素编码单元格存成了数值但长度无损，"
            f"已按原文采用（{shown}{'…' if len(numeric_cells) > 5 else ''}），未补零"
        )
    return assumptions


# ---------------------------------------------------------------------------
# 各页签转换
# ---------------------------------------------------------------------------


def _convert_index(
    access: WorkbookAccess,
    source: SourceFile,
    layout: HeaderLayout,
    *,
    versioning: Versioning,
    tariff: DatasetOutcome,
    ledger: DatasetOutcome,
    tariff_seen: dict[tuple[str, str], dict[str, Any]],
    ledger_seen: dict[tuple[str, str], dict[str, Any]],
    index_rows: dict[str, _IndexRow],
    outcomes: list[RecordOutcome],
    numeric_cells: list[str],
) -> None:
    sheet = access.sheet(layout.sheet)
    columns = layout.columns
    system = system_name(source, "hs_system") or "海关总署「互联网+海关」税则查询（用户导出整理）"
    source_url = system_name(source, "hs_url", "url")
    for row_number in data_rows(access, sheet, start_row=layout.header_row):
        code_cell = access.cell(sheet, row_number, columns["HS编码"])
        read = read_hs_code(
            "" if code_cell is None else code_cell.value,
            "" if code_cell is None else code_cell.kind,
        )
        locator = access.locator(sheet, row_number, columns["HS编码"])
        tariff.input_total += 1
        if not read.ok:
            tariff.count(RecordStatus.REJECTED)
            outcomes.append(
                RecordOutcome(
                    dataset=DatasetId.P14_TARIFF,
                    status=RecordStatus.REJECTED,
                    business_key=read.raw_text or f"row-{row_number}",
                    locator=locator,
                    reason=read.issue.message if read.issue else "编码不可用",
                    issues=[read.issue] if read.issue else [],
                )
            )
            continue
        if read.recovered_from_numeric:
            numeric_cells.append(locator)
        code = read.code
        description = _text(access, sheet, row_number, columns, "商品名称")
        match_status = _text(access, sheet, row_number, columns, "匹配状态")
        chapter, heading, subheading = code_parts(code)
        business = {
            "description": description,
            "chapter": chapter,
            "heading": heading,
            "subheading": subheading,
            "state": "active",
        }
        business_fp = fingerprint(business)
        notes: list[str] = []
        if read.level == "subheading":
            notes.append("来源只收录 8 位子目，不能确认 10 位申报编码")
        if match_status and not match_status.startswith("已匹配"):
            notes.append(f"来源匹配状态：{match_status}")
        unit = _text(access, sheet, row_number, columns, "第一计量单位")
        if unit:
            notes.append(f"第一计量单位代码 {unit}（来源未给单位名称）")
        record = {
            "version": versioning.tariff_version,
            "code": code,
            "description": description,
            "chapter": chapter,
            "heading": heading,
            "subheading": subheading,
            "state": "active",
            "source": source_ref(
                source, sheet=sheet.name, locator=locator, system=system, url=source_url
            ),
            "note": "；".join(notes),
        }
        key = (versioning.tariff_version, code)
        existing = tariff_seen.get(key)
        if existing is None:
            tariff_seen[key] = {
                "fingerprint": business_fp,
                "payload": record,
                "locator": locator,
            }
            # 每条输入行只记一个状态：8 位子目记为 pending（记录会写入，但要标限制）。
            if read.level == "subheading":
                tariff.count(RecordStatus.PENDING)
            else:
                tariff.count(RecordStatus.IMPORTED)
            index_rows[code] = _IndexRow(
                locator=locator,
                match_status=match_status,
                element_count=_parse_optional_count(
                    _text(access, sheet, row_number, columns, "申报要素数")
                ),
                required_count=_parse_optional_count(
                    _text(access, sheet, row_number, columns, "必填数")
                ),
            )
            if read.level == "subheading":
                outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P14_TARIFF,
                        status=RecordStatus.PENDING,
                        business_key=code,
                        locator=locator,
                        reason="8 位子目：只能说明该层级，不能确认 10 位申报编码",
                        issues=[
                            RecordIssue(
                                "HS_SUBHEADING_ONLY",
                                "来源只收录 8 位子目",
                                "HS编码",
                                severity="pending",
                            )
                        ],
                    )
                )
        else:
            # 同键要么是重复（内容一致），要么是冲突（内容不同）——只能记一个，
            # 否则“输入数 = 各状态合计”的对账会暴露重复计数。
            if existing["fingerprint"] == business_fp:
                tariff.count(RecordStatus.DUPLICATE)
            else:
                tariff.count(RecordStatus.CONFLICT)
                outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P14_TARIFF,
                        status=RecordStatus.CONFLICT,
                        business_key=code,
                        locator=locator,
                        reason=(
                            "同一编码在两个来源里内容不同；已保留先出现的一条，需人工确认以哪份为准"
                        ),
                        issues=[
                            RecordIssue(
                                "HS_RECORD_CONFLICT",
                                "同一编码同版本出现不同内容",
                                "HS编码",
                                severity="pending",
                            )
                        ],
                    )
                )

        _convert_ledger_row(
            access, source, sheet, row_number, columns,
            versioning=versioning, code=code, description=description,
            locator=locator, ledger=ledger, ledger_seen=ledger_seen,
            outcomes=outcomes, system=system, url=source_url,
        )


def _convert_ledger_row(
    access: WorkbookAccess,
    source: SourceFile,
    sheet,
    row_number: int,
    columns: Mapping[str, int],
    *,
    versioning: Versioning,
    code: str,
    description: str,
    locator: str,
    ledger: DatasetOutcome,
    ledger_seen: dict[tuple[str, str], dict[str, Any]],
    outcomes: list[RecordOutcome],
    system: str,
    url: str | None = None,
) -> None:
    column = columns.get("监管条件代码")
    raw = _text(access, sheet, row_number, columns, "监管条件代码")
    ledger.input_total += 1
    if not raw:
        # 空串是“来源没写”，不是“明确无监管”；不写 measures=[]，只记待核。
        ledger.count(RecordStatus.PENDING)
        outcomes.append(
            RecordOutcome(
                dataset=DatasetId.REGULATORY_CODE_LEDGER,
                status=RecordStatus.PENDING,
                business_key=code,
                locator=access.locator(sheet, row_number, column) if column else locator,
                reason="监管条件代码栏为空白：来源未声明，不等于“无监管要求”",
                issues=[
                    RecordIssue(
                        "REG_CODES_NOT_DECLARED",
                        "来源未填写监管条件代码",
                        "监管条件代码",
                        severity="pending",
                    )
                ],
            )
        )
        return
    key = (versioning.tariff_version, code)
    existing = ledger_seen.get(key)
    if existing is not None:
        if existing.get("codes_raw") == raw:
            ledger.count(RecordStatus.DUPLICATE)
            return
        # 同税号的监管代码在不同来源里不一致：记冲突（不是重复），保留先出现的原文。
        ledger.count(RecordStatus.CONFLICT)
        outcomes.append(
            RecordOutcome(
                dataset=DatasetId.REGULATORY_CODE_LEDGER,
                status=RecordStatus.CONFLICT,
                business_key=code,
                locator=access.locator(sheet, row_number, column) if column else locator,
                reason=(
                    f"同一税号的监管条件代码不同（已保留先出现的 {existing.get('codes_raw')!r}，"
                    f"本次为 {raw!r}），需人工确认，程序不替来源下结论"
                ),
                issues=[
                    RecordIssue(
                        "REG_CODES_CONFLICT",
                        "同一税号同版本监管条件代码不一致",
                        "监管条件代码",
                        severity="pending",
                    )
                ],
            )
        )
        return
    ledger_seen[key] = {
        "version": versioning.tariff_version,
        "hs_code": code,
        "product_name": description,
        "codes_raw": raw,
        "source": source_ref(
            source,
            sheet=sheet.name,
            locator=access.locator(sheet, row_number, column) if column else locator,
            system=system,
            url=url,
        ),
        "note": (
            "来源只给监管条件代码字符串，未给方向、措施名称、主管机关与生效/失效日；"
            "不拆码、不解释，本台账不进入 P17 监管条件库"
        ),
    }
    ledger.count(RecordStatus.IMPORTED)


def _convert_elements(
    access: WorkbookAccess,
    source: SourceFile,
    layout: HeaderLayout,
    *,
    versioning: Versioning,
    elements: DatasetOutcome,
    element_seen: dict[tuple[str, str], dict[str, Any]],
    element_codes: dict[str, str],
    index_rows: Mapping[str, _IndexRow],
    outcomes: list[RecordOutcome],
    numeric_cells: list[str],
) -> None:
    """要素明细按“编码的要素清单”为单位产出；任何一行有问题就整份清单拒绝。

    只跳过坏行、留下少几项的清单是最危险的做法——那会让 P14 误判“要素充足”。
    因此这里先把行级问题收集到所属编码，最后整份清单拒绝并逐行报告原因。
    """

    sheet = access.sheet(layout.sheet)
    columns = layout.columns
    system = system_name(source, "elements_system") or (
        "海关总署规范申报要素查询（用户导出整理）"
    )
    source_url = system_name(source, "elements_url", "url")
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row_number in data_rows(access, sheet, start_row=layout.header_row):
        elements.input_total += 1
        code_cell = access.cell(sheet, row_number, columns["HS编码"])
        read = read_hs_code(
            "" if code_cell is None else code_cell.value,
            "" if code_cell is None else code_cell.kind,
        )
        locator = access.locator(sheet, row_number, columns["HS编码"])
        label = _text(access, sheet, row_number, columns, "申报要素")
        flag = _text(access, sheet, row_number, columns, "必填标记")
        if not read.ok:
            elements.count(RecordStatus.REJECTED)
            outcomes.append(
                RecordOutcome(
                    dataset=DatasetId.P14_ELEMENTS,
                    status=RecordStatus.REJECTED,
                    business_key=read.raw_text or f"row-{row_number}",
                    locator=locator,
                    reason=read.issue.message if read.issue else "编码不可用",
                    issues=[read.issue] if read.issue else [],
                )
            )
            continue
        if read.recovered_from_numeric:
            numeric_cells.append(locator)
        code = read.code
        group = groups.get(code)
        if group is None:
            group = {
                "rows": [],
                "issues": [],
                "description": _text(access, sheet, row_number, columns, "商品名称"),
                "first_row": row_number,
                "last_row": row_number,
            }
            groups[code] = group
            order.append(code)
        group["rows"].append((row_number, label, flag))
        group["last_row"] = row_number
        problem: RecordIssue | None = None
        if not label:
            problem = RecordIssue("ELEMENT_LABEL_EMPTY", "申报要素名称为空", "申报要素")
        elif flag not in REQUIREMENT_BY_FLAG:
            problem = RecordIssue(
                "ELEMENT_FLAG_INVALID",
                f"必填标记只能是 1 或 0，实际为 {flag!r}；不猜成条件必填",
                "必填标记",
            )
        if problem is not None:
            group["issues"].append((row_number, label, problem))

    for code in order:
        group = groups[code]
        rows: list[tuple[int, str, str]] = group["rows"]
        first_locator = access.locator(sheet, group["first_row"], columns["HS编码"])
        block = f"A{group['first_row']}:G{group['last_row']}"
        row_issues: list[tuple[int, str, RecordIssue]] = group["issues"]
        if row_issues:
            # 整份清单拒绝：不产出“少几项”的清单。
            elements.count(RecordStatus.REJECTED, len(rows))
            for row_number, label, problem in row_issues:
                outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P14_ELEMENTS,
                        status=RecordStatus.REJECTED,
                        business_key=f"{code}#{label or f'行{row_number}'}",
                        locator=access.locator(sheet, row_number, columns["HS编码"]),
                        reason=problem.message,
                        issues=[problem],
                    )
                )
            outcomes.append(
                RecordOutcome(
                    dataset=DatasetId.P14_ELEMENTS,
                    status=RecordStatus.REJECTED,
                    business_key=code,
                    locator=first_locator,
                    reason=(
                        f"该编码的要素清单中有 {len(row_issues)} 行不合契约，整份清单拒绝；"
                        "不产出缺少要素的清单"
                    ),
                    issues=[
                        RecordIssue(
                            "ELEMENT_SCHEMA_REJECTED",
                            "清单内存在非法行，整份清单不入库",
                            "申报要素",
                        )
                    ],
                )
            )
            continue
        if code not in index_rows:
            elements.count(RecordStatus.REJECTED, len(rows))
            reason = (
                "申报要素明细里的编码在 HS 编码索引中不存在，无法确认层级与适用前缀"
            )
            build_outcome = RecordOutcome(
                dataset=DatasetId.P14_ELEMENTS,
                status=RecordStatus.REJECTED,
                business_key=code,
                locator=first_locator,
                reason=reason,
                issues=[RecordIssue("ELEMENT_CODE_NOT_IN_CATALOG", reason, "HS编码")],
            )
            outcomes.append(build_outcome)
            continue
        labels = [label for _, label, _ in rows]
        duplicated = sorted({label for label in labels if labels.count(label) > 1})
        if duplicated:
            elements.count(RecordStatus.REJECTED, len(rows))
            outcomes.append(
                RecordOutcome(
                    dataset=DatasetId.P14_ELEMENTS,
                    status=RecordStatus.REJECTED,
                    business_key=code,
                    locator=first_locator,
                    reason=f"同一编码的申报要素名称重复：{'、'.join(duplicated)}",
                    issues=[
                        RecordIssue(
                            "ELEMENT_LABEL_DUPLICATED",
                            "同一编码下要素名称重复，无法唯一定位",
                            "申报要素",
                        )
                    ],
                )
            )
            continue
        specs = [
            {
                "key": label,
                "label": label,
                "order": index,
                "role": "other",
                "requirement": REQUIREMENT_BY_FLAG[flag],
                "condition": "",
                "value_type": "text",
                "enum_values": [],
                "example": "",
                "aliases": [],
                "note": "",
            }
            for index, (_, label, flag) in enumerate(rows, start=1)
        ]
        business = {
            "description": group["description"],
            "elements": [(item["label"], item["requirement"]) for item in specs],
        }
        business_fp = fingerprint(business)
        payload = {
            "hs_code": code,
            "description": group["description"],
            "version": versioning.elements_version,
            "elements": specs,
            "source": source_ref(
                source, sheet=sheet.name, locator=block, system=system, url=source_url
            ),
            "note": _ELEMENT_ORDER_NOTE,
        }
        key = (versioning.elements_version, code)
        info = index_rows.get(code)
        count_problems = _declared_count_problems(info, rows)
        existing = element_seen.get(key)
        if existing is not None:
            if existing["fingerprint"] == business_fp:
                elements.count(RecordStatus.DUPLICATE, len(rows))
            else:
                elements.count(RecordStatus.CONFLICT, len(rows))
                outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P14_ELEMENTS,
                        status=RecordStatus.CONFLICT,
                        business_key=code,
                        locator=first_locator,
                        reason=(
                            f"同一编码的要素清单在两个来源里不同（本次 {len(rows)} 行）；"
                            "已保留先出现的一份，需人工确认"
                        ),
                        issues=[
                            RecordIssue(
                                "ELEMENT_SCHEMA_CONFLICT",
                                "同一编码同版本出现不同要素清单",
                                "申报要素",
                                severity="pending",
                            )
                        ],
                    )
                )
            continue
        element_seen[key] = {
            "fingerprint": business_fp,
            "payload": payload,
            "locator": first_locator,
        }
        element_codes[code] = first_locator
        if count_problems:
            elements.count(RecordStatus.PENDING, len(rows))
            outcomes.append(
                RecordOutcome(
                    dataset=DatasetId.P14_ELEMENTS,
                    status=RecordStatus.PENDING,
                    business_key=code,
                    locator=info.locator if info else first_locator,
                    reason="索引页的统计数与明细行数不一致；" + "；".join(count_problems),
                    issues=[
                        RecordIssue(
                            "ELEMENT_COUNT_MISMATCH",
                            "；".join(count_problems),
                            "申报要素数",
                            severity="pending",
                        )
                    ],
                )
            )
        else:
            elements.count(RecordStatus.IMPORTED, len(rows))


def _declared_count_problems(
    info: _IndexRow | None, rows: list[tuple[int, str, str]]
) -> list[str]:
    """来源自带的“申报要素数/必填数”与明细行数核对；不一致只记待核，不改数据。"""

    if info is None:
        return []
    actual_total = len(rows)
    actual_required = sum(1 for _, _, flag in rows if flag == "1")
    problems: list[str] = []
    if info.element_count is not None and info.element_count != actual_total:
        problems.append(f"申报要素数声明 {info.element_count}，明细实际 {actual_total}")
    if info.required_count is not None and info.required_count != actual_required:
        problems.append(f"必填数声明 {info.required_count}，明细实际必填 {actual_required}")
    return problems


__all__ = [
    "CODE_LENGTHS",
    "CatalogBuild",
    "CodeRead",
    "ELEMENT_SPEC",
    "INDEX_SPEC",
    "REQUIREMENT_BY_FLAG",
    "Versioning",
    "build_catalog",
    "code_parts",
    "data_rows",
    "dataset_descriptor",
    "element_layout",
    "index_layout",
    "read_hs_code",
    "source_ref",
    "system_name",
]
