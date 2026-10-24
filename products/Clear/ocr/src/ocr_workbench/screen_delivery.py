"""Stable live identity for image batches and their immutable exports."""
from __future__ import annotations

import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def delivery_snapshot(manager, project_id, batch_id):
    """Bind edits, adopted results, original versions and review transitions.

    This is a read of the existing business state, not a second task ledger.
    Snapshot read times do not affect identity. The review action's persisted
    epoch is material: confirming the same revision again invalidates old ZIPs.
    """
    # Preparation holds the batch/file guards while a whole group of CPU
    # futures finishes. Status must stay readable during that work. This
    # identity reads only persisted rows, so one WAL snapshot is sufficient;
    # exports still take their existing file guard and compare before/after.
    with manager.store.transaction() as db:
        db.execute('BEGIN')
        record = db.execute('SELECT * FROM screen_batches WHERE id=?', (batch_id,)).fetchone()
        if record is None:
            raise KeyError('批次不存在')
        if record['project_id'] != project_id:
            raise KeyError('批次不属于当前项目')
        batch = dict(record)
        batch['policy'] = json.loads(batch['policy'])
        rows = db.execute('''SELECT i.input_version_id,i.image_id,i.status,
            i.stage,i.attempt,i.result_id,i.result_revision,i.review_state,
            im.active_version,r.revision current_revision,s.result_id selected_result_id,
            rv.result_id review_result_id,rv.version_id review_version_id,
            rv.revision review_revision,rv.status human_review_status,rv.updated review_epoch
            FROM screen_items i JOIN images im ON im.id=i.image_id
            LEFT JOIN results r ON r.id=i.result_id
            LEFT JOIN selections s ON s.image_id=i.image_id
            LEFT JOIN reviews rv ON rv.image_id=i.image_id
            WHERE i.batch_id=? ORDER BY i.position''', (batch_id,)).fetchall()
        items = [dict(row) for row in rows]
        stale = []
        for item in items:
            reasons = []
            if item['active_version'] != item['input_version_id']:
                reasons.append('input_version_changed')
            if item['result_id']:
                if item['current_revision'] != item['result_revision']:
                    reasons.append('result_revision_changed')
                if item['selected_result_id'] not in {None, item['result_id']}:
                    reasons.append('selected_result_changed')
                if item['review_state'] == 'accepted' and (
                    item['human_review_status'] != 'confirmed'
                    or item['review_result_id'] != item['result_id']
                    or item['review_version_id'] != item['input_version_id']
                    or item['review_revision'] != item['result_revision']):
                    reasons.append('human_review_changed')
            if reasons:
                stale.append({'input_version_id': item['input_version_id'], 'reasons': reasons})
        identity = {'batch_id': batch_id, 'project_id': project_id,
                    'policy_sha256': digest(batch['policy']), 'items': items}
        return {**identity, 'snapshot_id': digest(identity), 'fresh': not stale,
                'stale_items': stale}
