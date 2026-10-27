"""Relocation preserves evidence bytes and frozen resources while moving pointers."""
import json
import sqlite3

import pytest

from packages.runtime.migration import relocate_operational, relocate_path, relocate_sqlite

OLD = r'F:\old DATA'
NEW = r'F:\新位置 DATA'


def test_structural_command_map_does_not_replace_business_strings_or_embedded_snapshots():
    value = {'native': {'body_file': OLD + r'\native-requests\1.json'},
             'payload': {'session_dir': OLD + r'\session', 'comment': OLD + r'\invoice.xlsx'},
             'request_id': 'same-request', 'model_binding': {'path': OLD + r'\model', 'sha256': 'a' * 64},
             'snapshot': {'body_file': OLD + r'\original.json'}, 'text': OLD + r'\quote'}
    moved = relocate_operational(value, OLD, NEW)
    assert moved['native']['body_file'] == NEW + r'\native-requests\1.json'
    assert moved['payload']['session_dir'] == NEW + r'\session'
    assert moved['payload']['comment'] == value['payload']['comment']
    for field in ('model_binding', 'snapshot', 'text', 'request_id'):
        assert moved[field] == value[field]
    assert value['native']['body_file'].startswith(OLD)


def test_exact_prefix_and_external_copies_respect_windows_case_and_path_boundaries():
    assert relocate_path(OLD.lower() + r'\a.xlsx', OLD, NEW) == NEW + r'\a.xlsx'
    assert relocate_path(OLD + r'ish\a.xlsx', OLD, NEW) == OLD + r'ish\a.xlsx'
    assert relocate_path('results/a.json', OLD, NEW) == 'results/a.json'
    assert relocate_path(OLD + r'\..\outside', OLD, NEW) == OLD + r'\..\outside'
    external = r'F:\import\original.png'
    assert relocate_path(external, OLD, NEW, {external: 'restored-inputs/hash.png'}) == NEW + r'\restored-inputs\hash.png'
    with pytest.raises(ValueError, match='relative DATA member'):
        relocate_path(external, OLD, NEW, {external: '../escape.png'})


def test_native_ocr_relocation_preserves_result_json_hashes_revisions_and_unknown_path_columns(tmp_path):
    with sqlite3.connect(tmp_path / 'ocr.sqlite3') as db:
        db.executescript('''PRAGMA user_version=22;
            CREATE TABLE images(id TEXT PRIMARY KEY,original_path TEXT,sha256 TEXT);
            CREATE TABLE versions(id TEXT PRIMARY KEY,path TEXT,sha256 TEXT);
            CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,engine TEXT);
            CREATE TABLE results(id TEXT,original TEXT,edited TEXT,revision INTEGER);
            CREATE TABLE new_business_records(path TEXT,content TEXT);''')
        raw = json.dumps({'path': OLD + r'\images\original.png', 'amount': '001,020.00', 'model_revision': 'pinned-old'}, ensure_ascii=False)
        db.execute('INSERT INTO images VALUES(?,?,?)', ('image', OLD + r'\images\original.png', 'a' * 64))
        db.execute('INSERT INTO versions VALUES(?,?,?)', ('version', 'images/relative.png', 'b' * 64))
        db.execute('INSERT INTO tasks VALUES(?,?,?)', ('task', 'running', 'glm'))
        db.execute('INSERT INTO results VALUES(?,?,?,?)', ('result', raw, raw, 7))
        db.execute('INSERT INTO new_business_records VALUES(?,?)', (OLD + r'\business.xlsx', raw))
        report = relocate_sqlite(db, OLD, NEW)
        assert db.execute('SELECT original_path,sha256 FROM images').fetchone() == (NEW + r'\images\original.png', 'a' * 64)
        assert db.execute('SELECT path FROM versions').fetchone()[0] == 'images/relative.png'
        assert db.execute('SELECT status,engine FROM tasks').fetchone() == ('interrupted', 'glm')
        assert db.execute('SELECT original,edited,revision FROM results').fetchone() == (raw, raw, 7)
        assert db.execute('SELECT path FROM new_business_records').fetchone()[0].startswith(OLD)
        assert report['updated_fields'] == {'images.original_path': 1}
        assert report['interrupted_jobs'] == 1


def test_classification_paths_move_but_frozen_manifest_and_resource_metadata_remain_and_leases_expire(tmp_path):
    with sqlite3.connect(tmp_path / 'classification.sqlite3') as db:
        db.executescript('''PRAGMA user_version=5;
            CREATE TABLE assets(id TEXT,path TEXT,sha256 TEXT);
            CREATE TABLE datasets(id TEXT,manifest_path TEXT,manifest_sha256 TEXT);
            CREATE TABLE models(id TEXT,package_path TEXT,metadata TEXT);
            CREATE TABLE jobs(id TEXT,state TEXT,lease_token TEXT,owner TEXT,lease_until REAL);
            CREATE TABLE leases(resource TEXT,token TEXT,expires REAL);''')
        pinned = json.dumps({'path': OLD + r'\model', 'checkpoint_sha256': 'c' * 64})
        db.execute('INSERT INTO assets VALUES(?,?,?)', ('a', r'F:\external\original.png', 'a' * 64))
        db.execute('INSERT INTO datasets VALUES(?,?,?)', ('d', OLD + r'\frozen.json', 'b' * 64))
        db.execute('INSERT INTO models VALUES(?,?,?)', ('m', OLD + r'\models\m', pinned))
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)', ('j', 'queued', 'old-token', 'old-pid', 1e12))
        db.execute('INSERT INTO leases VALUES(?,?,?)', ('worker', 'old-token', 1e12))
        report = relocate_sqlite(db, OLD, NEW, {r'F:\external\original.png': 'restored-inputs/a.png'})
        assert db.execute('SELECT path FROM assets').fetchone()[0] == NEW + r'\restored-inputs\a.png'
        assert db.execute('SELECT manifest_sha256 FROM datasets').fetchone()[0] == 'b' * 64
        assert db.execute('SELECT metadata FROM models').fetchone()[0] == pinned
        assert db.execute('SELECT state,lease_token,owner,lease_until FROM jobs').fetchone() == ('interrupted', None, None, None)
        assert db.execute('SELECT COUNT(*) FROM leases').fetchone()[0] == 0
        assert report['cleared_leases'] == 1


def test_live_document_state_changes_while_hashed_result_snapshot_remains_byte_identical(tmp_path):
    with sqlite3.connect(tmp_path / 'documents.sqlite3') as db:
        db.executescript('''PRAGMA user_version=1;
            CREATE TABLE documents(document_id TEXT,body TEXT);
            CREATE TABLE document_results(document_id TEXT,result_hash TEXT,body TEXT);''')
        original = json.dumps({'status': 'running', 'output_path': OLD + r'\document', 'request_id': 'same-request',
                               'document': {'path': OLD + r'\original', 'amount': '001.00'}})
        db.execute('INSERT INTO documents VALUES(?,?)', ('d', original))
        db.execute('INSERT INTO document_results VALUES(?,?,?)', ('d', 'a' * 64, original))
        relocate_sqlite(db, OLD, NEW)
        live = json.loads(db.execute('SELECT body FROM documents').fetchone()[0])
        assert live['status'] == 'needs_verification'
        assert live['output_path'].startswith(NEW)
        assert live['document'] == json.loads(original)['document']
        assert db.execute('SELECT result_hash,body FROM document_results').fetchone() == ('a' * 64, original)


def test_future_native_schema_refuses_before_any_field_or_job_mutation(tmp_path):
    with sqlite3.connect(tmp_path / 'future.sqlite3') as db:
        db.executescript('PRAGMA user_version=23; CREATE TABLE images(id TEXT,original_path TEXT);')
        db.execute('INSERT INTO images VALUES(?,?)', ('image', OLD + r'\original.png'))
        with pytest.raises(ValueError, match='Unsupported ocr SQLite version'):
            relocate_sqlite(db, OLD, NEW)
        assert db.execute('SELECT original_path FROM images').fetchone()[0].startswith(OLD)
