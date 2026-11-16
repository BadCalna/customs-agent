"""申报要素原文解析：位置式/编号式/键值式、歧义与冲突。"""
from django.test import SimpleTestCase

from adapters.sources.declaration_elements import (
    DeclarationElementsSource,
    ElementQuery,
    ParseMode,
    ParseStatus,
)
from adapters.sources.hs_lookup import QueryBasis
from tests.p14.helpers import (
    FIXED_CLOCK,
    element_schema,
    element_spec,
    element_store,
    numbered_declared,
    positional_declared,
)


class ParseTest(SimpleTestCase):
    def check(self, **overrides):
        query = ElementQuery(
            hs_code_raw="8544422900",
            basis=QueryBasis(declaration_date="2026-05-20"),
            **overrides,
        )
        return DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(query)

    def test_positional_full_length_is_parsed(self):
        result = self.check(declared_raw=positional_declared())
        self.assertIs(result.parse.mode, ParseMode.POSITIONAL)
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.parse.token_count, 5)
        self.assertEqual(result.missing_count, 0)
        self.assertFalse(result.parse.missing_orders)

    def test_positional_short_is_partial_not_missing(self):
        result = self.check(declared_raw="无品牌|工业设备连接|X-100")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertEqual(result.parse.missing_orders, [4, 5])
        self.assertEqual(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])
        self.assertIn("ELEMENT_PARSE_PARTIAL", [gap.code for gap in result.gaps])

    def test_pipe_template_is_not_treated_as_filled_facts(self):
        result = self.check(declared_raw="品牌类型|用途|型号|品牌|GTIN")
        self.assertIs(result.parse.status, ParseStatus.TEMPLATE_ONLY)
        self.assertIs(result.sufficiency.value, "insufficient")
        self.assertTrue(all(item.declared_value == "" for item in result.checks))
        self.assertTrue(result.supplement_items)

    def test_pipe_key_value_segments_are_parsed_as_keyed_not_positional(self):
        result = self.check(
            declared_raw="品牌类型:无品牌|用途:工业设备连接|型号:X-100|品牌:无|GTIN:"
        )
        self.assertIs(result.parse.mode, ParseMode.KEYED)
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertIs(result.sufficiency.value, "sufficient")
        self.assertEqual(result.checks[0].declared_value, "无品牌")
        self.assertEqual(result.invalid_count, 0)

    def test_pipe_mixed_key_value_and_positional_is_partial(self):
        result = self.check(declared_raw="品牌类型:无品牌|工业设备连接|X-100")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])

    def test_numbered_unknown_labels_are_not_silently_used_as_values(self):
        result = self.check(declared_raw="1:牌子类型;2:作用;3:规格型号")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertTrue(result.parse.unrecognized_keys)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])

    def test_numbered_value_only_is_not_guessed(self):
        result = self.check(declared_raw="1:无品牌;2:工业设备连接;3:X-100")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])

    def test_positional_extra_is_partial(self):
        result = self.check(declared_raw=positional_declared() + "|多余值")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIn("多出/缺少", result.parse.note)

    def test_numbered_with_labels_and_values_is_parsed(self):
        result = self.check(declared_raw=numbered_declared())
        self.assertIs(result.parse.mode, ParseMode.NUMBERED)
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.missing_count, 0)

    def test_keyed_aliases_are_resolved(self):
        result = self.check(declared_raw="品牌类型:无品牌;使用用途:工业设备连接;型号:X-100;品牌:无")
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.missing_count, 0)
        self.assertEqual(result.checks[1].declared_key, "使用用途")

    def test_newline_tab_and_space_before_label_are_segment_boundaries(self):
        for raw in (
            "品牌类型:无品牌\n用途:工业\n型号:X",
            "品牌类型:无品牌\t用途:工业\t型号:X",
            "品牌类型:无品牌 用途:工业 型号:X",
        ):
            with self.subTest(raw=raw):
                result = self.check(declared_raw=raw)
                self.assertIs(result.parse.status, ParseStatus.PARSED)
                self.assertNotIn("\n", result.checks[0].declared_value or "")
                self.assertNotIn("\t", result.checks[0].declared_value or "")
                self.assertEqual(result.checks[1].declared_value, "工业")
                self.assertEqual(result.checks[2].declared_value, "X")
                self.assertEqual(result.parse.unrecognized_keys, [])

    def test_value_with_space_before_next_label_is_split_not_swallowed(self):
        result = self.check(declared_raw="品牌类型:无品牌;用途:用于 型号:X;品牌:ACME")
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.checks[1].declared_value, "用于")
        self.assertEqual(result.checks[2].declared_value, "X")

    def test_mixed_pipe_and_semicolon_is_partial(self):
        result = self.check(declared_raw="品牌类型:无品牌|用途;型号:X")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])

    def test_trailing_label_without_colon_is_not_silently_split(self):
        specs = [element_spec("a", "甲", 1), element_spec("b", "乙", 2, requirement="optional")]
        store = element_store(schemas=[element_schema(specs=specs)])
        src = DeclarationElementsSource(store, clock=FIXED_CLOCK)
        result = src.check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=QueryBasis(tariff_version="2026"),
                declared_raw="甲:正确值 乙",
            )
        )
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.checks[0].declared_value, "正确值 乙")
        self.assertIs(result.sufficiency.value, "sufficient")

    def test_newline_or_tab_bare_label_is_partial_not_swallowed(self):
        specs = [element_spec("a", "甲", 1), element_spec("b", "乙", 2, requirement="optional")]
        store = element_store(schemas=[element_schema(specs=specs)])
        src = DeclarationElementsSource(store, clock=FIXED_CLOCK)
        for raw in ("甲:正确值\n乙", "甲:正确值\t乙"):
            with self.subTest(raw=raw):
                result = src.check(
                    ElementQuery(
                        hs_code_raw="8544422900",
                        basis=QueryBasis(tariff_version="2026"),
                        declared_raw=raw,
                    )
                )
                self.assertIs(result.parse.status, ParseStatus.PARTIAL)
                self.assertIs(result.sufficiency.value, "unverified")

    def test_keyed_unknown_label_is_partial_not_silently_ignored(self):
        result = self.check(declared_raw="品牌类型:无品牌;用途:工业设备连接;型号:X-100;品牌:无;奇怪要素:值")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIn("奇怪要素", result.parse.unrecognized_keys)
        self.assertEqual(result.sufficiency.value, "unverified")

    def test_template_only_is_treated_as_unfilled(self):
        result = self.check(declared_raw="1:品牌类型;2:用途;3:型号;4:品牌;5:GTIN")
        self.assertIs(result.parse.status, ParseStatus.TEMPLATE_ONLY)
        self.assertEqual(result.sufficiency.value, "insufficient")
        self.assertIn("ELEMENT_MISSING", [gap.code for gap in result.gaps])

    def test_numbering_gap_is_partial(self):
        result = self.check(declared_raw="1:品牌类型:无品牌;2:用途:工业;4:品牌:无")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIn(3, result.parse.missing_orders)
        self.assertEqual(result.sufficiency.value, "unverified")

    def test_duplicate_numbering_is_partial(self):
        result = self.check(
            declared_raw="1:品牌类型:无品牌;1:用途:工业;2:型号:X-100;3:品牌:无"
        )
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertTrue(any("重复" in item for item in result.parse.ambiguous_keys))

    def test_duplicate_element_name_is_partial_and_first_value_is_kept(self):
        result = self.check(
            declared_raw=(
                "1:品牌类型:无品牌;2:品牌类型:境外品牌（其它）;"
                "3:用途:工业;4:型号:X-100;5:品牌:ACME"
            )
        )
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])
        brand_type = next(item for item in result.checks if item.key == "brand_type")
        self.assertEqual(brand_type.declared_value, "无品牌")
        self.assertTrue(any("重复" in item for item in result.parse.ambiguous_keys))

    def test_duplicate_keyed_element_name_is_partial(self):
        result = self.check(
            declared_raw="品牌类型:无品牌;品牌类型:境外品牌（其它）;用途:工业;型号:X-100;品牌:ACME"
        )
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")

    def test_duplicate_structured_alias_is_partial(self):
        result = self.check(
            declared={
                "品牌": "ACME",
                "品牌（中文或外文名称）": "OTHER",
                "品牌类型": "无品牌",
                "用途": "工业",
                "型号": "X-100",
            }
        )
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")

    def test_ambiguous_element_name_is_not_guessed(self):
        specs = [
            element_spec("tungsten", "成分含量（钨的含量）", 1),
            element_spec("molybdenum", "成分含量（钼的含量）", 2),
        ]
        store = element_store(schemas=[element_schema(specs=specs)])
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=QueryBasis(tariff_version="2026"),
                declared={"成分含量": "99%"},
            )
        )
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertTrue(result.parse.ambiguous_keys)

    def test_mapping_wins_over_raw_but_conflict_blocks_conclusion(self):
        result = self.check(
            declared_raw=positional_declared(),
            declared={"model": "Y-200"},
        )
        self.assertIs(result.parse.mode, ParseMode.MIXED)
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIn("型号", result.parse.conflicts)
        self.assertEqual(result.checks[2].declared_value, "Y-200")
        self.assertEqual(result.sufficiency.value, "unverified")
        self.assertEqual(result.supplement_items, [])

    def test_raw_and_mapping_agree_is_parsed(self):
        result = self.check(
            declared_raw=positional_declared(),
            declared={"型号": "X-100"},
        )
        self.assertIs(result.parse.mode, ParseMode.MIXED)
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.parse.conflicts, [])
        self.assertEqual(result.sufficiency.value, "sufficient")

    def test_structured_mapping_alone_is_parsed(self):
        result = self.check(declared={"brand_type": "无品牌", "usage": "工业", "model": "X-100", "brand": "无"})
        self.assertIs(result.parse.mode, ParseMode.MAPPING)
        self.assertIs(result.parse.status, ParseStatus.PARSED)
        self.assertEqual(result.parse.note, "使用结构化要素；未提供原文")

    def test_empty_declared_facts_are_insufficient_and_actionable(self):
        result = self.check()
        self.assertIs(result.parse.status, ParseStatus.EMPTY)
        self.assertIs(result.sufficiency.value, "insufficient")
        self.assertEqual(result.required_total, 3)
        self.assertEqual(result.missing_count, 3)
        self.assertEqual(
            [item.key for item in result.supplement_items], ["brand_type", "usage", "model"]
        )
        self.assertIn("ELEMENT_MISSING", [gap.code for gap in result.gaps])
        self.assertIs(result.schema_status.value, "obtained")

    def test_no_query_at_all_is_not_checked(self):
        result = DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(None)
        self.assertIs(result.sufficiency.value, "not_checked")
        self.assertEqual(result.supplement_items, [])
        self.assertIsNone(result.schema_status)

    def test_oversized_raw_is_rejected_before_query(self):
        result = self.check(declared_raw="x" * 10_001)
        self.assertIs(result.sufficiency.value, "not_checked")
        self.assertIn("超过", result.reason)
        self.assertEqual(result.supplement_items, [])

    def test_nfkc_expansion_over_limit_is_unverified_not_truncated(self):
        # ﬃ 归一化为 ffi 后长度膨胀；原串在限制内，但归一化后超过上限。
        raw = "品牌类型:无品牌;" + ("ﬃ" * 3500) + ";型号:X-100"
        result = self.check(declared_raw=raw)
        self.assertIs(result.parse.status, ParseStatus.UNPARSED)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertIn("超过上限", result.parse.note)
        self.assertEqual(result.supplement_items, [])

    def test_absurdly_long_numbered_prefix_does_not_raise(self):
        result = self.check(declared_raw=("9" * 5000) + ":品牌类型")
        self.assertIs(result.parse.status, ParseStatus.PARTIAL)
        self.assertIs(result.sufficiency.value, "unverified")
        self.assertIn("未识别", result.parse.note)
        self.assertEqual(result.supplement_items, [])
