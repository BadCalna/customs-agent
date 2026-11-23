"""Original a192cc9 + working-tree LIVE acceptance; NOT a main UI compatibility test.

Run seed_workbench in a disposable CUSTOMS_SQLITE_PATH, start that server, then:
    python tests/workbench/browser_acceptance.py --url http://127.0.0.1:8000 \
        --chromium '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

This script intentionally writes both demo tasks; it refuses already-operated data.
Playwright is an optional local test dependency, not an application dependency.
"""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
APPROVE = 'ccd6ba4c-7180-5865-a208-d41a9754a93c'
RETURN = 'a286802f-6708-50e6-b541-be293e860b05'
A_ITEMS = ['098b1fb7-b5b2-5f0d-96cd-bbf2db9e9807', '23f291c0-5775-543a-bbd8-5af0fc7c9f9a', '2ce31872-4fa6-5b65-a9b6-6ad6055a7376']
R_ITEMS = ['8a226944-e5d7-5df5-b40a-3c84868a680d', 'c4306791-7a32-5451-86ce-863263836334']
R_V1 = '71796930-76d8-561d-9086-3b1ed32e116f'
R_V2 = '2cfab3fd-3aa2-5106-826f-43068ce3a36d'
OLD_ITEM = '790fc96d-c8bb-545d-9f35-67029a2a7a10'
parser = argparse.ArgumentParser()
parser.add_argument('--url', required=True, help='Disposable, freshly seeded loopback server')
parser.add_argument('--chromium')
parser.add_argument('--output', default='.local/workbench-browser')
args = parser.parse_args()
BASE = args.url.rstrip('/')
url = urlsplit(BASE)
if url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1', '::1'} or url.username or url.password or url.path or url.query or url.fragment:
    parser.error('仅允许一次性验收库的本机 HTTP 服务根地址')
if not os.environ.get('WORKBENCH_DEMO_PASSWORD'):
    parser.error('设置临时 WORKBENCH_DEMO_PASSWORD')
OUT = ROOT / args.output
OUT.mkdir(parents=True, exist_ok=True)
checks = []
errors = []


def passed(label):
    checks.append(label)
    print('PASS', label, flush=True)


def ready(page):
    # Hash navigation does not reset Playwright's document load state. Let the
    # hashchange handler start before waiting for the loaded application shell.
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    expect(page.locator('.topbar')).to_be_visible(timeout=15000)
    page.wait_for_load_state('networkidle')
    expect(page.locator('.topbar')).to_be_visible(timeout=15000)


def login(browser, username):
    context = browser.new_context(viewport={'width': 1536, 'height': 1100})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE + '/workbench/')
    page.wait_for_load_state('networkidle')
    page.fill('#live-username', username)
    page.fill('#live-password', os.environ['WORKBENCH_DEMO_PASSWORD'])
    page.get_by_role('button', name='登录', exact=True).click()
    ready(page)
    return context, page


def visit(page, role, task_id, item_id=''):
    page.goto(f'{BASE}/workbench/#/{role}/task/{task_id}' + (f'/{item_id}' if item_id else ''))
    ready(page)


def api(context, path):
    response = context.request.get(BASE + '/api/v1' + path)
    assert response.ok, (response.status, path, response.text())
    return response.json()


def refresh(page):
    page.reload()
    ready(page)


def errors_for(page, pattern, click, draft=None):
    """All error classes must preserve modal input and leave state unchanged."""
    for status in (401, 403, 409, 422, 501):
        def reject(route, request, status=status):
            route.fulfill(status=status, content_type='application/json', body=json.dumps({
                'error': {'code': 'acceptance_injected', 'message': f'验收注入 {status}', 'details': {}},
                'request_id': f'browser-{status}',
            }))
        page.route(pattern, reject)
        click()
        expect(page.locator('#live-modal-error')).to_contain_text(f'HTTP {status}')
        if draft:
            expect(page.locator(draft[0])).to_have_value(draft[1])
        assert not page.locator('#toast').inner_text().endswith('已由服务端保存')
        page.unroute(pattern, reject)
        page.locator('[data-act="close-modal"]').first.click()
        refresh(page)
        # Reopen the same form, preserving only intentional test input.
        yield status


def confirm(page, task_id, item_id, code, action):
    visit(page, 'business', task_id, item_id)
    if action == 'manual':
        page.locator('[data-act="live-edit-code"]').click()
        page.fill('#live-code', code)
        page.fill('#live-code-reason', '验收：手工确认材质用途，使用合成编码')
        page.locator('[data-act="live-confirm-code"]').click()
    else:
        page.locator(f'[data-act="confirm-code"][data-code="{code}"]').click()
    expect(page.locator('#toast')).to_have_text('编码已由服务端保存')
    refresh(page)
    expect(page.locator('[role="tabpanel"]')).to_contain_text(code)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, **({'executable_path': args.chromium} if args.chromium else {}))
    try:
        operator_context, operator = login(browser, 'workbench_operator')
        for task_id in (APPROVE, RETURN):
            detail = api(operator_context, '/tasks/' + task_id)
            assert detail['stage'] == 'awaiting_confirmation' and detail['current_submission_id'] is None, 'Use a fresh isolated seed database'
            initial_items = api(operator_context, f'/versions/{detail["current_version_id"]}/items?limit=100')['items']
            assert initial_items and all(row['business_confirmed_hs'] is None for row in initial_items), 'Use a fresh database with no current-version confirmations'
        passed('真实会话登录与两批初始状态')

        # Changing a hash cannot turn an operator into an expert.
        operator.goto(BASE + '/workbench/#/expert/queue')
        ready(operator)
        expect(operator).to_have_url(BASE + '/workbench/#/business/overview')
        passed('登录角色约束，路由不能冒充专家')

        visit(operator, 'business', APPROVE, A_ITEMS[2])
        expect(operator.locator('[data-act="open-submit"]')).to_be_disabled()
        passed('未逐行确认时禁用整批提交')
        def open_code():
            operator.locator('[data-act="live-edit-code"]').click()
            operator.fill('#live-code', '3926909090')
            operator.fill('#live-code-reason', '失败后保留的手工确认草稿')
        open_code()
        for status in errors_for(operator, '**/confirmation', lambda: operator.locator('[data-act="live-confirm-code"]').click(), ('#live-code-reason', '失败后保留的手工确认草稿')):
            card = api(operator_context, f'/versions/cf8482a2-20b6-56ab-9fe2-b91f90e1da48/items/{A_ITEMS[2]}')
            assert card['business_confirmed_hs']['value'] is None
            open_code()
        operator.locator('[data-act="close-modal"]').first.click()
        passed('逐行确认 401/403/409/422/501 均不报成功并保留草稿')

        # Drop transport before any server mutation.
        open_code()
        operator.route('**/confirmation', lambda route: route.abort('failed'))
        operator.locator('[data-act="live-confirm-code"]').click()
        expect(operator.locator('#live-modal-error')).to_contain_text('结果未确认')
        expect(operator.locator('#live-code')).to_have_value('3926909090')
        operator.unroute('**/confirmation')
        operator.locator('[data-act="close-modal"]').first.click()
        refresh(operator)
        passed('断网失败不报成功且保留手填编码')

        # A nominal HTTP 200 error envelope is not a successful operation.
        open_code()
        operator.route('**/confirmation', lambda route: route.fulfill(status=200, content_type='application/json', body='{"error":{"message":"malformed success"}}'))
        operator.locator('[data-act="live-confirm-code"]').click()
        expect(operator.locator('#live-modal-error')).to_contain_text('结果未确认')
        operator.unroute('**/confirmation')
        operator.locator('[data-act="close-modal"]').first.click()
        refresh(operator)
        passed('HTTP 200 错误体拒绝视为成功')

        confirm(operator, APPROVE, A_ITEMS[0], '8544422900', 'supplier')
        confirm(operator, APPROVE, A_ITEMS[1], '8543709990', 'ai')
        confirm(operator, APPROVE, A_ITEMS[2], '3926909090', 'manual')
        passed('原码/AI 建议/手填三种确认均落库并在刷新后保留')

        # A committed write followed by a failed read must be shown as unverified.
        operator.locator('[data-act="live-edit-code"]').click()
        operator.fill('#live-code', '3926909000')
        operator.fill('#live-code-reason', '验收：写入成功但重读故障')
        operator.route('**/history', lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"unavailable","message":"保存后的重读故障"}}'))
        operator.locator('[data-act="live-confirm-code"]').click()
        expect(operator.locator('#live-modal-error')).to_contain_text('服务端已保存，但页面刷新失败')
        expect(operator.locator('#live-code-reason')).to_have_value('验收：写入成功但重读故障')
        assert not operator.locator('#toast').evaluate('(el) => el.classList.contains("show")')
        card = api(operator_context, f'/versions/cf8482a2-20b6-56ab-9fe2-b91f90e1da48/items/{A_ITEMS[2]}')
        saved_history = api(operator_context, '/tasks/' + APPROVE + '/history')
        saved_confirmation = next(c for c in saved_history['versions'][0]['confirmations'] if c['confirmation_id'] == card['business_confirmed_hs']['source_id'])
        assert saved_confirmation['reason'] == '验收：写入成功但重读故障'
        operator.unroute('**/history')
        operator.locator('[data-act="close-modal"]').first.click()
        refresh(operator)
        passed('服务端已落库但重读失败时提示待核对，不显示成功且保留草稿')
        confirm(operator, APPROVE, A_ITEMS[2], '3926909090', 'manual')
        operator.locator('[data-act="open-submit"]').click()
        for _ in errors_for(operator, '**/submissions', lambda: operator.locator('[data-act="confirm-submit"]').click()):
            assert api(operator_context, '/tasks/' + APPROVE)['current_submission_id'] is None
            operator.locator('[data-act="open-submit"]').click()
        operator.locator('[data-act="close-modal"]').first.click()
        passed('整批提交 401/403/409/422/501 均不报成功、不改变提交状态')

        # Commit the request, then drop its reply. Also try a second click while pending.
        submission_requests = []
        def lose_submission_reply(route):
            submission_requests.append(route.request)
            expect(operator.locator('[data-act="confirm-submit"]')).to_be_disabled()
            operator.locator('[data-act="confirm-submit"]').evaluate('(el) => el.click()')
            response = route.fetch()
            assert response.ok, response.text()
            route.abort('failed')
        operator.route('**/submissions', lose_submission_reply)
        operator.locator('[data-act="open-submit"]').click()
        operator.locator('[data-act="confirm-submit"]').click()
        expect(operator.locator('#live-modal-error')).to_contain_text('结果未确认')
        assert not operator.locator('#toast').evaluate('(el) => el.classList.contains("show")')
        operator.unroute('**/submissions', lose_submission_reply)
        operator.locator('[data-act="close-modal"]').first.click()
        refresh(operator)
        assert len(submission_requests) == 1
        passed('提交中防重复点击，提交落库后响应丢失不报成功，刷新后可核对')
        task_a = api(operator_context, '/tasks/' + APPROVE)
        assert task_a['current_submission_status'] == 'pending'
        history_a = api(operator_context, '/tasks/' + APPROVE + '/history')
        snapshot_a = history_a['versions'][0]['submissions'][0]
        expected_a = dict(zip(A_ITEMS, ['8544422900', '8543709990', '3926909090']))
        assert {i['item_id']: i['submitted_hs'] for i in snapshot_a['items']} == expected_a
        operator.screenshot(path=str(OUT / '01-operator-submitted.png'), full_page=True)
        passed('整批提交的不可变快照与实际三种确认编码一致')

        expert_context, expert = login(browser, 'workbench_expert')
        visit(expert, 'expert', APPROVE, A_ITEMS[2])
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('3926909090')
        expect(expert.locator('[role="tabpanel"]')).to_contain_text(task_a['current_submission_id'])
        expert.screenshot(path=str(OUT / '02-expert-snapshot.png'), full_page=True)
        passed('专家页面读取业务员实际提交编码及提交ID')
        def open_approve():
            expert.locator('[data-act="live-open-decision"][data-mode="approve"]').first.click()
            expert.fill('#live-decision-reason', '专家审核草稿在失败后保留')
        open_approve()
        for _ in errors_for(expert, '**/decisions', lambda: expert.locator('[data-act="live-save-decision"]').click(), ('#live-decision-reason', '专家审核草稿在失败后保留')):
            assert api(expert_context, '/tasks/' + APPROVE)['current_submission_status'] == 'pending'
            open_approve()
        expert.locator('[data-act="close-modal"]').first.click()
        passed('专家通过 401/403/409/422/501 不报成功并保留整批草稿')

        open_approve()
        expert.fill(f'[data-final="{A_ITEMS[2]}"]', '3926909080')
        expert.locator('[data-act="live-save-decision"]').click()
        expect(expert.locator('#toast')).to_contain_text('调整提交编码必须填写改码理由')
        expert.fill(f'[data-change-reason="{A_ITEMS[2]}"]', '合成验收：人工复核后调整最终编码')
        expert.locator('[data-act="live-save-decision"]').click()
        expect(expert.locator('#toast')).to_have_text('专家整批通过已由服务端保存')
        refresh(expert)
        assert api(expert_context, '/tasks/' + APPROVE)['stage'] == 'approved'
        card_a = api(expert_context, f'/versions/cf8482a2-20b6-56ab-9fe2-b91f90e1da48/items/{A_ITEMS[2]}')
        assert card_a['submitted_hs']['value'] == '3926909090'
        assert card_a['expert_final_hs']['value'] == '3926909080'
        approved_snapshot = api(expert_context, '/tasks/' + APPROVE + '/history')['versions'][0]['submissions'][0]
        assert approved_snapshot['status'] == 'approved'
        assert approved_snapshot['items'] == snapshot_a['items']
        expert.screenshot(path=str(OUT / '03-expert-approved.png'), full_page=True)
        passed('专家改码需理由，通过刷新持久化且不改写提交快照')
        expert.locator('[data-act="set-tab"][data-tab="log"]').click()
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('decision.recoded')
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('3926909090 → 3926909080')
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('合成验收：人工复核后调整最终编码')
        expert.locator('[data-act="set-tab"][data-tab="hs"]').click()
        passed('商品操作记录展示真实改码前后值、原因与版本商品标识')

        confirm(operator, RETURN, R_ITEMS[0], '9026900090', 'ai')
        confirm(operator, RETURN, R_ITEMS[1], '8536909000', 'supplier')
        operator.locator('[data-act="open-submit"]').click()
        operator.locator('[data-act="confirm-submit"]').click()
        expect(operator.locator('#toast')).to_have_text('整批提交已由服务端保存')
        visit(expert, 'expert', RETURN, R_ITEMS[0])
        def open_return():
            expert.locator('[data-act="live-open-decision"][data-mode="return"]').first.click()
            expert.select_option('#live-issue-item', R_ITEMS[0])
            expert.select_option('#live-return-to', 'supplier')
            expert.fill('#live-issue-message', '验收：请补充传感器测量介质与用途，仅针对 V2 第5行')
            expert.fill('#live-decision-reason', '合成样例：关键用途待补充')
        open_return()
        for _ in errors_for(expert, '**/decisions', lambda: expert.locator('[data-act="live-save-decision"]').click(), ('#live-issue-message', '验收：请补充传感器测量介质与用途，仅针对 V2 第5行')):
            assert api(expert_context, '/tasks/' + RETURN)['current_submission_status'] == 'pending'
            open_return()
        expert.locator('[data-act="close-modal"]').first.click()
        passed('专家退回 401/403/409/422/501 不报成功并保留问题草稿')
        open_return()
        expert.locator('[data-act="live-save-decision"]').click()
        expect(expert.locator('#toast')).to_have_text('专家退回及问题已由服务端保存')
        refresh(expert)
        history_r = api(expert_context, '/tasks/' + RETURN + '/history')
        issue = next(i for i in history_r['issues'] if i['version_id'] == R_V2)
        assert issue['item_id'] == R_ITEMS[0] and issue['submission_id'] and issue['decision_id']
        assert issue['source_location']['row_number'] == 5
        expert.locator(f'[data-issue-id="{issue["issue_id"]}"] [data-act="live-locate"]').click()
        ready(expert)
        expect(expert).to_have_url(f'{BASE}/workbench/#/expert/task/{RETURN}/{R_ITEMS[0]}?version={R_V2}')
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('9026900090')
        expect(expert.locator('#line-table tbody tr').nth(1)).to_contain_text('整批已退回')
        expect(expert.locator('#line-table')).not_to_contain_text('待审核')
        expert.screenshot(path=str(OUT / '04-return-v2-location.png'), full_page=True)
        passed('退回问题持久化并定位 V2/商品/第5行/提交/裁决')

        expert.locator('[data-act="live-history"]').click()
        old_issue = next(i for i in history_r['issues'] if i['version_id'] == R_V1)
        expert.locator(f'[data-issue-id="{old_issue["issue_id"]}"] [data-act="live-locate"]').click()
        ready(expert)
        expect(expert).to_have_url(f'{BASE}/workbench/#/expert/task/{RETURN}/{OLD_ITEM}?version={R_V1}')
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('9026809000')
        expect(expert.locator('.content')).to_contain_text('此版本只读')
        expect(expert.locator('[data-act="live-open-decision"]')).to_have_count(0)
        refresh(expert)
        expect(expert.locator('[role="tabpanel"]')).to_contain_text('9026809000')
        expert.screenshot(path=str(OUT / '05-old-version-readonly.png'), full_page=True)
        passed('同名旧商品准确回查 V1，刷新保持版本，历史只读')

        supplier_context, supplier = login(browser, 'workbench_supplier')
        expect(supplier.locator('h1')).to_have_text('任务与退回问题')
        expect(supplier.locator('.content')).to_contain_text('当前版本 1 条待供应商处理')
        supplier.get_by_role('link', name='工作台验收-整批退回', exact=False).click()
        ready(supplier)
        expect(supplier.locator('.steps')).to_contain_text('整批已退回')
        passed('供应商首页按真实当前版本问题计数，任务阶段与服务端退回状态一致')
        visit(supplier, 'supplier', RETURN)
        expect(supplier.locator('.content')).to_contain_text('仅针对 V2 第5行')
        passed('供应商可见真实退回问题及版本商品来源')
        supplier.screenshot(path=str(OUT / '06-supplier-return.png'), full_page=True)

        # A child read failure must never silently load fixtures or partial state.
        expert.route('**/history', lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"unavailable","message":"历史读取故障"}}'))
        refresh(expert)
        expect(expert.locator('[role="alert"]')).to_contain_text('历史读取故障')
        expect(expert.locator('.demo-chip')).to_have_text('真实接口 · LIVE')
        assert 'DEMO' not in expert.locator('.demo-chip').inner_text()
        expert.unroute('**/history')
        passed('子请求失败展示真实错误，不回退样例或吞错')
        assert not errors, errors
        passed('浏览器无 JavaScript 未捕获异常')
        (OUT / 'results.json').write_text(json.dumps({'checks': checks, 'page_errors': errors, 'approved_snapshot': approved_snapshot, 'return_issue': issue}, ensure_ascii=False, indent=2) + '\n')
        print(f'{len(checks)} checks passed; evidence: {OUT}', flush=True)
    finally:
        browser.close()
