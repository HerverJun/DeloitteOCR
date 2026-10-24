"""Fixed lifecycle adapters for the job references already produced by tools.

Business submissions and unknown-submit recovery remain in their tool families.
This table only selects existing readers, observers, controls and projections.
"""
from dataclasses import dataclass, replace
from functools import partial
import uuid
from .context import require_write


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


def prepare_observation(reference, ctx):
    """Restore a missing service for an accepted receipt without resubmission.

    Callers retain their observation backoff when transport is unavailable.
    Permission, identity and generation failures must cross that boundary.
    """
    import http.client
    from workbench_platform.catalog.launch import LaunchStatus
    from workbench_platform.jobs.native_ocr import OcrUnavailable
    from workbench_platform.jobs.native_classification import ClassificationUnavailable
    ctx.current()
    lifecycle = adapter(reference)
    if lifecycle.prepare is None:
        return True
    require_write(ctx)
    ctx.current()
    try:
        lifecycle.prepare(reference, ctx)
    except LaunchStatus as error:
        if error.status not in {404, 410, 503} and not (error.status == 409 and error.code == 'RESOURCE_BUSY'):
            raise
        ctx.current()
        return False
    except PermissionError:
        raise
    except (OSError, http.client.HTTPException, OcrUnavailable, ClassificationUnavailable):
        ctx.current()
        return False
    ctx.current()
    return True


def _native_read(ref, ctx):
    from .native import read_job, scoped_handoff, ensure_read_service
    if ref.get('application') == 'ocr':
        from .native_mapping import binding, verify_sources
        if ref.get('workspace_id') != ctx.workspace_id:
            raise ValueError('原生任务不属于当前项目')
        source = binding(ctx, ref.get('parent_id'))
        if source:
            verify_sources(ctx, source)
            ensure_read_service(ctx, 'ocr', source['link_id'])
        else:
            scoped_handoff(ctx, ctx.services.ocr_handoff, ref['parent_id'])
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


def _multimodal_read(ref, ctx):
    from workbench_platform.application_services.operations.multimodal import scoped_job, checked_snapshot, Result
    if ref.get('coverage', {}).get('snapshot_id'):
        return checked_snapshot(ctx, Result(job_id=ref['object_id'], snapshot_id=ref['coverage']['snapshot_id']))['analysis']
    return scoped_job(ctx, ref['object_id'])


def _multimodal_cancel(ref, ctx):
    require_write(ctx)
    from workbench_platform.application_services.identity import digest
    value = _multimodal_read(ref, ctx)
    if value['status'] in {'succeeded', 'failed', 'cancelled'}:
        return {'request_state': 'already_settled', 'job': value}
    identity = 'c' + digest([ctx.operation_id or ctx.run_id, ref['object_id'], value['generation']])[:40]
    ctx.current()
    return ctx.services.multimodal_service.control(ctx.workspace_id, ref['object_id'], 'cancel',
        {'request_id': identity, 'expected_revision': value['revision'], 'expected_generation': value['generation']})


def _multimodal_progress(value):
    progress = value.get('progress') or {}
    return {'stage': value.get('stage'), 'counts': _counts(progress), 'hard_cancel': True,
            'has_error': bool(value.get('error'))}


def _batch_read(ref, ctx):
    from workbench_platform.application_services.operations.platform_management import batch
    return batch(ctx, ref['object_id'])


def _pdf_read(ref, ctx):
    from workbench_platform.application_services.operations.pdf import status
    value = status(ref, ctx)
    return {**value, **_pdf_progress(value)}


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
    require_write(ctx)
    current = _native_read(ref, ctx)
    if current['status'] in {'succeeded','partial','failed','cancelled'}:
        return {'request_state': 'already_settled', 'job': current}
    ctx.current()
    return ctx.services.job_service.action(ctx.workspace_id, ref['object_id'], 'cancel')


def _batch_cancel(ref, ctx):
    require_write(ctx)
    value = _batch_read(ref, ctx)
    if value['status'] in {'succeeded','partial','failed','cancelled'}:
        return {'request_state': 'already_settled', 'batch': value}
    ctx.current()
    return ctx.services.batch_service.control(ref['object_id'], {'operation':'cancel'})


def _data_cancel(ref, ctx):
    require_write(ctx)
    ctx.current()
    return ctx.services.data_workbench.cancel(ref['parent_id'], ref['object_id'])


def _forensic_cancel(ref, ctx):
    return {'status':'unsupported','hard_cancel':False,
            'message':'取证进程无硬取消；可停止Agent后续动作'}


def _pdf_cancel(ref, ctx):
    require_write(ctx)
    from workbench_platform.application_services.operations.pdf import cancel
    return cancel(ref, ctx)


def _prepare_native(ref, ctx):
    require_write(ctx)
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
    if module == 'multimodal_transfer':
        from workbench_platform.application_services.operations.multimodal import settle_transfer
        value = settle_transfer(ref, ctx)
    else:
        value = import_module('workbench_platform.application_services.operations.' + module).settle(ref, ctx)
    if value is not None:
        # Completing an operation must not erase the native receipt that makes
        # its ownership, recovery and final public observation verifiable.
        refs = value.setdefault('job_refs', [])
        if not any(item['object_id'] == ref['object_id'] and item.get('application') == ref.get('application') for item in refs):
            refs.append(ref)
    return value


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
    ('platform','documents'): Lifecycle(
        lambda ref, ctx: ctx.services.document_service.get(ctx.workspace_id, ref['object_id']),
        partial(_observe, 'documents'),
        lambda ref, ctx: ctx.services.document_service.control(ctx.workspace_id, ref['object_id'], 'cancel'),
        lambda value: {'stage': value['stage'], 'hard_cancel': True, 'has_error': bool(value['error'])}),
    ('data','multimodal_transfer'): Lifecycle(_data_read, partial(_observe,'multimodal_transfer'), _data_cancel, _data_progress),
    **{('multimodal', kind): Lifecycle(_multimodal_read, partial(_observe, 'multimodal'),
                                    _multimodal_cancel, _multimodal_progress)
       for kind in ('face', 'audio', 'seal', 'catnet', 'document_forensics', 'aigc_image')},
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
