"""Explicit failed-subset retry; validate the complete set before changing any job."""
import asyncio
import json

from .policy import PolicyDenied


def register_retry_tool(registry, services, operations, jobs):
    store = services.store

    def retry(args, context):
        allowed_states = {'failed', 'cancelled'} if args.action == 'retry' else {'paused', 'interrupted'}
        if len({(job.kind, job.job_id) for job in args.jobs}) != len(args.jobs):
            raise PolicyDenied('invalid_arguments', '重试任务列表不能重复')
        visual_snapshots = {}
        for job in args.jobs:
            if job.kind == 'visual_review':
                from ocr_workbench.multimodal_store import prepare_review
                snapshot = prepare_review(store, job.job_id, require_running=False)
                config, _ = services.visual_parameters(snapshot['model_id'])
                if config != snapshot['config']:
                    raise PolicyDenied('config_changed', '视觉快照配置已变化，请新建审校')
                visual_snapshots[job.job_id] = snapshot

        def effect(db, operation_id):
            checked = []
            for job in args.jobs:
                prior = db.execute("""SELECT j.* FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
                    WHERE o.id=? AND o.run_id=? AND o.project_id=? AND j.job_kind=? AND j.job_id=?""",
                    (args.operation_id, args.previous_run_id, context['run']['project_id'], job.kind, job.job_id)).fetchone()
                if not prior:
                    raise PolicyDenied('scope_denied', '任务不属于指定前序运行和操作')
                if job.kind == 'pdf_stage':
                    row = db.execute("""SELECT s.*,i.active_version,v.parent_id FROM document_stages s JOIN pages p ON p.id=s.page_id
                        LEFT JOIN images i ON i.id=p.image_id LEFT JOIN versions v ON v.id=i.active_version WHERE s.id=?""", (job.job_id,)).fetchone()
                    same_version = row and (row['version_id'] == row['active_version'] or row['version_id'] is None and row['parent_id'] is None)
                    table = 'document_stages'
                else:
                    row = db.execute("""SELECT t.*,i.active_version FROM tasks t JOIN images i ON i.id=t.image_id WHERE t.id=? AND t.project_id=?""",
                        (job.job_id, context['run']['project_id'])).fetchone()
                    expected = {'ocr': 'ocr', 'fusion': 'fusion', 'visual_review': 'multimodal', 'structure': 'geometry'}[job.kind]
                    same_version = row and row['kind'] == expected and row['version_id'] == row['active_version']
                    table = 'tasks'
                if not same_version:
                    raise PolicyDenied('stale_revision', '任务种类或页面版本已变化，不能重试旧任务')
                if row['status'] not in allowed_states:
                    raise PolicyDenied('business_failed', '仅能重试失败/取消任务，或继续暂停/中断任务；成功和运行中任务保持原状')
                if job.kind == 'pdf_stage':
                    competing = db.execute("SELECT 1 FROM document_stages WHERE page_id=? AND id<>? AND status IN ('queued','running','waiting_gpu')", (row['page_id'], job.job_id)).fetchone()
                    if competing:
                        raise PolicyDenied('business_failed', '页面已有其他处理中任务')
                if job.kind == 'visual_review':
                    from ocr_workbench.multimodal_store import _current_snapshot
                    current = db.execute('SELECT * FROM multimodal_requests WHERE task_id=?', (job.job_id,)).fetchone()
                    _current_snapshot(store, db, current)
                    snapshot = visual_snapshots[job.job_id]
                    if snapshot['config'].get('backend') == 'external':
                        from .visual import check_external_job_authorization
                        check_external_job_authorization(store, job.job_id, snapshot)
                children = []
                if job.kind == 'pdf_stage':
                    children = db.execute('SELECT t.* FROM page_ocr_inputs i JOIN tasks t ON t.id=i.task_id WHERE i.stage_id=?', (job.job_id,)).fetchall()
                    if any(t['project_id'] != context['run']['project_id'] or t['version_id'] != row['active_version'] or t['kind'] != 'region_ocr' for t in children):
                        raise PolicyDenied('stale_revision', '区域任务版本或归属已变化')
                checked.append((job, prior, table, children))
            links = []
            from .budgets import reserve_operation
            retry_children = [child for _, _, _, children in checked for child in children if child['status'] in allowed_states]
            if retry_children:
                current = operations.agent._run(db, context['run']['project_id'], context['run']['id'])
                reserve_operation(db, current, 0, len(retry_children))
                for child in retry_children:
                    db.execute("UPDATE tasks SET status='queued',phase='等待显式区域重试',error=NULL WHERE id=?", (child['id'],))
            for job, prior, table, children in checked:
                target = 'waiting_gpu' if children else 'queued'
                db.execute(f"UPDATE {table} SET status=?,phase='等待显式重试',error=NULL WHERE id=?", (target, job.job_id))
                # Reusing another run's job does not transfer cancellation ownership.
                ownership = prior['ownership'] if args.previous_run_id == context['run']['id'] else 'reused'
                links.append({'kind': job.kind, 'job_id': job.job_id, 'ownership': ownership,
                              'input_revision': prior['input_revision'], 'state': target})
            return {'retry_of': args.operation_id, 'job_ids': [job.job_id for job in args.jobs]}, links

        operation = operations.submit(context, 'retry_failed_jobs', args.model_dump(), effect,
                                      cost={'pages': len(args.jobs), 'jobs': len(args.jobs)})
        for queue in (services.documents, services.queue, services.fusion_queue, services.external_queue):
            if queue is not None:
                queue.wake.set()
        return jobs.operation_result(context['run']['project_id'], context['run']['id'], operation['operation_id'])

    async def retry_async(args, context):
        return await asyncio.to_thread(retry, args, context)

    registry.register('retry_failed_jobs', retry_async)
