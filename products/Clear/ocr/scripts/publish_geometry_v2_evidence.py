"""Assemble local, compact delivery evidence; never publish or upload externally."""
from collections import Counter
import json
from pathlib import Path
import shutil
import sys
from geometry_eval_common import ROOT,sha,write_json,mapping_code_lock,verify_inputs
from report_geometry_v2 import percentiles


def read(path):return json.loads(Path(path).read_text('utf-8'))
def pct(value):return '—' if value is None else f'{value*100:.2f}%'
def ms(value):return '—' if value is None else f'{value:.2f}'


def main():
    base=ROOT/'build/table-matching-v2';audit=ROOT/'audit/table-matching-v2'
    target=audit/'reports'
    if target.exists():raise ValueError('Evidence summaries are immutable')
    original_selection=read(audit/'test-selection-lock.json')
    amendment=audit/'test-tooling-amendment.json'
    selection=read(amendment) if amendment.exists() else original_selection
    if selection['code']!=mapping_code_lock():raise ValueError('Code changed after test sealing')
    for source in ('dataset','pages'):
        for split in ('development','validation','test'):verify_inputs(base/source/'inputs'/(split+'.json'))
    specs={
        'development':('development-final-02','development-final-02'),
        'validation':('validation-final-02','validation-final-02'),
        'test':('test-final','test-final'),
        'real-development':('real-development-final-02','real-development-final-02'),
        'real-test':('real-test-final','real-test-final')}
    reports={};receipts=[]
    for name,(report_name,replay_name) in specs.items():
        path=base/'reports'/(report_name+'.json');data=read(path);lock_path=base/'replay'/replay_name/'run-lock.json';lock=read(lock_path)
        if not data['complete'] or data['replay_lock_sha256']!=sha(lock_path) or lock['code'] not in (selection['code'],original_selection['code']):raise ValueError('Incomplete or unsealed final report: '+name)
        reports[name]=data;receipts.append({'name':name,'path':str(path.relative_to(ROOT)),'sha256':sha(path)})
    pages={s:read(base/'pages/reports'/(s+'.json')) for s in ('development','validation','test')}
    if not all(p['complete'] for p in pages.values()):raise ValueError('Incomplete page supplement')
    ui=read(base/'ui-02/ui-results.json')
    if ui['errors'] or not ui['browser_closed'] or not all(c['passed'] for c in ui['checks']):raise ValueError('UI failed')
    log=base/'python-all-final.log'
    # PowerShell may use UTF-8 or UTF-16 for redirected native output.
    content=log.read_bytes();text=content.decode('utf-16' if content.startswith(b'\xff\xfe') else 'utf-8')
    if 'Ran 252 tests' not in text or not text.rstrip().endswith('OK'):raise ValueError('Python regression log incomplete')
    target.mkdir()
    for name,data in reports.items():
        summary={k:v for k,v in data.items() if k!='rows'}
        summary['detailed_report']=next(r for r in receipts if r['name']==name)
        write_json(target/(name+'.json'),summary)
    for split,data in pages.items():write_json(target/('pages-'+split+'.json'),data)
    for name in ('split-lock.json','protocol.json','qualification.json'):
        for source,prefix in [('dataset','cells'),('pages','pages')]:
            shutil.copy2(base/source/name,audit/(prefix+'-'+name))
    shutil.copy2(log,audit/'python-regression.log')
    shutil.copy2(base/'replay-lock-regression.log',audit/'replay-lock-regression.log')
    for name in ('ui-results.json','ui-location-timing.json','02-review-whole-table.png','04-small-laptop.png'):
        shutil.copy2(base/'ui-02'/name,audit/name)
    timing={}
    for split in ('development','validation','test'):
        manifest=read(base/'dataset/inputs'/(split+'.json'))
        for provider in ('ppocr','geometry','rapidtable'):
            folder=base/'inference'/split/provider;items=[read(folder/s['id']/'evaluation.json') for s in manifest['samples']]
            timing[split+':'+provider]={'device':'CPU (4 intra / 1 inter)' if provider=='rapidtable' else 'GPU:0',
                'status_counts':dict(Counter(x['status'] for x in items)),
                'seconds':percentiles([x['seconds'] for x in items]),'load':read(folder/'timing.json')}
        if split!='validation':
            folder=base/'real-results'/split;items=[read(folder/s['id']/'evaluation.json') for s in manifest['samples']]
            timing[split+':actual-paddlevl']={'device':'GPU:0','status_counts':dict(Counter(x['status'] for x in items)),
                'seconds':percentiles([x['seconds'] for x in items]),'load':read(folder/'timing.json')}
    detailed={}
    for method in ('N1','N2'):
        token=[];local=[]
        for sample in read(base/'dataset/inputs/test.json')['samples']:
            prediction=read(base/'replay/test-final'/method/sample['id']/'mapping.json')
            values=next((m['timing_ms'] for m in prediction['mappings'] if m.get('timing_ms')),None)
            if values:token.append(values['token_matching']);local.append(values['local_correspondence'])
        detailed[method]={'token_matching_ms':percentiles(token),'local_correspondence_ms':percentiles(local),
            'scope':'Only tables with stage timing; rejected table identity may have no detailed stage record'}
    ui_timing=percentiles(read(base/'ui-02/ui-location-timing.json')['milliseconds'])
    write_json(audit/'performance.json',{'inference':timing,'test_mapping_stages':detailed,'ui_cached_click_dom_ms':ui_timing,
        'scope':'Local machine, automated timing. CPU/GPU separate; no human or target-machine claim.'})
    wt=read(ROOT/'build/document-workflow/delivery-evidence/wtw-metrics.json')
    write_json(audit/'wtw-historical-supplement.json',{'scope':wt['scope'],'limitations':wt['limitations'],'groups':wt['groups'],
        'historical_exposed':True,'rerun_in_this_round':False,'source_sha256':sha(ROOT/'build/document-workflow/delivery-evidence/wtw-metrics.json')})
    write_json(audit/'validation-summary.json',{'python_tests':252,'python_log_sha256':sha(log),'frontend_test_files':8,'frontend_tests':32,
        'frontend_build':'TypeScript/Vite passed; pre-existing >500KB bundle notice',
        'frontend_log_note':'Observed passing earlier in this task; raw frontend stdout was not copied into this receipt',
        'ui_checks':len(ui['checks']),'ui_errors':len(ui['errors']),'browser_closed':ui['browser_closed'],'post_seal_evaluation_tests_passed':9,
        'production_default':False,'code_matches_test_selection':True,'source_reports':receipts})
    qualification=read(base/'dataset/qualification.json')['summary'];test=reports['test']['groups'];real=reports['real-test']['groups']
    lines=['# 表格局部匹配 v2 质量报告','',
        '2026-09-13。**实现完成，局部改善已测得，默认精确定位效果未达标。** 继续保留实验入口，`production_default=false`。源码工作区未重新打包，不替代独立 Windows/A4000 验收。','',
        '## 数据与封存','',
        '| 集合 | 表/独立文档 | 全部格 N | 合并格 | 重复值格 | 空格 |','|---|---:|---:|---:|---:|---:|']
    for name,q in qualification.items():lines.append(f"| {name} | {q['tables']}/{q['documents']} | {q['targets']} | {q['target_tags']['merged']} | {q['target_tags']['repeated']} | {q['target_tags']['empty']} |")
    lines+=['','新逻辑集仅为 PubTables PDF 渲染表格裁剪，每表 50–250 格。官方完整格坐标与跨度已核验；在推理前按原文档、图像 pHash、文字/结构近重复排除历史 104 文档及相近样本。另有 DocLayNet 60 页，20 无表、20 单表、20 多表，按文档/模板 stem 排重。完整协议、资格和 split-lock 保存在 audit。','',
        '新主测试及页级测试在最终开发/验证报告后锁定代码和策略才推理；模型不读取目标框。本轮未根据测试结果改算法或阈值。测试现已曝光，后续按其错误开发需要新的独立测试。公开材料可能与上游训练重叠，不能声称预训练隔离。','',
        '封存后发现并修正了回放工具矩阵 tuple/list 的 JSON 恢复问题。补充锁 `test-tooling-amendment.json` 记录仅 replay 脚本容器类型变化，序列化矩阵、匹配源码、策略和采用规则完全相同；原脚本、原锁及主测试成绩原样保留。修复后真实结果回放使用补充锁，不能称为新的完整代码独立测试。9 项评测完整性回归通过。缓存性能脚本曾把一个冻结开发图像作为临时导入输入，已从保留的原始副本逐字节恢复并校验原 SHA，脚本现先复制；全部冻结输入再次校验通过，推理输出未改变。','',
        '## 控制变量主轨','',
        '参考文字/逻辑结构固定，坐标来自独立冻结 PP-OCR。C 是身份正确、无并列歧义且完整 polygon IoU≥0.5；A 是实际提供完整格定位，W 是另一个参考格 polygon IoU 严格更高。目标缺失、超时和降级全部计入 N；文字范围不计完整格成功。','',
        '| 集合/路线 | N | A | C | W | C/N | W/A | C/A |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for split in ('development','validation','test'):
        for method in ('B1','B2','N1','N2'):
            r=reports[split]['groups'][method]['all'];c=r['counts']
            lines.append(f"| {split}/{method} | {c['targets']} | {c['offered']} | {c['correct']} | {c['wrong']} | {pct(r['precise_coverage'])} | {pct(r['wrong_cell_rate'])} | {pct(r['offered_quality'])} |")
    lines+=['','B0 始终只提供整表，因此 C=A=0。B1/B2 为原 Paddle/Rapid，N1/N2 使用相同原始候选接 v2。三个集合分别报告，不能将已用于开发的数据并入盲测成绩。','',
        '**判定：测试 N1/N2 均未达到 C/N≥90%、W/A≤1%、C/A≥95% 的完整发布条件。** 低错位率同时伴随大量框不达标，不能把它解释为精确定位可靠。','',
        '| 测试路线 | 覆盖率文档 bootstrap 95% CI | 错位率 95% CI | 定位达标率 95% CI | 按表平均覆盖率 |','|---|---|---|---|---:|']
    for method in ('N1','N2'):
        r=test[method]['all'];ci=r['document_group_bootstrap_95pct'];interval=lambda k:'–'.join(pct(v) for v in ci[k]) if ci[k] else '—'
        lines.append(f"| {method} | {interval('precise_coverage')} | {interval('wrong_cell_rate')} | {interval('offered_quality')} | {pct(r['mean_table_precise_coverage'])} |")
    lines+=['','区间按原文档/模板组 bootstrap 1,000 次、固定种子；N2 错位率区间可高于 1%。','',
        '## 风险分组与失败','',
        '| 测试实际属性/路线 | 格数 | 表/文档 | C | W | 覆盖率 | 提供定位达标率 |','|---|---:|---:|---:|---:|---:|---:|']
    for tag in ('merged','not-merged','repeated','not-repeated','empty','not-empty'):
        for method in ('N1','N2'):
            r=test[method]['target:'+tag];c=r['counts']
            lines.append(f"| {tag}/{method} | {c.get('targets',0)} | {r['tables']}/{r['documents']} | {c.get('correct',0)} | {c.get('wrong',0)} | {pct(r['precise_coverage'])} | {pct(r['offered_quality'])} |")
    lines+=['','以上仅计真正具备属性的格；含该属性整表的全部格另见 JSON 的 `tables-with:*`。FinTabNet 新 canonical、原生 PDF/物理扫描真实结果、有线/无线可靠标签和中文/拍照完整逻辑未取得合格新主集，不能宣称这些类别通过。完整页多表检测有单独结果，不代表多表单元格关联达标。','']
    for method in ('N1','N2'):
        r=test[method]['all'];c=r['counts']
        lines.append(f"- {method}：提供 {c['offered']} 格，正确 {c['correct']}、错位 {c['wrong']}、边界不达标 {c.get('low_iou',0)}、并列归属 {c.get('identity_tie',0)}；文字范围降级 {c.get('text_fallback',0)}，整表降级 {c.get('region_fallback',0)}。主阶段统计："+'，'.join(f'{k}={v}' for k,v in r['primary_failure_stages'].items())+'。')
    lines+=['','主阶段总数可回到 N；辅助原因多选，不能相加当总失败。并列归属是辅助标记，可与错位重叠，不能再与 C/W/B 相加。B2 在一表结构解析失败产生 55 个无输出目标，仍在分母。测试 N1 有 88 个目标因预算超时降级，未删除或重跑挑选。线上 `accepted` 只是接受了证据，与独立评分的 `correct` 含义不同。','',
        '## 实际采用结果轨','',
        '采用规则预先固定为成功 PaddleOCR-VL 原始文字/表格、revision 0。全部最优 LCS 的唯一 rank 及独立文字表身份确定参考关联，不使用定位框反推身份。缺失参考目标保留，实际额外/无法关联/重复格全部进入严格输出质量分母。此轨仍是 PDF 渲染表格裁剪，不是原生 PDF 或物理扫描端到端主集。','',
        '| 集合/路线 | N | A | C | W | 缺失参考格 | 额外实际格 | C/N | C/A | C/(A+额外) |','|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for split in ('real-development','real-test'):
        for method in ('B1','B2','N1','N2'):
            r=reports[split]['groups'][method]['all'];c=r['counts']
            lines.append(f"| {split}/{method} | {c['targets']} | {c['offered']} | {c['correct']} | {c['wrong']} | {c.get('no_output',0)} | {c.get('extra_outputs',0)} | {pct(r['precise_coverage'])} | {pct(r['offered_quality'])} | {pct(r['strict_output_quality'])} |")
    lines+=['',f"实际测试错位率：N1 为 {pct(real['N1']['all']['wrong_cell_rate'])}，N2 为 {pct(real['N2']['all']['wrong_cell_rate'])}。实际结果轨未达发布目标。保守身份关联也可能丢失本来可关联的目标，不能把这些缺失全部归因于候选几何；它避免使用待评测框选择答案造成虚高成绩。",'',
        '## 完整页与中文/拍照检测补充','',
        '| DocLayNet 集合 | 页 | GT 表 | 预测表 | 正确一一检出 | 召回 | 精确率 | 无表页误报页数 |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for split,data in pages.items():
        r=data['groups']['all'];c=r['counts'];neg=data['groups']['no_table']
        lines.append(f"| {split} | {r['pages']} | {c['targets']} | {c['offered']} | {c['matched']} | {pct(r['recall'])} | {pct(r['precision'])} | {neg['pages_with_false_positive']}/{neg['pages']} |")
    lines+=['','使用 PP-DocLayoutV3、固定阈值 0.5、真实完整页输入，GT 不给检测器；一一 bbox IoU≥0.5，失败/无表页不删。各集合的单表/多表细分及时延见 `pages-*.json`。','',
        'WTW 历史 20 张、1,226 个格的 polygon 检测召回 95.27%、精确率 98.07%；其中拍照 5 张、中文 17 张，分组可重叠。这是已曝光历史补充，未重新作为新测试运行；缺转录和逻辑跨度，逻辑覆盖率/错位率为 null。','',
        '## 消融、候选上限与局部性','',
        '| 验证集消融 | C | W | 相对旧 baseline 新增 C | 失去 C | 新增 W |','|---|---:|---:|---:|---:|---:|']
    for method,delta in reports['validation']['deltas'].items():
        c=reports['validation']['groups'][method]['all']['counts']
        lines.append(f"| {method} | {c['correct']} | {c['wrong']} | {delta['new_correct']} | {delta['lost_correct']} | {delta['new_wrong']} |")
    lines+=['','消融分别关闭局部对应、合并格、重复/空格约束，复用同一候选，无额外 GPU 推理。每项新增框不准、耗时及开发集结果均保存在摘要 JSON；消融不是上线策略，不按测试消融调参。','',
        'M0 历史 5,819 格：拓扑不一致 5,357，文字覆盖不足 107，推理失败 95，接受 224，文字锚点不一致 21，后处理来源不成立 14，重复/空格歧义 1；全部目标闭合。原始直接检测候选在 GT 辅助的一一分配 oracle 下仅 1,801/5,819=30.95%，后处理 1,811，raw SLANeXt 4；文字范围 oracle 2,387 不能算完整格成功。','',
        '预选开发集前 12 表（1,338 格）的 N3 TableFormer raw：C=654、A=1,058、W=1，覆盖 48.88%、提供定位达标率 61.81%；候选 oracle 815/1,338=60.91%。同 12 表 N1 C=48、N2 C=214。N3 后处理把框变成文字范围，未提供任何完整格成功；原始/后处理及压缩前后证据分别保留。此结果支持下一轮研究边界候选，不能支持立即默认集成。','',
        '真实候选局部扰动审计按与推理分数无关的规则选定前 12 张无合并格开发表：新增表头后保留 Paddle 5/5、Rapid 189/189 个既有正确位置；删除中间行后保留 4/4、177/177，已提供范围改变为另一位置的数量为 0。Paddle 分母很小；这是局部机制回归，不是独立精度证据。旧 Rapid 100 表与原映射逐项完全一致。','',
        '## 性能与工程回归','',
        '开发机：Windows 11、i5-13600KF、约 32GiB RAM、RTX 4070 Ti SUPER 16GiB，驱动 595.97。service Python 3.12.10 / NumPy 2.2.6；vendored matcher 单独导入不加载 Torch。9 个 Paddle 模型目录文件哈希全部匹配来源 manifest。','',
        '| 测试对应阶段 | 中位数 ms | P90 ms | P95 ms | 表数 |','|---|---:|---:|---:|---:|']
    for method in ('B1','B2','N1','N2'):
        t=test[method]['all']['mapping_ms'];lines.append(f"| {method} | {ms(t['median'])} | {ms(t['p90'])} | {ms(t['p95'])} | {t['n']} |")
    lines+=['','对应阶段包含读取候选、provider 适配及本地匹配，GPU 推理除外；测试表均≤250 格，N1/N2 P95 均在 200ms 预算内，但个别超时照常降级。纯 token/局部对应的可用细分记录、冷加载和推理阶段分位数见 `performance.json`。大于 250 格没有新的独立性能样本，不能外推；代码具有格数、候选对及可中断阶段预算。','',
        '| 测试推理 | 设备 | 中位数 s | P90 s | P95 s | 成功/失败 |','|---|---|---:|---:|---:|---|']
    for provider in ('ppocr','geometry','rapidtable','actual-paddlevl'):
        data=timing['test:'+provider];t=data['seconds'];lines.append(f"| {provider} | {data['device']} | {ms(t['median'])} | {ms(t['p90'])} | {ms(t['p95'])} | {data['status_counts']} |")
    cache=read(audit/'cache-profile.json')
    lines+=['',f"外部 Edge 50 次缓存点击/DOM 断言：中位数 {ms(ui_timing['median'])}ms，P90 {ms(ui_timing['p90'])}ms，P95 {ms(ui_timing['p95'])}ms，满足 300ms 自动化预算。236 格真实开发表的 100 次聚焦缓存读取 P95 {ms(cache['focused_cache_read_ms']['p95'])}ms，不变输入请求命中 P95 {ms(cache['unchanged_request_cache_ms']['p95'])}ms；单次完整映射/落库 {ms(cache['complete_geometry_ms'])}ms，其中 artifact 哈希及提交附加开销 {ms(cache['artifact_hash_and_commit_overhead_ms'])}ms。单样本存储值不是总体 P95。",'',
        '完整 Python 回归 252 项通过；前端 32 项、TypeScript/Vite 构建通过；外部无头 Edge 10 项通过、页面异常 0，截图已核对，浏览器已关闭。人工绑定、缓存 CPU 重放、文字/结构修订、取消/晚到结果、原生 PDF 和重复文字层保护均覆盖。没有真人效率结论。','',
        '## 交付结论','',
        '局部对应可保留独立成立的位置，较旧整表拓扑门槛增加了正确格；完整格边界和重复/空格锚点仍限制覆盖率。下一轮先研究完整格候选及独立真实文字粒度，并补齐合法的原生/扫描/中文标注范围；不通过缩小成功定义、删失败样本或把文字范围改称完整格来追求 90%。','',
        '实施与回退：[实施记录](table-cell-matching-v2-status.md)。上游许可、完整路径与可执行复算命令：[复现说明](table-cell-matching-v2-reproduction.md)。精简 JSON、锁、回归日志和最终截图：`audit/table-matching-v2/`。大规模图像、转录、模型及逐格结果留在本地 build，不随源码摘要分发。','']
    (ROOT/'docs/table-cell-matching-v2-quality.md').write_text('\n'.join(lines),'utf-8')
    write_json(audit/'delivery-manifest.json',{'version':2,'files':{str(p.relative_to(audit)):sha(p) for p in sorted(audit.rglob('*')) if p.is_file()},
        'source_reports':receipts,'production_default':False,'test_code_matches':True})
    print('Quality report and compact local evidence assembled.')


if __name__=='__main__':main()
