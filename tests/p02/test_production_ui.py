from django.test import SimpleTestCase, override_settings


class ProductionEntryTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    def test_production_entry_serves_only_explicit_assets(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/app/assets/production.js", b"".join(page.streaming_content))
        self.assertEqual(self.client.get("/app/assets/production.js").status_code, 200)
        self.assertEqual(self.client.get("/app/assets/production.css").status_code, 200)
        self.assertEqual(self.client.get("/app/assets/workbench.html").status_code, 404)
        self.assertEqual(self.client.get("/demo/").status_code, 404)
