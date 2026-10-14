"""Reconcile corrected replay evidence without changing the blinded model outputs."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'src')]
from complex_table_run import save,sha


def main():
    audit=ROOT/'audit/luna-structure-sim-20260919-01';build=ROOT/'build/luna-structure-sim-20260919-01'
    corrected=ROOT/'build/luna-structure-sim-20260919-03';ca=ROOT/'audit/luna-structure-sim-20260919-03'
    p=json.loads((ca/'preparation.json').read_text('utf-8'))
    assert p['samples']==120 and p['before_available']==120 and p['eligible_samples']==0 and not p['preparation_errors']
    first=json.loads((build/'prepared.json').read_text('utf-8'))['rows']
    final=json.loads((corrected/'prepared.json').read_text('utf-8'))['rows']
    assert all(a['id']==b['id'] and a['before']==b['before'] for a,b in zip(first,final))
    original_inputs=[]
    for r in final:
        image=(ROOT/'build/tableformer-next-20260915/dataset/inputs'/r['sample']['image']).resolve()
        assert sha(image)==r['sample']['sha256'];original_inputs.append({'path':image.relative_to(ROOT).as_posix(),'sha256':sha(image)})
    report=json.loads((audit/'report.json').read_text('utf-8'))
    if (audit/'report.initial.json').exists():raise ValueError('Already reconciled')
    shutil.copy2(audit/'report.json',audit/'report.initial.json')
    from ocr_workbench.store import Store
    store=Store(build/'workspace');provider_map={}
    with store.transaction() as db:
        for row in db.execute('SELECT id,payload FROM structure_candidates'):
            pred=json.loads(row['payload'])['prediction']
            provider_map[row['id']]='tableformer-raw' if 'tf_table_cells' in pred else pred.get('component','unknown')
    assignments=json.loads((build/'diagnostic-assignments.json').read_text('utf-8'))
    for case,a in zip(report['diagnostic']['cases'],assignments):
        assert case['case']==a['case']
        for cid,score in case['candidate_scores'].items():
            score['recorded_provider_before_harness_fix']=score['provider']
            score['provider']=provider_map[a['lineage'][cid]['candidate_set_id']]
    conflicts={}
    from collections import Counter
    conflicts=dict(sum((Counter(r.get('conflict_counts',{})) for r in final),Counter()))
    report['product_replay'].update(reconciled_run='luna-structure-sim-20260919-03',
        source_route_fixed=True,source_route='separate historical TableFormer and Paddle candidate provider keys',
        proposal_conflict_counts=conflicts,preparation_sha256=sha(ca/'preparation.json'))
    report['simulation_receipt']={'configured_model':'gpt-5.6-luna','configured_reasoning_effort':'medium',
        'agent_task':'/root/luna_visual_sim','fork_turns':'none','image_cases':12,
        'model_summary':json.loads((build/'diagnostic-inbox/batch-summary.json').read_text('utf-8')),
        'model_summary_sha256':sha(build/'diagnostic-inbox/batch-summary.json'),
        'token_namespace':'short token IDs are bijective within each candidate, not a shared global namespace across candidates',
        'tools_assisted_serialization':True,'official_invocation_guidance':'https://learn.chatgpt.com/docs/agent-configuration/subagents',
        'no_model_rerun_or_feedback':True}
    report['harness_corrections']=[
        'Initial historical TableFormer omitted component, causing provider collision; run 03 separates source keys and again finds zero eligible samples.',
        'Import helper consumes temporary files. Original 120 images were recovered byte-for-byte from isolated original copies; subsequent imports use copies, and all original SHA256 hashes were rechecked.',
        'Run 02 failed input preparation during discovery of consumed source inputs; retained as failed harness evidence, not model evidence.',
        'Initial system-Python scoring lacked pillow_heif; scoring ran successfully using the packaged service runtime.']
    report['limitations'].append('Candidate-local short token namespaces and largest-table selection are diagnostic projections, not faithful production evidence payloads.')
    save(audit/'report.json',report)
    save(audit/'reconciliation.json',{'passed':True,'original_inputs':original_inputs,
        'corrected_replay':str(ca/'preparation.json'),'initial_report_sha256':sha(audit/'report.initial.json'),
        'model_responses_unchanged':all(sha(build/'diagnostic-inbox'/x['case']/'response.json')==x['response_sha256'] for x in report['response_locks']),
        'source_files_unchanged_since_release_tests':all(sha(ROOT/k)==v for k,v in json.loads((ROOT/'audit/complex-tables-20260919-01/regression/full-final-01.json').read_text('utf-8'))['source_files'].items())})
    doc=ROOT/'docs/luna-structure-simulation-20260919.md'
    content=doc.read_text('utf-8')
    content=content.replace('## 结论边界', '''## 主要得失

- `PMC6048067_table_0`：纠正 52 格，没有新增错误，复杂合并表头有收益。
- `PMC5925862_table_1`：纠正 40 格、改坏 2 格，净改善 38 格。
- `PMC5042378_table_0`：选择把表题和脚注纳入网格的候选，按照参考表区域产生行错位，改坏 81 格。Luna 的原始理由称合并和归属吻合，但逐格计分不支持这个判断。
- 已经正确的两张整表在假设采用后不再全部正确；例如既有 `10` 被候选的 `00` 替换。候选本身包含 OCR 差异，视觉结构选择不会自动保证文字保真。
- 仅行列／跨度纠正 26 格、改坏 6 格，净改善 20 格；文字与结构同时正确的净变化为 −5 格。不能只用结构指标宣称总收益。

## 复核与可复现性

最终产品门控结论使用 `luna-structure-sim-20260919-03`。历史 TableFormer 文件缺少提供方名称，初版重放存在提供方归类冲突；修正后两来源分别录入，120 个输入均成功准备，仍为 0 个合法发送候选。Luna 已查看的 12 份候选数据和原始回答没有修改或重跑。

重放导入接口会消费临时文件，初版误传了源路径；120 张原始图片已从隔离工作区保存的原始副本逐一恢复，并与历史 SHA256 全量核对一致。修正版只导入临时副本。失败尝试、恢复和来源修正记录均保留；旧交付 ZIP 与产品源码没有变更。

短 token ID 在各候选内一一映射，而非跨候选统一命名空间；模型可用工具复制完整 ID 列表。这些简化只用于观察看图选择能力，不能代替正式产品契约、长度预算和独立调用验证。

## 结论边界''')
    doc.write_text(content,'utf-8')
    save(audit/'artifact-index.json',{'files':[{'path':f.relative_to(ROOT).as_posix(),'bytes':f.stat().st_size,'sha256':sha(f)}
        for f in sorted([p for p in audit.rglob('*') if p.is_file() and p.name!='artifact-index.json']+
                       [p for p in (build/'diagnostic-inbox').rglob('*') if p.is_file()]+[doc]+
                       [ROOT/'scripts'/n for n in ['prepare_luna_structure_sim.py','prepare_luna_diagnostic.py','score_luna_structure_sim.py','finalize_luna_simulation.py']])]})
    print(json.dumps({'reconciled':True,'product_triggered':0,'diagnostic_net':report['diagnostic']['paired']['all']['net'],
        'inputs_preserved':len(original_inputs),'report':str(doc)},ensure_ascii=False))


if __name__=='__main__':main()
