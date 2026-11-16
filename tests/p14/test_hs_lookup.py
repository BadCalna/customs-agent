"""HS 来源查询的四态、有效性与证据边界。"""
from unittest.mock import patch

from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.sources.hs_lookup import (
    CodeState,
    EnvTariffStore,
    EvidenceStatus,
    HsLookupResult,
    HsLookupSource,
    HsQuery,
    QueryBasis,
    QueryStatus,
    SourceKind,
    TariffStoreError,
    TariffStoreNotConfigured,
)
from tests.p14.helpers import (
    FIXED_CLOCK,
    VERSIONS,
    official_tariff_dataset,
    synthetic_dataset,
    tariff_code,
    tariff_store,
)


class BrokenStore:
    def load(self):
        raise TariffStoreError("测试：文件损坏")


class UnexpectedStore:
    def load(self):
        raise RuntimeError("测试：存储实现缺陷")


class CountingStore:
    def __init__(self):
        self.calls = 0

    def load(self):
        self.calls += 1
        raise AssertionError("非法输入不应读取来源")


class HsLookupTest(SimpleTestCase):
    def setUp(self):
        self.basis = QueryBasis(declaration_date="2026-05-20")

    def lookup(self, raw="8544422900", *, store=None, basis=None):
        return HsLookupSource(store or tariff_store(), clock=FIXED_CLOCK).lookup(
            HsQuery(hs_code_raw=raw, basis=basis or self.basis)
        )

    def test_found_active_reviewed_official_code_is_usable(self):
        result = self.lookup()
        self.assertIs(result.query_status, QueryStatus.OK)
        self.assertIs(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertIs(result.code_state, CodeState.ACTIVE)
        self.assertTrue(result.authoritative)
        self.assertTrue(result.declaration_ready)
        self.assertTrue(result.usable_as_basis)
        self.assertEqual(result.code.code, "8544422900")
        self.assertEqual(result.searched_at, "2026-11-16T06:00:00+00:00")

    def test_evidence_is_locatable_and_deterministic(self):
        first = self.lookup()
        second = self.lookup()
        self.assertEqual(len(first.to_evidence_entries()), 1)
        evidence = first.evidence[0]
        self.assertEqual(evidence.evidence_id, "hs:2026:8544422900")
        self.assertEqual(evidence.document, "P14-UNIT-FIXTURE")
        self.assertTrue(evidence.locator)
        self.assertEqual(evidence.retrieved_at, "2026-11-16T00:00:00+08:00")
        self.assertEqual(evidence.model_dump(), second.evidence[0].model_dump())

    def test_query_does_not_contain_declared_facts_or_supplier_code_history(self):
        result = self.lookup()
        self.assertEqual(result.evidence[0].query["hs_code"], "8544422900")
        self.assertEqual(set(result.evidence[0].query), {"hs_code", "tariff_version", "basis"})
        self.assertNotIn("suggested_hs", result.model_dump())

    def test_not_found_is_not_invalid_and_has_no_fake_evidence(self):
        result = self.lookup("9999999999")
        self.assertIs(result.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertIs(result.code_state, CodeState.NOT_FOUND)
        self.assertFalse(result.declaration_ready)
        self.assertEqual(result.evidence, [])
        self.assertIn("不等于编码无效", result.reason)
        self.assertEqual(result.issues[0].code, "HS_CODE_NOT_FOUND")

    def test_code_in_other_version_gets_explicit_cross_version_note(self):
        store = tariff_store(codes=[tariff_code(version="2025")])
        result = self.lookup(store=store)
        self.assertIs(result.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertIn("HS_CODE_OTHER_VERSION", [item.code for item in result.issues])

    def test_unverified_version_cannot_claim_not_found(self):
        result = self.lookup("9999999999", basis=QueryBasis())
        self.assertIs(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertIs(result.code_state, CodeState.UNKNOWN)
        self.assertIn("不能宣称该码不存在", result.reason)

    def test_unverified_version_can_return_record_but_not_ready(self):
        store = tariff_store(versions=[VERSIONS[-1]])
        result = self.lookup(store=store, basis=QueryBasis())
        self.assertIs(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertEqual(result.code.code, "8544422900")
        self.assertFalse(result.declaration_ready)
        self.assertIn("HS_VERSION_UNVERIFIED", [item.code for item in result.issues])

    def test_no_basis_does_not_guess_between_multiple_versions(self):
        codes = [tariff_code(version="2025"), tariff_code(version="2026")]
        result = self.lookup(store=tariff_store(codes=codes), basis=QueryBasis())
        # 多版本且没有基准时不猜版本，也不声称该码不存在。
        self.assertIsNone(result.code)
        self.assertIs(result.code_state, CodeState.UNKNOWN)
        self.assertIs(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)

    def test_explicit_unavailable_version_does_not_fall_back(self):
        result = self.lookup(basis=QueryBasis(tariff_version="2027"))
        self.assertIs(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertIsNone(result.code)
        self.assertIs(result.code_state, CodeState.UNKNOWN)

    def test_expired_code_is_separate_from_format_and_not_found(self):
        store = tariff_store(codes=[tariff_code(state="expired", effective_to="2025-12-31")])
        result = self.lookup(store=store)
        self.assertIs(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertIs(result.code_state, CodeState.EXPIRED)
        self.assertFalse(result.declaration_ready)
        self.assertIn("HS_CODE_EXPIRED", [item.code for item in result.issues])

    def test_merged_code_keeps_replacement_as_source_fact(self):
        store = tariff_store(codes=[tariff_code(state="merged", merged_to="8544422900")])
        result = self.lookup(store=store)
        self.assertIs(result.code_state, CodeState.MERGED)
        self.assertEqual(result.code.merged_to, "8544422900")
        self.assertIn("HS_CODE_MERGED", [item.code for item in result.issues])

    def test_unknown_validity_is_not_treated_as_valid(self):
        store = tariff_store(codes=[tariff_code(state="unknown")])
        result = self.lookup(store=store)
        self.assertIs(result.code_state, CodeState.UNKNOWN)
        self.assertFalse(result.declaration_ready)
        self.assertIn("HS_CODE_STATE_UNKNOWN", [item.code for item in result.issues])

    def test_eight_digit_subheading_can_be_looked_up_but_not_declaration_ready(self):
        store = tariff_store(codes=[tariff_code(code="85444229")])
        result = self.lookup("85444229", store=store)
        self.assertIs(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertIs(result.code_state, CodeState.ACTIVE)
        self.assertFalse(result.declaration_ready)
        self.assertIn("HS_SUBHEADING_ONLY", [item.code for item in result.issues])

    def test_synthetic_data_is_clearly_not_a_business_basis(self):
        result = self.lookup(store=tariff_store(info=synthetic_dataset()))
        self.assertIs(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertTrue(result.dataset.synthetic)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.declaration_ready)
        self.assertIn("HS_AUTHORITY_NOT_OFFICIAL", [item.code for item in result.issues])
        self.assertIn("仅作线索", result.evidence[0].note)

    def test_third_party_data_is_never_a_business_basis(self):
        result = self.lookup(store=tariff_store(info=synthetic_dataset(SourceKind.THIRD_PARTY)))
        self.assertFalse(result.declaration_ready)
        self.assertIn("third_party", result.evidence[0].note)

    def test_official_but_unreviewed_data_is_not_ready(self):
        result = self.lookup(store=tariff_store(info=official_tariff_dataset(reviewed=False)))
        self.assertIs(result.evidence_status, EvidenceStatus.OBTAINED)
        self.assertFalse(result.authoritative)
        self.assertFalse(result.declaration_ready)

    def test_invalid_format_does_not_touch_store_or_claim_invalid_code(self):
        store = CountingStore()
        for raw in ("", "ABC", "123"):
            with self.subTest(raw=raw):
                result = self.lookup(raw, store=store)
                self.assertIs(result.query_status, QueryStatus.INVALID_INPUT)
                self.assertIsNone(result.evidence_status)
                self.assertIsNone(result.code)
                self.assertIn("不能据此判定编码无效", result.reason)
        self.assertEqual(store.calls, 0)

    def test_bad_query_shape_is_invalid_input(self):
        result = HsLookupSource(tariff_store(), clock=FIXED_CLOCK).lookup({"basis": "2026"})
        self.assertIs(result.query_status, QueryStatus.INVALID_INPUT)
        self.assertIsNone(result.evidence_status)

    def test_unconfigured_source_is_query_failed_not_not_found(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(TariffStoreNotConfigured):
                EnvTariffStore().load()
        # 已配置路径但不可读与未配置都通过结构化 query_failed 表达，不伪装成查无。
        result = self.lookup(store=BrokenStore())
        self.assertIs(result.query_status, QueryStatus.SOURCE_UNREADABLE)
        self.assertIs(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIsNone(result.code)
        self.assertIn("不等于查无此码", result.reason)

    def test_unexpected_store_error_is_contained(self):
        result = self.lookup(store=UnexpectedStore())
        self.assertIs(result.query_status, QueryStatus.SOURCE_ERROR)
        self.assertIs(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIn("RuntimeError", result.reason)

    def test_result_validator_rejects_status_mixing(self):
        result = self.lookup("9999999999")
        payload = result.model_dump(mode="json")
        payload["evidence_status"] = "query_failed"
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_rejects_declaration_ready_without_authority(self):
        result = self.lookup()
        payload = result.model_dump(mode="json")
        payload["authoritative"] = False
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_rejects_spoofed_synthetic_authority(self):
        result = self.lookup(store=tariff_store(info=synthetic_dataset()))
        payload = result.model_dump(mode="json")
        payload["authoritative"] = True
        payload["declaration_ready"] = True
        payload["usable_as_basis"] = True
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_rejects_basis_ready_mismatch(self):
        result = self.lookup()
        payload = result.model_dump(mode="json")
        payload["usable_as_basis"] = False
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_rejects_ready_without_code(self):
        result = self.lookup("9999999999")
        payload = result.model_dump(mode="json")
        payload["declaration_ready"] = True
        payload["usable_as_basis"] = True
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_rejects_ready_on_invalid_input(self):
        result = self.lookup("ABC")
        payload = result.model_dump(mode="json")
        payload["declaration_ready"] = True
        payload["usable_as_basis"] = True
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_requires_evidence_when_obtained(self):
        result = self.lookup()
        payload = result.model_dump(mode="json")
        payload["evidence"] = []
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_result_validator_rejects_version_unverified_claiming_not_found(self):
        result = self.lookup("9999999999", basis=QueryBasis())
        payload = result.model_dump(mode="json")
        payload["code_state"] = "not_found"
        with self.assertRaises(ValidationError):
            HsLookupResult.model_validate(payload)

    def test_same_input_is_repeatable(self):
        first = self.lookup().model_dump(mode="json")
        second = self.lookup().model_dump(mode="json")
        self.assertEqual(first, second)
