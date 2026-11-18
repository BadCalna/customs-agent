"""Formal entry and public static allowlist remain available with DEBUG off; fixtures do not."""
import posixpath
import re
from django.test import SimpleTestCase, override_settings
from apps.api.demo import WORKBENCH_ASSETS

MODULE_SPECIFIER = re.compile(r"""(?:from|import)\s*\(?\s*["']([^"']+)["']""")


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
        self.assertEqual(self.client.get('/tasks/').status_code,404)

    @override_settings(DEBUG=False)
    def test_formal_entries_module_graph_loads_without_debug(self):
        visited = set()
        pending = []
        for entry in ('/workbench/', '/uploads/'):
            response = self.client.get(entry)
            self.assertEqual(response.status_code, 200, entry)
            body = b''.join(response.streaming_content).decode()
            response.close()
            pending.extend(re.findall(r"""(?:href|src)\s*=\s*["'](/workbench/assets/[^"']+)["']""", body))
            for spec in MODULE_SPECIFIER.findall(body):
                if spec.startswith('/workbench/assets/'):
                    pending.append(spec)
        self.assertTrue(pending)
        while pending:
            url = pending.pop()
            if url in visited:
                continue
            visited.add(url)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            if url.endswith('.js'):
                source = b''.join(response.streaming_content).decode()
                for spec in MODULE_SPECIFIER.findall(source):
                    if spec.startswith('/'):
                        pending.append(spec)
                    elif spec.startswith('.'):
                        pending.append(posixpath.normpath(posixpath.join(posixpath.dirname(url), spec)))
            response.close()
        self.assertIn('/workbench/assets/components/review-actions.js', visited)
        self.assertIn('/workbench/assets/pages/p06.js', visited)

    @override_settings(DEBUG=True)
    def test_explicit_development_modes_keep_existing_prototype(self):
        for mode in ('demo','fixture'):
            response=self.client.get('/workbench/?mode='+mode)
            self.assertEqual(response.status_code,200)
            self.assertIn('交互原型',b''.join(response.streaming_content).decode())
