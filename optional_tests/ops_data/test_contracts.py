"""Fixture, API, and permission contracts for the standalone reference browser."""
import json
from hashlib import sha256
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve

from apps.reference_browser.apps import reference_browser_deploy_check
from apps.reference_browser.store import APP_ROOT, SnapshotUnreadable, load_generation, parse_generation


BASE = {"ROOT_URLCONF": "optional_tests.ops_data.urls", "REFERENCE_BROWSER_FIXTURE_TESTING": True,
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

    def test_note_detail_and_evidence_pin_the_original_snapshot(self):
        route = "/api/v1/ops/data/catalog/note/note-ch84"
        evidence = "/api/v1/ops/data/evidence?kind=note&record_id=note-ch84"
        old = self.api(f"{route}?snapshot_id=fx-2025-01", self.catalog_user)
        current = self.api(f"{route}?snapshot_id=fx-2026-01", self.catalog_user)
        self.assertEqual((old.status_code, current.status_code), (200, 200))
        historical = old.json()["record"]
        self.assertEqual(historical["sections"][0]["text"], "旧快照合成原文。")
        self.assertEqual(len(historical["sections"]), 1)
        self.assertNotEqual(historical["text_sha256"], current.json()["record"]["text_sha256"])
        old_ref = self.api(f"{evidence}&snapshot_id=fx-2025-01&anchor=p1", self.catalog_user)
        self.assertEqual(old_ref.status_code, 200)
        self.assertEqual(old_ref.json()["evidence_ref"], historical["sections"][0]["evidence_ref"])
        wrong = self.api(f"{evidence}&snapshot_id=fx-2025-01&anchor=p2", self.catalog_user)
        self.assertEqual(wrong.status_code, 404)
        unpinned = self.api(route, self.catalog_user)
        self.assertEqual(unpinned.json()["error"]["code"], "snapshot_required")

    def test_catalog_filters_segments_without_returning_full_text(self):
        route = "/api/v1/ops/data/catalog?tab=note&snapshot_id=fx-2026-01"
        response = self.api(f"{route}&label=子目注释&keyword=阀门&page_size=20", self.catalog_user)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["page"], body["page_size"], body["total"], body["has_next"]), (1, 20, 1, False))
        self.assertEqual([row["record_id"] for row in body["items"]], ["note-ch84"])
        self.assertNotIn("text_all", body["items"][0])
        self.assertNotIn("sections", body["items"][0])
        page2 = self.api(f"{route}&label=子目注释&keyword=阀门&page=2", self.catalog_user)
        self.assertEqual((page2.json()["total"], page2.json()["items"]), (1, []))
        invalid = self.api(f"{route}&page_size=101", self.catalog_user)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_query")

    def test_hs_zero_is_not_missing_and_fallback_is_not_exact(self):
        route = "/api/v1/ops/data/hs/{code}?snapshot_id=fx-2026-01&version=FX2026"
        exact = self.api(route.format(code="0101210000"), self.catalog_user).json()["record"]
        fallback = self.api(route.format(code="8504401900"), self.catalog_user).json()["record"]
        unknown = self.api(route.format(code="0101290000"), self.catalog_user).json()["record"]
        conflict = self.api(route.format(code="0101300000"), self.catalog_user).json()["record"]
        self.assertEqual(exact["tax_rate"]["rate_percent"], 0)
        self.assertEqual(exact["tax_rate"]["conditions"], [])
        self.assertEqual([element["requirement"] for element in exact["elements"]], ["conditional", "unknown"])
        self.assertEqual(fallback["tax_rate"]["match_level"], "fallback_8")
        self.assertIsNone(fallback["tax_rate"]["conditions"])
        self.assertTrue(fallback["needs_review"])
        self.assertIsNone(unknown["tax_rate"]["rate_percent"])
        self.assertEqual(conflict["status"], "conflict")
        self.assertIsNone(conflict["tax_rate"])
        self.assertEqual(len(conflict["source_variants"]), 2)

    def test_unavailable_snapshots_are_distinct_from_no_match(self):
        for route, user, state in (
            ("catalog?tab=note&snapshot_id=fx-input-missing", self.catalog_user, "input_missing"),
            ("catalog?tab=hs&snapshot_id=fx-not-provided", self.catalog_user, "not_provided"),
            ("history?snapshot_id=fx-unsupported", self.history_user, "unsupported"),
            ("history?snapshot_id=fx-2026-01&name=nonexistent", self.history_user, "no_match"),
        ):
            with self.subTest(state=state):
                response = self.api(f"/api/v1/ops/data/{route}", user)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual((body["outcome"], body["items"]), (state, []))

    def test_history_success_candidate_excludes_empty_outcome(self):
        response = self.api("/api/v1/ops/data/history?snapshot_id=fx-2026-01", self.history_user)
        self.assertEqual(response.status_code, 200)
        rows = {row["record_id"]: row for row in response.json()["items"]}
        self.assertTrue(rows["history-fixture-row-17"]["success_candidate"])
        self.assertFalse(rows["history-fixture-row-18"]["success_candidate"])
        self.assertEqual(rows["history-fixture-row-18"]["success_exclusion_reason"], "outcome_not_explicitly_successful")

    def test_unauthorized_history_does_not_enumerate(self):
        for user, expected_status, code in (
            (None, 401, "unauthorized"), (self.catalog_user, 403, "forbidden"),
        ):
            with self.subTest(code=code):
                errors = []
                for snapshot in ("fx-2026-01", "fx-does-not-exist"):
                    response = self.api(f"/api/v1/ops/data/history?snapshot_id={snapshot}", user)
                    self.assertEqual(response.status_code, expected_status)
                    body = response.json()
                    self.assertEqual(set(body), {"error", "request_id"})
                    self.assertEqual(body["error"]["code"], code)
                    self.assertEqual(body["error"]["details"], {})
                    errors.append(body["error"])
                self.assertEqual(*errors)

    def test_superuser_without_explicit_reference_grants_is_denied(self):
        user = get_user_model().objects.create_superuser(username="ops-ungranted-admin")
        for route in ("snapshots", "catalog?snapshot_id=fx-2026-01", "history?snapshot_id=fx-2026-01"):
            with self.subTest(route=route):
                response = self.api(f"/api/v1/ops/data/{route}", user)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "forbidden")

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

    def test_scope_policy_hides_rows_from_list_counts_detail_and_evidence(self):
        def visible(user, *, kind, snapshot_id, dataset_key=None, record_id=None):
            return kind != "history" or record_id != "history-fixture-row-17"

        history = "/api/v1/ops/data/history"
        snapshot = "snapshot_id=fx-2026-01"
        with override_settings(REFERENCE_BROWSER_SCOPE_POLICY=visible):
            response = self.api(f"{history}?{snapshot}", self.detail_user)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["total"], 2)
            self.assertEqual(body["datasets"][0]["record_count"], 2)
            self.assertEqual(body["snapshot"]["statistics"]["record_count"], 2)
            self.assertNotIn("history-fixture-row-17", response.content.decode())
            routes = (
                f"{history}/{{record_id}}?{snapshot}",
                f"/api/v1/ops/data/evidence?{snapshot}&kind=history&record_id={{record_id}}&anchor=row-17",
            )
            for route in routes:
                with self.subTest(route=route):
                    hidden, missing = (self.api(route.format(record_id=record_id), self.detail_user)
                                       for record_id in ("history-fixture-row-17", "nonexistent"))
                    self.assertEqual((hidden.status_code, missing.status_code), (404, 404))
                    self.assertEqual(hidden.json()["error"], missing.json()["error"])

    def test_denied_history_detail_evidence_and_export_emit_sanitized_events(self):
        routes = {
            "history.detail": "history/history-fixture-row-17?snapshot_id=fx-2026-01",
            "evidence.read": "evidence?kind=history&snapshot_id=fx-2026-01&record_id=history-fixture-row-17",
            "history.export": "history/export?snapshot_id=fx-2026-01",
        }
        for event, route in routes.items():
            with self.subTest(event=event), self.assertLogs("customs.ops.events", level="INFO") as captured:
                response = self.api(f"/api/v1/ops/data/{route}", self.history_user)
                self.assertEqual(response.status_code, 403)
                body = response.json()
                self.assertEqual(set(body), {"error", "request_id"})
                self.assertEqual(body["error"]["code"], "forbidden")
                self.assertEqual(response["Cache-Control"], "private, no-store")
                self.assertEqual(response["Referrer-Policy"], "no-referrer")
                self.assertEqual(len(captured.records), 1)
                log = captured.records[0]
                self.assertEqual(log.getMessage(), "ops.event")
                self.assertEqual(log.ops_event["event_type"], f"reference_browser.{event}")
                self.assertEqual(log.ops_event["outcome"], "denied")
                self.assertEqual(log.ops_event["request_id"], body["request_id"])
                self.assertIsNone(log.ops_event["target_id"])
                self.assertNotIn("history-fixture-row-17", json.dumps(log.ops_event))

    def test_standalone_reads_do_not_write_database_or_require_business_tables(self):
        client = Client()
        client.force_login(self.detail_user)
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/ops/data/history/history-fixture-row-17?snapshot_id=fx-2026-01")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reviews_task", connection.introspection.table_names())
        for query in queries:
            self.assertTrue(query["sql"].lstrip().upper().startswith("SELECT "), query["sql"])

    def test_no_write_routes_are_exposed(self):
        for route in ("import/runs", "import/fx-run/validate", "import/fx-run/publish",
                      "releases/release-fx-2026-01/rollback"):
            with self.subTest(route=route), self.assertRaises(Resolver404):
                resolve("/api/v1/ops/data/" + route)

    @override_settings(DEBUG=False, REFERENCE_BROWSER_FIXTURE=True, REFERENCE_BROWSER_FIXTURE_TESTING=False)
    def test_fixture_opt_in_is_disabled_without_debug_and_rejected_for_deploy(self):
        self.assertEqual(len(load_generation().snapshots), 0)
        self.assertEqual([error.id for error in reference_browser_deploy_check(None)], ["reference_browser.E001"])
        with override_settings(REFERENCE_BROWSER_FIXTURE=False, REFERENCE_BROWSER_FIXTURE_TESTING=True):
            self.assertEqual([error.id for error in reference_browser_deploy_check(None)], ["reference_browser.E001"])
        with override_settings(REFERENCE_BROWSER_FIXTURE=False, REFERENCE_BROWSER_FIXTURE_TESTING=False):
            self.assertEqual(reference_browser_deploy_check(None), [])

    def test_corrupt_generation_fails_with_query_failed_without_details(self):
        with patch("apps.reference_browser.services.load_generation", side_effect=SnapshotUnreadable("secret source path")):
            response = self.api("/api/v1/ops/data/catalog?tab=note&snapshot_id=fx-2026-01", self.catalog_user)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "query_failed")
        self.assertNotIn("secret source path", response.content.decode())

    def test_snapshot_parser_rejects_tampering(self):
        manifest = (APP_ROOT / "fixtures/generation.v1.json").read_bytes()
        payload = bytearray((APP_ROOT / "fixtures/snapshots.v1.json").read_bytes())
        evidence = (APP_ROOT / "evidence-map.v1.jsonl").read_bytes()
        payload[-1] ^= 1
        with self.assertRaises(SnapshotUnreadable):
            parse_generation(manifest, bytes(payload), evidence)

    def test_resealed_snapshots_reject_invalid_note_text(self):
        manifest = json.loads((APP_ROOT / "fixtures/generation.v1.json").read_bytes())
        original = (APP_ROOT / "fixtures/snapshots.v1.json").read_bytes()
        evidence = (APP_ROOT / "evidence-map.v1.jsonl").read_bytes()
        for check in ("hash", "reassembly"):
            with self.subTest(check=check), self.assertRaises(SnapshotUnreadable):
                payload = json.loads(original)
                note = payload["snapshots"][0]["records"][0]["note"]
                if check == "hash":
                    note["text_sha256"] = "0" * 64
                else:
                    part = note["sections"][0]
                    part["text"] = "X" + part["text"][1:]  # Preserve character counts.
                modified = json.dumps(payload).encode()
                sealed = {**manifest, "snapshots_sha256": sha256(modified).hexdigest()}
                parse_generation(json.dumps(sealed).encode(), modified, evidence)

    def test_resealed_evidence_still_rejects_invalid_rows(self):
        manifest = json.loads((APP_ROOT / "fixtures/generation.v1.json").read_bytes())
        payload = (APP_ROOT / "fixtures/snapshots.v1.json").read_bytes()
        evidence = (APP_ROOT / "evidence-map.v1.jsonl").read_bytes().splitlines()
        row = json.loads(evidence[0])
        for name, invalid in {
            "duplicate": [*evidence, evidence[0]],
            "unsafe-locator": [json.dumps({**row, "source_locator": "unsafe/name"}).encode(), *evidence[1:]],
            "wrong-snapshot": [json.dumps({**row, "snapshot_id": "fx-unknown"}).encode(), *evidence[1:]],
            "wrong-label": [json.dumps({**row, "label": "不匹配"}).encode(), *evidence[1:]],
        }.items():
            with self.subTest(name=name), self.assertRaises(SnapshotUnreadable):
                modified = b"\n".join(invalid) + b"\n"
                sealed = {**manifest, "evidence_sha256": sha256(modified).hexdigest()}
                parse_generation(json.dumps(sealed).encode(), payload, modified)
