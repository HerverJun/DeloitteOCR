"""SQLite workspace/link ledger with transactional revision checks and v1 migration.

Schema 1 is the legacy baseline: workspace(id, name), module_project_link
(id, workspace_id, module_id, native_project_id, display_name). Schema 2 adds
workspace.revision and stable identity constraints. No native project contents
are copied into this database. The caller supplies an isolated database path.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2


class UnsupportedSchema(ValueError):
    """Unknown unversioned or future schema; never silently rewrite it."""


class RevisionConflict(ValueError):
    """A workspace changed since the caller read its revision."""


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    revision: int


@dataclass(frozen=True)
class ModuleProjectLink:
    id: str
    workspace_id: str
    module_id: str
    native_project_id: str
    display_name: str


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or "\x00" in value:
        raise ValueError(f"Invalid {label}")
    return value


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _create_v1(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE workspace (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    connection.execute("CREATE TABLE module_project_link ("
                       "id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE, "
                       "module_id TEXT NOT NULL, native_project_id TEXT NOT NULL, display_name TEXT NOT NULL)")
    connection.execute("PRAGMA user_version=1")


def _upgrade_v1_to_v2(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE workspace ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 "
                       "CHECK (revision >= 0)")
    # Ambiguous legacy duplicate identities are not merged or guessed. The
    # unique index fails and the entire migration rolls back for review.
    connection.execute("CREATE UNIQUE INDEX link_native_identity ON module_project_link "
                       "(workspace_id, module_id, native_project_id)")
    connection.execute("CREATE INDEX link_by_workspace ON module_project_link (workspace_id, id)")
    connection.execute("PRAGMA user_version=2")


class WorkspaceStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).resolve()
        if not self.path.parent.is_dir():
            raise FileNotFoundError(self.path.parent)
        self.migration_backup: Path | None = None
        with closing(_connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION or version < 0:
                raise UnsupportedSchema(f"Unsupported future SQLite schema: {version}")
            if version == 0:
                names = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                if names:
                    raise UnsupportedSchema("Existing unversioned SQLite schema requires explicit import")
            if version == 1:
                self.migration_backup = self._backup(connection)
            if version < SCHEMA_VERSION:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Recheck under the write lock: another store may have
                    # migrated between our initial inspection and this lock.
                    current = connection.execute("PRAGMA user_version").fetchone()[0]
                    if current != version:
                        raise RevisionConflict("Schema version changed during migration")
                    if version == 0:
                        _create_v1(connection)
                    _upgrade_v1_to_v2(connection)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            connection.execute("PRAGMA journal_mode=WAL")

    def _backup(self, source: sqlite3.Connection) -> Path:
        backup = self.path.with_name(f"{self.path.name}.before-v1-to-v2-{uuid.uuid4().hex}.sqlite3")
        # Exclusive reservation prevents a backup ever overwriting evidence.
        with backup.open("xb"):
            pass
        try:
            with closing(_connect(backup)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("Migration backup integrity check failed")
        except BaseException:
            # Keep the reserved file for diagnosis. It is never represented
            # as a usable migration_backup after an exception.
            raise
        return backup

    @contextmanager
    def _connection(self):
        connection = _connect(self.path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise UnsupportedSchema(f"Expected SQLite schema {SCHEMA_VERSION}, found {version}")
            yield connection
        finally:
            connection.close()

    def create_workspace(self, name: str, *, workspace_id: str | None = None) -> Workspace:
        name = _text(name, "workspace name")
        key = _text(workspace_id or uuid.uuid4().hex, "workspace id")
        with self._connection() as connection:
            connection.execute("INSERT INTO workspace(id, name, revision) VALUES (?, ?, 0)", (key, name))
        return Workspace(key, name, 0)

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connection() as connection:
            row = connection.execute("SELECT id, name, revision FROM workspace WHERE id=?",
                                     (_text(workspace_id, "workspace id"),)).fetchone()
            return Workspace(*row) if row else None

    def list_workspaces(self) -> list[Workspace]:
        with self._connection() as connection:
            return [Workspace(*row) for row in connection.execute(
                "SELECT id, name, revision FROM workspace ORDER BY id")]

    def list_links(self, workspace_id: str) -> list[ModuleProjectLink]:
        with self._connection() as connection:
            return [ModuleProjectLink(*row) for row in connection.execute(
                "SELECT id, workspace_id, module_id, native_project_id, display_name "
                "FROM module_project_link WHERE workspace_id=? ORDER BY id",
                (_text(workspace_id, "workspace id"),))]

    def _mutate(self, workspace_id: str, expected_revision: int, operation):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a nonnegative integer")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT revision FROM workspace WHERE id=?", (workspace_id,)).fetchone()
                if row is None:
                    raise KeyError("Workspace not found")
                if row[0] != expected_revision:
                    raise RevisionConflict(f"Expected revision {expected_revision}, found {row[0]}")
                result = operation(connection)
                connection.execute("UPDATE workspace SET revision=revision+1 WHERE id=?", (workspace_id,))
                connection.execute("COMMIT")
                return result
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def rename_workspace(self, workspace_id: str, name: str, *, expected_revision: int) -> Workspace:
        key, name = _text(workspace_id, "workspace id"), _text(name, "workspace name")
        self._mutate(key, expected_revision,
                     lambda db: db.execute("UPDATE workspace SET name=? WHERE id=?", (name, key)))
        return Workspace(key, name, expected_revision + 1)

    def add_link(self, workspace_id: str, module_id: str, native_project_id: str,
                 display_name: str, *, expected_revision: int, link_id: str | None = None) -> ModuleProjectLink:
        link = ModuleProjectLink(_text(link_id or uuid.uuid4().hex, "link id"),
                                 _text(workspace_id, "workspace id"), _text(module_id, "module id"),
                                 _text(native_project_id, "native project id"),
                                 _text(display_name, "display name"))
        self._mutate(link.workspace_id, expected_revision, lambda db: db.execute(
            "INSERT INTO module_project_link(id, workspace_id, module_id, native_project_id, display_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (link.id, link.workspace_id, link.module_id, link.native_project_id, link.display_name)))
        return link

    def remove_link(self, workspace_id: str, link_id: str, *, expected_revision: int) -> None:
        key, link_key = _text(workspace_id, "workspace id"), _text(link_id, "link id")

        def delete(db):
            cursor = db.execute("DELETE FROM module_project_link WHERE workspace_id=? AND id=?", (key, link_key))
            if cursor.rowcount != 1:
                raise KeyError("Project link not found")

        self._mutate(key, expected_revision, delete)
