"""P23 退税率单元格口径：空白不是 0%，百分比必须由该单元格自己的数字格式支撑。

守的规则：只有“数值 + 百分数格式”或“明确的文本百分数”才算税率；格式读不出、
格式不是百分数、文本写着“不予退税”都拒绝该行，不按“看起来像百分比”换算，
也不把空白补成 0。
"""

from __future__ import annotations

from adapters.sources.reference_import.convert_tax import rate_from_cell
from tests.p23.support import P23TestCase


class BlankRateTest(P23TestCase):
    def test_blank_cell_is_not_zero(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="", cell_kind="", percent_format=None, format_code=None
        )
        self.assertEqual((kind, raw, percent, issue), ("", "", None, None), "空白必须是“来源未提供”")
        self.assertIsNone(issue, "空白交给调用方记 pending，不在单元格层报错")

    def test_whitespace_cell_is_not_zero(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="   ", cell_kind="inlineStr", percent_format=None, format_code=None
        )
        self.assertEqual((kind, raw, percent, issue), ("", "", None, None))


class NumericRateTest(P23TestCase):
    """数值单元格：格式是百分数才乘 100。"""

    def test_numeric_with_percent_format_is_export_rebate(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="0.13", cell_kind="n", percent_format=True, format_code="0.00%"
        )
        self.assertIsNone(issue)
        self.assertEqual(kind, "export_rebate")
        self.assertAlmostEqual(percent, 13.0)
        self.assertEqual(raw, "13.00%")

    def test_numeric_zero_with_percent_format_is_zero_rate(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="0", cell_kind="n", percent_format=True, format_code="0.00%"
        )
        self.assertIsNone(issue)
        self.assertEqual(kind, "zero_rate", "明确的 0 才是零税率，空白不是")
        self.assertAlmostEqual(percent, 0.0)
        self.assertEqual(raw, "0.00%")

    def test_numeric_without_percent_format_is_rejected(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="0.13", cell_kind="n", percent_format=False, format_code="0.00"
        )
        self.assertEqual((kind, percent), ("", None), "格式不是百分数时不得换算")
        self.assertEqual(raw, "0.13", "必须保留单元格原值供人工核对")
        self.assertEqual(issue.code, "TAX_RATE_FORMAT_UNVERIFIED")
        self.assertEqual(issue.field, "出口退税率")
        self.assertIn("0.00", issue.message, "必须回显读到的格式代码")
        self.assertIn("不能按", issue.message)

    def test_numeric_with_unreadable_format_is_rejected(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="0.13", cell_kind="n", percent_format=None, format_code=None
        )
        self.assertEqual((kind, percent), ("", None))
        self.assertEqual(issue.code, "TAX_RATE_FORMAT_UNVERIFIED")
        self.assertIn("读不出", issue.message, "读不出格式必须明说，不能猜成百分数")

    def test_numeric_out_of_range_is_rejected(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="1.5", cell_kind="n", percent_format=True, format_code="0.00%"
        )
        self.assertEqual((kind, percent), ("", None))
        self.assertEqual(issue.code, "TAX_RATE_OUT_OF_RANGE")
        self.assertIn("0–100", issue.message)

    def test_numeric_scientific_notation_is_rejected(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="1.3E-1", cell_kind="n", percent_format=True, format_code="0.00%"
        )
        self.assertEqual((kind, percent), ("", None))
        self.assertEqual(issue.code, "TAX_RATE_CELL_NOT_NUMERIC")
        self.assertEqual(raw, "1.3E-1")


class TextRateTest(P23TestCase):
    """文本单元格：只接受 ``13%`` / ``13`` 这类明确写法。"""

    def test_text_percent_is_parsed(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="13%", cell_kind="inlineStr", percent_format=None, format_code=None
        )
        self.assertIsNone(issue)
        self.assertEqual(kind, "export_rebate")
        self.assertAlmostEqual(percent, 13.0)
        self.assertEqual(raw, "13.00%")

    def test_text_plain_number_is_ambiguous_not_guessed(self):
        """文本 "13" 没写百分号：到底是 13% 还是 0.13%？不猜，保持待核。"""

        kind, raw, percent, issue = rate_from_cell(
            raw_text="13", cell_kind="inlineStr", percent_format=None, format_code=None
        )
        self.assertEqual((kind, percent), ("", None), "不得替来源选一个方向")
        self.assertEqual(issue.code, "TAX_RATE_TEXT_AMBIGUOUS")
        self.assertIn("百分号", issue.message)

    def test_text_zero_without_percent_is_ambiguous(self):
        kind, _raw, percent, issue = rate_from_cell(
            raw_text="0", cell_kind="inlineStr", percent_format=None, format_code=None
        )
        self.assertEqual((kind, percent), ("", None))
        self.assertEqual(issue.code, "TAX_RATE_TEXT_AMBIGUOUS")

    def test_text_zero_percent_is_zero_rate(self):
        kind, _raw, percent, issue = rate_from_cell(
            raw_text="0%", cell_kind="inlineStr", percent_format=None, format_code=None
        )
        self.assertIsNone(issue)
        self.assertEqual(kind, "zero_rate")
        self.assertAlmostEqual(percent, 0.0)

    def test_text_policy_wording_is_rejected(self):
        kind, raw, percent, issue = rate_from_cell(
            raw_text="不予退税", cell_kind="inlineStr", percent_format=None, format_code=None
        )
        self.assertEqual((kind, percent), ("", None), "政策口径不是税率，不得换算")
        self.assertEqual(issue.code, "TAX_RATE_CELL_NOT_NUMERIC")
        self.assertIn("不予退税", issue.message)
        self.assertIn("本批未提供", issue.message, "必须说明处理方式字段本批没有，不能猜")
        self.assertEqual(raw, "不予退税")
