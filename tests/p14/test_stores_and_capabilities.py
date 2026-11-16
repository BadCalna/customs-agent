"""JSON 存储契约、环境变量与来源能力清单。"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from adapters.sources.declaration_elements import (
    DeclarationElementsSource,
    ElementQuery,
    ElementStoreError,
    ElementStoreNotConfigured,
    EnvElementStore,
    JsonElementStore,
    Sufficiency,
)
from adapters.sources.hs_lookup import (
    ENV_TARIFF_PATH,
    EnvTariffStore,
    HsLookupSource,
    HsQuery,
    JsonTariffStore,
    QueryBasis,
    SourceAvailability,
    SourceKind,
    TariffStoreError,
    TariffStoreNotConfigured,
    describe_sources,
)
from tests.p14.helpers import FIXED_CLOCK

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TARIFF = FIXTURES / "tariff_synthetic.json"
ELEMENTS = FIXTURES / "elements_synthetic.json"


class JsonStoreTest(SimpleTestCase):
    def copy_payload(self, source: Path, mutate):
        payload = json.loads(source.read_text(encoding="utf-8"))
        mutate(payload)
        temp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, temp, ensure_ascii=False)
        temp.close()
        self.addCleanup(os.unlink, temp.name)
        return Path(temp.name)

    def write_text(self, text: str) -> Path:
        temp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        temp.write(text)
        temp.close()
        self.addCleanup(os.unlink, temp.name)
        return Path(temp.name)

    def test_tariff_fixture_loads_and_is_explicitly_synthetic(self):
        snapshot = JsonElementStore(ELEMENTS).load()
        self.assertTrue(snapshot.dataset.synthetic)
        self.assertFalse(snapshot.dataset.authoritative)
        self.assertEqual(len(snapshot.versions), 2)
        self.assertEqual({item.version for item in snapshot.schemas}, {"2025", "2026"})

    def test_fixture_lookup_end_to_end_marks_data_synthetic(self):
        source = HsLookupSource.load(TARIFF, clock=FIXED_CLOCK)
        result = source.lookup(
            HsQuery(hs_code_raw="8544422900", basis=QueryBasis(declaration_date="2026-05-20"))
        )
        self.assertEqual(result.code.code, "8544422900")
        self.assertTrue(result.version.verified)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.declaration_ready)
        self.assertEqual(result.evidence[0].source_type, "synthetic")

    def test_fixture_element_check_can_find_missing_but_cannot_conclude(self):
        source = DeclarationElementsSource.load(ELEMENTS, clock=FIXED_CLOCK)
        result = source.check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=QueryBasis(declaration_date="2026-05-20"),
                declared={"brand_type": "无品牌", "usage": "工业", "conductor_material": "铜"},
            )
        )
        self.assertIs(result.sufficiency, Sufficiency.UNVERIFIED)
        self.assertEqual(result.supplement_items, [])
        model = next(item for item in result.checks if item.key == "model")
        self.assertEqual(model.state.value, "missing")

    def test_version_specific_element_lists_are_not_mixed(self):
        source = DeclarationElementsSource.load(ELEMENTS, clock=FIXED_CLOCK)
        old = source.check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=QueryBasis(tariff_version="2025"),
                declared={"brand_type": "无品牌", "usage": "工业", "model": "X"},
            )
        )
        new = source.check(
            ElementQuery(
                hs_code_raw="8544422900",
                basis=QueryBasis(tariff_version="2026"),
                declared={"brand_type": "无品牌", "usage": "工业", "conductor_material": "铜"},
            )
        )
        self.assertEqual(old.required_total, 3)
        self.assertEqual(new.required_total, 5)
        self.assertNotEqual(
            [item.key for item in old.checks], [item.key for item in new.checks]
        )

    def test_bad_json_is_a_query_failure_not_no_result(self):
        path = self.write_text("{not json")
        with self.assertRaises(ElementStoreError):
            JsonElementStore(path).load()

    def test_non_utf8_file_is_a_store_error(self):
        temp = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
        temp.write(b"\xff\xfe\x00")
        temp.close()
        self.addCleanup(os.unlink, temp.name)
        with self.assertRaises(TariffStoreError):
            JsonTariffStore(temp.name).load()

    def test_unsupported_schema_version_is_rejected(self):
        path = self.copy_payload(TARIFF, lambda payload: payload.update(schema_version="p14.tariff.999"))
        with self.assertRaises(TariffStoreError):
            JsonTariffStore(path).load()

    def test_missing_versions_and_duplicate_codes_are_rejected(self):
        cases = [
            lambda payload: payload.pop("schema_version"),
            lambda payload: payload.pop("versions"),
            lambda payload: payload["codes"].append(dict(payload["codes"][0])),
            lambda payload: payload["codes"][0].update(version="2030"),
            lambda payload: payload["codes"][0].update(code="123"),
            lambda payload: payload["codes"][0].update(effective_to="not-a-date"),
            lambda payload: payload["codes"][2].pop("merged_to"),
            lambda payload: payload["codes"][0]["source"].pop("locator"),
            lambda payload: payload["codes"][0]["source"].update(
                document="\u034f", locator="\u034f"
            ),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                path = self.copy_payload(TARIFF, mutate)
                with self.assertRaises(TariffStoreError):
                    JsonTariffStore(path).load()

    def test_element_store_rejects_duplicate_and_unversioned_schemas(self):
        cases = [
            lambda payload: payload["schemas"].append(dict(payload["schemas"][0])),
            lambda payload: payload["schemas"][0].update(version="2030"),
            lambda payload: payload["schemas"][0]["elements"].append(
                dict(payload["schemas"][0]["elements"][0])
            ),
            lambda payload: payload["schemas"][0]["elements"][0].update(
                value_type="enum", enum_values=[]
            ),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                path = self.copy_payload(ELEMENTS, mutate)
                with self.assertRaises(ElementStoreError):
                    JsonElementStore(path).load()

    def test_env_store_is_explicitly_unconfigured_without_variable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TariffStoreNotConfigured):
                EnvTariffStore().load()
            with self.assertRaises(ElementStoreNotConfigured):
                EnvElementStore().load()

    def test_unconfigured_source_returns_structured_query_failed(self):
        with patch.dict(os.environ, {}, clear=True):
            hs = HsLookupSource(clock=FIXED_CLOCK).lookup(
                HsQuery(hs_code_raw="8544422900", basis=QueryBasis(tariff_version="2026"))
            )
            elements = DeclarationElementsSource(clock=FIXED_CLOCK).check(
                ElementQuery(
                    hs_code_raw="8544422900",
                    basis=QueryBasis(tariff_version="2026"),
                    declared={"用途": "工业"},
                )
            )
        self.assertEqual(hs.query_status.value, "source_unconfigured")
        self.assertEqual(hs.evidence_status.value, "query_failed")
        self.assertEqual(elements.query_status.value, "source_unconfigured")
        self.assertEqual(elements.sufficiency.value, "unverified")
        self.assertEqual(elements.supplement_items, [])

    def test_in_memory_stores_reject_duplicates_like_json(self):
        from adapters.sources.declaration_elements import ElementStoreError
        from adapters.sources.hs_lookup import TariffStoreError

        with self.assertRaises(TariffStoreError):
            from tests.p14.helpers import tariff_code, tariff_store

            tariff_store(codes=[tariff_code(), tariff_code()]).load()
        with self.assertRaises(ElementStoreError):
            from tests.p14.helpers import element_schema, element_store

            element_store(schemas=[element_schema(), element_schema()]).load()

    def test_env_store_uses_variable_when_present(self):
        with patch.dict(os.environ, {ENV_TARIFF_PATH: str(TARIFF)}, clear=True):
            snapshot = EnvTariffStore().load()
            self.assertTrue(snapshot.codes)
        with patch.dict(
            os.environ, {"CUSTOMS_DECLARATION_ELEMENTS_PATH": str(ELEMENTS)}, clear=True
        ):
            snapshot = EnvElementStore().load()
            self.assertTrue(snapshot.schemas)


class SourceCapabilityTest(SimpleTestCase):
    def test_capabilities_are_documented_and_not_oversold(self):
        items = describe_sources()
        self.assertGreaterEqual(len(items), 4)
        for item in items:
            with self.subTest(source=item.source_id):
                self.assertTrue(item.fields)
                self.assertTrue(item.limitations)
                self.assertTrue(item.checked_at)
                self.assertTrue(item.check_note)
                self.assertTrue(item.source_id)

    def test_no_official_source_claims_automated_availability(self):
        for item in describe_sources():
            if item.source_type is SourceKind.OFFICIAL_TARIFF:
                self.assertIsNot(
                    item.availability,
                    SourceAvailability.AVAILABLE,
                    f"{item.source_id} 不应声称官方自动取数可用",
                )

    def test_declaration_catalog_requires_manual_structured_import(self):
        item = next(item for item in describe_sources(content="declaration_elements") if item.source_type is SourceKind.OFFICIAL_DECLARATION)
        self.assertIs(item.availability, SourceAvailability.REQUIRES_AUTHORIZATION)
        self.assertTrue(any("批量下载" in text for text in item.limitations))

    def test_third_party_is_available_but_explicitly_not_authoritative(self):
        item = next(item for item in describe_sources() if item.source_type is SourceKind.THIRD_PARTY)
        self.assertIs(item.availability, SourceAvailability.AVAILABLE)
        self.assertTrue(any("不得作为合规结论" in text for text in item.limitations))

    def test_filtering_by_content(self):
        self.assertTrue(all(item.content in ("tariff", "both") for item in describe_sources(content="tariff")))
        self.assertTrue(
            all(
                item.content in ("declaration_elements", "both")
                for item in describe_sources(content="declaration_elements")
            )
        )
