"""Admit a typed business action, including a parsed goal's existing run."""
from .identity import Conflict, canonical


def start(runtime, session_id, goal, request_id, driver, saved, *, adopt_run_id=None):
    if adopt_run_id is None:
        return runtime.store.start_run(session_id, goal, request_id, 0, driver=driver, driver_state=saved)
    from .direct_scope import conflicts
    from .errors import ResourceBusy
    from workbench_platform.resources.reader_features import require_features
    require_features(runtime.store.root.parent, 'direct-' + driver, 'direct-resource-claims-v1', 'same-goal-continuation-v1')
    saved = {**saved, 'scope_workspace_id': runtime.store.session(session_id)['workspace_id']}
    with runtime.store.transaction() as db:
        run = dict(db.execute('SELECT * FROM runs WHERE id=?', (adopt_run_id,)).fetchone())
        if (run['session_id'], run['request_id'], run['driver'], run['status']) != (session_id, request_id, 'harness', 'running'):
            raise Conflict('目标解析的原运行已改变；未提交业务操作')
        if db.execute('SELECT 1 FROM operations WHERE run_id=?', (adopt_run_id,)).fetchone():
            raise Conflict('原目标已有业务回执，不能改成新的固定操作')
        for row in db.execute("SELECT * FROM runs WHERE id!=? AND status IN "
                              "('running','waiting_job','waiting_user','paused','needs_verification')", (adopt_run_id,)):
            if conflicts(driver, saved, runtime.store._resource_claims(dict(row))):
                raise ResourceBusy('同一业务对象有未完成操作（关联任务 ' + row['id'] + '），原目标已保留')
        db.execute('UPDATE runs SET driver=?,driver_state=? WHERE id=? AND generation=?',
                   (driver, canonical(saved), adopt_run_id, run['generation']))
    return runtime.store.run(adopt_run_id), True
