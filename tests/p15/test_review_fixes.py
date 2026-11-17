"""P15 审查遗留（R-07/R-08/R-09）的回归用例：无事实不算命中、脏入参不抛异常、日期型有效期按当天结束。"""
import json
from datetime import datetime

from django.test import SimpleTestCase

from adapters.sources.history_search import (
    HistoryDatasetInfo,
    HistoryQuery,
    HistorySearchOutcome,
    HistorySearchSource,
    _clean_attributes,
    _parse_time,
)

CLOCK_TEXT = "2026-11-15T00:00:00+08:00"
CLOCK = datetime.fromisoformat(CLOCK_TEXT)


def _record(record_id: str, name: str, attributes: dict, valid_until=None) -> dict:
    return {
        "record_id": record_id,
        "historical_hs": "7307210000",
        "historical_name": name,
        "elements": attributes,
        "attributes": {},
        "recorded_at": "2025-06-01",
        "outcome": "approved",
        "valid_until": valid_until,
        "source": {"document": "P15-REVIEW-FIXTURE", "locator": "第1行", "system": "synthetic-sample"},
    }


def _source(records: list[dict]) -> HistorySearchSource:
    return HistorySearchSource(
        records=records,
        dataset=HistoryDatasetInfo(dataset_id="p15-review-fix", synthetic=True),
        clock=lambda: CLOCK,
    )


class NoMatchedFactTest(SimpleTestCase):
    def test_query_without_any_matched_fact_is_not_a_hit(self):
        # R-07：只查询历史记录没有提供的属性，过去能靠 0.5×unverified 凑过 0.35 阈值。
        source = _source([_record("R-1", "不锈钢法兰盘", {"材质": "碳钢"})])
        result = source.search(HistoryQuery(name="不锈钢法兰盘", elements={"表面处理": "酸洗"}))
        self.assertIs(result.outcome, HistorySearchOutcome.NO_MATCH)
        self.assertEqual(result.candidates, [])

    def test_attribute_conflict_without_matched_key_is_still_a_candidate(self):
        # 同名、材质不同是真实的属性冲突（1×0.6×0.7=0.42），不是没有可比事实。
        source = _source([_record("R-conflict", "不锈钢法兰盘", {"材质": "不锈钢"})])
        result = source.search(HistoryQuery(name="不锈钢法兰盘", elements={"材质": "碳钢"}))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        candidate = result.candidates[0]
        self.assertEqual(candidate.score, 0.42)
        self.assertEqual(candidate.matched_keys, [])
        self.assertEqual(candidate.diff_keys, ["材质"])
        self.assertTrue(candidate.key_attribute_conflict)

    def test_one_matched_fact_still_hits(self):
        source = _source([_record("R-2", "不锈钢法兰盘", {"材质": "碳钢"})])
        result = source.search(HistoryQuery(name="不锈钢法兰盘", elements={"材质": "碳钢"}))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        self.assertEqual(result.candidates[0].matched_keys, ["材质"])

    def test_name_only_query_keeps_name_path(self):
        # 完全没有可比属性时不能一刀切丢掉，仍按"只靠品名打折"处理。
        source = _source([_record("R-3", "不锈钢法兰盘", {})])
        result = source.search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)


class DirtyInputTest(SimpleTestCase):
    def test_clean_attributes_ignores_non_mapping(self):
        # R-08：model_construct 可绕过校验，列表/字符串不能把检索变成 500。
        self.assertEqual(_clean_attributes(["材质", "碳钢"]), {})
        self.assertEqual(_clean_attributes("材质:碳钢"), {})
        self.assertEqual(_clean_attributes(None), {})

    def test_search_with_constructed_list_elements_does_not_raise(self):
        source = _source([_record("R-4", "不锈钢法兰盘", {"材质": "碳钢"})])
        query = HistoryQuery.model_construct(
            name="不锈钢法兰盘", elements=["材质"], spec="", material="", attributes={},
        )
        result = source.search(query)
        self.assertIsNotNone(result)
        # 列表入参被当作"无属性"处理：不抛异常，按品名路径或降级结果返回都算通过。
        self.assertIn(
            result.outcome,
            {HistorySearchOutcome.INVALID_QUERY, HistorySearchOutcome.QUERY_FAILED,
             HistorySearchOutcome.NO_MATCH, HistorySearchOutcome.MATCHED},
        )


class DateOnlyValidUntilTest(SimpleTestCase):
    def test_date_only_is_end_of_day(self):
        # R-09：valid_until=2026-12-31 表示有效至当天结束，不能按 UTC 零点提前判过期。
        parsed = _parse_time("2026-12-31")
        self.assertEqual((parsed.hour, parsed.minute, parsed.second), (23, 59, 59))

    def test_timestamp_is_untouched(self):
        parsed = _parse_time("2026-12-31T00:00:00+00:00")
        self.assertEqual((parsed.hour, parsed.minute, parsed.second), (0, 0, 0))

    def test_record_valid_on_its_last_day_is_not_expired(self):
        source = _source([_record("R-5", "不锈钢法兰盘", {"材质": "碳钢"}, valid_until="2026-11-15")])
        result = source.search(HistoryQuery(name="不锈钢法兰盘", elements={"材质": "碳钢"}))
        candidate = next(c for c in result.candidates if c.record_id == "R-5")
        # 时钟是 2026-11-15，记录有效至 2026-11-15 当天：不能判过期/过旧。
        self.assertFalse(candidate.stale)
        self.assertNotIn("过期", candidate.validity_note)
