"""Record optional precision admission, bulk controls and actual cold recovery."""
import json
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment


def main():
    require_environment()
    root=fpath('F:/OCR/output/platform-specialized-agent-20261007')
    browser_root=root/'evidence/precision-route-real-ui-004'
    browser=read_json(browser_root/'browser.json')
    final_view=read_json(browser_root/'browser-final-view.json')
    stop=read_json(browser_root/'stopped.json')
    cold_root=root/'runs/precision-route-cold-restore-20261011-003'
    cold=read_json(cold_root/'report.json')
    if not (browser['passed'] and final_view['passed'] and stop['servers_stopped']
            and stop['task_queue_stopped'] and not stop['native_tasks'] and cold['status']=='passed'):
        raise ValueError('Actual workflow or cold recovery is not completely qualified/stopped')
    tests={}
    for name,member in {
        'frontend':'evaluation/precision-route-ui-001/junit.xml',
        'platform_original_budgets':'evaluation/precision-route-bridge-budget-001/junit.xml',
        'agent_skill_snapshot_and_authority':'evaluation/precision-route-agent-skill-002/junit.xml',
        'native_controls_and_route':'evaluation/precision-route-control-001/junit.xml',
        'cold_archive_comparison':'evaluation/precision-route-cold-comparison-001/junit.xml',
        'cold_geometry_relocation_and_tampering':'evaluation/precision-route-cold-geometry-001/junit.xml',
    }.items():
        path=root/member
        suites=list(ET.parse(path).getroot().iter('testsuite'))
        if not suites or any(int(s.get('failures','0')) or int(s.get('errors','0')) for s in suites):
            raise ValueError('Failed or missing required check: '+name)
        tests[name]={**bound(path),'reported_cases_including_subtests':sum(int(s.get('tests','0')) for s in suites)}
    def head(path):return subprocess.check_output(['git','-C',str(path),'rev-parse','--short','HEAD'],text=True).strip()
    sources={family:head(root/'worktrees'/family) for family in ('ocr','platform','classification')}
    sources['main_before_record']=head(fpath('F:/OCR'))
    freeze_path=root/'source-snapshots/precision-route-workflow-common-20261011-004/freeze.json'
    freeze=read_json(freeze_path)
    if any(not freeze['sources'][family]['base_head'].startswith(sources[family]) for family in ('ocr','platform','classification')):
        raise ValueError('Common freeze does not bind the committed source heads')
    evidence={name:bound(path) for name,path in {
        'original_precision_submission':root/'evidence/precision-route-real-ui-003/precision-submission.json',
        'ordinary_submission':root/'evidence/precision-route-real-ui-003/ordinary-submission.json',
        'unknown_cancel_original_receipt':root/'evidence/precision-route-real-ui-003/cancel-original-lookup.json',
        'browser':browser_root/'browser.json','final_ui':browser_root/'browser-final-view.json',
        'final_screenshot':browser_root/'screenshots/precision-entry-final.png',
        'browser_stopped':browser_root/'browser-stopped.json','host_stopped':browser_root/'stopped.json',
        'cold_recovery':cold_root/'report.json','common_freeze':freeze_path,
        'earlier_actual_quality_and_limits':root/'evaluation/precision-route-closure-20261011-002.json',
    }.items()}
    retained_failures={name:bound(path) for name,path in {
        'consumed_input_fixture':root/'evidence/precision-route-real-ui-001/failure.json',
        'stopped_queue_admission':root/'evidence/precision-route-real-ui-002/browser-failure.json',
        'cancel_http_timeout':root/'evidence/precision-route-real-ui-003/browser-failure.json',
        'desktop_skill_validator_preexisting_product_field':root/'evaluation/precision-route-agent-skill-001/skill-validation.log',
        'cold_comparator_archive_root':root/'runs/precision-route-cold-restore-20261011-001/failure.json',
        'cold_transform_proof_included_unused_path':root/'runs/precision-route-cold-restore-20261011-002/failure.json',
    }.items()}
    receipt={'format':'precision-route-workflow-and-cold-closure-v1','sources':sources,
        'evidence':evidence,'tests':tests,'retained_failures':retained_failures,
        'accuracy_ui_connected':True,'ordinary_submission_unchanged':True,
        'original_large_batch_budgets':{'200':72000,'240':86400,'1000':360000},
        'actual_ui_engineering_inputs':240,'actual_new_cancellation_seconds':next(
            row['seconds'] for row in browser['actual_control_seconds'] if not row['original_action_recovered']),
        'unknown_submit_or_action_preserves_original_identity':True,
        'actual_cold_inputs':cold['scope_inputs'],'historical_model_attempts_preserved':cold['actual_model_attempts'],
        'historical_failed_model_attempts_preserved':cold['actual_failed_model_attempts'],
        'cold_current_reader_schema':cold['current_reader_schema'],
        'original_native009_bytes_unchanged':cold['original_native_bytes_unchanged'],
        'original_costs_policy_requests_and_deadline_preserved':cold['original_execution_ledger_preserved'],
        'new_model_calls':0,'new_agent_calls':0,'owned_models_or_native_services_active':0,
        'review_workload_reduction_qualified':False,'final_model_or_agent_qualification':False,
        'holdout_or_challenge_used_for_tuning':False,'formal_current':'v0.9.3','completed_plan':False,
        'next_action':'Establish defensible review relief on current daily workflows, then final common-source M3 model/Agent and M4 product adoption; preserve untouched holdout240/challenge24.'}
    closure=root/'evaluation/precision-route-workflow-closure-20261011-001.json'
    frozen_json(closure,receipt)
    plan_path=fpath('F:/OCR/docs/platform-automation/specialized-agent-tasks-20261007.json')
    plan=read_json(plan_path)
    plan['last_updated']='2026-10-11'
    plan['update_basis_main_head']=sources['main_before_record']
    plan['execution_checkpoint_20261011_precision_workflow']={
        'closure':str(closure),'sources':sources,'accuracy_ui_connected':True,
        'original_batch_budgets':receipt['original_large_batch_budgets'],
        'actual_ui_inputs':240,'actual_cold_inputs':cold['scope_inputs'],
        'actual_new_cancellation_seconds':receipt['actual_new_cancellation_seconds'],
        'reader_schema':20,'review_workload_reduction_qualified':False,
        'completed_plan':False,'formal_current':'v0.9.3','owned_models_or_native_services_active':0,
        'next_action':receipt['next_action']}
    plan_path.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n','utf-8')
    text_path=fpath('F:/OCR/docs/platform-automation/平台专用Agent提升计划-20261007.md')
    text=text_path.read_text('utf-8')
    note=('\n2026-10-11 精度入口与原预算已接通：OCR `'+sources['ocr']+'`／平台 `'+sources['platform']+'`。'
        '新界面明确按原图业务文字分流，提示耗时增加且仍需核对；200／240／1000图在首次提交固定整批72000／86400／360000秒。'
        '普通请求及历史策略不改，Agent技能增加按需读取的显式精度策略。前端30项／类型检查／构建、平台13项、Agent26项／14子检查、原生50项／43子检查通过。'
        '真实浏览器240个工程版本证明提交、原请求重放、不同预算拒绝和普通模式一致；暴露的逐项停止超时已改为整批读取与事务更新，'
        '沿原身份恢复后剩余240项停止约'+f"{receipt['actual_new_cancellation_seconds']:.2f}"+'秒，界面最终全部已取消，未调用模型或Agent。'
        '当前schema20消费者另对实际009的40图／42次识别尝试／2次失败完成新的字节副本冷备、迁移、产品恢复及HTTP来源／当前导出读回；'
        '原库字节、政策、请求身份、成本和截止时间保持，旧数据根隔离后仍一致。首轮评测器误将迁移前历史备份路径按当前根归一化，'
        '独立核对确认活跃记录一致且历史备份字节未变。第二轮发现透视证据哈希包含未读取的准备图路径，'
        '恢复到新目录会改变相同像素／格位的证据身份；现仅排除此存储路径，来源与变换绑定保持。'
        '补迁移／篡改检查后第三个冷恢复身份完整通过；全部失败保留。'
        '这补齐入口和当前恢复工程证据，未新增识别／Agent或校对减负资格；M1/M2及M3/M4继续，正式current仍为v0.9.3。'
        '见[精度工作流与冷恢复闭环](../../output/platform-specialized-agent-20261007/evaluation/precision-route-workflow-closure-20261011-001.json)。\n')
    if 'precision-route-workflow-closure-20261011-001.json' not in text:text_path.write_text(text+note,'utf-8')
    print(json.dumps({'closure':str(closure),'sources':sources,'completed_plan':False},ensure_ascii=False))


if __name__=='__main__':main()
