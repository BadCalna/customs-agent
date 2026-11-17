"""P17 监管适配：三态结论、方向、措施可定位与两用物项。

核心红线：
- “没查到”“没声明”“方向不对”“缺日期”都不等于“无监管”；
- 只有来源明确声明、且日期/方向可核、来源权威时，``declared_none`` 才能读作无监管；
- 代码含义只来自来源，不给名称就是未知，不靠硬编码或模型补；
- 两用物项只能给 unknown 线索，必须转专家。
"""
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.sources.hs_lookup import EvidenceStatus, QueryBasis, QueryStatus
from adapters.sources.regulatory_lookup import (
    Direction,
    DualUseItem,
    DualUseVerdict,
    EnvRegulatoryStore,
    InMemoryRegulatoryStore,
    MeasureCategory,
    MeasuresState,
    RecordState,
    RegulatoryOutcome,
    RegulatoryQuery,
    RegulatoryResult,
    RegulatorySource,
    RegulatorySourceKind,
    SourceStoreError,
    direction_matches,
)
from tests.p17.helpers import (
    FIXED_CLOCK,
    OMIT,
    VERSIONS,
    dual_use_item,
    measure,
    official_regulatory_dataset,
    regulatory_dataset,
    regulatory_record,
    regulatory_store,
)

BASIS = QueryBasis(declaration_date="2026-05-20")
HS = "8482102000"


def lookup(*, query=None, store=None):
    return RegulatorySource(store or regulatory_store(), clock=FIXED_CLOCK).lookup(
        query or RegulatoryQuery(hs_code_raw=HS, direction=Direction.IMPORT, basis=BASIS)
    )


class DirectionContractTest(SimpleTestCase):
    def test_missing_direction_is_never_queried(self):
        class CountingStore:
            def load(self):
                raise AssertionError("未给方向不应读取来源")

        result = RegulatorySource(CountingStore(), clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(hs_code_raw=HS, basis=BASIS)
        )
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertIsNone(result.evidence_status)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertEqual(result.issues[0].code, "REG_DIRECTION_UNKNOWN")
        self.assertIn("不默认方向", result.reason)

    def test_record_with_measures_cannot_be_bidirectional(self):
        store = InMemoryRegulatoryStore(
            [regulatory_record(direction=Direction.BOTH.value, measures=[measure()])],
            VERSIONS,
            dataset=official_regulatory_dataset(),
        )
        with self.assertRaises(SourceStoreError) as caught:
            store.load()
        self.assertIn("必须拆成两条方向明确的记录", str(caught.exception))

    def test_direction_matching_rules(self):
        self.assertTrue(direction_matches(Direction.BOTH, Direction.IMPORT))
        self.assertTrue(direction_matches(Direction.BOTH, Direction.EXPORT))
        self.assertTrue(direction_matches(Direction.IMPORT, Direction.IMPORT))
        self.assertFalse(direction_matches(Direction.IMPORT, Direction.EXPORT))
        self.assertFalse(direction_matches(Direction.IMPORT, Direction.UNKNOWN))
        self.assertFalse(direction_matches(Direction.UNKNOWN, Direction.IMPORT))
        self.assertFalse(direction_matches(Direction.BOTH, Direction.UNKNOWN))

    def test_record_cannot_declare_unknown_direction(self):
        store = InMemoryRegulatoryStore(
            [regulatory_record(direction=Direction.UNKNOWN.value)],
            VERSIONS,
            dataset=official_regulatory_dataset(),
        )
        with self.assertRaises(SourceStoreError) as caught:
            store.load()
        self.assertIn("必须声明适用方向", str(caught.exception))


class InputRefusalTest(SimpleTestCase):
    def test_image_input_is_refused(self):
        for key in ("image", "image_id", "picture", "ocr"):
            with self.subTest(key=key):
                result = RegulatorySource(regulatory_store(), clock=FIXED_CLOCK).lookup(
                    {"hs_code_raw": HS, "direction": "import", key: "x"}
                )
                self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
                self.assertEqual(result.issues[0].code, "REG_QUERY_REFUSED")
                self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)

    def test_caller_cannot_assert_requires_licence(self):
        result = RegulatorySource(regulatory_store(), clock=FIXED_CLOCK).lookup(
            {"hs_code_raw": HS, "direction": "import", "requires_licence": True}
        )
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertIn("不能由调用方直接断言", result.issues[0].message)

    def test_invalid_code_does_not_become_no_regulation(self):
        result = lookup(
            query=RegulatoryQuery(hs_code_raw="ABC", direction=Direction.IMPORT, basis=BASIS)
        )
        self.assertEqual(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertEqual(result.evidence, [])


class MeasuresTest(SimpleTestCase):
    def test_authoritative_measures_are_usable_and_locatable(self):
        result = lookup()
        self.assertEqual(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertEqual(result.outcome, RegulatoryOutcome.MEASURES_REQUIRED)
        self.assertEqual(result.measures_state, MeasuresState.DECLARED)
        self.assertTrue(result.usable_as_basis)
        self.assertFalse(result.expert_review_required)
        self.assertEqual(len(result.measures), 1)
        self.assertEqual(result.measures[0].code, "A")
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(result.evidence[0].evidence_id, "reg:2026:8482102000:import")
        self.assertEqual(
            result.evidence[1].evidence_id, "reg:2026:8482102000:import:measure1"
        )
        self.assertEqual(result.evidence[1].excerpt, "入境货物通关单（合成）")

    def test_measure_without_name_blocks_conclusion(self):
        store = regulatory_store(
            [regulatory_record(measures=[measure(name="", category=MeasureCategory.UNKNOWN)])]
        )
        result = lookup(store=store)
        self.assertEqual(result.outcome, RegulatoryOutcome.MEASURES_REQUIRED)
        self.assertFalse(result.usable_as_basis)
        self.assertTrue(result.expert_review_required)
        self.assertIn("REG_MEASURE_NAME_UNKNOWN", [item.code for item in result.issues])
        self.assertEqual(result.measures[0].name, "")
        self.assertTrue(any("含义未取得" in reason for reason in result.pending_reasons))

    def test_unlocated_measure_blocks_conclusion(self):
        store = regulatory_store(
            [regulatory_record(measures=[measure(document=None, locator=None)])]
        )
        result = lookup(store=store)
        self.assertFalse(result.usable_as_basis)
        self.assertEqual(len(result.evidence), 1)
        self.assertIn("REG_MEASURE_UNLOCATED", [item.code for item in result.issues])

    def test_synthetic_source_still_reports_measures_but_not_usable(self):
        store = regulatory_store(dataset=regulatory_dataset())
        result = lookup(store=store)
        self.assertEqual(result.outcome, RegulatoryOutcome.MEASURES_REQUIRED)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.usable_as_basis)
        self.assertIn("REG_AUTHORITY_NOT_OFFICIAL", [item.code for item in result.issues])
        self.assertIn("仅作线索", result.evidence[0].note)

    def test_official_unreviewed_is_not_authoritative(self):
        store = regulatory_store(dataset=official_regulatory_dataset(reviewed=False))
        result = lookup(store=store)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.usable_as_basis)


class DeclaredNoneTest(SimpleTestCase):
    def declared_none_store(self, **overrides):
        payload = regulatory_record(direction=Direction.BOTH.value, measures=[])
        payload.update(overrides)
        return regulatory_store([payload])

    def test_declared_none_is_usable_only_for_authoritative_source(self):
        result = lookup(store=self.declared_none_store())
        self.assertEqual(result.outcome, RegulatoryOutcome.DECLARED_NONE)
        self.assertEqual(result.measures_state, MeasuresState.DECLARED_NONE)
        self.assertEqual(result.measures, [])
        self.assertTrue(result.usable_as_basis)
        self.assertFalse(result.expert_review_required)
        self.assertEqual(result.evidence[0].excerpt, "来源明确声明无监管要求")

    def test_synthetic_declared_none_must_not_be_read_as_no_regulation(self):
        store = regulatory_store(
            [regulatory_record(direction=Direction.BOTH.value, measures=[])],
            dataset=regulatory_dataset(),
        )
        result = lookup(store=store)
        self.assertEqual(result.outcome, RegulatoryOutcome.DECLARED_NONE)
        self.assertFalse(result.usable_as_basis)
        self.assertIn("不能作为合规依据", result.reason)
        self.assertIn("更不能读作“无监管”", result.reason)

    def test_missing_measures_field_is_unknown(self):
        store = regulatory_store([regulatory_record(measures=OMIT)])
        result = lookup(store=store)
        self.assertEqual(result.measures_state, MeasuresState.NOT_DECLARED)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertFalse(result.usable_as_basis)
        self.assertIn("REG_MEASURES_NOT_DECLARED", [item.code for item in result.issues])
        self.assertIn("来源未声明监管要求", result.pending_reasons)


class DateAndStateTest(SimpleTestCase):
    def test_missing_declaration_date_is_unknown_not_declared_none(self):
        store = regulatory_store([regulatory_record(direction=Direction.BOTH.value, measures=[])])
        result = lookup(
            query=RegulatoryQuery(
                hs_code_raw=HS, direction=Direction.IMPORT, basis=QueryBasis(tariff_version="2026")
            ),
            store=store,
        )
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertEqual(result.effective_status.value, "unknown")
        self.assertIn("REG_EFFECTIVE_DATE_UNKNOWN", [item.code for item in result.issues])
        self.assertFalse(result.usable_as_basis)

    def test_date_outside_record_window_is_not_effective(self):
        store = regulatory_store([regulatory_record(effective_to="2026-03-31")])
        result = lookup(store=store)
        self.assertEqual(result.outcome, RegulatoryOutcome.NOT_EFFECTIVE)
        self.assertFalse(result.usable_as_basis)
        self.assertIn("REG_RECORD_NOT_EFFECTIVE", [item.code for item in result.issues])

    def test_abolished_record_is_not_effective(self):
        store = regulatory_store([regulatory_record(state=RecordState.ABOLISHED.value)])
        result = lookup(store=store)
        self.assertEqual(result.outcome, RegulatoryOutcome.NOT_EFFECTIVE)
        self.assertIn("REG_RECORD_ABOLISHED", [item.code for item in result.issues])

    def test_unknown_state_blocks_conclusion(self):
        store = regulatory_store([regulatory_record(state=RecordState.UNKNOWN.value)])
        result = lookup(store=store)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertIn("REG_RECORD_STATE_UNKNOWN", [item.code for item in result.issues])

    def test_partial_effective_dates_stay_unknown(self):
        store = regulatory_store([regulatory_record(effective_from="2026-01-01", effective_to=None)])
        result = lookup(store=store)
        self.assertEqual(result.effective_status.value, "unknown")
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertIn("REG_RECORD_EFFECTIVE_UNVERIFIED", [item.code for item in result.issues])


class DirectionLookupTest(SimpleTestCase):
    def test_other_direction_is_not_proof_of_no_regulation(self):
        store = regulatory_store([regulatory_record(direction=Direction.IMPORT.value)])
        result = lookup(
            query=RegulatoryQuery(hs_code_raw=HS, direction=Direction.EXPORT, basis=BASIS),
            store=store,
        )
        self.assertEqual(result.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertIn(
            "REG_OTHER_VERSION_OR_DIRECTION", [item.code for item in result.issues]
        )
        self.assertIn(
            "不能据此判定本期本方向无监管",
            [item.message for item in result.issues][-1],
        )

    def test_both_record_matches_specific_direction(self):
        store = regulatory_store([regulatory_record(direction=Direction.BOTH.value, measures=[])])
        result = lookup(store=store)
        self.assertTrue(result.direction_matched)
        self.assertEqual(result.outcome, RegulatoryOutcome.DECLARED_NONE)

    def test_exact_direction_record_wins_over_both(self):
        store = regulatory_store(
            [
                regulatory_record(direction=Direction.BOTH.value, measures=[]),
                regulatory_record(direction=Direction.IMPORT.value, measures=[measure()]),
            ]
        )
        result = lookup(store=store)
        self.assertEqual(result.record.direction, Direction.IMPORT)
        self.assertEqual(result.outcome, RegulatoryOutcome.MEASURES_REQUIRED)

    def test_subheading_code_is_not_a_declaration_conclusion(self):
        store = regulatory_store([regulatory_record(hs_code="72081000")])
        result = lookup(
            query=RegulatoryQuery(hs_code_raw="72081000", direction=Direction.IMPORT, basis=BASIS),
            store=store,
        )
        self.assertEqual(result.format.state.value, "subheading_only")
        self.assertIn("REG_SUBHEADING_ONLY", [item.code for item in result.issues])


class DualUseTest(SimpleTestCase):
    def setUp(self):
        self.store = regulatory_store(
            [regulatory_record(dual_use=[dual_use_item()])]
        )

    def test_dual_use_hit_always_needs_expert(self):
        result = lookup(store=self.store)
        self.assertTrue(result.expert_review_required)
        self.assertEqual(len(result.dual_use), 1)
        self.assertEqual(result.dual_use[0].verdict, DualUseVerdict.UNKNOWN)
        self.assertIn("REG_DUAL_USE_UNVERIFIED", [item.code for item in result.issues])
        self.assertTrue(any("两用物项线索" in reason for reason in result.pending_reasons))
        # 两用物项未判定前，监管分项不算核清：不可作依据、必须转专家（与措施类别=dual_use 一致）。
        self.assertFalse(result.usable_as_basis)
        self.assertEqual(len(result.evidence), 3)

    def test_dual_use_evidence_is_locatable(self):
        result = lookup(store=self.store)
        ids = [item.evidence_id for item in result.evidence]
        self.assertIn("reg:2026:8482102000:import:dual1", ids)

    def test_dual_use_verdict_cannot_be_decided(self):
        with self.assertRaises(ValidationError) as caught:
            DualUseItem.model_validate(
                {**dual_use_item(), "verdict": "applies"}
            )
        self.assertIn("Input should be 'unknown'", str(caught.exception))

    def test_unlocated_dual_use_is_reported(self):
        store = regulatory_store(
            [regulatory_record(dual_use=[dual_use_item(document=None, locator=None)])]
        )
        result = lookup(store=store)
        self.assertIn("REG_DUAL_USE_UNLOCATED", [item.code for item in result.issues])
        self.assertEqual(len(result.evidence), 2)


class CompatibilityAndFailureTest(SimpleTestCase):
    def test_unconfigured_source_keeps_unknown(self):
        import os
        from unittest.mock import patch

        source = RegulatorySource(EnvRegulatoryStore(), clock=FIXED_CLOCK)
        with patch.dict(os.environ, {"CUSTOMS_REGULATORY_PATH": ""}):
            result = source.lookup(
                RegulatoryQuery(hs_code_raw=HS, direction=Direction.IMPORT, basis=BASIS)
            )
        self.assertEqual(result.query_status, QueryStatus.SOURCE_UNCONFIGURED)
        self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertIn("不等于无监管要求", result.reason)

    def test_store_crash_is_structured_failure(self):
        class BrokenStore:
            def load(self):
                raise RuntimeError("测试：存储实现缺陷")

        result = RegulatorySource(BrokenStore(), clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(hs_code_raw=HS, direction=Direction.IMPORT, basis=BASIS)
        )
        self.assertEqual(result.query_status, QueryStatus.SOURCE_ERROR)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)

    def test_unverified_version_is_unknown(self):
        result = lookup(
            query=RegulatoryQuery(hs_code_raw=HS, direction=Direction.IMPORT, basis=QueryBasis())
        )
        self.assertEqual(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertEqual(result.outcome, RegulatoryOutcome.UNKNOWN)
        self.assertIn("不能宣称无监管要求", result.reason)


class ForgedResultTest(SimpleTestCase):
    def setUp(self):
        self.result = lookup()
        self.payload = self.result.model_dump(mode="json")

    def test_forging_outcome_is_rejected(self):
        payload = self.result.model_dump(mode="json")
        payload["outcome"] = "declared_none"
        with self.assertRaises(ValidationError) as caught:
            RegulatoryResult.model_validate(payload)
        self.assertIn("outcome 必须由来源与基准计算", str(caught.exception))

    def test_usable_requires_authoritative_source(self):
        payload = self.result.model_dump(mode="json")
        payload["authoritative"] = False
        with self.assertRaises(ValidationError) as caught:
            RegulatoryResult.model_validate(payload)
        self.assertIn("来源非权威时不得 usable_as_basis", str(caught.exception))

    def test_forging_direction_is_rejected(self):
        payload = self.result.model_dump(mode="json")
        payload["direction"] = "export"
        with self.assertRaises(ValidationError) as caught:
            RegulatoryResult.model_validate(payload)
        self.assertIn("direction 必须回填调用方给出的方向", str(caught.exception))

    def test_forging_measures_is_rejected(self):
        payload = self.result.model_dump(mode="json")
        payload["measures"] = [{**measure(code="B"), "name": "凭空补的名称"}]
        with self.assertRaises(ValidationError) as caught:
            RegulatoryResult.model_validate(payload)
        self.assertIn("measures 必须与记录逐条一致", str(caught.exception))

    def test_unknown_outcome_must_explain_why(self):
        store = regulatory_store([regulatory_record(measures=OMIT)])
        result = lookup(store=store)
        payload = result.model_dump(mode="json")
        payload["pending_reasons"] = []
        with self.assertRaises(ValidationError) as caught:
            RegulatoryResult.model_validate(payload)
        self.assertIn("unknown 必须写明为什么不能当成无监管", str(caught.exception))

    def test_usable_with_unknown_outcome_is_rejected(self):
        store = regulatory_store([regulatory_record(measures=OMIT)])
        result = lookup(store=store)
        payload = result.model_dump(mode="json")
        payload["usable_as_basis"] = True
        with self.assertRaises(ValidationError) as caught:
            RegulatoryResult.model_validate(payload)
        self.assertIn("usable_as_basis 只在有明确结论的结果上成立", str(caught.exception))


class DatasetKindTest(SimpleTestCase):
    def test_regulatory_dataset_rejects_tax_kind(self):
        from adapters.sources.regulatory_lookup import RegulatoryDatasetInfo

        with self.assertRaises(ValidationError) as caught:
            RegulatoryDatasetInfo.model_validate(
                {
                    "dataset_id": "x",
                    "source_type": "official_tax",
                    "authority": "authoritative",
                }
            )
        self.assertIn("不属于本来源类型", str(caught.exception))

    def test_synthetic_cannot_be_authoritative(self):
        from adapters.sources.regulatory_lookup import RegulatoryDatasetInfo

        with self.assertRaises(ValidationError):
            RegulatoryDatasetInfo.model_validate(
                {
                    "dataset_id": "x",
                    "source_type": RegulatorySourceKind.SYNTHETIC.value,
                    "authority": "authoritative",
                    "synthetic": True,
                }
            )
