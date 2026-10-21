"""Bounded text reads from fixed native results and verified artifact bytes."""
from __future__ import annotations

import codecs
import hashlib

from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.artifacts.store import ArtifactUnavailable
from workbench_platform.artifacts.text_outputs import text_page, verified_text
from .identity import RunSuperseded
from .errors import NativeReaderUnavailable
from .references import asset_allowed, verify


TEXT_FORMATS = {'text/plain': 'txt', 'text/csv': 'csv', 'application/json': 'json',
                'text/markdown': 'md', 'text/html': 'html'}
TEXT_MEDIA = frozenset(TEXT_FORMATS)


class TextReadFault(Exception):
    def __init__(self, code, message, action, *, data=None, status='error'):
        self.code, self.message, self.action = code, message, action
        self.data, self.status = data or {}, status


def _fixed_file(artifact, *, parent_id=None):
    return AgentRef(kind='artifact', workspace_id=artifact.workspace_id,
        object_id=artifact.artifact_id, revision=artifact.revision,
        sha256=artifact.blob_hash, parent_id=parent_id)


def _text_format(artifact, catalog_name=None, *, data_csv=False):
    if artifact.media_type in TEXT_MEDIA:
        return TEXT_FORMATS[artifact.media_type]
    # Windows historically labels Data CSV as Excel. Only an exact stored Data
    # binding can disambiguate it; arbitrary bundles/renames cannot opt in.
    if (data_csv and artifact.media_type == 'application/vnd.ms-excel' and isinstance(catalog_name, str)
            and catalog_name.lower().endswith('.csv')):
        return 'csv'
    return None


def _scope(artifact, ctx, bundle=None):
    sources = {source.asset_id for source in artifact.source_refs}
    if bundle:
        sources.update(item['asset_id'] for item in bundle.get('inputs', []) if item.get('asset_id'))
        sources.update(bundle.get('source_assets', []))
    if (ctx.selected_assets and not sources) or any(not asset_allowed(ctx, item) for item in sources):
        raise TextReadFault('artifact_scope_denied', '成果来源不在当前任务允许的资料范围，未读取内容',
            '回到当前任务的成果目录选择已授权成果；需要其它资料时先明确增加任务范围。')


def _choices(bundle, ctx):
    center = ctx.services.artifact_center
    files = []
    for item in bundle['files'][:50]:
        artifact = center.file(ctx.workspace_id, bundle['bundle_id'], item['name'])
        reader = getattr(center, 'read_file_context', None)
        context = reader(ctx.workspace_id, artifact.artifact_id) if (
            artifact.media_type == 'application/vnd.ms-excel' and callable(reader)) else None
        data_csv = bool(context and context['bundle_id'] == bundle['bundle_id'] and context['file_name'] == item['name'])
        files.append({'name': item['name'], 'media_type': artifact.media_type,
            'size_bytes': artifact.size_bytes,
            'readable_text': _text_format(artifact, item['name'], data_csv=data_csv) is not None,
            'reference': _fixed_file(artifact, parent_id=bundle['bundle_id']).model_dump(mode='json')})
    return {'files': files, 'files_total': len(bundle['files']), 'files_truncated': len(bundle['files']) > len(files),
            'bundle_id': bundle['bundle_id']}


def _artifact(reference, ctx, file_name):
    center = ctx.services.artifact_center
    bundle = None
    try:
        artifact = center.store.get(ctx.workspace_id, reference.object_id)
    except ArtifactUnavailable as error:
        if error.code != 'NOT_FOUND':
            raise
        # Artifact references historically identify bundles. Preserve that
        # meaning and require an actual file choice instead of guessing CSV.
        bundle = verify(reference.model_dump(mode='json'), ctx)
        choices = _choices(bundle, ctx)
        if not file_name or not any(item['name'] == file_name for item in bundle['files']):
            raise TextReadFault('artifact_file_required', '此引用是成果包，请从真实文件目录选择要读取的文本文件',
                '用相同reference和目录中的file_name再次调用results_read_text，或直接使用文件reference；支持UTF-8 TXT/CSV/JSON/Markdown/HTML，只读取文字，不执行标记或脚本。',
                data=choices, status='unsupported')
        artifact = center.file(ctx.workspace_id, reference.object_id, file_name)
    else:
        _scope(artifact, ctx)
        if reference.parent_id:
            bundle_ref = AgentRef(kind='artifact', workspace_id=ctx.workspace_id,
                                  object_id=reference.parent_id, revision=1)
            bundle = center.get(ctx.workspace_id, bundle_ref.object_id)
            if not any(item['artifact_id'] == artifact.artifact_id for item in bundle['files']):
                raise TextReadFault('artifact_parent_mismatch', '文件不属于引用中的成果包，未读取内容',
                    '使用artifacts_get读取原成果包文件目录，并采用目录提供的文件身份。')
        else:
            # The shared catalog resolves exact stored file ownership; neither
            # a model-provided filename nor a CSV header chooses a Data schema.
            context_reader = getattr(center, 'read_file_context', None)
            file_context = context_reader(ctx.workspace_id, artifact.artifact_id) if callable(context_reader) else None
            if file_context:
                bundle = center.get(ctx.workspace_id, file_context['bundle_id'])
                if not any(item['artifact_id'] == artifact.artifact_id for item in bundle['files']):
                    raise TextReadFault('artifact_parent_mismatch', '成果目录无法核实文件绑定，未读取内容',
                        '使用artifacts_get核对原成果目录，不要更换文件身份猜测内容。')
        _scope(artifact, ctx, bundle)
        fixed = _fixed_file(artifact, parent_id=reference.parent_id)
        if type(reference.revision) is not int or reference.revision != artifact.revision:
            raise TextReadFault('artifact_revision_mismatch', '文件引用版本与登记的固定版本不同，未读取内容',
                '核对原成果目录的版本；只有确认需要该固定文件后才使用返回的file_reference读取。',
                data={'file_reference': fixed.model_dump(mode='json')})
        if reference.sha256 is not None and reference.sha256 != artifact.blob_hash:
            raise TextReadFault('artifact_hash_mismatch', '文件引用哈希与登记内容不同，未读取内容',
                '核对原成果目录的SHA256，不要删掉哈希或改用其它成果来绕过原版本检查。',
                data={'file_reference': fixed.model_dump(mode='json')})
        if file_name is not None:
            raise TextReadFault('artifact_file_name_unnecessary', '此引用已经是文件身份，无需file_name',
                '删除file_name，用相同文件reference与offset/limit调用results_read_text。',
                data={'file_reference': fixed.model_dump(mode='json')})
    _scope(artifact, ctx, bundle)
    fixed = _fixed_file(artifact, parent_id=bundle['bundle_id'] if bundle else None)
    details = {'file_reference': fixed.model_dump(mode='json'), 'media_type': artifact.media_type,
               'size_bytes': artifact.size_bytes, 'file_name': file_name}
    if bundle:
        details['bundle_id'] = bundle['bundle_id']
        details['file_name'] = next(item['name'] for item in bundle['files'] if item['artifact_id'] == artifact.artifact_id)
        if bundle.get('table_schema') is not None:
            details['table_schema'] = bundle['table_schema']
            details['structured_read_tools'] = ['data_read_table', 'data_read_result']
            if bundle.get('session_id'):
                details['data_session_id'] = bundle['session_id']
    reader = getattr(center, 'read_file_context', None)
    context = reader(ctx.workspace_id, artifact.artifact_id) if (
        artifact.media_type == 'application/vnd.ms-excel' and callable(reader)) else None
    data_csv = bool(context and bundle and context['bundle_id'] == bundle['bundle_id']
                    and context['file_name'] == details['file_name'])
    details['text_format'] = _text_format(artifact, details['file_name'], data_csv=data_csv)
    if details['text_format'] is None:
        raise TextReadFault('artifact_text_unsupported', '此成果是二进制或不受支持的文本格式，未按文字解码',
            '从原成果包选择UTF-8 TXT/CSV/JSON/Markdown/HTML文件。需要Data业务原值时使用data_read_table或data_read_result，Parquet/XLSX通过原下载入口读取。',
            data={**details, **({'available_files': _choices(bundle, ctx)} if bundle else {})}, status='unsupported')
    return artifact, details


def _page(stream, *, offset, limit, expected_hash, expected_size):
    # Validate the entire UTF-8 stream and its original hash while retaining
    # at most one requested character page, including for a very large CSV.
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
    total = 0
    size = 0
    pieces = []
    file_hash, text_hash = hashlib.sha256(), hashlib.sha256()
    first, bom_removed = True, False
    while True:
        raw = stream.read(65536)
        if first:
            bom_removed, first = raw.startswith(codecs.BOM_UTF8), False
        size += len(raw)
        file_hash.update(raw)
        text = decoder.decode(raw, final=not raw)
        if '\0' in text:
            raise UnicodeError('NUL is not supported in a text artifact')
        text_hash.update(text.encode('utf-8'))
        start, end = max(0, offset - total), min(len(text), offset + limit - total)
        if end > start:
            pieces.append(text[start:end])
        total += len(text)
        if not raw:
            break
    if file_hash.hexdigest() != expected_hash or size != expected_size:
        raise ArtifactUnavailable('CORRUPT')
    end = min(total, offset + limit)
    return {'text': ''.join(pieces), 'text_total_chars': total, 'text_offset': offset,
        'text_next_offset': end if end < total else None, 'text_truncated': offset > 0 or end < total,
        'text_sha256': text_hash.hexdigest(), 'file_sha256': file_hash.hexdigest(),
        'encoding': 'utf-8', 'utf8_bom_removed': bom_removed, 'content_verified': True}


def read_text(reference, ctx, *, offset=0, limit=8000, file_name=None):
    """Return an authored repair action for every supported reference failure."""
    try:
        ref = AgentRef.model_validate(reference)
        if ref.workspace_id != ctx.workspace_id:
            raise TextReadFault('text_workspace_denied', '引用不属于当前项目，未读取内容',
                '使用当前任务的成果或固定结果引用；跨项目资料须先按项目授权流程处理。')
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 32000:
            raise TextReadFault('text_page_invalid', '文字分页参数无效',
                'offset使用不小于0的字符位置，limit使用1到32000；下一页采用text_next_offset。')
        if hasattr(ctx, 'current'):
            ctx.current()
        if ref.kind == 'result' and ref.application == 'ocr':
            if file_name is not None:
                raise TextReadFault('text_file_name_invalid', 'OCR/PDF固定结果不使用file_name',
                    '删除file_name并保留同一固定OCR/PDF结果reference。')
            value = verify(ref.model_dump(mode='json'), ctx)
            result = AgentToolResult(status='success', summary='已读取固定版本原文；字符分页与全文长度明确',
                data={**text_page(verified_text(value), offset=offset, limit=limit),
                    'result_id': ref.object_id, 'revision': ref.revision, 'source_locator': value['locator']}, result_refs=[ref])
        elif ref.kind == 'artifact':
            artifact, details = _artifact(ref, ctx, file_name)
            try:
                with ctx.services.artifact_center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                        expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                    with path.open('rb') as stream:
                        page = _page(stream, offset=offset, limit=limit,
                                     expected_hash=artifact.blob_hash, expected_size=artifact.size_bytes)
            except UnicodeError:
                raise TextReadFault('artifact_encoding_unsupported', '成果字节不是受支持的UTF-8文本，未返回部分或替换字符',
                    '使用原下载文件核对编码，或从成果目录选择UTF-8 TXT/CSV/JSON；不要把二进制文件改称文本。',
                    data=details, status='unsupported') from None
            result = AgentToolResult(status='success', summary='已按固定文件版本和SHA256读取UTF-8原导出文字；只返回请求字符页',
                data={**page, **details, 'revision': artifact.revision,
                      'source_locators': [source.model_dump(mode='json') for source in artifact.source_refs[:50]],
                      'source_locators_total': len(artifact.source_refs),
                      'source_locators_truncated': len(artifact.source_refs) > 50},
                warnings=['CSV内容是原导出展示字节；保留安全转义和source_row等来源列，业务原值/列语义以Data结构接口为准。']
                    if details['text_format'] == 'csv' else [])
        else:
            alternatives = ['results_describe']
            if ref.application == 'data':
                alternatives = ['data_read_table' if ref.kind == 'table' else 'data_read_result']
            raise TextReadFault('text_reference_unsupported', '此引用不能作为全文或成果文本文件读取',
                'results_read_text接受固定OCR/PDF result引用，或kind=artifact的UTF-8成果文件。Data表/比较结果请使用结构化读取工具。',
                data={'supported_references': ['ocr/result', 'artifact: UTF-8 TXT/CSV/JSON'],
                      'alternative_tools': alternatives}, status='unsupported')
        if hasattr(ctx, 'current'):
            ctx.current()
        return result.model_dump(mode='json')
    except RunSuperseded:
        raise
    except NativeReaderUnavailable:
        # Keep service preparation distinct from an invalid fixed reference;
        # the shared tool boundary projects the authored recovery guidance.
        raise
    except TextReadFault as error:
        return AgentToolResult(status=error.status, summary=error.message,
            error={'code': error.code, 'message': error.message, 'retryable': False},
            data={**error.data, 'failure': {'code': error.code, 'stage': 'text_read',
                'retryable': False, 'user_action': error.action}}).model_dump(mode='json')
    except ArtifactUnavailable as error:
        return AgentToolResult(status='error', summary='固定成果文件不可读取，未生成替代内容',
            error={'code': 'artifact_text_unavailable', 'message': '固定成果文件不存在、不可用或完整性核对失败', 'retryable': False},
            data={'failure': {'code': 'artifact_text_unavailable', 'stage': 'text_read',
                'reason': error.code, 'retryable': False,
                'user_action': '用artifacts_get核对原成果包及文件状态；保留原版本，不要重新导出或重算来猜测成功。'}}).model_dump(mode='json')
    except (ValueError, KeyError):
        return AgentToolResult(status='error', summary='固定引用的版本、内容或资料范围不可核实，未读取文字',
            error={'code': 'text_reference_unavailable', 'message': '固定引用的版本、内容或资料范围不可核实', 'retryable': False},
            data={'failure': {'code': 'text_reference_unavailable', 'stage': 'text_read', 'retryable': False,
                'user_action': '从当前任务的原成果或固定结果目录核对reference及版本；成果包需同时给file_name，文件artifact ID可直接读取。'}}).model_dump(mode='json')
