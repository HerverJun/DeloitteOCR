"""Complete, fixed OCR text with bounded explicit previews and UTF-8 downloads."""
from __future__ import annotations

from copy import copy
from dataclasses import is_dataclass, replace
import hashlib
import json
from urllib.parse import quote

from packages.contracts.agent import AgentRef
from workbench_platform.application_services.identity import canonical, digest


TEXT_RULE_VERSION = 'ocr-readable-text-v1'


def text_page(text, *, offset=0, limit=8000):
    if not isinstance(text, str) or type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 32000:
        raise ValueError('全文分页参数无效')
    end = min(len(text), offset + limit)
    return {'text': text[offset:end], 'text_total_chars': len(text), 'text_offset': offset,
            'text_next_offset': end if end < len(text) else None,
            'text_truncated': offset > 0 or end < len(text),
            'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def verified_text(value):
    snapshot = value.get('snapshot') or {}
    edited = snapshot.get('edited') or {}
    if snapshot.get('available') is not True or not isinstance(edited.get('text'), str):
        raise ValueError('固定 OCR 版本的完整文字不可读取')
    return edited['text']


def verify_text_artifact(reference, ctx, *, bundle):
    """Recover the fixed result only from a genuine, complete text sidecar.

    A title, summary, filename or provenance claim alone cannot satisfy OCR
    coverage. The native version and exact full TXT bytes must agree too.
    This reader never publishes, repairs or replaces an artifact.
    """
    from workbench_platform.application_services.references import verify
    ref = AgentRef.model_validate(reference)
    if ref.kind != 'artifact' or ref.workspace_id != ctx.workspace_id or bundle.get('bundle_id') != ref.object_id:
        raise ValueError('原文成果与本次固定引用身份不同')
    names = {file['name'] for file in bundle.get('files', [])}
    if bundle.get('kind') != 'native_export' or not {'ocr-text.txt', 'text-provenance.json'} <= names:
        return None
    center = ctx.services.artifact_center
    provenance_file = center.file(ctx.workspace_id, ref.object_id, 'text-provenance.json')
    with center.store.stage_download(ctx.workspace_id, provenance_file.artifact_id,
            expected_revision=provenance_file.revision, expected_hash=provenance_file.blob_hash) as path:
        if path.stat().st_size > 256 * 1024:
            raise ValueError('固定原文来源记录超过核验范围')
        provenance = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(provenance, dict):
        raise ValueError('固定原文来源记录不是有效对象')
    if provenance.get('rule_version') != TEXT_RULE_VERSION:
        return None
    original = AgentRef.model_validate(provenance['reference'])
    if original.kind != 'result' or original.application != 'ocr' or original.workspace_id != ctx.workspace_id:
        raise ValueError('原文成果没有固定OCR/PDF结果引用')
    value = verify(original.model_dump(mode='json'), ctx)
    text = verified_text(value)
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    identity = {'rule_version': TEXT_RULE_VERSION, 'reference': original.model_dump(), 'text_sha256': text_hash}
    if ref.object_id != 'b' + digest(identity)[:48]:
        raise ValueError('原文成果发布身份与固定结果不同')
    source = value['source_asset']
    locator = {**source['source_locator']}
    if value.get('locator', {}).get('page_number'):
        locator.update(kind='page', page_number=value['locator']['page_number'], precision='page_only')
    if (provenance.get('text_sha256') != text_hash or provenance.get('text_total_chars') != len(text)
            or provenance.get('source') != source or provenance.get('source_locator') != locator
            or (original.coverage.get('page_number') is not None
                and original.coverage['page_number'] != value.get('locator', {}).get('page_number'))):
        raise ValueError('原文成果的全文、原件版本或页码来源不同')
    text_file = center.file(ctx.workspace_id, ref.object_id, 'ocr-text.txt')
    expected = text.encode('utf-8')
    if (text_file.blob_hash, text_file.size_bytes) != (text_hash, len(expected)):
        raise ValueError('成果文件不是固定版本的完整原文')
    with center.store.stage_download(ctx.workspace_id, text_file.artifact_id,
            expected_revision=text_file.revision, expected_hash=text_file.blob_hash) as path:
        if path.read_bytes() != expected:
            raise ValueError('成果文件与完整原文逐字节核对失败')
    return {'reference': original, 'verified': value,
        'readable_text': {'result_id': original.object_id, 'revision': original.revision,
            'reference': original.model_dump(mode='json'), **text_page(text), 'source_locator': locator,
            'download_url': f'/api/platform/v1/workspaces/{ctx.workspace_id}/artifacts/{ref.object_id}/files/ocr-text.txt'},
        'proof': {'bundle_id': ref.object_id, 'result_id': original.object_id,
            'result_revision': original.revision, 'result_sha256': original.sha256,
            'text_sha256': text_hash, 'text_total_chars': len(text),
            'source_asset_id': source['asset_id'], 'source_asset_revision': source['asset_revision'],
            'source_blob_hash': source['blob_hash'], 'scope': locator}}


def publish_text_result(ctx, reference, *, verified=None):
    """Publish a sidecar without mutating the cited JSON, version or old bundle.

    The bundle identity belongs to the fixed result and text content, rather
    than the delivery operation. Repeated delivery and recovery reuse it.
    """
    from workbench_platform.application_services.references import verify
    from .native_exports import publish
    if hasattr(ctx, 'current'):
        ctx.current()
    ref = AgentRef.model_validate(reference.model_dump() if hasattr(reference, 'model_dump') else reference)
    if ref.kind != 'result' or ref.application != 'ocr' or ref.workspace_id != ctx.workspace_id:
        raise ValueError('文字交付必须引用本项目固定 OCR 结果')
    value = verified if verified is not None else verify(ref.model_dump(), ctx)
    text = verified_text(value)
    source = value['source_asset']
    locator = {**source['source_locator']}
    if value.get('locator', {}).get('page_number'):
        locator.update(kind='page', page_number=value['locator']['page_number'], precision='page_only')
    identity = {'rule_version': TEXT_RULE_VERSION, 'reference': ref.model_dump(),
                'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    operation_id = 'o' + digest(identity)[:48]
    output_ctx = replace(ctx, operation_id=operation_id) if is_dataclass(ctx) else copy(ctx)
    if not is_dataclass(ctx):
        output_ctx.operation_id = operation_id
    inputs = [{'asset_id': source['asset_id'], 'revision': source['asset_revision'],
               'blob_hash': source['blob_hash'], 'source_locator': locator}]
    provenance = {**identity, 'source': source, 'source_locator': locator, 'text_total_chars': len(text)}
    if hasattr(ctx, 'current'):
        ctx.current()
    bundle = publish(output_ctx, 'OCR 原文', {
        'ocr-text.txt': (text.encode('utf-8'), 'text/plain'),
        'text-provenance.json': (canonical(provenance).encode('utf-8'), 'application/json'),
    }, inputs, [locator], TEXT_RULE_VERSION)
    base = f'/api/platform/v1/workspaces/{ctx.workspace_id}/artifacts/{bundle["bundle_id"]}/files/'
    delivered = {**bundle, 'files': [{**file, 'download_url': base + quote(file['name'])} for file in bundle['files']]}
    readable = {'result_id': ref.object_id, 'revision': ref.revision, 'reference': ref.model_dump(),
                **text_page(text), 'source_locator': locator, 'download_url': base + 'ocr-text.txt'}
    return {'readable_text': readable, 'delivery': delivered,
            'artifact_ref': AgentRef(kind='artifact', workspace_id=ctx.workspace_id,
                                    object_id=bundle['bundle_id'], revision=1).model_dump()}
