"""P17 边界：不虚构、不推断、不落库、不改写输入。

这些用例对应任务包的验收标准，任何一条失败都说明适配器越界了：
- 关税税率/图片不能变成退税率；
- 监管代码含义只能来自来源，不能硬编码或补造；
- 查无/失败/缺基准不能变成“零税率”“不予退税”“无监管”；
- 证据逐条可定位，且两个适配器的证据 ID 不互相覆盖。
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

from adapters.sources.hs_lookup import QueryBasis
from adapters.sources.regulatory_lookup import (
    Direction,
    RegulatoryQuery,
    RegulatorySource,
)
from adapters.sources.tax_lookup import (
    TaxRateQuery,
    TaxRateSource,
)
from tests.p17.helpers import FIXED_CLOCK, REGULATORY_FIXTURE, TAX_FIXTURE

BASIS = QueryBasis(declaration_date="2026-05-20")
REPO_ROOT = Path(__file__).resolve().parents[2]
MODULES = {
    name: REPO_ROOT / "adapters" / "sources" / f"{name}.py"
    for name in ("tax_lookup", "regulatory_lookup")
}


class NoInventedFactsTest(SimpleTestCase):
    """不许硬编码业务含义：代码名称、退税率都只能来自来源。"""

    def test_regulatory_module_does_not_hardcode_document_names(self):
        source = MODULES["regulatory_lookup"].read_text(encoding="utf-8")
        for name in ("入境货物通关单", "出境货物通关单", "两用物项出口管制清单"):
            with self.subTest(name=name):
                self.assertNotIn(name, source)

    def test_regulatory_module_has_no_code_to_meaning_table(self):
        source = MODULES["regulatory_lookup"].read_text(encoding="utf-8")
        # 形如 {"A": "..."} / "A": "..." 的代码→含义映射都不允许出现。
        self.assertIsNone(re.search(r'["\']A["\']\s*:\s*["\']', source))
        self.assertIsNone(re.search(r'["\']B["\']\s*:\s*["\']', source))

    def test_tax_module_does_not_hardcode_any_rate(self):
        source = MODULES["tax_lookup"].read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"rate_percent\s*=\s*\d", source))
        self.assertIsNone(re.search(r"rate_percent\":\s*\d", source))

    def test_measure_without_name_stays_empty(self):
        result = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK).lookup(
            RegulatoryQuery(
                hs_code_raw="8471300000", direction=Direction.EXPORT, basis=BASIS
            )
        )
        self.assertEqual(result.measures[0].code, "B")
        self.assertEqual(result.measures[0].name, "")
        self.assertEqual(result.measures[0].agency, "")
        self.assertFalse(result.usable_as_basis)


class NoZeroOrNoneByDefaultTest(SimpleTestCase):
    """缺数据不能变成 0% / 不予退税 / 无监管。"""

    def test_unknown_codes_never_yield_zero_rate(self):
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        for code in (
            "1111111111",
            "2222222222",
            "8482102099",
            "0000000000",
            "9999999998",
        ):
            with self.subTest(code=code):
                result = source.lookup(
                    TaxRateQuery(hs_code_raw=code, basis=BASIS, original_rate_raw="0")
                )
                self.assertIsNone(result.looked_up_rate)
                self.assertIsNone(result.rate_comparison.looked_up_percent)
                self.assertNotEqual(result.rate_comparison.status.value, "matched")
                self.assertEqual(result.applicability.value, "unknown")
                self.assertEqual(result.evidence, [])

    def test_unknown_codes_never_yield_declared_none(self):
        source = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        for code in ("1111111111", "2222222222", "8482102099", "0000000000"):
            for direction in (Direction.IMPORT, Direction.EXPORT):
                with self.subTest(code=code, direction=direction.value):
                    result = source.lookup(
                        RegulatoryQuery(
                            hs_code_raw=code, direction=direction, basis=BASIS
                        )
                    )
                    self.assertNotEqual(result.outcome.value, "declared_none")
                    self.assertEqual(result.outcome.value, "unknown")

    def test_declared_none_only_when_source_says_empty_measures(self):
        source = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        cases = [
            ("8501109100", Direction.IMPORT),
            ("8482102000", Direction.IMPORT),
            ("7304190000", Direction.IMPORT),
            ("3917400000", Direction.IMPORT),
        ]
        for code, direction in cases:
            with self.subTest(code=code, direction=direction.value):
                result = source.lookup(
                    RegulatoryQuery(hs_code_raw=code, direction=direction, basis=BASIS)
                )
                if result.outcome.value == "declared_none":
                    self.assertEqual(result.record.measures, [])
                    self.assertEqual(result.direction_matched, True)

    def test_zero_rate_only_comes_from_source(self):
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        zero = source.lookup(
            TaxRateQuery(hs_code_raw="7304190000", basis=BASIS, original_rate_raw="0")
        )
        self.assertEqual(zero.looked_up_rate.rate_kind.value, "zero_rate")
        self.assertEqual(zero.looked_up_rate.rate_percent, 0)
        non_zero = source.lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="0")
        )
        self.assertEqual(non_zero.looked_up_rate.rate_percent, 13)
        self.assertEqual(non_zero.rate_comparison.status.value, "conflict")


class NoTariffOrImageInferenceTest(SimpleTestCase):
    def test_tax_rejects_tariff_rate_and_image_inputs(self):
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        for payload in (
            {"hs_code_raw": "8482102000", "tariff_rate": "8"},
            {"hs_code_raw": "8482102000", "duty_rate": "8"},
            {"hs_code_raw": "8482102000", "image": "a.png"},
            {"hs_code_raw": "8482102000", "ocr": "退税率13%"},
        ):
            with self.subTest(payload=payload):
                result = source.lookup(payload)
                self.assertEqual(result.query_status.value, "invalid_input")
                self.assertIsNone(result.looked_up_rate)
                self.assertIsNone(result.evidence_status)

    def test_regulatory_rejects_asserted_conclusions(self):
        source = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        for payload in (
            {
                "hs_code_raw": "8482102000",
                "direction": "import",
                "has_controls": False,
            },
            {
                "hs_code_raw": "8482102000",
                "direction": "import",
                "requires_licence": False,
            },
            {"hs_code_raw": "8482102000", "direction": "import", "ocr": "无监管条件"},
        ):
            with self.subTest(payload=payload):
                result = source.lookup(payload)
                self.assertEqual(result.query_status.value, "invalid_input")
                self.assertEqual(result.outcome.value, "unknown")


class EvidenceIntegrityTest(SimpleTestCase):
    def collect_evidence(self):
        tax = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        reg = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        entries = []
        for code, original in (
            ("8482102000", "13%"),
            ("8536490090", "13%"),
            ("7304190000", "0"),
            ("2710110000", "0"),
            ("8501109100", "13%"),
        ):
            entries.extend(
                tax.lookup(
                    TaxRateQuery(hs_code_raw=code, basis=BASIS, original_rate_raw=original)
                ).evidence
            )
        for code, direction in (
            ("8482102000", Direction.IMPORT),
            ("8536490090", Direction.EXPORT),
            ("8536490090", Direction.IMPORT),
            ("8501109100", Direction.IMPORT),
            ("3917400000", Direction.IMPORT),
        ):
            entries.extend(
                reg.lookup(
                    RegulatoryQuery(hs_code_raw=code, direction=direction, basis=BASIS)
                ).evidence
            )
        return entries

    def test_every_evidence_is_locatable(self):
        entries = self.collect_evidence()
        self.assertGreater(len(entries), 8)
        for entry in entries:
            with self.subTest(evidence=entry.evidence_id):
                self.assertTrue(entry.document and entry.document.strip())
                self.assertTrue(entry.locator and entry.locator.strip())
                self.assertTrue(entry.title)
                self.assertIsNotNone(entry.query)
                self.assertIn(entry.status.value, {"obtained", "version_unverified"})

    def test_evidence_ids_are_unique_and_namespaced(self):
        entries = self.collect_evidence()
        ids = [entry.evidence_id for entry in entries]
        self.assertEqual(len(ids), len(set(ids)))
        for evidence_id in ids:
            self.assertTrue(evidence_id.startswith(("tax:", "reg:")))

    def test_tax_and_regulatory_evidence_do_not_collide(self):
        tax_ids = {
            entry.evidence_id
            for entry in TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
            .lookup(TaxRateQuery(hs_code_raw="8482102000", basis=BASIS))
            .evidence
        }
        reg_ids = {
            entry.evidence_id
            for entry in RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
            .lookup(
                RegulatoryQuery(
                    hs_code_raw="8482102000", direction=Direction.IMPORT, basis=BASIS
                )
            )
            .evidence
        }
        self.assertTrue(tax_ids)
        self.assertTrue(reg_ids)
        self.assertFalse(tax_ids & reg_ids)


class InputPreservationTest(SimpleTestCase):
    def test_original_rate_raw_is_preserved_verbatim(self):
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        messy = " 13％ "
        result = source.lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw=messy)
        )
        self.assertEqual(result.original_rate.raw, messy)
        self.assertEqual(result.original_rate.percent, 13.0)

    def test_query_object_is_not_mutated(self):
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        query = TaxRateQuery(
            hs_code_raw=" 8482102000 ",
            basis=BASIS,
            original_rate_raw="13%",
        )
        before = query.model_dump()
        result = source.lookup(query)
        self.assertEqual(query.model_dump(), before)
        self.assertEqual(result.format.raw, " 8482102000 ")
        self.assertEqual(result.format.normalized, "8482102000")

    def test_regulatory_query_is_not_mutated(self):
        source = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        query = RegulatoryQuery(
            hs_code_raw="8482102000", direction=Direction.IMPORT, basis=BASIS
        )
        before = query.model_dump()
        source.lookup(query)
        self.assertEqual(query.model_dump(), before)


class PurityTest(SimpleTestCase):
    def test_adapters_do_not_touch_database_network_or_files(self):
        forbidden = (
            "from django",
            "import django",
            ".objects.",
            "write_text(",
            "open(",
            "requests.",
            "httpx.",
            "urllib.request",
            "socket.",
            "openai.",
            "anthropic.",
            "pytesseract",
            "easyocr",
            "PIL",
        )
        for name, path in MODULES.items():
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(token, source)

    def test_adapters_only_import_approved_application_module(self):
        for name, path in MODULES.items():
            source = path.read_text(encoding="utf-8")
            apps_imports = re.findall(r"^from apps[\w.]*", source, re.MULTILINE)
            with self.subTest(module=name):
                self.assertLessEqual(set(apps_imports), {"from apps.api.schemas"})
