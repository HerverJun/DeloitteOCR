"""Verify fixed outputs for ordinary turns without the legacy audit workflow."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote

from .registry import invoke


def remaining_items(results):
    remaining = {}
    for result in results:
        data = result.get('data') or {}
        for failure in data.get('failures', []):
            identifiers = ([failure['failure_key']] if failure.get('failure_key') else
                           failure.get('asset_ids') or ([failure['asset_id']] if failure.get('asset_id') else []))
            for identifier in identifiers or [json.dumps(failure, sort_keys=True, ensure_ascii=False)]:
                remaining[identifier] = failure
        if result.get('status') == 'success':
            for key in data.get('completed_failure_keys', []):
                remaining.pop(key, None)
            completed = list(data.get('completed_asset_ids', []))
            completed.extend(item.get('asset_id') for item in data.get('files', []))
            completed.extend((item.get('source_locator') or {}).get('asset_id') for item in data.get('readable_text', []))
            for identifier in completed:
                remaining.pop(identifier, None)
    return list(remaining.values())


def check_outputs(ctx, references, summary):
    from workbench_platform.application_services.references import verify
    from workbench_platform.artifacts.text_outputs import verify_text_artifact
    from .delivery_jobs import verify_native_jobs
    from .requirement_coverage import project_requirements
    objects, bundles, text_outputs, checked_files = [], [], [], []
    for reference in references:
        ctx.current()
        value = verify(reference, ctx)
        objects.append(value)
        if reference['kind'] != 'artifact':
            continue
        files = []
        for file in value['files']:
            artifact = ctx.services.artifact_center.file(ctx.workspace_id, reference['object_id'], file['name'])
            with ctx.services.artifact_center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                # stage_download verifies the blob; the returned identity fixes
                # the exact bytes and URL displayed by the ordinary turn.
                checked_files.append({'name': file['name'], 'sha256': artifact.blob_hash, 'bytes': path.stat().st_size})
            files.append({**file, 'download_url': f'/api/platform/v1/workspaces/{ctx.workspace_id}/artifacts/{reference["object_id"]}/files/' + quote(file['name'])})
        bundles.append({'bundle_id': reference['object_id'], 'kind': value.get('kind'),
                        'title': value.get('title', '任务成果'), 'files': files})
        text = verify_text_artifact(reference, ctx, bundle=value)
        if text:
            text_outputs.append(text['readable_text'])
    records = [{**row, 'arguments': json.loads(row['arguments']), 'result': json.loads(row['result'])}
               for row in ctx.store.rows('SELECT * FROM operations WHERE run_id=? AND result IS NOT NULL', (ctx.run_id,))]
    coverage = project_requirements(records, references, ctx, verified_objects=objects)
    native_checks, limitations = verify_native_jobs(references, ctx, verified_objects=objects, requirement_coverage=coverage)
    partial = bool(coverage.get('missing') or coverage.get('unverified') or limitations)
    return {'status': 'success', 'summary': summary[:2000],
        'artifact_refs': [ref for ref in references if ref['kind'] == 'artifact'],
        'result_refs': [ref for ref in references if ref['kind'] != 'artifact'], 'job_refs': [],
        'data': {'verified': True, 'finalized': True, 'completeness': 'partial' if partial else 'complete',
                 'delivery': bundles, 'readable_text': text_outputs, 'checked_files': checked_files,
                 'delivery_checks': native_checks, 'requirement_coverage': coverage, 'limitations': limitations}}


async def verify_outputs(runtime, state, references, summary):
    try:
        return await invoke(check_outputs, runtime.context(state), references, summary)
    except (ValueError, KeyError, OSError) as error:
        from .delivery_jobs import NativeDeliveryBlocked
        reason = str(error)[:500] if isinstance(error, NativeDeliveryBlocked) else '输出暂时无法核实，已保存的文件和原操作回执保留，请核对原服务后继续'
        return {'status': 'error', 'summary': reason, 'data': {'verified': False,
            'finalized': False, 'completeness': 'partial'}, 'error': {'code': 'output_unverified', 'message': reason}}


def output_links(content, outputs, workspace_id):
    if not outputs or outputs.get('status') != 'success':
        return content
    data = outputs.get('data') or {}
    bundle = data.get('bundle') or {}
    if not bundle.get('bundle_id'):
        return content
    paths = data.get('files') or []
    mapping = {}
    basenames = {}
    for item in paths:
        relative = item['path'].replace('\\', '/').lstrip('/')
        bundle_id = item.get('bundle_id') or bundle['bundle_id']
        url = f'/api/platform/v1/workspaces/{workspace_id}/artifacts/{bundle_id}/files/' + quote(item['filename'])
        mapping[relative] = url
        basenames.setdefault(relative.rsplit('/', 1)[-1], []).append(url)
    mapping.update({name: urls[0] for name, urls in basenames.items() if len(urls) == 1})
    def replace(match):
        original = match.group(1)
        candidate = unquote(original).replace('\\', '/')
        for prefix in ('sandbox:/mnt/data/', 'sandbox:/', '/mnt/data/'):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        return '](' + mapping.get(candidate, original) + ')'
    return re.sub(r'\]\(([^)]+)\)', replace, content)
