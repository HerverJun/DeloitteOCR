"""Explicit restart recovery changes only this run's originally created jobs."""
import json

from ocr_workbench.store import Conflict


def resume_owned(agent, services, project_id, run_id, generation):
    store = agent.business
    # Resolve external snapshots before the write transaction; no network call.
    visual = {}
    for row in store.rows("""SELECT DISTINCT t.id FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
        JOIN tasks t ON t.id=j.job_id WHERE o.run_id=? AND o.project_id=? AND j.ownership='created'
        AND j.job_kind='visual_review' AND t.status IN ('paused','interrupted')""", (run_id, project_id)):
        from ocr_workbench.multimodal_store import prepare_review
        snapshot = prepare_review(store, row['id'], require_running=False)
        config, _ = services.visual_parameters(snapshot['model_id'])
        if config != snapshot['config']:
            raise Conflict('视觉连接已变化，请重新提交审校')
        visual[row['id']] = snapshot
    with store.file_lock, store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        run = agent._run(db, project_id, run_id)
        agent.require_generation(run, generation)
        if run['status'] != 'interrupted':
            raise Conflict('恢复任务必须对应尚未继续的中断运行')
        links = db.execute("""SELECT DISTINCT j.job_kind,j.job_id FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
            WHERE o.run_id=? AND o.project_id=? AND j.ownership='created'""", (run_id, project_id)).fetchall()
        checked = []
        for link in links:
            if link['job_kind'] == 'pdf_stage':
                row = db.execute("""SELECT s.*,i.active_version,v.parent_id FROM document_stages s JOIN pages p ON p.id=s.page_id
                    JOIN documents d ON d.id=p.document_id LEFT JOIN images i ON i.id=p.image_id
                    LEFT JOIN versions v ON v.id=i.active_version WHERE s.id=? AND d.project_id=?""", (link['job_id'], project_id)).fetchone()
                table = 'document_stages'
                if not row or row['status'] not in {'paused', 'interrupted'}:
                    continue
                if row['version_id'] != row['active_version'] and not (row['version_id'] is None and row['parent_id'] is None):
                    raise Conflict('PDF 页面版本已变化，不能继续旧阶段')
                if getattr(services.documents, 'review_only', False) and json.loads(row['parameters']).get('mode') != 'native':
                    raise Conflict('仅校对模式不能继续本地识别阶段')
                if db.execute("SELECT 1 FROM document_stages WHERE page_id=? AND id<>? AND status IN ('queued','running','waiting_gpu')", (row['page_id'], row['id'])).fetchone():
                    raise Conflict('PDF 页面已有其他处理阶段')
                children = db.execute("SELECT t.* FROM tasks t JOIN page_ocr_inputs i ON i.task_id=t.id WHERE i.stage_id=?", (row['id'],)).fetchall()
                if any(t['version_id'] != row['active_version'] or t['project_id'] != project_id or t['kind'] != 'region_ocr' for t in children):
                    raise Conflict('区域任务版本或归属已变化')
                checked.extend(('tasks', t['id'], 'queued') for t in children if t['status'] in {'paused', 'interrupted'})
                target = 'waiting_gpu' if children else 'queued'
            else:
                row = db.execute('SELECT t.*,i.active_version FROM tasks t JOIN images i ON i.id=t.image_id WHERE t.id=? AND t.project_id=?', (link['job_id'], project_id)).fetchone()
                table, target = 'tasks', 'queued'
                if not row or row['status'] not in {'paused', 'interrupted'}:
                    continue
                expected = {'ocr': 'ocr', 'fusion': 'fusion', 'structure': 'geometry', 'visual_review': 'multimodal'}[link['job_kind']]
                if row['kind'] != expected or row['version_id'] != row['active_version']:
                    raise Conflict('任务种类或图像版本已变化，不能继续旧任务')
                if link['job_kind'] == 'visual_review':
                    from ocr_workbench.multimodal_store import _current_snapshot
                    _current_snapshot(store, db, db.execute('SELECT * FROM multimodal_requests WHERE task_id=?', (row['id'],)).fetchone())
                    snapshot = visual.get(row['id'])
                    if snapshot is None:
                        raise Conflict('视觉任务恢复快照已变化')
                    if snapshot['config'].get('backend') == 'external':
                        from .visual import check_external_job_authorization
                        check_external_job_authorization(store, row['id'], snapshot)
                    elif getattr(services.documents, 'review_only', False):
                        raise Conflict('仅校对模式不能继续本地视觉任务')
                elif getattr(services.documents, 'review_only', False):
                    raise Conflict('仅校对模式不能继续本地模型任务')
            checked.append((table, row['id'], target))
        for table, key, target in checked:
            db.execute(f"UPDATE {table} SET status=?,phase='等待显式继续',error=NULL WHERE id=? AND status IN ('paused','interrupted')", (target, key))
    for queue in (services.documents, services.queue, services.fusion_queue, services.external_queue):
        if queue is not None:
            queue.wake.set()
            if hasattr(queue, 'recover_worker'):
                queue.recover_worker()
    return [{'table': table, 'id': key} for table, key, _ in checked]
