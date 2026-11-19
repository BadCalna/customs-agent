"""Offline evaluation runner for P18 expert-check aspect matrix & redlines."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from adapters.engines.expert_check import (
    ENGINE_VERSION,
    EXPERT_ASPECTS,
    PROMPT_VERSION,
    RULES_VERSION,
    SYSTEM_PROMPT,
    ExpertCheckEngine,
)
from adapters.engines.protocols import ExecutionContext, ExpertCheckInput, ExpertCheckOutput, ImageFact
from adapters.sources.declaration_elements import (
    DeclarationElementsSource,
    ElementRole,
    ElementSchema,
    ElementSpec,
    ElementStoreError,
    InMemoryElementStore,
    Requirement,
)
from adapters.sources.history_search import (
    HistoryDatasetInfo,
    HistoryRecord,
    HistorySearchSource,
    HistorySourceRef,
)
from adapters.sources.hs_lookup import (
    Authority,
    CodeState,
    DatasetInfo,
    HsLookupSource,
    InMemoryTariffStore,
    ReviewStatus,
    SourceKind,
    SourceRef,
    TariffCode,
    VersionWindow,
)
from adapters.sources.regulatory_lookup import (
    Direction,
    DualUseItem,
    DualUseVerdict,
    InMemoryRegulatoryStore,
    MeasureCategory,
    RecordState,
    RegulatoryDatasetInfo,
    RegulatoryMeasure,
    RegulatoryRecord,
    RegulatorySource,
    RegulatorySourceKind,
)
from adapters.sources.tax_lookup import (
    InMemoryTaxRateStore,
    RateKind,
    RateState,
    RebateCondition,
    RebateSourceKind,
    SourceStoreError,
    TaxRateDatasetInfo,
    TaxRateRecord,
    TaxRateSource,
)
from apps.api.schemas import EvidenceEntry, EvidenceStatus, ExpertVerdict

from .cases import (
    SUITE_VERSION,
    EvaluationThresholds,
    ExpertEvalCase,
    ExpertGoldSuite,
    HS_ALT,
    HS_EIGHT,
    HS_MAIN,
    HS_ZERO,
    ModelFixture,
    fixed_cases,
)

REPORT_SCHEMA_VERSION = "p18-expert-eval-1.0.0"
FIXED_EVAL_CLOCK = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc)
FIXED_TIME_FUNC = lambda: FIXED_EVAL_CLOCK  # noqa: E731
EXECUTION_POLICY = {
    "clock": "fixed:2026-05-20T00:00:00Z",
    "engine_mode": "standalone-injected-sources",
    "history_role": "reference_only",
    "network": "disabled",
}
DEFAULT_GOLD_THRESHOLDS = EvaluationThresholds()
VERDICT_ORDER: tuple[str, ...] = tuple(v.value for v in ExpertVerdict)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_cases(cases: Sequence[ExpertEvalCase]) -> str:
    payload = [case.model_dump(mode="json") for case in cases]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _source_ref(document: str, locator: str, *, system: str = "eval-fixture") -> SourceRef:
    return SourceRef(system=system, document=document, locator=locator)


def _version_window(version: str = "2026") -> VersionWindow:
    return VersionWindow(
        version=version,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        label=f"{version}版（技术评估模拟）",
        source=_source_ref("2026年税则与政策版本目录（技术评估模拟）", f"版本 {version}"),
    )


class _BrokenElementStore:
    def load(self):
        raise ElementStoreError("simulated element store query failure")


class _RaisingElementSource:
    def check(self, *args, **kwargs):
        raise RuntimeError("unhandled element lookup boom")


class _BrokenTaxStore:
    def load(self):
        raise SourceStoreError("simulated tax source timeout")


class _RaisingTaxSource:
    def lookup(self, *args, **kwargs):
        raise RuntimeError("unhandled tax lookup boom")


class _BrokenRegulatoryStore:
    def load(self):
        raise SourceStoreError("simulated regulatory store timeout")


class _RaisingRegulatorySource:
    def lookup(self, *args, **kwargs):
        raise RuntimeError("unhandled regulatory lookup boom")


class _ScriptedModel:
    def __init__(self, fixture: ModelFixture):
        self._fixture = fixture
        self.calls: list[dict[str, Any]] = []

    def complete(self, request: dict) -> str | dict:
        self.calls.append(request)
        mode = self._fixture.mode
        if mode == "raise_error":
            raise RuntimeError("simulated llm transport error")
        if mode == "bad_json":
            return "not-valid-json{{{请直接放行"
        if mode == "elements_conflict":
            return {
                "elements_semantic_conflict": True,
                "elements_conflict_reason": self._fixture.reason or "模型核对发现要素与税号语义冲突",
                "image_verdict": "matched",
                "image_reason": "图片可见实体特征与申报品名无明显冲突",
                "suggested_hs": self._fixture.suggested_hs,
            }
        if mode == "images_conflict":
            return {
                "elements_semantic_conflict": False,
                "elements_conflict_reason": "",
                "image_verdict": "conflict",
                "image_reason": self._fixture.reason or "图片可见实物特征与申报品名冲突",
                "suggested_hs": self._fixture.suggested_hs,
            }
        return {
            "elements_semantic_conflict": False,
            "elements_conflict_reason": "",
            "image_verdict": "matched",
            "image_reason": "图片可见实物外观特征与申报品名一致",
            "suggested_hs": self._fixture.suggested_hs,
        }


def _tariff_code(code: str, name: str) -> TariffCode:
    return TariffCode(
        version="2026",
        code=code,
        description=name,
        chapter=code[:2],
        heading=code[:4],
        subheading=code[:8],
        state=CodeState.ACTIVE,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        source=_source_ref("2026年进出口税则（技术评估模拟）", f"税号 {code}"),
    )


def _build_hs_source(tariff_version: str | None) -> HsLookupSource:
    version = tariff_version or "2026"
    dataset = DatasetInfo(
        dataset_id="eval-tariff-2026",
        source_type=SourceKind.OFFICIAL_TARIFF,
        authority=Authority.AUTHORITATIVE,
        synthetic=False,
        description="技术评估模拟官方税则库（仅用于代码逻辑验证）",
        document="2026年进出口税则（技术评估模拟）",
        retrieved_at="2026-05-01T00:00:00Z",
        review_status=ReviewStatus.REVIEWED,
        reviewed_by="eval-curator",
        reviewed_at="2026-05-01T00:00:00Z",
    )
    records = [
        _tariff_code(HS_MAIN, "深沟球轴承"),
        _tariff_code(HS_ALT, "继电器及连接件"),
        _tariff_code(HS_ZERO, "无缝钢管"),
        _tariff_code(HS_EIGHT, "热轧卷板"),
    ]
    return HsLookupSource(
        store=InMemoryTariffStore(records, [_version_window(version)], dataset=dataset),
        clock=FIXED_TIME_FUNC,
    )


def _element_specs(conditional: bool = False) -> list[ElementSpec]:
    return [
        ElementSpec(
            key="brand_type",
            label="品牌类型",
            order=1,
            role=ElementRole.CUSTOMS,
            requirement=Requirement.REQUIRED,
        ),
        ElementSpec(
            key="usage",
            label="用途",
            order=2,
            role=ElementRole.CLASSIFICATION,
            requirement=Requirement.REQUIRED,
        ),
        ElementSpec(
            key="model",
            label="型号",
            order=3,
            role=ElementRole.CLASSIFICATION,
            requirement=Requirement.REQUIRED,
        ),
        ElementSpec(
            key="brand_name",
            label="品牌（中文或外文名称）",
            order=4,
            role=ElementRole.CUSTOMS,
            requirement=Requirement.CONDITIONAL if conditional else Requirement.REQUIRED,
            condition="当品牌类型非无品牌时必须申报品牌名称" if conditional else "",
        ),
        ElementSpec(
            key="gtin",
            label="GTIN",
            order=5,
            role=ElementRole.OTHER,
            requirement=Requirement.OPTIONAL,
        ),
    ]


def _build_elements_source(mode: str) -> Any:
    if mode == "raise_error":
        return _RaisingElementSource()
    if mode == "query_failed":
        return DeclarationElementsSource(store=_BrokenElementStore(), clock=FIXED_TIME_FUNC)

    is_synth = mode == "synthetic"
    authority = Authority.SYNTHETIC if is_synth else Authority.AUTHORITATIVE
    dataset = DatasetInfo(
        dataset_id=f"eval-elements-{mode}",
        source_type=SourceKind.SYNTHETIC if is_synth else SourceKind.OFFICIAL_DECLARATION,
        authority=authority,
        synthetic=True if is_synth else False,
        description="技术评估申报要素目录（仅用于代码逻辑验证）",
        document="2026年规范申报目录（技术评估模拟）",
        retrieved_at=None if is_synth else "2026-05-01T00:00:00Z",
        review_status=ReviewStatus.UNREVIEWED if is_synth else ReviewStatus.REVIEWED,
        reviewed_by="" if is_synth else "eval-curator",
        reviewed_at=None if is_synth else "2026-05-01T00:00:00Z",
    )

    schemas: list[ElementSchema] = []
    versions = [] if mode == "version_unverified" else [_version_window("2026")]
    if mode != "not_found":
        target_code = HS_EIGHT if mode == "official_eight_digit" else HS_MAIN
        schemas.append(
            ElementSchema(
                hs_code=target_code,
                description="测试税号品名",
                version="2026",
                elements=_element_specs(conditional=(mode == "official_conditional")),
                source=_source_ref("2026年规范申报目录（技术评估模拟）", f"目录条目 {target_code}"),
            )
        )
        schemas.append(
            ElementSchema(
                hs_code=HS_ZERO,
                description="无缝钢管",
                version="2026",
                elements=_element_specs(conditional=False),
                source=_source_ref("2026年规范申报目录（技术评估模拟）", f"目录条目 {HS_ZERO}"),
            )
        )
    return DeclarationElementsSource(
        store=InMemoryElementStore(schemas, versions, dataset=dataset),
        clock=FIXED_TIME_FUNC,
    )


def _build_tax_source(mode: str) -> Any:
    if mode == "raise_error":
        return _RaisingTaxSource()
    if mode == "query_failed":
        return TaxRateSource(store=_BrokenTaxStore(), clock=FIXED_TIME_FUNC)

    is_synth = mode == "synthetic"
    source_kind = RebateSourceKind.SYNTHETIC if is_synth else RebateSourceKind.OFFICIAL_TAX
    dataset = TaxRateDatasetInfo(
        dataset_id=f"eval-tax-{mode}",
        source_type=source_kind.value,
        authority=Authority.SYNTHETIC if is_synth else Authority.AUTHORITATIVE,
        synthetic=True if is_synth else False,
        description="技术评估退税率库（仅用于代码逻辑验证）",
        document="国家税务总局出口退税率文库（技术评估模拟）",
        retrieved_at=None if is_synth else "2026-05-01T00:00:00Z",
        review_status=ReviewStatus.UNREVIEWED if is_synth else ReviewStatus.REVIEWED,
        reviewed_by="" if is_synth else "eval-curator",
        reviewed_at=None if is_synth else "2026-05-01T00:00:00Z",
    )

    records: list[TaxRateRecord] = []
    versions = [] if mode == "version_unverified" else [_version_window("2026")]
    if mode != "not_found":
        hs_code = HS_EIGHT if mode == "official_eight_digit" else (HS_ZERO if mode == "official_zero_rate" else HS_MAIN)
        common = dict(
            version="2026",
            hs_code=hs_code,
            product_name="测试税号退税条目",
            effective_from="2026-01-01",
            effective_to="2026-12-31",
            source=_source_ref("国家税务总局出口退税率文库（技术评估模拟）", f"退税条目 {hs_code}"),
            rate_state=RateState.IN_FORCE,
        )
        if mode == "official_zero_rate":
            records.append(
                TaxRateRecord(
                    **common,
                    rate_kind=RateKind.ZERO_RATE,
                    rate_raw="0%",
                    rate_percent=0.0,
                    conditions=[],
                )
            )
        elif mode == "official_not_refundable":
            records.append(
                TaxRateRecord(
                    **common,
                    rate_kind=RateKind.NOT_REFUNDABLE,
                    rate_raw="不予退税",
                    rate_percent=None,
                    conditions=[],
                )
            )
        elif mode == "official_conditional":
            records.append(
                TaxRateRecord(
                    **common,
                    rate_kind=RateKind.EXPORT_REBATE,
                    rate_raw="13%",
                    rate_percent=13.0,
                    conditions=[
                        RebateCondition(
                            text="需提供特定出口退税凭证",
                            document="退税附加条件公告（技术评估模拟）",
                            locator="第 3 条",
                        )
                    ],
                )
            )
        elif mode == "official_conditions_missing":
            records.append(
                TaxRateRecord(
                    **common,
                    rate_kind=RateKind.EXPORT_REBATE,
                    rate_raw="13%",
                    rate_percent=13.0,
                    conditions=None,
                )
            )
        else:
            records.append(
                TaxRateRecord(
                    **common,
                    rate_kind=RateKind.EXPORT_REBATE,
                    rate_raw="13%",
                    rate_percent=13.0,
                    conditions=[],
                )
            )
    return TaxRateSource(store=InMemoryTaxRateStore(records, versions, dataset=dataset), clock=FIXED_TIME_FUNC)


def _build_regulatory_source(mode: str, direction: str) -> Any:
    if mode == "raise_error":
        return _RaisingRegulatorySource()
    if mode == "query_failed":
        return RegulatorySource(store=_BrokenRegulatoryStore(), clock=FIXED_TIME_FUNC)

    is_synth = mode == "synthetic"
    source_kind = RegulatorySourceKind.SYNTHETIC if is_synth else RegulatorySourceKind.OFFICIAL_REGULATORY
    dataset = RegulatoryDatasetInfo(
        dataset_id=f"eval-reg-{mode}",
        source_type=source_kind.value,
        authority=Authority.SYNTHETIC if is_synth else Authority.AUTHORITATIVE,
        synthetic=True if is_synth else False,
        description="技术评估监管条件库（仅用于代码逻辑验证）",
        document="2026年海关监管条件目录（技术评估模拟）",
        retrieved_at=None if is_synth else "2026-05-01T00:00:00Z",
        review_status=ReviewStatus.UNREVIEWED if is_synth else ReviewStatus.REVIEWED,
        reviewed_by="" if is_synth else "eval-curator",
        reviewed_at=None if is_synth else "2026-05-01T00:00:00Z",
    )

    records: list[RegulatoryRecord] = []
    versions = [] if mode == "version_unverified" else [_version_window("2026")]
    record_direction = Direction.EXPORT if direction == "export" else Direction.IMPORT
    if mode != "not_found":
        hs_code = HS_EIGHT if mode == "official_eight_digit" else HS_MAIN
        common = dict(
            version="2026",
            hs_code=hs_code,
            direction=record_direction,
            state=RecordState.IN_FORCE,
            effective_from="2026-01-01",
            effective_to="2026-12-31",
            product_name="测试监管税号条目",
            source=_source_ref("2026年海关监管条件目录（技术评估模拟）", f"监管条目 {hs_code}"),
        )
        if mode == "official_measures":
            records.append(
                RegulatoryRecord(
                    **common,
                    measures=[
                        RegulatoryMeasure(
                            code="A",
                            category=MeasureCategory.INSPECTION_QUARANTINE,
                            name="入境货物通关单",
                            agency="海关总署",
                            document="法检目录（技术评估模拟）",
                            locator=f"法检行 {hs_code}-A",
                        )
                    ],
                    dual_use=[],
                )
            )
        elif mode == "official_dual_use":
            records.append(
                RegulatoryRecord(
                    **common,
                    measures=[],
                    dual_use=[
                        DualUseItem(
                            list_name="两用物项和技术进出口许可证管理目录（技术评估模拟）",
                            item_description="高性能精密轴承敏感物项线索",
                            document="两用物项与技术出口许可证管理目录（技术评估模拟）",
                            locator="清单第9章-轴承条目",
                            verdict=DualUseVerdict.UNKNOWN,
                        )
                    ],
                )
            )
        elif mode == "official_measure_dual_use":
            records.append(
                RegulatoryRecord(
                    **common,
                    measures=[
                        RegulatoryMeasure(
                            code="3",
                            category=MeasureCategory.DUAL_USE,
                            name="两用物项和技术出口许可证",
                            agency="商务部",
                            document="两用物项出口管理目录（技术评估模拟）",
                            locator="代码 3-出口",
                        )
                    ],
                    dual_use=[],
                )
            )
        else:
            records.append(RegulatoryRecord(**common, measures=[], dual_use=[]))

        if hs_code != HS_ZERO:
            records.append(
                RegulatoryRecord(
                    version="2026",
                    hs_code=HS_ZERO,
                    direction=record_direction,
                    state=RecordState.IN_FORCE,
                    effective_from="2026-01-01",
                    effective_to="2026-12-31",
                    product_name="无缝钢管",
                    source=_source_ref("2026年海关监管条件目录（技术评估模拟）", f"监管条目 {HS_ZERO}"),
                    measures=[],
                    dual_use=[],
                )
            )
    return RegulatorySource(store=InMemoryRegulatoryStore(records, versions, dataset=dataset), clock=FIXED_TIME_FUNC)


def _build_history_source(mode: str) -> HistorySearchSource | None:
    if mode == "none":
        return None
    dataset = HistoryDatasetInfo(
        dataset_id="eval-history-2026",
        synthetic=True,
        description="技术评估历史参考样本",
    )
    records = [
        HistoryRecord(
            record_id="hist-eval-1",
            historical_hs=HS_ALT,
            historical_name="深沟球轴承（合成测试商品）",
            elements={"品牌类型": "无品牌", "用途": "工业设备连接", "型号": "X-100"},
            recorded_at="2025-11-12",
            outcome="approved",
            source=HistorySourceRef(
                document="历史归类台账（技术评估模拟）",
                locator="第 18 行",
                system="eval-history",
            ),
        )
    ]
    return HistorySearchSource(records=records, dataset=dataset, clock=FIXED_TIME_FUNC)


@dataclass
class SourceBundle:
    engine: ExpertCheckEngine
    model: _ScriptedModel | None


def build_source_bundle(case: ExpertEvalCase) -> SourceBundle:
    """为单条评估样本构造完全隔离的来源与模型适配器。"""
    hs_source = _build_hs_source(case.tariff_version)
    elements_source = _build_elements_source(case.sources.elements)
    tax_source = _build_tax_source(case.sources.tax)
    regulatory_source = _build_regulatory_source(case.sources.regulatory, case.direction)
    history_source = _build_history_source(case.sources.history)
    model = None if case.model.mode == "none" else _ScriptedModel(case.model)
    engine = ExpertCheckEngine(
        elements_source=elements_source,
        tax_source=tax_source,
        regulatory_source=regulatory_source,
        hs_source=hs_source,
        history_source=history_source,
        model=model,
    )
    return SourceBundle(engine=engine, model=model)


def run_case(case: ExpertEvalCase) -> tuple[ExpertCheckOutput, dict[str, str]]:
    """执行单条样本，返回完整 `ExpertCheckOutput` 与分项 verdict 映射。"""
    bundle = build_source_bundle(case)
    attributes: dict[str, str] = {
        "direction": case.direction,
    }
    if case.declaration_date:
        attributes["declaration_date"] = case.declaration_date
    if case.tariff_version:
        attributes["tariff_version"] = case.tariff_version

    image_evidence = [
        EvidenceEntry(
            evidence_id=f"img-fact:{img.source_cell}:{img.image_id}",
            source_type="image_fact",
            title=f"申报表附图 {img.source_cell}",
            status=EvidenceStatus.OBTAINED,
            document="申报要素表（技术评估模拟）.xlsx",
            locator=f"cell {img.source_cell}",
            query={"image_id": img.image_id, "source_cell": img.source_cell},
        )
        for img in case.images
        if img.status == "available"
    ]
    payload = ExpertCheckInput(
        submitted_hs=case.submitted_hs,
        supplier_tax_rate_raw=case.supplier_tax_rate_raw,
        name=case.name,
        elements_text=case.elements_text,
        attributes=attributes,
        images=[
            ImageFact(
                image_id=img.image_id,
                status=img.status,
                source_cell=img.source_cell,
                visible_description=img.visible_description,
                error_message=img.error_message,
            )
            for img in case.images
        ],
        evidence=image_evidence,
        submission_id=f"eval-sub-{case.id}",
        context=ExecutionContext(
            item_id=f"eval-item-{case.id}",
            run_id=f"eval-run-{case.id}",
            task_id=f"eval-task-{case.id}",
            version_id="v1",
            input_revision=1,
            input_hash=hashlib.sha256(case.id.encode("utf-8")).hexdigest(),
        ),
    )
    output = bundle.engine.expert_check(
        payload,
        direction=case.direction,
    )
    verdicts = {finding.aspect: finding.verdict.value for finding in output.findings}
    return output, verdicts


def _empty_matrix() -> dict[str, dict[str, int]]:
    return {
        expected: {actual: 0 for actual in VERDICT_ORDER}
        for expected in VERDICT_ORDER
    }


def _check_case_invariants(case: ExpertEvalCase, output: ExpertCheckOutput, actual_verdicts: dict[str, str]) -> list[str]:
    failures: list[str] = []
    expected_map = {
        "elements": case.expected_verdicts.elements.value,
        "images": case.expected_verdicts.images.value,
        "tax": case.expected_verdicts.tax.value,
        "regulatory": case.expected_verdicts.regulatory.value,
    }
    if output.target_hs != case.submitted_hs:
        failures.append(f"target_hs_mutated:{output.target_hs}!={case.submitted_hs}")

    for aspect in EXPERT_ASPECTS:
        exp = expected_map[aspect]
        act = actual_verdicts.get(aspect, "")
        if act != exp:
            failures.append(f"{aspect}:expected={exp},actual={act}")

    expected_status = case.expected_evidence_status.value if case.expected_evidence_status else None
    actual_status = output.evidence_status.value if output.evidence_status else None
    if actual_status != expected_status:
        failures.append(f"evidence_status:expected={expected_status},actual={actual_status}")

    actual_codes = {issue.code for issue in output.issues if issue.code}
    for code in case.expected_issue_codes:
        if code not in actual_codes:
            failures.append(f"missing_issue_code:{code}")

    if case.expected_suggested_hs is not None and output.suggested_hs != case.expected_suggested_hs:
        failures.append(f"suggested_hs:expected={case.expected_suggested_hs},actual={output.suggested_hs}")

    evidence_by_id = {entry.evidence_id: entry for entry in output.evidence}
    for finding in output.findings:
        if not finding.reason.strip():
            failures.append(f"empty_reason:{finding.aspect}")
        if finding.verdict is ExpertVerdict.MATCHED and not finding.evidence_ids:
            failures.append(f"matched_without_evidence:{finding.aspect}")
        for ev_id in finding.evidence_ids:
            entry = evidence_by_id.get(ev_id)
            if entry is None or not entry.document.strip() or not entry.locator.strip():
                failures.append(f"dangling_evidence:{finding.aspect}:{ev_id}")

    if any(code in {"APPROVED", "PASSED"} for code in actual_codes):
        failures.append("forbidden_pass_code")
    return failures


def evaluate_cases(
    cases: Sequence[ExpertEvalCase] | None = None,
    *,
    suite_id: str = SUITE_VERSION,
    gold_suite: ExpertGoldSuite | None = None,
    thresholds: EvaluationThresholds = DEFAULT_GOLD_THRESHOLDS,
    case_runner: Callable[[ExpertEvalCase], tuple[ExpertCheckOutput, dict[str, str]]] = run_case,
) -> dict[str, Any]:
    """评估 P18 四类分项混淆矩阵、unknown 覆盖率、失败样本与诚实达标判断。"""
    if gold_suite is not None:
        if not isinstance(gold_suite, ExpertGoldSuite):
            gold_suite = ExpertGoldSuite.model_validate(gold_suite)
        active_cases = list(gold_suite.cases)
        suite_id = gold_suite.suite_id
        thresholds = gold_suite.thresholds
        is_gold = True
        annotated_by: str | None = gold_suite.annotated_by
        annotated_at: str | None = gold_suite.annotated_at
    else:
        active_cases = list(cases) if cases is not None else fixed_cases()
        is_gold = False
        annotated_by = None
        annotated_at = None

    if not active_cases:
        raise ValueError("评估样本集不能为空")

    any_synthetic = any(case.synthetic for case in active_cases)
    gold_effective = bool(is_gold and not any_synthetic)

    per_aspect_matrices: dict[str, dict[str, dict[str, int]]] = {
        aspect: _empty_matrix() for aspect in EXPERT_ASPECTS
    }
    overall_matrix = _empty_matrix()
    failed_cases: list[dict[str, Any]] = []
    case_results: list[dict[str, Any]] = []

    expected_unknown_total = 0
    correct_unknown_total = 0
    false_matched_on_unknown = 0
    false_conflict_on_unknown = 0
    expected_conflict_total = 0
    correct_conflict_total = 0
    false_matched_on_conflict = 0
    overall_correct = 0
    overall_total = 0

    for case in active_cases:
        output, actual_verdicts = case_runner(case)
        failures = _check_case_invariants(case, output, actual_verdicts)

        expected_map = {
            "elements": case.expected_verdicts.elements.value,
            "images": case.expected_verdicts.images.value,
            "tax": case.expected_verdicts.tax.value,
            "regulatory": case.expected_verdicts.regulatory.value,
        }
        for aspect in EXPERT_ASPECTS:
            exp = expected_map[aspect]
            act = actual_verdicts[aspect]
            per_aspect_matrices[aspect][exp][act] += 1
            overall_matrix[exp][act] += 1
            overall_total += 1
            if act == exp:
                overall_correct += 1
            if exp == "unknown":
                expected_unknown_total += 1
                if act == "unknown":
                    correct_unknown_total += 1
                elif act == "matched":
                    false_matched_on_unknown += 1
                elif act == "conflict":
                    false_conflict_on_unknown += 1
            if exp == "conflict":
                expected_conflict_total += 1
                if act == "conflict":
                    correct_conflict_total += 1
                elif act == "matched":
                    false_matched_on_conflict += 1

        actual_codes = sorted({issue.code for issue in output.issues if issue.code})
        row_summary = {
            "id": case.id,
            "synthetic": case.synthetic,
            "passed": len(failures) == 0,
            "target_hs": output.target_hs,
            "suggested_hs": output.suggested_hs,
            "expected_verdicts": expected_map,
            "actual_verdicts": actual_verdicts,
            "expected_evidence_status": case.expected_evidence_status.value if case.expected_evidence_status else None,
            "actual_evidence_status": output.evidence_status.value if output.evidence_status else None,
            "actual_issue_codes": actual_codes,
            "failure_reasons": failures,
        }
        case_results.append(row_summary)
        if failures:
            failed_cases.append(
                {
                    "id": case.id,
                    "description": case.description,
                    **row_summary,
                }
            )

    unknown_coverage = (
        round(correct_unknown_total / expected_unknown_total, 6)
        if expected_unknown_total
        else 1.0
    )
    overall_accuracy = round(overall_correct / overall_total, 6) if overall_total else 0.0
    conflict_recall = (
        round(correct_conflict_total / expected_conflict_total, 6)
        if expected_conflict_total
        else 1.0
    )

    per_aspect_accuracy: dict[str, float] = {}
    for aspect, matrix in per_aspect_matrices.items():
        aspect_total = sum(sum(row.values()) for row in matrix.values())
        aspect_correct = sum(matrix[v][v] for v in VERDICT_ORDER)
        per_aspect_accuracy[aspect] = round(aspect_correct / aspect_total, 6) if aspect_total else 0.0

    technical_checks_passed = len(failed_cases) == 0 and false_matched_on_unknown == 0
    if not gold_effective:
        threshold_verdict = {
            "status": "pending_expert_gold_standard",
            "gold_standard": False,
            "customs_accuracy_verified": False,
            "technical_checks_passed": technical_checks_passed,
            "reason": (
                "当前报告包含合成技术验证样本（synthetic=true），未接入 Jay/关务专家 D-11 金标准标注，"
                "仅证实口径约束与适配器边界正确，严禁宣称真实关务准确率已达标。"
            ),
            "thresholds": thresholds.model_dump(mode="json"),
        }
    else:
        threshold_failures: list[str] = []
        if unknown_coverage < thresholds.min_unknown_coverage:
            threshold_failures.append(
                f"unknown_coverage={unknown_coverage} < {thresholds.min_unknown_coverage}"
            )
        if false_matched_on_unknown > thresholds.max_false_matched_on_unknown:
            threshold_failures.append(
                f"false_matched_on_unknown={false_matched_on_unknown} > {thresholds.max_false_matched_on_unknown}"
            )
        if overall_accuracy < thresholds.min_overall_accuracy:
            threshold_failures.append(
                f"overall_accuracy={overall_accuracy} < {thresholds.min_overall_accuracy}"
            )
        if conflict_recall < thresholds.min_conflict_recall:
            threshold_failures.append(
                f"conflict_recall={conflict_recall} < {thresholds.min_conflict_recall}"
            )
        if failed_cases:
            threshold_failures.append(f"failed_cases={len(failed_cases)}")
        passed = len(threshold_failures) == 0
        threshold_verdict = {
            "status": "passed" if passed else "failed",
            "gold_standard": True,
            "customs_accuracy_verified": passed,
            "technical_checks_passed": technical_checks_passed,
            "reason": (
                "已满足 D-11 金标准全部评估阈值且零 false matched on unknown"
                if passed
                else f"未达金标准阈值：{'; '.join(threshold_failures)}"
            ),
            "failure_reasons": threshold_failures,
            "thresholds": thresholds.model_dump(mode="json"),
        }

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "suite_id": suite_id,
        "suite_version": SUITE_VERSION if not gold_effective else suite_id,
        "gold_standard": gold_effective,
        "verification_scope": "customs_gold_standard" if gold_effective else "technical_synthetic_verification",
        "annotated_by": annotated_by,
        "annotated_at": annotated_at,
        "versions": {
            "engine_version": ENGINE_VERSION,
            "prompt_version": PROMPT_VERSION,
            "rules_version": RULES_VERSION,
            "prompt_sha256": _sha256_text(SYSTEM_PROMPT),
            "cases_sha256": _sha256_cases(active_cases),
        },
        "execution_policy": dict(EXECUTION_POLICY),
        "case_count": len(active_cases),
        "synthetic_case_count": sum(1 for case in active_cases if case.synthetic),
        "case_pass_count": len(active_cases) - len(failed_cases),
        "case_pass_rate": round((len(active_cases) - len(failed_cases)) / len(active_cases), 6),
        "metrics": {
            "overall_aspect_total": overall_total,
            "overall_aspect_accuracy": overall_accuracy,
            "per_aspect_accuracy": per_aspect_accuracy,
            "expected_unknown_count": expected_unknown_total,
            "correct_unknown_count": correct_unknown_total,
            "unknown_coverage": unknown_coverage,
            "false_matched_on_unknown": false_matched_on_unknown,
            "false_conflict_on_unknown": false_conflict_on_unknown,
            "expected_conflict_count": expected_conflict_total,
            "correct_conflict_count": correct_conflict_total,
            "conflict_recall": conflict_recall,
            "false_matched_on_conflict": false_matched_on_conflict,
        },
        "confusion_matrix": {
            "axes": {
                "rows": "expected",
                "cols": "actual",
                "labels": list(VERDICT_ORDER),
            },
            "overall": overall_matrix,
            "by_aspect": per_aspect_matrices,
        },
        "failed_cases": failed_cases,
        "case_results": case_results,
        "threshold_verdict": threshold_verdict,
    }


def evaluate_gold_suite(suite: ExpertGoldSuite | dict[str, Any]) -> dict[str, Any]:
    """使用 Jay/关务专家 D-11 金标准样本集评估；拒绝包含 synthetic=true 的输入。"""
    validated = suite if isinstance(suite, ExpertGoldSuite) else ExpertGoldSuite.model_validate(suite)
    return evaluate_cases(gold_suite=validated)
