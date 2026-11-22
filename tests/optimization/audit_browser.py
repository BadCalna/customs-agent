"""Collect real rendered DOM, matching server responses and viewport images. No route mocks."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect


def inspect(browser, base, output, device):
    width, height = (1440, 900) if device == 'desktop' else (390, 844)
    mobile = device == 'mobile'
    ctx = browser.new_context(viewport=dict(width=width, height=height), locale='zh-CN', timezone_id='Asia/Shanghai',
                              device_scale_factor=1, is_mobile=mobile, has_touch=mobile, reduced_motion='reduce')
    page = ctx.new_page(); page.set_default_timeout(6500)
    row = {'viewport':[width, height], 'js_errors': [], 'http_errors': [], 'responses': [], 'images': {}, 'themes': {}}
    page.on('pageerror', lambda e: row['js_errors'].append(str(e)))
    def response(r):
        path = urlparse(r.url).path
        if r.status >= 400: row['http_errors'].append([r.status, path])
        if r.status == 200 and re.fullmatch(r'/api/v1/runs/[^/]+', path):
            try:
                j = r.json(); row['responses'].append({'run':j['progress'], 'mode':j['result_mode']})
            except Exception as e: row['responses'].append({'error': str(e)})
        if r.status == 200 and re.fullmatch(r'/api/v1/versions/[^/]+/items', path):
            try:
                from collections import Counter
                j=r.json()['items']; row['responses'].append({'items':len(j),'compare':dict(Counter(i.get('comparison_status') or 'none' for i in j))})
            except Exception as e: row['responses'].append({'error': str(e)})
    page.on('response', response)
    loc=page.locator
    def idle():
        page.wait_for_load_state('networkidle'); page.evaluate('document.fonts.ready')
    def txt(sel): return loc(sel).first.inner_text().strip() if loc(sel).count() and loc(sel).first.is_visible() else ''
    def vis(sel): return bool(loc(sel).count() and loc(sel).first.is_visible())
    def shot(name):
        f=output/f'{device}-{name}.png'; page.screenshot(path=f, animations='disabled'); row['images'][f.name]=hashlib.sha256(f.read_bytes()).hexdigest()
    def scroll(sel=None):
        if sel: loc(sel).first.evaluate("el=>el.scrollIntoView({block:'start',behavior:'instant'})")
        else: page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
    def geom(): return page.evaluate('''() => ({viewport:innerWidth, root:document.documentElement.clientWidth, content:document.documentElement.scrollWidth, body:document.body.scrollWidth})''')
    def palette(): return page.evaluate('''()=>[document.documentElement,document.body,document.querySelector('main'),document.querySelector('button.primary,button[type=submit]')].filter(Boolean).map(el=>[getComputedStyle(el).backgroundColor,getComputedStyle(el).color,getComputedStyle(el).backgroundImage])''')
    def act(el): (el.tap if mobile else el.click)()
    try:
        row['direct_login_status']=ctx.request.get(base+'/login/').status
        page.goto(base+('/login/' if os.environ.get('AUDIT_LOGIN_ONLY')=='1' else '/')); idle(); expect(loc('[name=username]')).to_be_visible()
        row['login_path']=urlparse(page.url).path; row['login_text']=txt('body'); row['login_geometry']=geom(); row['login_labels']=[loc('[name='+n+']').evaluate('e=>Array.from(e.labels||[]).map(l=>l.textContent.trim())') for n in ('username','password')]
        row['login_workspace_visible']=vis('#workspace'); shot('login')
        picker=loc('[data-theme-picker]:visible').first
        if picker.count():
            options=picker.locator('option').evaluate_all('os=>os.map(o=>o.value)'); row['theme_choices']=options
            row['theme_picker_snapshot']=picker.aria_snapshot()
            for v in options:
                picker.select_option(v); idle(); row['themes'][v]={'login_palette':palette()};
                if not mobile: shot('login-'+re.sub('[^a-zA-Z0-9_-]','_',v))
            page.reload(); idle(); row['theme_after_refresh']=picker.input_value()
        user=loc('[name=username]'); pw=loc('[name=password]')
        user.fill('operator_a'); user.press('Tab'); row['tab_from_user']=page.evaluate('document.activeElement.name'); pw.fill('intentionally-incorrect')
        with page.expect_response(lambda r: r.url.endswith('/auth/login') and r.request.method=='POST') as wrong:
            pw.press('Enter')
        row['wrong_status']=wrong.value.status; idle()
        row['login_error_feedback']=txt('[role=alert]:visible') or txt('#notice') or txt('#login-feedback'); row['wrong_password_text']=txt('body')
        if os.environ.get('AUDIT_LOGIN_ONLY')=='1': return row
        user.fill('operator_a'); pw.fill(os.environ['AUDIT_DEMO_PASSWORD']); pw.press('Enter'); expect(loc('#workspace')).to_be_visible(); idle();
        row['workspace_path']=urlparse(page.url).path; row['identity']=txt('#identity'); row['workspace_palette']=palette()
        if picker.count():
            row['theme_after_login']=picker.input_value()
            for v in row['theme_choices']:
                picker.select_option(v); idle(); row['themes'][v]['workspace_palette']=palette()
            picker.select_option(row['theme_choices'][0]); idle()
        tasks = loc('#task-list [data-task]'); initial=tasks.count(); row['task_counts']={'initial':initial}
        search=loc('#task-search'); reset=loc('#task-reset'); stage=loc('#task-stage')
        if search.count():
            search.fill('主流程'); idle(); row['task_counts']['search']=tasks.count(); row['search_text']=txt('#task-list'); search.fill(''); idle(); row['task_counts']['cleared']=tasks.count(); search.fill('audit-never-matches'); idle(); row['no_results_text']=txt('#task-list'); row['task_counts']['none']=tasks.count(); act(reset); idle(); row['task_counts']['reset']=tasks.count()
        if stage.count():
            stage.select_option('approved'); expect(loc('#task-list')).to_have_attribute('aria-busy','false'); idle(); row['stage_text']=txt('#task-list'); row['task_counts']['stage']=tasks.count(); act(reset); expect(loc('#task-list')).to_have_attribute('aria-busy','false'); idle(); row['task_counts']['stage_reset']=tasks.count()
        main=tasks.filter(has_text='契约演示-主流程').first; act(main); expect(loc('#run-detail')).to_be_visible(); idle();
        row['task_detail']=txt('#task-detail'); row['v2_run']=txt('#run-detail'); row['v2_items_text']=txt('#item-list'); row['v2_table_rows']=loc('#item-list tbody tr').count(); row['workspace_geometry']=geom()
        if mobile: shot('selected-position')
        scroll(); shot('workspace'); summary='#review-insights' if vis('#review-insights') else '#run-detail'; row['summary_selector']=summary; row['v2_summary']=txt(summary); scroll(summary); shot('data')
        if picker.count() and not mobile:
            for v in row['theme_choices'][1:]:
                picker.select_option(v); idle(); scroll(); shot('workspace-'+re.sub('[^a-zA-Z0-9_-]','_',v));
            picker.select_option(row['theme_choices'][0]); idle()
        row['v2_distribution']=loc('#review-insights .ri-distribution li').all_inner_texts(); row['v2_coverage']=loc('#review-insights .ri-coverage li').all_inner_texts(); row['v2_total']=txt('#review-insights .ri-total')
        item=loc('#review-insights .ri-follow [data-item]').first if vis('#review-insights .ri-follow [data-item]') else (loc('#run-detail [data-item]').first if vis('#run-detail [data-item]') else loc('#item-list [data-item]').first)
        js=loc('#run-detail details');
        if not item.is_visible() and js.count(): act(js.first.locator('summary'))
        act(item); expect(loc('#item-detail')).to_be_visible(); idle(); row['item_card']=txt('#item-detail'); scroll('#item-detail'); shot('item')
        act(loc('[data-version]').filter(has_text='V1').first); expect(loc('#run-detail')).to_be_visible(); idle(); row['v1_run']=txt('#run-detail'); row['v1_summary']=txt(summary); row['v1_items_text']=txt('#item-list'); row['v1_table_rows']=loc('#item-list tbody tr').count();
        row['v1_distribution']=loc('#review-insights .ri-distribution li').all_inner_texts(); row['v1_coverage']=loc('#review-insights .ri-coverage li').all_inner_texts(); row['v1_total']=txt('#review-insights .ri-total')
        act(loc('#logout')); expect(user).to_be_visible(); idle(); page.reload(); idle(); row['logout']={'user_visible':user.is_visible(), 'workspace_visible':vis('#workspace'), 'task_visible':vis('#task-list [data-task]'), 'path':urlparse(page.url).path}
    except Exception as exc:
        row['incomplete']=str(exc).replace(os.environ['AUDIT_DEMO_PASSWORD'], '[temporary password redacted]'); row['last_text']=txt('body'); shot('failure')
    finally: ctx.close()
    return row


def main():
    base, out, channel=sys.argv[1:]; out=Path(out)
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True, **({'channel':channel} if channel!='chromium' else {})); result={'browser':b.version}
        for device in ['desktop','mobile']:
            result[device]=inspect(b, base, out, device); (out/'measurements.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n'); print(device, 'complete' if 'incomplete' not in result[device] else 'incomplete: '+result[device]['incomplete'])
        b.close()
    if any('incomplete' in result[v] for v in ['desktop','mobile']): raise SystemExit(1)


if __name__=='__main__': main()
