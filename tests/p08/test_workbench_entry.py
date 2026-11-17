"""Formal entry and public static allowlist remain available with DEBUG off; fixtures do not."""
from django.test import SimpleTestCase, override_settings
from apps.api.demo import WORKBENCH_ASSETS


class WorkbenchEntryTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    def test_live_entry_and_only_allowlisted_resources(self):
        response=self.client.get('/workbench/')
        self.assertEqual(response.status_code,200)
        html=b''.join(response.streaming_content).decode()
        self.assertIn('/workbench/assets/workbench-live.js',html)
        self.assertNotIn('customs-demo-only',html)
        upload=self.client.get('/uploads/')
        self.assertEqual(upload.status_code,200);upload.close()
        for path in WORKBENCH_ASSETS:
            response=self.client.get('/workbench/assets/'+path)
            self.assertEqual(response.status_code,200,path);response.close()
        for path in ('workbench.html','pages/p07.html','../config/settings.py','components/../api.js'):
            response=self.client.get('/workbench/assets/'+path)
            self.assertEqual(response.status_code,404,path)
        self.assertEqual(self.client.get('/workbench/?mode=fixture').status_code,404)
        self.assertEqual(self.client.get('/demo/fixtures/DEMO-SET.json').status_code,404)

    @override_settings(DEBUG=True)
    def test_explicit_development_modes_keep_existing_prototype(self):
        for mode in ('demo','fixture'):
            response=self.client.get('/workbench/?mode='+mode)
            self.assertEqual(response.status_code,200)
            self.assertIn('交互原型',b''.join(response.streaming_content).decode())
