"""Readable evidence from fixed batch results, with literal cell values."""
from contextlib import closing
from decimal import Decimal
import json

from packages.contracts.audit import ItemEvidence
from packages.contracts.models import SourceLocator
from workbench_platform.batches.store import fingerprint
from workbench_platform.data_api.service import DataError


def _source(member, table=None, row=None, column=None):
    locator = {'schema_version': '1.0', 'kind': 'file',
               **{key: member[key] for key in ('asset_id', 'asset_revision', 'blob_hash')},
               'precision': 'exact'}
    if table and table.get('source', {}).get('sheet_name') and row:
        locator.update(kind='cell' if column else 'row', table_number=1,
                       sheet_name=table['source']['sheet_name'], row_number=int(row))
        if column:
            locator['column_number'] = int(column[1:])
    return SourceLocator.model_validate(locator).model_dump(mode='json')


def _data_fields(batch_service, batch, item):
    source, raw = item['source'], item['raw_output']
    session_id, result_id = source.get('session_id'), source.get('result_id')
    if not session_id or not result_id or 'ordinal' not in raw:
        return [], [], '技术异常；固定处理模板：' + json.dumps(batch['template']['config'], ensure_ascii=False)
    page = batch_service.data.data.result_page_after(session_id, result_id, raw['ordinal'], 1)
    if not page['rows'] or page['rows'][0] != raw:
        raise DataError('证据与固定结果索引不一致，不能替代原证据', 409)
    summary, policy = page['metadata'], page['metadata']['policy']
    members = {member['role']: member for member in batch['inputs']}
    category = raw['category']
    differences = set(json.loads(raw.get('different_columns_json') or '[]'))
    sources = []

    def cell(side, column):
        prefix = ('L_' if side == 'left' else 'R_') + column + '_'
        if not raw.get(side + '_row_id'):
            return None
        kind = raw[prefix + 'kind']
        value = raw.get(prefix + 'value')
        if kind in {'null', 'unknown_formula'}:
            value = None
        elif kind == 'boolean':
            value = value == 'True'
        location = _source(members[side], summary[side], raw.get(side + '_source_row'), column)
        if location not in sources:
            sources.append(location)
        return {'role': side, 'kind': kind, 'raw': raw.get(prefix + 'raw'),
                'value': value, 'source': location, 'row_id': raw[side + '_row_id'],
                'table_revision': summary[side]['revision']}

    fields, seen = [], set()
    amounts = {tuple(pair) for pair in policy['amount_pairs']}
    for pair in [*policy['key_pairs'], *policy['compare_pairs']]:
        left, right = pair
        if (left, right) in seen:
            continue
        seen.add((left, right))
        values = [value for value in (cell('left', left), cell('right', right)) if value is not None]
        names = {side: {entry['column_id']: entry.get('header') or entry['column_id']
                        for entry in summary[side]['columns']} for side in ('left', 'right')}
        amount = (left, right) in amounts
        delta, tolerance, within = None, None, None
        if category.startswith('ambiguous'):
            outcome, rule = 'ambiguous', 'ambiguous'
        elif category.startswith('only_'):
            outcome, rule = 'unmatched', 'unmatched'
        else:
            outcome = 'different' if left + ':' + right in differences else 'equal'
            rule = 'amount' if amount else 'text' if policy['type_policy'] == 'text' else 'exact'
            if amount and len(values) == 2 and all(value['kind'] in {'decimal', 'integer'} for value in values):
                delta = str(Decimal(values[0]['value']) - Decimal(values[1]['value']))
                tolerance = policy['amount_tolerance']
                within = abs(Decimal(delta)) <= Decimal(tolerance)
                outcome = 'within_tolerance' if within else 'outside_tolerance'
        fields.append({'name': names['left'][left] if names['left'][left] == names['right'][right]
                       else names['left'][left] + ' / ' + names['right'][right],
                       'values': values, 'rule_kind': rule, 'outcome': outcome,
                       'delta': delta, 'tolerance': tolerance, 'within_tolerance': within})
    return fields, sources, json.dumps(policy, ensure_ascii=False, sort_keys=True)


def _native_fields(scenario, item, source):
    raw, adoption = item.get('raw_output', {}), item.get('adoption', {})
    if item.get('category') != 'review':
        # A failed execution has no known recognition or classification result.
        return []

    def value(role, text):
        known = isinstance(text, str)
        return {'role': role, 'kind': 'text' if known else 'unknown',
                'raw': text if known else None, 'value': text if known else None, 'source': source}

    def output_text(output):
        if isinstance(output, str):
            return output
        if isinstance(output, dict) and isinstance(output.get('text'), str):
            return output['text']
        return None

    fields = []
    if scenario == 'ocr':
        original = output_text(raw.get('original_output'))
        values = [value('original', original)]
        if 'edited_output' in raw:
            values.append(value('manual', output_text(raw['edited_output'])))
        if adoption:
            values.append(value('adopted', output_text(adoption.get('snapshot', {}).get('edited'))))
        fields.append({'name': '识别原文 / 已采用文字', 'values': values,
                       'rule_kind': 'native_revision', 'outcome': 'adopted' if adoption else 'unadopted'})
    else:
        values = [value('original', raw.get('prediction_label_id'))]
        if raw.get('human_label_id') is not None and not adoption:
            values.append(value('manual', raw['human_label_id']))
        if adoption:
            values.append(value('adopted', adoption.get('human_label_id')))
        fields.append({'name': '模型建议类别 ID / 已采用人工类别 ID', 'values': values,
                       'rule_kind': 'native_revision', 'outcome': 'adopted' if adoption else 'unadopted'})
    versions = []
    for role, revision in [('original', raw.get('revision', raw.get('disposition_revision'))),
                           ('adopted', adoption.get('native_revision'))]:
        if type(revision) is int and revision >= 0:
            versions.append({'role': role, 'kind': 'integer', 'raw': str(revision),
                             'value': str(revision), 'source': source})
    if versions:
        fields.append({'name': '原生修订版本', 'values': versions, 'rule_kind': 'native_revision',
                       'outcome': 'adopted' if adoption else 'unadopted'})
    return fields


def batch_item_evidence(batch_service, batch_id, item_id, revision):
    # The SQL result index and its disposition are observed together; sources
    # and comparison views are immutable and checked against the indexed row.
    with batch_service.store.lock:
        with closing(batch_service.store.db()) as db:
            db.execute('BEGIN')
            batch, _, _ = batch_service.store._snapshot(db, batch_id)
            item = batch_service.store._selected_items(db, batch_id, [item_id])[0]
        batch_service.workspace(batch['workspace_id'])
        if batch['revision'] != revision:
            raise DataError('证据批次版本冲突，请刷新所见结果', 409)
        scenario = batch['template']['scenario']
        raw, adoption = item.get('raw_output', {}), item.get('adoption', {})
        if scenario == 'data_compare':
            fields, sources, rule_summary = _data_fields(batch_service, batch, item)
        else:
            member = item.get('source', {})
            sources = [_source(member)] if all(key in member for key in ('asset_id', 'asset_revision', 'blob_hash')) else []
            fields = _native_fields(scenario, item, sources[0] if sources else None)
            rule_summary = json.dumps(batch['template']['config'], ensure_ascii=False, sort_keys=True)
        # Source previews are bounded, but the exact source count remains visible.
        if len(rule_summary) > 4000:
            raise DataError('规则说明超过当前证据契约容量', 409)
        result = {'schema_version': '1.0', 'audit_version': '1.0', 'workspace_id': batch['workspace_id'],
                  'batch_id': batch_id, 'item_id': item_id, 'batch_revision': revision,
                  'scenario': scenario, 'issue_type': 'result' if item.get('category') in
                    {'review', 'different', 'only_left', 'only_right', 'ambiguous_left', 'ambiguous_right'} else 'technical',
                  'template_id': batch['template_id'], 'template_version': batch['template_version'],
                  'config_hash': batch['template']['config_hash'],
                  'result_id': raw.get('native_result_id') or item.get('source', {}).get('result_id'),
                  'native_version_id': raw.get('native_version_id'),
                  'native_revision': adoption.get('native_revision', raw.get('revision', raw.get('disposition_revision'))),
                  'adoption_operation_id': adoption.get('operation_id'), 'rule_summary': rule_summary,
                  'rule_hash': fingerprint(batch['template']['config']), 'fields': fields,
                  'sources': sources[:10], 'source_count': len(sources),
                  'native_context_available': scenario != 'data_compare' and item.get('category') == 'review'}
        result['snapshot_hash'] = fingerprint({**result, 'indexed_item': item})
        return ItemEvidence.model_validate(result).model_dump(mode='json')
