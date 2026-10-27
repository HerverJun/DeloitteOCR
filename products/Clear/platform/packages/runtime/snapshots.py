"""SQLite's own snapshot API, shared by single and central backups."""
from contextlib import closing
from pathlib import Path
import sqlite3


def sqlite_snapshot(source, target):
    # The caller holds the write barrier and has drained live native work.
    # rw allows rollback of a hot journal, but cannot create a missing source.
    with closing(sqlite3.connect(source.as_uri()+'?mode=rw', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Source SQLite integrity failed')
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite backup integrity failed')


def external_assets(root):
    """Only originals explicitly referenced by the native classification DB."""
    database = Path(root)/'native/classification/platform.sqlite3'
    if not database.is_file():
        return []
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        paths = [row[0] for row in db.execute('SELECT DISTINCT path FROM assets')] if 'assets' in tables else []
    from .config import _local, _no_reparse
    result = set()
    for value in paths:
        if not value:
            continue
        requested = Path(value)
        if not requested.is_absolute():
            requested = database.parent/requested
        requested = _local(requested, 'Registered external original')
        _no_reparse(requested, 'Registered external original')
        actual = requested.resolve()
        if actual.is_file() and not actual.is_relative_to(root):
            result.add(actual)
    return sorted(result)
