"""Workspace identity, transactional updates, and durable SQLite migration."""

import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from workbench_platform.store import RevisionConflict, UnsupportedSchema, WorkspaceStore
from workbench_platform.store import workspaces as schema


def legacy_connection(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    schema._create_v1(db)
    db.execute("COMMIT")
    return db


def test_distinct_same_name_projects_and_durable_restart(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    store = WorkspaceStore(path)
    first = store.create_workspace("Case", workspace_id="one")
    second = store.create_workspace("Case", workspace_id="two")
    assert first.id != second.id and len(store.list_workspaces()) == 2
    a = store.add_link(first.id, "classification", "native-a", "同名", expected_revision=0)
    b = store.add_link(first.id, "classification", "native-b", "同名", expected_revision=1)
    c = store.add_link(first.id, "ocr", "native-a", "同名", expected_revision=2)
    reopened = WorkspaceStore(path)
    assert reopened.get_workspace("one").revision == 3
    assert {link.id for link in reopened.list_links("one")} == {a.id, b.id, c.id}
    assert {(link.module_id, link.native_project_id) for link in reopened.list_links("one")} == {
        ("classification", "native-a"), ("classification", "native-b"), ("ocr", "native-a")}
    assert reopened.list_links("two") == []
    with pytest.raises(sqlite3.IntegrityError):
        reopened.add_link("one", "classification", "native-a", "another label", expected_revision=3)
    assert reopened.get_workspace("one").revision == 3


def test_revision_conflict_and_concurrent_writers(tmp_path):
    store = WorkspaceStore(tmp_path / "store.sqlite3")
    store.create_workspace("Original", workspace_id="ws")

    def add(name):
        try:
            store.add_link("ws", "classification", name, "same", expected_revision=0)
            return "committed"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(add, ("A", "B"))) == ["committed", "conflict"]
    assert store.get_workspace("ws").revision == 1
    assert len(store.list_links("ws")) == 1
    with pytest.raises(RevisionConflict):
        store.rename_workspace("ws", "stale", expected_revision=0)
    assert store.get_workspace("ws").name == "Original"
    assert store.rename_workspace("ws", "New", expected_revision=1).revision == 2
    with pytest.raises(ValueError, match="Expected revision"):
        store.add_link("ws", "ocr", "p", "P", expected_revision=True)


def test_failed_second_statement_rolls_back_link_and_revision(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = WorkspaceStore(path)
    store.create_workspace("Case", workspace_id="ws")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_revision BEFORE UPDATE OF revision ON workspace "
                   "BEGIN SELECT RAISE(ABORT, 'injected revision failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.add_link("ws", "classification", "native", "Project", expected_revision=0)
    reopened = WorkspaceStore(path)
    assert reopened.get_workspace("ws").revision == 0
    assert reopened.list_links("ws") == []


def test_legacy_wal_backup_contains_committed_rows_and_can_restore(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    writer = legacy_connection(path)
    try:
        writer.execute("INSERT INTO workspace(id, name) VALUES ('ws', 'legacy')")
        writer.execute("INSERT INTO module_project_link VALUES ('link', 'ws', 'classification', 'native', 'P')")
        assert (tmp_path / "legacy.sqlite3-wal").stat().st_size > 0
        migrated = WorkspaceStore(path)
        assert migrated.migration_backup and migrated.migration_backup.is_file()
        assert migrated.get_workspace("ws").revision == 0
        assert migrated.list_links("ws")[0].native_project_id == "native"
        backup = migrated.migration_backup
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            assert db.execute("SELECT id FROM module_project_link").fetchone()[0] == "link"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored_path = tmp_path / "restored.sqlite3"
        shutil.copy2(backup, restored_path)
        restored = WorkspaceStore(restored_path)
        assert restored.get_workspace("ws").name == "legacy"
        assert restored.list_links("ws")[0].native_project_id == "native"
    finally:
        writer.close()


def test_migration_failure_rolls_back_ddl_and_retains_usable_backup(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    with legacy_connection(path) as db:
        db.execute("INSERT INTO workspace(id, name) VALUES ('ws', 'unchanged')")
    real_upgrade = schema._upgrade_v1_to_v2

    def interrupt(db):
        db.execute("ALTER TABLE workspace ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        raise RuntimeError("interrupted migration")

    monkeypatch.setattr(schema, "_upgrade_v1_to_v2", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [row[1] for row in db.execute("PRAGMA table_info(workspace)")] == ["id", "name"]
        assert db.execute("SELECT name FROM workspace WHERE id='ws'").fetchone()[0] == "unchanged"
    backups = list(tmp_path.glob("legacy.sqlite3.before-v1-to-v2-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    monkeypatch.setattr(schema, "_upgrade_v1_to_v2", real_upgrade)
    assert WorkspaceStore(path).get_workspace("ws").revision == 0


def test_new_database_migration_failure_does_not_leave_partial_tables(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"

    def interrupt(db):
        db.execute("ALTER TABLE workspace ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        raise RuntimeError("new database interrupted")

    monkeypatch.setattr(schema, "_upgrade_v1_to_v2", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_unknown_legacy_and_future_versions_rejected_without_rewrite(tmp_path):
    unknown = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown) as db:
        db.execute("CREATE TABLE unrelated(secret TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserved')")
    with pytest.raises(UnsupportedSchema, match="unversioned"):
        WorkspaceStore(unknown)
    with sqlite3.connect(unknown) as db:
        assert db.execute("SELECT secret FROM unrelated").fetchone()[0] == "preserved"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    future = tmp_path / "future.sqlite3"
    with sqlite3.connect(future) as db:
        db.execute("PRAGMA user_version=99")
        db.execute("CREATE TABLE marker(value TEXT)")
    with pytest.raises(UnsupportedSchema, match="future"):
        WorkspaceStore(future)
    with sqlite3.connect(future) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
        assert db.execute("SELECT name FROM sqlite_master WHERE name='marker'").fetchone()


def test_legacy_duplicate_identity_is_not_merged(tmp_path):
    path = tmp_path / "duplicate.sqlite3"
    with legacy_connection(path) as db:
        db.execute("INSERT INTO workspace VALUES ('ws', 'Case')")
        db.execute("INSERT INTO module_project_link VALUES ('a', 'ws', 'classification', 'same', 'Name')")
        db.execute("INSERT INTO module_project_link VALUES ('b', 'ws', 'classification', 'same', 'Other')")
    with pytest.raises(sqlite3.IntegrityError):
        WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM module_project_link").fetchone()[0] == 2


def test_link_removal_is_scoped_and_revision_checked(tmp_path):
    store = WorkspaceStore(tmp_path / "remove.sqlite3")
    store.create_workspace("A", workspace_id="a")
    store.create_workspace("B", workspace_id="b")
    link = store.add_link("a", "classification", "native", "Name", expected_revision=0)
    with pytest.raises(KeyError, match="link not found"):
        store.remove_link("b", link.id, expected_revision=0)
    assert store.get_workspace("b").revision == 0
    with pytest.raises(RevisionConflict):
        store.remove_link("a", link.id, expected_revision=0)
    store.remove_link("a", link.id, expected_revision=1)
    assert store.get_workspace("a").revision == 2 and store.list_links("a") == []
