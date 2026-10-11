"""Record the native precision result and later recovery compatibility guards."""
import json

from common import fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment


def main():
    require_environment()
    root=fpath('F:/OCR/output/platform-specialized-agent-20261007')
    actual_path=root/'evaluation/precision-route-actual-closure-20261011-001.json'
    actual=read_json(actual_path)
    tests={key:bound(root/path) for key,path in {
        'native_schema20_and_legacy_history':'evaluation/precision-route-schema20-001/junit.xml',
        'platform_reader_admission':'evaluation/precision-route-reader-admission-002/junit.xml',
        'native_route_feature_inference':'evaluation/precision-route-native-admission-001/junit.xml',
        'reader_initial_failure_retained':'evaluation/precision-route-reader-admission-001/junit.xml',
        'independent_reader_final':'evaluation/precision-route-reader-final-001/junit.xml'}.items()}
    sources={'main_helpers':'98a8d94','actual_native_ocr':'6a91106','actual_native_platform':'a449d6f',
        'current_guarded_ocr':'67ba716','current_guarded_platform':'79778f8','classification':'d90707b'}
    receipt={'format':'precision-route-native-and-reader-closure-v1',
        'actual_native_closure':bound(actual_path),'sources':sources,'tests':tests,
        'latest_guarded_common_freeze':bound(root/'source-snapshots/precision-route-guarded-common-20261011-002/freeze.json'),
        'native_summary':{key:actual[key] for key in ('actual_scope','actual_counts','actual_primary_routes',
            'native_model_attempts','actual_model_engines','actual_model_attempt_states',
            'technical_fallback_attempts','technical_fallback_success','source_budget_overshoots',
            'root_budget_overshoot_seconds','actual_wall_seconds','first_native_result','current_xlsx')},
        'quality':actual['quality'],'paired_with_actual_h008':actual['paired_with_actual_h008'],
        'reader_guards':{'native_schema':20,'old_schema19_reader_rejected_before_writes':True,
            'historical_policy_cost_deadline_and_request_preserved':True,
            'platform_feature':'screen-business-language-route-v1',
            'feature_inferred_without_marker':True,'guard_source_has_cpu_evidence_only':True,
            'original_native009_schema19_unchanged':True},
        'default_changed':False,'accuracy_ui_connected':False,'review_workload_reduction_qualified':False,
        'same_source_performance_comparison':False,'agent_qualification':False,
        'holdout_or_challenge_used_for_tuning':False,'formal_current':'v0.9.3','completed_plan':False,
        'next_action':'Connect measured optional accuracy route with explicit original batch budget and preserve historical submissions; verify real daily review/current exports and actual review relief, then final M3/M4.'}
    closure=root/'evaluation/precision-route-closure-20261011-002.json'
    frozen_json(closure,receipt)
    plan_path=fpath('F:/OCR/docs/platform-automation/specialized-agent-tasks-20261007.json')
    plan=read_json(plan_path)
    checkpoint=plan['execution_checkpoint_20261011_precision_route']
    checkpoint.update(actual_native_status_at_checkpoint='terminal',sources=sources,closure=str(closure),
        native_summary=receipt['native_summary'],quality_summary={key:actual['quality'][key] for key in (
            'visible_table_exact_images','visible_exact_cells','visible_reference_cells','visible_critical_exact_cells',
            'visible_critical_cells','visible_error_cells','visible_critical_error_cells','review_reference_cells')},
        paired_delta=actual['paired_with_actual_h008']['totals'],reader_guards=receipt['reader_guards'],
        next_action=receipt['next_action'],owned_models_or_native_services_active=0)
    plan['update_basis_main_head']='98a8d94'
    plan_path.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n','utf-8')
    text_path=fpath('F:/OCR/docs/platform-automation/平台专用Agent提升计划-20261007.md')
    note=('\n原生精度009现已正常终态：原图语言分流H16／GLM24，实际42次识别尝试中40成功、2次GLM达到token上限失败；'
        '两次H技术兜底均成功，旧失败完整保留，来源／整批原预算均无超出或重置。'
        '40项上下文、40本当前XLSX及全部尝试已经从独立字节副本读回。'
        '完整验证40整表13/40，可见2428/2711、关键1633/1821，业务说明42/48、额外25、UI泄漏5；'
        '相对实际H008净修复217可见格／177关键格，同时新增24可见错／4关键错，全部待核。'
        '仍有283可见错／188关键错、6个额外表和168个额外格；40图／2711格仍须复核，没有自动覆盖或人工计时减负资格。'
        '实际1142.60秒、首原生结果178.92秒，无Agent调用，不作同条件性能或最终Agent资格。'
        '后续恢复保护另提交OCR `67ba716`／平台 `79778f8`：schema20阻止旧reader静默按固定模型恢复，'
        '保存并从意图／真实策略推导language-route能力要求；历史策略、成本、截止时间及请求身份保持。'
        '39项／31子测试、28项平台读回及独立native能力反例通过。009保留原冻结schema19证据不回写，后加保护只有CPU工程资格。'
        '精度入口整合、可证明校对减负和M3/M4继续推进，正式current保持v0.9.3。'
        '见[精度实际闭环](../../output/platform-specialized-agent-20261007/evaluation/precision-route-closure-20261011-002.json)。\n')
    text=text_path.read_text('utf-8')
    if note not in text:text_path.write_text(text+note,'utf-8')
    print({'closure':str(closure),'goal':'active','completed_plan':False})


if __name__=='__main__':main()
