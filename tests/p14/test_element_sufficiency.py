"""事实充足性判定：充分/不足/待核/未核对，以及补料项边界。"""
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.sources.declaration_elements import (
    DeclarationElementsResult,
    DeclarationElementsSource,
    ElementQuery,
    ElementStoreError,
    ElementStoreNotConfigured,
    Sufficiency,
)
from adapters.sources.hs_lookup import (
    EvidenceStatus,
    QueryBasis,
    QueryStatus,
    SourceKind,
)
from tests.p14.helpers import (
    FIXED_CLOCK,
    element_schema,
    element_spec,
    element_store,
    numbered_declared,
    official_elements_dataset,
    synthetic_dataset,
)


class ElementSufficiencyTest(SimpleTestCase):
    def setUp(self):
        self.basis = QueryBasis(declaration_date="2026-05-20")

    def check(self, **overrides):
        params = {"hs_code_raw": "8544422900", "basis": self.basis}
        params.update(overrides)
        query = ElementQuery(**params)
        return DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(query)

    def test_complete_required_facts_are_sufficient(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业设备连接", "model": "X-100", "brand": "无"}
        )
        self.assertIs(result.sufficiency, Sufficiency.SUFFICIENT)
        self.assertEqual(result.required_total, 3)
        self.assertEqual(result.missing_count, 0)
        self.assertEqual(result.invalid_count, 0)
        self.assertEqual(result.unverifiable_count, 0)
        self.assertEqual(result.supplement_items, [])
        self.assertIs(result.query_status, QueryStatus.OK)
        self.assertTrue(result.authoritative)

    def test_missing_required_is_insufficient_and_actionable(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业设备连接", "brand": "无"}
        )
        self.assertIs(result.sufficiency, Sufficiency.INSUFFICIENT)
        self.assertEqual(result.missing_count, 1)
        self.assertEqual([item.key for item in result.supplement_items], ["model"])
        self.assertEqual(result.supplement_items[0].suggested_role, "supplier")
        self.assertEqual(result.gaps[0].code, "ELEMENT_MISSING")
        self.assertEqual(result.gaps[0].field, "elements.model")

    def test_present_but_empty_is_distinct_from_missing(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业设备连接", "model": "", "brand": "无"}
        )
        model = next(item for item in result.checks if item.key == "model")
        self.assertEqual(model.state.value, "empty")
        self.assertIs(result.sufficiency, Sufficiency.INSUFFICIENT)

    def test_invalid_enum_is_insufficient_and_explains_allowed_values(self):
        result = self.check(
            declared={"brand_type": "随便写", "usage": "工业", "model": "X", "brand": "无"}
        )
        self.assertIs(result.sufficiency, Sufficiency.INSUFFICIENT)
        self.assertEqual(result.invalid_count, 1)
        self.assertIn("允许", result.checks[0].reason)
        self.assertEqual(result.supplement_items[0].key, "brand_type")

    def test_invalid_number_and_percentage_are_checked_when_source_declares_type(self):
        specs = [
            element_spec("voltage", "电压", 1, value_type="number"),
            element_spec("fat", "脂肪含量", 2, value_type="percentage"),
        ]
        store = element_store(schemas=[element_schema(specs=specs)])
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=self.basis,
                declared={"电压": "大于60V", "脂肪含量": "120%"},
            )
        )
        self.assertIs(result.sufficiency, Sufficiency.INSUFFICIENT)
        self.assertEqual(result.invalid_count, 2)
        self.assertTrue(all(item.state.value == "invalid_number" for item in result.checks))

    def test_conditional_absent_is_unverified_not_missing(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "model": "X"}
        )
        brand = next(item for item in result.checks if item.key == "brand")
        self.assertEqual(brand.state.value, "unverifiable")
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertEqual(result.supplement_items, [])
        self.assertIn("ELEMENT_CONDITION_UNVERIFIED", [gap.code for gap in result.gaps])
        self.assertEqual(
            next(gap for gap in result.gaps if gap.code == "ELEMENT_CONDITION_UNVERIFIED").suggested_role,
            "expert",
        )

    def test_conditional_present_is_satisfied(self):
        result = self.check(
            declared={"brand_type": "境外品牌（其它）", "usage": "工业", "model": "X", "brand": "ACME"}
        )
        brand = next(item for item in result.checks if item.key == "brand")
        self.assertEqual(brand.state.value, "satisfied")
        self.assertIs(result.sufficiency, Sufficiency.SUFFICIENT)

    def test_unknown_requirement_absent_blocks_sufficiency_without_supplier_request(self):
        specs = [element_spec("usage", "用途", 1, requirement="unknown")]
        store = element_store(schemas=[element_schema(specs=specs)])
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(hs_code_raw="8544422900", basis=self.basis, declared={"型号": "X"})
        )
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertEqual(result.required_total, 0)
        self.assertEqual(result.unverifiable_count, 1)
        self.assertEqual(result.supplement_items, [])

    def test_optional_absent_does_not_block(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "model": "X", "brand": "无"}
        )
        gtin = next(item for item in result.checks if item.key == "gtin")
        self.assertEqual(gtin.state.value, "not_applicable")
        self.assertIs(result.sufficiency, Sufficiency.SUFFICIENT)

    def test_synthetic_schema_never_yields_business_conclusion(self):
        store = element_store(info=synthetic_dataset())
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(hs_code_raw="8544422900", basis=self.basis, declared_raw=numbered_declared())
        )
        self.assertIs(result.schema_status, EvidenceStatus.OBTAINED)
        self.assertFalse(result.authoritative)
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertEqual(result.supplement_items, [])
        self.assertIn("ELEMENT_AUTHORITY_NOT_OFFICIAL", [gap.code for gap in result.gaps])
        self.assertNotIn("ELEMENT_MISSING", [gap.code for gap in result.gaps])

    def test_third_party_schema_never_yields_business_conclusion(self):
        store = element_store(info=synthetic_dataset(SourceKind.THIRD_PARTY))
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(hs_code_raw="8544422900", basis=self.basis, declared_raw=numbered_declared())
        )
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertFalse(result.authoritative)

    def test_official_unreviewed_schema_is_not_a_business_conclusion(self):
        store = element_store(info=official_elements_dataset(reviewed=False))
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(hs_code_raw="8544422900", basis=self.basis, declared_raw=numbered_declared())
        )
        self.assertFalse(result.authoritative)
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)

    def test_unverified_version_does_not_claim_sufficient_or_insufficient(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "model": "X", "brand": "无"},
            basis=QueryBasis(),
        )
        self.assertIs(result.schema_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertEqual(result.supplement_items, [])
        self.assertIn("ELEMENT_VERSION_UNVERIFIED", [gap.code for gap in result.gaps])

    def test_missing_schema_is_a_coverage_gap_not_no_elements_required(self):
        result = DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(
            ElementQuery(hs_code_raw="9999999999", basis=self.basis, declared={"用途": "工业"})
        )
        self.assertIs(result.schema_status, EvidenceStatus.NOT_FOUND)
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertEqual(result.evidence, [])
        self.assertIn("不等于该编码无需申报要素", result.reason)
        self.assertEqual(result.gaps[0].code, "ELEMENT_SCHEMA_NOT_FOUND")

    def test_eight_digit_code_cannot_confirm_declaration_element_sufficiency(self):
        store = element_store(schemas=[element_schema(code="85444229")])
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(hs_code_raw="85444229", basis=self.basis, declared_raw="无品牌|工业|X|无|")
        )
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertIn("8 位", result.reason)
        self.assertEqual(result.supplement_items, [])

    def test_invalid_hs_format_is_not_checked_and_never_queries(self):
        for raw in ("", "ABC", "123"):
            with self.subTest(raw=raw):
                result = DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(
                    ElementQuery(hs_code_raw=raw, basis=self.basis, declared={"用途": "工业"})
                )
                self.assertIs(result.query_status, QueryStatus.INVALID_INPUT)
                self.assertIs(result.sufficiency, Sufficiency.NOT_CHECKED)
                self.assertIsNone(result.schema_status)
                self.assertEqual(result.evidence, [])

    def test_store_errors_are_unverified_not_insufficient(self):
        class Broken:
            def load(self):
                raise ElementStoreError("测试：文件损坏")

        class Missing:
            def load(self):
                raise ElementStoreNotConfigured("测试：未配置")

        for store, status in ((Broken(), QueryStatus.SOURCE_UNREADABLE), (Missing(), QueryStatus.SOURCE_UNCONFIGURED)):
            with self.subTest(status=status):
                result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
                    ElementQuery(hs_code_raw="8544422900", basis=self.basis, declared={"用途": "工业"})
                )
                self.assertIs(result.query_status, status)
                self.assertIs(result.schema_status, EvidenceStatus.QUERY_FAILED)
                self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
                self.assertEqual(result.supplement_items, [])

    def test_evidence_is_locatable_deterministic_and_not_a_conclusion(self):
        first = self.check(declared_raw=numbered_declared())
        second = self.check(declared_raw=numbered_declared())
        evidence = first.evidence[0]
        self.assertEqual(evidence.evidence_id, "elements:2026:8544422900")
        self.assertEqual(evidence.document, "P14-UNIT-FIXTURE")
        self.assertTrue(evidence.locator)
        self.assertEqual(evidence.status, EvidenceStatus.OBTAINED)
        self.assertIn("应申报要素", evidence.excerpt)
        self.assertEqual(evidence.model_dump(), second.evidence[0].model_dump())
        self.assertNotIn("suggested_hs", first.model_dump())

    def test_result_validator_rejects_supplement_when_unverified(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["sufficiency"] = "unverified"
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_rejects_sufficient_with_missing(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "model": "X", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["checks"][0]["state"] = "missing"
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_rejects_fabricated_supplement_item(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["supplement_items"][0]["key"] = "not-in-schema"
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_optional_invalid_value_is_actionable_correction(self):
        specs = [
            element_spec("usage", "用途", 1),
            element_spec(
                "optional_code",
                "选填代码",
                2,
                requirement="optional",
                value_type="enum",
                enum_values=["A", "B"],
            ),
        ]
        store = element_store(schemas=[element_schema(specs=specs)])
        result = DeclarationElementsSource(store, clock=FIXED_CLOCK).check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=self.basis,
                declared={"用途": "工业", "选填代码": "C"},
            )
        )
        self.assertIs(result.sufficiency, Sufficiency.INSUFFICIENT)
        self.assertEqual(result.invalid_count, 1)
        self.assertEqual([item.key for item in result.supplement_items], ["optional_code"])
        self.assertIn("ELEMENT_VALUE_INVALID", [gap.code for gap in result.gaps])

    def test_result_validator_rejects_synthetic_sufficient(self):
        result = DeclarationElementsSource(
            element_store(info=synthetic_dataset()), clock=FIXED_CLOCK
        ).check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=self.basis,
                declared_raw=numbered_declared(),
            )
        )
        payload = result.model_dump(mode="json")
        payload["sufficiency"] = "sufficient"
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_rejects_spoofed_synthetic_authority(self):
        result = DeclarationElementsSource(
            element_store(info=synthetic_dataset()), clock=FIXED_CLOCK
        ).check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=self.basis,
                declared_raw=numbered_declared(),
            )
        )
        payload = result.model_dump(mode="json")
        payload["authoritative"] = True
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_rejects_partial_parse_with_business_conclusion(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["parse"]["status"] = "partial"
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_rejects_conflict_on_sufficient(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "model": "X", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["parse"]["conflicts"] = ["型号"]
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_rejects_spoofed_supplement_item_fields(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["supplement_items"][0]["suggested_role"] = "expert"
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_result_validator_requires_dataset_when_schema_present(self):
        result = self.check(
            declared={"brand_type": "无品牌", "usage": "工业", "model": "X", "brand": "无"}
        )
        payload = result.model_dump(mode="json")
        payload["dataset"] = None
        with self.assertRaises(ValidationError):
            DeclarationElementsResult.model_validate(payload)

    def test_same_input_is_repeatable(self):
        first = self.check(declared_raw=numbered_declared()).model_dump(mode="json")
        second = self.check(declared_raw=numbered_declared()).model_dump(mode="json")
        self.assertEqual(first, second)
