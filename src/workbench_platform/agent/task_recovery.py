"""Resume one goal without replaying unknown business effects."""
from dataclasses import replace
import json

from .registry import invoke, tool_activity
from .store import Conflict, canonical, now


RECOVERABLE = frozenset({'provider_unavailable', 'rate_limited', 'authentication', 'config_changed',
    'harness_exited', 'harness_protocol', 'truncated_response', 'response_unknown',
    'context_limit', 'CONTEXT_WINDOW_EXCEEDED', 'context_compaction_failed', 'stalled', 'resource_busy'})
AUTO_RETRY = frozenset({'provider_unavailable', 'rate_limited', 'harness_exited', 'truncated_response', 'response_unknown'})


def last_fault(runtime, run_id):
    rows = runtime.store.rows("SELECT payload FROM events WHERE run_id=? AND kind='error' ORDER BY seq DESC LIMIT 1", (run_id,))
    return json.loads(rows[0]['payload']).get('code') if rows else None


def can_resume_run(runtime, run):
    if run['status'] in {'paused', 'needs_verification'}:
        return True
    if run['status'] == 'partial' and run.get('driver', 'harness') == 'harness':
        return True
    return run['status'] == 'failed' and last_fault(runtime, run['id']) in RECOVERABLE


async def check_business_receipts(runtime, run, state):
    """Only a native lookup may release an uncertain non-idempotent effect."""
    for record in runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY created', (run['id'],)):
        result = json.loads(record['result']) if record['result'] else None
        if result is not None and result.get('status') != 'needs_verification':
            continue
        spec = runtime.registry.tools.get(record['tool'])
        if spec is None:
            raise Conflict('原操作工具不可读取，回执保留；恢复该工具后继续')
        # Reads and explicitly idempotent tools recover through the usual
        # journal path. An explicit unknown receipt still requires lookup.
        if result is None and (spec.effect == 'read' or spec.idempotent):
            continue
        context = replace(runtime.context(state), operation_id=record['id'])
        recovered = await invoke(spec.lookup_operation,
            spec.input_schema.model_validate(json.loads(record['arguments'])), context) if spec.lookup_operation else None
        context.current()
        if recovered is None:
            raise Conflict('原生写入回执尚不可核实；已完成成果可读取，请恢复原服务后再点继续，未重新提交')
        recovered = runtime.registry.validated_result(spec, recovered, context)
        if recovered['status'] == 'needs_verification':
            raise Conflict('原生写入回执仍待核实，未重新提交')
        runtime.store.finish_operation(record['id'], recovered)
        call = {'call_id': record['call_id'], 'tool': record['tool'], 'arguments': json.loads(record['arguments'])}
        tool_activity(context, call, 'restored', recovered)


async def prepare_resume(runtime, run, *, automatic=False):
    from workbench_platform.resources.reader_features import require_features
    require_features(runtime.store.root.parent, 'same-goal-continuation-v1')
    state = runtime.graph.load(run['id'])
    if not state:
        rows = runtime.store.rows('SELECT payload FROM events WHERE identity=?', (run['request_id'],))
        if not rows:
            raise Conflict('原用户目标记录不可读取，未建立新的目标')
        state = runtime.initial_state(run, json.loads(rows[0]['payload'])['content'])
    await check_business_receipts(runtime, run, state)
    if automatic:
        current = runtime.store.run(run['id'])
        if current['generation'] != run['generation'] or current['status'] in {'paused', 'cancelled'}:
            from .store import RunSuperseded
            raise RunSuperseded('用户已停止自动恢复')
    from .harness_service import incomplete_delivery, remaining_delivery_work
    partial = state.get('delivery') if incomplete_delivery(state.get('delivery')) else None
    if run['status'] == 'partial' and partial is None:
        previous = runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='delivery_publish' "
            "AND json_extract(result,'$.status')='success' AND json_extract(result,'$.data.completeness')='partial' "
            'ORDER BY created DESC LIMIT 1', (run['id'],))
        if previous:
            partial = json.loads(previous[0]['result'])
    if run['status'] == 'partial' or partial is not None or state.get('partial_delivery'):
        require_features(runtime.store.root.parent, 'partial-goal-continuation-v1')
        from .delivery_staging import restore_partial_stages
        restore_partial_stages(runtime, run)
        if partial is not None:
            state['partial_delivery'] = partial
        state['remaining_delivery_work'] = remaining_delivery_work(state.get('partial_delivery'))
    # These snapshots only stop the controller. Immutable published responses,
    # delivery batches and accepted/unknown native receipts remain untouched.
    state.update(answer=None, answer_display_id=None, delivery=None, delivery_display_id=None)
    state.pop('wait', None)
    unknown = runtime.store.rows("SELECT id FROM model_requests WHERE run_id=? AND state='sent'", (run['id'],))
    # Unknown model generations have no business authority: tool dispatch is
    # journaled separately. After checking receipts, preserve the old request
    # and let Harness rebuild committed history before a bounded retry.
    for record in unknown:
        with runtime.store.transaction() as db:
            db.execute("UPDATE model_requests SET state='abandoned',response=? WHERE id=? AND state='sent'",
                (canonical({'error': 'response_unknown', 'resolution': 'automatic_retry' if automatic else 'explicit_continue',
                            'business_receipts_checked': True}), record['id']))
        runtime.store.event(run['session_id'], run['id'], record['id'] + ':abandoned', 'model_attempt_abandoned',
            {'request_id': record['id'], 'reason': '自动恢复原目标；旧响应不执行，已核对原操作回执' if automatic else '用户继续原目标；旧响应不执行，已核对原操作回执', 'generation': run['generation']})
        if state.get('pending_model_request') == record['id']:
            state.pop('pending_model_request', None)
    code = last_fault(runtime, run['id'])
    if code in RECOVERABLE and not runtime.model_provider:
        current = runtime.connection.view()
        if not current.get('available') and code in {'authentication', 'config_changed'}:
            raise Conflict('已完成成果保留；请先修复并测试模型连接，再点继续')
        if current.get('available') and current['revision'] != run['connection_revision']:
            config, _ = runtime.connection.resolve(current['revision'])
            with runtime.store.transaction() as db:
                db.execute('UPDATE runs SET connection_revision=?,updated=? WHERE id=? AND generation=?',
                           (current['revision'], now(), run['id'], run['generation']))
            state['frozen_config'] = config
            runtime.store.event(run['session_id'], run['id'],
                run['id'] + ':connection-repaired:' + str(current['revision']), 'connection_repaired',
                {'previous_revision': run['connection_revision'], 'revision': current['revision'],
                 'generation': run['generation']})
    state.update(repeated_action=0, no_progress=0, idle_without_progress=0)
    if not automatic:
        state['started_active_seconds'] = 0
    state.pop('last_bridge_error', None)
    state.pop('progress_guard', None)
    state.pop('progress_repair', None)
    runtime.graph.save(state)
    return state


def recovery_message(runtime, run_id, code, message):
    count = len(runtime.store.rows("SELECT id FROM operations WHERE run_id=? AND state='finished' "
        "AND json_extract(result,'$.status')='success'", (run_id,)))
    completed = f'已保存 {count} 项完成步骤及原资料；' if count else '原目标、资料和操作回执已保存；'
    action = '修复并测试模型连接后点“继续”' if code in {'authentication', 'config_changed'} else '点“继续”接着完成剩余步骤'
    return completed + '后续步骤待继续。' + action + '。' + message


def retry_delay(error, attempt):
    """Honor a provider retry time; otherwise use the two short backoffs."""
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    import math
    value = getattr(error, 'retry_after', None)
    if value is None:
        value = (getattr(error, 'response_diagnostics', None) or {}).get('retry_after')
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(str(value)) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = None
    return max(0, seconds) if seconds is not None and math.isfinite(seconds) else (1 if attempt == 0 else 4)
