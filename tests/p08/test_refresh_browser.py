"""Opt-in real browser + live Django host regression (no business API stubs).

P08_BROWSER_TESTS=1 python manage.py test tests.p08.test_refresh_browser
Requires Python Playwright and its Chromium browser. All records are synthetic
and live only in Django's isolated test database.
"""
import os
import time
import unittest

from django.test import LiveServerTestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import DecisionItem, Submission
from tests.p01.factories import PASSWORD, assign, make_item, make_task, make_user, make_version
from tests.p08 import test_expert_host_api as host_api


@unittest.skipUnless(os.environ.get('P08_BROWSER_TESTS') == '1', 'opt-in Chromium acceptance: P08_BROWSER_TESTS=1')
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RefreshBrowserTests(LiveServerTestCase):
    # Share the real CSRF-aware API helpers, not TestCase's atomic transaction.
    login = host_api.ExpertHostDecisionTests.login
    write = host_api.ExpertHostDecisionTests.write

    def setUp(self):
        supplier = make_user('refresh-supplier', Role.SUPPLIER)
        operator = make_user('refresh-operator', Role.OPERATOR)
        self.expert = make_user('refresh-expert', Role.EXPERT)
        self.task = make_task(supplier, stage='awaiting_confirmation')
        assign(self.task, operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, supplier)
        self.items = [make_item(self.version, row=row, name=f'合成刷新商品{row}') for row in (8, 15, 22)]
        op = self.login(operator)
        confirmed = []
        for item in self.items:
            response = self.write(op, f'/api/v1/versions/{self.version.pk}/items/{item.pk}/confirmation',
                                  {'confirmed_hs': '8544421900', 'expected_revision': item.revision}, method='patch')
            self.assertEqual(response.status_code, 200, response.content)
            confirmed.append({'item_id': str(item.pk), 'confirmation_id': response.json()['confirmation_id']})
        self.task.refresh_from_db()
        response = self.write(op, f'/api/v1/tasks/{self.task.pk}/submissions',
                              {'version_id': str(self.version.pk), 'revision': self.task.revision, 'items': confirmed},
                              key='refresh-browser-batch')
        self.assertEqual(response.status_code, 200, response.content)
        self.submission_id = response.json()['submission_id']

    def test_real_refresh_button_keeps_draft_and_persists_recode(self):
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('P08_BROWSER_CHANNEL') or None)
            try:
                page = browser.new_page()
                errors, writes = [], []
                page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' and '/decisions' in request.url else None)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.live_server_url + '/workbench/live/?task=' + str(self.task.pk))
                page.wait_for_load_state('networkidle')
                page.locator('#wb-login input[name=username]').fill(self.expert.username)
                page.locator('#wb-login input[name=password]').fill(PASSWORD)
                page.locator('#wb-login button').click()
                final = page.locator(f'[data-ra-field="final-hs"][data-item="{self.items[1].pk}"]')
                reason = page.locator(f'[data-ra-field="change-reason"][data-item="{self.items[1].pk}"]')
                expect(final).to_be_visible()
                final.fill('8536901900')
                reason.fill('合成真实刷新改码原因')
                page.locator('[data-ra-field="decision-reason"]').fill('刷新后仍应提交草稿')
                page.evaluate('window.originalReview = document.querySelector("#wb-review .ra-root")')

                # Gate browser reads before sending them; once released every read
                # still reaches the real server and uses its unmodified response.
                page.evaluate('''() => {
                    const realFetch = window.fetch;
                    window.refreshReads = [];
                    window.fetch = async (...args) => {
                        const method = args[1]?.method || 'GET';
                        if (method === 'GET' && String(args[0]).includes('/api/v1/')) {
                            await new Promise(resolve => window.refreshReads.push({url: String(args[0]), resolve}));
                        }
                        return realFetch(...args);
                    };
                }''')
                page.get_by_role('button', name='刷新任务详情', exact=True).click()
                seen = []
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    expect(page.locator('#wb-review')).to_be_hidden()
                    self.assertTrue(page.evaluate('window.originalReview === document.querySelector("#wb-review .ra-root")'))
                    page.locator('[data-ra-act="decide"]').dispatch_event('click')
                    page.wait_for_timeout(10)
                    if not page.evaluate('window.refreshReads.length'):
                        continue
                    url = page.evaluate('window.refreshReads[0].url')
                    seen.append(url)
                    with page.expect_response(lambda response: response.url.endswith(url)) as read:
                        page.evaluate('window.refreshReads.shift().resolve()')
                    self.assertEqual(read.value.status, 200)
                    if '/versions/' in url and '/items' in url:
                        break
                else:
                    self.fail(f'refresh response chain timed out: {seen}')
                page.evaluate('''() => {
                    window.refreshReads.forEach(read => read.resolve());
                    window.refreshReads.push = read => {read.resolve(); return 0;};
                }''')
                expect(final).to_be_visible()
                expect(final).to_have_value('8536901900')
                expect(reason).to_have_value('合成真实刷新改码原因')
                self.assertTrue(page.evaluate('window.originalReview === document.querySelector("#wb-review .ra-root")'))
                self.assertTrue(any('/history' in url for url in seen))
                self.assertEqual(writes, [], 'suspended form must not send a decision request')
                with page.expect_response(lambda response: '/decisions' in response.url and response.request.method == 'POST') as pending:
                    page.get_by_role('button', name='提交通过决定', exact=True).click()
                response = pending.value
                self.assertEqual(response.status, 200, response.text())
                body = response.request.post_data_json
                self.assertEqual(body['reason'], '刷新后仍应提交草稿')
                self.assertEqual(next(row for row in body['items'] if row['item_id'] == str(self.items[1].pk)),
                                 {'item_id': str(self.items[1].pk), 'final_hs': '8536901900', 'change_reason': '合成真实刷新改码原因'})
                self.assertEqual(errors, [])
            finally:
                browser.close()
        saved = DecisionItem.objects.get(decision__submission_id=self.submission_id, item=self.items[1])
        self.assertEqual(saved.previous_hs, '8544421900')
        self.assertEqual(saved.final_hs, '8536901900')
        self.assertEqual(saved.change_reason, '合成真实刷新改码原因')
        self.assertEqual(Submission.objects.get(pk=self.submission_id).status, 'approved')
