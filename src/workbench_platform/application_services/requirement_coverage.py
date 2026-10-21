"""Versioned, read-only OCR requirement coverage over immutable native attempts.

Attempt failures stay in their original receipts. A successful fixed result can
repair only the same original bytes, input revision, operation and page scope.
"""
from __future__ import annotations

import json
import re

from .identity import digest, RunSuperseded


RULE_VERSION = 'input-requirements-v1'
UNKNOWN_CODES = {'response_unknown', 'unknown_response', 'receipt_unknown', 'native_response_unknown', 'engine_response_unknown'}


def delivered_text_artifacts(references, ctx, *, verified_objects=()):
    """Derive result references only from actually cited, byte-verified TXT."""
    from workbench_platform.artifacts.text_outputs import verify_text_artifact
    bundles = {value['bundle_id']: value for value in verified_objects
               if isinstance(value, dict) and value.get('bundle_id')}
    results = []
    for reference in references:
        ref = _dict(reference)
        if ref.get('kind') != 'artifact' or ref['object_id'] not in bundles:
            continue
        ctx.current()
        value = verify_text_artifact(ref, ctx, bundle=bundles[ref['object_id']])
        ctx.current()
        if value is not None:
            results.append(value)
    return results


def _dict(value):
    return value.model_dump() if hasattr(value, 'model_dump') else dict(value)


def engine_requirements(ctx, observed_engines=()):
    """Use saved user requirements, never a model's fallback tool parameters."""
    run = ctx.store.run(ctx.run_id)
    state = run.get('driver_state') or {}
    if isinstance(state, str):
        state = json.loads(state)
    request = state.get('request') or {}
    if 'engines' in request:
        return tuple(sorted(set(request['engines'] or [])))
    if state.get('execution_contract') == 'clear-general-agent-v1':
        # New goal-sized tools carry explicit input/parameter contracts. Trial
        # engines named for a sample comparison are not requirements to run
        # every engine on every file in the final batch.
        return ()
    tasks = getattr(getattr(ctx.services, 'agent', None), 'tasks', None)
    task = tasks.get(ctx.session_id) if tasks is not None else {}
    explicit = task.get('required_engines')
    if explicit is not None:
        return tuple(sorted(set(explicit)))
    request = task.get('execution_request') or {}
    if 'engines' in request:
        return tuple(sorted(set(request['engines'] or [])))
    # Historical natural-language sessions have no typed requirement field.
    # An explicitly named engine is conservatively retained. Explicit fallback
    # language permits replacement unless the goal also asks for comparison.
    goal = task.get('goal') or ''
    compare = bool(re.search(r'比较|对比|分别|各自|compare|comparison', goal, re.I))
    fallback = bool(re.search(r'后备|备用|备选|降级|失败.{0,12}(?:改用|使用|用)|fallback', goal, re.I))
    named = tuple(sorted(engine for engine in set(observed_engines)
                         if engine and re.search(r'(?<![A-Za-z0-9])' + re.escape(engine) + r'(?![A-Za-z0-9])', goal, re.I)))
    return named if compare or not fallback else ()


def _unit(member, task, *, page=None):
    return {'asset_id': member.asset_id, 'asset_revision': member.asset_revision,
            'blob_hash': member.blob_hash, 'operation': 'extract_text',
            'scope': {'kind': 'page', 'page_number': page} if page is not None else {'kind': 'image'},
            'engine': task.get('engine') or '', 'state': task['status'],
            'failure_code': (task.get('error_details') or {}).get('code'),
            'result_id': task.get('result_id'), 'native_task_id': task.get('id')}


def _base(unit):
    return {key: unit[key] for key in ('asset_id', 'asset_revision', 'blob_hash', 'operation', 'scope')}


def _key(unit, engine=''):
    return digest({**_base(unit), 'required_engine': engine})


class OcrCoverageReader:
    def __init__(self, ctx):
        self.ctx, self.cache = ctx, {}

    def handoff(self, handoff_id):
        if handoff_id in self.cache:
            return self.cache[handoff_id]
        from workbench_platform.application_services.native import scoped_handoff, scoped_set
        ctx, service = self.ctx, self.ctx.services.ocr_handoff
        ctx.current()
        row = scoped_handoff(ctx, service, handoff_id)
        fixed = scoped_set(ctx, row['asset_set_id'], row['asset_set_revision'])
        adapter = service._adapter(service._context(ctx.workspace_id, row['link_id']))
        imported = adapter.import_status(row['request_id'])
        if imported.get('state') != 'completed':
            raise ValueError('固定 OCR 输入映射尚未完成')
        members = {item.item_id: item for item in fixed.items}
        sources = {}
        for item in imported['items']:
            if item.get('status') != 'imported' or item['item_id'] not in members:
                raise ValueError('固定 OCR 输入映射不完整')
            key = (item.get('target_asset_id'), item.get('target_version_id'))
            if key[0]:
                sources[key] = members[item['item_id']]
            if item.get('target_document_id'):
                sources[item['target_document_id']] = members[item['item_id']]
        native = adapter.snapshot()
        images = {image['id']: image for image in native['images']}
        units = {}
        for task in native['tasks']:
            image = images.get(task.get('image_id'), {})
            if image.get('document_id') in sources:
                member = sources[image['document_id']]
                page = image.get('page_number')
                if type(page) is not int or page < 1:
                    continue
                units[task['id']] = _unit(member, task, page=page)
            else:
                key = (task.get('image_id'), task.get('input_version_id') or task.get('version_id'))
                if key in sources:
                    units[task['id']] = _unit(sources[key], task)
        value = {'row': row, 'adapter': adapter, 'sources': sources, 'units': units}
        self.cache[handoff_id] = value
        return value

    def attempt(self, ref):
        ref = _dict(ref)
        value = self.handoff(ref['parent_id'])
        if ref.get('coverage', {}).get('kind') == 'pdf_pages':
            receipt = value['adapter'].page_request(ref['native_id'], ref['object_id'])
            if not receipt.get('submitted'):
                raise ValueError('PDF 原请求回执不可核实')
            member = value['sources'].get(ref['native_id'])
            if member is None:
                raise ValueError('PDF 原件映射不可核实')
            return [_unit(member, {'status': stage['status'], 'id': stage.get('task_id'),
                                  'engine': receipt['parameters'].get('engine') or '',
                                  'result_id': (stage.get('output') or {}).get('result_id')},
                          page=stage['page_number']) for stage in receipt['stages']]
        from workbench_platform.application_services.native import read_job
        job = read_job(self.ctx, ref['object_id'])
        ids = job['native_job_ids']
        if not ids or any(identifier not in value['units'] for identifier in ids):
            raise ValueError('OCR 原尝试与固定输入无法完整映射')
        return [value['units'][identifier] for identifier in ids]

    def reference(self, reference):
        """Map an adopted result through its fixed source and successful task."""
        from packages.contracts.agent import AgentRef
        from .native_mapping import binding, verify_reference, fixed_source_member, adapter_for
        raw = _dict(reference)
        # Handoff-only embeddings have no native-binding repository. Their
        # existing result reader still owns its original verification path.
        value = binding(self.ctx, raw['parent_id']) if callable(getattr(self.ctx.store, 'rows', None)) else None
        if value is None:
            return self.result(raw['parent_id'], raw['object_id'])
        ref = AgentRef.model_validate(raw)
        self.ctx.current()
        verified = verify_reference(self.ctx, ref, value)
        adapter = adapter_for(self.ctx, 'ocr', value['link_id'])
        state = adapter.snapshot()
        ids = value['native_items']
        matches = [task for task in state['tasks'] if task['id'] in ids
                   and task.get('result_id') == ref.object_id and task['status'] == 'succeeded']
        if len(ids) != 1 or len(matches) != 1 or ids[0] != matches[0]['id']:
            raise ValueError('原生绑定结果的唯一成功任务不可核实')
        task = matches[0]
        results = [result for result in adapter.results(ids) if result.native_result_id == ref.object_id]
        version = task.get('input_version_id') or task.get('version_id')
        if (len(results) != 1 or not version or
                (results[0].native_task_id, results[0].image_id, results[0].version_id) !=
                (task['id'], task['image_id'], version)):
            raise ValueError('原生绑定结果与成功任务的输入版本不同')
        images = [image for image in state['images'] if image['id'] == task['image_id']]
        if len(images) != 1:
            raise ValueError('原生绑定结果的输入图像不可核实')
        image, locator = images[0], verified['locator']
        source_key = image.get('document_id') or image['id']
        source = value['sources'].get(source_key)
        if source is None or source['native_id'] != source_key:
            raise ValueError('原生绑定结果的原件映射不同')
        member = fixed_source_member(self.ctx, value, source)
        page = None
        if source['native_kind'] == 'document':
            page = image.get('page_number')
            if (not image.get('document_id') or type(page) is not int or page < 1
                    or locator.get('document_id') != image['document_id']
                    or locator.get('page_number') != page):
                raise ValueError('原生绑定结果的实际页码不同')
        elif source['native_kind'] != 'image' or image.get('document_id') or locator.get('image_id') != image['id']:
            raise ValueError('原生绑定结果的图像来源不同')
        self.ctx.current()
        return _unit(member, task, page=page)

    def result(self, handoff_id, result_id):
        matches = [unit for unit in self.handoff(handoff_id)['units'].values()
                   if unit['result_id'] == result_id and unit['state'] == 'succeeded']
        if len(matches) != 1:
            raise ValueError('成功 OCR 结果的原尝试无法核实')
        return matches[0]


def project_requirements(records, references, ctx, *, verified_objects=()):
    """Return derived coverage and exact repaired operation/job identities.

    Unmapped attempts remain failures in the ordinary delivery check. An
    unavailable reader never licenses erasing a previous failure.
    """
    reader = OcrCoverageReader(ctx)
    delivered, attempts, unreadable = [], {}, []
    for ref in references:
        ref = _dict(ref)
        if ref.get('kind') == 'result' and ref.get('application') == 'ocr' and ref.get('parent_id'):
            try:
                delivered.append(reader.reference(ref))
            except RunSuperseded:
                raise
            except Exception:
                unreadable.append({'result_id': ref['object_id'], 'reason': 'source_mapping_unverified'})
    for value in verified_objects:
        if isinstance(value, dict) and value.get('kind') == 'ocr_result':
            for item in value.get('items', []):
                try:
                    delivered.append(reader.result(value['handoff_id'], item['native_result_id']))
                except RunSuperseded:
                    raise
                except Exception:
                    unreadable.append({'result_id': item['native_result_id'], 'reason': 'source_mapping_unverified'})
    for record in records:
        result = record['result']
        refs = [ref for ref in result.get('job_refs', []) if ref.get('application') == 'ocr'
                and ref.get('parent_id') and ref.get('coverage', {}).get('kind') not in {'fusion', 'visual_review'}]
        # Compatibility with old OCR settlement receipts that omitted job_refs.
        job = (result.get('data') or {}).get('job') or {}
        parent = (result.get('data') or {}).get('handoff_id') or record['arguments'].get('handoff_id')
        if not refs and parent and job.get('platform_job_id') and record['tool'] in {'ocr_run', 'jobs_retry_failed'}:
            refs = [{'application': 'ocr', 'object_id': job['platform_job_id'], 'parent_id': parent}]
        if not refs and parent and record['tool'] == 'ocr_read_results':
            try:
                imported = ctx.services.ocr_handoff.status(ctx.workspace_id, parent)
                processing = imported.get('processing') or {}
                if processing.get('platform_job_id'):
                    refs = [{'application': 'ocr', 'object_id': processing['platform_job_id'], 'parent_id': parent}]
            except RunSuperseded:
                raise
            except Exception:
                unreadable.append({'operation_id': record['id'], 'reason': 'attempt_mapping_unverified'})
        if not refs:
            continue
        units = []
        try:
            for ref in refs:
                units.extend(reader.attempt(ref))
        except RunSuperseded:
            raise
        except Exception:
            unreadable.append({'operation_id': record['id'], 'reason': 'attempt_mapping_unverified'})
            continue
        if units:
            attempts[record['id']] = {'record': record, 'units': units, 'jobs': [ref['object_id'] for ref in refs]}
    engines = engine_requirements(ctx, [unit['engine'] for unit in delivered] +
                                  [unit['engine'] for attempt in attempts.values() for unit in attempt['units']]) if delivered or attempts else ()
    available = {}
    for unit in delivered:
        available.setdefault(_key(unit), unit)
        available.setdefault(_key(unit, unit['engine']), unit)
    requirements, repaired, repaired_jobs = {}, [], set()
    for attempt in attempts.values():
        record, units = attempt['record'], attempt['units']
        # Existing untyped multi-engine submissions conservatively preserve
        # each requested engine. The direct driver saves its user policy.
        requested = record['arguments'].get('engines') or []
        required = engines or (tuple(requested) if len(requested) > 1 else ())
        required_keys = set()
        failed_keys = set()
        for unit in units:
            for engine in required or ('',):
                key = _key(unit, engine)
                requirements[key] = {**_base(unit), 'required_engine': engine}
                required_keys.add(key)
                if unit['state'] in {'failed', 'cancelled', 'interrupted'}:
                    failed_keys.add(key)
        unknown = ((record['result'].get('error') or {}).get('code') in UNKNOWN_CODES
                   or any(unit.get('failure_code') in UNKNOWN_CODES for unit in units))
        if failed_keys and failed_keys <= available.keys() and not unknown:
            used = sorted({available[key]['result_id'] for key in failed_keys})
            repaired.append({'operation_id': record['id'], 'tool': record['tool'],
                             'repaired_by': used[0], 'result_ids': used,
                             'rule_version': RULE_VERSION})
            if required_keys <= available.keys():
                repaired_jobs.update(attempt['jobs'])
    # A delivered immutable result is also a verifiable requirement, including
    # when an older receipt has no job reference.
    for unit in delivered:
        for engine in engines or ('',):
            requirements.setdefault(_key(unit, engine), {**_base(unit), 'required_engine': engine})
    missing = sorted(set(requirements) - available.keys())
    return {'rule_version': RULE_VERSION, 'total': len(requirements),
            'satisfied': len(requirements) - len(missing), 'missing': len(missing),
            'requirements_manifest_sha256': digest(sorted(requirements.items())),
            'missing_requirements': [requirements[key] for key in missing[:50]],
            'missing_requirements_truncated': len(missing) > 50,
            'unverified': unreadable[:50], 'repaired_operations': repaired,
            'repaired_job_ids': sorted(repaired_jobs)}
