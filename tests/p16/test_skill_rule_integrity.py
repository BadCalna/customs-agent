"""Offline parser regressions; no model, business records or reference datasets."""
import hashlib
import tempfile
from pathlib import Path
from unittest import TestCase

from adapters.engines.mock import EngineTechnicalError
from adapters.engines.skill_precheck import DEFAULT_SKILL_PATH, _load_skill


class SkillRuleIntegrityTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "skill.md"
        self.source = DEFAULT_SKILL_PATH.read_text(encoding="utf-8")

    def check_rejected(self, changes):
        for label, before, after in changes:
            with self.subTest(label=label):
                self.assertEqual(self.source.count(before), 1)
                self.path.write_text(self.source.replace(before, after), encoding="utf-8")
                with self.assertRaisesRegex(EngineTechnicalError, "^预审Skill读取或阶段规则校验失败$"):
                    _load_skill(self.path)

    def test_duplicate_or_competing_section(self):
        common = "## 一、确认商品及两个页签的关联"
        stage = "## 二、业务员预审：独立推荐HS并比较"
        self.check_rejected([
            ("duplicate common", common, common + "\n2. SYNTHETIC\n" + common),
            ("duplicate stage", stage, stage + "\n1. SYNTHETIC\n" + stage),
            ("competing stage title", stage, stage + "（另稿）\n" + stage),
            ("trailing spaces", stage, stage + "  \n" + stage),
        ])

    def test_ambiguous_numbering_or_boundaries(self):
        self.check_rejected([
            ("duplicate", "2. **主品名**", "2. SYNTHETIC\n2. **主品名**"),
            ("duplicate omitted rule", "8. `预审结论`", "8. SYNTHETIC\n8. `预审结论`"),
            ("noncanonical separator", "8. `预审结论`", "8) `预审结论`"),
            ("renumbered omitted rule", "3. 在申报要素页", "5. 在申报要素页"),
            ("empty selected rule", "4. 有依据支持唯一", "4. \n有依据支持唯一"),
            ("nested pseudo-rule", "   对钢材类商品", "   8. SYNTHETIC\n   对钢材类商品"),
        ])

    def test_numeric_and_inline_code_continuations_are_valid(self):
        original = _load_skill(DEFAULT_SKILL_PATH)
        changed = self.source.replace(
            "   对钢材类商品，只要供应商申报要素填有成分百分比，就另做合计校验：逐一识别同一商品的元素及其百分数，按原值相加，目标为100%。明确数值合计不等于100%时，在`预审结论`披露已列成分、合计及与100%的差额，要求供应商核实或补齐；即使HS一致也不能省略。若只列部分元素、写“铁/Fe余量”、使用含量区间或数值单位不明，说明已列数值合计及无法核实总量的原因，不擅自把差额补为铁含量或修改原申报要素。",
            "   100%成分仍需逐项核对。\n   `10位编码`需保留来源上下文。",
        )
        self.path.write_text(changed, encoding="utf-8")
        result = _load_skill(self.path)
        self.assertIn("100%成分仍需逐项核对。", result["rules"])
        self.assertIn("`10位编码`需保留来源上下文。", result["rules"])
        self.assertNotEqual(result["sha256"], original["sha256"])


        for marker in ("读取`references`", "在映射底表`申报要素明细`取完整字段及必填标记"):
            self.check_rejected([("removed anchor", marker, "SYNTHETIC"),
                                 ("doubled anchor", marker, marker * 2)])

    def test_allowed_rules_multiline_and_provenance(self):
        original = _load_skill(DEFAULT_SKILL_PATH)
        self.path.write_bytes((self.source + "\n<!-- SYNTHETIC unused -->\n").replace("\n", "\r\n").encode())
        result = _load_skill(self.path)
        self.assertEqual(result["rules"], original["rules"])
        self.assertEqual(result["selected_rules_sha256"], hashlib.sha256(result["rules"].encode()).hexdigest())
        self.assertNotEqual(result["sha256"], original["sha256"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertIn("   对钢材类商品", result["rules"])
        self.assertNotIn("SYNTHETIC", result["rules"])
        self.assertNotIn("专家决定通过或退回", result["rules"])
