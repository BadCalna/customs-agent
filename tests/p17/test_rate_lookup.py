"""P17 退税适配：四态、三种处理方式、机械比对与适用性。

核心红线：
- 查不到记录 ≠ 不予退税 ≠ 零税率；
- 原表空白 ≠ 0%；
- 缺日期/缺条件声明/来源非权威 → 不得 ``applies``；
- 原税率、查询值、机械比对三块分开，且不允许人工伪造。
"""
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.sources.hs_lookup import Authority, EvidenceStatus, QueryBasis, QueryStatus
from adapters.sources.tax_lookup import (
    ComparisonStatus,
    ConditionsState,
    OriginalRateState,
    RateKind,
    RateState,
    RebateCondition,
    RebateSourceKind,
    TaxApplicability,
    TaxRateQuery,
    TaxRateResult,
    TaxRateSource,
    compare_rates,
    parse_original_rate,
)
from tests.p17.helpers import (
    FIXED_CLOCK,
    OMIT,
    condition,
    official_tax_dataset,
    rate_record,
    tax_dataset,
    tax_store,
)

BASIS = QueryBasis(declaration_date="2026-05-20")
HS = "8482102000"


def lookup(*, query=None, store=None):
    return TaxRateSource(store or tax_store(), clock=FIXED_CLOCK).lookup(
        query or TaxRateQuery(hs_code_raw=HS, basis=BASIS, original_rate_raw="13%")
    )


class FoundRateTest(SimpleTestCase):
    def test_authoritative_reviewed_official_rate_can_be_basis(self):
        result = lookup()
        self.assertEqual(result.query_status, QueryStatus.OK)
        self.assertEqual(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertTrue(result.authoritative)
        self.assertEqual(result.applicability, TaxApplicability.APPLIES)
        self.assertTrue(result.usable_as_basis)
        self.assertFalse(result.expert_review_required)
        self.assertEqual(result.pending_reasons, [])
        self.assertEqual(result.original_rate.percent, 13.0)
        self.assertEqual(result.looked_up_rate.rate_percent, 13.0)
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.MATCHED)
        self.assertEqual(result.conditions_state, ConditionsState.DECLARED_NONE)

    def test_three_blocks_are_separate(self):
        result = lookup()
        # 原值、来源值、机械比对是三块独立输出，不合成一个“税率结论”。
        self.assertIsNotNone(result.original_rate)
        self.assertIsNotNone(result.looked_up_rate)
        self.assertIsNotNone(result.rate_comparison)
        self.assertEqual(result.rate_comparison.produced_by, "deterministic_adapter")

    def test_synthetic_source_is_clue_only(self):
        store = tax_store(dataset=tax_dataset())
        result = lookup(store=store)
        self.assertEqual(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.usable_as_basis)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertIn("TAX_AUTHORITY_NOT_OFFICIAL", [item.code for item in result.issues])
        self.assertTrue(any("非权威" in reason for reason in result.pending_reasons))
        self.assertIn("仅作线索", result.evidence[0].note)

    def test_official_but_unreviewed_is_not_authoritative(self):
        store = tax_store(dataset=official_tax_dataset(reviewed=False))
        result = lookup(store=store)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.usable_as_basis)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)

    def test_third_party_source_cannot_be_authoritative(self):
        store = tax_store(
            dataset=tax_dataset(
                kind=RebateSourceKind.THIRD_PARTY,
                authority=Authority.UNVERIFIED_THIRD_PARTY,
            )
        )
        result = lookup(store=store)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.usable_as_basis)
        self.assertEqual(result.evidence[0].source_type, "third_party")

    def test_evidence_is_locatable_and_reusable(self):
        result = lookup()
        self.assertEqual(len(result.evidence), 1)
        entry = result.evidence[0]
        self.assertEqual(entry.evidence_id, "tax:2026:8482102000")
        self.assertEqual(entry.status, EvidenceStatus.OBTAINED)
        self.assertEqual(entry.document, "P17-UNIT-FIXTURE")
        self.assertEqual(entry.locator, "测试夹具第 1 条")
        self.assertIsNotNone(entry.retrieved_at)
        self.assertIn("退税政策版本 2026", entry.scope)
        self.assertEqual(entry.query["hs_code"], HS)
        self.assertEqual(result.to_evidence_entries(), result.evidence)

    def test_evidence_id_stable_across_calls(self):
        first = lookup()
        second = lookup()
        self.assertEqual(
            [item.evidence_id for item in first.evidence],
            [item.evidence_id for item in second.evidence],
        )

    def test_reason_reports_rate_state_and_applicability(self):
        result = lookup()
        self.assertIn("处理方式", result.reason)
        self.assertIn("申报日适用性 effective", result.reason)


class RateKindTest(SimpleTestCase):
    def test_zero_rate_is_zero_not_missing(self):
        store = tax_store(
            [rate_record(rate_kind=RateKind.ZERO_RATE.value, rate_raw="0", rate_percent=0)]
        )
        result = lookup(
            query=TaxRateQuery(hs_code_raw=HS, basis=BASIS, original_rate_raw="0"),
            store=store,
        )
        self.assertEqual(result.looked_up_rate.rate_kind, RateKind.ZERO_RATE)
        self.assertEqual(result.looked_up_rate.rate_percent, 0)
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.MATCHED)
        self.assertTrue(result.usable_as_basis)

    def test_not_refundable_has_no_percent_and_is_not_compared_as_zero(self):
        store = tax_store(
            [
                rate_record(
                    rate_kind=RateKind.NOT_REFUNDABLE.value,
                    rate_raw="不予退税",
                    rate_percent=None,
                )
            ]
        )
        result = lookup(
            query=TaxRateQuery(hs_code_raw=HS, basis=BASIS, original_rate_raw="0"),
            store=store,
        )
        self.assertEqual(result.looked_up_rate.rate_kind, RateKind.NOT_REFUNDABLE)
        self.assertIsNone(result.looked_up_rate.rate_percent)
        # 原表写了 0，但来源是“不予退税”：不能因此判成 0% 一致。
        self.assertEqual(result.original_rate.percent, 0.0)
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.NOT_COMPARED)
        self.assertIn("不是百分数", result.rate_comparison.reason)
        # 原值 13% 与“不予退税”不可数值比对，也不能当成 0% 一致。
        self.assertNotEqual(result.rate_comparison.status, ComparisonStatus.MATCHED)

    def test_exempt_has_no_percent(self):
        store = tax_store(
            [rate_record(rate_kind=RateKind.EXEMPT.value, rate_raw="免税", rate_percent=None)]
        )
        result = lookup(store=store)
        self.assertEqual(result.looked_up_rate.rate_kind, RateKind.EXEMPT)
        self.assertIsNone(result.looked_up_rate.rate_percent)

    def test_unknown_kind_blocks_conclusion(self):
        store = tax_store(
            [rate_record(rate_kind=RateKind.UNKNOWN.value, rate_raw="", rate_percent=None)]
        )
        result = lookup(store=store)
        self.assertEqual(result.looked_up_rate.rate_kind, RateKind.UNKNOWN)
        self.assertIn("TAX_RATE_KIND_UNKNOWN", [item.code for item in result.issues])
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertFalse(result.usable_as_basis)

    def test_unknown_state_blocks_conclusion(self):
        store = tax_store([rate_record(rate_state=RateState.UNKNOWN.value)])
        result = lookup(store=store)
        self.assertIn("TAX_RATE_STATE_UNKNOWN", [item.code for item in result.issues])
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)


class OriginalRateParsingTest(SimpleTestCase):
    def test_blank_is_blank_not_zero(self):
        parsed = parse_original_rate("  ")
        self.assertEqual(parsed.state, OriginalRateState.BLANK)
        self.assertIsNone(parsed.percent)
        self.assertIn("空白不是 0%", parsed.note)

    def test_zero_is_numeric_zero(self):
        parsed = parse_original_rate("0")
        self.assertEqual(parsed.state, OriginalRateState.NUMERIC)
        self.assertEqual(parsed.percent, 0.0)

    def test_percent_sign_and_fullwidth(self):
        for text in ("13%", "13％", " 13 % ", "13.0"):
            with self.subTest(text=text):
                parsed = parse_original_rate(text)
                self.assertEqual(parsed.state, OriginalRateState.NUMERIC)
                self.assertEqual(parsed.percent, 13.0)

    def test_non_numeric_words(self):
        for text in ("免税", "不予退税", "零税率", "无"):
            with self.subTest(text=text):
                parsed = parse_original_rate(text)
                self.assertEqual(parsed.state, OriginalRateState.NON_NUMERIC)
                self.assertIsNone(parsed.percent)
                self.assertIn("不能按 0% 处理", parsed.note)

    def test_unparseable_values_are_not_guessed(self):
        for text in ("13-17", "按政策", "13%或9%", "100.5"):
            with self.subTest(text=text):
                parsed = parse_original_rate(text)
                self.assertEqual(parsed.state, OriginalRateState.UNPARSEABLE)
                self.assertIsNone(parsed.percent)

    def test_raw_text_is_preserved(self):
        parsed = parse_original_rate(" 13 % ")
        self.assertEqual(parsed.raw, " 13 % ")


class ComparisonTest(SimpleTestCase):
    def compare(self, original: str, record: dict):
        store = tax_store([record])
        return lookup(
            query=TaxRateQuery(hs_code_raw=HS, basis=BASIS, original_rate_raw=original),
            store=store,
        )

    def test_conflict_is_reported_and_not_arbitrated(self):
        result = self.compare("9%", rate_record())
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.CONFLICT)
        self.assertIn("TAX_RATE_CONFLICT", [item.code for item in result.issues])
        self.assertIn("不由适配器裁定", result.issues[0].message)
        self.assertTrue(result.expert_review_required)
        self.assertIn("原表退税率与来源值不一致", result.pending_reasons)

    def test_blank_original_is_not_compared_and_not_zero(self):
        result = self.compare("", rate_record())
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.NOT_COMPARED)
        self.assertIn("TAX_ORIGINAL_RATE_BLANK", [item.code for item in result.issues])
        self.assertIsNone(result.original_rate.percent)
        self.assertIn("不能按 0% 比对", result.rate_comparison.reason)

    def test_numeric_kind_with_unparseable_original_is_unverified(self):
        result = self.compare("按政策", rate_record())
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.UNVERIFIED)
        self.assertIn("TAX_ORIGINAL_RATE_UNPARSEABLE", [item.code for item in result.issues])

    def test_compare_rates_reports_not_compared_without_source(self):
        comparison = compare_rates(parse_original_rate("13%"), None)
        self.assertEqual(comparison.status, ComparisonStatus.NOT_COMPARED)
        self.assertIn("未取得来源退税率", comparison.reason)

    def test_mutation_of_comparison_is_rejected(self):
        result = self.compare("9%", rate_record())
        payload = result.model_dump(mode="json")
        payload["rate_comparison"]["status"] = "matched"
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("rate_comparison 必须由机械比对规则产生", str(caught.exception))


class ConditionsTest(SimpleTestCase):
    def test_declared_none_allows_applies(self):
        result = lookup()
        self.assertEqual(result.conditions_state, ConditionsState.DECLARED_NONE)
        self.assertEqual(result.conditions, [])
        self.assertEqual(result.applicability, TaxApplicability.APPLIES)

    def test_missing_conditions_field_blocks_applies(self):
        store = tax_store([rate_record(conditions=OMIT)])
        result = lookup(store=store)
        self.assertEqual(result.conditions_state, ConditionsState.NOT_DECLARED)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertIn("TAX_CONDITION_NOT_DECLARED", [item.code for item in result.issues])
        self.assertIn("来源未声明适用条件", result.pending_reasons)

    def test_declared_conditions_are_conditional_and_unverified(self):
        store = tax_store([rate_record(conditions=[condition()])])
        result = lookup(store=store)
        self.assertEqual(result.conditions_state, ConditionsState.DECLARED_UNVERIFIED)
        self.assertEqual(result.applicability, TaxApplicability.CONDITIONAL)
        self.assertFalse(result.usable_as_basis)
        self.assertIn("TAX_CONDITION_UNVERIFIED", [item.code for item in result.issues])
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(
            result.evidence[1].evidence_id, "tax:2026:8482102000:cond1"
        )
        self.assertEqual(result.evidence[1].excerpt, "合成条件原文")

    def test_unlocated_condition_adds_no_evidence_but_is_reported(self):
        store = tax_store(
            [rate_record(conditions=[condition(document=None, locator=None)])]
        )
        result = lookup(store=store)
        self.assertEqual(len(result.evidence), 1)
        self.assertIn("TAX_CONDITION_UNLOCATED", [item.code for item in result.issues])
        self.assertTrue(any("无法定位" in reason for reason in result.pending_reasons))

    def test_conditions_cannot_be_rewritten_in_result(self):
        result = lookup()
        payload = result.model_dump(mode="json")
        payload["conditions"] = [RebateCondition(text="凭空加的条件").model_dump(mode="json")]
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("conditions 必须与记录逐条一致", str(caught.exception))


class NotFoundTest(SimpleTestCase):
    def test_absent_code_is_not_found_and_never_zero(self):
        result = lookup(query=TaxRateQuery(hs_code_raw="9999999999", basis=BASIS))
        self.assertEqual(result.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertIsNone(result.record)
        self.assertIsNone(result.looked_up_rate)
        self.assertEqual(result.evidence, [])
        self.assertIn("TAX_RATE_NOT_FOUND", [item.code for item in result.issues])
        self.assertIn("不等于不予退税", result.reason)
        self.assertEqual(result.rate_comparison.status, ComparisonStatus.NOT_COMPARED)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)

    def test_absent_code_is_not_converted_to_zero_percent(self):
        for code in ("9999999999", "1111111111", "8482102099"):
            with self.subTest(code=code):
                result = lookup(query=TaxRateQuery(hs_code_raw=code, basis=BASIS))
                self.assertIsNone(result.looked_up_rate)
                self.assertNotEqual(result.rate_comparison.looked_up_percent, 0.0)

    def test_other_version_hint_is_reported_without_claiming_ineligible(self):
        store = tax_store(
            [rate_record(version="2025", effective_from="2025-01-01", effective_to="2025-12-31")]
        )
        result = lookup(store=store)
        self.assertEqual(result.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertIn("TAX_RATE_OTHER_VERSION", [item.code for item in result.issues])

    def test_unverified_version_cannot_claim_not_refundable(self):
        result = lookup(query=TaxRateQuery(hs_code_raw=HS, basis=QueryBasis()))
        self.assertEqual(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertIn("不能宣称不予退税或零税率", result.reason)


class EffectiveDateTest(SimpleTestCase):
    def test_record_starting_later_than_declaration_date(self):
        store = tax_store([rate_record(effective_from="2026-07-01", effective_to="2026-12-31")])
        result = lookup(store=store)
        self.assertEqual(result.effective_status.value, "not_yet_effective")
        self.assertEqual(result.applicability, TaxApplicability.NOT_EFFECTIVE)
        self.assertIn("TAX_RATE_NOT_EFFECTIVE", [item.code for item in result.issues])

    def test_record_expired_before_declaration_date(self):
        store = tax_store([rate_record(effective_from="2026-01-01", effective_to="2026-03-31")])
        result = lookup(store=store)
        self.assertEqual(result.effective_status.value, "expired")
        self.assertEqual(result.applicability, TaxApplicability.NOT_EFFECTIVE)

    def test_abolished_record_is_not_effective(self):
        store = tax_store([rate_record(rate_state=RateState.ABOLISHED.value)])
        result = lookup(store=store)
        self.assertEqual(result.applicability, TaxApplicability.NOT_EFFECTIVE)
        self.assertIn("TAX_RATE_ABOLISHED", [item.code for item in result.issues])

    def test_superseded_record_is_not_effective(self):
        store = tax_store([rate_record(rate_state=RateState.SUPERSEDED.value)])
        result = lookup(store=store)
        self.assertEqual(result.applicability, TaxApplicability.NOT_EFFECTIVE)
        self.assertIn("TAX_RATE_SUPERSEDED", [item.code for item in result.issues])

    def test_partial_effective_dates_stay_unknown(self):
        store = tax_store([rate_record(effective_from="2026-01-01", effective_to=None)])
        result = lookup(store=store)
        self.assertEqual(result.effective_status.value, "unknown")
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertIn(
            "TAX_RATE_EFFECTIVE_UNVERIFIED", [item.code for item in result.issues]
        )

    def test_missing_declaration_date_blocks_effectiveness(self):
        store = tax_store()
        result = lookup(
            query=TaxRateQuery(hs_code_raw=HS, basis=QueryBasis(tariff_version="2026"))
        )
        self.assertEqual(result.effective_status.value, "unknown")
        self.assertIn("TAX_EFFECTIVE_DATE_UNKNOWN", [item.code for item in result.issues])
        self.assertIn("TAX_ORIGINAL_RATE_BLANK", [item.code for item in result.issues])


class RebateCodeTest(SimpleTestCase):
    def test_matching_rebate_code_allows_applies(self):
        store = tax_store([rate_record(rebate_code="0310")])
        result = lookup(
            query=TaxRateQuery(
                hs_code_raw=HS, rebate_code="0310", basis=BASIS, original_rate_raw="13%"
            ),
            store=store,
        )
        self.assertEqual(result.applicability, TaxApplicability.APPLIES)
        self.assertTrue(result.usable_as_basis)

    def test_mismatched_rebate_code_blocks_applies(self):
        store = tax_store([rate_record(rebate_code="0310")])
        result = lookup(
            query=TaxRateQuery(
                hs_code_raw=HS, rebate_code="0999", basis=BASIS, original_rate_raw="13%"
            ),
            store=store,
        )
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertIn("TAX_REBATE_CODE_MISMATCH", [item.code for item in result.issues])
        self.assertIn("适用退税代码无法核对", result.pending_reasons)

    def test_source_without_rebate_code_blocks_applies_when_query_has_one(self):
        result = lookup(
            query=TaxRateQuery(
                hs_code_raw=HS, rebate_code="0310", basis=BASIS, original_rate_raw="13%"
            )
        )
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)


class ForgedResultTest(SimpleTestCase):
    def setUp(self):
        self.result = lookup()
        self.payload = self.result.model_dump(mode="json")

    def test_forging_applicability_is_rejected(self):
        payload = dict(self.payload)
        payload["applicability"] = "conditional"
        payload["usable_as_basis"] = False
        payload["pending_reasons"] = ["假设存在待核原因"]
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("applicability 必须由来源与基准计算", str(caught.exception))

    def test_usable_as_basis_must_match_applicability(self):
        payload = dict(self.payload)
        payload["usable_as_basis"] = False
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("usable_as_basis 必须与 applicability=applies 一致", str(caught.exception))

    def test_forging_looked_up_rate_is_rejected(self):
        payload = self.result.model_dump(mode="json")
        payload["looked_up_rate"]["rate_percent"] = 9
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("looked_up_rate 必须逐字段镜像来源记录", str(caught.exception))

    def test_forging_evidence_count_is_rejected(self):
        payload = self.result.model_dump(mode="json")
        payload["evidence"] = []
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("证据必须为 1 条记录证据", str(caught.exception))

    def test_unknown_applicability_requires_pending_reason(self):
        store = tax_store(dataset=tax_dataset())
        result = lookup(store=store)
        payload = result.model_dump(mode="json")
        payload["pending_reasons"] = []
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("非 applies 的结果必须写明至少一条待核原因", str(caught.exception))

    def test_result_cannot_mark_comparison_as_model_output(self):
        payload = self.result.model_dump(mode="json")
        payload["rate_comparison"]["produced_by"] = "ai_judgement"
        with self.assertRaises(ValidationError) as caught:
            TaxRateResult.model_validate(payload)
        self.assertIn("机械比对只能由适配器固定规则产生", str(caught.exception))
