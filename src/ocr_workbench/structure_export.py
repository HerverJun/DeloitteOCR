"""Source indices copied under the same adopted export database snapshot."""
import json


def native_structure_units(db, result, version):
    """Resolve immutable native tokens; editable provenance alone is insufficient."""
    sources, used, output = {}, set(), {}
    for ti, table in enumerate(result['edited']['tables']):
        review = table.get('structure_review',{})
        key = review.get('candidate_set_id')
        if not key:
            continue
        if key not in sources:
            row = db.execute('SELECT payload FROM structure_candidates WHERE id=? AND result_id=? AND version_id=? AND image_sha256=?',
                (key,result['id'],version['id'],version['sha256'])).fetchone()
            sources[key] = {t['id']:t for t in json.loads(row[0])['tokens']} if row else {}
        for cell in table['cells']:
            ids = cell.get('structure_source',{}).get('token_ids',[])
            tokens = [sources[key].get(i) for i in ids]
            if (not ids or any(t is None or t['source_kind'] != 'native' for t in tokens) or
                len(set(ids)) != len(ids) or used.intersection(ids) or
                cell['text'] not in (''.join(t['raw_text'] for t in tokens), ' '.join(t['raw_text'] for t in tokens))):
                continue
            used.update(ids)
            output[(ti,cell['row'],cell['column'])] = [{'text':t['raw_text'],'polygon':t['polygon']} for t in tokens]
    return output


def structure_sources(db, result):
    adopted = [dict(table_index=i, review=t['structure_review'], cells=[
        {'row': c['row'], 'column': c['column'], 'row_span': c['row_span'], 'column_span': c['column_span'],
         'text': c['text'], 'is_header': c.get('is_header', False), 'header_role':c.get('header_role'), 'source': c.get('structure_source')}
        for c in t['cells']]) for i,t in enumerate(result['edited']['tables']) if t.get('structure_review')]
    if not adopted:
        return None
    candidate_ids = {t['review']['candidate_set_id'] for t in adopted}
    candidates = []
    for row in db.execute('SELECT * FROM structure_candidates WHERE result_id=?', (result['id'],)):
        if row['id'] in candidate_ids:
            candidates.append({**dict(row), 'payload': json.loads(row['payload'])})
    proposals = [{**dict(row), 'payload': json.loads(row['payload']), 'scope': json.loads(row['scope'])}
                 for row in db.execute('SELECT * FROM structure_proposals WHERE result_id=?', (result['id'],))]
    decisions = [dict(row) for row in db.execute('SELECT request_id,proposal_id,action,created FROM structure_decisions WHERE result_id=? ORDER BY created', (result['id'],))]
    return {'schema_version': 1, 'result_id': result['id'], 'revision': result['revision'],
            'automatic_adoption': False, 'adopted_tables': adopted, 'candidates': candidates,
            'proposals': proposals, 'decisions': decisions}
