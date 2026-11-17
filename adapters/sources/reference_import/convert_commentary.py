"""品目注释/子目注释（税则解释说明）转换（P23）。

现有 P14 契约只消费「税则编码」与「申报要素清单」，**没有**注释正文的字段或检索入口
（2026-11-17 在本仓库范围内核对：``adapters/``、``apps/``、``tests/`` 均无消费方）。
因此这里产出的是**独立的、版本化的解释资料快照** ``p23.commentary.1``，
由 :mod:`adapters.sources.tariff_commentary` 读取，并明确标注“已导入、尚未接入 P16”。

保真要求
--------
- 正文原样保存，不改写、不概括、不按固定长度切分；
- 按来源原有编码层级组织：品目（4 位，如 ``0101``）、章注（``CH1``）、类注（``CA1``）、
  其他注释（``ot3``/``ot4``）；
- 每个条目带来源定位（批次、序号、tariff_no）与正文长度，可回查原件；
- ``text_all`` 与分段正文的重新拼接结果必须一致；不一致只记待核，不改正文。

**不能**拿它证明某个 10 位编码存在或有效：``tariff_no`` 是品目/章/类层级键，
不是申报编码；读取接口也只接受这些键。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from adapters.sources.reference_import.convert_catalog import Versioning
from adapters.sources.reference_import.model import (
    COMMENTARY_SCHEMA_VERSION,
    DatasetId,
    DatasetKind,
    DatasetOutcome,
    DatasetStatus,
    InputKind,
    RecordIssue,
    RecordOutcome,
    RecordStatus,
    SourceFile,
    parse_iso_date,
    sha256_text,
    visible_text,
)

#: 来源已知的标签与条目类型；出现新值只记待核，不丢正文。
KNOWN_SECTION_LABELS = ("类注", "章注", "总注释", "品目注释", "子目注释")
KNOWN_ITEM_KINDS = ("品目", "章注", "类注", "其他注释")

#: 来源顶层已知字段；多出来的字段写进报告而不是静默忽略。
KNOWN_TOP_LEVEL_KEYS = ("source", "query", "fetched_at", "total", "items")


@dataclass
class CommentaryBuild:
    payload: dict[str, Any] | None = None
    outcome: DatasetOutcome | None = None
    outcomes: list[RecordOutcome] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def render_text_all(sections: Iterable[Mapping[str, str]]) -> str:
    """按来源格式重新拼接 ``text_all``：``【label】\\ntext`` 以空行分隔。"""

    blocks = [f"【{visible_text(item.get('label'))}】\n{item.get('text', '')}" for item in sections]
    return "\n\n".join(blocks)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_commentary(
    manifest: SourceManifest,
    *,
    versioning: Versioning,
    sources: Iterable[SourceFile],
) -> CommentaryBuild:
    build = CommentaryBuild()
    outcome = DatasetOutcome(
        dataset=DatasetId.TARIFF_COMMENTARY,
        status=DatasetStatus.PROVIDED,
        input_unit="注释条目",
    )
    seen: dict[str, dict[str, Any]] = {}
    documents: list[str] = []
    urls: list[str] = []
    fetched: list[str] = []
    unknown_labels: set[str] = set()
    unknown_kinds: set[str] = set()
    unknown_top_keys: set[str] = set()

    for source in sources:
        if source.kind is InputKind.COMMENTARY_WORKBOOK:
            outcome.status = DatasetStatus.UNSUPPORTED
            outcome.reason = (
                "本批只导入注释 JSON：同一批次的 XLSX 与 JSON 已由抓取体检报告核对为逐格一致，"
                "JSON 保真度更高（保留分段结构与 section 标签）"
            )
            build.outcome = outcome
            return build
        documents.append(source.original_filename or source.path.name)
        try:
            payload = _read_json(source.path)
        except (OSError, ValueError) as exc:
            outcome.status = DatasetStatus.INPUT_MISSING
            outcome.reason = f"注释原件不可读：{type(exc).__name__}"
            build.outcome = outcome
            return build
        if not isinstance(payload, Mapping):
            outcome.status = DatasetStatus.UNSUPPORTED
            outcome.reason = "注释原件顶层不是对象，结构未知"
            build.outcome = outcome
            return build
        unknown_top_keys.update(set(payload) - set(KNOWN_TOP_LEVEL_KEYS))
        source_meta = payload.get("source") or {}
        if isinstance(source_meta, Mapping):
            for key in ("page", "api_home", "api_list", "api_detail"):
                value = visible_text(source_meta.get(key))
                if value and value not in urls:
                    urls.append(value)
        fetched_at = visible_text(payload.get("fetched_at"))
        if fetched_at:
            fetched.append(fetched_at)
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            outcome.status = DatasetStatus.UNSUPPORTED
            outcome.reason = "注释原件缺少非空 items 数组"
            build.outcome = outcome
            return build
        declared_total = payload.get("total")
        if isinstance(declared_total, int) and declared_total != len(items):
            outcome.notes.append(
                f"来源声明 total={declared_total}，实际 items={len(items)}；按实际条数导入"
            )
        for position, item in enumerate(items, start=1):
            outcome.input_total += 1
            if not isinstance(item, Mapping):
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.TARIFF_COMMENTARY,
                        status=RecordStatus.REJECTED,
                        business_key=f"item-{position}",
                        locator=f"{source.path.name} 第 {position} 条",
                        reason="条目不对象，结构未知",
                        issues=[RecordIssue("COMMENTARY_ITEM_SHAPE_UNKNOWN", "条目不是对象")],
                    )
                )
                continue
            tariff_no = visible_text(item.get("tariff_no"))
            kind = visible_text(item.get("kind"))
            locator = f"{source.path.name}#items[{position}] tariff_no={tariff_no or '?'}"
            if not tariff_no:
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.TARIFF_COMMENTARY,
                        status=RecordStatus.REJECTED,
                        business_key=f"item-{position}",
                        locator=locator,
                        reason="缺少 tariff_no，无法定位到税则层级",
                        issues=[
                            RecordIssue("COMMENTARY_TARIFF_NO_MISSING", "缺少 tariff_no")
                        ],
                    )
                )
                continue
            detail = item.get("detail")
            if not isinstance(detail, Mapping):
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.TARIFF_COMMENTARY,
                        status=RecordStatus.REJECTED,
                        business_key=tariff_no,
                        locator=locator,
                        reason="缺少 detail 对象，注释正文不可得",
                        issues=[
                            RecordIssue("COMMENTARY_DETAIL_MISSING", "缺少 detail")
                        ],
                    )
                )
                continue
            sections_raw = detail.get("sections")
            if not isinstance(sections_raw, list) or not sections_raw:
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.TARIFF_COMMENTARY,
                        status=RecordStatus.REJECTED,
                        business_key=tariff_no,
                        locator=locator,
                        reason="缺少 sections 分段正文，无法保留条款上下文",
                        issues=[
                            RecordIssue("COMMENTARY_SECTIONS_MISSING", "缺少 sections")
                        ],
                    )
                )
                continue
            sections: list[dict[str, Any]] = []
            problems: list[RecordIssue] = []
            counters: dict[str, int] = {}
            for section in sections_raw:
                if not isinstance(section, Mapping):
                    problems.append(
                        RecordIssue("COMMENTARY_SECTION_SHAPE_UNKNOWN", "分段不是对象")
                    )
                    break
                label = visible_text(section.get("label"))
                text = section.get("text")
                if not label:
                    problems.append(
                        RecordIssue("COMMENTARY_SECTION_LABEL_MISSING", "分段缺少标签")
                    )
                    break
                if not isinstance(text, str):
                    problems.append(
                        RecordIssue(
                            "COMMENTARY_SECTION_TEXT_NOT_STRING",
                            f"分段 {label} 的正文不是字符串",
                        )
                    )
                    break
                if label not in KNOWN_SECTION_LABELS:
                    unknown_labels.add(label)
                counters[label] = counters.get(label, 0) + 1
                sections.append(
                    {
                        "label": label,
                        "text": text,
                        "anchor": f"{tariff_no}#{label}-{counters[label]}",
                    }
                )
            if problems:
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.TARIFF_COMMENTARY,
                        status=RecordStatus.REJECTED,
                        business_key=tariff_no,
                        locator=locator,
                        reason="；".join(item.message for item in problems),
                        issues=problems,
                    )
                )
                continue
            text_all = detail.get("text_all")
            if not isinstance(text_all, str):
                text_all = render_text_all(sections)
                problems.append(
                    RecordIssue(
                        "COMMENTARY_TEXT_ALL_MISSING",
                        "来源未给 text_all，已按分段正文重新拼接（内容未改写）",
                        severity="pending",
                    )
                )
            recomposed = render_text_all(sections)
            if text_all != recomposed:
                problems.append(
                    RecordIssue(
                        "COMMENTARY_TEXT_ALL_MISMATCH",
                        "text_all 与分段正文重新拼接结果不一致；保留原文并转人工核对",
                        severity="pending",
                    )
                )
            # 来源的 char_count 口径是「各分段正文长度之和」（不含【标签】与空行），
            # 与 text_all 的总长度不同；按来源口径核对才算自洽。
            section_char_total = sum(len(item["text"]) for item in sections)
            source_char_count = detail.get("char_count")
            if isinstance(source_char_count, int) and source_char_count != section_char_total:
                problems.append(
                    RecordIssue(
                        "COMMENTARY_CHAR_COUNT_MISMATCH",
                        f"来源 char_count={source_char_count} 与分段正文合计 "
                        f"{section_char_total} 不一致",
                        severity="pending",
                    )
                )
            if kind not in KNOWN_ITEM_KINDS:
                unknown_kinds.add(kind)
                problems.append(
                    RecordIssue(
                        "COMMENTARY_KIND_UNKNOWN",
                        f"条目类型 {kind!r} 未见过；正文保留并转人工确认层级",
                        severity="pending",
                    )
                )
            if tariff_no in seen:
                if seen[tariff_no]["text_sha256"] == sha256_text(text_all):
                    outcome.count(RecordStatus.DUPLICATE)
                else:
                    outcome.count(RecordStatus.CONFLICT)
                    build.outcomes.append(
                        RecordOutcome(
                            dataset=DatasetId.TARIFF_COMMENTARY,
                            status=RecordStatus.CONFLICT,
                            business_key=tariff_no,
                            locator=locator,
                            reason="同一 tariff_no 的注释正文不同；已保留先出现的一条",
                            issues=[
                                RecordIssue(
                                    "COMMENTARY_RECORD_CONFLICT",
                                    "同一层级键出现不同正文",
                                    severity="pending",
                                )
                            ],
                        )
                    )
                continue
            record = {
                "tariff_no": tariff_no,
                "kind": kind,
                "chapter": visible_text(item.get("chapter")),
                "title": visible_text(item.get("tariff_name")),
                "source_index": item.get("index"),
                "sections": sections,
                "text_all": text_all,
                "text_sha256": sha256_text(text_all),
                "text_length": len(text_all),
                "section_char_total": section_char_total,
                "source_char_count": source_char_count if isinstance(source_char_count, int) else None,
                "source_status": visible_text(item.get("status")),
                "source": {
                    "system": "海关总署商品归类参数查询（品目注释）",
                    "document": source.original_filename or source.path.name,
                    "locator": locator,
                    # URL 直接用原件 source.page，不写死常量。
                    "url": urls[0] if urls else None,
                    "publisher": "海关总署",
                    "retrieved_at": source.retrieved_at,
                },
                "note": "；".join(item.message for item in problems),
            }
            seen[tariff_no] = record
            if problems:
                outcome.count(RecordStatus.PENDING)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.TARIFF_COMMENTARY,
                        status=RecordStatus.PENDING,
                        business_key=tariff_no,
                        locator=locator,
                        reason="；".join(item.message for item in problems),
                        issues=problems,
                    )
                )
            else:
                outcome.count(RecordStatus.IMPORTED)

    if not seen:
        outcome.status = DatasetStatus.INPUT_MISSING
        outcome.reason = "没有可解析的注释条目"
        build.outcome = outcome
        return build

    # 按来源出现顺序输出（多来源时先出现的条目在前，不重排正文）
    ordered = dict(seen)
    retrieved_at = source_retrieved(sources)
    synthetic = manifest.kind is DatasetKind.SYNTHETIC
    dataset = {
        "dataset_id": versioning.dataset_id(DatasetId.TARIFF_COMMENTARY),
        "source_type": "synthetic" if synthetic else "official_tariff",
        "authority": "synthetic" if synthetic else "authoritative",
        "synthetic": synthetic,
        "description": (
            "海关总署商品归类参数查询的品目注释/子目注释/章注/类注全量导出；"
            "正文原样保留，按来源原有层级组织。"
        ),
        "document": "、".join(sorted(set(documents))),
        "url": urls[0] if urls else None,
        "retrieved_at": retrieved_at,
        "review_status": "unreviewed",
        "reviewed_by": "",
        "reviewed_at": None,
        "notes": (
            "由 P23 导入流水线生成；未复核。这是独立的版本化解释资料快照，"
            "当前适配器没有消费方，**已导入、尚未接入 P16**。"
            "tariff_no 是品目/章/类层级键，不是 10 位申报编码；"
            "不得用它证明某个 10 位编码存在或有效。"
        ),
    }
    build.payload = {
        "schema_version": COMMENTARY_SCHEMA_VERSION,
        "dataset": dataset,
        "version": {
            "version": versioning.commentary_version,
            "fetched_at": fetched[0] if fetched else None,
            "label": (
                f"品目注释全量批次 {versioning.commentary_version}"
                "（来源为海关总署归类参数查询；正文未改写）"
            ),
        },
        "integration": {
            "p14_consumer": None,
            "p16_engine": "not_wired",
            "reason": (
                "P14 的 HsLookupSource/DeclarationElementsSource 只消费税则编码与申报要素清单，"
                "没有注释正文入口；本快照由 adapters.sources.tariff_commentary 独立读取，"
                "接入 P16 检索前不得声称引擎已可用"
            ),
        },
        "records": list(ordered.values()),
    }
    if unknown_top_keys:
        outcome.notes.append(
            "来源顶层出现未识别字段（已忽略但记录在此）：" + "、".join(sorted(unknown_top_keys))
        )
    if unknown_labels:
        outcome.notes.append("出现未识别的分段标签：" + "、".join(sorted(unknown_labels)))
    if unknown_kinds:
        outcome.notes.append("出现未识别的条目类型：" + "、".join(sorted(unknown_kinds)))
    build.outcome = outcome
    build.assumptions = [
        "commentary_verbatim：正文逐字保留，未做固定长度切分、未改写为模型结论",
        "commentary_layering：按来源 tariff_no（品目 4 位 / CH 章 / CA 类 / ot 其他）组织，"
        "不映射成 10 位编码",
        "commentary_not_wired：已导入、尚未接入 P16；当前没有引擎检索入口",
        "commentary_xlsx_not_imported：同批次 XLSX 未单独导入（与 JSON 逐格一致，JSON 保真度更高）",
    ]
    return build


def source_retrieved(sources: Iterable[SourceFile]) -> str | None:
    for source in sources:
        if source.retrieved_at and parse_iso_date(source.retrieved_at) is not None:
            return source.retrieved_at
    return None


__all__ = [
    "CommentaryBuild",
    "KNOWN_ITEM_KINDS",
    "KNOWN_SECTION_LABELS",
    "KNOWN_TOP_LEVEL_KEYS",
    "build_commentary",
    "render_text_all",
]
