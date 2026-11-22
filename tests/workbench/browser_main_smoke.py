"""Read-only compatibility smoke for main's production UI; not the S01–S12 sign-off."""
import argparse
import os
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


APPROVE = 'ccd6ba4c-7180-5865-a208-d41a9754a93c'
RETURN = 'a286802f-6708-50e6-b541-be293e860b05'
V1 = '71796930-76d8-561d-9086-3b1ed32e116f'
V2 = '2cfab3fd-3aa2-5106-826f-43068ce3a36d'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--chromium', help='Optional Chromium executable')
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1', '::1'}
            or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment):
        parser.error('Use a loopback HTTP root URL for a disposable server')
    if not os.environ.get('WORKBENCH_DEMO_PASSWORD'):
        parser.error('Set WORKBENCH_DEMO_PASSWORD to the temporary seed password')
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=args.chromium)
        try:
            for role in ('operator', 'expert', 'supplier'):
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.on('pageerror', lambda exc: errors.append(str(exc)))
                    page.goto(args.url)
                    page.wait_for_load_state('networkidle')
                    form = page.locator('#login-form')
                    username = 'workbench_' + role
                    form.locator('[name=username]').fill(username)
                    form.locator('[name=password]').fill(os.environ['WORKBENCH_DEMO_PASSWORD'])
                    form.get_by_role('button').click()
                    expect(page.locator('[data-task]')).to_have_count(2)
                    me_response = context.request.get(args.url.rstrip('/') + '/api/v1/me')
                    assert me_response.ok and me_response.json()['role'] == role
                    for task, count in ((APPROVE, 3), (RETURN, 2)):
                        page.locator(f'[data-task="{task}"]').click()
                        detail = page.locator('#task-detail')
                        expect(detail).to_contain_text(task)
                        expect(detail).to_contain_text('当前结果为模拟数据，不能用于正式审核')
                        rows = page.locator('#item-list tr[data-item]')
                        expect(rows).to_have_count(count)
                        expect(detail.locator('[data-action="submit"], #decision-form')).to_have_count(0)
                        if task == RETURN:
                            for version, code in ((V1, '9026809000'), (V2, '9026900090')):
                                detail.locator(f'[data-version="{version}"]').click()
                                expect(rows.first).to_contain_text(code)
                                rows.first.click()
                                expect(page.locator('#item-detail')).to_contain_text(code)
                                expect(page.locator('#confirmation-form')).to_have_count(0)
                    print(f'PASS {role}: real login, both batches, distinct V1/V2, mock write gate')
                finally:
                    context.close()
        finally:
            browser.close()
    assert not errors, errors


if __name__ == '__main__':
    main()
