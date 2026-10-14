"""Freeze bounded positive/confirmation/negative-control evidence and decision."""
from datetime import datetime,timezone
import difflib
import json
from pathlib import Path
import shutil
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from native_repair_pilot import read,save,sha
from ocr_workbench.geometry_contract import fingerprint
from ocr_workbench.structure_wire import encode,dumps,restore,evidence

RUN='luna-local-review-20260919-06'
AUDIT=ROOT/'audit'/RUN;BUILD=ROOT/'build'/RUN


def main():
    runs=['luna-local-review-20260919-'+n for n in ('06','07','08')]
    scores=[read(ROOT/'audit'/r/'scores.json') for r in runs]
    inputs=[(r,item) for r in runs for item in read(ROOT/'build'/r/'prepared.json')]
    before=[];after=[];times=[];locks=[]
    for run,item in inputs:
        if not item['snapshot']:continue
        packet=ROOT/'build'/run/'inbox'/item['case'];snapshot=item['snapshot']
        started=time.perf_counter();wire=encode(snapshot);times.append((time.perf_counter()-started)*1000)
        assert wire==read(packet/'wire.json') and fingerprint(wire)==snapshot['wire_sha256']
        assert restore(wire['archive'])==evidence(snapshot)
        assert sha(packet/'page.png')==item['sent_image_sha256']
        assert len(dumps(wire))<=40000
        before.append(len(dumps(evidence(snapshot))));after.append(len(dumps(wire)))
        for name in ('wire.json','page.png','PROMPT.txt','response.json'):
            p=packet/name;locks.append({'path':str(p.relative_to(ROOT)),'sha256':sha(p)})
    save(AUDIT/'model-packet-lock.json',locks)
    for run in ('06','07'):
        protocol=read(ROOT/'audit'/('luna-local-review-20260919-'+run)/'protocol.json')
        for name,digest in protocol['source_hashes'].items():assert sha(ROOT/name)==digest
    for run in ('structure-repair-native-20260919-01','structure-repair-native-20260919-02','structure-repair-native-20260919-03'):
        for lock in read(ROOT/'audit'/run/'reference-response-lock.json'):
            assert sha(ROOT/'build'/run/'reference-inbox'/lock['case_id']/'response.json')==lock['sha256']
    regression=read(ROOT/'audit/structure-repair-native-20260919-03/regression/backend-wire-final.json')
    assert regression['passed'] and regression['source_unchanged']
    for name,digest in regression['source_files'].items():assert sha(ROOT/'src/ocr_workbench'/name)==digest
    roundtrips=read(AUDIT/'response-roundtrips.json');assert roundtrips['passed'] and roundtrips['tables']==13
    price=read(AUDIT/'cost-estimates.json')
    stats={'development':{k:v for k,v in scores[0].items() if k!='records'},
        'new_document_confirmation':{k:v for k,v in scores[1].items() if k!='records'},
        'repeat_review_controls':{k:v for k,v in scores[2].items() if k!='records'},
        'primary_inputs':15,'primary_sendable':13,'primary_budget_blocked':2,
        'agent_calls':19,'reference_model_calls_this_run':3,'production_endpoint_calls':0,
        'combined_local_corrected':scores[0]['local']['corrected']+scores[1]['local']['corrected'],
        'combined_luna_corrected':scores[0]['luna']['corrected']+scores[1]['luna']['corrected'],
        'wire_characters_min':min(after),'wire_characters_max':max(after),
        'aggregate_lossless_size_reduction':1-sum(after)/sum(before),
        'encode_ms_observed_max':max(times),'encode_ms_observed_median':statistics.median(times),
        'performance_scope':'one observation per frozen packet, encode includes full reconstruction check; not a controlled P95 benchmark',
        'uncertainty':'Astra model reference, 6 primary documents / 2 template families; new confirmation is 3 documents within same audit family; one unqualified changed development cell; no formal universal accuracy claim'}
    save(AUDIT/'combined-summary.json',stats)
    policy={'decision':'retain bounded experimental header-review route; no universal table correction or automatic adoption',
        'online_model':'gpt-5.6-luna or a separately evaluated budget model; Astra is offline reference only',
        'validated_scope':'native PDFs, existing fixed literals/source identities, <=8 changed header flags, legal complete candidates',
        'production_changes':['lossless structure-evidence-v2 transport','full-snapshot and wire hashes','one global short token namespace mapped back before existing hard validation','readable current/candidate change views'],
        'experimental_only':['native header generator and fixed candidate budget filtering remain research injection, not default UI routing','Astra-based quality measurement','agent-based Luna calls'],
        'cost_policy':['user-triggered external review only','keep current on refusal/invalid/over-budget','no automatic retries','at most existing two attempts per table revision','no recurring Astra online review'],
        'potential_optimization':'single-candidate primary cases had no observed gain over local choice (2 of 13); skipping such ranking calls is a post-hoc cost suggestion, not enabled or independently validated',
        'not_established':['real endpoint reasoning/output-cap behavior','actual billed cost and latency','scans/photos','merge/split improvement','new-template confirmation','general benchmark quality'],
        'next_work':'validate the frozen protocol on a configured real endpoint and broader templates before promoting header generator from research to product UI; stop expansion if net benefit disappears',
        'user_manual_research_required':False,'new_zip':False,'global_plan_complete':False}
    save(AUDIT/'integration-decision.json',policy)
    attempts=[]
    for suffix in ('01','02','03','04','05'):
        path=ROOT/'audit'/('luna-local-review-20260919-'+suffix)
        if path.exists():attempts.append({'run':path.name,'model_calls':0,'purpose':'lossless coding iterations before model calls',
            'status':'preparation aborted at image path field on first attempt; subsequent attempts measured packing, superseded by frozen 06' if suffix=='01' else 'superseded before model calls',
            'eligible':read(path/'eligibility.json')['eligible'] if (path/'eligibility.json').exists() else None})
    save(AUDIT/'pre-model-attempts.json',attempts)
    sources=['src/ocr_workbench/structure_arbitration.py','src/ocr_workbench/structure_wire.py',
        'tests/test_external_review.py','tests/test_structure_wire.py','scripts/luna_local_review.py',
        'scripts/luna_confirmation_data.py','scripts/luna_response_roundtrip.py','scripts/luna_repeat_controls.py',
        'scripts/luna_cost_profile.py','scripts/finalize_luna_local_review.py']
    source_locks=[]
    for name in sources:
        p=ROOT/name;copy=BUILD/'final-source'/name;copy.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,copy)
        item={'path':name,'sha256':sha(p),'snapshot':str(copy.relative_to(ROOT))}
        baseline=ROOT/'build/luna-local-review-20260919-01/baseline-source'/p.name
        if baseline.exists():
            item['baseline_sha256']=sha(baseline)
            diff=''.join(difflib.unified_diff(baseline.read_text('utf-8').splitlines(keepends=True),p.read_text('utf-8').splitlines(keepends=True),fromfile=str(baseline.relative_to(ROOT)),tofile=name))
            out=AUDIT/'diffs'/(p.name+'.diff');out.parent.mkdir(exist_ok=True);out.write_text(diff,'utf-8')
        source_locks.append(item)
    save(AUDIT/'final-source-lock.json',source_locks)
    report=f'''# Luna 有界表头复核：探索与冻结复测（2026-09-19）

**结论：保留这条小范围路线。** 让 Luna 在已有合法候选中选择有限的表头修改，可以取得额外收益；生产方案不依赖 Astra 在线逐表审核。保持实验范围和建议式采用，不扩大为通用整表重写。

## 结果

| 阶段 | 输入表／模型调用 | 固定本地确认纠正 | Luna 确认纠正 | Luna 相对本地净增 | 确认改坏 | 未确认改动格 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 开发 | 9／7 | 21 | 26 | +5 | 0 | 1 |
| 冻结后新文档复测 | 6／6 | 18 | 29 | +11 | 0 | 0 |
| 已采用结果重复复核压力测试 | 6／6 | 0 | 0 | 0 | 0 | 0 |

主实验共 15 张表，13 张可发送，2 张大表保持超限拒绝。两阶段在相同合法候选池上比较固定本地首选与 Luna，确认纠正由 **39 格增至 55 格，增加 16 格**；保留不变的方案不产生这些修复。所有修复均为 `is_header` 元数据，文字与跨度不变；不表示 55 个 OCR 错字被改正。

重复复核另外使用 6 个已经实际采用的结果，重新生成满足同样硬约束的追加表头候选，不按参考答案挑候选。Luna **6 次全部弃权**，没有把第一条数据行误标为表头。它们是已曝光材料的压力测试，不混入新文档收益分母。

新材料来自香港第 86 号审计报告第 2、3、4 章，每份取按冻结规则发现的前两张合格表，共 3 份文档、6 页。规则仅使用页序、表规模、原生预览和冻结候选能否生成；没有使用 Astra 标签筛选。原图由三个独立 `gpt-6-astra` 任务创建完整参考表。Luna 每表一个独立 `gpt-5.6-luna`、medium 任务，只看发送图像及候选，没有答案或历史上下文。

本轮共实际调用 Luna 子任务 19 次，新增 Astra 参考任务 3 次；真实外部 API 端点调用为 0。子任务可用工具读取 JSON、复制 token 列表，这与无工具的一次生产请求存在差异。发送图像与产品相同，保持 1,048,576 像素／1,600 边长上限。

## 做出的工程改动

原来单候选完整证据为 52,101～326,994 字符，无法通过 40,000 字符发送限制。本轮新增 `structure-evidence-v2`：共享字段与来源、精确矩形编码、完整候选间差分；保留原始完整快照，同时给模型提供可读的当前表和候选改动视图。每份输入都必须逐字段还原并校验原始证据哈希。文字 token 使用全局一一映射的短 ID，响应映回原 ID 后仍执行原有完整网格、文字归属、图像／修订及候选校验。

最终 19 个发送包为 {min(after):,}～{max(after):,} 字符，相对相同完整证据合计减少 {stats['aggregate_lossless_size_reduction']:.1%}。发送上限、次数上限和图片限制均未提高；没有截掉证据、四舍五入坐标、丢弃文字或修改金额。

压缩与提示词迭代均发生在第一次 Luna 调用之前；之后源文件哈希保持不变，新文档复测未调参。传输已接入现有外部结构复核路径，兼容 OpenAI／Anthropic 传输适配；旧提示词快照会按既有过期机制拒绝。

表头生成器和按固定排序裁减重复候选仍属于研究注入，尚未新增默认产品入口；没有自动采用、启动即调用、额外供应商管理或生产 Astra 依赖。

## 成本判断

按 [Luna 官方价格](https://developers.openai.com/api/docs/models/gpt-5.6-luna)（每百万输入 $0.20、输出 $1.20）及[官方图像计费规则](https://developers.openai.com/api/docs/guides/images-vision)，用本地 `o200k_base` 估算文字 token，并按实际发送尺寸计算图像 token：

- 仅按可见答复长度估算，主实验单次约 **${price['usd_per_primary_request_visible_min']:.4f}～${price['usd_per_primary_request_visible_max']:.4f}**。
- 若每次用满当前 2,048 输出 token 预算，约 **${price['usd_per_primary_request_full_output_min']:.4f}～${price['usd_per_primary_request_full_output_max']:.4f}**；按本批输入构成，1,000 次约 **${price['usd_per_1000_primary_requests_full_output_mean']:.2f}**。

这是估算，tokenizer 没有 Luna 的专名映射，使用 `o200k_base` 代理；不是真实账单或保证上限。真实推理 token、少量消息封装、网关加价、实际延迟，以及输出预算是否导致截断均未通过真实端点实测。Astra 仅是离线研发参考成本，不计入逐表生产请求。

成本策略保持按需调用、超限保留、失败不自动重试。两个只有一个合法候选的主实验请求没有相对本地增益；减少此类排序调用是可研究的后续节省项，本轮不伪称已经独立验证或启用。

## 工程验证

- 后端全量发现 {regression['tests']} 项：{regression['tests']-len(regression['skipped'])} 项通过，{len(regression['skipped'])} 项因独立 GriTS 环境跳过，无失败；导入当前源码，运行前后哈希相同。
- 13 份实际 Luna 答复已在隔离项目中通过真实排队、快照准备、响应完成、建议显示、采用、撤销／重做、重开与 XLSX 导出。建议完成不会自动改变正文；采用后文字、原生来源记录和表头标记符合选择，原始结果不变。
- 编码测试覆盖共享引用循环、未知引用、重复／缺失文字 ID、响应增加自由字段、哈希篡改、重复值独立身份、Unicode、浮点数、空串及无效候选。OpenAI／Anthropic 本地协议回归通过。
- 本轮未改前端源码；未新增浏览器截图或刷新安装 ZIP。单包编码观测中位数 {statistics.median(times):.1f} ms、最慢 {max(times):.1f} ms，未作正式 P95 性能验收。

## 我作出的范围决定

目前结果足以支持继续保留“原生 PDF 的有界表头候选复核”，不足以启用全量自动纠错。Astra 模型参考不是人工真值；开发中仍有 1 个未确认改动格，不能把零确认改坏解释为保证零风险。新复测的三份文档仍属同一审计报告家族；扫描、照片、新模板、重复金额跨格移动及真正合并／拆分均未因此得到验证。

后续应优先验证冻结协议在真实低成本端点上的输出预算和账单，再扩展文档／模板。若扩展后失去净收益，就保留已验证的小场景，不升级到 Astra 在线替这条路线兜底。研究审核继续由模型与程序承担，不需要用户逐格处理。

证据入口：`audit/luna-local-review-20260919-06/combined-summary.json`、`cost-estimates.json`、`response-roundtrips.json`、`final-source-lock.json`。开发、新文档和压力测试分别为 run `06`、`07`、`08`。首次五轮编码准备尝试未调用模型，全部保留；旧结构修复实验与交付包未覆盖。
'''
    report_path=ROOT/'docs/luna-bounded-review-results-20260919.md';report_path.write_text(report,'utf-8')
    save(AUDIT/'progress.json',{'run':RUN,'stage':'bounded_exploration_complete_with_limits','completed':datetime.now(timezone.utc).isoformat(),
        'report':str(report_path.relative_to(ROOT)),'decision':policy['decision'],'manual_research_required':False,
        'luna_agent_calls':19,'astra_new_reference_calls':3,'production_endpoint_calls':0,'global_plan_complete':False})
    (AUDIT/'NEXT.md').write_text('Bounded header route retained: frozen new-document confirmation +11 cells beyond local, 0 measured harm; 6 repeat controls all abstained. No auto-adoption. Next: real endpoint output-limit/cost/latency validation and new-template confirmation before productizing research header generator. Do not substitute production Astra or claim universal merge/split/OCR gains.\n','utf-8')
    files=[]
    for run in runs+['structure-repair-native-20260919-03']:
        for root in (ROOT/'build'/run,ROOT/'audit'/run):
            for p in sorted(root.rglob('*')):
                if not p.is_file() or p.name=='artifact-index.json' or p.suffix in ('.pyc','.sqlite3-wal','.sqlite3-shm'):continue
                files.append({'path':str(p.relative_to(ROOT)),'bytes':p.stat().st_size,'sha256':sha(p)})
    files.append({'path':str(report_path.relative_to(ROOT)),'bytes':report_path.stat().st_size,'sha256':sha(report_path)})
    save(AUDIT/'artifact-index.json',{'scope':'final three model runs, new confirmation acquisition, report; excludes this index and transient caches','files':files})
    print(json.dumps({'primary_net_gain':stats['combined_luna_corrected']-stats['combined_local_corrected'],
        'new_document_net_gain':scores[1]['luna']['net']-scores[1]['local']['net'],
        'repeat_control_abstentions':scores[2]['eligible']-scores[2]['selected'],'backend':regression['tests'],
        'artifact_files':len(files),'report':str(report_path)},ensure_ascii=False))


if __name__=='__main__':main()
