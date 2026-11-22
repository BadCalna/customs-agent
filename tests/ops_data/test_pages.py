"""HTML shell contracts under the independent 03 URLConf."""
import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(ROOT_URLCONF="tests.ops_data.urls", REFERENCE_BROWSER_FIXTURE_TESTING=True,
                   REFERENCE_BROWSER_FIXTURE=False)
class ReferenceBrowserPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reference-reader")
        self.index_url = reverse("reference_browser:index")
        self.evidence_url = reverse("reference_browser:evidence")

    def grant(self, *codenames):
        self.user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="reference_browser", codename__in=codenames,
        ))
        self.client.force_login(self.user)

    def test_anonymous_and_forbidden_access_states(self):
        response = self.client.get(self.index_url)
        self.assertContains(response, "请先登录工作台", status_code=401)
        self.assertNotContains(response, 'data-view="note"', status_code=401)
        self.client.force_login(self.user)
        response = self.client.get(self.index_url)
        self.assertContains(response, "暂未开通参考数据权限", status_code=403)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("Cookie", response.headers["Vary"])

    def test_catalog_permission_renders_only_catalog_tabs(self):
        self.grant("view_catalog")
        response = self.client.get(self.index_url)
        self.assertContains(response, 'data-view="note"')
        self.assertContains(response, 'data-view="hs"')
        self.assertNotContains(response, 'data-view="history"')
        self.assertContains(response, f'data-evidence-url="{self.evidence_url}"')
        self.assertContains(response, "reference_browser/reference-browser.js")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.client.head(self.index_url).status_code, 200)
        self.assertEqual(self.client.post(self.index_url).status_code, 405)

    def test_history_list_and_detail_permissions_stay_separate(self):
        self.grant("view_history")
        response = self.client.get(self.index_url, {"tab": "history"})
        self.assertContains(response, 'data-view="history"')
        self.assertNotContains(response, 'data-view="note"')
        self.assertContains(response, 'data-can-history-detail="false"')
        self.assertNotContains(response, 'name="detail_reason"')
        self.grant("view_history_detail")
        self.assertContains(self.client.get(self.index_url), 'name="detail_reason"')

    def test_evidence_deep_link_pins_the_requested_section(self):
        self.grant("view_catalog")
        response = self.client.get(self.evidence_url, {
            "kind": "note", "snapshot_id": "fx-2026-01", "record_id": "note-ch84", "anchor": "p2",
        })
        self.assertContains(response, "证据定位")
        payload, = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
                              response.content.decode(), flags=re.DOTALL)
        data = json.loads(payload)
        self.assertEqual(data["snapshot_id"], "fx-2026-01")
        self.assertEqual(data["source_locator"], "CH84:p2")
        self.assertEqual(data["section"]["anchor"], "p2")
        self.assertIn("传动", data["section"]["text"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.client.post(self.evidence_url).status_code, 405)

    def test_history_evidence_denies_list_only_and_allows_detail_permission(self):
        params = {"kind": "history", "snapshot_id": "fx-2026-01",
                  "record_id": "history-fixture-row-17", "anchor": "row-17"}
        response = self.client.get(self.evidence_url, params)
        self.assertNotContains(response, params["record_id"], status_code=401)
        self.grant("view_history")
        response = self.client.get(self.evidence_url, params)
        self.assertNotContains(response, params["record_id"], status_code=403)
        self.assertNotContains(response, 'type="application/json"', status_code=403)
        self.grant("view_history_detail")
        response = self.client.get(self.evidence_url, params)
        self.assertContains(response, params["record_id"])
        self.assertContains(response, params["anchor"])

    def test_unavailable_evidence_never_echoes_identifiers_or_internal_errors(self):
        self.grant("view_catalog")
        params = {"kind": "note", "snapshot_id": "fx-withdrawn", "record_id": "not-a-public-locator"}
        response = self.client.get(self.evidence_url, params)
        self.assertNotContains(response, params["record_id"], status_code=404)
        self.assertNotContains(response, params["snapshot_id"], status_code=404)
        params["snapshot_id"] = "fx-2026-01"
        with patch("apps.reference_browser.services.load_generation", side_effect=ValueError("private source path")):
            response = self.client.get(self.evidence_url, params)
        self.assertNotContains(response, "private source path", status_code=503)
