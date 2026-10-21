"""Durable, immutable delivery batches in the existing audit task record."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from packages.contracts.agent import AgentRef
from .store import Conflict, RunSuperseded, canonical, digest, now


class RequirementsConflict(ValueError):
    pass


def _tasks(ctx):
    tasks = getattr(getattr(ctx.services, 'agent', None), 'tasks', None)
    if tasks is None:
        # The same registered tools also run in standalone service fixtures.
        from .tasks import AuditTasks
        tasks = AuditTasks(SimpleNamespace(store=ctx.store))
    return tasks


def _command(args, ctx):
    return {'session_id': ctx.session_id, 'delivery_publish': args.model_dump()}


def _fence(db, ctx):
    run = db.execute('SELECT session_id,workspace_id,generation,status FROM runs WHERE id=?', (ctx.run_id,)).fetchone()
    if (not run or run['session_id'] != ctx.session_id or run['workspace_id'] != ctx.workspace_id
            or run['generation'] != ctx.generation or run['status'] == 'cancelled'):
        raise RunSuperseded('运行已停止，验收记录未写入')


def replay(args, ctx):
    tasks = _tasks(ctx)
    with ctx.store.transaction() as db:
        _fence(db, ctx)
        return tasks._replay(db, ctx.operation_id, _command(args, ctx))


def snapshot(ctx):
    tasks = _tasks(ctx)
    with ctx.store.transaction() as db:
        _fence(db, ctx)
        body, _ = tasks._body(db, ctx.session_id)
        stages = deepcopy(body.get('delivery_requirements', {}).get('stages', []))
    return stages, digest(stages)


def _ref_key(ref):
    return canonical({key: ref.get(key) for key in
                      ('kind', 'workspace_id', 'object_id', 'revision', 'application', 'parent_id', 'native_id')})


def _merge_refs(old, current):
    merged = {}
    for value in [*old, *current]:
        value = AgentRef.model_validate(value).model_dump()
        key = _ref_key(value)
        previous = merged.get(key)
        if previous is not None:
            value = {**value, 'sha256': value.get('sha256') or previous.get('sha256')}
            previous = {**previous, 'sha256': previous.get('sha256') or value.get('sha256')}
            if previous != value:
                raise RequirementsConflict('已保存的成果版本、哈希或来源范围不能替换；请使用新的真实成果引用')
        merged[key] = value
    return list(merged.values())


def _check_key(check):
    return canonical({key: check.get(key) for key in ('name', 'bundle_id', 'filename', 'column')})


def merge(args, stages):
    """Omitted values inherit saved requirements; supplied values cannot weaken them."""
    request = args.model_dump()
    artifacts = [ref for stage in stages for ref in stage['artifact_refs']]
    results = [ref for stage in stages for ref in stage['result_refs']]
    if not args.finalize:
        artifact_keys = {_ref_key(ref) for ref in request['artifact_refs']}
        result_keys = {_ref_key(ref) for ref in request['result_refs']}
        artifacts = [ref for ref in artifacts if _ref_key(ref) in artifact_keys]
        results = [ref for ref in results if _ref_key(ref) in result_keys]
    request['artifact_refs'] = _merge_refs(artifacts, request['artifact_refs'])
    request['result_refs'] = _merge_refs(results, request['result_refs'])
    bundles = {ref['object_id'] for ref in request['artifact_refs']}
    checks = {}
    for check in [*(check for stage in stages for check in stage['checks']
                    if args.finalize or check['bundle_id'] in bundles), *request['checks']]:
        key = _check_key(check)
        if key in checks and checks[key] != check:
            raise RequirementsConflict('已保存的验收要求不能省略或改成较低条件；最终交付会重新核验原要求')
        checks[key] = check
    request['checks'] = list(checks.values())
    request['require_review'] = request.get('require_review', False) or any(stage.get('require_review', False) for stage in stages)
    return request


def expected_files(stages, bundle_ids):
    files = {}
    for stage in stages:
        for file in stage['files']:
            if file['bundle_id'] not in bundle_ids:
                continue
            key = (file['bundle_id'], file['filename'])
            if key in files and files[key] != file:
                raise RequirementsConflict('已保存批次中的固定成果文件身份不一致')
            files[key] = file
    return files


def restore_partial_stages(runtime, run):
    """Retain old partial batches after an explicit same-run Continue.

    Older readers consumed stages even for an incomplete final publication.
    Its immutable effective request contains the original refs and checks.
    Re-admit that exact batch without changing any receipt or native work.
    """
    tasks = runtime.tasks
    with runtime.store.transaction() as db:
        body, revision = tasks._body(db, run['session_id'])
        requirements = body.get('delivery_requirements') or {}
        history = [item for item in requirements.get('history', []) if item['run_id'] == run['id']]
        if not history or history[-1].get('completeness') != 'partial':
            return
        record = history[-1]
        stages = requirements.setdefault('stages', [])
        if any(item['operation_id'] == record['operation_id'] for item in stages):
            return
        stages.append({key: deepcopy(value) for key, value in record.items()
            if key not in {'consumed_stages', 'provisional_stages', 'finalized', 'completeness'}})
        tasks._save(db, run['session_id'], body, revision)


def commit(request, effective, files, result, stages_fingerprint, ctx, *, provisional=False):
    """Receipt and stage consumption commit atomically before tool acknowledgement."""
    tasks = _tasks(ctx)
    command = _command(request, ctx)
    with ctx.store.transaction() as db:
        _fence(db, ctx)
        previous = tasks._replay(db, ctx.operation_id, command)
        if previous is not None:
            return previous
        body, revision = tasks._body(db, ctx.session_id)
        requirements = body.setdefault('delivery_requirements', {'version': 1, 'stages': [], 'history': []})
        stages = requirements['stages']
        if digest(stages) != stages_fingerprint:
            raise Conflict('验收批次在读取后已改变，请重新读取任务再验收')
        record = {'operation_id': ctx.operation_id, 'run_id': ctx.run_id, 'created': now(),
                  'summary': effective.summary, 'artifact_refs': [ref.model_dump() for ref in effective.artifact_refs],
                  'result_refs': [ref.model_dump() for ref in effective.result_refs],
                  'checks': [check.model_dump() for check in effective.checks], 'files': deepcopy(files)}
        record['require_review'] = effective.require_review
        record.update(draft=bool(result.get('data', {}).get('draft')), provisional=provisional)
        if effective.finalize:
            requirements.setdefault('history', []).append({**record, 'consumed_stages': deepcopy(stages),
                'provisional_stages': deepcopy(requirements.get('provisional_stages', [])),
                'finalized': True, 'completeness': result['data']['completeness']})
            if result['data']['completeness'] == 'partial':
                from workbench_platform.resources.reader_features import require_features
                require_features(ctx.store.root.parent, 'partial-goal-continuation-v1')
                requirements['stages'] = [deepcopy(record)]
            else:
                requirements['stages'] = []
                requirements['provisional_stages'] = []
        else:
            target = requirements.setdefault('provisional_stages', []) if provisional else stages
            target.append(record)
            result['data'].update(stage_id=ctx.operation_id, staged_batches=len(stages), provisional=provisional,
                                  provisional_batches=len(requirements.get('provisional_stages', [])),
                                  saved_checks=sum(len(stage['checks']) for stage in stages))
        tasks._save(db, ctx.session_id, body, revision)
        return tasks._receipt(db, ctx.operation_id, command, result)
