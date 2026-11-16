"""编码格式、查询基准与来源权威性的独立判定（A05）。"""
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.sources.hs_lookup import (
    Authority,
    DatasetInfo,
    HsFormatState,
    QueryBasis,
    ReviewStatus,
    SourceKind,
    VersionStatus,
    VersionWindow,
    classify_hs_format,
    resolve_version,
)


class HsFormatTest(SimpleTestCase):
    def test_ten_digit_code_is_declaration_ready(self):
        result = classify_hs_format("8544422900")
        self.assertIs(result.state, HsFormatState.VALID)
        self.assertEqual(result.normalized, "8544422900")
        self.assertTrue(result.queryable)
        self.assertTrue(result.declaration_ready)

    def test_dots_spaces_and_full_width_digits_are_normalized_for_query_only(self):
        result = classify_hs_format("８５４４.４２.２９.００")
        self.assertIs(result.state, HsFormatState.VALID)
        self.assertEqual(result.raw, "８５４４.４２.２９.００")
        self.assertEqual(result.normalized, "8544422900")

    def test_surrounding_whitespace_is_preserved_in_raw_but_ignored_for_query(self):
        result = classify_hs_format("  8544422900  ")
        self.assertIs(result.state, HsFormatState.VALID)
        self.assertEqual(result.raw, "  8544422900  ")
        self.assertEqual(result.normalized, "8544422900")

    def test_eight_digits_is_subheading_not_invalid_code(self):
        result = classify_hs_format("85444229")
        self.assertIs(result.state, HsFormatState.SUBHEADING_ONLY)
        self.assertTrue(result.queryable)
        self.assertFalse(result.declaration_ready)

    def test_empty_is_kept_empty(self):
        result = classify_hs_format("  ")
        self.assertIs(result.state, HsFormatState.EMPTY)
        self.assertEqual(result.normalized, "")
        self.assertFalse(result.queryable)

    def test_non_digits_are_never_repaired(self):
        result = classify_hs_format("85444229AB")
        self.assertIs(result.state, HsFormatState.NONDIGIT)
        self.assertEqual(result.normalized, "")
        self.assertFalse(result.queryable)

    def test_other_lengths_are_format_errors(self):
        for raw in ("1", "854442290", "85444229000"):
            with self.subTest(raw=raw):
                result = classify_hs_format(raw)
                self.assertIs(result.state, HsFormatState.WRONG_LENGTH)
                self.assertFalse(result.queryable)


class VersionResolutionTest(SimpleTestCase):
    def setUp(self):
        self.versions = [
            VersionWindow(version="2025", effective_from="2025-01-01", effective_to="2025-12-31"),
            VersionWindow(version="2026", effective_from="2026-01-01", effective_to="2026-12-31"),
        ]

    def test_missing_basis_is_unverified_and_does_not_use_today(self):
        result = resolve_version(QueryBasis(), self.versions)
        self.assertIs(result.status, VersionStatus.UNVERIFIED)
        self.assertIsNone(result.version)
        self.assertIn("不代填拟申报日期", result.reason)

    def test_explicit_version_is_verified_without_fabricating_business_date(self):
        result = resolve_version(QueryBasis(tariff_version="2026"), self.versions)
        self.assertIs(result.status, VersionStatus.VERIFIED)
        self.assertEqual(result.version, "2026")
        self.assertIn("未提供申报日期", result.reason)

    def test_date_selects_unique_version(self):
        result = resolve_version(QueryBasis(declaration_date="2025-06-30"), self.versions)
        self.assertIs(result.status, VersionStatus.VERIFIED)
        self.assertEqual(result.version, "2025")

    def test_iso_datetime_is_accepted_but_original_basis_is_retained(self):
        basis = QueryBasis(declaration_date="2026-05-20T10:00:00+08:00", note="来自供应商列")
        result = resolve_version(basis, self.versions)
        self.assertIs(result.status, VersionStatus.VERIFIED)
        self.assertEqual(result.version, "2026")
        self.assertIn("来自供应商列", result.basis_note)

    def test_version_and_matching_date_are_verified(self):
        basis = QueryBasis(tariff_version="2026", declaration_date="2026-01-01")
        self.assertIs(resolve_version(basis, self.versions).status, VersionStatus.VERIFIED)

    def test_version_and_conflicting_date_are_unverified(self):
        for declaration_date in ("2025-12-31", "2027-01-01"):
            with self.subTest(declaration_date=declaration_date):
                basis = QueryBasis(tariff_version="2026", declaration_date=declaration_date)
                result = resolve_version(basis, self.versions)
                self.assertIs(result.status, VersionStatus.UNVERIFIED)
                self.assertEqual(result.version, "2026")

    def test_unavailable_version_is_not_replaced_by_latest(self):
        result = resolve_version(QueryBasis(tariff_version="2027"), self.versions)
        self.assertIs(result.status, VersionStatus.UNVERIFIED)
        self.assertEqual(result.version, "2027")
        self.assertIn("不能用其它版本顶替", result.reason)

    def test_date_outside_all_windows_is_unverified(self):
        result = resolve_version(QueryBasis(declaration_date="2024-12-31"), self.versions)
        self.assertIs(result.status, VersionStatus.UNVERIFIED)
        self.assertIsNone(result.version)

    def test_malformed_date_is_not_truncated_into_valid_date(self):
        result = resolve_version(QueryBasis(declaration_date="2026-05-20 garbage"), self.versions)
        self.assertIs(result.status, VersionStatus.UNVERIFIED)
        self.assertIn("无法解析", result.reason)

    def test_overlapping_windows_are_unverified(self):
        versions = [
            VersionWindow(version="A", effective_from="2026-01-01", effective_to="2026-12-31"),
            VersionWindow(version="B", effective_from="2026-06-01", effective_to="2027-05-31"),
        ]
        result = resolve_version(QueryBasis(declaration_date="2026-07-01"), versions)
        self.assertIs(result.status, VersionStatus.UNVERIFIED)
        self.assertIn("无法唯一确定", result.reason)

    def test_no_version_windows_cannot_verify_basis(self):
        result = resolve_version(QueryBasis(tariff_version="2026"), [])
        self.assertIs(result.status, VersionStatus.UNVERIFIED)

    def test_verified_resolution_requires_version_and_window(self):
        from adapters.sources.hs_lookup import VersionResolution

        with self.assertRaises(ValidationError):
            VersionResolution(status=VersionStatus.VERIFIED, version="2026")
        with self.assertRaises(ValidationError):
            VersionResolution(
                status=VersionStatus.VERIFIED,
                window=VersionWindow(version="2026", effective_from="2026-01-01"),
            )


class DatasetAuthorityTest(SimpleTestCase):
    def test_official_unreviewed_data_is_not_authoritative(self):
        info = DatasetInfo(
            source_type=SourceKind.OFFICIAL_TARIFF,
            authority=Authority.AUTHORITATIVE,
            synthetic=False,
            review_status=ReviewStatus.UNREVIEWED,
        )
        self.assertFalse(info.authoritative)

    def test_official_reviewed_data_is_authoritative(self):
        info = DatasetInfo(
            source_type=SourceKind.OFFICIAL_TARIFF,
            authority=Authority.AUTHORITATIVE,
            synthetic=False,
            review_status=ReviewStatus.REVIEWED,
            reviewed_by="expert-a",
            reviewed_at="2026-11-16T00:00:00+08:00",
            retrieved_at="2026-11-16T00:00:00+08:00",
        )
        self.assertTrue(info.authoritative)

    def test_reviewed_data_requires_reviewer_time_and_retrieved_at(self):
        with self.assertRaises(ValidationError):
            DatasetInfo(
                source_type=SourceKind.OFFICIAL_TARIFF,
                authority=Authority.AUTHORITATIVE,
                review_status=ReviewStatus.REVIEWED,
                reviewed_by="expert-a",
                reviewed_at="2026-11-16T00:00:00+08:00",
            )
        with self.assertRaises(ValidationError):
            DatasetInfo(
                source_type=SourceKind.OFFICIAL_TARIFF,
                authority=Authority.AUTHORITATIVE,
                review_status=ReviewStatus.REVIEWED,
                reviewed_by="expert-a",
                reviewed_at="not-a-date",
                retrieved_at="2026-11-16T00:00:00+08:00",
            )
        with self.assertRaises(ValidationError):
            DatasetInfo(
                source_type=SourceKind.OFFICIAL_TARIFF,
                authority=Authority.AUTHORITATIVE,
                review_status=ReviewStatus.REVIEWED,
                reviewed_by="expert-a",
                reviewed_at="2026-11-16T00:00:00+08:00",
                retrieved_at="not-a-date",
            )

    def test_synthetic_cannot_claim_authoritative_or_real(self):
        with self.assertRaises(ValidationError):
            DatasetInfo(
                source_type=SourceKind.SYNTHETIC,
                authority=Authority.AUTHORITATIVE,
                synthetic=True,
            )
        with self.assertRaises(ValidationError):
            DatasetInfo(
                source_type=SourceKind.SYNTHETIC,
                authority=Authority.SYNTHETIC,
                synthetic=False,
            )

    def test_third_party_cannot_claim_authoritative(self):
        with self.assertRaises(ValidationError):
            DatasetInfo(
                source_type=SourceKind.THIRD_PARTY,
                authority=Authority.AUTHORITATIVE,
                synthetic=False,
            )

    def test_unspecified_official_data_is_not_authoritative(self):
        info = DatasetInfo(source_type=SourceKind.OFFICIAL_TARIFF, synthetic=False)
        self.assertIs(info.authority, Authority.UNSPECIFIED)
        self.assertFalse(info.authoritative)
