"""CPU safety/identity checks for the real cold-run driver, not qualification."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path[:0] = [
    'F:/OCR/output/platform-specialized-agent-20261007/worktrees/platform',
    'F:/OCR/output/platform-specialized-agent-20261007/worktrees/platform/src',
]
from cold_backup_restore_native import checkpoint_owned, copy_actual, database_state, inventory


def test_exact_byte_copy_keeps_source_wal_and_committed_rows_unchanged(tmp_path):
    assert tmp_path.drive.upper() == tmp_path.resolve().drive.upper() == 'F:'
    source, copied = tmp_path / 'source', tmp_path / 'new-copy'
    source.mkdir()
    with closing(sqlite3.connect(source / 'actual.sqlite3')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE records(id PRIMARY KEY,body)')
        db.execute('INSERT INTO records VALUES(1,?)', ('committed in WAL',))
        db.commit()
        before = inventory(source)
        assert 'actual.sqlite3-wal' in before and 'actual.sqlite3-shm' in before
        receipt = copy_actual(source, copied, {'actual.sqlite3': 'renamed.sqlite3'})
        assert len(receipt) == 3
        checkpoint_owned(copied)
        with closing(sqlite3.connect(copied / 'renamed.sqlite3')) as restored:
            assert restored.execute('SELECT * FROM records').fetchall() == [(1, 'committed in WAL')]
        assert inventory(source) == before


def test_logical_comparison_accepts_only_root_relocation_not_value_changes(tmp_path):
    assert tmp_path.resolve().drive.upper() == 'F:'
    source, target = tmp_path / 'source', tmp_path / 'restored'
    for root in (source, target):
        root.mkdir()
        with closing(sqlite3.connect(root / 'state.sqlite3')) as db, db:
            db.execute('CREATE TABLE records(id PRIMARY KEY,body)')
            db.execute('INSERT INTO records VALUES(1,?)', (json.dumps({'path': str(root / 'original.png'), 'amount': '-0.01'}),))
    assert database_state(source) == database_state(target)
    with closing(sqlite3.connect(target / 'state.sqlite3')) as db, db:
        db.execute('UPDATE records SET body=?', (json.dumps({'path': str(target / 'original.png'), 'amount': '0.01'}),))
    assert database_state(source) != database_state(target)
