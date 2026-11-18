"""P23 编码读取口径：文本保留前导零、8 位只算子目、数值单元格绝不补零。

守的规则：``read_hs_code`` 只回答“这格能不能原样当编码用”。前导零丢失、位数不对、
小数/科学计数法都按“精度已损坏”拒绝，并且 **不补零、不纠位、不截断**。
"""

from __future__ import annotations

from adapters.sources.reference_import.convert_catalog import CODE_LENGTHS, read_hs_code
from tests.p23.support import P23TestCase


class TextCellReadingTest(P23TestCase):
    """文本单元格：字符串原样保留，长度只认 8/10。"""

    def test_ten_digit_text_keeps_leading_zero(self):
        read = read_hs_code("0101210010", "inlineStr")
        self.assertTrue(read.ok)
        self.assertEqual(read.code, "0101210010", "必须保留前导零")
        self.assertEqual(read.level, "declaration")
        self.assertFalse(read.recovered_from_numeric)
        self.assertIsNone(read.issue)
        self.assertEqual(read.raw_text, "0101210010")

    def test_eight_digit_text_is_subheading_not_error(self):
        read = read_hs_code("01012100", "inlineStr")
        self.assertTrue(read.ok, "8 位是合法层级，不是错码")
        self.assertEqual(read.code, "01012100")
        self.assertEqual(read.level, "subheading")
        self.assertEqual(CODE_LENGTHS[8], "subheading")
        self.assertEqual(CODE_LENGTHS[10], "declaration")

    def test_nondigit_text_is_rejected_without_repair(self):
        read = read_hs_code("85A", "inlineStr")
        self.assertFalse(read.ok)
        self.assertEqual(read.code, "", "非法编码不得进入输出")
        self.assertEqual(read.issue.code, "HS_CODE_NONDIGIT")
        self.assertEqual(read.issue.field, "HS编码")
        self.assertIn("85A", read.issue.message, "报告必须回显原文")
        self.assertEqual(read.raw_text, "85A")

    def test_eleven_digit_text_is_rejected(self):
        read = read_hs_code("01012100101", "inlineStr")
        self.assertFalse(read.ok)
        self.assertEqual(read.code, "")
        self.assertEqual(read.issue.code, "HS_CODE_WRONG_LENGTH")
        self.assertIn("11", read.issue.message, "必须说明实际位数")

    def test_empty_text_is_rejected(self):
        read = read_hs_code("   ", "inlineStr")
        self.assertFalse(read.ok)
        self.assertEqual(read.issue.code, "HS_CODE_EMPTY")
        self.assertEqual(read.code, "")


class NumericCellReadingTest(P23TestCase):
    """数值单元格：只有长度无损（正好 8/10 位整数字面量）才采用。"""

    def test_nine_digit_numeric_reports_precision_lost(self):
        read = read_hs_code("101210010", "n")
        self.assertFalse(read.ok)
        self.assertEqual(read.code, "", "不得补零成 0101210010")
        self.assertEqual(read.issue.code, "HS_CELL_NUMERIC_PRECISION_LOST")
        self.assertIn("不补零", read.issue.message)
        self.assertFalse(read.recovered_from_numeric)

    def test_ten_digit_numeric_is_accepted_and_flagged(self):
        read = read_hs_code("1012100100", "n")
        self.assertTrue(read.ok, "10 位整数字面量不存在前导零丢失，可原样采用")
        self.assertEqual(read.code, "1012100100")
        self.assertEqual(read.level, "declaration")
        self.assertTrue(read.recovered_from_numeric, "必须标记来源是数值单元格")

    def test_eight_digit_numeric_is_accepted_as_subheading(self):
        read = read_hs_code("10121001", "n")
        self.assertTrue(read.ok)
        self.assertEqual(read.level, "subheading")
        self.assertTrue(read.recovered_from_numeric)

    def test_scientific_notation_numeric_is_rejected(self):
        read = read_hs_code("1.0121001E8", "n")
        self.assertFalse(read.ok)
        self.assertEqual(read.code, "")
        self.assertEqual(read.issue.code, "HS_CELL_NUMERIC_PRECISION_LOST")
        self.assertIn("精度", read.issue.message)

    def test_decimal_numeric_is_rejected(self):
        read = read_hs_code("10121001.0", "n")
        self.assertFalse(read.ok)
        self.assertEqual(read.issue.code, "HS_CELL_NUMERIC_PRECISION_LOST")

    def test_unsupported_cell_kind_is_rejected(self):
        read = read_hs_code("1012100100", "b")
        self.assertFalse(read.ok)
        self.assertEqual(read.issue.code, "HS_CELL_TYPE_UNSUPPORTED")
        self.assertIn("'b'", read.issue.message)
