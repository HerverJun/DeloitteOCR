"""Real PostgreSQL + SQLite + original-file portable backup/restore regression."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from sqlalchemy import create_engine, select, text

from workbench_platform.tasks import schema_v1 as s
from workbench_platform.tasks.maintenance import backup, restore, verify_backup
from workbench_platform.tasks.private_payloads import seal
from .conftest import submission


def test_consistent_restore_preserves_frozen_evidence_and_blocks_private_replay(task_case, tmp_path, monkeypatch):
    ledger, admin, editor, viewer, other, workspace = task_case
    root = tmp_path / 'source DATA'
    root.mkdir()
    monkeypatch.delenv('CLEAR_INSTALL_CONFIG', raising=False)
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(root))
    monkeypatch.setenv('CLEAR_BACKUP_ROOT', str(tmp_path / 'backup outside DATA'))
    original = b'ORIGINAL 00001234 amount=99.90\x00\xff'
    blob = root / 'asset-blobs/original.bin'
    blob.parent.mkdir(); blob.write_bytes(original)
    original_hash = hashlib.sha256(original).hexdigest()
    frozen = {'model_id': 'pinned-legacy-model', 'sha256': 'a' * 64,
              'request_note': str(root / 'do-not-rewrite-business-text')}
    command = {'method': 'POST', 'payload': {'input_path': str(blob), 'description': str(blob)},
               'frozen_resource': frozen}
    value = submission(workspace, 'portable-roundtrip')
    value['options'] = {'frozen_resource': frozen, 'number': '00001234', 'amount': '99.90'}
    task = ledger.submit(editor, value, command, [{
        'asset_id': 'original', 'asset_revision': 7, 'blob_hash': original_hash,
        'name': '原件.bin', 'size_bytes': len(original)}])
    private_task = ledger.submit(editor, submission(workspace, 'portable-private'),
        seal(root, {'method': 'POST', 'payload': {'api_key': 'do-not-replay-with-copied-credentials'}}))
    with ledger.engine.connect() as db:
        request_hash = db.execute(select(s.tasks.c.request_hash).where(s.tasks.c.id == task['id'])).scalar_one()
    ledger.login(editor['username'], 'correct horse battery staple')
    artifact = root / 'documents/result.json'
    artifact.parent.mkdir(); artifact.write_text('{"number":"00001234","amount":"99.90"}', 'utf-8')
    with ledger.engine.begin() as db:
        db.execute(s.task_artifact_links.insert().values(id=task['id'] + '-output', task_id=task['id'],
            run_id=task['current_run_id'], name='result.json', kind='output', source_path=str(artifact),
            blob_hash=hashlib.sha256(artifact.read_bytes()).hexdigest(), size_bytes=artifact.stat().st_size))
    for relative, table, identity in (
        ('documents/documents.sqlite3', 'documents', 'document_id'),
        ('comparisons/comparisons.sqlite3', 'comparison_jobs', 'id'),
    ):
        path = root / relative
        path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f'CREATE TABLE {table}({identity} TEXT PRIMARY KEY,body TEXT)')
            db.execute('PRAGMA user_version=1')
            db.execute(f'INSERT INTO {table} VALUES(?,?)', ('legacy-job', json.dumps({
                'status': 'queued', 'input_path': str(blob), 'frozen_resource': frozen,
                'original_request': {'path': str(blob)}, 'revision': 7, 'result_hash': original_hash})))
    result = backup(ledger.engine, root)
    backup_root = Path(result['backup'])
    assert backup_root.is_relative_to(tmp_path / 'backup outside DATA')
    assert verify_backup(ledger.engine, backup_root)['verified']
    destination = tmp_path / '恢复 DATA'
    restored = restore(ledger.engine, backup_root, destination)
    restored_engine = create_engine(json.loads((destination / 'central-restore-private.json').read_text('utf-8'))['database_url'])
    try:
        assert restored['verified']
        assert blob.read_bytes() == original
        assert (destination / 'asset-blobs/original.bin').read_bytes() == original
        with restored_engine.connect() as db:
            row = db.execute(select(s.tasks).where(s.tasks.c.id == task['id'])).mappings().one()
            assert row['options'] == value['options']
            assert row['request_hash'] == request_hash
            dispatch = db.execute(select(s.task_dispatches).where(s.task_dispatches.c.task_id == task['id'])).mappings().one()
            assert dispatch['command']['payload']['input_path'] == str(destination / 'asset-blobs/original.bin')
            assert dispatch['command']['payload']['description'] == str(blob)
            assert dispatch['command']['frozen_resource'] == frozen
            private = db.execute(select(s.task_dispatches).where(s.task_dispatches.c.task_id == private_task['id'])).mappings().one()
            assert private['state'] == 'needs_verification'
            assert private['command']['private_payload_ref']
            assert db.execute(select(s.user_sessions.c.id)).first() is None
            assert db.execute(select(s.task_artifact_links.c.source_path).where(s.task_artifact_links.c.task_id == task['id'])).scalar_one() == str(destination / 'documents/result.json')
            restored_input = db.execute(select(s.task_inputs).where(s.task_inputs.c.task_id == task['id'])).mappings().one()
            assert restored_input['blob_hash'] == original_hash and restored_input['asset_revision'] == 7
        for relative, table, identity in (
            ('documents/documents.sqlite3', 'documents', 'document_id'),
            ('comparisons/comparisons.sqlite3', 'comparison_jobs', 'id'),
        ):
            with closing(sqlite3.connect(destination / relative)) as db:
                body = json.loads(db.execute(f'SELECT body FROM {table} WHERE {identity}=?', ('legacy-job',)).fetchone()[0])
            assert body['status'] == 'needs_verification'
            assert body['input_path'] == str(destination / 'asset-blobs/original.bin')
            assert body['frozen_resource'] == frozen
            assert body['original_request']['path'] == str(blob)
            assert body['revision'] == 7 and body['result_hash'] == original_hash
        assert (destination / 'migration/path-map-v1.json').is_file()
        assert not (destination / 'task-command-secrets').exists()
        assert (destination / 'migration/account-secrets-reenter/task-command-secrets').is_dir()
    finally:
        restored_engine.dispose()
        assert re.fullmatch('clear_restore_[a-f0-9]+', restored['database'])
        disposable_admin = create_engine(ledger.engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
        try:
            with disposable_admin.connect() as db:
                db.execute(text('DROP DATABASE "' + restored['database'] + '" WITH (FORCE)'))
        finally:
            disposable_admin.dispose()
