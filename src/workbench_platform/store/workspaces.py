"""Contract-shaped workspace/link ledger with SQLite transactional revisions.

Schema 1 is a defined compatibility fixture, not evidence of historical
platform production data. It has workspace(id, name) and links with id,
module_id, native_project_id and display_name. Schema 2 uses the frozen v1.0
contract names. No native application project contents are copied here.
"""

from __future__ import annotations

import sqlite3
import uuid
import re
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2


class UnsupportedSchema(ValueError):
    """Unknown unversioned or future schema; never silently rewrite it."""


class RevisionConflict(ValueError):
    """A workspace changed since the caller read its revision."""


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    name: str
    revision: int
    created_at: str
    updated_at: str
    schema_version: str = "1.0"


@dataclass(frozen=True)
class ModuleProjectLink:
    workspace_id: str
    app_id: str
    module_project_id: str
    link_id: str
    role: str
    revision: int
    schema_version: str = "1.0"


ROLES = frozenset({"owner", "editor", "viewer"})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _role(value: str) -> str:
    if not isinstance(value, str) or value not in ROLES:
        raise ValueError("Invalid link role")
    return value


ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise ValueError(f"Invalid {label}")
    return value


def _id(value: str, label: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Invalid {label}")
    return value


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _create_v1(connection: sqlite3.Connection) -> None:
    """Defined compatibility fixture used to exercise the v1→v2 migration."""
    connection.execute("CREATE TABLE workspace (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    connection.execute("CREATE TABLE module_project_link ("
                       "id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE, "
                       "module_id TEXT NOT NULL, native_project_id TEXT NOT NULL, display_name TEXT NOT NULL)")
    connection.execute("PRAGMA user_version=1")


def _create_v2(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE workspace (workspace_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                       "revision INTEGER NOT NULL CHECK (revision >= 1), "
                       "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE module_project_link ("
                       "link_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL "
                       "REFERENCES workspace(workspace_id) ON DELETE CASCADE, "
                       "app_id TEXT NOT NULL, module_project_id TEXT NOT NULL, "
                       "role TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')), "
                       "revision INTEGER NOT NULL CHECK (revision >= 1))")
    connection.execute("CREATE UNIQUE INDEX link_native_identity ON module_project_link "
                       "(workspace_id, app_id, module_project_id)")
    connection.execute("CREATE INDEX link_by_workspace ON module_project_link (workspace_id, link_id)")
    connection.execute("PRAGMA user_version=2")


def _upgrade_v1_to_v2(connection: sqlite3.Connection) -> None:
    expected = {"workspace": {"id", "name"}, "module_project_link":
                {"id", "workspace_id", "module_id", "native_project_id", "display_name"}}
    for table, columns in expected.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if actual != columns:
            raise UnsupportedSchema(f"Schema 1 {table} columns do not match compatibility shape")
    # A defined older shape is migratable only if its persisted identities can
    # actually satisfy the current wire contract; never silently remap IDs.
    for workspace_id, name in connection.execute("SELECT id, name FROM workspace"):
        _id(workspace_id, "legacy workspace id")
        _text(name, "legacy workspace name")
    for row in connection.execute("SELECT id, workspace_id, module_id, native_project_id "
                                  "FROM module_project_link"):
        for value, label in zip(row, ("legacy link id", "legacy workspace id", "legacy app id",
                                      "legacy module project id")):
            _id(value, label)
    stamp = _utc()  # First known platform registration time, not claimed legacy creation time.
    connection.execute("ALTER TABLE workspace RENAME COLUMN id TO workspace_id")
    connection.execute("ALTER TABLE workspace ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 "
                       "CHECK (revision >= 1)")
    connection.execute("ALTER TABLE workspace ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    connection.execute("ALTER TABLE workspace ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    connection.execute("UPDATE workspace SET created_at=?, updated_at=?", (stamp, stamp))
    connection.execute("ALTER TABLE module_project_link RENAME COLUMN id TO link_id")
    connection.execute("ALTER TABLE module_project_link RENAME COLUMN module_id TO app_id")
    connection.execute("ALTER TABLE module_project_link RENAME COLUMN native_project_id TO module_project_id")
    connection.execute("ALTER TABLE module_project_link ADD COLUMN role TEXT NOT NULL DEFAULT 'owner' "
                       "CHECK (role IN ('owner','editor','viewer'))")
    connection.execute("ALTER TABLE module_project_link ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 "
                       "CHECK (revision >= 1)")
    # Ambiguous legacy duplicate identities are not merged or guessed. The
    # unique index fails and the entire migration rolls back for review.
    connection.execute("CREATE UNIQUE INDEX link_native_identity ON module_project_link "
                       "(workspace_id, app_id, module_project_id)")
    connection.execute("CREATE INDEX link_by_workspace ON module_project_link (workspace_id, link_id)")
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
            if version < SCHEMA_VERSION:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Recheck under the write lock: another store may have
                    # migrated between our initial inspection and this lock.
                    current = connection.execute("PRAGMA user_version").fetchone()[0]
                    if current != version:
                        raise RevisionConflict("Schema version changed during migration")
                    if version == 0:
                        _create_v2(connection)
                    else:
                        # Reserve the sole writer first. A separate reader can
                        # back up a WAL snapshot while our write reservation
                        # prevents a commit between backup and migration.
                        with closing(_connect(self.path)) as snapshot_reader:
                            self.migration_backup = self._backup(snapshot_reader)
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
        key = _id("w" + uuid.uuid4().hex if workspace_id is None else workspace_id, "workspace id")
        stamp = _utc()
        with self._connection() as connection:
            connection.execute("INSERT INTO workspace(workspace_id, name, revision, created_at, updated_at) "
                               "VALUES (?, ?, 1, ?, ?)", (key, name, stamp, stamp))
        return Workspace(key, name, 1, stamp, stamp)

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connection() as connection:
            row = connection.execute("SELECT workspace_id, name, revision, created_at, updated_at "
                                     "FROM workspace WHERE workspace_id=?",
                                     (_id(workspace_id, "workspace id"),)).fetchone()
            return Workspace(*row) if row else None

    def list_workspaces(self) -> list[Workspace]:
        with self._connection() as connection:
            return [Workspace(*row) for row in connection.execute(
                "SELECT workspace_id, name, revision, created_at, updated_at "
                "FROM workspace ORDER BY workspace_id")]

    def list_links(self, workspace_id: str) -> list[ModuleProjectLink]:
        with self._connection() as connection:
            return [ModuleProjectLink(*row) for row in connection.execute(
                "SELECT workspace_id, app_id, module_project_id, link_id, role, revision "
                "FROM module_project_link WHERE workspace_id=? ORDER BY link_id",
                (_id(workspace_id, "workspace id"),))]

    def _mutate(self, workspace_id: str, expected_revision: int, operation):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Expected revision must be a positive integer")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT revision FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone()
                if row is None:
                    raise KeyError("Workspace not found")
                if row[0] != expected_revision:
                    raise RevisionConflict(f"Expected revision {expected_revision}, found {row[0]}")
                result = operation(connection)
                connection.execute("UPDATE workspace SET revision=revision+1, updated_at=? "
                                   "WHERE workspace_id=?", (_utc(), workspace_id))
                connection.execute("COMMIT")
                return result
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def rename_workspace(self, workspace_id: str, name: str, *, expected_revision: int) -> Workspace:
        key, name = _id(workspace_id, "workspace id"), _text(name, "workspace name")
        self._mutate(key, expected_revision,
                     lambda db: db.execute("UPDATE workspace SET name=? WHERE workspace_id=?", (name, key)))
        return self.get_workspace(key)

    def add_link(self, workspace_id: str, app_id: str, module_project_id: str,
                 *, role: str, expected_revision: int, link_id: str | None = None) -> ModuleProjectLink:
        link = ModuleProjectLink(_id(workspace_id, "workspace id"), _id(app_id, "app id"),
                                 _id(module_project_id, "module project id"),
                                 _id("l" + uuid.uuid4().hex if link_id is None else link_id, "link id"),
                                 _role(role), 1)
        self._mutate(link.workspace_id, expected_revision, lambda db: db.execute(
            "INSERT INTO module_project_link(workspace_id, app_id, module_project_id, link_id, role, revision) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (link.workspace_id, link.app_id, link.module_project_id, link.link_id, link.role)))
        return link

    def update_link_role(self, workspace_id: str, link_id: str, role: str, *,
                         expected_revision: int, expected_link_revision: int) -> ModuleProjectLink:
        key, link_key, role = (_id(workspace_id, "workspace id"),
                               _id(link_id, "link id"), _role(role))
        if type(expected_link_revision) is not int or expected_link_revision < 1:
            raise ValueError("Expected link revision must be a positive integer")

        def update(db):
            row = db.execute("SELECT app_id, module_project_id, revision FROM module_project_link "
                             "WHERE workspace_id=? AND link_id=?", (key, link_key)).fetchone()
            if row is None:
                raise KeyError("Project link not found")
            if row[2] != expected_link_revision:
                raise RevisionConflict(f"Expected link revision {expected_link_revision}, found {row[2]}")
            db.execute("UPDATE module_project_link SET role=?, revision=revision+1 "
                       "WHERE workspace_id=? AND link_id=?", (role, key, link_key))
            return ModuleProjectLink(key, row[0], row[1], link_key, role, row[2] + 1)

        return self._mutate(key, expected_revision, update)

    def remove_link(self, workspace_id: str, link_id: str, *, expected_revision: int) -> None:
        key, link_key = _id(workspace_id, "workspace id"), _id(link_id, "link id")

        def delete(db):
            cursor = db.execute("DELETE FROM module_project_link WHERE workspace_id=? AND link_id=?", (key, link_key))
            if cursor.rowcount != 1:
                raise KeyError("Project link not found")

        self._mutate(key, expected_revision, delete)
