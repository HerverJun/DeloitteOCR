"""Use the same target IDs, snapshots and queues as the existing review UI."""
import json

from ocr_workbench.multimodal_contract import target_catalog


def targets_for_result(db, result_id, *, edit=None):
    if edit is None:
        row = db.execute('SELECT edited FROM results WHERE id=?', (result_id,)).fetchone()
        if row is None:
            raise KeyError('识别结果不存在')
        edit = json.loads(row[0])
    return target_catalog(edit)


def check_external_job_authorization(store, task_id, snapshot):
    # Existing UI jobs have no agent link and keep their established behavior.
    links = store.rows("""SELECT DISTINCT o.run_id,o.project_id FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
        WHERE j.job_id=? AND j.job_kind='visual_review'""", (task_id,))
    if not links:
        return
    from .policy import AgentPolicy
    from .store import AgentStore
    agent = AgentStore(store)
    policy = AgentPolicy(agent)
    page = store.rows('SELECT id,document_id FROM pages WHERE image_id=?', (snapshot['image_id'],))[0]
    for link in links:
        run = agent.run(link['project_id'], link['run_id'])
        config = snapshot['config']
        policy.authorize_outbound(run, role='visual', endpoint=config['base_url'], config_revision=config['revision'],
            document_ids=[page['document_id']], page_ids=[page['id']], data_kinds=['image', 'text', 'metadata'])
