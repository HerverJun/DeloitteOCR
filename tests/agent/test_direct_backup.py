"""Cold direct-run backups retain receipts without requiring legacy checkpoints."""
from contextlib import closing
import sqlite3

import pytest

from scripts.backup_workspace import DIRECT_BACKUP_DRIVERS, create, restore, verify


def data_root(tmp_path, driver):
    root = tmp_path / 'data'
    (root / 'agent').mkdir(parents=True)
    for name in ('assets.sqlite3', 'workspaces.sqlite3'):
        with closing(sqlite3.connect(root / name)) as db, db:
            db.execute('CREATE TABLE original_identity(id TEXT PRIMARY KEY)')
            db.execute("INSERT INTO original_identity VALUES('unchanged-original')")
    with closing(sqlite3.connect(root / 'agent/operations.sqlite3')) as db, db:
        db.execute('CREATE TABLE runs(id TEXT,session_id TEXT,driver TEXT,driver_state TEXT)')
        db.execute('CREATE TABLE operations(id TEXT,run_id TEXT,state TEXT,result TEXT)')
        db.execute('INSERT INTO runs VALUES(?,?,?,?)',
                   ('original-run', 'original-session', driver, '{"version":1,"stage":"wait"}'))
        db.execute('INSERT INTO operations VALUES(?,?,?,?)',
                   ('original-operation', 'original-run', 'committed', '{"native_id":"original-native-job"}'))
    return root


@pytest.mark.parametrize('driver', sorted(DIRECT_BACKUP_DRIVERS))
def test_direct_cold_backup_restore_preserves_original_receipts(tmp_path, driver):
    root = data_root(tmp_path, driver)
    backup, recovered = tmp_path / 'backup', tmp_path / 'recovered'
    manifest = create(root, backup)
    assert 'agent/operations.sqlite3' in manifest['files']
    verify(backup)
    restore(backup, recovered)
    assert not (recovered / 'agent/checkpoints.sqlite3').exists()
    for target in (root, recovered):
        with closing(sqlite3.connect(target / 'agent/operations.sqlite3')) as db:
            assert db.execute('SELECT * FROM runs').fetchall() == [
                ('original-run', 'original-session', driver, '{"version":1,"stage":"wait"}')]
            assert db.execute('SELECT * FROM operations').fetchall() == [
                ('original-operation', 'original-run', 'committed', '{"native_id":"original-native-job"}')]


def test_direct_run_does_not_relax_legacy_checkpoint_requirement(tmp_path):
    root = data_root(tmp_path, 'image-text-v1')
    with closing(sqlite3.connect(root / 'agent/operations.sqlite3')) as db, db:
        db.execute("INSERT INTO runs VALUES('legacy-run','legacy-session','harness','{}')")
        db.execute("INSERT INTO operations VALUES('legacy-write','legacy-run','committed','{}')")
    with pytest.raises(ValueError, match='Legacy Agent backup requires'):
        create(root, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_direct_run_keeps_existing_harness_log_requirement(tmp_path):
    root = data_root(tmp_path, 'pdf-text-v1')
    with closing(sqlite3.connect(root / 'agent/operations.sqlite3')) as db, db:
        db.execute('CREATE TABLE harness_events(session_id TEXT)')
        db.execute("INSERT INTO harness_events VALUES('original-session')")
    with pytest.raises(ValueError, match='Harness backup requires one durable session log'):
        create(root, tmp_path / 'backup')


def test_pre_driver_legacy_database_still_requires_checkpoint(tmp_path):
    root = data_root(tmp_path, 'harness')
    with closing(sqlite3.connect(root / 'agent/operations.sqlite3')) as db, db:
        db.execute('ALTER TABLE runs DROP COLUMN driver')
    with pytest.raises(ValueError, match='Legacy Agent backup requires'):
        create(root, tmp_path / 'backup')
