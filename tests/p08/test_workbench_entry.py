"""Formal entry and public static allowlist remain available with DEBUG off; fixtures do not."""
import posixpath
import re
from django.test import SimpleTestCase, override_settings
from apps.api.demo import WEB_ROOT, WORKBENCH_ASSETS

MODULE_SPECIFIER = re.compile(r"""(?:from|import)\s*\(?\s*["']([^"']+)["']""")


class WorkbenchEntryTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    def test_live_entry_and_only_allowlisted_resources(self):
        response=self.client.get('/workbench/')
        self.assertEqual(response.status_code,200)
        html=b''.join(response.streaming_content).decode()
        self.assertIn('/workbench/assets/api.js',html)
        self.assertNotIn('customs-demo-only',html)
        center=self.client.get('/workbench/live/')
        self.assertEqual(center.status_code,200)
        center_html=b''.join(center.streaming_content).decode()
        self.assertIn('/workbench/assets/workbench-live.js',center_html)
        self.assertNotIn('customs-demo-only',center_html)
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
        for entry in ('/workbench/', '/workbench/live/', '/uploads/'):
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

    def test_legacy_workbench_query_deep_links_redirect_to_live_center_with_allowlisted_params(self):
        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                both = self.client.get('/workbench/?task=T1&version=V1')
                self.assertEqual(both.status_code, 302)
                self.assertEqual(both['Location'], '/workbench/live/?task=T1&version=V1')

                filtered = self.client.get('/workbench/?task=T1&evil=1&next=https://evil.example')
                self.assertEqual(filtered.status_code, 302)
                self.assertEqual(filtered['Location'], '/workbench/live/?task=T1')

                version_only = self.client.get('/workbench/?version=V1')
                self.assertEqual(version_only.status_code, 302)
                self.assertEqual(version_only['Location'], '/workbench/live/?version=V1')

                plain = self.client.get('/workbench/?unrelated=1')
                self.assertEqual(plain.status_code, 200)
                plain.close()

                fixture = self.client.get('/workbench/?mode=fixture&task=T1')
                self.assertEqual(fixture.status_code, 200 if debug else 404)
                if debug:
                    fixture.close()

    @override_settings(DEBUG=False)
    def test_run_and_export_links_target_workbench_live_center(self):
        uploads_resp = self.client.get('/uploads/')
        self.assertEqual(uploads_resp.status_code, 200)
        p06_html = b''.join(uploads_resp.streaming_content).decode()
        uploads_resp.close()
        self.assertIn('<a href="/workbench/live/">运行与导出</a>', p06_html)
        self.assertNotIn('<a href="/workbench/">运行与导出</a>', p06_html)

        production_html = (WEB_ROOT / 'production.html').read_text(encoding='utf-8')
        self.assertRegex(
            production_html,
            r'<a\s+href="/workbench/live/">\s*运行与导出',
        )
        self.assertNotRegex(
            production_html,
            r'<a\s+href="/workbench/">\s*运行与导出',
        )

        wb_resp = self.client.get('/workbench/')
        self.assertEqual(wb_resp.status_code, 200)
        wb_html = b''.join(wb_resp.streaming_content).decode()
        wb_resp.close()
        self.assertIn('href="/workbench/live/"', wb_html)
