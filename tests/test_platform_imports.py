"""Native database upgrade boundary for persistent platform imports."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ocr_workbench.store import Store


class PlatformImportMigrationTests(unittest.TestCase):
    def test_upgrade_from_prior_schema_preserves_project_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            project = store.project("existing")
            database = root / "workbench.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute("DROP TABLE platform_import_items")
                    connection.execute("DROP TABLE platform_import_requests")
                    connection.execute("PRAGMA user_version=14")
            reopened = Store(root)
            self.assertEqual(reopened.schema_version, 15)
            self.assertEqual(reopened.one("projects", project["id"])["name"], "existing")
            with reopened.transaction() as connection:
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='platform_import_items'"
                ).fetchone())
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertTrue(reopened.migration_backup.is_file())


if __name__ == "__main__":
    unittest.main()
