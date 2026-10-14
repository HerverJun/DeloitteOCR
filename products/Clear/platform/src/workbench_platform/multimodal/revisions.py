"""Stable review identities, including a deterministic view of legacy results."""
import copy
from workbench_platform.batches.store import fingerprint
from workbench_platform.data_api.service import DataError


def identified_rows(job_id, field, rows):
    result = []
    for index, original in enumerate(rows):
        row = copy.deepcopy(original)
        row.setdefault('row_id', 'r' + fingerprint([job_id, field, index,
            row.get('start_ms'), row.get('end_ms'), row.get('page_number'), row.get('box')])[:32])
        result.append(row)
    return result


def attach_row_ids(item, result):
    for field in ('segments', 'seals'):
        if field in result:
            result[field] = identified_rows(item['job_id'], field, result[field])


def normalized_edits(item, field, edits):
    rows = identified_rows(item['job_id'], field, (item.get('result') or {}).get(field, []))
    positions = {row['row_id']: index for index, row in enumerate(rows)}
    allowed = {'text', 'speaker'} if field == 'segments' else {'text', 'subject', 'number'}
    normalized, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - allowed - {'index', 'row_id'}:
            raise DataError('修订字段无效')
        row_id, index = edit.get('row_id'), edit.get('index')
        if row_id is not None:
            if not isinstance(row_id, str) or row_id not in positions:
                raise DataError('修订行身份不存在', 409)
            expected = positions[row_id]
            if index is not None and (type(index) is not int or index != expected):
                raise DataError('修订行身份与原始位置不一致', 409)
            index = expected
        elif type(index) is not int or not 0 <= index < len(rows):
            raise DataError('修订行无效')
        row_id = rows[index]['row_id']
        if row_id in seen:
            raise DataError('同一结果不能在一次修订中重复提交')
        seen.add(row_id)
        changes = {key: value for key, value in edit.items() if key in allowed}
        if any(not isinstance(value, str) or len(value) > 10000 for value in changes.values()):
            raise DataError('修订字段无效')
        normalized.append({'row_id': row_id, 'index': index, **changes})
    return normalized


def reviewed_result(item):
    result = copy.deepcopy(item['result'])
    attach_row_ids(item, result)
    field = 'segments' if item['kind'] == 'audio' else 'seals'
    for edit in normalized_edits(item, field, (item.get('review') or {}).get('edits', [])):
        result[field][edit['index']].update({key: value for key, value in edit.items()
                                            if key not in {'index', 'row_id'}})
    return result
