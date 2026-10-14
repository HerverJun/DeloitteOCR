"""Score frozen Luna decisions after inference; labels never enter the inbox."""
from collections import Counter,defaultdict
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import unicodedata

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from prepare_luna_structure_sim import OUT,AUDIT
from complex_table_run import save,sha
from measure_structure_net_benefit import summarize
from ocr_workbench.structure_arbitration import validate_response


def normalize(text):return ''.join(unicodedata.normalize('NFKC',text).split())


def score(reference,edit):
    predicted=defaultdict(list)
    for ti,table in enumerate(edit.get('tables',[])):
        for c in table['cells']:predicted[(ti,c['row'],c['column'])].append(c)
    refkeys={(c.get('table',0),c['row'],c['column']) for c in reference['targets']}
    rows=[]
    for c in reference['targets']:
        key=(c.get('table',0),c['row'],c['column']);found=predicted.get(key,[])
        structural=len(found)==1 and all(found[0][k]==c[k] for k in ('row_span','column_span'))
        literal=structural and normalize(found[0]['text'])==normalize(c['text'])
        rows.append({'key':list(key),'correct':bool(literal),'structure_correct':bool(structural),
            'tags':c.get('tags',[]),'before_reference_text':c['text'],
            'predicted_text':found[0]['text'] if len(found)==1 else None})
    output_count=sum(len(v) for v in predicted.values())
    extras=sum(len(v) if k not in refkeys else max(0,len(v)-1) for k,v in predicted.items())
    dimensions=True
    tables=set(c.get('table',0) for c in reference['targets'])
    for ti in tables:
        subset=[c for c in reference['targets'] if c.get('table',0)==ti]
        expected=(max(c['row']+c['row_span'] for c in subset),max(c['column']+c['column_span'] for c in subset))
        dimensions &= ti<len(edit.get('tables',[])) and (edit['tables'][ti]['rows'],edit['tables'][ti]['columns'])==expected
    dimensions &= len(edit.get('tables',[]))==len(tables)
    return {'rows':rows,'targets':len(rows),'correct':sum(c['correct'] for c in rows),
        'structure_correct':sum(c['structure_correct'] for c in rows),'predicted_cells':output_count,'extra_outputs':extras,
        'full_table_structure':bool(dimensions and not extras and all(c['structure_correct'] for c in rows)),
        'full_table_exact':bool(dimensions and not extras and all(c['correct'] for c in rows))}


def aggregate(scores):
    totals=Counter()
    for s in scores:totals.update({k:s[k] for k in ('targets','correct','structure_correct','predicted_cells','extra_outputs','full_table_exact','full_table_structure')})
    return {**dict(totals),'tables':len(scores),'coverage':totals['correct']/totals['targets'] if totals['targets'] else None,
        'precision':totals['correct']/totals['predicted_cells'] if totals['predicted_cells'] else None,
        'structural_coverage':totals['structure_correct']/totals['targets'] if totals['targets'] else None}


def main():
    if (AUDIT/'report.json').exists():raise ValueError('Final report exists; preserve this run')
    rows=json.loads((OUT/'prepared.json').read_text('utf-8'))['rows'];byid={r['id']:r for r in rows}
    assignments=json.loads((OUT/'diagnostic-assignments.json').read_text('utf-8'))
    inbox=OUT/'diagnostic-inbox'
    missing=[a['case'] for a in assignments if not (inbox/a['case']/'response.json').exists()]
    if missing:raise ValueError('Inference unfinished: '+str(missing))
    lp=ROOT/'build/tableformer-next-20260915/dataset/sealed/test.annotations.json'
    labels={s['id']:s for s in json.loads(lp.read_text('utf-8'))['samples']}
    before={r['id']:score(labels[r['id']],r.get('before',{'tables':[]})) for r in rows}
    effective_events=[]
    for r in rows:
        assert not r.get('snapshots'), 'Expected preserved zero-trigger product replay'
        for i,c in enumerate(before[r['id']]['rows']):
            effective_events.append({'target_id':r['id']+':'+str(i),'group_id':r['group_id'],
                'before_correct':c['correct'],'after_correct':c['correct'],'triggered':False,'abstained':False})
    cases=[];events=[];structure_events=[];after_scores=[];selected_before=[];response_locks=[]
    for a in assignments:
        folder=inbox/a['case'];request=json.loads((folder/'request.json').read_text('utf-8'))
        assert sha(folder/'request.json')==a['request_sha256'] and sha(folder/'page.png')==a['image_sha256']
        raw=json.loads((folder/'response.json').read_text('utf-8'))
        validation_error=None
        try:response=validate_response(raw,request['structure'])
        except (ValueError,KeyError,TypeError) as e:
            validation_error=str(e);response={'decision':'abstain','candidate_id':None,'reason':'invalid response rejected'}
        row=byid[a['sample_id']];base=before[row['id']];edit=deepcopy(row['before'])
        selected=response['decision']=='select'
        if selected:edit['tables'][a['table_index']]=a['lineage'][response['candidate_id']]['table']
        after=score(labels[row['id']],edit);selected_before.append(base);after_scores.append(after)
        candidate_scores={}
        for cid,c in a['lineage'].items():
            alternative=deepcopy(row['before']);alternative['tables'][a['table_index']]=c['table']
            val=score(labels[row['id']],alternative)
            candidate_scores[cid]={'correct':val['correct'],'structure_correct':val['structure_correct'],'provider':c['provider'],
                'full_table_exact':val['full_table_exact'],'full_table_structure':val['full_table_structure']}
        local_events=[]
        for i,(b,c) in enumerate(zip(base['rows'],after['rows'])):
            assert b['key']==c['key']
            event={'target_id':row['id']+':'+str(i),'group_id':row['group_id'],
                'before_correct':b['correct'],'after_correct':c['correct'],'triggered':True,
                'abstained':not selected,'tags':b['tags']}
            events.append(event);local_events.append(event)
            structure_events.append({**event,'before_correct':b['structure_correct'],'after_correct':c['structure_correct']})
        case={'case':a['case'],'sample_id':row['id'],'response':raw,'validation_error':validation_error,'product_eligible':False,
            'before':{k:v for k,v in base.items() if k!='rows'},'after':{k:v for k,v in after.items() if k!='rows'},
            'net':after['correct']-base['correct'],'structure_net':after['structure_correct']-base['structure_correct'],
            'corrected':sum(not e['before_correct'] and e['after_correct'] for e in local_events),
            'harmed':sum(e['before_correct'] and not e['after_correct'] for e in local_events),
            'candidate_scores':candidate_scores,
            'oracle_net':max([base['correct']]+[v['correct'] for v in candidate_scores.values()])-base['correct']}
        cases.append(case)
        save(AUDIT/'per-case'/(a['case']+'.json'),{**case,'before_cells':base['rows'],'after_cells':after['rows']})
        response_locks.append({'case':a['case'],'response_sha256':sha(folder/'response.json'),'request_sha256':a['request_sha256'],'image_sha256':a['image_sha256']})
    grouped={}
    for tag in ['merged','empty','repeated']:
        subset=[e for e in events if tag in e['tags']]
        grouped[tag]=summarize(subset,[e['target_id'] for e in subset])
    metrics=summarize(events,[e['target_id'] for e in events])
    report={'model':'gpt-5.6-luna','reasoning_effort':'medium','invocation':'one blinded Codex subagent, 12 image cases, persistent batch context',
        'real_model_visual_calls':True,'real_api_endpoint_tested':False,'billed_usage_cost':'not_measured','human_efficiency':'not_measured',
        'product_replay':{'samples':120,'eligible_tables':0,'actual_api_requests':0,'before':aggregate(list(before.values())),
            'effective_paired':summarize(effective_events,[e['target_id'] for e in effective_events])},
        'diagnostic':{'samples':len(cases),'selected':sum(c['response']['decision']=='select' for c in cases),
            'abstained':sum(c['response']['decision']=='abstain' for c in cases),'invalid_responses':sum(bool(c['validation_error']) for c in cases),
            'before':aggregate(selected_before),'after':aggregate(after_scores),'paired':metrics,
            'structure_only_paired':summarize(structure_events,[e['target_id'] for e in structure_events]),'groups':grouped,
            'oracle_net':sum(c['oracle_net'] for c in cases),'cases':cases},
        'metric':'exact indexed row,column,spans and normalized literal text, NOT geometry IoU or prior full-cell localization coverage',
        'limitations':['historical public crops; no fresh independent Chinese financial pages','compressed diagnostic candidate projection differs from production payload',
            'product text-preservation gates block all samples; hypothetical diagnostic gains are not realizable product gains',
            'token ID serialization assisted by tools; no API endpoint, pricing, latency or timeout claim','12 small clustered observations; selection results not used for retuning'],
        'labels_sha256':sha(lp),'response_locks':response_locks,'created_utc':datetime.now(timezone.utc).isoformat()}
    save(AUDIT/'report.json',report)
    save(AUDIT/'paired-events.json',{'scope':'Luna compressed visual diagnostic only; counterfactual gate bypass is never applied to user data',
        'real_service_measured':False,'expected_target_ids':[e['target_id'] for e in events],'events':events})
    d=report['diagnostic'];c=metrics['all']['counts'];interval=metrics['all']['net_rate_95_interval']
    lines=['# GPT-5.6-Luna 多模态结构仲裁模拟 · 2026-09-19','',
        '本轮实际调用指定的 `gpt-5.6-luna` 子 Agent 查看原图并选择候选；这是模型参与的盲测模拟，不是固定答案的协议回放，也不是已配置外部 API 端点的验收。旧 0.13.0rc1 ZIP 未修改。','',
        '## 当前产品链路','',
        '120 张历史公开表格裁剪重放中，121 张采用表均未产生可发送的合法结构候选，API 实际触发数为 0。因此按当前产品硬校验，净增益为 0；这不代表 Luna 看图无效，而是候选／文字保全的上游限制。最常见的是当前采用文字无法映射到 PP-OCR 来源候选，以及未分配 token。随机检查也不能绕过该约束。','',
        '## Luna 视觉选择诊断','',
        '沿用上一轮预先固定的 12 个随机样本 ID，没有按错误或模型答复挑样本。Luna 只看封闭 inbox 中的图像、当前表和来自实际模型输出的候选，未获得参考标签、评分或历史反馈。候选没有从参考答案生成；压缩了来源字段，并对 token ID 作一一映射。网格不合法的候选被排除，候选顺序固定随机化。诊断选择仅在评测副本中计算，不通过产品硬校验的建议不会成为可采用结果。','',
        f"Luna 选择 {d['selected']} 例、弃权 {d['abstained']} 例；响应硬校验失败 {d['invalid_responses']} 例。",'',
        '| 指标（12 张，诊断性假设采用） | 本地当前 | Luna 建议后 |','| --- | ---: | ---: |',
        f"| 目标格 | {d['before']['targets']} | {d['after']['targets']} |",
        f"| 行列／跨度及文字同时正确 | {d['before']['correct']} | {d['after']['correct']} |",
        f"| 正确覆盖率 | {d['before']['coverage']:.2%} | {d['after']['coverage']:.2%} |",
        f"| 仅行列／跨度正确 | {d['before']['structure_correct']} | {d['after']['structure_correct']} |",
        f"| 整表结构与文字全部正确 | {d['before']['full_table_exact']} | {d['after']['full_table_exact']} |",
        f"| 额外输出格 | {d['before']['extra_outputs']} | {d['after']['extra_outputs']} |",'',
        f"纠正 {c['corrected']} 格，改坏 {c['harmed']} 格，净变化 {metrics['all']['net']:+d} 格。按文档组 bootstrap 的净变化率 95% 区间：[{interval[0]:.2%}, {interval[1]:.2%}]。所有目标格、未改变的正确格与弃权均计入，分组见机器报告。该指标要求固定行列和跨度及文字相符，不是上一轮的几何 IoU 定位覆盖率。",'',
        '| 案例 | 决策 | 纠正 | 改坏 | 净变化 |','| --- | --- | ---: | ---: | ---: |']
    lines += [f"| {x['sample_id']} | {x['response']['decision']} | {x['corrected']} | {x['harmed']} | {x['net']:+d} |" for x in cases]
    lines += ['', '## 结论边界','',
        '模型真实参与了看图判断；当前产品可兑现的增益仍为 0，因为上游候选未通过文字保全。不能把诊断中假设采用候选的收益等同于真实 API 净收益。真实接口费用、token 用量、网络延迟、90 秒超时和人工节省时间没有测量；子 Agent 可借助工具复制 token ID，且 12 例共享上下文，这与每表一次 API 请求不同。样本均为已曝光的公开英文科学表格裁剪，不能外推到中文财务整页、扫描或照片。', '',
        '证据：`audit/luna-structure-sim-20260919-01/report.json`、`per-case/`、`protocol.json`、`diagnostic-protocol.json`；逐例原图、冻结请求及原始响应：`build/luna-structure-sim-20260919-01/diagnostic-inbox/`。', '']
    (ROOT/'docs/luna-structure-simulation-20260919.md').write_text('\n'.join(lines),'utf-8')
    print(json.dumps({'diagnostic':{k:d[k] for k in ('samples','selected','abstained','invalid_responses','before','after')},'paired':metrics['all'],'product_net':0},ensure_ascii=False))


if __name__=='__main__':main()
