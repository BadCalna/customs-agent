"""RQ must preserve main's post-recommendation history and privacy boundary."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings

from adapters.engines.mock import MockEngine
from apps.accounts.models import Role
from apps.reviews.models import Task
from apps.runs import services
from apps.runs.jobs import perform_work
from apps.runs.models import Run
from tests.p01.factories import assign, make_item, make_task, make_user, make_version
from tests.p15.test_database_wiring import fixture_store


@override_settings(ENGINE_MODE="mock", EXECUTOR_MODE="rq", HISTORY_REFERENCE_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class QueueHistoryReferenceTests(TestCase):
    def setUp(self):
        self.supplier = make_user("rq-history-supplier", Role.SUPPLIER)
        self.operator = make_user("rq-history-operator", Role.OPERATOR)
        self.task = make_task(self.supplier, stage=Task.Stage.PRECHECKING)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version, name="合成法兰盘", supplier_hs="9999999999")
        self.item.supplier_elements_raw = "材质：不锈钢|规格：DN50"
        self.item.save(update_fields=["supplier_elements_raw"])
        self.run = Run.objects.create(task=self.task, version=self.version, kind="precheck", is_current=True)
        services.register_run(self.run)

    @patch("adapters.sources.history_search.default_history_store", side_effect=fixture_store)
    def test_worker_appends_history_without_anchoring_or_changing_engine_result(self, store):
        with override_settings(HISTORY_REFERENCE_ENABLED=False):
            baseline = services.evaluate_row(self.run, self.item)
        payload_before = services._payload(self.run, self.item).model_dump(mode="json")
        recommend = MockEngine.recommend
        inputs = []
        def capture(engine, payload):
            inputs.append(payload.model_dump(mode="json"))
            return recommend(engine, payload)
        with patch.object(MockEngine, "recommend", autospec=True, side_effect=capture):
            perform_work(str(self.run.pk))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "succeeded")
        row = self.run.items.get()
        self.assertEqual(inputs, [payload_before])
        self.assertNotIn("history_reference", inputs[0])
        self.assertNotIn("historical_hs", str(inputs[0]))
        self.assertNotIn("7307210000", str(inputs[0]))
        for key in ("suggested_hs", "comparison_status", "confidence"):
            self.assertEqual(getattr(row, key), baseline[key])
        result = deepcopy(row.result)
        history = result.pop("history_reference")
        result.pop("attempts")
        self.assertEqual(result, baseline["result"])
        self.assertEqual(history["outcome"], "matched")
        self.assertEqual(history["candidates"][0]["historical_hs"], "7307210000")
        self.assertFalse(any(entry.get("source_type") == "history" for entry in row.result["evidence"]))
        self.assertEqual(row.attempt_records.get().input_hash, payload_before["context"]["input_hash"])
        store.assert_called_once()
        self.item.refresh_from_db()
        self.assertEqual(self.item.supplier_hs_raw, "9999999999")

    @override_settings(HISTORY_REFERENCE_ENABLED=False)
    def test_disabled_worker_never_reads_history(self):
        with patch("apps.reviews.services.history_reference.HistorySearchSource") as source:
            perform_work(str(self.run.pk))
        source.assert_not_called()
        self.assertNotIn("history_reference", self.run.items.get().result)

    @patch("adapters.sources.history_search.default_history_store", side_effect=fixture_store)
    def test_supplier_run_and_card_never_expose_history_or_legacy_history_evidence(self, _store):
        perform_work(str(self.run.pk))
        row = self.run.items.get()
        self.assertIn("history_reference", row.result)
        # Older records may have put history in generic evidence. Both public
        # serializers must filter it before validating or exposing the payload.
        row.result["evidence"].append({"source_type": "history", "title": "private-contract-marker"})
        row.result["history_reference"]["private_marker"] = "private-contract-marker"
        row.save(update_fields=["result"])
        self.client.force_login(self.supplier)
        for url in (f"/api/v1/runs/{self.run.pk}",
                    f"/api/v1/versions/{self.version.pk}/items/{self.item.pk}"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("private-contract-marker", response.content.decode())
                self.assertNotIn("history_reference", response.json())
                self.assertTrue(response.json()["evidence"])
                self.assertTrue(all(entry["source_type"] != "history" for entry in response.json()["evidence"]))
        self.assertIn("history_reference", self.run.items.get().result)

    @patch("adapters.sources.history_search.default_history_store", side_effect=fixture_store)
    def test_history_does_not_bypass_engine_configuration_fence(self, store):
        with override_settings(ENGINE_MODE="openai"):
            perform_work(str(self.run.pk))
        store.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "superseded")
        self.assertFalse(self.run.items.get().attempt_records.exists())
