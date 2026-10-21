"""Stable Clear representation; upstream payloads are retained independently."""
import re
from html import unescape
from .tables import parse_tables


def literal(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(literal(item) for item in value)
    if isinstance(value, dict):
        return literal(value.get('content', value.get('text', '')))
    return ''


def children(value):
    yield value
    if isinstance(value.get('content'), list):
        for child in value['content']:
            if isinstance(child, dict):
                yield from children(child)


def normalize(middle, source, run_id, diagnostics=(), generation=1):
    pages, warnings, tables = [], [str(item.get('message', item)) for item in diagnostics], []
    parser = {'id': 'docvortex', 'version': '0.5.16', 'device': 'cpu', 'pdf_parse_mode': 'txt'}
    for page in middle.get('pages', []):
        number = page['page_idx'] + 1
        blocks = []
        for order, original in enumerate(page.get('blocks', [])):
            kind = original.get('type', 'text')
            text = literal(original.get('content', ''))
            box = original.get('bbox')
            locator = {**{key: source[key] for key in ('asset_id', 'asset_revision', 'blob_hash')},
                       'parse_run_id': run_id, 'generation': generation,
                       'page_number': number, 'precision': 'approximate' if box else 'page_only',
                       'coordinate_space': 'normalized' if box else None, 'box': box}
            block = {'block_id': f'{run_id}g{generation}p{number}b{order + 1}', 'type': kind, 'text': text, 'bbox': box,
                     'source': locator, 'order': order, 'original_index': original.get('index'),
                     'level': original.get('level'), 'children': original.get('content') if isinstance(original.get('content'), list) else []}
            if kind == 'table':
                body = next((item for item in children(original) if item.get('type') == 'table_body'), {})
                html = literal(body.get('content', ''))
                block['html'] = html
                parsed = parse_tables(html, warnings=warnings)
                if len(parsed) == 1:
                    identifier = f'{run_id}g{generation}p{number}t{len(tables) + 1}'
                    table = {**parsed[0], 'table_id': identifier, 'revision': 1, 'page_number': number,
                             'block_id': block['block_id'], 'source': locator, 'reviewed': False,
                             'engine_identity': parser, 'model_revisions': {}}
                    # Block extent never implies a cell extent.
                    for cell in table['cells']:
                        cell['polygon'] = None
                        cell['source'] = {**locator, 'precision': 'page_only', 'box': None, 'coordinate_space': None}
                    tables.append(table)
                    block['table_id'] = identifier
                elif html:
                    warnings.append(f'第 {number} 页表格结构未完整解析，原始内容已保留')
                text = unescape(re.sub(r'<[^>]*>', ' ', text))
                block['text'] = text
            blocks.append(block)
        pages.append({'page_number': number, 'width': None, 'height': None, 'blocks': blocks,
                      'needs_ocr': not any(block['text'].strip() for block in blocks if block['type'] not in {'image', 'chart'}),
                      'source': {**source, 'page_number': number}, 'engine': 'docvortex', 'engine_identity': parser})
    suffix = source.get('original_name', '').rsplit('.', 1)[-1].lower()
    return {'schema_version': '1.0', 'kind': 'clear.document', 'revision': generation, 'document_id': run_id,
            'parse_run_id': run_id, 'generation': generation,
            'pagination_kind': 'logical' if suffix in {'docx'} else 'slide' if suffix == 'pptx' else 'physical',
            'source': source, 'engine': parser,
            'pages': pages, 'tables': tables, 'metadata': middle.get('metadata'), 'warnings': warnings,
            'cross_page_table_merge': False}
