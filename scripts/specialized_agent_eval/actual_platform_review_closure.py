"""Seal full Portal review and budgeted precision-submission engineering evidence."""
import json
import subprocess

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound
from freeze_sources import PARTS, inventory


def require(value, message):
    if not value: raise ValueError(message)


def main():
    root = fpath('F:/OCR/output/platform-specialized-agent-20261007')
    host = root / 'evidence/actual-platform-review-ui-004'
    browser_root = host / 'browser-run-001'
    readback_path = root / 'evaluation/actual-platform-review-readback-001/report.json'
    browser, readback, stopped = map(read_json, (browser_root / 'browser.json', readback_path, host / 'stopped.json'))
    require(browser['passed'] and readback['status'] == 'passed' and stopped['servers_stopped']
            and stopped['original_source_bytes_unchanged'] and stopped['original_execution_history_preserved'],
            'Full Portal review evidence is incomplete')
    require(readback['inputs'] == 40 and readback['untouched_results_preserved'] == 39
            and readback['workbooks_read'] == 122 and len(browser['downloads']) == 5,
            'Actual review scope or download denominator changed')
    require(browser['precision_submission_native_persistence_verified']
            and browser['precision_submission_cancelled_without_recognition']
            and readback['platform_precision_policy_request_and_deadline_verified']
            and stopped['additional_submission_batches'] == 1, 'Precision submission is not independently verified')
    require(read_json(browser_root / 'browser-stopped.json')['browser_stopped'], 'Browser remains active')
    frozen = read_json(host / 'source/freeze.json')
    sources = {}
    for family in PARTS:
        worktree = root / 'worktrees' / family
        require(inventory(worktree, PARTS[family]) == frozen['sources'][family]['files'],
                'Tested production source bytes changed: ' + family)
        head = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], text=True).strip()
        sources[family] = {'head': head, 'base_head_at_freeze': frozen['sources'][family]['base_head'],
                           'tested_working_bytes_equal_current': True}
    checks = root / 'evaluation/portal-review-route-001'
    vitest = read_json(checks / 'vitest.json')
    require(vitest['numPassedTests'] == 19 and vitest['numFailedTests'] == 0, 'Portal regression did not pass')
    require('13 passed' in (checks / 'export-conflict-tests-002.log').read_text('utf-8') and
            'OK' in (checks / 'readback-counterexamples.log').read_text('utf-8') and
            'built in' in (checks / 'build.log').read_text('utf-8'), 'Affected verification is incomplete')
    evidence = {name: bound(path) for name, path in {
        'common_product_source': host / 'source/freeze.json',
        'browser': browser_root / 'browser.json', 'independent_xlsx_revisions_and_policy': readback_path,
        'host_stopped': host / 'stopped.json', 'executed_host': host / 'executed-host.py',
        'executed_browser': browser_root / 'executed-browser.mjs',
        'portal_tests': checks / 'vitest.json', 'portal_typecheck': checks / 'typecheck.log',
        'portal_build': checks / 'build.log', 'adapter_and_freshness_tests': checks / 'export-conflict-tests-002.log',
        'readback_counterexamples': checks / 'readback-counterexamples.log',
        'precision_submission': browser_root / 'precision-submission.json',
    }.items()}
    failures = [bound(path) for path in (
        root / 'evidence/actual-platform-review-ui-001/failure.json',
        root / 'evidence/actual-platform-review-ui-002/browser-run-001/browser-failure.json',
        root / 'evidence/actual-platform-review-ui-002/browser-run-002/browser-failure.json',
        root / 'evidence/actual-platform-review-ui-002/stopped.json',
        root / 'evidence/actual-platform-review-ui-003/browser-run-001/browser-failure.json',
        root / 'evidence/actual-platform-review-ui-003/cancel-after-browser-locator-failure.json',
        root / 'evidence/actual-platform-review-ui-003/stopped.json',
        checks / 'export-conflict-tests-001-stopped.json')]
    receipt = {'format': 'actual-platform-review-workflow-closure-v1', 'status': 'passed',
        'sources': sources, 'evidence': evidence, 'retained_failed_attempts': failures,
        'actual_recognition_inputs': 40, 'scripted_review_inputs': 1, 'unmodified_unconfirmed_inputs': 39,
        'browser_zip_downloads': 5, 'independently_read_workbooks': 122,
        'actual_two_readings_cell_and_metadata_edits_persist': True,
        'draft_close_and_defer_protected': True, 'replayed_action_does_not_duplicate_edit': True,
        'stale_edit_and_stale_export_return_409': True, 'historical_bundle_freshness_verified': True,
        'concurrent_edit_preserved_after_deliberate_reload': True,
        'managed_bundles_match_native_revision_vectors': True,
        'partial_export_accounts_for_all_40_inputs': True,
        'precision_original_scope_policy_request_and_deadline_persist': True,
        'precision_retry_returns_original_batch': True, 'queued_precision_scope_cancelled': 40,
        'new_model_calls': 0, 'new_agent_calls': 0, 'original_failed_model_attempts': 2,
        'original_request_cost_policy_and_deadline_preserved': True,
        'full_production_portal': True, 'ocr_results_injected': False,
        'trusted_native_launch_and_context_callbacks_bound': True,
        'scripted_review_actions': True, 'human_quality_or_relief_qualification': False,
        'review_scope_reduction': 0, 'final_agent_qualification': False, 'completed_plan': False,
        'holdout240_or_challenge24_used': False, 'formal_current': 'v0.9.3',
        'verification': {'portal_tests': 19, 'portal_typecheck_exit': 0, 'portal_build_exit': 0,
                         'adapter_and_freshness_tests': 13, 'readback_counterexamples': 3},
        'next_action': 'Establish defensible review workload reduction, then final common-source M3 model/Agent and M4 adoption qualifications.'}
    closure = root / 'evaluation/actual-platform-review-workflow-closure-20261011-001.json'
    frozen_json(closure, receipt)
    plan_path = fpath('F:/OCR/docs/platform-automation/specialized-agent-tasks-20261007.json')
    plan = read_json(plan_path)
    plan['last_updated'] = '2026-10-11'
    for milestone in plan['milestones']:
        if milestone['id'] == 'M1':
            milestone['next_action'] = ('保留默认H＋数学显示；显式精度入口使用实际009验证的business_language_v1及原预算内90秒技术兜底预留，'
                '平台提交已与原生持久策略联通。保留全部失败与质量回退；继续解决复核减负，禁止把全图待核或错误减少当作免人工资格。')
        if milestone['id'] == 'M2':
            milestone['next_action'] = ('完整平台已通过实际40图副本复核／编辑／当前ZIP及精度策略持久联动，122本Excel独立读回；'
                '业务说明来源、并发409及草稿保护已验证。继续证明真实校对减负；表头范围v2已由业务值反例否决，禁止采用。')
    plan['execution_checkpoint_20261011_actual_platform_review'] = {
        'closure': str(closure), 'sources': sources, 'actual_recognition_inputs': 40,
        'scripted_review_inputs': 1, 'independently_read_workbooks': 122,
        'full_production_portal': True, 'precision_policy_persisted_and_cancelled_without_recognition': True,
        'review_workload_reduction_qualified': False, 'completed_plan': False,
        'formal_current': 'v0.9.3', 'goal_status': 'active', 'owned_services_active': 0,
        'next_action': receipt['next_action']}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    path = fpath('F:/OCR/docs/platform-automation/平台专用Agent提升计划-20261007.md')
    text = path.read_text('utf-8')
    note = ('\n2026-10-11 完整平台日常复核联动已在平台`d182744`／OCR`e7746fc`上通过实际精度009的40图字节副本验收。'
        '平台提高精度入口显式传入语言分流、每图360秒、180秒请求、原额度内90秒兜底预留及整批原额度；真实HTTP提交、重复提交查回和取消后策略／身份／截止时间均经复制数据库独立核实，未启动识别队列。'
        '实际两版说明选值、格值与说明手改、草稿关闭／稍后复核保护、retain／accepted／deferred及当前／历史ZIP联动通过。'
        '实际并发场景发现原生409被平台适配器误转503，已修正并复验；5个浏览器下载ZIP共122本Excel独立读回，部分交付仍列全40输入。'
        '其余39图未改未确认，原42次模型尝试／2次失败、成本与预算保留；新增模型／Agent调用0。'
        '宿主绑定隔离原生实例的可信启动与context回调，不能据此宣布完整supervisor或最终Agent资格。'
        '错误HTTP状态断言、依赖运行时缺PIL、AntD按钮空格定位及停止的测试尝试全部保留。'
        '19项平台页面测试、13项适配器／当前导出测试、3项读回反例、类型检查和构建通过。'
        '复核范围未缩小，M1/M2及M3/M4仍未完成；表头v2保持否决，正式current仍为v0.9.3。'
        '见[完整平台实际复核闭环](../../output/platform-specialized-agent-20261007/evaluation/actual-platform-review-workflow-closure-20261011-001.json)。\n')
    if 'actual-platform-review-workflow-closure-20261011-001.json' not in text:
        path.write_text(text + note, 'utf-8')
    print(json.dumps({'closure': str(closure), 'workbooks_read': 122, 'completed_plan': False}))


if __name__ == '__main__': main()
