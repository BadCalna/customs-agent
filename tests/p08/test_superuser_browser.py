"""Real /workbench/ regression: P08_BROWSER_TESTS=1 P08_BROWSER_CHANNEL=chrome
python manage.py test tests.p08.test_superuser_browser (requires Playwright).
"""
import os
import re
import unittest

from django.test import LiveServerTestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import Confirmation, Task
from tests.p01.factories import PASSWORD, assign, make_item, make_task, make_user, make_version
from tests.p01.test_superuser_access import make_superuser


@unittest.skipUnless(os.environ.get("P08_BROWSER_TESTS") == "1", "opt-in Chromium acceptance: P08_BROWSER_TESTS=1")
@override_settings(DEBUG=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SuperuserWorkbenchBrowserTests(LiveServerTestCase):
    host = "127.0.0.1"

    @classmethod
    def _make_connections_override(cls):
        # Default test SQLite uses mode=memory&cache=shared, so live HTTP
        # threads can use separate connections to the same test database.
        # Sharing one connection races when task detail and history load in parallel.
        overrides = super()._make_connections_override()
        return {
            alias: connection
            for alias, connection in overrides.items()
            if "mode=memory&cache=shared" not in str(connection.settings_dict["NAME"])
        }

    def test_unassigned_superuser_can_browse_all_tasks_but_cannot_edit(self):
        from playwright.sync_api import expect, sync_playwright

        load_timeout = 15_000

        admin = make_superuser()
        operator = make_user("review-operator", Role.OPERATOR)
        supplier_a = make_user("review-supplier-a", Role.SUPPLIER)
        supplier_b = make_user("review-supplier-b", Role.SUPPLIER)
        task_a = make_task(supplier_a, title="可确认任务甲", stage=Task.Stage.AWAITING_CONFIRMATION)
        task_b = make_task(supplier_b, title="未分派任务乙")
        assign(task_a, operator, Role.OPERATOR)
        old_version = make_version(task_a, supplier_a, filename="历史合同.xlsx")
        current_version = make_version(task_a, supplier_a, number=2)
        make_item(old_version, name="历史商品甲")
        current_item = make_item(current_version, name="当前商品甲")
        confirmed_item = make_item(current_version, row=9, name="已确认商品甲")
        Confirmation.objects.create(
            item=confirmed_item, revision=confirmed_item.revision,
            confirmed_hs=confirmed_item.supplier_hs_raw, created_by=operator,
        )
        make_item(make_version(task_b, supplier_b), name="未分派商品乙")
        self.assertFalse(admin.task_assignments.exists())

        url = self.live_server_url + "/workbench/"
        readonly_list = url + "#/readonly/tasks"
        rows = '[data-act="open-task"]'
        writes = (
            '[data-act="confirm-code"], [data-act="live-edit-code"], [data-act="open-submit"], '
            '[data-act="live-open-decision"], [data-act="open-upload"], [data-act="reprecheck"]'
        )

        def login(page, user, route=""):
            page.goto(url + route)
            form = page.locator("#live-login-form")
            form.locator('[name="username"]').fill(user.username)
            form.locator('[name="password"]').fill(PASSWORD)
            with page.expect_response(lambda response: response.url.endswith("/api/v1/auth/login")) as result:
                form.get_by_role("button", name="登录").click()
            self.assertEqual(result.value.status, 200)
            return result.value.json()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get("P08_BROWSER_CHANNEL") or None)
            try:
                page = browser.new_page()
                errors, business_writes, task_reads = [], [], []

                def track_request(request):
                    if request.method == "GET" and "/api/v1/tasks/" in request.url:
                        task_reads.append(request.url)
                    if request.method not in {"GET", "HEAD", "OPTIONS"} and "/api/v1/" in request.url:
                        if "/api/v1/auth/" not in request.url:
                            business_writes.append(request.url)

                page.on("request", track_request)
                page.on("pageerror", lambda error: errors.append(str(error)))
                session = login(page, admin)
                self.assertEqual(session["role"], "")
                self.assertIs(session["is_readonly"], True)
                expect(page).to_have_url(readonly_list)
                expect(page.get_by_role("heading", name="全部审核任务")).to_be_visible(timeout=load_timeout)
                expect(page.get_by_text("超管 · 只读", exact=True)).to_be_visible()
                expect(page.locator(rows)).to_have_count(2)
                expect(page.locator(rows).filter(has_text=task_b.title)).to_be_visible()
                expect(page.locator(writes)).to_have_count(0)
                self.assertEqual(task_reads, [], "list must not load every task's detail and history")

                page.locator(rows).filter(has_text=task_b.title).click()
                try:
                    expect(page.get_by_role("heading", name="未分派商品乙")).to_be_visible(timeout=load_timeout)
                except AssertionError as exc:
                    raise AssertionError(f"detail did not load: {page.locator('body').inner_text()} errors={errors}") from exc
                expect(page.locator(writes)).to_have_count(0)
                page.get_by_role("link", name="返回全部任务").click()
                page.locator(rows).filter(has_text=task_a.title).click()
                expect(page.get_by_role("heading", name=current_item.name)).to_be_visible(timeout=load_timeout)
                pending = page.locator('[data-filter="pending"]')
                item_rows = page.locator("#line-table tbody tr")
                expect(item_rows).to_have_count(2)
                expect(pending).to_have_text("未确认1")
                expect(page.locator(".split-3 > div").filter(has_text="待业务员确认")).to_have_text("待业务员确认1 条")
                pending.click()
                expect(item_rows).to_have_count(1)
                expect(item_rows.first).to_contain_text(current_item.name)
                expect(page.locator(writes)).to_have_count(0)
                page.get_by_role("button", name="版本与问题记录").click()
                dialog = page.get_by_role("dialog", name="版本、提交与退回问题")
                expect(dialog.get_by_text("历史合同.xlsx")).to_be_visible()
                dialog.locator(f'[data-version="{old_version.pk}"]').click()
                expect(page.get_by_role("heading", name="历史商品甲")).to_be_visible(timeout=load_timeout)
                page.reload()
                expect(page).to_have_url(re.compile(r"#/readonly/task/.*\?version=" + str(old_version.pk) + "$"))
                expect(page.get_by_role("heading", name="历史商品甲")).to_be_visible(timeout=load_timeout)
                expect(page.locator(writes)).to_have_count(0)

                page.goto(url + f"#/business/task/{task_a.pk}/{current_item.pk}")
                expect(page).to_have_url(readonly_list)
                expect(page.locator(rows)).to_have_count(2, timeout=load_timeout)
                expect(page.locator(writes)).to_have_count(0)
                page.get_by_role("button", name="退出登录").click()
                expect(page.locator("#live-login-form")).to_be_visible()
                self.assertEqual(business_writes, [])
                self.assertEqual(errors, [])

                # Start at the operator's target route before login so the positive
                # control doesn't trigger a second load while login is hydrating data.
                business = browser.new_page()
                self.assertIs(login(business, operator, "#/business/tasks")["is_readonly"], False)
                expect(business).to_have_url(url + "#/business/tasks")
                expect(business.locator(rows)).to_have_count(1, timeout=load_timeout)
                business.locator(rows).filter(has_text=task_a.title).click()
                expect(business.locator('[data-act="live-edit-code"]')).to_be_visible(timeout=load_timeout)
            finally:
                browser.close()
