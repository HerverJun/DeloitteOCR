"""Freeze the native repair pilot's combined evidence without rewriting old runs."""
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from native_repair_pilot import ROOT, read, save, sha

RUNS=['structure-repair-native-20260919-01','structure-repair-native-20260919-02']
AUDIT=ROOT/'audit'/RUNS[0]
BUILD=ROOT/'build'/RUNS[0]


def main():
    scores=[read(ROOT/'audit'/run/'reference-scores.json') for run in RUNS]
    generations=[read(ROOT/'audit'/run/'patch-generation.json') for run in RUNS]
    assert len({g['source_sha256'] for g in generations})==1
    assert generations[0]['source_sha256']==sha(ROOT/'src/ocr_workbench/structure_repair.py')
    records=[dict(r,run=run) for run,score in zip(RUNS,scores) for r in score['records']]
    safe=[r for r in records if r['selected'] and r['selected']['safe_correct_repair']]
    summary={'tables':len(records),'documents':len({r['document'] for r in records}),
        'template_groups':len({r['template'] for r in records}),
        'selected_fully_verified_repairs':len(safe),'verified_documents':len({r['document'] for r in safe}),
        'corrected_header_cells':sum(r['selected']['corrected'] for r in records),
        'harmed_qualified_header_cells':sum(r['selected']['harmed'] for r in records),
        'unqualified_changed_cells':sum(r['selected']['unqualified'] for r in records),
        'literal_changes':sum(r['selected']['literal_changes'] for r in records),
        'reference_cells':sum(r['before']['targets'] for r in records),
        'before_joint_correct':sum(r['before']['with_header_correct'] for r in records),
        'after_joint_correct':sum(r['selected']['after']['with_header_correct'] for r in records),
        'candidate_patches':sum(g['candidate_patches'] for g in generations),
        'frozen_algorithm_sha256':generations[0]['source_sha256'],
        'max_generation_ms':max(g['max_milliseconds'] for g in generations),
        'scope':'development/regression; header metadata only; independent image-only Astra model reference, not objective truth or independent confirmation',
        'continuation_gate_met':len(safe)>=5 and len({r['document'] for r in safe})>=3 and all(r['selected']['literal_changes']==0 for r in records),
        'extension_selection':'three next unused pages from bounded discovery, after first batch scoring; same frozen algorithm; not holdout',
        'records':records}
    save(AUDIT/'combined-local-results.json',summary)
    for run in RUNS:
        folder=ROOT/'audit'/run
        for lock in read(folder/'reference-response-lock.json'):
            assert sha(ROOT/'build'/run/'reference-inbox'/lock['case_id']/'response.json')==lock['sha256']
        assert sha(ROOT/'build'/run/'patches.json')==read(folder/'patch-generation.json')['candidates_sha256']
    replay_paths=[ROOT/'build'/RUNS[0]/'product-replay-06/rows.json',ROOT/'build'/RUNS[1]/'product-replay-02/rows.json']
    replays=[r for path in replay_paths for r in read(path)]
    gates=[dict(t,page=r['page_id']) for r in replays for t in r['tables']]
    sizes=[v['characters'] for t in gates for v in t['retained_candidate_characters']]
    gate={'tables':len(gates),'applicable_proposals_before_filter':sum(t['applicable_proposals'] for t in gates),
        'eligible_snapshots':sum(t['snapshot'] is not None for t in gates),
        'luna_simulation_calls':0,'real_service_calls':0,'luna_net_gain':'not_measured',
        'single_retained_candidate_chars_min':min(sizes),'single_retained_candidate_chars_max':max(sizes),
        'max_chars':40000,'limit_unchanged':True,'evidence_truncated':False,
        'selection':'exact bounded header variants; fewest affected identities then variant name; reject duplicates and lower ranks using ordinary per-proposal decisions; no labels',
        'roundtrips':sum(len(r['roundtrips']) for r in replays),
        'source_integrity':all(r['source_unchanged'] for r in replays),
        'failures':[{'page':t['page'],'table':t['table'],'reason':t['reason'],'retained_candidate_sizes':t['retained_candidate_characters']} for t in gates if t['reason']],
        'local_adoption_scope':'isolated research Stores only; injected experimental header alternatives; not default product generator integration',
        'roundtrip_checks':['exact literals','native adoption provenance','immutable original','undo','redo','reopen','XLSX values/header bold/merged ranges'],
        'replays':[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in replay_paths]}
    assert gate['eligible_snapshots']==0 and gate['roundtrips']==9 and gate['source_integrity']
    save(AUDIT/'combined-product-results.json',gate)
    tests=read(AUDIT/'regression/backend-native-complete.json')
    assert tests['passed'] and tests['source_unchanged']
    for filename,digest in tests['source_files'].items():assert sha(ROOT/'src/ocr_workbench'/filename)==digest
    front=json.loads((AUDIT/'regression/frontend-final.json').read_text('utf-8-sig'))
    assert front['test_exit']==front['build_exit']==0
    sources=['src/ocr_workbench/'+name for name in ['structure_repair.py','native_tables.py','table_tool.py','fusion_alignment.py','structure_diagnostics.py']]
    sources+=['tests/'+name for name in ['test_structure_repair.py','test_pdf_table_tools.py','test_structure_workflow.py']]
    sources+=['scripts/'+name for name in ['native_repair_pilot.py','native_repair_product_replay.py','finalize_native_repair.py']]
    source_records=[]
    for name in sources:
        path=ROOT/name;target=BUILD/'final-source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        initial=BUILD/'baseline-source'/name
        baseline_kind='start of native run'
        if not initial.exists():
            initial=BUILD/'pre-region-fix'/path.name;baseline_kind='before region/product-provenance follow-up'
        if not initial.exists():
            initial=ROOT/'build/structure-repair-20260919-01/baseline-source'/name;baseline_kind='earlier exploration snapshot; includes prior changes'
        record={'path':name,'sha256':sha(path),'snapshot':str(target.relative_to(ROOT))}
        if initial.exists():
            record.update(baseline=str(initial.relative_to(ROOT)),baseline_sha256=sha(initial),baseline_scope=baseline_kind)
            diff=''.join(difflib.unified_diff(initial.read_text('utf-8').splitlines(keepends=True),path.read_text('utf-8').splitlines(keepends=True),fromfile=str(initial.relative_to(ROOT)),tofile=name))
            out=AUDIT/'diffs'/(path.name+'.diff');out.parent.mkdir(exist_ok=True);out.write_text(diff,'utf-8')
        source_records.append(record)
    save(AUDIT/'final-source-lock.json',source_records)
    table_lines=[]
    for r in records:
        selected=r['selected']
        table_lines.append(f"| {r['page_id']} / {r['table_id']} | {selected['variant']} | {selected['corrected']} | {selected['unqualified']} | {'通过' if selected['safe_correct_repair'] else '未完全验证'} |")
    report=f'''# 原生 PDF 结构修复探索结果（2026-09-19）

本轮获得 **6 个经独立 Astra 模型参考验证的局部修复，覆盖 3 份文档**。共检查 9 张表，固定本地策略纠正 25 个表头标记；可判定格中改坏 0 个，字面内容变更 0 个。收益限于表头／行标签元数据，不能代表合并、拆分或文字识别已改善。

本轮以 GPT-Astra（`gpt-6-astra`）代替研究人工标注。3 个初始文档任务、1 个扩展页任务均只看图像创建完整参考表，未看到候选或分数；响应及评分输入哈希均已复验。人工审核数为 0，用户无需逐格复核。模型参考并非客观真值。

## 修复与实际行为

- 修复实验补丁中旧图像来源可用于当前图像移动的漏洞。
- 原生 PDF 采用时记录完整字面值、真实来源片段、分隔符、偏移及图像身份；同页不同候选共用稳定来源 ID。
- 修复原生预览区域在复核中丢失的问题：恢复不可变原始表的当前图像证据，从而区分同页相似表；旧图像或普通表不能套用此回退。
- 采用结构建议时，仅在完整文字与来源身份均保留时携带原生片段记录；不通过相似字符串重建来源。
- 表头候选仍是研究原型，通过明确的实验候选注入进行产品重放，尚未接入默认生成器或自动采用。

## 本地效果

来源为香港 2023、2024 预算案及第 86 号审计报告第一章。9 表分布在 7 页、3 文档、2 模板组。固定候选为第一行表头、前两行表头、正文首列行标签；最多影响 8 个已采用格，固定按影响数量、变体名称排序，不按答案挑选。

| 页／表 | 固定首选 | 确认纠正格 | 未能验证的改动格 | 整个补丁 |
| --- | --- | ---: | ---: | --- |
{chr(10).join(table_lines)}

共 {summary['reference_cells']} 个 Astra 参考格，包含文字、槽位／跨度和表头标记的联合符合数从 {summary['before_joint_correct']} 增至 {summary['after_joint_correct']}。其余识别或结构错误仍然存在，不能把“6 个安全局部补丁”解释为“6 张完全正确的表”。有 {summary['unqualified_changed_cells']} 个改动格因原有文字不符或参考不确定未计作成功；“改坏 0”仅适用于可判定格。所有 26 个生成补丁均通过字面值不变检查。

初始 6 表得到 4 个完整通过补丁；按先前发现清单追加 3 页后又得到 2 个，算法哈希相同。合并满足本轮“至少 5 个修复、跨 3 文档、内容保全零违规”的继续门槛。扩展发生在首批评分之后，全部属于开发／回归材料；不算独立确认集，不宣称达到正式通用质量指标。

候选生成观测最慢 {summary['max_generation_ms']:.1f} ms，已超过 200 ms；这不是受控性能测试，也不声明性能达标。

## 产品链路与 Luna

使用真实 `Documents` 导入、原生渲染／提取、预览、候选存储与复核。9 表共有 **{gate['applicable_proposals_before_filter']} 个可采用建议**（含整表与单格、重复变体，不是 171 个独立正确修复）。按固定策略保留精确有界候选后，完整候选仍为 **{min(sizes):,}～{max(sizes):,} 字符**，超过现有 **40,000** 字符上限，故 9 表全部无法创建外部发送快照。

没有提高上限、裁掉证据、缩短来源 ID 或绕过校验。**本轮 Luna 调用 0，额外净收益未测；真实 API 费用／延迟未测。** 这次阻塞已从缺少来源和多表身份歧义推进到“完整证据序列化太大”，并非 Astra 审核缺位，也不是数据集损坏。

在隔离研究项目中，9 个固定首选补丁均完成实际采用、撤销、重做、项目重开和 XLSX 导出检查，原文、原生来源记录、表头粗体及合并范围均保持预期。保留所有失败重放及拒绝决策，未修改用户工作项目。

## 回归与交付边界

- 后端发现 {tests['tests']} 项：{tests['tests']-len(tests['skipped'])} 项通过，{len(tests['skipped'])} 项因独立 GriTS 环境条件跳过；无失败。执行入口显式以当前 `src` 为首位，并验证运行期间源码哈希未变。
- 前端 51 项通过，生产构建成功；保留现有大 chunk 提示。未改前端源码，本轮未重跑浏览器交互。
- 9 张真实表的采用／历史／导出重放通过；公共 PDF 输入及冻结的 Astra 响应哈希复验通过。
- 先前一次直接 `-m unittest` 误用了嵌入运行时的已打包模块；其失败不作为当前源码结果，随后以显式导入当前源码的测试和最终全量结果为准。
- 本轮修改工作区源码并保留最终源码快照、差异和证据清单；既有交付 ZIP 未升级，未启用默认修复或自动采用。

## 下一项可执行工作

对外部仲裁设计版本化的**无损证据编码**：内部仍保留完整候选快照；文字池、来源记录和几何按不可变 ID 只发送一次，候选只引用相应记录。先验证解码后与原快照逐字段一致，保持图像／修订／候选／文字 ID 硬校验和当前预算；再冻结新协议重放这 9 张表，只有合法输入才交给独立 Luna。任何候选证据不全都继续拒绝，不以提高发送上限替代修复。

编码工作应单独记录协议版本、完整快照哈希、线协议哈希与响应映射，覆盖重复金额、空白、人工修改、过期图片、乱序引用及截断响应。通过后在同一合法候选池比较固定本地首选、保持不变及 Luna 选择，再用新的文档／模板作独立确认。当前表头实验不能代替真正合并／拆分收益验证。

证据入口：`audit/structure-repair-native-20260919-01/combined-local-results.json`、`combined-product-results.json`、`final-source-lock.json`、`regression/`。原始页、完整 Astra 参考和产品重放位于对应 `build/structure-repair-native-20260919-01/` 与 `-02/`。旧实验结果保留原样。
'''
    path=ROOT/'docs/native-structure-repair-results-20260919.md';path.write_text(report,'utf-8')
    progress=read(AUDIT/'progress.json');progress.update(stage='bounded_native_pilot_complete_with_gaps',completed=datetime.now(timezone.utc).isoformat(),
        continuation_gate_met=True,local_verified_repairs=6,product_roundtrips=9,luna_calls=0,
        blocker='unchanged product evidence character limit',report=str(path.relative_to(ROOT)))
    save(AUDIT/'progress.json',progress)
    (AUDIT/'NEXT.md').write_text('Current pilot complete: 6/9 Astra-reference-verified header repairs across 3 documents, 25 corrected metadata cells, zero literal changes. All 9 local adoption/history/XLSX roundtrips passed. Luna not called: even one complete candidate exceeds unchanged 40,000-character limit. Next: versioned lossless shared-evidence encoding with exact reconstruction and existing hard checks, then same-input Luna comparison and new-template independent confirmation. Do not claim merge/split gains, formal accuracy or a refreshed ZIP.\n','utf-8')
    artifacts=[]
    for run in RUNS:
        for base in (ROOT/'audit'/run,ROOT/'build'/run):
            for item in sorted(base.rglob('*')):
                if not item.is_file() or item.name=='artifact-index.json' or item.suffix in ('.pyc','.sqlite3-shm','.sqlite3-wal'):continue
                artifacts.append({'path':str(item.relative_to(ROOT)),'bytes':item.stat().st_size,'sha256':sha(item)})
    artifacts.append({'path':str(path.relative_to(ROOT)),'bytes':path.stat().st_size,'sha256':sha(path)})
    save(AUDIT/'artifact-index.json',{'scope':'two native runs and final report; final-source snapshots capture intended current source; excludes index itself and transient caches','files':artifacts})
    print(json.dumps({'local':{k:v for k,v in summary.items() if k!='records'},'product':{k:v for k,v in gate.items() if k!='failures'},'indexed_files':len(artifacts)},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
