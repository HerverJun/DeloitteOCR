"""A cold reader must preserve an orphan WAL and never checkpoint the source."""
from contextlib import closing
import hashlib
import sqlite3
import subprocess
import sys

from scripts.backup_workspace import create, restore, verify


def test_cold_backup_absorbs_orphan_committed_wal_without_changing_source_database(tmp_path):
    data = tmp_path / 'source'
    data.mkdir()
    for name in ('assets.sqlite3', 'workspaces.sqlite3'):
        with closing(sqlite3.connect(data / name)) as db, db:
            db.execute('CREATE TABLE evidence(value TEXT)')
            db.execute("INSERT INTO evidence VALUES('main-file-row')")
    source = data / 'assets.sqlite3'
    child = """import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute("INSERT INTO evidence VALUES('orphan-wal-committed-row')")
db.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, '-I', '-B', '-c', child, str(source)], cwd=tmp_path, check=True)
    wal = source.with_name(source.name + '-wal')
    assert wal.is_file() and wal.stat().st_size > 0
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    wal_hash = hashlib.sha256(wal.read_bytes()).hexdigest()
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1
    archive, recovered = tmp_path / 'backup', tmp_path / 'restored'
    create(data, archive)
    verify(archive)
    restore(archive, recovered)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert wal.is_file() and hashlib.sha256(wal.read_bytes()).hexdigest() == wal_hash
    for target in (archive / source.name, recovered / source.name):
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            assert db.execute('SELECT value FROM evidence ORDER BY rowid').fetchall() == [
                ('main-file-row',), ('orphan-wal-committed-row',)]
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
