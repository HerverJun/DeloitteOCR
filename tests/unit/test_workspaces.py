"""Contract-compatible workspace persistence and recoverable migration."""

import re
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from packages.contracts.models import ModuleProjectLink as WireLink
from packages.contracts.models import Workspace as WireWorkspace
from workbench_platform.store import RevisionConflict, UnsupportedSchema, WorkspaceStore
from workbench_platform.store import workspaces as schema


def legacy_connection(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    schema._create_v1(db)  # Defined compatibility fixture, not production history.
    db.execute("COMMIT")
    return db


def link(store, workspace_id, app_id, project_id, expected_revision, role="owner"):
    return store.add_link(workspace_id, app_id, project_id, role=role,
                          expected_revision=expected_revision)


def exercise_post_migration_writes(store):
    assert store.get_workspace("ws").revision == 1
    renamed = store.rename_workspace("ws", "After migration", expected_revision=1)
    assert renamed.revision == 2 and WireWorkspace.model_validate(asdict(renamed))
    actual_ocr_id = "9" + "a" * 31
    added = link(store, "ws", "ocr", actual_ocr_id, 2)
    assert WireLink.model_validate(asdict(added)).module_project_id == actual_ocr_id
    changed = store.update_link_role("ws", added.link_id, "viewer",
                                     expected_revision=3, expected_link_revision=1)
    assert changed.revision == 2 and changed.role == "viewer"
    store.remove_link("ws", added.link_id, expected_revision=4)
    assert store.get_workspace("ws").revision == 5
    fresh = store.create_workspace("Fresh", workspace_id="fresh")
    assert fresh.revision == 1
    assert link(store, "fresh", "classification", "native_a", 1).revision == 1


def test_contract_fields_utc_and_same_name_distinct_projects_survive_restart(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    store = WorkspaceStore(path)
    first = store.create_workspace("Case", workspace_id="one")
    second = store.create_workspace("Case", workspace_id="two")
    assert first.workspace_id != second.workspace_id and len(store.list_workspaces()) == 2
    assert first.revision == 1 and first.created_at == first.updated_at
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z", first.created_at)
    assert WireWorkspace.model_validate(asdict(first)).workspace_id == "one"
    a = link(store, "one", "classification", "native_a", 1)
    b = link(store, "one", "classification", "native_b", 2, role="editor")
    c = link(store, "one", "ocr", "native_a", 3, role="viewer")
    assert [WireLink.model_validate(asdict(value)).role for value in (a, b, c)] == [
        "owner", "editor", "viewer"]
    reopened = WorkspaceStore(path)
    assert WireWorkspace.model_validate(asdict(reopened.get_workspace("one"))).revision == 4
    assert {item.link_id for item in reopened.list_links("one")} == {a.link_id, b.link_id, c.link_id}
    assert {(item.app_id, item.module_project_id) for item in reopened.list_links("one")} == {
        ("classification", "native_a"), ("classification", "native_b"), ("ocr", "native_a")}
    assert reopened.list_links("two") == []
    with pytest.raises(sqlite3.IntegrityError):
        link(reopened, "one", "classification", "native_a", 4)
    assert reopened.get_workspace("one").revision == 4
    assert WireWorkspace.model_validate(asdict(store.create_workspace("Auto"))).workspace_id.startswith("w")
    assert WireLink.model_validate(asdict(link(store, "two", "ocr", "p", 1))).link_id.startswith("l")
    assert WireLink.model_validate(asdict(link(store, "two", "ocr", "9" + "a" * 31, 2))).module_project_id[0] == "9"
    with pytest.raises(ValueError, match="module project id"):
        link(store, "two", "ocr", "../private", 3)


def test_revision_conflict_and_concurrent_writers(tmp_path):
    store = WorkspaceStore(tmp_path / "store.sqlite3")
    store.create_workspace("Original", workspace_id="ws")

    def add(project_id):
        try:
            link(store, "ws", "classification", project_id, 1)
            return "committed"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(add, ("A", "B"))) == ["committed", "conflict"]
    assert store.get_workspace("ws").revision == 2 and len(store.list_links("ws")) == 1
    with pytest.raises(RevisionConflict):
        store.rename_workspace("ws", "stale", expected_revision=1)
    assert store.get_workspace("ws").name == "Original"
    prior = store.get_workspace("ws")
    updated = store.rename_workspace("ws", "New", expected_revision=2)
    assert updated.revision == 3 and updated.updated_at >= prior.updated_at
    assert WireWorkspace.model_validate(asdict(updated)).name == "New"
    with pytest.raises(ValueError, match="Expected revision"):
        link(store, "ws", "ocr", "p", True)
    with pytest.raises(ValueError, match="workspace id"):
        store.create_workspace("Invalid", workspace_id="9invalid")
    with pytest.raises(ValueError, match="app id"):
        link(store, "ws", "../unsafe", "p", 3)


def test_link_role_and_its_own_revision_are_cas_protected(tmp_path):
    store = WorkspaceStore(tmp_path / "roles.sqlite3")
    store.create_workspace("Case", workspace_id="ws")
    original = link(store, "ws", "ocr", "project", 1)
    changed = store.update_link_role("ws", original.link_id, "editor",
                                     expected_revision=2, expected_link_revision=1)
    assert changed.role == "editor" and changed.revision == 2
    assert store.get_workspace("ws").revision == 3
    with pytest.raises(RevisionConflict):
        store.update_link_role("ws", original.link_id, "viewer",
                               expected_revision=3, expected_link_revision=1)
    assert store.list_links("ws")[0].role == "editor"
    with pytest.raises(ValueError, match="role"):
        store.update_link_role("ws", original.link_id, "admin",
                               expected_revision=3, expected_link_revision=2)


def test_failed_second_statement_rolls_back_link_and_workspace_revision(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = WorkspaceStore(path)
    store.create_workspace("Case", workspace_id="ws")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_revision BEFORE UPDATE OF revision ON workspace "
                   "BEGIN SELECT RAISE(ABORT, 'injected revision failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        link(store, "ws", "classification", "native", 1)
    reopened = WorkspaceStore(path)
    assert reopened.get_workspace("ws").revision == 1 and reopened.list_links("ws") == []


def test_legacy_wal_backup_preserves_rows_and_restores_contract_shape(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    writer = legacy_connection(path)
    original_native_id = "9" + "a" * 31
    try:
        writer.execute("INSERT INTO workspace(id, name) VALUES ('ws', 'legacy')")
        writer.execute("INSERT INTO module_project_link VALUES (?, ?, ?, ?, ?)",
                       ("link", "ws", "ocr", original_native_id, "P"))
        assert (tmp_path / "legacy.sqlite3-wal").stat().st_size > 0
        migrated = WorkspaceStore(path)
        assert migrated.migration_backup and migrated.migration_backup.is_file()
        assert WireWorkspace.model_validate(asdict(migrated.get_workspace("ws"))).revision == 1
        assert WireLink.model_validate(asdict(migrated.list_links("ws")[0])).module_project_id == original_native_id
        backup = migrated.migration_backup
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            assert db.execute("SELECT id FROM module_project_link").fetchone()[0] == "link"
            assert db.execute("SELECT display_name FROM module_project_link").fetchone()[0] == "P"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with sqlite3.connect(path) as db:
            assert "display_name" not in [row[1] for row in db.execute("PRAGMA table_info(module_project_link)")]
        exercise_post_migration_writes(migrated)
        restored_path = tmp_path / "restored.sqlite3"
        shutil.copy2(backup, restored_path)
        restored = WorkspaceStore(restored_path)
        assert restored.get_workspace("ws").name == "legacy"
        assert restored.list_links("ws")[0].module_project_id == original_native_id
        exercise_post_migration_writes(restored)
    finally:
        writer.close()


def test_backup_and_migration_hold_writer_boundary(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    writer = legacy_connection(path)
    writer.execute("INSERT INTO workspace VALUES ('before', 'committed before migration')")
    attempted, committed = threading.Event(), threading.Event()
    original_backup = WorkspaceStore._backup
    workers = []

    def competing_writer():
        with sqlite3.connect(path, timeout=5, isolation_level=None) as db:
            attempted.set()
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO workspace(workspace_id,name,revision,created_at,updated_at) "
                       "VALUES ('after','committed after migration',1,'2026-09-25T00:00:00Z','2026-09-25T00:00:00Z')")
            db.execute("COMMIT")
            committed.set()

    def guarded_backup(self, source):
        thread = threading.Thread(target=competing_writer)
        workers.append(thread)
        thread.start()
        assert attempted.wait(2) and not committed.wait(0.1)
        return original_backup(self, source)

    monkeypatch.setattr(WorkspaceStore, "_backup", guarded_backup)
    try:
        migrated = WorkspaceStore(path)
        workers[0].join(timeout=5)
        assert committed.is_set()
        assert {item.workspace_id for item in migrated.list_workspaces()} == {"before", "after"}
        with sqlite3.connect(migrated.migration_backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            assert db.execute("SELECT id FROM workspace").fetchall() == [("before",)]
    finally:
        writer.close()


def test_migration_failure_rolls_back_ddl_and_retains_usable_backup(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    with legacy_connection(path) as db:
        db.execute("INSERT INTO workspace(id, name) VALUES ('ws', 'unchanged')")
    real_upgrade = schema._upgrade_v1_to_v2

    def interrupt(db):
        db.execute("ALTER TABLE workspace RENAME COLUMN id TO workspace_id")
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
    assert WorkspaceStore(path).get_workspace("ws").revision == 1


def test_new_database_migration_failure_does_not_leave_partial_tables(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"

    def interrupt(db):
        db.execute("CREATE TABLE workspace (workspace_id TEXT PRIMARY KEY)")
        raise RuntimeError("new database interrupted")

    monkeypatch.setattr(schema, "_create_v2", interrupt)
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


def test_legacy_invalid_id_cannot_be_migrated_into_wire_model(tmp_path):
    path = tmp_path / "invalid.sqlite3"
    with legacy_connection(path) as db:
        db.execute("INSERT INTO workspace VALUES ('9invalid', 'Case')")
    with pytest.raises(ValueError, match="legacy workspace id"):
        WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT id FROM workspace").fetchone()[0] == "9invalid"


def test_link_removal_is_scoped_and_revision_checked(tmp_path):
    store = WorkspaceStore(tmp_path / "remove.sqlite3")
    store.create_workspace("A", workspace_id="a")
    store.create_workspace("B", workspace_id="b")
    item = link(store, "a", "classification", "native", 1)
    with pytest.raises(KeyError, match="link not found"):
        store.remove_link("b", item.link_id, expected_revision=1)
    assert store.get_workspace("b").revision == 1
    with pytest.raises(RevisionConflict):
        store.remove_link("a", item.link_id, expected_revision=1)
    store.remove_link("a", item.link_id, expected_revision=2)
    assert store.get_workspace("a").revision == 3 and store.list_links("a") == []
