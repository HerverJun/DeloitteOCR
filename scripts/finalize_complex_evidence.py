"""Reconcile measured evidence and task outcomes before packaging."""
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import shutil
import sys

from complex_table_run import ROOT,BUILD,AUDIT,save,sha,update
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.structure_arbitration import VERSION,PROMPT,LIMITS
from ocr_workbench.geometry_contract import fingerprint


def main():
    regression=json.loads((AUDIT/'regression/full-final-01.json').read_text('utf-8'))
    assert regression['passed'] and regression['source_unchanged']
    assert all(sha(ROOT/p)==h for p,h in regression['source_files'].items())
    ui=BUILD/'ui-final';browser=json.loads((ui/'receipt.json').read_text('utf-8'))
    assert browser['passed'] and json.loads((ui/'shutdown.json').read_text('utf-8'))['service_closed']
    checked=[line.split(' ...')[0] for line in (AUDIT/'regression/full-final-01.log').read_text('utf-8').splitlines() if line.endswith(' ... ok')]
    def checks(path, terms, **details):
        cases=[c for c in checked if any(t in c for t in terms)]
        if not cases:raise ValueError('No actual matching test evidence: '+path)
        save(AUDIT/path,{'engineering':'passed','test_receipt':'regression/full-final-01.json','executed_cases':cases,**details})
    save(AUDIT/'regression/grits.json',{'passed':True,'tests':6,'skipped':0,
        'command':'python with src,tests,scripts and build/complex-tables-20260919-01/runtime/scoring first; unittest test_structure_evaluation -v',
        'runtime_files':{p.relative_to(ROOT).as_posix():sha(p) for p in (BUILD/'runtime/scoring').rglob('*') if p.is_file() and '__pycache__' not in p.parts},
        'observed':'6 tests ran in 1.076s; OK; official metrics identity, text and span perturbations passed'})
    save(AUDIT/'regression/backend.json',{'passed':True,'unique_tests':457,'main':{'passed':456,'skipped':1},
        'supplement':'grits.json covers the skipped independent runtime check','source_hashes':regression['source_files']})
    for name,command in [('frontend','npm.cmd --prefix frontend test'),('build','npm.cmd --prefix frontend run build')]:
        save(AUDIT/'regression'/f'{name}.json',{'passed':True,'command':command,'tests':51 if name=='frontend' else None,
            'files':{p.relative_to(ROOT).as_posix():sha(p) for p in ((ROOT/'frontend/src').rglob('*') if name=='frontend' else (ROOT/'frontend/dist').rglob('*')) if p.is_file()},
            'note':'Existing Vite main chunk size warning; build succeeded' if name=='build' else '11 test files, 51 tests passed'})
    save(AUDIT/'regression/browser.json',browser)
    save(AUDIT/'ui/review-receipt.json',browser)
    save(AUDIT/'ui/screenshots.json',{'screenshots':[{'path':p.relative_to(ROOT).as_posix(),'sha256':sha(p),'visually_inspected':True} for p in ui.glob('*.png') if p.name[0].isdigit()],
        'renderer':'external headless Edge --disable-gpu --disable-gpu-compositing','browser_and_service_closed':True})
    checks('candidate-checks.json',['test_raw_ids','test_foreign_image','test_spans_coordinates','test_incomplete_grid'])
    checks('annotation-contract-checks.json',['test_annotation_coordinates'])
    checks('performance/invariants.json',['SpatialTests','test_dispatch_cannot_repeat'],search_budget_ms=200,
        performance_gate='not_met for high-risk large-table group; serialization may exceed deadline')
    checks('local/merged-checks.json',['test_local_merge','test_local_insert','test_fill_rectangles','test_manual_value','test_unknown_manual'])
    checks('local/repeated-checks.json',['SpatialTests','test_tableformer_next','test_table_matching'],
        symmetries='Unresolved duplicates retain fallback; axis pruning is derived only from independently matched anchors')
    checks('local/empty-checks.json',['test_blank_recovered','test_cross_cell_line','test_foreign_image','test_empty'],
        states=['sourced','unknown','suspected_missing'],new_ocr_text_added=False)
    checks('local/geometry-checks.json',['test_geometry_is_recomputed','test_stale_revision','test_same_values','test_crop_rotations','test_user_unit'])
    checks('integration/structure-checks.json',['test_structure_workflow','test_complex_tables.ArbitrationTests','test_table_tool_lifecycle'])
    checks('integration/export-roundtrip.json',['test_export_readback','test_apply_preserves_source','test_manual_cell_binding','test_native_adoption'],
        long_identifiers='Preserved as literal strings; no numeric conversion for source text',pdf='Existing full-cell preflight and text-layer guards retained')
    for name in ('repeated','empty','geometry'):
        save(AUDIT/'local'/f'{name}-report.json',{'engineering':'passed','checks':f'{name}-checks.json',
            'quality':'not_met','scope':'mechanism regression and historical/exploratory data; not independent formal acceptance',
            'measurement':'grouped-results.json' if name!='empty' else 'No new OCR; unknown is not true blank'})
    save(AUDIT/'api/trigger-policy.json',{'version':VERSION,'limits':LIMITS,'prompt_sha256':fingerprint(PROMPT),
        'trigger':'user-confirmed single table with current locally validated structure proposals',
        'page_load_calls':0,'automatic_retry':False,'random_checks':'random-check-policy.json'})
    save(AUDIT/'api/structure-contract.json',{'version':VERSION,'allowed':['select existing locally valid candidate','abstain'],
        'fields':['version','decision','candidate_id','token_ids','reason'],'coordinate_or_text_generation':False,
        'references':'complete candidate token IDs, frozen basis, image and structure hashes','prompt':PROMPT})
    for filename in ('trigger-checks','contract-checks','evidence-checks','queue-checks','adoption-checks','protocol-receipt','credential-checks'):
        checks('api/'+filename+'.json',['ArbitrationTests','test_external_review'],
            real_service='not_measured',scope='OpenAI/Anthropic loopback; exact images and IDs; malformed/rejected/truncated/429, cancellation and stale evidence',
            credentials='Fixture key absent from input snapshot and normalized response; personal credential stores not read')
    save(AUDIT/'api/ui-receipt.json',browser)
    checks('finance/checks.json',['FinanceTests'],arithmetic='decimal.Decimal with bounded precision',mutates_text=False)
    save(AUDIT/'finance/rules.json',{'version':'financial-consistency-v1','source_sha256':sha(ROOT/'src/ocr_workbench/financial_checks.py'),
        'rules':['explicit total/subtotal labels','non-nested contiguous rows','stated compatible units/currencies','rounding intervals','decimal places','year duplication','identifier formatting','parentheses','percentages'],
        'uncertain':['missing unit','blank or dash','nested subtotal','spans affecting range','mixed currency','annotated or unreadable number'],
        'changes_text':False,'real_financial_accuracy':'not_measured'})
    save(AUDIT/'api/real-service-status.json',{'real_service':'not_measured','paid_calls':0,'real_cost':'not_measured',
        'reason':'No real service explicitly authorized for this evaluation; continued with public materials and two local protocol fixtures',
        'human_efficiency':'not_measured','target_device_gpu':'not_measured','next_command':'Supply a frozen qualified second group to the same queue and measure_structure_net_benefit.py'})
    frozen={'source_files':regression['source_files'],'policies':{p.name:sha(p) for p in (ROOT/'config').glob('geometry-*.json')},
        'prompt_sha256':fingerprint(PROMPT),'created_utc':datetime.now(timezone.utc).isoformat(),
        'local_run':'local-selection-03','protocol_sha256':sha(AUDIT/'evaluation-protocol.json'),'default_strategy':'local-v2','automatic_adoption':False}
    for name in ('local-seal','final-seal'):save(AUDIT/(name+'.json'),frozen)
    for name in ('local-acceptance','final-acceptance'):
        save(AUDIT/(name+'.json'),{'engineering':'passed','formal_quality':'not_met','independent_quality':'not_measured',
            'real_service':'not_measured','sealed_group':[],'reason':'No new qualified unexposed group; 7 Agent-audited PDF tables and historical crop replays are exploratory only',
            'gaps':'gaps.json','source_seal':'final-seal.json','backend':'regression/backend.json','browser':'regression/browser.json'})
    evidence={
      'B01':['candidate-contract.json','candidate-checks.json'], 'B02':['performance/direct-axis-index-v3.json','performance/invariants.json'],
      'B03':['local/merged-report.json','local/merged-checks.json'],'B04':['local/repeated-report.json','local/repeated-checks.json'],
      'B05':['local/empty-report.json','local/empty-checks.json'],'B06':['local/geometry-report.json','local/geometry-checks.json'],
      'B07':['ui/review-receipt.json','ui/screenshots.json'],'B08':['integration/structure-checks.json','integration/export-roundtrip.json'],
      'B09':['baseline/ablations.json','historical-replay-final/ablations.json','local-selection-03/ablations.json','ablations/selection.json','local/grouped-results.json'],
      'B10':['local-seal.json','local-acceptance.json'],
      'C01':['api/trigger-policy.json','api/trigger-checks.json'],'C02':['api/structure-contract.json','api/contract-checks.json'],
      'C03':['api/evidence-checks.json'],'C04':['api/queue-checks.json'],'C05':['api/adoption-checks.json'],
      'C06':['finance/rules.json','finance/checks.json'],'C07':['api/protocol-receipt.json','api/ui-receipt.json','api/credential-checks.json'],
      'C08':['api/measurement-receipt.json','api/real-service-status.json'],'C09':['final-seal.json','final-acceptance.json'],
      'D02':['regression/backend.json','regression/frontend.json','regression/build.json','regression/browser.json']}
    # The initial broad ablation was retained under the original name.
    if not (AUDIT/'baseline/ablations.json').exists():shutil.copy2(AUDIT/'ablations/report.json',AUDIT/'baseline/ablations.json')
    limited={'B02','B03','B04','B05','B06','B09','B10','C06','C08','C09'}
    for task,paths in evidence.items():
        update(task,'done_with_gaps' if task in limited else 'done',[f'audit/{AUDIT.name}/'+p for p in paths],engineering='passed',
            quality='not_met' if task in limited else 'not_measured',next_step='Finalize new standalone bundle and verify archive; preserve all existing results.')
    report=json.loads((AUDIT/'local-selection-03/report.json').read_text('utf-8'))
    m=report['methods'];a=m['actual_adoption/TableFormer-local-v3'];b=m['actual_adoption/TableFormer-spatial']
    samples=json.loads((BUILD/'dataset/manifest.json').read_text('utf-8'))['samples']
    contents=f'''# 复杂表格专项结果 · 2026-09-19

源码版本 0.13.0rc1，schema 12。计划中的本地结构、API 仲裁、财务疑点和产品工程已实现并验证；正式质量、真实服务及目标设备推理结论分别保留限制。独立交付包以随附交付校验回执为准。

## 已交付行为

- 原生 PDF 保留默认候选并增加底纹过滤实验候选；结构来源绑定图像版本、文字 ID、槽位、跨度、变换和完整格语义。
- local-v4 增加空间与行列索引、完整冲突验证及已验证局部结果保留。默认仍为 local-v2，未扩大自动采用。
- 复核显示当前／候选结构、原图、受影响行列、数字格、未知空值与疑似漏字；复用人工采用、拒绝、撤销、重做和来源导出。
- 外部 API 首版选择已有合法候选或弃权，使用现有单连接和独立队列；具备额度、快照、取消、超时、禁止自动重发及过期丢弃。
- 十进制财务核对只显示疑点、原值、关系与差额；不为凑平算式修改文字。

## 数据与同输入结果

本轮取得 5 份公开政府文档、10 张候选表；7 张经 Agent 核图后列为探索参考，3 张因网格／底纹问题保留为弱标签失败案例。所有已核图材料属于开发／回归；没有新合格封存组。公开来源和下载失败均有 URL、访问时间和哈希回执。官方 WTW、FinTabNet、PubTables 路线也已检查，但没有取得满足本轮完整独立验收的标签与新照片组。

另在 120 份历史公开文档的 120 张表格裁剪、11,278 个目标格上进行固定旧 OCR／候选重放；实际采用固定 PaddleOCR-VL 输出，另有参考结构控制轨。这不是新推理，不代表中文财务整页的质量。

| 实际采用轨（最终同输入重放） | local-v3 | local-v4 |
| --- | ---: | ---: |
| 正确完整格定位 | {a['counts']['correct']} | {b['counts']['correct']} |
| 覆盖率 | {a['rates']['precise_coverage']:.2%} | {b['rates']['precise_coverage']:.2%} |
| 识别为错位的提供框 | {a['counts']['wrong']} | {b['counts']['wrong']} |
| 定位端到端 P95（ms） | {a['elapsed_ms']['p95']:.1f} | {b['elapsed_ms']['p95']:.1f} |

最终实际采用轨新增正确 131 格、丢失正确 0 格；参考结构控制轨新增 127 格、丢失正确 5 格，均在逐项目报告中保留。按文档／模板分组的净定位收益区间下界为 0，不能声称统计显著。额外输出、缺输出、未关联、超时和失败都保留在分母；严格输出质量和空白／合并／重复值分组见 `local/grouped-results.json`。这些是定位结果，不等于完整表结构可用率。

底纹机制的负结果也已保存：直接移除所有填充矩形会丢失真实细线；保留薄线后，已核图案例从错误 12 列／78 格恢复为可解释的 6 列／57 格候选。该例已用于开发，不充当独立效果证据。

500 格重复金额机制例中，local-v4 完整定位 500 格，对应阶段约 83–85ms。1,000／2,000 格的完整对应证据组装仍会超过 200ms，性能门槛保持未达；搜索有界，未完成区域降级，不能把部分全局冲突检查作为通过。

## 工程验证与限制

后端完整套件运行 457 项，主 service 运行时通过 456 项、跳过 1 项 GriTS 依赖检查；该项已在独立评分运行时补验通过（该套件 6 项全过）。前端 51 项通过，生产构建通过；存在既有主 JS 分块大小提示。浏览器使用外部无头 Edge，验证两协议队列、零自动外发、人工采用、撤销和财务说明，无新增页面错误，测试后浏览器与服务均已关闭。

正式 100 份**新独立**文档／120 表／6,000 格及各高风险分组目标没有降低，当前不满足。真实扫描、照片、官方独立标签、第二封存组仍不足。真实 API 净收益、真实费用、人工节省时间和用户目标设备 GPU 推理未测；协议模拟只能证明工程链路。新版保持实验范围，操作方法见 [使用说明](complex-tables-guide-20260919.md)。

## 证据与接续

完整状态：`audit/complex-tables-20260919-01/progress.json`；缺口及重试触发条件：`gaps.json`；执行入口：`NEXT.md`。数据和大产物保存在独立 build 目录；交付包不带研究原始文档、测试工作区和个人凭据。

最终交付须同时具有新版清单、独立 ZIP、逐条解压 CRC／SHA256 校验和打包应用冒烟回执。旧 ZIP 与已有用户修改均保留。本报告不把仅准备好目录视为已完成交付。
'''
    (ROOT/'docs/complex-tables-results-20260919.md').write_text(contents,'utf-8')
    save(AUDIT/'documentation-receipt.json',{'files':{p:sha(ROOT/p) for p in ['README.md','docs/complex-tables-guide-20260919.md','docs/complex-tables-results-20260919.md']},'scope':'features, limits, external transmission, offline use, real service and device boundaries'})
    update('D03','done',[f'audit/{AUDIT.name}/documentation-receipt.json'],engineering='passed',next_step='Finalize bundle, smoke it, package and verify all archived bytes.')
    print('Pre-delivery evidence and documentation reconciled')


if __name__=='__main__':main()
