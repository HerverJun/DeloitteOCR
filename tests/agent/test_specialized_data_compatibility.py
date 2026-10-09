"""Actual-record capability inference, cold persistence, and binding rejection (CPU)."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.backup_workspace import create, restore, verify
from scripts.product_compatibility import DATA_COMPATIBILITY, check
from workbench_platform.agent.preferences import TaskPreferences
from workbench_platform.agent.store import AgentStore, canonical, digest
from workbench_platform.reader_requirements import (
    infer_reader_requirements, mark_reader_feature, marker_requirements, value_hash,
)


def files(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def platform_root(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ('assets.sqlite3', 'workspaces.sqlite3'):
        with closing(sqlite3.connect(root / name)):
            pass
    return root


def preferences(root, *, snapshot=True, source_mode='user_form'):
    store = AgentStore(root / 'agent')
    service = TaskPreferences(store)
    session = store.create_session('work1', 'CPU business fixture', ['asset1'], 'session-one')
    run = None
    if snapshot or source_mode == 'user_event':
        run, _ = store.start_run(session['id'], '默认以后每张图片一个Excel', 'run-one', 1)
    source = {'mode': 'user_form', 'quote': '用户明确保存默认值'}
    context = None
    if source_mode == 'user_event':
        from types import SimpleNamespace
        seq = store.rows("SELECT seq FROM events WHERE run_id=? AND kind='message'", (run['id'],))[0]['seq']
        source = {'mode': 'user_event', 'quote': '默认以后每张图片一个Excel', 'event_seq': seq}
        context = SimpleNamespace(workspace_id='work1', session_id=session['id'], run_id=run['id'], generation=run['generation'])
    service.update('work1', 'screen_excel', 0, {'output_layout': 'per_image', 'value_mode': 'text'}, [],
                   source, 'save-one', context=context)
    frozen = service.freeze('work1', session['id'], run['id'], 'screen_excel') if snapshot else None
    if run:
        with store.transaction() as db:
            db.execute("UPDATE runs SET status='completed' WHERE id=?", (run['id'],))
        # The real saver is not started; this test exercises cold paired stores.
        with closing(sqlite3.connect(root / 'agent/checkpoints.sqlite3')) as db, db:
            db.execute('CREATE TABLE checkpoint_fixture(key TEXT)')
    return store, service, session, run, frozen


def delivery(root, *, full_vector=False):
    path = root / 'screen-batches.sqlite3'
    options = {'include_pending_review': False, 'numeric_columns': None}
    source = [{'input_version_id': 'version1', 'image_id': 'image1'}]
    identity = {'batch_id': 'batch1', 'project_id': 'project1', 'policy_sha256': 'a' * 64,
                'items': [{'input_version_id': 'version1', 'image_id': 'image1', 'result_revision': 0}]}
    snapshot = {'snapshot_id': value_hash(identity), 'export_options': options}
    if full_vector:
        snapshot['version_vector'] = {**identity, **snapshot}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''CREATE TABLE screen_batch_refs(workspace_id TEXT,batch_id TEXT,project_id TEXT,source_json TEXT);
            CREATE TABLE screen_batch_exports(workspace_id TEXT,batch_id TEXT,payload_hash TEXT,bundle_id TEXT,snapshot_json TEXT);''')
        db.execute('INSERT INTO screen_batch_refs VALUES(?,?,?,?)', ('work1', 'batch1', 'project1', json.dumps(source)))
        db.execute('INSERT INTO screen_batch_exports VALUES(?,?,?,?,?)', ('work1', 'batch1', 'b' * 64, 'bundle1', json.dumps(snapshot)))
    return path


def resolution(root, *, standalone=False, status='intent'):
    path = root / 'workbench.sqlite3' if standalone else root / 'native/ocr/workbench.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    policy = {'primary_engine': 'glm', 'require_human_review': True}
    bindings = [{'input_version_id': 'version1', 'result_id': 'result1', 'result_revision': 0,
                 'source_sha256': 'a' * 64, 'table_snapshot_sha256': 'b' * 64, 'problem_signature': 'c' * 64}]
    value = {'resolution_version': 'screen-resolution-v1', 'status': status, 'parent_batch_id': 'batch1',
             'values_modified': False, 'selection_applied': False, 'human_review': 'not_performed',
             'bindings': bindings, 'policy': policy,
             'submit_body': {'request_id': 'child-request', 'version_ids': ['version1'], 'policy': policy}}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''PRAGMA user_version=18;
            CREATE TABLE screen_actions(batch_id TEXT,request_id TEXT,payload_hash TEXT,receipt TEXT);
            CREATE TABLE screen_batches(id TEXT,project_id TEXT);
            CREATE TABLE versions(id TEXT,image_id TEXT,sha256 TEXT);
            CREATE TABLE results(id TEXT,task_id TEXT,revision INTEGER);
            CREATE TABLE tasks(id TEXT,version_id TEXT,image_id TEXT,project_id TEXT);''')
        db.execute('INSERT INTO screen_batches VALUES(?,?)', ('batch1', 'project1'))
        db.execute('INSERT INTO versions VALUES(?,?,?)', ('version1', 'image1', 'a' * 64))
        db.execute('INSERT INTO tasks VALUES(?,?,?,?)', ('task1', 'version1', 'image1', 'project1'))
        db.execute('INSERT INTO results VALUES(?,?,?)', ('result1', 'task1', 0))
        db.execute('INSERT INTO screen_actions VALUES(?,?,?,?)', ('batch1', 'request-one', 'd' * 64, json.dumps(value)))
    return path


def old_reader(feature):
    descriptor = deepcopy(DATA_COMPATIBILITY)
    descriptor['reader_features'].remove(feature)
    return descriptor


@pytest.mark.parametrize('standalone', [False, True])
@pytest.mark.parametrize('foreign_root_batch', [False, True])
def test_local_candidate_inference_preserves_workspace_path_and_root_batch_ownership(
        tmp_path, standalone, foreign_root_batch):
    root = platform_root(tmp_path / 'data')
    path = resolution(root, standalone=standalone)
    snapshot = {'input_version_id': 'version1', 'result_id': 'result1',
                'result_revision': 0, 'batch_id': 'batch1', 'source_sha256': 'a' * 64}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''PRAGMA user_version=19;
            ALTER TABLE tasks ADD COLUMN input_version_id TEXT;
            ALTER TABLE tasks ADD COLUMN kind TEXT;
            ALTER TABLE tasks ADD COLUMN engine TEXT;
            CREATE TABLE screen_local_requests(task_id TEXT,batch_id TEXT,root_batch_id TEXT,
                request_id TEXT,input_version_id TEXT,parent_result_id TEXT,pixel_key TEXT,
                snapshot TEXT,metadata TEXT,candidate_state TEXT);''')
        db.execute('UPDATE tasks SET input_version_id=version_id,kind=?,engine=?', ('ocr', 'hunyuan'))
        db.execute('INSERT INTO screen_batches VALUES(?,?)',
                   ('root-batch', 'foreign-project' if foreign_root_batch else 'project1'))
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                   ('local-task', 'version1', 'image1', 'project1', 'version1', 'screen_local_text', 'glm'))
        db.execute('INSERT INTO screen_local_requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('local-task', 'batch1', 'root-batch', 'request-local', 'version1', 'result1',
                    'e' * 64, json.dumps(snapshot), '{}', 'available'))
    before = files(root)
    if foreign_root_batch:
        with pytest.raises(ValueError, match='ownership'):
            infer_reader_requirements(root)
    else:
        inferred = infer_reader_requirements(root)
        assert 'screen-local-candidates-v1' in inferred['reader_features']
        assert check(root, DATA_COMPATIBILITY)['compatible']
        assert any(item.get('task_id') == 'local-task' for item in inferred['evidence'])
    assert files(root) == before


@pytest.mark.parametrize('feature', ['agent-preferences-v1', 'screen-delivery-snapshot-v1', 'screen-resolution-v1'])
def test_missing_marker_cannot_hide_actual_specialized_records(tmp_path, feature):
    root = platform_root(tmp_path / 'data')
    if feature == 'agent-preferences-v1':
        preferences(root)
    elif feature == 'screen-delivery-snapshot-v1':
        delivery(root)
    else:
        resolution(root)
    marker = root / 'product-reader-requirements.json'
    if marker.exists():
        marker.unlink()  # deliberate CPU fixture: an omitted marker is the tested fault
    before = files(root)
    assert feature in check(root, DATA_COMPATIBILITY)['required_reader_features']
    with pytest.raises(ValueError, match='不能直接降级'):
        check(root, old_reader(feature))
    assert files(root) == before
    assert not marker.exists()


def test_old_databases_empty_optional_tables_and_no_snapshot_column_remain_compatible(tmp_path):
    root = platform_root(tmp_path / 'data')
    AgentStore(root / 'agent')
    with closing(sqlite3.connect(root / 'screen-batches.sqlite3')) as db, db:
        db.execute('CREATE TABLE screen_batch_exports(workspace_id TEXT,batch_id TEXT,payload_hash TEXT,bundle_id TEXT)')
    descriptor = deepcopy(DATA_COMPATIBILITY)
    descriptor['reader_features'] = []
    assert check(root, descriptor)['compatible']
    TaskPreferences(AgentStore(root / 'agent'))
    assert check(root, descriptor)['compatible']
    assert not infer_reader_requirements(root)['reader_features']


def test_backup_derives_private_marker_and_cold_restores_frozen_preference(tmp_path):
    root = platform_root(tmp_path / 'source')
    store, service, session, run, frozen = preferences(root)
    marker = root / 'product-reader-requirements.json'
    marker.write_text(json.dumps({'format': 'clear-product-reader-requirements-v1',
                                 'reader_features': ['fixed-draft-transfer-v1'], 'note': 'keep origin fields'}), 'utf-8')
    before = files(root)
    backup = tmp_path / 'backup'
    manifest = create(root, backup)
    assert files(root) == before
    assert 'product-reader-requirements.json' in manifest['files']
    required = marker_requirements(backup)
    assert set(required['reader_features']) == {'fixed-draft-transfer-v1', 'agent-preferences-v1'}
    assert required['note'] == 'keep origin fields'
    assert verify(backup)['files'] == manifest['files']
    restored = tmp_path / 'restored'
    restore(backup, restored)
    reopened = TaskPreferences(AgentStore(restored / 'agent'))
    assert reopened.preview('work1', 'screen_excel') == service.preview('work1', 'screen_excel')
    assert reopened.snapshots('work1', session['id'], run['id'])['snapshots'][0] == frozen
    reopened.update('work1', 'screen_excel', 1, {'value_mode': 'confirmed_numeric'}, [],
                    {'mode': 'user_form', 'quote': '更改以后任务默认值'}, 'save-two')
    assert reopened.freeze('work1', session['id'], run['id'], 'screen_excel') == frozen
    assert reopened.preview('work1', 'screen_excel')['effective_values']['value_mode'] == 'confirmed_numeric'
    assert check(restored, DATA_COMPATIBILITY)['compatible']
    with pytest.raises(ValueError, match='不能直接降级'):
        check(restored, old_reader('agent-preferences-v1'))


@pytest.mark.parametrize('kind', ['snapshot_sha', 'snapshot_workspace', 'snapshot_scope', 'snapshot_values', 'profile_source', 'profile_business', 'request_source'])
def test_preference_snapshot_and_source_tamper_rejected_readonly(tmp_path, kind):
    root = platform_root(tmp_path / 'data')
    store, *_ = preferences(root)
    with store.transaction() as db:
        if kind in ('profile_source', 'profile_business'):
            row = db.execute('SELECT body FROM agent_preference_profiles').fetchone()
            value = json.loads(row['body'])
            if kind == 'profile_source':
                value['output_layout']['workspace_id'] = 'other-workspace'
            else:
                value['output_layout']['business'] = 'project'
            db.execute('UPDATE agent_preference_profiles SET body=?', (canonical(value),))
        elif kind == 'request_source':
            row = db.execute('SELECT response FROM agent_preference_requests').fetchone()
            value = json.loads(row['response'])
            value['workspace_id'] = 'other-workspace'
            db.execute('UPDATE agent_preference_requests SET response=?', (canonical(value),))
        else:
            row = db.execute('SELECT body FROM agent_preference_snapshots').fetchone()
            value = json.loads(row['body'])
            if kind == 'snapshot_workspace':
                value['workspace_id'] = 'other-workspace'
            elif kind == 'snapshot_scope':
                value['scope_sha256'] = '0' * 64
            elif kind == 'snapshot_values':
                value['values']['value_mode'] = 'confirmed_numeric'
            sha = '0' * 64 if kind == 'snapshot_sha' else digest(value)
            db.execute('UPDATE agent_preference_snapshots SET body=?,sha256=?', (canonical(value), sha))
    before = files(root)
    with pytest.raises(ValueError):
        check(root, DATA_COMPATIBILITY)
    assert files(root) == before


def test_preference_user_origin_requires_actual_project_event(tmp_path):
    root = platform_root(tmp_path / 'data')
    store, *_ = preferences(root, source_mode='user_event')
    assert check(root, DATA_COMPATIBILITY)['compatible']
    with store.transaction() as db:
        db.execute("UPDATE events SET payload=? WHERE kind='message'", (json.dumps({'role': 'assistant', 'content': '默认以后每张图片一个Excel'}),))
    with pytest.raises(ValueError, match='actual project user event'):
        check(root, DATA_COMPATIBILITY)


@pytest.mark.parametrize('kind', ['source', 'ownership', 'intent'])
def test_resolution_frozen_source_result_and_intent_tamper_rejected(tmp_path, kind):
    root = platform_root(tmp_path / 'data')
    path = resolution(root)
    assert check(root, DATA_COMPATIBILITY)['compatible']
    with closing(sqlite3.connect(path)) as db, db:
        value = json.loads(db.execute('SELECT receipt FROM screen_actions').fetchone()[0])
        if kind == 'source':
            value['bindings'][0]['source_sha256'] = '0' * 64
        elif kind == 'ownership':
            db.execute("UPDATE tasks SET project_id='other-project'")
        else:
            value['submit_body']['version_ids'] = ['other-version']
        db.execute('UPDATE screen_actions SET receipt=?', (json.dumps(value),))
    before = files(root)
    with pytest.raises(ValueError):
        check(root, DATA_COMPATIBILITY)
    assert files(root) == before


def test_resolution_old_revision_is_history_not_current_and_unknown_version_rejected(tmp_path):
    root = platform_root(tmp_path / 'data')
    path = resolution(root, status='submitted')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('UPDATE results SET revision=1')
    assert check(root, DATA_COMPATIBILITY)['compatible']
    with closing(sqlite3.connect(path)) as db, db:
        value = json.loads(db.execute('SELECT receipt FROM screen_actions').fetchone()[0])
        value['resolution_version'] = 'screen-resolution-v99'
        db.execute('UPDATE screen_actions SET receipt=?', (json.dumps(value),))
    with pytest.raises(ValueError):
        check(root, DATA_COMPATIBILITY)


def test_standalone_native_backup_retains_unsubmitted_intent_without_source_write(tmp_path):
    root = tmp_path / 'native-source'
    root.mkdir()
    resolution(root, standalone=True)
    before = files(root)
    backup = tmp_path / 'native-backup'
    manifest = create(root, backup, kind='ocr')
    assert files(root) == before
    assert 'product-reader-requirements.json' in manifest['files']
    assert 'screen-resolution-v1' in marker_requirements(backup)['reader_features']
    restored = tmp_path / 'native-restored'
    restore(backup, restored)
    with closing(sqlite3.connect(restored / 'workbench.sqlite3')) as db:
        receipt = json.loads(db.execute('SELECT receipt FROM screen_actions').fetchone()[0])
        assert receipt['status'] == 'intent' and receipt['submit_body']['request_id'] == 'child-request'
    assert infer_reader_requirements(restored)['reader_features'] == ['screen-resolution-v1']


@pytest.mark.parametrize('kind', ['batch_source', 'vector_source', 'vector_sha'])
def test_delivery_record_actual_batch_and_full_vector_tamper_rejected(tmp_path, kind):
    root = platform_root(tmp_path / 'data')
    path = delivery(root, full_vector=True)
    assert check(root, DATA_COMPATIBILITY)['compatible']
    with closing(sqlite3.connect(path)) as db, db:
        value = json.loads(db.execute('SELECT snapshot_json FROM screen_batch_exports').fetchone()[0])
        if kind == 'batch_source':
            db.execute("UPDATE screen_batch_exports SET workspace_id='other-workspace'")
        elif kind == 'vector_source':
            value['version_vector']['items'][0]['image_id'] = 'other-image'
        else:
            value['version_vector']['policy_sha256'] = '0' * 64
        db.execute('UPDATE screen_batch_exports SET snapshot_json=?', (json.dumps(value),))
    before = files(root)
    with pytest.raises(ValueError):
        check(root, DATA_COMPATIBILITY)
    assert files(root) == before


def test_marker_merge_and_untracked_capability_cannot_be_ignored_by_backup(tmp_path):
    root = platform_root(tmp_path / 'source')
    preferences(root, snapshot=False)
    marker = root / 'product-reader-requirements.json'
    marker.write_text(json.dumps({'format': 'clear-product-reader-requirements-v1', 'reader_features': ['future-reader-v9'], 'note': 'keep'}), 'utf-8')
    mark_reader_feature(root, 'agent-preferences-v1')
    assert marker_requirements(root)['reader_features'] == ['agent-preferences-v1', 'future-reader-v9']
    assert marker_requirements(root)['note'] == 'keep'
    with pytest.raises(ValueError, match='不能直接降级'):
        check(root, DATA_COMPATIBILITY)
    backup = tmp_path / 'backup'
    create(root, backup)
    marker = backup / 'product-reader-requirements.json'
    marker.unlink()
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    manifest['files'].pop('product-reader-requirements.json')
    manifest_path.write_text(json.dumps(manifest), 'utf-8')
    with pytest.raises(ValueError, match='reader capabilities'):
        verify(backup)
