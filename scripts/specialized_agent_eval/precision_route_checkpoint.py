"""Save the tested optional route while its separate actual trial is running."""
from datetime import datetime, timezone
import json

from common import fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment


def main():
    require_environment()
    root = fpath('F:/OCR/output/platform-specialized-agent-20261007')
    names = {
        'initial_contract_failure': 'evaluation/precision-route-contract-001/junit.xml',
        'corrected_contract': 'evaluation/precision-route-contract-002/junit.xml',
        'broad_native_regression_with_legacy_message_failure': 'evaluation/precision-route-regression-001/junit.xml',
        'final_route_and_reader_contract': 'evaluation/precision-route-final-contract-001/junit.xml',
        'final_budget_and_reader_repair': 'evaluation/precision-route-regression-repair-001/junit.xml',
        'platform_bridge_and_stale_schema_test_failure': 'evaluation/precision-route-platform-001/junit.xml',
        'platform_schema_and_bridge_repair': 'evaluation/precision-route-platform-repair-001/junit.xml',
        'common_source': 'source-snapshots/precision-route-common-20261011-001/freeze.json',
        'trial_source': 'source-snapshots/m1-precision-route-validation-direct-20261011-009/trial-source-binding.json',
        'trial_readiness': 'runs/m1-precision-route-validation-direct-prepare-20261011-009/launch-readiness.json'}
    receipt = {
        'format': 'precision-route-implementation-checkpoint-v1',
        'utc': datetime.now(timezone.utc).isoformat(),
        'sources': {'main_helpers': 'd93756c', 'ocr': '6a91106', 'platform': 'a449d6f', 'classification': 'd90707b'},
        'evidence': {key: bound(root / path) for key, path in names.items()},
        'test_summary': {
            'corrected_route': '16 passed / 23 subtests',
            'broad_native_regression': '120 passed / 45 subtests; one legacy error-message failure retained',
            'final_route_reader_and_binding': '31 passed / 23 subtests',
            'budget_and_reader_repair': '22 passed, including the failed legacy budget case',
            'platform_bridge': '9 passed; one stale test expected supported schema19 to be rejected',
            'platform_repair': '2 passed: schema19 accepted, unknown future schema rejected without writes; explicit fields preserved'},
        'policy': {'default_changed': False, 'optional_route': 'business_language_v1',
            'source_only_language': True, 'per_item_route_frozen': True,
            'per_image_original_budget_seconds': 360, 'glm_fallback_reserve_inside_original_seconds': 90,
            'human_review_required': True, 'quality_triggered_second_model': False,
            'old_failed_runs_or_deadlines_reset': False},
        'actual_validation_trial': {'path': str(root / 'runs/m1-precision-route-validation-direct-20261011-009'),
            'status_at_checkpoint': 'running', 'scope_inputs': 40, 'agent_calls': 0,
            'quality_qualified': False, 'final_agent_qualification': False},
        'formal_current': 'v0.9.3', 'goal_status': 'active', 'completed_plan': False,
        'holdout_or_challenge_used_for_tuning': False,
        'next_action': 'Finish native009; independently copy/read all attempts and current XLSX, then score complete validation40. Resolve review relief before M1/M2 closure; M3/M4 remain pending.'}
    receipt_path = root / 'evaluation/precision-route-implementation-checkpoint-20261011-001.json'
    frozen_json(receipt_path, receipt)
    plan_path = fpath('F:/OCR/docs/platform-automation/specialized-agent-tasks-20261007.json')
    plan = read_json(plan_path)
    plan['last_updated'] = '2026-10-11'
    plan['update_basis_main_head'] = 'd93756c'
    plan['execution_checkpoint_20261011_precision_route'] = {
        'goal_status': 'active', 'completed_plan': False, 'formal_current': 'v0.9.3',
        'sources': receipt['sources'], 'implementation_checkpoint': str(receipt_path),
        'actual_native_run': receipt['actual_validation_trial']['path'],
        'actual_native_status_at_checkpoint': 'running', 'scope_inputs': 40,
        'test_summary': receipt['test_summary'], 'policy': receipt['policy'],
        'holdout_or_challenge_used_for_tuning': False, 'next_action': receipt['next_action']}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    text_path = fpath('F:/OCR/docs/platform-automation/平台专用Agent提升计划-20261007.md')
    note = ('\n2026-10-11 可选精度路线已提交OCR `6a91106`／平台 `a449d6f`，默认H＋数学显示路线保持。'
        '仅按绑定原图、业务区域和独立PP文字分流，逐图冻结决策；中文或证据不明用H，拉丁文本用GLM并仅在技术失败时兜底H。'
        '每图360秒内预留90秒，缓存晋升、恢复、原图变化和定向精度子批次保持原预算与来源；首轮因额度跳过有独立回执。'
        '专项31项／23子测试及预算补测22项通过；广泛原生120项通过后的旧错误提示回归已修复，平台9项桥接及两项修复检查通过。'
        '旧schema测试误拒已支持的19版，现确认19可读、未知未来版及旧reader被拒且不改库。'
        '新身份原生009已启动validation40，实际质量、完整尝试／XLSX读回尚待完成，不作采用或减负结论。'
        '见[实施检查点](../../output/platform-specialized-agent-20261007/evaluation/precision-route-implementation-checkpoint-20261011-001.json)。\n')
    existing = text_path.read_text('utf-8')
    if note not in existing:
        text_path.write_text(existing + note, 'utf-8')
    print({'checkpoint': str(receipt_path), 'goal': 'active', 'native009': 'running'})


if __name__ == '__main__':
    main()
