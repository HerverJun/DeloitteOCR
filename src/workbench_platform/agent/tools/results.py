"""Typed, deterministic result-to-input bridges; models never retype table data."""
import csv
import io
import json
import hashlib
import os
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult
from packages.contracts.uploads import UploadBinding
from workbench_platform.application_services.references import verify
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import canonical, digest
from workbench_platform.supervisor.processes import require_f_path


class Describe(AgentModel):
    reference: AgentRef
    table_index: int | None = Field(default=None, ge=0, le=99)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=50)


class ToInput(Describe):
    file_name: str | None = Field(default=None, min_length=1, max_length=240)


class TextRead(AgentModel):
    reference: AgentRef
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=32000)
    file_name: str | None = Field(default=None, min_length=1, max_length=240)


def read_text(args, ctx):
    from workbench_platform.application_services.result_text import read_text as read_fixed_text
    return read_fixed_text(args.reference, ctx, offset=args.offset, limit=args.limit, file_name=args.file_name)


def describe(args, ctx):
    if args.reference.application == 'multimodal' and args.reference.kind == 'result':
        from .multimodal import read, Result
        verify(args.reference.model_dump(), ctx)
        return read(Result(job_id=args.reference.parent_id, snapshot_id=args.reference.object_id,
                           offset=args.offset, limit=args.limit), ctx)
    value = verify(args.reference.model_dump(), ctx)
    if args.reference.kind in {'artifact', 'asset', 'asset_set', 'evidence'}:
        data = value
        if args.reference.kind == 'evidence':
            from .audit import evidence_data
            data = evidence_data(value)
    elif args.reference.kind == 'result' and args.reference.application == 'ocr':
        tables = value['table_snapshot']['tables']
        catalog = [{'table_index': index, 'rows': table['rows'], 'columns': table['columns'],
                    'caption': table.get('caption', ''), 'has_spans': any(cell['row_span'] != 1 or cell['column_span'] != 1 for cell in table['cells'])}
                   for index, table in enumerate(tables)]
        data = {'tables': catalog, 'source_locator': value['locator'], 'adopted': value['selected'],
                'table_source': value['table_snapshot']['source'], 'warnings': value['table_snapshot']['warnings']}
        if args.table_index is not None:
            if args.table_index >= len(tables):
                raise ValueError('表索引超出实际结构目录')
            table = tables[args.table_index]
            data['sample_cells'] = table['cells'][:30]
    elif args.reference.kind in {'table', 'result'} and args.reference.application == 'data':
        data = value
    elif args.reference.kind == 'result' and args.reference.application == 'classification':
        data = {key: value[key] for key in ('model_id', 'input_set_id', 'items')}
        data['items'] = data['items'][:50]
    elif args.reference.kind == 'result' and args.reference.application == 'forensics':
        data = {key: value[key] for key in ('inspection_id', 'tools', 'files', 'report_hash')}
    else:
        raise ValueError('引用类型不能作为结果描述')
    return AgentToolResult(status='success', summary='已核实类型引用并读取真实结构，数据只来自原生结果', data=data,
                           result_refs=[args.reference]).model_dump()


def install(payload, name, media_type, sources, leaves, provenance, ctx):
    from workbench_platform.application_services.references import leaf_assets
    leaves = sorted({leaf for asset_id in leaves for leaf in leaf_assets(ctx, asset_id)})
    if not leaves:
        raise ValueError('衔接输入必须有可核实的原始资料来源')
    registry = ctx.services.asset_library[0]
    uploads = require_f_path(registry.roots['uploads'])
    target = uploads / ('.bridge-' + ctx.operation_id + '.tmp')
    sha256 = hashlib.sha256(payload).hexdigest()
    binding = UploadBinding(schema_version='1.0', workspace_id=ctx.workspace_id, request_id=ctx.operation_id,
                            name=name, sha256=sha256, size_bytes=len(payload))
    registry.reserve_upload(binding)
    try:
        with target.open('wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        status, _ = registry.commit_upload(binding, target, media_type=media_type)
    finally:
        target.unlink(missing_ok=True)
    asset = status.receipt.asset
    # Persist literal cell provenance separately from the model-sized summary.
    folder = require_f_path(ctx.store.root / 'bridges')
    folder.mkdir(exist_ok=True)
    provenance_payload = canonical({'asset_id': asset.asset_id, 'input_sha256': sha256,
                                   'sources': sources, 'provenance': provenance}).encode('utf-8')
    path = folder / (ctx.operation_id + '.json')
    if path.exists() and path.read_bytes() != provenance_payload:
        raise ValueError('衔接来源身份变化')
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(provenance_payload)
    os.replace(temporary, path)
    with ctx.store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO derived_inputs VALUES(?,?,?,?,?,?,?)',
            (ctx.workspace_id, asset.asset_id, canonical(sources), canonical(sorted(set(leaves))),
             path.relative_to(ctx.store.root).as_posix(), hashlib.sha256(provenance_payload).hexdigest(), ctx.operation_id))
    return AgentToolResult(status='success', summary='已从真实结构生成受控下游输入，保留原始值和来源，未让主控重录数据',
        data={'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash,
              'original_name': asset.original_name, 'provenance_hash': hashlib.sha256(provenance_payload).hexdigest()},
        result_refs=[AgentRef(kind='asset', workspace_id=ctx.workspace_id, object_id=asset.asset_id,
            revision=asset.revision, sha256=asset.blob_hash, sources=[AgentRef.model_validate(source) for source in sources])]).model_dump()


def to_input(args, ctx):
    value = verify(args.reference.model_dump(), ctx)
    ref = args.reference
    if ref.application == 'multimodal' and ref.kind == 'result':
        from .multimodal import to_data, Result
        return to_data(Result(job_id=ref.parent_id, snapshot_id=ref.object_id), ctx)
    if ref.kind == 'asset':
        return AgentToolResult(status='success', summary='此引用已是可用输入', result_refs=[ref], data=value).model_dump()
    if ref.kind == 'asset_set':
        return AgentToolResult(status='success', summary='此集合可直接交给原生工具', result_refs=[ref], data=value).model_dump()
    if ref.application == 'ocr' and ref.kind == 'result':
        tables = value['table_snapshot']['tables']
        if args.table_index is None or args.table_index >= len(tables):
            raise ValueError('请从实际结构目录选择table_index，缺表不能生成输入')
        table = tables[args.table_index]
        if not 1 <= table['columns'] <= 256 or table['rows'] * table['columns'] > 1_000_000:
            raise ValueError('表超过下游限制')
        if any(cell['row_span'] != 1 or cell['column_span'] != 1 for cell in table['cells']):
            return AgentToolResult(status='unsupported', summary='此表包含合并单元格，不能猜测金额重复/展开语义；请先在原生编辑器明确表结构').model_dump()
        rows = [['' for _ in range(table['columns'])] for _ in range(table['rows'])]
        occupied = set()
        for cell in table['cells']:
            position = (cell['row'], cell['column'])
            if (position in occupied or not 0 <= position[0] < table['rows'] or not 0 <= position[1] < table['columns']
                    or not isinstance(cell['text'], str)):
                raise ValueError('真实表格单元格坐标或原文无效')
            occupied.add(position)
            rows[cell['row']][cell['column']] = cell['text']
        if len(occupied) != table['rows'] * table['columns']:
            raise ValueError('表格结构不完整，不能补猜缺失单元格')
        stream = io.StringIO(newline='')
        csv.writer(stream).writerows(rows)
        source = value['source_asset']
        provenance = {'result_id': ref.object_id, 'result_revision': ref.revision, 'table_index': args.table_index,
                      'source_locator': value['locator'], 'source_asset': source,
                      'cells': table['cells'], 'inference_selected': value['selected'],
                      'table_source': value['table_snapshot']['source'], 'tables_sha256': value['table_snapshot']['tables_sha256'],
                      'financial_values_are_original_literals': True}
        return install(stream.getvalue().encode('utf-8-sig'), 'ocr-table-' + str(args.table_index + 1) + '.csv',
            'text/csv', [ref.model_dump()], [source['asset_id']], provenance, ctx)
    if ref.kind == 'artifact':
        # ClearData's CSV is escaped for spreadsheet safety. Parquet retains
        # canonical raw literals, including formulas and long business IDs.
        file_name = args.file_name
        if not file_name or not file_name.lower().endswith('.parquet') or not any(file['name'] == file_name for file in value['files']):
            return AgentToolResult(status='unsupported', summary='文件衔接请选择成果目录中的Parquet原值文件；展示CSV的安全转义不能作为业务原值').model_dump()
        center = ctx.services.artifact_center
        artifact = center.file(ctx.workspace_id, ref.object_id, file_name)
        with center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            payload = path.read_bytes()
        leaves = [item['asset_id'] for item in value.get('inputs', []) if item.get('asset_id')]
        return install(payload, file_name, 'application/vnd.apache.parquet', [ref.model_dump()], leaves,
                       {'bundle_id': ref.object_id, 'file_name': file_name, 'blob_hash': artifact.blob_hash,
                        'inputs': value.get('inputs', [])}, ctx)
    return AgentToolResult(status='unsupported', summary='此引用暂不支持直接转文件；分类预测可用assets_select_from_results，数据表先导出Parquet再衔接').model_dump()


def register(registry):
    for name, schema, handler, description, effect in [
        ('results_read_text', TextRead, read_text, '按固定引用有界读取OCR/PDF原文或本项目UTF-8 TXT/CSV/JSON/Markdown/HTML成果文件；HTML只返回源文字，不执行。文件artifact ID可直接读取，bundle引用须用file_name选择文件。offset/limit按字符，limit<=32000；返回总长、下一位置、原文件SHA与固定版本。CSV保留原导出展示字节；业务原值优先data_read_table/data_read_result。', 'read'),
        ('results_describe', Describe, describe, '核实类型化原生结果、表索引与来源；先查询真实表，不能按文字编造表。', 'read'),
        ('results_to_input', ToInput, to_input, 'OCR原生结构表确定性转为受控CSV输入；数据成果用Parquet原值文件；保持长编号、公式字面量、页/表/单元格来源。合并表不猜测展开。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            input_kinds=('result', 'artifact') if name == 'results_read_text' else ('result', 'artifact', 'asset_set'),
            output_kinds=('result', 'artifact') if name == 'results_read_text' else ('asset', 'asset_set')))
