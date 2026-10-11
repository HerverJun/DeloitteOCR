"""Seal actual recognition-copy review/export/recovery engineering evidence."""
import json
import subprocess

from common import fpath, frozen_json, read_json
from controlled_recovery import bound
from freeze_sources import PARTS, inventory


def require(value, message):
    if not value:
        raise ValueError(message)


def main():
    root = fpath('F:/OCR/output/platform-specialized-agent-20261007')
    host = root / 'evidence/actual-review-ui-001'
    browser_root = host / 'browser-run-003'
    readback_path = root / 'evaluation/actual-review-ui-readback-007/report.json'
    cold_path = root / 'runs/actual-review-cold-restore-001/report.json'
    browser, readback, cold, stopped = map(read_json, (
        browser_root / 'browser.json', readback_path, cold_path, host / 'stopped.json'))
    require(browser['passed'] and readback['status'] == cold['status'] == 'passed'
            and stopped['servers_stopped'] and stopped['original_native_bytes_unchanged'], 'Workflow evidence is incomplete')
    require(readback['inputs'] == cold['scope_inputs'] == 40 and readback['workbooks_read'] == 122
            and len(browser['downloads']) == 5 and readback['untouched_results_preserved'] == 39,
            'Actual review or workbook denominator changed')
    require(cold['scripted_review_workflow'] == str(host) and cold['actual_model_attempts'] == 42
            and cold['actual_failed_model_attempts'] == 2 and cold['original_execution_ledger_preserved']
            and cold['reviewed_source_status_counts'] == {'completed': 1, 'needs_review': 39},
            'Cold restoration did not consume the exact reviewed actual copy')
    require(read_json(browser_root / 'browser-stopped.json')['browser_stopped'], 'Browser remains active')
    frozen = read_json(host / 'source/freeze.json')
    sources = {}
    for family in PARTS:
        worktree = root / 'worktrees' / family
        require(inventory(worktree, PARTS[family]) == frozen['sources'][family]['files'], 'Product source changed: ' + family)
        head = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], text=True).strip()
        require(head == frozen['sources'][family]['base_head'], 'Frozen source HEAD changed')
        sources[family] = head
    sources['main_before_record'] = subprocess.check_output(['git', '-C', 'F:/OCR', 'rev-parse', 'HEAD'], text=True).strip()
    failures = []
    for attempt, reason in (
        (1, 'Browser JSON serialized coordinate 173.0 as 173; literal geometry JSON in XLSX differed. Oracle now checks immutable SQLite revisions.'),
        (2, 'Independent helper omitted product OCRZ1 revision compression prefix; fixed decoder, no product change.'),
        (3, 'Browser JSON rounded the existing unsafe-integer engine-load ordinal; exact snapshot comparison rejected it.'),
        (4, 'Same unsafe-integer ordinal rejection, preserved with edited-revision mismatch diagnostic.'),
        (5, 'Expanded diagnostic located only the engine-load ordinal rounding; binding fields and model strings remained equal.'),
        (6, 'Workbook checks completed, but helper shadowed original-root variable during final inventory; fixed variable, no product change.'),
    ):
        path = root / f'evaluation/actual-review-ui-readback-{attempt:03d}/failure.json'
        frozen_json(path, {'status': 'failed', 'reason': reason, 'original_evidence_preserved': True,
                           'superseded_by': str(readback_path), 'product_repair': False})
        failures.append(bound(path))
    for path in (host / 'browser-failure.json', host / 'browser-run-002/browser-failure.json',
                 host / 'download-diagnostic-no-revoke/result.json', host / 'download-diagnostic-original/result.json',
                 host / 'download-diagnostic-error-reason/result.json'):
        failures.append(bound(path))
    evidence = {name: bound(path) for name, path in {
        'common_product_source': host / 'source/freeze.json',
        'browser': browser_root / 'browser.json', 'independent_xlsx_and_revisions': readback_path,
        'host_stopped_and_exit_failure_retained': host / 'stopped.json',
        'executed_host': host / 'executed-host.py', 'executed_browser': browser_root / 'executed-browser.mjs',
        'cold_restoration': cold_path, 'previous_recognition_quality': root / 'evaluation/precision-route-closure-20261011-002.json',
    }.items()}
    receipt = {'format': 'actual-review-export-cold-workflow-closure-v1', 'status': 'passed',
        'sources': sources, 'evidence': evidence, 'retained_failed_attempts': failures,
        'actual_recognition_source_inputs': 40, 'scripted_review_inputs': 1,
        'unmodified_inputs': 39, 'browser_zip_downloads': 5, 'independently_read_workbooks': 122,
        'source_readings_cell_and_metadata_edits_persist': True,
        'draft_navigation_and_export_protected': True, 'replayed_action_does_not_duplicate_edit': True,
        'stale_edit_and_stale_export_rejected': True, 'concurrent_edit_preserved_after_deliberate_reload': True,
        'live_before_after_export_vectors_equal': True, 'partial_export_accounts_for_all_40_inputs': True,
        'reviewed_current_copy_cold_restored': True, 'reader_schema': 20,
        'original_model_attempts': 42, 'original_failed_model_attempts': 2,
        'original_request_cost_policy_and_deadline_preserved': True,
        'new_model_calls': 0, 'new_agent_calls': 0,
        'product_code_changes_this_checkpoint': False, 'scripted_review_actions': True,
        'human_quality_or_relief_qualification': False, 'review_scope_reduction': 0,
        'final_agent_or_full_platform_qualification': False, 'completed_plan': False,
        'holdout240_or_challenge24_used': False, 'formal_current': 'v0.9.3',
        'next_action': 'Complete current-source platform daily-flow integration and defensible review relief, then final common-source M3/M4 qualification.'}
    closure = root / 'evaluation/actual-review-workflow-closure-20261011-001.json'
    frozen_json(closure, receipt)
    plan_path = fpath('F:/OCR/docs/platform-automation/specialized-agent-tasks-20261007.json')
    plan = read_json(plan_path)
    plan['last_updated'] = '2026-10-11'
    plan['update_basis_main_head'] = sources['main_before_record'][:7]
    plan['execution_checkpoint_20261011_actual_review'] = {
        'closure': str(closure), 'sources': sources,
        'actual_recognition_source_inputs': 40, 'scripted_review_inputs': 1,
        'independently_read_workbooks': 122, 'cold_restored_current_reviewed_copy': True,
        'review_workload_reduction_qualified': False, 'completed_plan': False,
        'formal_current': 'v0.9.3', 'owned_models_or_native_services_active': 0,
        'next_action': receipt['next_action']}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    path = fpath('F:/OCR/docs/platform-automation/平台专用Agent提升计划-20261007.md')
    text = path.read_text('utf-8')
    note = ('\n2026-10-11 已在当前OCR `e7746fc`／平台 `3a78959`共同源码上，使用实际精度009的40图字节副本完成原生界面日常复核／编辑／当前导出工程验收。'
        '没有注入识别结果：实际两版业务说明及来源可比较、选辅助原文、单元格与说明手改，草稿阻止切图／关闭／导出；'
        '保留待核、脚本确认、稍后复核、重新加载及重复保存回执一致。第二编辑器修改后，旧复核保存与旧版本导出均409拒绝；'
        '显式重新加载及重新绑定保留并发修改。5个浏览器实下载ZIP共122本XLSX全部独立读回，表格、业务说明、来源和导出前后完整版本向量一致；'
        '1本部分交付仍列全40个输入，其余39图未改、未确认。脚本确认只验证状态机，不能算真实人工核对或减负。'
        '该含手改／确认／历史ZIP的当前副本又完成产品冷备、隔离旧根、恢复及40图HTTP／像素证据读回，原42次识别／2次失败、预算、成本、请求身份和截止时间均保留。'
        '浏览器初次因下载目录斜杠格式取消，经本机规范路径修正；旧按钮名称定位失败、宿主读保护拒绝自身最终源文件清点、独立读回的坐标文字／大整数JSON／压缩前缀及变量错误均保留，未以评测器修正冒充产品改进。'
        '新增模型与Agent调用均0，复核范围未缩小，M1/M2及M3/M4仍未完成，正式current仍为v0.9.3。'
        '见[实际识别副本复核与恢复闭环](../../output/platform-specialized-agent-20261007/evaluation/actual-review-workflow-closure-20261011-001.json)。\n')
    if 'actual-review-workflow-closure-20261011-001.json' not in text:
        path.write_text(text + note, 'utf-8')
    print(json.dumps({'closure': str(closure), 'workbooks_read': 122, 'completed_plan': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
