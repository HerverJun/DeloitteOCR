"""Reproduce cross-feature review lifecycle defects without inference or a browser.

Run with the bundled service Python. Only disposable workspaces are written.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]

from test_multimodal_store import MultimodalStoreTests
from test_structure_workflow import StructureStoreTests
from ocr_workbench.document_review import document_review_queue
from ocr_workbench.structure_store import record_candidates, refresh_proposals, structure_view, decide_structure
from ocr_workbench.table_tool import tool_identity
from ocr_workbench.store import Conflict, encoded


def queue_duplicate_id():
    fixture = MultimodalStoreTests()
    fixture.setUp()
    try:
        fixture.generated(request='first-model-run')
        fixture.generated(request='second-model-run')
        document = fixture.store.page_for_version(fixture.version['id'])['document_id']
        queue = document_review_queue(fixture.store, document)
        rows = [t for t in queue['tasks'] if t['kind'] == 'multimodal']
        counts = Counter(t['id'] for t in rows)
        assert len(rows) == 6 and len(counts) == 3
        fixture.enqueue(request='pending-run-1')
        fixture.enqueue(request='pending-run-2')
        queue = document_review_queue(fixture.store, document)
        pending = [t for t in queue['tasks'] if t['kind'] == 'multimodal_task']
        assert len(pending) == 2 and pending[0]['id'] == pending[1]['id']
        return {'confirmed': True, 'proposal_count': len(rows), 'unique_queue_ids': len(counts),
                'duplicate_groups': [[{'id': t['id'], 'proposal_id': t['proposal_id'], 'target': t['target']}
                                      for t in rows if t['id'] == key] for key in counts],
                'pending_tasks': pending}
    finally:
        fixture.tearDown()


def stale_pdf_queue():
    fixture = StructureStoreTests()
    fixture.setUp()
    try:
        # A synthetic native-PDF result with a current provider run. The tested
        # view/decision code does not require rendering or PDF inference.
        store, rid = fixture.store, fixture.result_id
        page = store.page_for_version(fixture.version['id'])
        raw = store.result(rid)['original']
        tool = tool_identity()
        raw.update(origin='document', document={'page_id': page['id'], 'mode': 'native',
                   'table_tool': {'state': 'ready', 'retry_allowed': True, 'tool_key': tool['key'],
                                  'run_id': 'first-tool-run', 'message': 'Current fixture candidates'}})
        pred = deepcopy(fixture.pred)
        pred.update(candidate_provider_key='pdfplumber/fixture', table_tool_run_id='first-tool-run')
        with store.transaction() as db:
            db.execute('DELETE FROM structure_candidates WHERE result_id=?', (rid,))
            db.execute('UPDATE documents SET kind=? WHERE id=?', ('pdf', page['document_id']))
            db.execute('UPDATE results SET original=? WHERE id=?', (encoded(raw), rid))
            record_candidates(db, rid, fixture.version, pred, fixture.blocks)
        before = refresh_proposals(store, rid, 0)
        proposal = next(p for p in before['proposals'] if p['kind'] == 'replace_table')
        assert proposal['can_apply']
        changed = {**tool, 'key': 'upgraded-tool-key'}
        with patch('ocr_workbench.table_tool.tool_identity', return_value=changed):
            panel = structure_view(store, rid)
            queue = document_review_queue(store, page['document_id'])
            stale = [t for t in queue['tasks'] if t['kind'] == 'structure']
            assert panel['table_tool']['state'] == 'outdated'
            assert panel['proposals'] == [] and panel['candidates'] == []
            assert stale and any(p['can_apply'] for task in stale for p in task['alternatives'])
            try:
                decide_structure(store, rid, proposal['id'], fixture.body(proposal))
            except Conflict as error:
                rejection = str(error)
            else:
                raise AssertionError('Old provider candidate unexpectedly applied')
            refresh_proposals(store, rid, 0)
            rechecked = document_review_queue(store, page['document_id'])
            stale_after_check = [t for t in rechecked['tasks'] if t['kind'] == 'structure']
            assert stale_after_check
        return {'confirmed': True, 'panel_proposals': len(panel['proposals']),
                'panel_candidates': len(panel['candidates']), 'tool_state': panel['table_tool']['state'],
                'queue_summary': queue['summary'], 'stale_queue_rows': stale,
                'stale_rows_after_rechecking': len(stale_after_check),
                'decision_rejected': rejection, 'content_revision_after': store.result(rid)['revision']}
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    receipt = {'duplicate_review_queue_ids': queue_duplicate_id(),
               'outdated_pdf_candidates_in_queue': stale_pdf_queue()}
    output = Path(__file__).with_name('review-features-receipt.json')
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
