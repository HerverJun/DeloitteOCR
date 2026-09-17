"""Add one synthetic, pending review to an already-adopted audit fixture.

No model, queue worker, or browser is started. Call before the fixture service
starts, after other queued OCR tasks have completed.
"""
from uuid import uuid4


def seed_pending_multimodal(store, result_id):
    from ocr_workbench.document_review import document_review_queue
    from ocr_workbench.multimodal_contract import build_targets
    from ocr_workbench.multimodal_store import enqueue_review, prepare_review, complete_review, view_review

    if store.rows("SELECT id FROM tasks WHERE status='queued' AND kind!='fusion'"):
        raise RuntimeError('Finish queued non-fusion fixture tasks before seeding synthetic review')
    result = store.result(result_id)
    original_task = store.one('tasks', result['task_id'])
    version_id = result['original'].get('project_image_version') or original_task['version_id']
    version = store.one('versions', version_id)
    # One existing text span or real logical cell is enough for navigation.
    target = build_targets(result['edited'], result['original'], version, 'page')[0]['target']
    task_id = enqueue_review(store, result_id, {
        'revision': result['revision'], 'version_id': version_id,
        'model_id': 'audit-synthetic-review', 'scope': 'target', 'target': target,
        'request_id': 'audit-nav-' + uuid4().hex,
    }, {'profile_id': 'audit-synthetic-review', 'fixture_only': True})
    claimed = store.claim()
    assert claimed is not None and claimed['id'] == task_id, 'Fixture queue was modified concurrently'
    snapshot = prepare_review(store, task_id)
    complete = complete_review(store, task_id, {
        'items': [{'target_id': item['id'], 'decision': 'keep', 'after': item['before'],
                   'reason': '导航审计用合成建议：尚未人工处理'} for item in snapshot['targets']],
        'summary': '导航审计合成数据，无模型推理',
        'identity': {'synthetic_fixture': True},
    })
    assert complete
    proposal = next(p for p in view_review(store, result_id)['proposals'] if p['task_id'] == task_id)
    page = store.page_for_version(version_id)
    rows = document_review_queue(store, page['document_id'], limit=100)['tasks']
    assert any(t['kind'] == 'multimodal' and t['result_id'] == result_id and
               t['proposal_id'] == proposal['id'] for t in rows), 'Synthetic review is absent from document queue'
    return {'task_id': task_id, 'proposal_id': proposal['id'], 'result_id': result_id,
            'document_id': page['document_id'], 'target': target}
