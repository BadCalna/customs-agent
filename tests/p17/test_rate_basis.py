"""P17 退税适配：输入契约、查询基准与版本解析。

本文件只验证“能不能查、按哪个版本查、非法输入有没有被挡住”，
真实退税率的业务判断不在这里。
"""
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.sources.hs_lookup import EvidenceStatus, QueryBasis, QueryStatus
from adapters.sources.tax_lookup import (
    ConditionsState,
    SourceStoreError,
    OriginalRateState,
    RateKind,
    RateState,
    RebateSourceKind,
    TaxApplicability,
    TaxRateQuery,
    TaxRateSource,
)
from tests.p17.helpers import (
    FIXED_CLOCK,
    VERSIONS,
    official_tax_dataset,
    rate_record,
    tax_dataset,
    tax_store,
)


class CountingStore:
    """非法输入不应读取来源：读一次就断言失败。"""

    def __init__(self):
        self.calls = 0

    def load(self):
        self.calls += 1
        raise AssertionError("非法输入不应读取来源")


class BrokenStore:
    def load(self):
        raise RuntimeError("测试：存储实现缺陷")


class InputContractTest(SimpleTestCase):
    def setUp(self):
        self.basis = QueryBasis(declaration_date="2026-05-20")

    def lookup(self, query, *, store=None):
        return TaxRateSource(store or tax_store(), clock=FIXED_CLOCK).lookup(query)

    def test_none_query_is_invalid_and_reads_no_source(self):
        store = CountingStore()
        result = self.lookup(None, store=store)
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertIsNone(result.evidence_status)
        self.assertIsNone(result.record)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertEqual(store.calls, 0)

    def test_unsupported_input_type_is_invalid(self):
        result = self.lookup("8482102000")
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertIn("类型不受支持", result.reason)
        self.assertEqual(result.evidence_status, None)

    def test_tariff_rate_input_is_refused_not_ignored(self):
        # 关税税率不等于出口退税率：带进来就必须拒绝，不能静默忽略后照常算。
        result = self.lookup(
            {
                "hs_code_raw": "8482102000",
                "basis": {"declaration_date": "2026-05-20"},
                "tariff_rate": "8%",
            }
        )
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertIsNone(result.evidence_status)
        self.assertEqual(result.issues[0].code, "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED")
        self.assertIn("关税税率不等于出口退税率", result.reason)
        self.assertFalse(result.usable_as_basis)

    def test_all_tariff_rate_aliases_are_refused(self):
        for key in (
            "tariff_rate",
            "customs_duty_rate",
            "import_tariff_rate",
            "export_tariff_rate",
            "duty_rate",
        ):
            with self.subTest(key=key):
                result = self.lookup({"hs_code_raw": "8482102000", key: "5"})
                self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
                self.assertEqual(
                    result.issues[0].code, "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED"
                )

    def test_image_inputs_are_refused(self):
        for key in ("image", "image_id", "picture", "ocr", "tax_rate_from_image"):
            with self.subTest(key=key):
                result = self.lookup({"hs_code_raw": "8482102000", key: "x"})
                self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
                self.assertEqual(result.issues[0].code, "TAX_RATE_FROM_IMAGE_REFUSED")
                self.assertTrue(
                    "图片" in result.issues[0].message or "OCR" in result.issues[0].message,
                    result.issues[0].message,
                )

    def test_unknown_field_is_rejected_by_strict_contract(self):
        with self.assertRaises(ValidationError):
            TaxRateQuery.model_validate(
                {"hs_code_raw": "8482102000", "rebate_code": "", "unexpected": 1}
            )

    def test_empty_code_is_invalid_and_does_not_claim_ineligible(self):
        result = self.lookup(TaxRateQuery(hs_code_raw="  ", basis=self.basis))
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertEqual(result.format.state.value, "empty")
        self.assertIsNone(result.looked_up_rate)
        self.assertEqual(result.rate_comparison.status.value, "not_compared")
        self.assertIn("未发起查询", result.reason)
        self.assertFalse(result.usable_as_basis)

    def test_nondigit_code_is_invalid(self):
        result = self.lookup(TaxRateQuery(hs_code_raw="8482ABC000", basis=self.basis))
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertEqual(result.format.state.value, "nondigit")

    def test_wrong_length_code_is_invalid(self):
        result = self.lookup(TaxRateQuery(hs_code_raw="8482102", basis=self.basis))
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertEqual(result.format.state.value, "wrong_length")
        self.assertIn("既不是 8 位税目/子目也不是 10 位申报编码", result.format.reason)

    def test_invalid_result_reports_pending_reason(self):
        result = self.lookup(None)
        self.assertTrue(result.pending_reasons)
        self.assertEqual(result.original_rate.state, OriginalRateState.BLANK)

    def test_store_exception_becomes_structured_failure(self):
        result = self.lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=self.basis), store=BrokenStore()
        )
        self.assertEqual(result.query_status, QueryStatus.SOURCE_ERROR)
        self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIsNone(result.record)
        self.assertEqual(result.applicability, TaxApplicability.UNKNOWN)
        self.assertIn("不等于查无退税", result.reason)


class BasisTest(SimpleTestCase):
    def lookup(self, *, hs="8482102000", basis, store=None):
        return TaxRateSource(store or tax_store(), clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw=hs, basis=basis)
        )

    def test_no_basis_keeps_version_unverified_and_fills_no_date(self):
        result = self.lookup(basis=QueryBasis())
        self.assertEqual(result.query_status, QueryStatus.OK)
        self.assertEqual(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertFalse(result.version.verified)
        self.assertIsNone(result.record)
        self.assertIn("系统不代填拟申报日期", result.reason)
        self.assertEqual(result.evidence, [])

    def test_date_only_resolves_single_version(self):
        result = self.lookup(basis=QueryBasis(declaration_date="2026-05-20"))
        self.assertTrue(result.version.verified)
        self.assertEqual(result.version.version, "2026")

    def test_version_only_is_verified_without_date(self):
        result = self.lookup(basis=QueryBasis(tariff_version="2025"))
        self.assertTrue(result.version.verified)
        self.assertEqual(result.version.version, "2025")
        # 版本已核但无申报日期：记录有效期仍是 unknown，不能冒充“已核对”。
        self.assertEqual(result.effective_status.value, "unknown")

    def test_date_outside_all_windows_is_unverified(self):
        result = self.lookup(basis=QueryBasis(declaration_date="2024-03-01"))
        self.assertFalse(result.version.verified)
        self.assertEqual(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertIn("不推断适用版本", result.version.reason)

    def test_unparseable_date_is_unverified(self):
        result = self.lookup(basis=QueryBasis(declaration_date="2026/05/20"))
        self.assertFalse(result.version.verified)
        self.assertIn("不能用查询当天代替", result.version.reason)

    def test_undeclared_version_is_unverified(self):
        result = self.lookup(basis=QueryBasis(tariff_version="2019"))
        self.assertFalse(result.version.verified)
        self.assertIn("不能用其它版本顶替", result.version.reason)

    def test_ambiguous_date_across_overlapping_windows_is_unverified(self):
        overlapping = [
            {"version": "A", "effective_from": "2026-01-01", "effective_to": "2026-12-31"},
            {"version": "B", "effective_from": "2026-06-01", "effective_to": "2027-05-31"},
        ]
        store = tax_store(
            [rate_record(version="A")], versions=overlapping, dataset=official_tax_dataset()
        )
        result = self.lookup(basis=QueryBasis(declaration_date="2026-07-01"), store=store)
        self.assertFalse(result.version.verified)
        self.assertIn("无法唯一确定适用版本", result.version.reason)

    def test_date_earlier_than_requested_version_is_unverified(self):
        result = self.lookup(
            basis=QueryBasis(declaration_date="2025-03-01", tariff_version="2026")
        )
        self.assertFalse(result.version.verified)
        self.assertIn("早于版本 2026 的生效日期", result.version.reason)

    def test_subheading_code_can_query_eight_digit_record(self):
        records = [rate_record(hs_code="72081000", rate_raw="10%", rate_percent=10)]
        store = tax_store(records)
        result = self.lookup(hs="72081000", basis=QueryBasis(declaration_date="2026-05-20"), store=store)
        self.assertEqual(result.format.state.value, "subheading_only")
        self.assertIsNotNone(result.record)
        self.assertIn("TAX_SUBHEADING_ONLY", [item.code for item in result.issues])
        # 8 位只说明层级，不能当作可直接申报的 10 位结论。
        self.assertFalse(result.usable_as_basis)

    def test_version_window_from_dataset_is_used_when_record_has_no_dates(self):
        records = [rate_record(effective_from=None, effective_to=None)]
        store = tax_store(records, dataset=official_tax_dataset())
        result = self.lookup(basis=QueryBasis(declaration_date="2026-05-20"), store=store)
        self.assertEqual(result.effective_status.value, "effective")
        self.assertEqual(result.evidence[0].effective_from, "2026-01-01")
        self.assertEqual(result.evidence[0].effective_to, "2026-12-31")


class SourceConfigurationTest(SimpleTestCase):
    def test_unconfigured_source_does_not_read_any_sample(self):
        import os
        from unittest.mock import patch

        from adapters.sources.tax_lookup import EnvTaxRateStore

        source = TaxRateSource(EnvTaxRateStore(), clock=FIXED_CLOCK)
        with patch.dict(os.environ, {"CUSTOMS_TAX_RATE_PATH": ""}):
            result = source.lookup(
                TaxRateQuery(
                    hs_code_raw="8482102000",
                    basis=QueryBasis(declaration_date="2026-05-20"),
                )
            )
        self.assertEqual(result.query_status, QueryStatus.SOURCE_UNCONFIGURED)
        self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIsNone(result.looked_up_rate)
        self.assertIn("不提供默认样本", result.reason)


class RecordContractTest(SimpleTestCase):
    """数据文件里的记录必须自证：处理方式与数值一致、原文保留、日期合法。"""

    def build(self, **overrides):
        from adapters.sources.tax_lookup import InMemoryTaxRateStore

        records = [rate_record(**overrides)]
        return InMemoryTaxRateStore(
            records, VERSIONS, dataset=official_tax_dataset()
        ).load()

    def test_export_rebate_requires_percent(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.build(rate_percent=None)
        self.assertIn("必须给出 rate_percent", str(caught.exception))

    def test_export_rebate_cannot_be_zero(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.build(rate_percent=0)
        self.assertIn("0% 属于零税率", str(caught.exception))

    def test_zero_rate_requires_zero(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.build(rate_kind=RateKind.ZERO_RATE.value, rate_percent=13)
        self.assertIn("必须是 0", str(caught.exception))

    def test_non_numeric_kind_cannot_carry_percent(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.build(rate_kind=RateKind.NOT_REFUNDABLE.value, rate_percent=0)
        self.assertIn("不是数值型", str(caught.exception))

    def test_declared_kind_requires_raw_text(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.build(rate_raw="")
        self.assertIn("rate_raw 必须保留原文", str(caught.exception))

    def test_hs_code_must_be_digits(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.build(hs_code="8482A02000")
        self.assertIn("8 或 10 位数字", str(caught.exception))

    def test_synthetic_dataset_cannot_be_authoritative(self):
        from adapters.sources.tax_lookup import TaxRateDatasetInfo

        with self.assertRaises(ValidationError) as caught:
            TaxRateDatasetInfo.model_validate(
                {
                    "dataset_id": "x",
                    "source_type": RebateSourceKind.SYNTHETIC.value,
                    "authority": "authoritative",
                    "synthetic": True,
                }
            )
        self.assertIn("合成数据不能声明 authoritative", str(caught.exception))

    def test_dataset_kind_must_belong_to_tax_sources(self):
        from adapters.sources.tax_lookup import TaxRateDatasetInfo

        with self.assertRaises(ValidationError) as caught:
            TaxRateDatasetInfo.model_validate(
                {
                    "dataset_id": "x",
                    "source_type": "official_regulatory",
                    "authority": "authoritative",
                }
            )
        self.assertIn("不属于本来源类型", str(caught.exception))

    def test_third_party_cannot_declare_authoritative(self):
        with self.assertRaises(ValidationError) as caught:
            tax_dataset(kind=RebateSourceKind.THIRD_PARTY, authority="authoritative")
        self.assertIn("第三方来源只能声明", str(caught.exception))

    def test_reviewed_dataset_requires_reviewer_and_time(self):
        from adapters.sources.tax_lookup import TaxRateDatasetInfo

        with self.assertRaises(ValidationError) as caught:
            TaxRateDatasetInfo.model_validate(
                {
                    "dataset_id": "x",
                    "source_type": RebateSourceKind.OFFICIAL_TAX.value,
                    "authority": "authoritative",
                    "synthetic": False,
                    "review_status": "reviewed",
                    "retrieved_at": "2026-11-17T00:00:00+08:00",
                }
            )
        self.assertIn("必须记录 reviewed_by", str(caught.exception))


class ConditionContractTest(SimpleTestCase):
    def test_condition_requires_visible_text(self):
        from adapters.sources.tax_lookup import RebateCondition

        with self.assertRaises(ValidationError):
            RebateCondition(text="\u200b\u3000")

    def test_condition_needs_both_document_and_locator(self):
        from adapters.sources.tax_lookup import RebateCondition

        with self.assertRaises(ValidationError) as caught:
            RebateCondition(text="条件", document="公告")
        self.assertIn("要么同时给 document 与 locator", str(caught.exception))

    def test_condition_url_must_be_http(self):
        from adapters.sources.tax_lookup import RebateCondition

        with self.assertRaises(ValidationError):
            RebateCondition(text="条件", url="ftp://example.com/a.pdf")

    def test_conditions_state_three_way(self):
        from adapters.sources.tax_lookup import TaxRateRecord
        from tests.p17.helpers import SOURCE

        base = dict(
            version="2026",
            hs_code="8482102000",
            rate_kind=RateKind.EXPORT_REBATE.value,
            rate_raw="13%",
            rate_percent=13.0,
            rate_state=RateState.IN_FORCE.value,
            source=SOURCE,
        )
        declared_none = TaxRateRecord.model_validate({**base, "conditions": []})
        not_declared = TaxRateRecord.model_validate(base)
        declared = TaxRateRecord.model_validate(
            {**base, "conditions": [{"text": "条件文本"}]}
        )
        self.assertEqual(declared_none.conditions_state, ConditionsState.DECLARED_NONE)
        self.assertEqual(not_declared.conditions_state, ConditionsState.NOT_DECLARED)
        self.assertEqual(declared.conditions_state, ConditionsState.DECLARED_UNVERIFIED)
