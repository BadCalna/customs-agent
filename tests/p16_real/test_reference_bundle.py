"""Offline temporary contract fixtures, NOT actual customs data or review.

The deliberately real-labelled metadata only simulates the published contract.
Nothing is installed, published, fetched, written to a database or loaded via dotenv.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.engines.reference_bundle import (
    ENV_HISTORY_PATH, ENV_PUBLISHED_DIR, ENV_RELEASE_DIR, P23_FILE_MAP,
    ReferenceBundle, ReferenceBundleConfig, ReferenceBundleError,
)
from adapters.sources import history_search as history
from adapters.sources import hs_lookup as tariff
from adapters.sources.reference_import.model import fingerprint

HS = "0101210010"
SOURCE = {"document": "UNIT-CONTRACT-NOT-REAL", "locator": "Sheet!A10",
          "retrieved_at": "2026-11-17T00:00:00+08:00"}


def dataset(name, reviewed=False):
    result = {"dataset_id": f"UNIT-CONTRACT-{name}", "synthetic": False,
              "source_type": "official_tax" if name == "p17_tax_rates" else "official_tariff",
              "authority": "authoritative", "review_status": "reviewed" if reviewed else "unreviewed",
              "retrieved_at": "2026-11-17T00:00:00+08:00"}
    if reviewed:
        result.update(reviewed_by="UNIT-TEST-NOT-A-REAL-REVIEWER", reviewed_at="2026-11-18T00:00:00+08:00")
    return result


def payloads(reviewed=False):
    window = [{"version": "2026", "effective_from": "2026-11-16", "effective_to": "2026-12-31"}]
    text = "【品目注释】\nUNIT TEST TEXT"
    data = {
        "p14_tariff": {"schema_version": "p14.tariff.1", "versions": copy.deepcopy(window),
                       "codes": [{"code": HS, "version": "2026", "state": "active", "source": SOURCE}]},
        "p14_elements": {"schema_version": "p14.elements.1", "versions": copy.deepcopy(window),
                         "schemas": [{"hs_code": HS, "version": "2026", "source": SOURCE,
                                      "elements": [{"key": "用途", "label": "用途", "order": 1,
                                                    "requirement": "required"}]}]},
        "p17_tax_rates": {"schema_version": "p17.taxrate.1", "versions": [{**window[0], "version": "2026C"}],
                          "rates": [{"hs_code": HS, "version": "2026C", "rate_kind": "zero_rate",
                                     "rate_percent": 0, "rate_raw": "0%", "source": SOURCE}]},
        "tariff_commentary": {"schema_version": "p23.commentary.1", "version": {"version": "2026-11-17T10-20"},
                              "records": [{"tariff_no": "0101", "kind": "品目", "source": SOURCE,
                                           "sections": [{"label": "品目注释", "text": "UNIT TEST TEXT", "anchor": "0101#1"}],
                                           "text_all": text, "text_length": len(text),
                                           "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                           "section_char_total": len("UNIT TEST TEXT")}]},
    }
    for name, payload in data.items():
        payload["dataset"] = dataset(name, reviewed)
    return data


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def release_fixture(root, *, data=None, reviewed=False, activate=True):
    """Build exactly P23's published structure, no candidate manifest.json."""
    data = payloads(reviewed) if data is None else data
    review = None
    if reviewed:
        review = {"decision": "approved", "reviewed_by": "UNIT-TEST-NOT-A-REAL-REVIEWER",
                  "reviewed_at": "2026-11-18T00:00:00+08:00", "scope": [], "confirmations": {}}
    business = fingerprint({"unit-test": "not-real-business-data"})
    fp = fingerprint({"snapshot_business_fingerprint": business, "converter_version": "p23-unit-test",
                      "review": review, "outputs": {name: fingerprint(value) for name, value in data.items()}})
    snapshot_id = "unit-contract-aabbccddeeff"
    release_id = f"rel-aabbccddeeff-{fp[:12]}"
    path = root / "published" / "releases" / release_id
    path.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, payload in data.items():
        filename = P23_FILE_MAP[name]
        write_json(path / filename, payload)
        raw = (path / filename).read_bytes()
        files[name] = {"file": filename, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                       "review_status": payload["dataset"]["review_status"]}
    release = {"schema_version": "p23.release.1", "release_id": release_id, "snapshot_id": snapshot_id,
               "dataset_kind": "real", "converter_version": "p23-unit-test", "business_fingerprint": business,
               "release_fingerprint": fp, "files": files, "review": review,
               "review_status": "reviewed" if reviewed else "unreviewed"}
    write_json(path / "release.json", release)
    write_json(path / ".current.json", {key: release[key] for key in (
        "release_id", "release_fingerprint", "business_fingerprint", "review_status")})
    if activate:
        (root / "published" / "current").symlink_to(f"releases/{release_id}", target_is_directory=True)
        (root / "published" / "current.json").symlink_to("current/.current.json")
    return path, release


class ReferenceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.release = release_fixture(self.root)

    def load(self, **kwargs):
        return ReferenceBundle.load(ReferenceBundleConfig(release_dir=self.path, **kwargs))

    def mutate_release(self, **kwargs):
        changed = {**self.release, **kwargs}
        write_json(self.path / "release.json", changed)
        return changed

    def test_published_real_unreviewed_is_readable_not_authoritative(self):
        bundle = ReferenceBundle.load(ReferenceBundleConfig(published_dir=self.root / "published"))
        result = bundle.hs_source.lookup({"hs_code_raw": HS, "basis": {"declaration_date": "2026-11-18"}})
        self.assertFalse(result.authoritative)
        self.assertFalse(result.declaration_ready)
        self.assertEqual(result.code.code, HS)
        self.assertEqual(bundle.version, "2026")
        self.assertEqual(bundle.bundle_id, self.release["release_id"])
        self.assertEqual(bundle.fingerprint, self.release["release_fingerprint"])
        self.assertEqual(bundle.source_metadata["p17_tax_rates"]["versions"], ["2026C"])
        self.assertIs(bundle.source_metadata["p14_tariff"]["authoritative"], False)
        self.assertEqual(bundle.source_metadata["tariff_commentary"]["version"], "2026-11-17T10-20")
        self.assertIsNone(bundle.history_source)
        self.assertIsNone(bundle.commentary_source)
        self.assertFalse((self.path / "manifest.json").exists())

    def test_require_reviewed_rejects_unreviewed(self):
        with self.assertRaisesRegex(ReferenceBundleError, "unreviewed"):
            self.load(require_reviewed=True)

    def test_reviewed_contract_allows_sources_and_commentary(self):
        path, _ = release_fixture(self.root / "reviewed", reviewed=True)
        bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path, require_reviewed=True, include_commentary=True))
        result = bundle.hs_source.lookup({"hs_code_raw": HS, "basis": {"declaration_date": "2026-11-18"}})
        self.assertTrue(result.authoritative)
        self.assertTrue(result.declaration_ready)
        self.assertIsNotNone(bundle.commentary_source)
        checked = bundle.elements_source.check({"hs_code_raw": HS, "basis": {"tariff_version": "2026"}, "declared": {"用途": "UNIT"}})
        self.assertEqual(checked.sufficiency.value, "sufficient")

    def test_unconfigured_never_reads_default_sources_or_dotenv(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(history, "default_history_store", side_effect=AssertionError("default fixture")), patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv")):
            with self.assertRaisesRegex(ReferenceBundleError, "Explicitly configure"):
                ReferenceBundle.load()
            bundle = self.load()
            self.assertIsNone(bundle.history_source)

    def test_env_only_explicit_variables(self):
        config = ReferenceBundleConfig.from_env({ENV_RELEASE_DIR: str(self.path), "CUSTOMS_TARIFF_PATH": "ignored-sample.json"})
        self.assertEqual(ReferenceBundle.load(config).version, "2026")
        with self.assertRaises(ReferenceBundleError):
            ReferenceBundle.load(ReferenceBundleConfig.from_env({ENV_RELEASE_DIR: str(self.path), ENV_PUBLISHED_DIR: str(self.root / "published")}))

    def test_missing_pointer_does_not_search_releases_or_candidates(self):
        with self.assertRaisesRegex(ReferenceBundleError, "pointer"):
            ReferenceBundle.load(ReferenceBundleConfig(published_dir=self.root / "nonexistent"))

    def test_direct_current_path_is_pinned(self):
        bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=self.root / "published" / "current"))
        self.assertEqual(bundle.bundle_id, self.release["release_id"])

    def test_unsafe_current_targets_and_missing_declared_data_refused(self):
        current = self.root / "published" / "current"
        current.unlink()
        current.symlink_to("../../outside-release")
        with self.assertRaisesRegex(ReferenceBundleError, "Invalid published/current"):
            ReferenceBundle.load(ReferenceBundleConfig(published_dir=self.root / "published"))
        (self.path / "tax_rates.json").unlink()
        with self.assertRaisesRegex(ReferenceBundleError, "missing"):
            self.load()

    def test_rejected_review_record_refused(self):
        self.mutate_release(review_status="reviewed", review={"decision": "rejected"})
        # The pointer is independently correct; failure must still come from review.
        write_json(self.path / ".current.json", {**self.release, "review_status": "reviewed"})
        with self.assertRaisesRegex(ReferenceBundleError, "approved"):
            self.load()

    def test_legacy_current_json_refused(self):
        pointer = self.root / "published" / "current.json"
        pointer.unlink()
        write_json(pointer, self.release)
        with self.assertRaisesRegex(ReferenceBundleError, "Legacy"):
            ReferenceBundle.load(ReferenceBundleConfig(published_dir=self.root / "published"))

    def test_current_reads_link_once_even_if_activation_interleaves(self):
        changed = payloads()
        changed["p14_tariff"]["codes"][0]["description"] = "NEW RELEASE"
        next_path, _ = release_fixture(self.root, data=changed, activate=False)
        actual_readlink = os.readlink
        calls = []
        def readlink(path):
            result = actual_readlink(path)
            if Path(path).name == "current":
                calls.append(str(path))
                pending = self.root / "published" / "unit-next-current"
                pending.symlink_to(f"releases/{next_path.name}", target_is_directory=True)
                os.replace(pending, self.root / "published" / "current")
            return result
        with patch("adapters.engines.reference_bundle.os.readlink", side_effect=readlink):
            bundle = ReferenceBundle.load(ReferenceBundleConfig(published_dir=self.root / "published"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(bundle.bundle_id, self.release["release_id"])
        self.assertNotEqual(next_path.name, bundle.bundle_id)
        self.assertEqual(bundle.tariff_store.load().codes[0].description, "")

    def test_pointer_metadata_mismatch_refused(self):
        write_json(self.path / ".current.json", {**self.release, "release_fingerprint": "0" * 64})
        with self.assertRaisesRegex(ReferenceBundleError, "mismatch"):
            self.load()

    def test_synthetic_release_refused(self):
        self.mutate_release(dataset_kind="synthetic")
        with self.assertRaisesRegex(ReferenceBundleError, "real"):
            self.load()

    def test_synthetic_none_or_coerced_flags_refused(self):
        for flag in (True, None, "false", 0):
            with self.subTest(flag=flag):
                data = payloads()
                data["p14_tariff"]["dataset"]["synthetic"] = flag
                path, _ = release_fixture(self.root / str(flag), data=data)
                with self.assertRaisesRegex(ReferenceBundleError, "synthetic is False"):
                    ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))

    def test_dataset_missing_or_third_party_refused(self):
        for change in ({"source_type": "third_party"}, {"dataset_id": ""}, {"review_status": "approved"}):
            data = payloads()
            data["p14_tariff"]["dataset"].update(change)
            path, _ = release_fixture(self.root / next(iter(change)), data=data)
            with self.assertRaises(ReferenceBundleError):
                ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))

    def test_missing_required_file_and_bytes_or_digest_corruption_refused(self):
        for field, value in (("bytes", 1), ("sha256", "0" * 64), ("file", "../tariff.json")):
            with self.subTest(field=field):
                files = copy.deepcopy(self.release["files"])
                files["p14_tariff"][field] = value
                self.mutate_release(files=files)
                with self.assertRaises(ReferenceBundleError):
                    self.load()
        files = copy.deepcopy(self.release["files"])
        del files["p14_tariff"]
        self.mutate_release(files=files)
        with self.assertRaisesRegex(ReferenceBundleError, "missing required"):
            self.load()

    def test_release_fingerprint_recomputed_not_just_compared_to_pointer(self):
        self.mutate_release(converter_version="tampered-converter")
        with self.assertRaisesRegex(ReferenceBundleError, "release_fingerprint"):
            self.load()

    def test_expected_fingerprint_and_version_are_enforced(self):
        for kwargs in ({"expected_fingerprint": "0" * 64}, {"expected_version": "2025"}):
            with self.assertRaises(ReferenceBundleError):
                self.load(**kwargs)

    def test_cross_dataset_version_mismatch_rejected_even_with_valid_release_hash(self):
        for field, value in (("version", "2025"), ("effective_from", "2025-01-01")):
            data = payloads()
            data["p14_elements"]["versions"][0][field] = value
            if field == "version":
                data["p14_elements"]["schemas"][0]["version"] = value
            path, _ = release_fixture(self.root / field, data=data)
            with self.assertRaisesRegex(ReferenceBundleError, "version windows"):
                ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))

    def test_review_status_cannot_be_forged_independently(self):
        self.mutate_release(review_status="reviewed")
        with self.assertRaisesRegex(ReferenceBundleError, "review_status"):
            self.load()

    def test_injected_p14_p17_stores_must_match_validated_release(self):
        bundle = self.load()
        injected = self.load(tariff_store=bundle.tariff_store, elements_store=bundle.elements_store,
                             tax_rate_store=bundle.tax_rate_store)
        self.assertIsInstance(injected.hs_source, tariff.HsLookupSource)
        wrong = tariff.InMemoryTariffStore([], bundle.tariff_store.load().versions, dataset=bundle.tariff_store.load().dataset)
        with self.assertRaisesRegex(ReferenceBundleError, "does not match"):
            self.load(tariff_store=wrong)

    def test_loaded_bundle_detached_from_files_and_returned_snapshots(self):
        bundle = self.load()
        snapshot = bundle.tariff_store.load()
        snapshot.codes[0].code = "9999999999"
        write_json(self.path / "tariff.json", {})
        self.assertEqual(bundle.tariff_store.load().codes[0].code, HS)
        self.assertEqual(bundle.hs_source.lookup({"hs_code_raw": HS, "basis": {"tariff_version": "2026"}}).code.code, HS)
        with self.assertRaises(ReferenceBundleError):
            self.load()

    def test_history_only_explicit_p15_json_no_default_fixture(self):
        path = self.root / "data" / "private" / "p15-contract" / "history_records.json"
        path.parent.mkdir(parents=True)
        payload = {"schema_version": "p15.history.1", "dataset": {"dataset_id": "PRIVATE-UNIT", "synthetic": False},
                   "records": [{"record_id": "UNIT-1", "historical_hs": HS, "historical_name": "UNIT PRODUCT",
                                "recorded_at": "2026-11-17", "outcome": "approved",
                                "source": {"document": "PRIVATE-UNIT-DOC", "locator": "item 1"}}]}
        write_json(path, payload)
        with patch.object(history, "default_history_store", side_effect=AssertionError("default")):
            bundle = self.load(history_path=path)
        result = bundle.history_source.search({"name": "UNIT PRODUCT"})
        self.assertTrue(result.reference_only)
        self.assertEqual(result.candidates[0].historical_hs, HS)
        self.assertNotIn("PRIVATE-UNIT-DOC", json.dumps(bundle.source_metadata))
        self.assertNotIn(str(path), json.dumps(bundle.source_metadata))
        self.assertEqual(bundle.source_metadata["history"]["record_count"], 1)
        self.assertEqual(ReferenceBundleConfig.from_env({ENV_RELEASE_DIR: str(self.path), ENV_HISTORY_PATH: str(path)}).history_path, str(path))

    def test_injected_history_source_is_pinned_and_never_default_constructed(self):
        source = history.HistorySearchSource(records=[], dataset=history.HistoryDatasetInfo(dataset_id="PG-INJECTED-UNIT", synthetic=False))
        bundle = self.load(history_source=source)
        self.assertIsInstance(bundle.history_source, history.HistorySearchSource)
        source._store._dataset.synthetic = True
        self.assertIs(bundle.history_source.search({"name": "UNIT"}).dataset.synthetic, False)
        with self.assertRaisesRegex(ReferenceBundleError, "only one"):
            self.load(history_source=source, history_path="ignored")

    def test_synthetic_or_missing_history_source_fails_without_fallback(self):
        for flag in (True, None):
            source = history.HistorySearchSource(records=[], dataset=history.HistoryDatasetInfo(dataset_id="UNIT", synthetic=flag))
            with self.assertRaisesRegex(ReferenceBundleError, "real P15"):
                self.load(history_source=source)
        with self.assertRaises(ReferenceBundleError):
            self.load(history_path=self.root / "missing-history.json")

    def test_optional_commentary_missing_and_corruption_are_not_silently_hidden(self):
        data = payloads()
        del data["tariff_commentary"]
        path, _ = release_fixture(self.root / "no-commentary", data=data)
        bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
        self.assertIsNone(bundle.commentary_source)
        with self.assertRaisesRegex(ReferenceBundleError, "missing required"):
            ReferenceBundle.load(ReferenceBundleConfig(release_dir=path, include_commentary=True))
        data = payloads()
        data["tariff_commentary"]["records"][0]["text_sha256"] = "0" * 64
        path, _ = release_fixture(self.root / "bad-commentary", data=data)
        with self.assertRaisesRegex(ReferenceBundleError, "snapshot contract"):
            ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))

    def test_manifest_required_filename_is_not_guessed(self):
        with self.assertRaises(ReferenceBundleError):
            ReferenceBundle.load(ReferenceBundleConfig(release_dir=self.root / "candidates" / "missing"))
        files = copy.deepcopy(self.release["files"])
        files["p17_tax_rates"]["file"] = "tax_rates"
        self.mutate_release(files=files)
        with self.assertRaisesRegex(ReferenceBundleError, "file mapping"):
            self.load()

    def test_full_p23_ledger_is_verified_but_not_used_as_regulatory_source(self):
        data = payloads()
        data["regulatory_code_ledger"] = {
            "schema_version": "p23.regulatory-codes.1",
            "dataset": {**dataset("ledger"), "source_type": "official_regulatory_codes_only"},
            "records": [{"hs_code": HS, "regulatory_codes_raw": "AB"}],
        }
        path, _ = release_fixture(self.root / "ledger", data=data)
        bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
        self.assertIn("regulatory_code_ledger", bundle.source_metadata)
        self.assertNotIn("p17_regulatory", bundle.source_metadata)

    def test_strict_config_booleans_and_commentary_opt_in(self):
        with self.assertRaises(ReferenceBundleError):
            self.load(require_reviewed="false")
        with self.assertRaises(ReferenceBundleError):
            self.load(commentary_store=object())


if __name__ == "__main__":
    unittest.main()
