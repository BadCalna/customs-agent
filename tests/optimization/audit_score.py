"""Independently derive the frozen 20 scenario marks from both devices' retained observations."""
import json
import sys
from pathlib import Path


def score(path):
    doc=json.loads((Path(path)/'measurements.json').read_text()); devices=[doc['desktop'],doc['mobile']]
    if any('incomplete' in d or 'logout' not in d for d in devices):
        raise ValueError('Partial observation: do not publish an extrapolated score')
    def chroma(d, stage):
        return {tuple(d['themes'][v][stage+'_palette'][0][:2]) for v in d['themes']}
    def within(d, part):
        g = d[part+'_geometry']; return max(g['content'],g['body'])-d['viewport'][0]<=1
    def run_matches(d):
        return any(r.get('run')=={'total':8, 'succeeded':7,'failed':1,'queued':0,'running':0} for r in d['responses']) and all(s in d['v2_run'] for s in ['总数 8','完成 7','失败 1'])
    def distribution(d):
        if d['summary_selector']=='#run-detail': return False
        labels={'consistent':'编码一致','inconsistent':'编码不一致','needs_confirmation':'待核对','supplier_missing':'原码缺失','supplier_invalid':'原码异常','none':'暂未有结论'}
        counted=next((r['compare'] for r in d['responses'] if r.get('items')==8),{})
        expected={labels[k]+str(v)+'条' for k,v in counted.items()}; observed={''.join(s.split()) for s in d['v2_distribution']}
        return len(expected)>=2 and expected==observed and '不等于审核通过' in d['v2_summary']
    def versions(d):
        return [d['v2_table_rows'],d['v1_table_rows']]==[8,2] and sorted(r['items'] for r in d['responses'] if 'items' in r)==[2,8] and '总数 2' in d['v1_run'] and '总数 8' in d['v2_run'] and '铝制型材' not in d['v1_items_text']
    def key(d): return d['tab_from_user']=='password' and d['identity'] and not d['js_errors']
    source=json.loads((Path(path)/'source.json').read_text())['source']
    brand={'baseline':'关务审核工作台', 'candidate':'关务预审工作台'}[source]
    tests={
        'A1':lambda d: all(d['login_labels']) and brand in d['login_text'].splitlines() and '登录' in d['login_text'] and not d['login_workspace_visible'],
        'A2':lambda d: '单证预审' in d['login_text'] and any(w in d['login_text'] for w in ['人工审核','人工复核']),
        'A3':lambda d: d['wrong_status']==401 and d['login_error_feedback'] and d['identity'],
        'A4':lambda d: d['logout']['user_visible'] and not d['logout']['workspace_visible'] and not d['logout']['task_visible'],
        'B1':lambda d: len(chroma(d,'login'))>=2,
        'B2':lambda d: bool(d['themes']) and d['theme_after_refresh']==d['theme_choices'][-1],
        'B3':lambda d: bool(d['themes']) and d['theme_after_login']==d['theme_after_refresh'] and d['workspace_palette'][0][:2]==d['themes'][d['theme_after_login']]['login_palette'][0][:2],
        'B4':lambda d: len(chroma(d,'login'))>=3 and len(chroma(d,'workspace'))>=3 and '界面主题' in d.get('theme_picker_snapshot',''),
        'C1':lambda d: '契约演示-主流程' in d['task_detail'] and 'V2' in d['task_detail'] and d['v2_table_rows']==8,
        'C2':lambda d: d['task_counts'].get('search')==1 and d['task_counts'].get('cleared')==3 and '主流程' in d['search_text'],
        'C3':lambda d: d['task_counts'].get('stage')==1 and d['task_counts'].get('stage_reset')==3 and '已通过' in d['stage_text'],
        'C4':lambda d: d['task_counts'].get('none')==0 and d['task_counts'].get('reset')==3 and d['no_results_text'] and d['no_results_text']!='暂无任务',
        'D1':run_matches, 'D2':distribution,
        'D3':lambda d: '申报要素' in d['item_card'] and '行' in d['item_card'] and '模拟' in d['item_card'] and ('不能' in d['item_card'] or '不可' in d['item_card']),
        'D4':versions, 'E3':lambda d: bool(d['item_card'] and d['v1_items_text'] and d['v2_items_text']), 'E4':key}
    points={k: int(all(bool(test(d)) for d in devices))*5 for k,test in tests.items()}
    for i,d in enumerate(devices,1): points[f'E{i}']=int(all(within(d,p) for p in ['login','workspace']))*5
    grouped={k:sum(points[f'{k}{i}'] for i in range(1,5)) for k in 'ABCDE'}
    return {'points':dict(sorted(points.items())), 'dimensions':grouped, 'total':sum(grouped.values())}


if __name__=='__main__': print(json.dumps(score(sys.argv[1]),ensure_ascii=False,indent=2))
