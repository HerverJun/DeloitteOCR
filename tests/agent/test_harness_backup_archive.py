"""Sealed migration SQLite bytes survive backup; live business WAL remains logical data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.backup_workspace import create, restore, verify


ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/archive-backup-tests')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArchiveBackupTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'source'
        self.data.mkdir()
        for name in ('assets.sqlite3', 'workspaces.sqlite3'):
            with closing(sqlite3.connect(self.data / name)) as db, db:
                db.execute('CREATE TABLE evidence(value TEXT)')
                db.execute("INSERT INTO evidence VALUES('main-file-row')")
        self.archive = self.data / 'agent/harness/legacy/snapshot-v2/operations.sqlite3'
        self.archive.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.archive)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('CREATE TABLE original_receipts(id INTEGER PRIMARY KEY,body TEXT)')
            db.commit()
            for ordinal in range(4):
                db.execute('INSERT INTO original_receipts VALUES(?,?)', (ordinal, 'immutable source receipt ' + str(ordinal)))
                db.commit()
        self.pinned = sha(self.archive)
        self.manifest = self.archive.parent / 'manifest.json'
        self.manifest.write_text(json.dumps({'operations.sqlite3': self.pinned}), encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def test_roundtrip_preserves_pinned_archive_bytes_and_absorbs_business_wal(self):
        original_manifest = self.manifest.read_bytes()
        business = self.data / 'assets.sqlite3'
        with closing(sqlite3.connect(business)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("INSERT INTO evidence VALUES('committed-only-in-business-wal')")
            db.commit()
            self.assertGreater(business.with_name(business.name + '-wal').stat().st_size, 0)
            with closing(sqlite3.connect(business.as_uri() + '?mode=ro&immutable=1', uri=True)) as disk:
                self.assertEqual(disk.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 1)
            backup, restored = self.root / 'backup', self.root / 'restored'
            result = create(self.data, backup)
            relative = self.archive.relative_to(self.data)
            self.assertEqual(result['files'][relative.as_posix()]['sha256'], self.pinned)
            self.assertEqual(sha(backup / relative), self.pinned)
            verify(backup)
            restore(backup, restored)
            self.assertEqual(sha(restored / relative), self.pinned)
            self.assertEqual(sha(self.archive), self.pinned)
            self.assertEqual((restored / self.manifest.relative_to(self.data)).read_bytes(), original_manifest)
            self.assertEqual(self.manifest.read_bytes(), original_manifest)
            for target in (backup / 'assets.sqlite3', restored / 'assets.sqlite3'):
                with closing(sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)) as copied:
                    self.assertEqual(copied.execute('SELECT value FROM evidence ORDER BY rowid').fetchall(),
                        [('main-file-row',), ('committed-only-in-business-wal',)])
                    self.assertEqual(copied.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertFalse(any(name.endswith(('-wal', '-shm')) for name in result['files']))

    def test_archive_with_any_journal_sidecar_is_rejected_without_rewriting_evidence(self):
        for suffix in ('-journal', '-wal', '-shm'):
            with self.subTest(suffix=suffix):
                sidecar = self.archive.with_name(self.archive.name + suffix)
                sidecar.write_bytes(b'not a sealed archive')
                destination = self.root / ('reject' + suffix)
                try:
                    with self.assertRaisesRegex(ValueError, 'journal files'):
                        create(self.data, destination)
                    self.assertFalse(destination.exists())
                    self.assertEqual(sha(self.archive), self.pinned)
                    self.assertEqual(sidecar.read_bytes(), b'not a sealed archive')
                finally:
                    sidecar.unlink()

    def test_wal_header_archive_is_rejected_even_when_sqlite_removed_all_sidecars(self):
        with closing(sqlite3.connect(self.archive)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            db.execute("INSERT INTO original_receipts VALUES(99,'closed WAL archive')")
            db.commit()
        self.assertEqual(self.archive.read_bytes()[18:20], b'\x02\x02')
        self.assertFalse(any(self.archive.with_name(self.archive.name + suffix).exists()
                             for suffix in ('-journal', '-wal', '-shm')))
        before = self.archive.read_bytes()
        with self.assertRaisesRegex(ValueError, 'WAL or unsupported header'):
            create(self.data, self.root / 'reject-wal-header')
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertFalse((self.root / 'reject-wal-header').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
