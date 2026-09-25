"""Persistent, server-authored recent successful application launches."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from packages.contracts.models import LaunchReceipt, RecentWork, RecentWorkPage


class RecentSnapshotExpired(ValueError):
    pass


class LaunchIdConflict(ValueError):
    pass


class RecentWorkStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if not self.path.parent.is_dir():
            raise ValueError("Configured recent-work directory does not exist")
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recent_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL CHECK(revision>=1));
                INSERT OR IGNORE INTO recent_meta(singleton, revision) VALUES (1, 1);
                CREATE TABLE IF NOT EXISTS launch_attempt (
                    app_id TEXT NOT NULL, launch_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved', 'succeeded')),
                    PRIMARY KEY(app_id, launch_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS launch_attempt_id ON launch_attempt(launch_id);
                CREATE TABLE IF NOT EXISTS recent_work (
                    recent_id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('platform_launch', 'native_project_open')),
                    app_id TEXT NOT NULL, workspace_id TEXT, link_id TEXT, module_project_id TEXT,
                    launch_id TEXT NOT NULL, instance_id TEXT NOT NULL, opened_at TEXT NOT NULL,
                    CHECK((source='platform_launch' AND workspace_id IS NULL AND link_id IS NULL AND module_project_id IS NULL)
                       OR (source='native_project_open' AND workspace_id IS NOT NULL AND link_id IS NOT NULL AND module_project_id IS NOT NULL))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS recent_direct_identity ON recent_work(source, app_id, launch_id)
                    WHERE source='platform_launch';
                CREATE UNIQUE INDEX IF NOT EXISTS recent_project_identity ON recent_work(source, workspace_id, app_id, link_id, module_project_id, launch_id)
                    WHERE source='native_project_open';
                CREATE INDEX IF NOT EXISTS recent_order ON recent_work(opened_at DESC, recent_id DESC);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def reserve_launch(self, app_id: str, launch_id: str) -> bool:
        """A reserved ID survives restarts, including a crash during startup."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                inserted = db.execute("INSERT OR IGNORE INTO launch_attempt(app_id, launch_id, state) "
                                      "VALUES (?, ?, 'reserved')", (app_id, launch_id)).rowcount == 1
                if not inserted:
                    owner = db.execute("SELECT app_id FROM launch_attempt WHERE launch_id=?", (launch_id,)).fetchone()
                    if owner is not None and owner[0] != app_id:
                        raise LaunchIdConflict("Launch ID belongs to another application")
                db.execute("COMMIT")
                return inserted
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def record_launch(self, receipt: LaunchReceipt) -> RecentWork:
        """Atomically mark the verified launch and its one recent event."""
        item = RecentWork(schema_version="1.0", recent_id="r" + uuid.uuid4().hex,
                          source="platform_launch", app_id=receipt.app_id,
                          launch_id=receipt.launch_id, instance_id=receipt.instance_id,
                          opened_at=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT state FROM launch_attempt WHERE app_id=? AND launch_id=?",
                                      (receipt.app_id, receipt.launch_id)).fetchone()
                if existing is None:
                    raise ValueError("Launch ID was not reserved")
                cursor = db.execute("INSERT OR IGNORE INTO recent_work "
                                    "(recent_id, source, app_id, launch_id, instance_id, opened_at) "
                                    "VALUES (?, ?, ?, ?, ?, ?)",
                                    (item.recent_id, item.source, item.app_id, item.launch_id,
                                     item.instance_id, item.opened_at))
                if cursor.rowcount:
                    db.execute("UPDATE recent_meta SET revision=revision+1 WHERE singleton=1")
                db.execute("UPDATE launch_attempt SET state='succeeded' WHERE app_id=? AND launch_id=?",
                           (receipt.app_id, receipt.launch_id))
                row = db.execute("SELECT recent_id, source, app_id, workspace_id, link_id, "
                                 "module_project_id, launch_id, instance_id, opened_at FROM recent_work "
                                 "WHERE source=? AND app_id=? AND launch_id=?",
                                 (item.source, item.app_id, item.launch_id)).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return RecentWork(schema_version="1.0", **dict(zip(
            ("recent_id", "source", "app_id", "workspace_id", "link_id", "module_project_id",
             "launch_id", "instance_id", "opened_at"), row)))

    def list_page(self, *, limit: int, cursor: int = 0, snapshot_revision: int | None = None,
                  workspace_id: str | None = None) -> RecentWorkPage:
        if type(limit) is not int or not 1 <= limit <= 200 or type(cursor) is not int or cursor < 0:
            raise ValueError("Invalid recent page request")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            try:
                revision = db.execute("SELECT revision FROM recent_meta WHERE singleton=1").fetchone()[0]
                if snapshot_revision is not None and snapshot_revision != revision:
                    raise RecentSnapshotExpired("Recent work changed")
                rows = db.execute("SELECT recent_id, source, app_id, workspace_id, link_id, "
                                  "module_project_id, launch_id, instance_id, opened_at FROM recent_work "
                                  "WHERE workspace_id IS ? ORDER BY opened_at DESC, recent_id DESC LIMIT ? OFFSET ?",
                                  (workspace_id, limit + 1, cursor)).fetchall()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        fields = ("recent_id", "source", "app_id", "workspace_id", "link_id", "module_project_id",
                  "launch_id", "instance_id", "opened_at")
        items = [RecentWork(schema_version="1.0", **dict(zip(fields, row))) for row in rows[:limit]]
        return RecentWorkPage(schema_version="1.0", items=items,
                              next_cursor=str(cursor + limit) if len(rows) > limit else None,
                              snapshot_revision=revision)
