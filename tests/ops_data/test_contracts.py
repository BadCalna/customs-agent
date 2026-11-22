"""Contract and security tests for the standalone 03 reference browser.

The tests are intentionally API-level and fixture-only: they prove snapshot
pinning, evidence-map shape, pagination, permission non-enumeration, and that
write/import routes are absent without touching business tables.
"""
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings

from apps.reference_browser.store import APP_ROOT, parse_generation, load_generation


BASE = {"ROOT_URLCONF": "tests.ops_data.urls", "REFERENCE_BROWSER_FIXTURE_TESTING": True,
        "REFERENCE_BROWSER_FIXTURE": False}


@override_settings(**BASE)
class OpsDataContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.catalog_user = user_model.objects.create_user(username="ops-catalog")
        cls.history_user = user_model.objects.create_user(username="ops-history")
        cls.detail_user = user_model.objects.create_user(username="ops-detail")
        content_type = Permission.objects.get(codename="view_catalog", content_type__app_label="reference_browser").content_type
        cls.catalog_user.user_permissions.add(Permission.objects.get(codename="view_catalog", content_type=content_type))
        cls.history_user.user_permissions.add(Permission.objects.get(codename="view_history", content_type=content_type))
        cls.detail_user.user_permissions.add(
            Permission.objects.get(codename="view_history", content_type=content_type),
            Permission.objects.get(codename="view_history_detail", content_type=content_type),
        )

    def api(self, path, user=None):
        client = Client()
        if user:
            client.force_login(user)
        return client.get(path)

    def test_fixture_generation_and_evidence_map_are_sealed(self):
        generation = load_generation()
        self.assertIn(("fx-2026-01", "note"), generation.snapshots)
        self.assertIn(("fx-2026-01", "hs"), generation.snapshots)
        self.assertIn(("fx-2026-01", "history"), generation.snapshots)
        self.assertEqual(len(generation.evidence), 33)
        evidence_path = APP_ROOT / "evidence-map.v1.jsonl"
        rows = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), len(generation.evidence))
        for row in rows:
            self.assertEqual(set(row), {"kind", "snapshot_id", "record_id", "anchor", "label", "source_locator"})
            self.assertNotRegex(json.dumps(row, ensure_ascii=False), r"/|\\\\|1[3-9][0-9]{9}")

    def test_catalog_and_hs_pin_requested_snapshot(self):
        response = self.api("/api/v1/ops/data/catalog?tab=note&snapshot_id=fx-2026-01", self.catalog_user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["snapshot_id"], "fx-2026-01")
        hs = self.api("/api/v1/ops/data/hs/0101210000?snapshot_id=fx-2026-01&version=FX2026", self.catalog_user)
        self.assertEqual(hs.status_code, 200)
        self.assertEqual(hs.json()["record"]["code"], "0101210000")
        self.assertEqual(hs.json()["record"]["tax_rate"]["match_level"], "exact_10")

    def test_history_success_candidate_excludes_empty_outcome(self):
        response = self.api("/api/v1/ops/data/history?snapshot_id=fx-2026-01", self.history_user)
        self.assertEqual(response.status_code, 200)
        rows = {row["record_id"]: row for row in response.json()["items"]}
        self.assertTrue(rows["history-fixture-row-17"]["success_candidate"])
        self.assertFalse(rows["history-fixture-row-18"]["success_candidate"])
        self.assertEqual(rows["history-fixture-row-18"]["success_exclusion_reason"], "outcome_not_explicitly_successful")

    def test_unauthorized_history_does_not_enumerate(self):
        anonymous = self.api("/api/v1/ops/data/history?snapshot_id=fx-2026-01")
        self.assertEqual(anonymous.status_code, 401)
        forbidden = self.api("/api/v1/ops/data/history?snapshot_id=fx-2026-01", self.catalog_user)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(anonymous.json()["error"]["details"], forbidden.json()["error"]["details"])
        self.assertNotIn("history-fixture-row-17", forbidden.content.decode())

    def test_history_detail_requires_both_permissions_and_evidence(self):
        denied = self.api("/api/v1/ops/data/history/history-fixture-row-17?snapshot_id=fx-2026-01", self.history_user)
        self.assertEqual(denied.status_code, 403)
        allowed = self.api("/api/v1/ops/data/history/history-fixture-row-17?snapshot_id=fx-2026-01", self.detail_user)
        self.assertEqual(allowed.status_code, 200)
        evidence = self.api("/api/v1/ops/data/evidence?kind=history&snapshot_id=fx-2026-01&record_id=history-fixture-row-17&anchor=row-17", self.detail_user)
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.json()["evidence_ref"]["anchor"], "row-17")

    def test_withdrawn_snapshot_and_missing_snapshot_are_same_not_found(self):
        withdrawn = self.api("/api/v1/ops/data/catalog?tab=note&snapshot_id=fx-withdrawn", self.catalog_user)
        missing = self.api("/api/v1/ops/data/catalog?tab=note&snapshot_id=fx-does-not-exist", self.catalog_user)
        self.assertEqual(withdrawn.status_code, missing.status_code, 404)
        self.assertEqual(withdrawn.json()["error"], missing.json()["error"])

    def test_no_write_routes_are_exposed(self):
        for route in ("import/runs", "releases/release-fx-2026-01/rollback"):
            response = Client().post("/api/v1/ops/data/" + route)
            self.assertEqual(response.status_code, 405 if response.status_code != 404 else 404)

    def test_corrupt_generation_fails_with_query_failed_without_details(self):
        with patch("apps.reference_browser.services.load_generation", side_effect=Exception("secret source path")):
            response = self.api("/api/v1/ops/data/catalog?tab=note&snapshot_id=fx-2026-01", self.catalog_user)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "query_failed")
        self.assertNotIn("secret source path", response.content.decode())

    def test_snapshot_parser_rejects_tampering(self):
        manifest = (APP_ROOT / "fixtures/generation.v1.json").read_bytes()
        payload = bytearray((APP_ROOT / "fixtures/snapshots.v1.json").read_bytes())
        evidence = (APP_ROOT / "evidence-map.v1.jsonl").read_bytes()
        payload[-1] ^= 1
        with self.assertRaises(Exception):
            parse_generation(manifest, bytes(payload), evidence)
