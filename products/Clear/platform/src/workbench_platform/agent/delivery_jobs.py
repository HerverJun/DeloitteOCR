"""Read current native states for the actual references being delivered."""
from __future__ import annotations

import json

from .store import canonical, RunSuperseded


class NativeDeliveryBlocked(ValueError):
    def __init__(self, message, checks):
        super().__init__(message)
        self.checks = checks


def _identity(reference):
    if hasattr(reference, 'model_dump'):
        reference = reference.model_dump()
    return canonical({key: reference.get(key) for key in
                      ('kind', 'workspace_id', 'application', 'object_id', 'parent_id', 'revision', 'native_id')})


def verify_native_jobs(references, ctx, *, verified_objects=(), requirement_coverage=None, partial_screen_batches=()):
    from workbench_platform.application_services.operations.jobs import read_reference
    wanted = {_identity(ref) for ref in references}
    jobs, matched, known_jobs = {}, set(), {}
    records = ctx.store.rows('SELECT o.result FROM operations o JOIN runs r ON r.id=o.run_id '
        'WHERE r.session_id=? AND o.result IS NOT NULL ORDER BY o.created DESC,o.rowid DESC', (ctx.session_id,))
    for record in records:
        result = json.loads(record['result'])
        for ref in result.get('job_refs', []):
            known_jobs.setdefault((ref['application'], ref['object_id']), ref)
        produced = {_identity(ref) for field in ('artifact_refs', 'result_refs') for ref in result.get(field, [])}
        selected = (wanted & produced) - matched
        if selected and result.get('job_refs'):
            for ref in result['job_refs']:
                jobs[_identity(ref)] = ref
            matched.update(selected)
    for value in verified_objects:
        if not isinstance(value, dict):
            continue
        kind = value.get('kind')
        application, identifier, parent = None, None, None
        if value.get('screen_delivery') and value.get('handoff_id'):
            application, identifier = 'ocr', value['handoff_id']
            known = known_jobs.get((application, identifier))
            source = ctx.services.screen_batches._row(ctx.workspace_id, identifier)
            parent = known.get('parent_id') if known else source['handoff_id']
        elif kind in {'ocr_result', 'classification_result'} or value.get('platform_job_id') and value.get('handoff_id'):
            application = 'classification' if kind == 'classification_result' or value.get('native_job_id') else 'ocr'
            identifier, parent = value.get('platform_job_id'), value.get('handoff_id')
        elif value.get('export_id') and value.get('session_id'):
            application, identifier, parent = 'data', value['export_id'], value['session_id']
        elif kind == 'image_forensics' or value.get('inspection_id'):
            application, identifier = 'forensics', value.get('inspection_id')
            parent = identifier
        elif value.get('snapshot_id') and value.get('job_id'):
            application, identifier, parent = 'multimodal', value['job_id'], value['job_id']
        elif value.get('kind') == 'manual_analysis' and value.get('platform_job_id'):
            application, identifier, parent = 'multimodal', value['platform_job_id'], value['platform_job_id']
        if identifier and parent:
            ref = known_jobs.get((application, identifier)) or {
                'kind': 'job', 'workspace_id': ctx.workspace_id, 'application': application,
                'object_id': identifier, 'parent_id': parent, 'revision': 1}
            if value.get('screen_delivery'):
                ref = {**ref, 'coverage': {**ref.get('coverage', {}), 'kind': 'screen_batch'}}
            if application == 'multimodal' and not ref.get('coverage', {}).get('kind'):
                ref = {**ref, 'coverage': {'kind': value.get('kind') if value.get('kind') in {'face', 'audio', 'seal', 'catnet', 'document_forensics', 'aigc_image'} else value.get('analysis_kind')}}
            if application == 'multimodal' and value.get('snapshot_id'):
                ref = {**ref, 'coverage': {**ref.get('coverage', {}), 'snapshot_id': value['snapshot_id']}}
                # This citation names a completed immutable attempt. A newer
                # retry cannot rewrite the native status of that attempt.
                jobs = {key: job for key, job in jobs.items() if not
                        (job['application'] == application and job['object_id'] == identifier)}
            if ref.get('parent_id') != parent:
                raise NativeDeliveryBlocked('成果来源与原生作业账本身份不一致', [])
            if not any(job['application'] == application and job['object_id'] == identifier for job in jobs.values()):
                jobs[_identity(ref)] = ref
    checks, limitations, blocked = [], [], []
    repaired_jobs = set((requirement_coverage or {}).get('repaired_job_ids', []))
    for ref in jobs.values():
        try:
            ctx.current()
            if ref.get('coverage', {}).get('kind') == 'screen_batch':
                value = ctx.services.screen_batches.status(ctx.workspace_id, ref['object_id'], limit=1)
            else:
                value = read_reference(ref, ctx)
            status = value.get('status', value.get('state'))
            evidence = {'job_id': ref['object_id'], 'application': ref['application'],
                        'parent_id': ref.get('parent_id'), 'native_status': status}
        except RunSuperseded:
            raise
        except Exception as error:
            # No native submission or state mutation is allowed during delivery.
            status = 'unverified'
            evidence = {'job_id': ref['object_id'], 'application': ref['application'],
                        'native_status': status, 'error_type': type(error).__name__}
        passed = status in {'succeeded', 'completed', 'success'}
        terminal = passed or status in {'partial', 'failed', 'cancelled', 'needs_review'}
        if ref.get('coverage', {}).get('kind') == 'screen_batch':
            counts = value.get('counts', {})
            terminal = terminal and not counts.get('pending')
            if counts.get('needs_review') or counts.get('failed') or counts.get('cancelled'):
                passed = False
            if ref['object_id'] in partial_screen_batches and status != 'unverified':
                terminal, passed = True, False
                evidence['explicit_partial_export'] = True
        message = '原生作业已完成' if passed else '原生作业状态：' + str(status or 'unknown')
        if terminal and not passed and ref['object_id'] in repaired_jobs:
            passed = True
            evidence.update(requirement_coverage='satisfied_by_fixed_retry',
                            coverage_rule_version=requirement_coverage['rule_version'])
            message = '原尝试失败历史保留；相同原件、版本与要求已由固定成功结果覆盖'
        checks.append({'name': 'native_job_status', 'status': 'passed' if passed else 'failed',
                       'requirement': '核实原生作业已终止并完成', 'message': message, 'evidence': evidence})
        if not terminal:
            blocked.append(ref['object_id'])
        elif not passed:
            limitations.append(message + '（' + ref['object_id'] + '）')
    if blocked:
        raise NativeDeliveryBlocked('交付关联的原生作业尚未完成或当前状态无法核实：' + '、'.join(blocked), checks)
    return checks, limitations
