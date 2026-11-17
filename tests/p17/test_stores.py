"""P17 JSON 存储契约、环境变量与来源能力清单。

数据文件不合契约时必须明确报错，不能静默降级成“查无”或“无监管”。
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from adapters.sources.hs_lookup import QueryBasis
from adapters.sources.regulatory_lookup import (
    ENV_REGULATORY_PATH,
    Direction,
    EnvRegulatoryStore,
    JsonRegulatoryStore,
    RegulatoryQuery,
    RegulatorySource,
    RegulatorySourceKind,
    configured_regulatory_path,
)
from adapters.sources.regulatory_lookup import (
    describe_sources as describe_regulatory_sources,
)
from adapters.sources.tax_lookup import (
    ENV_TAX_RATE_PATH,
    EnvTaxRateStore,
    JsonTaxRateStore,
    RebateSourceKind,
    SourceStoreError,
    SourceStoreNotConfigured,
    TaxRateQuery,
    TaxRateSource,
    configured_tax_rate_path,
    describe_sources as describe_rebate_sources,
)
from tests.p17.helpers import FIXED_CLOCK

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TAX_FIXTURE = FIXTURES / "tax_rates_synthetic.json"
REGULATORY_FIXTURE = FIXTURES / "regulatory_synthetic.json"
BASIS = QueryBasis(declaration_date="2026-05-20")


class FixtureDocumentTest(SimpleTestCase):
    def test_fixtures_declare_synthetic_dataset_and_schema(self):
        for path, version, key in (
            (TAX_FIXTURE, "p17.taxrate.1", "rates"),
            (REGULATORY_FIXTURE, "p17.regulatory.1", "records"),
        ):
            with self.subTest(fixture=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["schema_version"], version)
                self.assertEqual(payload["dataset"]["source_type"], "synthetic")
                self.assertTrue(payload["dataset"]["synthetic"])
                self.assertEqual(payload["dataset"]["authority"], "synthetic")
                self.assertEqual(payload["dataset"]["review_status"], "unreviewed")
                self.assertGreaterEqual(len(payload[key]), 6)

    def test_fixture_covers_the_rate_kind_matrix(self):
        payload = json.loads(TAX_FIXTURE.read_text(encoding="utf-8"))
        kinds = {item["rate_kind"] for item in payload["rates"]}
        self.assertLessEqual(
            {"export_rebate", "zero_rate", "not_refundable"}, kinds
        )
        states = {item["rate_state"] for item in payload["rates"]}
        self.assertIn("abolished", states)
        undeclared = [item for item in payload["rates"] if "conditions" not in item]
        self.assertTrue(undeclared, "样本必须包含“来源未声明条件”的行")
        declared_none = [item for item in payload["rates"] if item.get("conditions") == []]
        self.assertTrue(declared_none, "样本必须包含“来源明确无条件”的行")

    def test_fixture_covers_the_regulatory_matrix(self):
        payload = json.loads(REGULATORY_FIXTURE.read_text(encoding="utf-8"))
        records = payload["records"]
        self.assertTrue(any(item.get("measures") == [] for item in records))
        self.assertTrue(any("measures" not in item for item in records))
        self.assertTrue(any(item.get("dual_use") for item in records))
        self.assertTrue(any(item["state"] == "abolished" for item in records))
        self.assertTrue(
            any(
                measure
                for item in records
                for measure in (item.get("measures") or [])
                if measure["code"] and not measure.get("name")
            ),
            "样本必须包含“只有代码没有名称”的措施",
        )


class MutatingFixture:
    """在临时目录里复制夹具并改写；不污染仓库内样本。"""

    def __init__(self, source: Path):
        self.source = source
        self.payload = json.loads(source.read_text(encoding="utf-8"))

    def write(self, *, raw_text: str | None = None, encoding: str = "utf-8") -> Path:
        directory = tempfile.mkdtemp(prefix="p17-store-")
        path = Path(directory) / self.source.name
        text = raw_text if raw_text is not None else json.dumps(self.payload, ensure_ascii=False)
        path.write_text(text, encoding=encoding)
        return path


class JsonStoreContractTest(SimpleTestCase):
    def load_tax(self, mutate):
        fixture = MutatingFixture(TAX_FIXTURE)
        mutate(fixture.payload)
        return JsonTaxRateStore(fixture.write()).load()

    def load_regulatory(self, mutate):
        fixture = MutatingFixture(REGULATORY_FIXTURE)
        mutate(fixture.payload)
        return JsonRegulatoryStore(fixture.write()).load()

    def test_fixture_loads_with_dataset_and_versions(self):
        snapshot = JsonTaxRateStore(TAX_FIXTURE).load()
        self.assertEqual(snapshot.dataset.source_type, RebateSourceKind.SYNTHETIC.value)
        self.assertFalse(snapshot.dataset.authoritative)
        self.assertEqual([item.version for item in snapshot.versions], ["2025", "2026"])
        self.assertEqual(len(snapshot.records), 9)

    def test_regulatory_fixture_loads(self):
        snapshot = JsonRegulatoryStore(REGULATORY_FIXTURE).load()
        self.assertEqual(
            snapshot.dataset.source_type, RegulatorySourceKind.SYNTHETIC.value
        )
        self.assertEqual(len(snapshot.records), 8)

    def test_missing_schema_version_is_refused(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(lambda payload: payload.pop("schema_version"))
        self.assertIn("缺少 schema_version", str(caught.exception))

    def test_unsupported_schema_version_is_refused(self):
        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(lambda payload: payload.update(schema_version="p17.taxrate.9"))
        self.assertIn("结构版本不受支持", str(caught.exception))

    def test_duplicate_version_is_refused(self):
        def mutate(payload):
            payload["versions"].append(dict(payload["versions"][1]))

        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(mutate)
        self.assertIn("重复的版本号", str(caught.exception))

    def test_duplicate_record_is_refused(self):
        def mutate(payload):
            payload["rates"].append(dict(payload["rates"][0]))

        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(mutate)
        self.assertIn("退税率记录重复", str(caught.exception))

    def test_undeclared_version_reference_is_refused(self):
        def mutate(payload):
            payload["rates"][0]["version"] = "2099"

        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(mutate)
        self.assertIn("引用未声明的版本", str(caught.exception))

    def test_bad_date_is_refused(self):
        def mutate(payload):
            payload["rates"][0]["effective_from"] = "2026/01/01"

        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(mutate)
        self.assertIn("不是有效日期", str(caught.exception))

    def test_duplicate_regulatory_key_is_refused(self):
        def mutate(payload):
            payload["records"].append(dict(payload["records"][0]))

        with self.assertRaises(SourceStoreError) as caught:
            self.load_regulatory(mutate)
        self.assertIn("监管记录重复", str(caught.exception))

    def test_malformed_json_reports_line(self):
        fixture = MutatingFixture(TAX_FIXTURE)
        path = fixture.write(raw_text="{not json")
        with self.assertRaises(SourceStoreError) as caught:
            JsonTaxRateStore(path).load()
        self.assertIn("不是合法 JSON", str(caught.exception))

    def test_non_utf8_file_is_refused(self):
        fixture = MutatingFixture(TAX_FIXTURE)
        directory = tempfile.mkdtemp(prefix="p17-store-")
        path = Path(directory) / "bad.json"
        path.write_bytes(b"\xff\xfe{}")
        with self.assertRaises(SourceStoreError) as caught:
            JsonTaxRateStore(path).load()
        self.assertIn("不是 UTF-8", str(caught.exception))

    def test_error_message_does_not_leak_absolute_path(self):
        missing = FIXTURES / "does-not-exist.json"
        with self.assertRaises(SourceStoreError) as caught:
            JsonTaxRateStore(missing).load()
        message = str(caught.exception)
        self.assertIn("does-not-exist.json", message)
        self.assertNotIn(str(FIXTURES.parent), message)

    def test_unknown_dataset_source_type_is_refused(self):
        def mutate(payload):
            payload["dataset"]["source_type"] = "official_regulatory"

        with self.assertRaises(SourceStoreError) as caught:
            self.load_tax(mutate)
        self.assertIn("不属于本来源类型", str(caught.exception))

    def test_missing_dataset_falls_back_to_undeclared_source(self):
        snapshot = self.load_tax(lambda payload: payload.pop("dataset"))
        self.assertFalse(snapshot.dataset.authoritative)
        self.assertEqual(snapshot.dataset.source_type, RebateSourceKind.SYNTHETIC.value)


class EnvironmentStoreTest(SimpleTestCase):
    def test_tax_env_store_unconfigured(self):
        with patch.dict("os.environ", {ENV_TAX_RATE_PATH: ""}):
            with self.assertRaises(SourceStoreNotConfigured) as caught:
                EnvTaxRateStore().load()
        self.assertIn("不提供默认样本", str(caught.exception))

    def test_tax_env_store_reads_configured_path(self):
        with patch.dict("os.environ", {ENV_TAX_RATE_PATH: str(TAX_FIXTURE)}):
            self.assertEqual(configured_tax_rate_path(), TAX_FIXTURE)
            self.assertEqual(len(EnvTaxRateStore().load().records), 9)

    def test_regulatory_env_store_unconfigured(self):
        with patch.dict("os.environ", {ENV_REGULATORY_PATH: ""}):
            with self.assertRaises(SourceStoreNotConfigured):
                EnvRegulatoryStore().load()

    def test_regulatory_env_store_reads_configured_path(self):
        with patch.dict("os.environ", {ENV_REGULATORY_PATH: str(REGULATORY_FIXTURE)}):
            self.assertEqual(configured_regulatory_path(), REGULATORY_FIXTURE)
            self.assertEqual(len(EnvRegulatoryStore().load().records), 8)


class FixtureLookupTest(SimpleTestCase):
    """端到端：合成样本只证明分支正确，不能读成真实结论。"""

    def test_tax_fixture_never_returns_usable_basis(self):
        source = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK)
        codes = [
            "8482102000",
            "8536490090",
            "7304190000",
            "2710110000",
            "8471300000",
            "8501109100",
            "3917400000",
            "72081000",
            "9999999999",
        ]
        for code in codes:
            for original in ("", "13%", "0"):
                with self.subTest(code=code, original=original):
                    result = source.lookup(
                        TaxRateQuery(
                            hs_code_raw=code, basis=BASIS, original_rate_raw=original
                        )
                    )
                    self.assertFalse(result.usable_as_basis)
                    self.assertFalse(result.authoritative)

    def test_regulatory_fixture_never_returns_usable_basis(self):
        source = RegulatorySource.load(REGULATORY_FIXTURE, clock=FIXED_CLOCK)
        cases = [
            ("8482102000", Direction.IMPORT),
            ("8482102000", Direction.EXPORT),
            ("8536490090", Direction.EXPORT),
            ("8501109100", Direction.IMPORT),
            ("3917400000", Direction.IMPORT),
            ("7304190000", Direction.IMPORT),
            ("8471300000", Direction.EXPORT),
            ("9999999999", Direction.IMPORT),
        ]
        for code, direction in cases:
            with self.subTest(code=code, direction=direction.value):
                result = source.lookup(
                    RegulatoryQuery(hs_code_raw=code, direction=direction, basis=BASIS)
                )
                self.assertFalse(result.usable_as_basis)
                self.assertFalse(result.authoritative)

    def test_fixture_results_are_reproducible(self):
        first = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="13%")
        )
        second = TaxRateSource.load(TAX_FIXTURE, clock=FIXED_CLOCK).lookup(
            TaxRateQuery(hs_code_raw="8482102000", basis=BASIS, original_rate_raw="13%")
        )
        self.assertEqual(first.model_dump(), second.model_dump())


class CapabilityTest(SimpleTestCase):
    def test_rebate_capabilities_are_well_formed(self):
        items = describe_rebate_sources()
        self.assertTrue(items)
        ids = [item.source_id for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        for item in items:
            with self.subTest(source=item.source_id):
                self.assertTrue(item.name)
                self.assertTrue(item.fields)
                self.assertTrue(item.limitations)
                self.assertIsNotNone(item.checked_at)
                if item.url:
                    self.assertTrue(item.url.startswith(("http://", "https://")))

    def test_regulatory_capabilities_are_well_formed(self):
        items = describe_regulatory_sources()
        self.assertTrue(items)
        for item in items:
            with self.subTest(source=item.source_id):
                self.assertTrue(item.name)
                self.assertTrue(item.limitations)
                self.assertIsNotNone(item.checked_at)
                if item.url:
                    self.assertTrue(item.url.startswith(("http://", "https://")))

    def test_content_filter(self):
        self.assertTrue(
            all(
                item.content == "tax_rate"
                for item in describe_rebate_sources(content="tax_rate")
            )
        )
        self.assertTrue(
            all(
                item.content == "dual_use"
                for item in describe_regulatory_sources(content="dual_use")
            )
        )

    def test_tax_capabilities_mark_real_availability_honestly(self):
        by_id = {item.source_id: item for item in describe_rebate_sources()}
        library = by_id["tax-rebate-library"]
        self.assertTrue(library.auth_required)
        self.assertEqual(library.availability.value, "requires_authorization")
        self.assertIn("未取得", library.check_note)

    def test_customs_portal_is_recorded_unreachable(self):
        by_id = {item.source_id: item for item in describe_regulatory_sources()}
        self.assertEqual(by_id["customs-regulatory-portal"].availability.value, "unreachable")
