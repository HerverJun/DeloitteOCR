"""Strict persisted shape for explicit, source-bound cell confirmations."""
import json
import re

VERSION = 'screen-cell-confirmation-v1'
HEX = re.compile(r'[0-9a-f]{64}\Z')
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,120}\Z')
MEMBER_KEYS = {'input_version_id', 'image_id', 'source_sha256', 'result_id', 'result_revision',
    'review_binding_token', 'preparation_sha256', 'evidence_sha256', 'source_name', 'table',
    'row', 'column', 'row_span', 'column_span', 'before', 'source_polygon', 'crop_box',
    'width', 'height', 'pixel_sha256', 'geometry_basis'}
CONFIRMATION_KEYS = {'confirmed_text', 'group_id', 'request_id', 'actor', 'confirmed_at'}


def validate_cell_state(state, tables):
    if (not isinstance(state, dict) or set(state) != {'version', 'result_revision',
            'input_version_id', 'source_sha256', 'review_binding_token', 'cells'}
            or state['version'] != VERSION or type(state['result_revision']) is not int
            or state['result_revision'] < 1 or not isinstance(state['input_version_id'], str)
            or not state['input_version_id'] or not isinstance(state['cells'], list)
            or not 1 <= len(state['cells']) <= 100000):
        raise ValueError('无效的单元格复核状态')
    for key in ('source_sha256', 'review_binding_token'):
        if not isinstance(state[key], str) or not HEX.fullmatch(state[key]):
            raise ValueError('无效的单元格复核来源')
    seen = set()
    for cell in state['cells']:
        if not isinstance(cell, dict) or set(cell) != MEMBER_KEYS | CONFIRMATION_KEYS:
            raise ValueError('无效的单元格确认记录')
        for key in ('input_version_id', 'image_id', 'result_id', 'source_name', 'geometry_basis', 'confirmed_at'):
            if not isinstance(cell[key], str) or not cell[key] or len(cell[key]) > 16000:
                raise ValueError('无效的单元格确认身份')
        for key in ('source_sha256', 'review_binding_token', 'preparation_sha256', 'evidence_sha256', 'pixel_sha256', 'group_id'):
            if not isinstance(cell[key], str) or not HEX.fullmatch(cell[key]):
                raise ValueError('无效的单元格确认哈希')
        if (cell['input_version_id'] != state['input_version_id'] or cell['source_sha256'] != state['source_sha256']
                or cell['actor'] != 'human' or not isinstance(cell['request_id'], str)
                or not REQUEST.fullmatch(cell['request_id']) or type(cell['result_revision']) is not int
                or not 0 <= cell['result_revision'] < state['result_revision']):
            raise ValueError('单元格确认失去原图或修订绑定')
        for key in ('before', 'confirmed_text'):
            if (not isinstance(cell[key], str) or not cell[key].strip() or len(cell[key]) > 16000
                    or any(ord(c) < 32 and c not in '\t\n\r' for c in cell[key])):
                raise ValueError('无效的单元格确认文字')
        ti, row, column, rs, cs = [cell[key] for key in ('table', 'row', 'column', 'row_span', 'column_span')]
        if (any(type(v) is not int or v < 0 for v in (ti, row, column))
                or any(type(v) is not int or v < 1 for v in (rs, cs)) or ti >= len(tables)):
            raise ValueError('无效的单元格确认位置')
        matches = [part for part in tables[ti]['cells'] if (part['row'], part['column']) == (row, column)]
        if len(matches) != 1 or (matches[0]['row_span'], matches[0]['column_span']) != (rs, cs):
            raise ValueError('单元格确认位置不属于当前结构')
        position = (ti, row, column)
        if position in seen:
            raise ValueError('单元格确认位置重复')
        seen.add(position)
        box, polygon = cell['crop_box'], cell['source_polygon']
        if (not isinstance(box, list) or len(box) != 4 or any(type(v) is not int or v < 0 for v in box)
                or box[0] >= box[2] or box[1] >= box[3]
                or type(cell['width']) is not int or type(cell['height']) is not int
                or (cell['width'], cell['height']) != (box[2] - box[0], box[3] - box[1])
                or not isinstance(polygon, list) or len(polygon) != 4
                or any(not isinstance(p, list) or len(p) != 2
                       or any(type(v) not in (int, float) for v in p) for p in polygon)):
            raise ValueError('无效的整格源像素范围')
    if len(json.dumps(state, ensure_ascii=False, allow_nan=False)) > 50_000_000:
        raise ValueError('单元格确认记录超出限制')
