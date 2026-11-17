"""P17 复查修复回归（2026-11-17）。

对应复查提出的 4 项缺陷，逐条钉死，避免同类问题复发：

1. **P1 8 位监管码被当成可用依据**：来源声明无监管时返回“可作依据/无需专家”。
   现在 8 位子目一律 `usable_as_basis=False`、`expert_review_required=True`，并写明待核原因。
2. **P1 两用物项存在专家分流漏洞**：把两用物项填在“监管措施类别”里不走专家。
   现在 `category=dual_use` 与 `dual_use[]` 两种写法等价：不可用 + 转专家 + 报问题。
3. **P1 退税处理无法比对却标为无需专家**：原表 13% 与来源“不予退税”不可数值比对，
   现在 `rate_comparison=not_compared` 也要求专家复核。
4. **P2 8 位退税编码查询抛异常**：原值与来源一致时漏了待核原因，触发结果自检异常。
   现在正常返回结构化结果；并加了“不可用必须有原因”和“自检失败转技术失败”两道防线。

另加两条不变量：监管结果可用 ⇒ 不需要专家复核；两个适配器对夹具码 × 输入变体都不抛异常。
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from adapters.sources.hs_lookup import QueryBasis
from adapters.sources.regulatory_lookup import (
    Direction,
    MeasureCategory,
    RegulatoryQuery,
    RegulatoryResult,
    RegulatorySource,
)
from adapters.sources.tax_lookup import (
    RateKind,
    TaxRateQuery,
    TaxRateResult,
    TaxRateSource,
)
from tests.p17.helpers import (
    FIXED_CLOCK,
    REGULATORY_FIXTURE,
    TAX_FIXTURE,
    dual_use_item,
    measure,
    official_regulatory_dataset,
    official_tax_dataset,
    rate_record,
    regulatory_record,
    regulatory_store,
    tax_store,
)

BASIS = QueryBasis(declaration_date="2026-05-20")


# ---------------------------------------------------------------------------
# 缺陷 4 / 1：8 位子目
# ---------------------------------------------------------------------------


class EightDigitCodeTest(SimpleTestCase):
    """8 位子目只能说明层级，不能作为申报依据，也不能让查询崩掉。"""

    def tax_lookup(self, code="72081000", *, original="10%"):
        store = tax_store(
            [rate_record(hs_code=code, rate_raw="10%", rate_percent=10)],
            dataset=official_tax_dataset(),
        )
        return TaxRateSource(store, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw=code, basis=BASIS, original_rate_raw=original)
        )

    def test_eight_digit_tax_query_returns_normally(self):
        # 缺陷 4：原值与来源一致时曾漏掉待核原因，结果自检抛 ValidationError。
        result = self.tax_lookup()
        self.assertIsInstance(result, TaxRateResult)
        self.assertEqual(result.evidence_status.value, "obtained")
        self.assertEqual(result.rate_comparison.status.value, "matched")
        self.assertTrue(result.pending_reasons)

    def test_eight_digit_tax_query_is_not_usable(self):
        result = self.tax_lookup()
        self.assertEqual(result.applicability.value, "unknown")
        self.assertFalse(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)
        self.assertIn("TAX_SUBHEADING_ONLY", [item.code for item in result.issues])
        self.assertTrue(any("8 位" in reason for reason in result.pending_reasons))

    def test_eight_digit_regulatory_declared_none_is_not_usable(self):
        # 缺陷 1：曾返回 usable_as_basis=True / expert_review_required=False。
        store = regulatory_store(
            [regulatory_record(hs_code="72081000", measures=[])],
            dataset=official_regulatory_dataset(),
        )
        result = RegulatorySource(store, clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(hs_code_raw="72081000", direction=Direction.IMPORT, basis=BASIS)
        )
        self.assertEqual(result.outcome.value, "declared_none")
        self.assertFalse(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)
        self.assertIn("REG_SUBHEADING_ONLY", [item.code for item in result.issues])
        self.assertTrue(any("8 位" in reason for reason in result.pending_reasons))

    def test_eight_digit_regulatory_measures_are_not_usable(self):
        store = regulatory_store(
            [regulatory_record(hs_code="72081000", measures=[measure()])],
            dataset=official_regulatory_dataset(),
        )
        result = RegulatorySource(store, clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(hs_code_raw="72081000", direction=Direction.IMPORT, basis=BASIS)
        )
        self.assertFalse(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)


# ---------------------------------------------------------------------------
# 缺陷 2：两用物项的两个入口
# ---------------------------------------------------------------------------


class DualUseRoutingTest(SimpleTestCase):
    """两用物项无论写在“措施类别”还是“线索列表”，都必须转专家且不可用。"""

    def lookup(self, record):
        store = regulatory_store([record], dataset=official_regulatory_dataset())
        return RegulatorySource(store, clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(hs_code_raw="8482102000", direction=Direction.IMPORT, basis=BASIS)
        )

    def test_dual_use_measure_category_requires_expert(self):
        # 缺陷 2：曾经返回 expert_review_required=False 且 issues 为空。
        result = self.lookup(
            regulatory_record(measures=[measure(code="3", category=MeasureCategory.DUAL_USE)])
        )
        self.assertTrue(result.expert_review_required)
        self.assertFalse(result.usable_as_basis)
        self.assertIn(
            "REG_MEASURE_DUAL_USE_UNVERIFIED", [item.code for item in result.issues]
        )
        self.assertTrue(any("两用物项" in reason for reason in result.pending_reasons))

    def test_dual_use_measure_category_is_not_usable(self):
        result = self.lookup(
            regulatory_record(measures=[measure(code="3", category=MeasureCategory.DUAL_USE)])
        )
        self.assertFalse(result.usable_as_basis)
        self.assertEqual(result.outcome.value, "measures_required")

    def test_dual_use_list_entry_is_also_not_usable(self):
        # 与措施类别保持一致：有未判定的两用物项线索时，监管结论不算核清。
        result = self.lookup(regulatory_record(dual_use=[dual_use_item()]))
        self.assertFalse(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)

    def test_unknown_measure_category_is_not_usable(self):
        result = self.lookup(
            regulatory_record(measures=[measure(category=MeasureCategory.UNKNOWN)])
        )
        self.assertIn(
            "REG_MEASURE_CATEGORY_UNKNOWN", [item.code for item in result.issues]
        )
        self.assertFalse(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)

    def test_declared_none_usable_has_no_pending_or_expert(self):
        # 可用且无需专家时不能再挂“待核原因”，否则与标志自相矛盾。
        result = self.lookup(regulatory_record(measures=[]))
        self.assertEqual(result.outcome.value, "declared_none")
        self.assertTrue(result.usable_as_basis)
        self.assertFalse(result.expert_review_required)
        self.assertEqual(result.pending_reasons, [])

    def test_clean_measures_stay_usable_without_expert(self):
        # 对照组：不把正常情形一起判成待核。
        result = self.lookup(regulatory_record(measures=[measure()]))
        self.assertTrue(result.usable_as_basis)
        self.assertFalse(result.expert_review_required)
        self.assertEqual(result.outcome.value, "measures_required")


# ---------------------------------------------------------------------------
# 缺陷 3：不可比对的退税处理
# ---------------------------------------------------------------------------


class NonComparableRateTest(SimpleTestCase):
    def lookup(self, *, kind, rate_raw, original):
        store = tax_store(
            [rate_record(rate_kind=kind.value, rate_raw=rate_raw, rate_percent=None)],
            dataset=official_tax_dataset(),
        )
        return TaxRateSource(store, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw=original)
        )

    def test_not_refundable_requires_expert(self):
        # 缺陷 3：曾经 expert_review_required=False，但比对原因明确写着“需人工核对”。
        result = self.lookup(
            kind=RateKind.NOT_REFUNDABLE, rate_raw="不予退税", original="13%"
        )
        self.assertEqual(result.rate_comparison.status.value, "not_compared")
        self.assertTrue(result.expert_review_required)
        self.assertIn("不是百分数", result.rate_comparison.reason)

    def test_exempt_requires_expert(self):
        result = self.lookup(kind=RateKind.EXEMPT, rate_raw="免税", original="13%")
        self.assertEqual(result.rate_comparison.status.value, "not_compared")
        self.assertTrue(result.expert_review_required)

    def test_blank_original_requires_expert(self):
        store = tax_store(dataset=official_tax_dataset())
        result = TaxRateSource(store, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="")
        )
        self.assertEqual(result.rate_comparison.status.value, "not_compared")
        self.assertTrue(result.expert_review_required)

    def test_matched_rate_needs_no_expert(self):
        # 对照组：唯一“无需专家”的情形是已核可依 + 数值一致。
        store = tax_store(dataset=official_tax_dataset())
        result = TaxRateSource(store, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="13%")
        )
        self.assertEqual(result.rate_comparison.status.value, "matched")
        self.assertFalse(result.expert_review_required)
        self.assertTrue(result.usable_as_basis)


# ---------------------------------------------------------------------------
# 不变量与防线
# ---------------------------------------------------------------------------


class InvariantTest(SimpleTestCase):
    """把“可用 ⇒ 无需专家”钉成不变量，防止再次出现“可用但没人看”。"""

    def regulatory_case(self, record, *, direction=Direction.IMPORT, dataset=None):
        store = regulatory_store([record], dataset=dataset or official_regulatory_dataset())
        return RegulatorySource(store, clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(hs_code_raw=record["hs_code"], direction=direction, basis=BASIS)
        )

    def test_regulatory_usable_implies_no_expert_review(self):
        records = [
            regulatory_record(measures=[measure()]),
            regulatory_record(measures=[]),
            regulatory_record(measures=[measure(name="", category=MeasureCategory.UNKNOWN)]),
            regulatory_record(measures=[measure(category=MeasureCategory.DUAL_USE)]),
            regulatory_record(dual_use=[dual_use_item()]),
            regulatory_record(measures=[measure(document=None, locator=None)]),
            regulatory_record(hs_code="72081000"),
            regulatory_record(direction=Direction.BOTH.value, measures=[]),
        ]
        for record in records:
            with self.subTest(record=record["hs_code"] + record["direction"]):
                result = self.regulatory_case(record)
                if result.usable_as_basis:
                    self.assertFalse(
                        result.expert_review_required,
                        "可用结果不该同时要求专家复核",
                    )

    def test_tax_usable_without_matching_comparison_still_requires_expert(self):
        # 退税的 usable 说的是“来源事实可作依据”，与原值比对是另一件事：
        # 可用但比对没对上时仍必须转专家。
        store = tax_store(
            [rate_record(rate_kind=RateKind.NOT_REFUNDABLE.value, rate_raw="不予退税", rate_percent=None)],
            dataset=official_tax_dataset(),
        )
        result = TaxRateSource(store, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="13%")
        )
        self.assertTrue(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)

    def test_tax_never_raises_across_fixture_matrix(self):
        # 缺陷 4 的通用防线：任何“不可用但没有原因”的组合都不能抛异常。
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        codes = [
            "8482102000", "8536490090", "7304190000", "2710110000",
            "8471300000", "8501109100", "3917400000", "72081000", "9999999999", "",
        ]
        for code in codes:
            for original in ("", "0", "13%", "免税", "按政策"):
                for date in ("2026-05-20", None):
                    with self.subTest(code=code, original=original, date=date):
                        result = source.lookup(
                            TaxRateQuery(
                                hs_code_raw=code,
                                basis=QueryBasis(declaration_date=date),
                                original_rate_raw=original,
                            )
                        )
                        self.assertIsInstance(result, TaxRateResult)
                        if result.applicability.value != "applies":
                            self.assertTrue(result.pending_reasons or result.record is None)

    def test_regulatory_never_raises_across_fixture_matrix(self):
        source = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        codes = [
            "8482102000", "8536490090", "8501109100", "7304190000",
            "8471300000", "3917400000", "72081000", "9999999999", "",
        ]
        for code in codes:
            for direction in (Direction.IMPORT, Direction.EXPORT, Direction.UNKNOWN):
                for date in ("2026-05-20", None):
                    with self.subTest(code=code, direction=direction.value, date=date):
                        result = source.lookup(
                            RegulatoryQuery(
                                hs_code_raw=code,
                                direction=direction,
                                basis=QueryBasis(declaration_date=date),
                            )
                        )
                        self.assertIsInstance(result, RegulatoryResult)
                        if result.outcome.value == "unknown":
                            self.assertTrue(result.pending_reasons)


class SelfCheckGuardTest(SimpleTestCase):
    """自检失败必须变成结构化技术失败，不能 500，也不能返回“看起来通过”的结果。"""

    def test_regulatory_self_check_failure_is_reported_as_technical_failure(self):
        store = regulatory_store(dataset=official_regulatory_dataset())
        # 故意让证据缺失：三块（记录/措施/线索）对不上，结果校验会拒绝。
        with patch.object(RegulatorySource, "_evidence", return_value=[]):
            result = RegulatorySource(store, clock=FIXED_CLOCK).lookup(
                RegulatoryQuery(
                    hs_code_raw="8482102000", direction=Direction.IMPORT, basis=BASIS
                )
            )
        self.assertEqual(result.query_status.value, "source_error")
        self.assertEqual(result.evidence_status.value, "query_failed")
        self.assertEqual(result.outcome.value, "unknown")
        self.assertIn("结果自检失败", result.reason)
        self.assertFalse(result.usable_as_basis)
        self.assertIsNone(result.record)

    def test_tax_self_check_failure_is_reported_as_technical_failure(self):
        store = tax_store(dataset=official_tax_dataset())
        with patch.object(TaxRateSource, "_evidence", return_value=[]):
            result = TaxRateSource(store, clock=FIXED_CLOCK).lookup(
                TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="13%")
            )
        self.assertEqual(result.query_status.value, "source_error")
        self.assertEqual(result.evidence_status.value, "query_failed")
        self.assertIn("结果自检失败", result.reason)
        self.assertFalse(result.usable_as_basis)
        self.assertIsNone(result.record)
