"""P15 历史成功记录检索：命中/无命中/关键差异/旧版本、四态分离与结构防误用。

样本全部是合成数据（``adapters/sources/fixtures/history_records.sample.json``），
测试只证明接口行为和差异标注，不证明真实历史库的检索效果。
"""
import json
import os
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.engines.protocols import RecommendOutput
from adapters.sources.history_search import (
    DEFAULT_RECORDS_PATH,
    ENV_RECORDS_PATH,
    OUTCOME_EVIDENCE_STATUS,
    USAGE_NOTE,
    HistoryCandidate,
    HistoryDatasetInfo,
    HistoryQuery,
    HistoryRecord,
    HistorySearchOutcome,
    HistorySearchResult,
    HistorySearchSource,
    HistorySourceRef,
    HistoryStoreError,
    JsonHistoryStore,
    default_records_path,
)
from apps.api.schemas import EvidenceStatus

BASE_DIR = Path(settings.BASE_DIR)
RECORDS_PATH = BASE_DIR / "adapters" / "sources" / "fixtures" / "history_records.sample.json"
CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "queries.json"
CLOCK_TEXT = "2026-11-15T00:00:00+08:00"
CLOCK = datetime.fromisoformat(CLOCK_TEXT)
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def make_source(**kwargs) -> HistorySearchSource:
    """固定时钟，避免“距今多少天”随运行日期漂移。"""

    return HistorySearchSource.load(RECORDS_PATH, clock=lambda: CLOCK, **kwargs)


def fixed_clock() -> datetime:
    return CLOCK


def case(case_id: str) -> dict:
    for item in CASES:
        if item["case_id"] == case_id:
            return item
    raise AssertionError(f"固定样例缺少用例：{case_id}")


def run_case(case_id: str, **kwargs):
    return make_source(**kwargs).search(HistoryQuery.model_validate(case(case_id)["query"]))


class FixedSampleCaseTest(SimpleTestCase):
    """固定检索样例逐条锁定；用例文件就是回交文档引用的“固定样例”。"""

    def test_fixture_dataset_is_labeled_synthetic(self):
        self.assertEqual(DEFAULT_RECORDS_PATH.resolve(), RECORDS_PATH.resolve())
        result = run_case("hit-basic")
        self.assertIsNotNone(result.dataset)
        self.assertTrue(result.dataset.synthetic)
        self.assertEqual(result.dataset.dataset_id, "synthetic-p15-v1")
        self.assertEqual(result.scanned_records, 5)

    def test_every_fixed_case(self):
        for item in CASES:
            with self.subTest(case=item["case_id"]):
                result = run_case(item["case_id"])
                self.assertEqual(result.outcome.value, item["expected_outcome"])
                self.assertEqual(result.query_gaps, item["expected_query_gaps"])
                expected_top = item["expected_top_record"]
                if expected_top is None:
                    self.assertEqual(result.candidates, [])
                    self.assertEqual(result.evidence_status, EvidenceStatus.NOT_FOUND)
                else:
                    self.assertEqual(result.candidates[0].record_id, expected_top)
                    self.assertEqual(result.evidence_status, EvidenceStatus.OBTAINED)
                by_id = {candidate.record_id: candidate for candidate in result.candidates}
                for record_id, keys in item["expected_diff_keys"].items():
                    self.assertEqual(by_id[record_id].diff_keys, keys)
                for record_id in item.get("expected_stale_records_include", []):
                    self.assertTrue(by_id[record_id].stale)
                for record_id in item.get("expected_expired_records", []):
                    self.assertIn("已过期", by_id[record_id].validity_note)

    def test_candidates_are_sorted_by_score(self):
        result = run_case("hit-basic")
        scores = [candidate.score for candidate in result.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreaterEqual(len(result.candidates), 2)

    def test_search_is_deterministic(self):
        first = run_case("hit-basic")
        second = run_case("hit-basic")
        self.assertEqual(first.model_dump(), second.model_dump())


class HitTest(SimpleTestCase):
    """有命中：历史 HS、历史时间、来源引用原样保留，并标明仅供引用。"""

    def setUp(self):
        self.result = run_case("hit-basic")
        self.top = self.result.candidates[0]

    def test_outcome_and_evidence_status(self):
        self.assertIs(self.result.outcome, HistorySearchOutcome.MATCHED)
        self.assertIs(self.result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertEqual(self.result.searched_at, CLOCK_TEXT)
        self.assertEqual(self.result.query.name, "不锈钢法兰盘")

    def test_historical_fields_are_preserved(self):
        self.assertEqual(self.top.record_id, "SYN-H-0001")
        self.assertEqual(self.top.historical_hs, "7307210000")
        self.assertEqual(self.top.historical_name, "不锈钢法兰盘")
        # 历史业务日期原样保留：不解析、不改写格式、不补默认时区。
        self.assertEqual(self.top.recorded_at, "2023-05-18")
        self.assertEqual(self.top.historical_outcome, "approved")

    def test_source_reference_is_locatable(self):
        self.assertEqual(self.top.source.document, "SYN-DECL-2023-0518-0042")
        self.assertEqual(self.top.source.locator, "第 3 项")
        self.assertIsNone(self.top.source.url)
        self.assertEqual(self.top.source.system, "synthetic-sample")

    def test_matched_keys_and_score_are_explainable(self):
        self.assertEqual(self.top.matched_keys, ["工艺", "材质", "用途", "规格"])
        self.assertEqual(self.top.diff_keys, [])
        self.assertEqual(self.top.unverified_keys, [])
        self.assertFalse(self.top.key_attribute_conflict)
        self.assertEqual(self.top.score, 1.0)
        self.assertIn("品名词元重合 1.00", self.top.similarity_note)
        self.assertIn("关键属性一致", self.top.similarity_note)

    def test_unknown_scope_and_validity_are_flagged(self):
        self.assertEqual(self.top.applicable_scope, "石油化工管道连接件")
        self.assertIsNone(self.top.valid_until)
        self.assertIn("未提供有效期", self.top.validity_note)
        self.assertIn("HS 版本与当前税则是否一致未核实", self.top.validity_note)
        self.assertIs(self.top.version_status, EvidenceStatus.VERSION_UNVERIFIED)

    def test_result_is_reference_only(self):
        self.assertIs(self.result.reference_only, True)
        self.assertEqual(self.result.usage_note, USAGE_NOTE)
        self.assertEqual(self.top.usage_note, USAGE_NOTE)
        self.assertIs(self.top.reference_only, True)

    def test_evidence_entries_are_obtained_not_fabricated(self):
        entries = self.result.to_evidence_entries()
        self.assertEqual(len(entries), len(self.result.candidates))
        entry = entries[0]
        self.assertEqual(entry.evidence_id, "history:SYN-H-0001")
        self.assertEqual(entry.source_type, "history")
        self.assertIs(entry.status, EvidenceStatus.OBTAINED)
        self.assertEqual(entry.document, "SYN-DECL-2023-0518-0042")
        self.assertEqual(entry.locator, "第 3 项")
        self.assertEqual(entry.retrieved_at, CLOCK_TEXT)
        self.assertIn("2023-05-18", entry.note)
        self.assertIn("参考", entry.note)
        self.assertIn("合成样本", entry.title)
        self.assertIsNone(entry.effective_from)
        self.assertEqual(entry.scope, "石油化工管道连接件")
        self.assertEqual(entry.query["name"], "不锈钢法兰盘")

    def test_source_url_is_preserved_when_present(self):
        result = make_source().search(HistoryQuery(name="热轧不锈钢卷"))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        top = result.candidates[0]
        self.assertEqual(top.record_id, "SYN-H-0004")
        self.assertEqual(
            top.source.url,
            "https://synthetic.invalid/declaration/SYN-DECL-2024-0220-0088",
        )
        self.assertEqual(
            result.to_evidence_entries()[0].url,
            "https://synthetic.invalid/declaration/SYN-DECL-2024-0220-0088",
        )


class NoMatchTest(SimpleTestCase):
    """无命中是检索成功后的“没查到”，不能写成查询失败。"""

    def test_no_match_is_not_a_failure(self):
        result = run_case("no-match")
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)
        self.assertIs(result.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.to_evidence_entries(), [])
        self.assertIn("没查到", result.reason)
        self.assertIn("不是查询失败", result.reason)
        self.assertEqual(result.scanned_records, 5)
        self.assertIsNotNone(result.dataset)


class KeyAttributeDiffTest(SimpleTestCase):
    """关键属性差异必须独立呈现：总分再高也不能把它藏进 similarity_note 之外。"""

    def setUp(self):
        self.result = run_case("key-attribute-diff")
        self.by_id = {candidate.record_id: candidate for candidate in self.result.candidates}

    def test_top_match_is_the_consistent_record(self):
        self.assertEqual(self.result.candidates[0].record_id, "SYN-H-0002")
        self.assertEqual(self.result.candidates[0].diff_keys, [])
        self.assertEqual(self.result.candidates[0].score, 1.0)

    def test_conflicting_attributes_stay_in_diff_keys(self):
        conflict = self.by_id["SYN-H-0001"]
        self.assertEqual(conflict.diff_keys, ["材质", "规格"])
        self.assertTrue(conflict.key_attribute_conflict)
        self.assertIn("关键属性存在差异，不得直接沿用历史码", conflict.similarity_note)
        self.assertIn("材质（查询 碳钢 / 历史 不锈钢）", conflict.similarity_note)
        self.assertIn("规格（查询 DN80 / 历史 DN50）", conflict.similarity_note)

    def test_conflict_is_penalized_but_still_reported(self):
        conflict = self.by_id["SYN-H-0001"]
        self.assertLess(conflict.score, self.by_id["SYN-H-0002"].score)
        # 品名完全一致、分数不低，仍不能因为“看起来很像”而吞掉材质差异。
        self.assertGreaterEqual(conflict.score, 0.35)
        self.assertIn("材质", conflict.diff_keys)

    def test_missing_record_attribute_is_unverified_not_matched(self):
        # R-07 后"一个共同事实都没有"的记录不会再成为候选，因此这里补一个真实匹配键，
        # 只观察"历史未提供的属性"如何被标注。
        result = make_source().search(
            HistoryQuery(name="不锈钢法兰盘", elements={"材质": "不锈钢", "表面处理": "酸洗"})
        )
        candidate = next(item for item in result.candidates if item.record_id == "SYN-H-0001")
        self.assertIn("材质", candidate.matched_keys)
        self.assertIn("表面处理", candidate.unverified_keys)
        self.assertNotIn("表面处理", candidate.matched_keys)
        self.assertNotIn("表面处理", candidate.diff_keys)


class OldVersionTest(SimpleTestCase):
    """旧时间版本：日期原样保留，并明确标注版本未核实、有效期已过。"""

    def setUp(self):
        self.result = run_case("old-version")
        self.old = next(
            candidate for candidate in self.result.candidates if candidate.record_id == "SYN-H-0003"
        )

    def test_old_record_is_top_and_keeps_raw_timestamp(self):
        self.assertEqual(self.result.candidates[0].record_id, "SYN-H-0003")
        self.assertEqual(self.old.recorded_at, "2019-03-12T10:20:00+08:00")

    def test_version_and_validity_are_marked_unverified_or_expired(self):
        self.assertEqual(self.old.hs_version, "2017")
        self.assertEqual(self.old.valid_until, "2021-12-31")
        self.assertTrue(self.old.stale)
        self.assertGreater(self.old.age_days, 2000)
        self.assertIn("已过期", self.old.validity_note)
        self.assertIn("HS 版本", self.old.validity_note)
        self.assertEqual(self.old.version_status, EvidenceStatus.VERSION_UNVERIFIED)

    def test_old_record_source_still_locatable(self):
        self.assertEqual(self.old.source.document, "SYN-DECL-2019-0312-0009")
        self.assertEqual(self.old.source.locator, "第 2 项")


class OutcomeSeparationTest(SimpleTestCase):
    """no_match 与 query_failed 分开；存储异常不得伪装成“没查到”。"""

    def test_outcome_to_evidence_status_mapping_is_fixed(self):
        self.assertIs(OUTCOME_EVIDENCE_STATUS[HistorySearchOutcome.MATCHED], EvidenceStatus.OBTAINED)
        self.assertIs(OUTCOME_EVIDENCE_STATUS[HistorySearchOutcome.NO_MATCH], EvidenceStatus.NOT_FOUND)
        self.assertIs(
            OUTCOME_EVIDENCE_STATUS[HistorySearchOutcome.QUERY_FAILED],
            EvidenceStatus.QUERY_FAILED,
        )
        self.assertIsNone(OUTCOME_EVIDENCE_STATUS[HistorySearchOutcome.INVALID_QUERY])

    def test_store_error_is_query_failed(self):
        class FailingStore:
            def load(self):
                raise HistoryStoreError("模拟存储不可读")

        result = HistorySearchSource(store=FailingStore(), clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIs(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.to_evidence_entries(), [])
        self.assertIn("检索失败", result.reason)
        self.assertIn("不能记作 no_match", result.reason)

    def test_unexpected_store_bug_is_still_query_failed(self):
        class BrokenStore:
            def load(self):
                raise RuntimeError("模拟实现缺陷")

        result = HistorySearchSource(store=BrokenStore(), clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIn("RuntimeError", result.reason)

    def test_no_match_and_query_failed_do_not_collide(self):
        class FailingStore:
            def load(self):
                raise HistoryStoreError("模拟存储不可读")

        no_match = run_case("no-match")
        failed = HistorySearchSource(store=FailingStore(), clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertNotEqual(no_match.outcome, failed.outcome)
        self.assertNotEqual(no_match.evidence_status, failed.evidence_status)
        self.assertIs(no_match.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertIs(failed.evidence_status, EvidenceStatus.QUERY_FAILED)

    def test_missing_file_is_query_failed(self):
        source = HistorySearchSource.load(Path("/tmp/p15-does-not-exist.json"), clock=fixed_clock)
        result = source.search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIn("不可读", result.reason)

    def test_malformed_record_is_query_failed(self):
        source = HistorySearchSource(
            [{"record_id": "X-1", "historical_hs": "7307210000"}], clock=fixed_clock
        )
        result = source.search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIn("不符合契约", result.reason)


class InvalidInputTest(SimpleTestCase):
    """脏输入返回空候选 + 原因，不抛 500，也不冒充“没查到”。"""

    def setUp(self):
        self.source = make_source()

    def test_dirty_inputs_return_invalid_query_instead_of_raising(self):
        inputs = [
            None,
            "",
            123,
            [],
            {},
            {"name": 123},
            {"elements": ["材质"]},
            {"name": "法兰", "attributes": {"材质": 1}},
            HistoryQuery(),
            HistoryQuery(name="   "),
            object(),
        ]
        for value in inputs:
            with self.subTest(value=repr(value)):
                result = self.source.search(value)
                self.assertIs(result.outcome, HistorySearchOutcome.INVALID_QUERY)
                self.assertIsNone(result.evidence_status)
                self.assertEqual(result.candidates, [])
                self.assertTrue(result.reason)
                self.assertIsNone(result.query)
                self.assertIsNone(result.searched_at)

    def test_model_construct_bypass_is_invalid_not_crash(self):
        """即使调用方绕过字段校验，也只能得到 invalid_query，不能抛异常。"""

        broken = HistoryQuery.model_construct(
            name=None, elements=None, spec=None, material=None, attributes=None
        )
        result = self.source.search(broken)
        self.assertIs(result.outcome, HistorySearchOutcome.INVALID_QUERY)
        self.assertTrue(result.reason)

    def test_overlong_name_is_truncated_not_raised(self):
        result = self.source.search(HistoryQuery(name="不" * 500))
        self.assertTrue(result.truncated)
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)

    def test_too_many_attributes_are_capped_and_flagged(self):
        # 键0..键79 与历史记录一条都对不上：R-07 之后必须判 no_match，不能靠 unverified 凑分。
        query = HistoryQuery(
            name="不锈钢法兰盘",
            elements={f"键{index}": f"值{index}" for index in range(80)},
        )
        result = self.source.search(query)
        self.assertTrue(result.truncated)
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)
        self.assertEqual(result.candidates, [])
        self.assertLessEqual(len(result.query.elements), 50)


class ScoreThresholdTest(SimpleTestCase):
    def test_limit_caps_candidates(self):
        result = make_source(limit=1).search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertEqual(len(result.candidates), 1)

    def test_min_score_can_reject_everything(self):
        result = make_source(min_score=0.99).search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)
        self.assertEqual(result.candidates, [])

    def test_missing_elements_is_discounted_and_flagged(self):
        result = make_source().search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertEqual(result.query_gaps, ["申报要素"])
        self.assertEqual(result.candidates[0].score, 0.8)
        self.assertIn("输入缺口", result.reason)


class StructuralGuardTest(SimpleTestCase):
    """历史成功码不能在结构上被当成当前结论。"""

    def setUp(self):
        self.result = run_case("hit-basic")

    def test_no_current_conclusion_field_names(self):
        for model in (HistorySearchResult, HistoryCandidate):
            self.assertNotIn("suggested_hs", model.model_fields)
        self.assertNotIn("hs", HistoryCandidate.model_fields)
        self.assertNotIn("confidence", HistoryCandidate.model_fields)

    def test_reference_only_cannot_be_disabled(self):
        with self.assertRaises(ValidationError):
            HistorySearchResult(**(self.result.model_dump() | {"reference_only": False}))
        candidate = self.result.candidates[0]
        with self.assertRaises(ValidationError):
            HistoryCandidate(**(candidate.model_dump() | {"reference_only": False}))

    def test_history_candidates_cannot_enter_recommend_output(self):
        with self.assertRaises(ValidationError):
            RecommendOutput(candidates=self.result.candidates)

    def test_outcome_and_status_must_match(self):
        with self.assertRaises(ValidationError):
            HistorySearchResult(
                outcome=HistorySearchOutcome.NO_MATCH,
                evidence_status=EvidenceStatus.QUERY_FAILED,
            )
        with self.assertRaises(ValidationError):
            HistorySearchResult(
                outcome=HistorySearchOutcome.MATCHED,
                evidence_status=EvidenceStatus.OBTAINED,
                candidates=[],
            )
        with self.assertRaises(ValidationError):
            HistorySearchResult(
                outcome=HistorySearchOutcome.INVALID_QUERY,
                candidates=self.result.candidates,
            )


class DatasetDeclarationTest(SimpleTestCase):
    def test_in_memory_records_can_declare_real_data(self):
        record = JsonHistoryStore(RECORDS_PATH).load().records[0]
        source = HistorySearchSource(
            [record],
            dataset=HistoryDatasetInfo(dataset_id="real-export-check", synthetic=False),
            clock=fixed_clock,
        )
        result = source.search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        self.assertIs(result.dataset.synthetic, False)
        self.assertNotIn("合成样本", result.to_evidence_entries()[0].title)

    def test_undeclared_dataset_is_labeled_not_silently_trusted(self):
        record = JsonHistoryStore(RECORDS_PATH).load().records[0]
        result = HistorySearchSource([record], clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertIsNone(result.dataset.synthetic)
        self.assertIn("数据来源未声明", result.to_evidence_entries()[0].title)

    def test_env_override_changes_default_path(self):
        previous = os.environ.get(ENV_RECORDS_PATH)
        os.environ[ENV_RECORDS_PATH] = str(RECORDS_PATH)
        try:
            self.assertEqual(default_records_path().resolve(), RECORDS_PATH.resolve())
        finally:
            if previous is None:
                os.environ.pop(ENV_RECORDS_PATH, None)
            else:
                os.environ[ENV_RECORDS_PATH] = previous


class NeedsReviewRecordTest(SimpleTestCase):
    """待核异常记录仍可被检索，但必须把标记透传到候选与证据。"""

    def _record(self, **overrides):
        payload = {
            "record_id": "HIST-REVIEW-001",
            "historical_hs": "7307210000",
            "historical_name": "待核法兰盘",
            "elements": {"材质": "不锈钢"},
            "material": "不锈钢",
            "spec": "DN50",
            "recorded_at": "2025-01-01",
            "outcome": "approved",
            "source": HistorySourceRef(document="SYN-REVIEW", locator="第 1 项"),
        }
        payload.update(overrides)
        return HistoryRecord(**payload)

    def test_needs_review_is_propagated_to_candidate_and_evidence(self):
        record = self._record(
            attributes={
                "needs_review": "true",
                "blocking_issue_codes": "reversed_item_range,missing_contract",
            }
        )
        source = HistorySearchSource(
            records=[record], clock=fixed_clock, include_needs_review=True
        )
        result = source.search(HistoryQuery(name="待核法兰盘", elements={"材质": "不锈钢"}))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        candidate = result.candidates[0]
        self.assertTrue(candidate.needs_review)
        self.assertEqual(candidate.blocking_issue_codes, ["reversed_item_range", "missing_contract"])
        self.assertIn("待核异常", result.reason)

        entries = result.to_evidence_entries()
        self.assertEqual(len(entries), 1)
        self.assertIn("待核异常", entries[0].title)
        self.assertNotIn("历史成功记录", entries[0].title)
        self.assertIn("reversed_item_range", entries[0].note)
        self.assertIn("不能作为成功案例", entries[0].note)

    def test_blocking_codes_without_explicit_flag_still_marks_review(self):
        record = self._record(attributes={"blocking_issue_codes": "missing_contract"})
        source = HistorySearchSource(
            records=[record], clock=fixed_clock, include_needs_review=True
        )
        result = source.search(HistoryQuery(name="待核法兰盘", elements={"材质": "不锈钢"}))
        candidate = result.candidates[0]
        self.assertTrue(candidate.needs_review)
        self.assertEqual(candidate.blocking_issue_codes, ["missing_contract"])

    def test_needs_review_is_excluded_from_success_hits_by_default(self):
        record = self._record(
            attributes={
                "needs_review": "true",
                "blocking_issue_codes": "reversed_item_range",
            }
        )
        source = HistorySearchSource(records=[record], clock=fixed_clock)
        result = source.search(HistoryQuery(name="待核法兰盘", elements={"材质": "不锈钢"}))
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.excluded_needs_review, 1)
        self.assertIn("待核异常", result.reason)
        self.assertIn("已排除", result.reason)

    def test_normal_record_is_not_flagged(self):
        record = self._record(attributes={"needs_review": "false"})
        source = HistorySearchSource(records=[record], clock=fixed_clock)
        result = source.search(HistoryQuery(name="待核法兰盘", elements={"材质": "不锈钢"}))
        candidate = result.candidates[0]
        self.assertFalse(candidate.needs_review)
        self.assertEqual(candidate.blocking_issue_codes, [])
        self.assertIn("历史成功记录", result.to_evidence_entries()[0].title)


class SuccessOutcomeFilterTest(SimpleTestCase):
    """只有明确成功的记录进入历史成功案例；已拒绝/缺失 outcome 一律排除（Jay 评审）。"""

    def _records(self):
        base = JsonHistoryStore(RECORDS_PATH).load().records[0].model_dump()
        rejected = dict(base, record_id="SYN-H-REJECT", outcome="rejected")
        missing = dict(base, record_id="SYN-H-NOOUTCOME")
        missing.pop("outcome", None)
        approved = dict(base, record_id="SYN-H-OK", outcome="approved")
        return rejected, missing, approved

    def test_rejected_record_is_excluded(self):
        rejected, _, approved = self._records()
        result = HistorySearchSource([rejected, approved], clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        ids = {c.record_id for c in result.candidates}
        self.assertEqual(ids, {"SYN-H-OK"})

    def test_missing_outcome_is_not_treated_as_success(self):
        _, missing, _ = self._records()
        result = HistorySearchSource([missing], clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)
        self.assertIn("符合成功口径 0 条", result.reason)

    def test_only_explicit_success_values_are_eligible(self):
        base = JsonHistoryStore(RECORDS_PATH).load().records[0].model_dump()
        rows = [
            dict(base, record_id=f"SYN-{value}", outcome=value)
            for value in ("approved", "passed", "放行", "通过", "returned", "", "unknown")
        ]
        result = HistorySearchSource(rows, clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        eligible = {c.record_id for c in result.candidates}
        self.assertEqual(eligible, {"SYN-approved", "SYN-passed", "SYN-放行", "SYN-通过"})
        self.assertIn("符合成功口径 4 条", result.reason)
