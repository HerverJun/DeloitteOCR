"""Fixed lifecycle adapters for the job references already produced by tools.

Business submissions and unknown-submit recovery remain in their tool families.
This table only selects existing readers, observers, controls and projections.
"""
from dataclasses import dataclass, replace
from functools import partial
import uuid


@dataclass(frozen=True)
class Lifecycle:
    read: object
    settle: object
    cancel: object
    progress: object
    prepare: object = None


def key(reference):
    application = reference['application']
    return application, reference.get('coverage', {}).get('kind') or application


def adapter(reference):
    try:
        return ADAPTERS[key(reference)]
    except KeyError:
        raise ValueError('Native job kind has no registered lifecycle') from None


def _native_read(ref, ctx):
    from .native import read_job
    return read_job(ctx, ref['object_id'])


def _data_read(ref, ctx):
    session = ctx.services.data_workbench.get(ref['parent_id'])
    if session['workspace_id'] != ctx.workspace_id:
        raise ValueError('数据任务项目身份不一致')
    return ctx.services.data_workbench.job(ref['parent_id'], ref['object_id'])


def _forensic_read(ref, ctx):
    value = ctx.services.forensic_service.get(ctx.workspace_id, ref['object_id'])
    return {**{name: value[name] for name in ('inspection_id','status','tools','error','report_hash')},
            'hard_cancel': False}


def _batch_read(ref, ctx):
    from workbench_platform.agent.tools.platform_management import batch
    return batch(ctx, ref['object_id'])


def _pdf_read(ref, ctx):
    from workbench_platform.agent.tools.pdf import status
    return status(ref, ctx)


def _screen_read(ref, ctx):
    from workbench_platform.agent.tools.screen_batches import status_reference
    return status_reference(ref,ctx)


def _screen_cancel(ref, ctx):
    from workbench_platform.agent.tools.screen_batches import cancel_reference
    return cancel_reference(ref,ctx)


def _local_screen_read(ref, ctx):
    from workbench_platform.agent.tools.screen_local_candidates import status_reference
    return status_reference(ref, ctx)


def _local_screen_cancel(ref, ctx):
    from workbench_platform.agent.tools.screen_local_candidates import cancel_reference
    return cancel_reference(ref, ctx)


def _native_cancel(ref, ctx):
    current = _native_read(ref, ctx)
    if current['status'] in {'succeeded','partial','failed','cancelled'}:
        return {'request_state': 'already_settled', 'job': current}
    return ctx.services.job_service.action(ctx.workspace_id, ref['object_id'], 'cancel')


def _batch_cancel(ref, ctx):
    value = _batch_read(ref, ctx)
    if value['status'] in {'succeeded','partial','failed','cancelled'}:
        return {'request_state': 'already_settled', 'batch': value}
    return ctx.services.batch_service.control(ref['object_id'], {'operation':'cancel'})


def _data_cancel(ref, ctx):
    return ctx.services.data_workbench.cancel(ref['parent_id'], ref['object_id'])


def _forensic_cancel(ref, ctx):
    return {'status':'unsupported','hard_cancel':False,
            'message':'取证进程无硬取消；可停止Agent后续动作'}


def _pdf_cancel(ref, ctx):
    from workbench_platform.agent.tools.pdf import cancel
    return cancel(ref, ctx)


def _prepare_native(ref, ctx):
    from .native import availability, prepare, scoped_handoff
    from .native_mapping import binding, verify_sources
    family = ref['application']
    if availability(ctx.services, family)['state'] != 'preparable':
        return
    service = getattr(ctx.services, family + '_handoff')
    row = binding(ctx, ref['parent_id'])
    if row:
        verify_sources(ctx, row)
    else:
        row = scoped_handoff(ctx, service, ref['parent_id'])
    ctx.current()
    # New process identity, same original native job and business receipt.
    prepare(replace(ctx, operation_id='l' + uuid.uuid4().hex), family, row['link_id'])


def cold_interruption(item):
    """Markers verified in native OCR recover(), distinct from human 已暂停."""
    state = item.get('status', item.get('native_status'))
    return state == 'interrupted' or state == 'paused' and item.get('phase') == '等待继续'


def _observe(module, ref, ctx):
    from importlib import import_module
    return import_module('workbench_platform.agent.tools.' + module).settle(ref, ctx)


def _ocr_observe(module, ref, ctx):
    job = _native_read(ref, ctx)
    ctx.current()
    if job['status'] == 'paused':
        service = ctx.services.job_service
        native = service._adapter(service._context(ctx.workspace_id, 'ocr', job['link_id']))
        tasks = native.batch(job['native_job_ids']).tasks
        selected = [item.native_task_id for item in tasks
                    if cold_interruption({'native_status':item.native_status,'phase':item.phase})]
        if selected:
            ctx.current()
            native._request('POST', f'/api/projects/{native.context.module_project_id}/queue/resume',
                            {'task_ids':selected})
        # A paused human/unknown task stays paused. Waiting has no model round.
        return None
    return _observe(module, ref, ctx)


def _counts(value):
    if not isinstance(value, dict):
        return {}
    return {name:number for name,number in value.items()
            if isinstance(name,str) and (number is None or type(number) is int and number >= 0)}


def _ordinary_progress(value):
    if value.get('status', value.get('state')) in {'paused','pausing'}:
        return {'needs_resume':True,
                'resume_message':'原生作业已暂停，请在手动工具中恢复；Agent保持等待'}
    return {}


def _data_progress(value):
    result = value.get('result') or {}
    summary = result.get('summary', {}) if isinstance(result, dict) else {}
    return {'counts':_counts(summary),'coverage':_counts(summary.get('coverage',{}))}


def _forensic_progress(value):
    return {'hard_cancel':False,'counts':{'selected_algorithms':len(value.get('tools',[]))}}


def _batch_progress(value):
    result = {'counts':_counts(value.get('summary')),'coverage':_counts(value.get('coverage'))}
    for flag in ('execution_complete','review_complete','index_complete'):
        source = value.get('summary',{}) if flag != 'index_complete' else value.get('coverage',{})
        if type(source.get(flag)) is bool:
            result[flag] = source[flag]
    return result


def _pdf_progress(value):
    stages = value.get('stages',[])
    counts = {'total':len(stages)}
    for stage in stages:
        state = stage.get('status','unknown')
        counts[state] = counts.get(state,0) + 1
    result = {'counts':counts,'stage':'pdf_pages','has_error':any(bool(s.get('error')) for s in stages)}
    if stages:
        states = {stage.get('status','unknown') for stage in stages}
        if states <= {'succeeded','failed','cancelled'}:
            result['status'] = ('succeeded' if states == {'succeeded'} else 'partial' if 'succeeded' in states
                                else 'failed' if 'failed' in states else 'cancelled')
        else:
            result['status'] = 'running' if 'running' in states else 'paused' if states & {'paused','interrupted'} else 'waiting'
        if any(s['status']=='paused' and not cold_interruption(s) for s in stages):
            result.update(_ordinary_progress({'status':'paused'}))
    return result


ADAPTERS = {
    ('data','data'): Lifecycle(_data_read, partial(_observe,'data'), _data_cancel, _data_progress),
    ('forensics','forensics'): Lifecycle(_forensic_read, partial(_observe,'forensics'), _forensic_cancel, _forensic_progress),
    ('platform','batch'): Lifecycle(_batch_read, partial(_observe,'platform_management'), _batch_cancel, _batch_progress),
    ('ocr','ocr'): Lifecycle(_native_read, partial(_ocr_observe,'ocr'), _native_cancel, _ordinary_progress, _prepare_native),
    ('ocr','fusion'): Lifecycle(_native_read, partial(_ocr_observe,'ocr_processing'), _native_cancel, _ordinary_progress, _prepare_native),
    ('ocr','visual_review'): Lifecycle(_native_read, partial(_ocr_observe,'ocr_management'), _native_cancel, _ordinary_progress, _prepare_native),
    ('ocr','pdf_pages'): Lifecycle(_pdf_read, partial(_observe,'pdf'), _pdf_cancel, _pdf_progress, _prepare_native),
    ('ocr','screen_batch'): Lifecycle(_screen_read,partial(_observe,'screen_batches'),_screen_cancel,_ordinary_progress,_prepare_native),
    ('ocr','screen_local_candidates'): Lifecycle(_local_screen_read,partial(_observe,'screen_local_candidates'),_local_screen_cancel,_ordinary_progress,_prepare_native),
    ('classification','classification'): Lifecycle(_native_read, partial(_observe,'classification'), _native_cancel, _ordinary_progress, _prepare_native),
    ('classification','train'): Lifecycle(_native_read, partial(_observe,'classification_management'), _native_cancel, _ordinary_progress, _prepare_native),
}
