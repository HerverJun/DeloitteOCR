"""Paged literal source/result reading and explicit OCR table concatenation."""
import csv
import io
from pathlib import Path
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.application_services.references import verify
from .results import install


class Read(AgentModel):
    reference: AgentRef
    page_number: int | None = Field(default=None, ge=1)
    table_index: int | None = Field(default=None, ge=0, le=99)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12000, ge=1, le=20000)


class Table(AgentModel):
    reference: AgentRef
    table_index: int = Field(ge=0, le=99)
    header_rows: int = Field(default=1, ge=0, le=5)


class Concat(AgentModel):
    tables: list[Table] = Field(min_length=2, max_length=30)
    file_name: str = Field(default='combined-ocr-tables.csv', min_length=1, max_length=240, pattern=r'^[^/\\]+\.csv$')


def read(args, ctx):
    value = verify(args.reference.model_dump(), ctx)
    ref = args.reference
    if ref.kind == 'asset':
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, ref.object_id)
        if asset.size_bytes > 32*1024*1024:
            return AgentToolResult(status='unsupported', summary='单文件超过原文读取限制，请用结构化导入或按页处理').model_dump()
        suffix = Path(asset.original_name).suffix.lower()
        with ctx.services.asset_library[0].stage_verified(ctx.workspace_id, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash) as path:
            if suffix == '.pdf':
                if args.page_number is None:
                    raise ValueError('PDF原文须明确页码')
                from pypdf import PdfReader
                reader = PdfReader(path)
                if reader.is_encrypted or args.page_number > len(reader.pages):
                    raise ValueError('PDF页码或加密状态不支持')
                text = reader.pages[args.page_number-1].extract_text() or ''
            elif suffix in {'.txt', '.md', '.csv', '.tsv', '.json', '.log', '.xml', '.html'}:
                text = None
                for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
                    try:
                        text = path.read_text(encoding=encoding); break
                    except UnicodeError:
                        continue
                if text is None:
                    raise ValueError('文本编码不可读取')
            else:
                return AgentToolResult(status='unsupported', summary='此格式需现有数据导入或OCR工具，不能猜测原文').model_dump()
        data = {'text': text[args.offset:args.offset+args.limit], 'total_chars': len(text), 'offset': args.offset,
                'page_number': args.page_number, 'name': asset.original_name}
        next_offset = args.offset+args.limit if args.offset+args.limit < len(text) else None
    elif ref.kind == 'result' and ref.application == 'ocr':
        edited = value['snapshot']['edited']
        if args.table_index is not None:
            tables = value['table_snapshot']['tables']
            if args.table_index >= len(tables):
                raise ValueError('实际结果没有该表')
            table = tables[args.table_index]; cells = table['cells']
            # Cell pages use a bounded count regardless of the text char limit.
            data = {'table_index': args.table_index, 'rows': table['rows'], 'columns': table['columns'],
                    'cells': cells[args.offset:args.offset+min(args.limit, 50)], 'total_cells': len(cells), 'source_locator': value['locator']}
            next_offset = args.offset+min(args.limit, 50) if args.offset+min(args.limit, 50) < len(cells) else None
        else:
            text = edited['text']; data = {'text': text[args.offset:args.offset+args.limit], 'offset': args.offset,
                'total_chars': len(text), 'table_count': len(value['table_snapshot']['tables']), 'source_locator': value['locator']}
            next_offset = args.offset+args.limit if args.offset+args.limit < len(text) else None
    else:
        raise ValueError('原文读取需要原资料或OCR结果引用')
    return AgentToolResult(status='success', summary='已分页读取真实保存原文/结构，保留固定来源引用', data=data, result_refs=[ref], next_offset=next_offset).model_dump()


def grid(table):
    if not 1 <= table['columns'] <= 256 or table['rows']*table['columns'] > 1000000:
        raise ValueError('结构表超过拼接限制')
    rows = [['']*table['columns'] for _ in range(table['rows'])]; occupied = set()
    for cell in table['cells']:
        if cell['row_span'] != 1 or cell['column_span'] != 1:
            raise ValueError('含合并单元格，须先明确结构，不能猜测展开')
        position = cell['row'], cell['column']
        if position in occupied or not 0 <= position[0] < table['rows'] or not 0 <= position[1] < table['columns']:
            raise ValueError('结构坐标无效')
        occupied.add(position); rows[position[0]][position[1]] = cell['text']
    if len(occupied) != table['rows']*table['columns']:
        raise ValueError('表结构缺单元格')
    return rows


def concat(args, ctx):
    rows, sources, leaves, provenance = [], [], [], []
    columns = header = None
    for number, selection in enumerate(args.tables):
        ref = selection.reference
        if ref.kind != 'result' or ref.application != 'ocr':
            raise ValueError('拼接只接受已保存OCR结构表')
        value = verify(ref.model_dump(), ctx)
        table = value['table_snapshot']['tables'][selection.table_index]
        content = grid(table)
        if selection.header_rows > len(content):
            raise ValueError('表头超出实际表')
        current_header = content[:selection.header_rows]
        if columns is None:
            columns, header = table['columns'], current_header
        if columns != table['columns'] or header != current_header:
            raise ValueError('列数或原文表头不同，须用现有数据工具明确字段映射')
        start = len(rows)
        rows.extend(content if number == 0 else content[selection.header_rows:])
        if len(rows)*columns > 1000000:
            raise ValueError('拼接规模超出限制')
        sources.append(ref.model_dump()); leaves.append(value['source_asset']['asset_id'])
        provenance.append({'reference': ref.model_dump(), 'table_index': selection.table_index, 'header_rows': selection.header_rows,
            'output_row_start': start, 'source_row_start': 0 if number == 0 else selection.header_rows, 'output_row_count': len(rows)-start,
            'locator': value['locator'], 'source_asset': value['source_asset'], 'cells': table['cells']})
    stream = io.StringIO(newline=''); csv.writer(stream).writerows(rows)
    return install(stream.getvalue().encode('utf-8-sig'), args.file_name, 'text/csv', sources, leaves,
                   {'kind': 'explicit_table_concatenation', 'tables': provenance, 'columns': columns, 'rows': len(rows), 'values_are_literals': True}, ctx)


def register(registry):
    registry.register(ToolSpec('documents_read', '分页读取原资料文字/PDF指定页或OCR保存文字/单元格；offset为字符或单元格位置，不截断为一次性摘要。', Read, read))
    registry.register(ToolSpec('results_concat_tables', '按显式顺序拼接多个OCR结构表，表头与列数必须相同；保留逐行/单元格来源和原值，不猜测合并单元格或跨页关系。', Concat, concat, effect='derive', idempotent=True))
