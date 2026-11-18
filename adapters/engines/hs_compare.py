"""P16 原码边界检查；六类优先级仍委托 P02 的 compare_original。

不查得到 ≠ 无效；8 位子目 ≠ 错码。查询基准只能由调用者显式提供。
本模块无持久化、无网络，不改变独立推荐或供应商原文。
"""
import re

from apps.api.schemas import EvidenceStatus, IssueEntry, ResultMode
from adapters.sources.hs_lookup import (
    CodeState, HsLookupResult, HsLookupSource, HsQuery, QueryBasis, QueryStatus,
    _strip_invisible,
)
from .mock import EngineTechnicalError, compare_original
from .protocols import CompareInput, CompareOutput

RULES_VERSION = "p16-compare-1.0.0"
_FULL = re.compile(r"[0-9]{10}\Z")
_SUBHEADING = re.compile(r"[0-9]{8}\Z")


def _merge_issues(*groups):
    result = []
    seen = set()
    for group in groups:
        for issue in group:
            key = tuple(issue.model_dump().items())
            if key not in seen:
                seen.add(key)
                result.append(issue)
    return result


def compare_original_checked(
    payload: CompareInput, *, hs_source: HsLookupSource | None = None,
    basis: QueryBasis | None = None,
) -> CompareOutput:
    """补齐格式与已证实状态检查，再复用唯一的六类优先级实现。

    优先级外的所有问题也保留。例如关联异常不吞掉推荐证据缺口。
    调用方不能用 format_valid=True 将非法原文洗为有效。
    """
    checked = payload.model_copy(deep=True)
    recommendation = checked.recommendation
    raw = checked.supplier_hs_raw.strip()
    extra = []
    subheading = bool(_SUBHEADING.fullmatch(raw))
    if not raw:
        extra.append(IssueEntry(code="SUPPLIER_HS_MISSING", message="供应商原 HS 为空", field="supplier_hs_raw"))
    if raw and not _FULL.fullmatch(raw) and not subheading:
        checked.supplier_hs_format_valid = False
        checked.supplier_hs_validity_note = "原 HS 不是 10 位 ASCII 数字；原文保留，不补零或猜测"
    if raw and not checked.supplier_hs_format_valid:
        extra.append(IssueEntry(code="SUPPLIER_HS_INVALID", message="原 HS 格式或已证实有效性异常", field="supplier_hs_raw"))
    if subheading and checked.supplier_hs_format_valid:
        extra.append(IssueEntry(
            code="SUPPLIER_HS_SUBHEADING_ONLY", field="supplier_hs_raw",
            message="原码仅为 8 位税目/子目，不等于错码，需确认完整申报编码",
            suggested_role="operator",
        ))

    if (raw and _FULL.fullmatch(raw) and checked.supplier_hs_format_valid
            and hs_source is not None):
        try:
            lookup = hs_source.lookup(HsQuery(hs_code_raw=raw, basis=basis or QueryBasis()))
            lookup = HsLookupResult.model_validate(lookup.model_dump())
            if (lookup.query is None or lookup.query.hs_code_raw != raw
                    or lookup.query.basis != (basis or QueryBasis())):
                raise ValueError("来源响应查询身份不符")
            if lookup.code is not None and (
                    lookup.code.code != raw or lookup.code.version != lookup.version.version):
                raise ValueError("来源记录与查询身份不符")
        except Exception:
            raise EngineTechnicalError("P16 原码来源调用失败；未判断原码无效") from None
        extra.extend(lookup.issues)
        real = lookup.dataset is not None and lookup.dataset.synthetic is False
        if (lookup.query_status == QueryStatus.OK and lookup.authoritative and real
                and lookup.version.verified and lookup.code is not None
                and lookup.code_state in (CodeState.EXPIRED, CodeState.MERGED)):
            checked.supplier_hs_format_valid = False
            checked.supplier_hs_validity_note = "权威来源在指定税则版本明确声明原码已失效或并码；原文保留"
            extra.append(IssueEntry(code="SUPPLIER_HS_INVALID", message=checked.supplier_hs_validity_note,
                                    field="supplier_hs_raw"))
        elif not lookup.declaration_ready or not real:
            extra.append(IssueEntry(
                code="SUPPLIER_HS_VALIDITY_PENDING", field="supplier_hs_raw",
                message="原码有效性尚未核实；查无、来源失败或版本待核不等于无效",
                suggested_role="expert",
            ))

    if recommendation.suggested_hs is None:
        extra.append(IssueEntry(code="RECOMMENDATION_PENDING", message="尚无独立推荐，不能比较", suggested_role="expert"))
    if recommendation.evidence_status != EvidenceStatus.OBTAINED:
        extra.append(IssueEntry(code="EVIDENCE_PENDING", message="依据未取得或版本待核，不能据此判无风险", suggested_role="expert"))
    if recommendation.suggested_hs is not None and not _FULL.fullmatch(recommendation.suggested_hs):
        extra.append(IssueEntry(
            code="SUGGESTED_HS_INVALID", message="推荐编码非完整 10 位 ASCII 数字，不能比较",
            suggested_role="operator",
        ))
    if recommendation.evidence_status == EvidenceStatus.OBTAINED and not recommendation.evidence:
        extra.append(IssueEntry(
            code="EVIDENCE_EMPTY", message="状态为已取得但无可定位证据，仍需核实", suggested_role="expert",
        ))
    if any(not _strip_invisible(e.document) or not _strip_invisible(e.locator)
           or e.status != EvidenceStatus.OBTAINED for e in recommendation.evidence):
        extra.append(IssueEntry(
            code="EVIDENCE_UNVERIFIED", message="推荐依据缺少定位或状态未核实", suggested_role="expert",
        ))
    if (checked.context.result_mode == ResultMode.LIVE and recommendation.evidence
            and not any(e.source_type in {"official_tariff", "official_declaration"}
                        and e.status == EvidenceStatus.OBTAINED
                        and _strip_invisible(e.document) and _strip_invisible(e.locator)
                        for e in recommendation.evidence)):
        extra.append(IssueEntry(
            code="OFFICIAL_EVIDENCE_PENDING", message="真实比较缺少可定位的官方依据；历史、合成与第三方仅供参考",
            suggested_role="expert",
        ))
    recommendation.gaps = _merge_issues(recommendation.gaps, extra)
    result = compare_original(checked)
    # 优先级只决定主结论，不得吞掉低优先级问题或修改调用方对象。
    result.issues = _merge_issues(result.issues, recommendation.gaps)
    return result
