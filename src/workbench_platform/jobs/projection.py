"""Persist native job observations without taking ownership of native work.

The caller supplies a trusted, live ResolvedContext and observations from the
bounded native adapters. This ledger records a display projection and an event
cursor; it never changes a native job, guesses a missing result, or resumes a
queue. A caller may poll ``events_since`` after a browser reconnect.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from workbench_platform.bridge.context import ResolvedContext

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})
_FAILURES = frozenset({"unreachable", "not_ready", "not_found", "forbidden"})
_ITEM_STATES = frozenset({"pending", "succeeded", "failed", "cancelled"})


@runtime_checkable
class ReviewItem(Protocol):
    native_item_id: str
    state: str


@runtime_checkable
class ClassificationStatus(Protocol):
    native_job_id: str
    kind: str
    state: str
    stage: str
    total: int
    done: int
    failed: int
    actions: tuple[str, ...]


@runtime_checkable
class OcrBatchStatus(Protocol):
    tasks: tuple
    succeeded: int
    failed: int
    cancelled: int
    pending: int


class ProjectionConflict(ValueError):
    """A stale or mismatched observation cannot replace durable state."""


class ProjectionUnavailable(ValueError):
    """A requested projection or cursor cannot be served."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


@dataclass(frozen=True)
class Counts:
    total: int | None
    succeeded: int
    failed: int
    cancelled: int
    pending: int

    def __post_init__(self) -> None:
        values = (self.succeeded, self.failed, self.cancelled, self.pending)
        if (any(type(value) is not int or value < 0 for value in values)
                or (self.total is not None and (type(self.total) is not int
                    or self.total < 0 or sum(values) != self.total))):
            raise ProjectionConflict("Invalid native counts")


def _classification(value: ClassificationStatus, items: Iterable[ReviewItem] | None) -> dict:
    if (not isinstance(value, ClassificationStatus) or not _valid_id(value.native_job_id)
            or type(value.total) is not int or value.total < 0
            or type(value.done) is not int or value.done < 0
            or type(value.failed) is not int or value.failed < 0
            or not isinstance(value.stage, str) or not isinstance(value.actions, tuple)
            or any(action not in {"cancel", "pause", "resume", "retry_failed"}
                   for action in value.actions)):
        raise ProjectionConflict("Invalid classification observation")
    if value.kind == "inference":
        if items is None:
            raise ProjectionConflict("Inference requires a complete native item snapshot")
        rows = tuple(items)
        if (len(rows) != value.total or len({row.native_item_id for row in rows}) != len(rows)
                or any(not isinstance(row, ReviewItem) or not _valid_id(row.native_item_id)
                       or row.state not in _ITEM_STATES for row in rows)):
            raise ProjectionConflict("Incomplete classification item snapshot")
        counts = Counts(len(rows), *(sum(row.state == state for row in rows)
                                     for state in ("succeeded", "failed", "cancelled", "pending")))
        # Native counters are an independent integrity check. They do not tell
        # us how many items were cancelled or left unprocessed on failure.
        if counts.succeeded != value.done or counts.failed != value.failed:
            raise ProjectionConflict("Native job and item counters disagree")
    else:
        if items is not None or value.kind not in {"train", "import"}:
            raise ProjectionConflict("Unexpected classification items")
        # Training's done/total are changing stage units (images, epochs,
        # export steps), never the immutable item coverage of a JobRef.
        counts = Counts(None, 0, 0, 0, 0)
    status = {"completed_with_errors": "partial", "pausing": "pausing",
              "cancelling": "cancelling"}.get(value.state, value.state)
    if status == "succeeded" and (counts.pending or counts.failed or counts.cancelled):
        raise ProjectionConflict("Native success contradicts item ledger")
    if status == "partial" and not (counts.succeeded and (counts.failed or counts.cancelled)):
        raise ProjectionConflict("Native partial status contradicts item ledger")
    if status not in {"queued", "running", "pausing", "paused", "cancelling",
                      "cancelled", "succeeded", "partial", "failed"}:
        raise ProjectionConflict("Unknown classification state")
    return {"status": status, "native_status": value.state, "stage": value.stage[:256],
            "counts": asdict(counts), "progress_done": value.done,
            "progress_total": value.total, "capabilities": list(value.actions)}


def _ocr(value: OcrBatchStatus, native_ids: tuple[str, ...]) -> dict:
    if (not isinstance(value, OcrBatchStatus) or not isinstance(value.tasks, tuple)
            or len(value.tasks) != len(native_ids)
            or any(not _valid_id(getattr(row, "native_task_id", None))
                   or getattr(row, "native_status", None) not in {
                       "queued", "running", "paused", "interrupted", "succeeded", "failed", "cancelled"}
                   for row in value.tasks)
            or {row.native_task_id for row in value.tasks} != set(native_ids)):
        raise ProjectionConflict("OCR task set changed")
    counts = Counts(len(native_ids), value.succeeded, value.failed, value.cancelled, value.pending)
    statuses = {row.native_status for row in value.tasks}
    if (counts.succeeded != sum(row.native_status == "succeeded" for row in value.tasks)
            or counts.failed != sum(row.native_status == "failed" for row in value.tasks)
            or counts.cancelled != sum(row.native_status == "cancelled" for row in value.tasks)):
        raise ProjectionConflict("OCR task counts disagree")
    if counts.pending:
        status = ("running" if "running" in statuses else "paused" if statuses <= {
            "paused", "interrupted", "succeeded", "failed", "cancelled"} else "queued")
    else:
        status = ("succeeded" if counts.succeeded == counts.total else "partial"
                  if counts.succeeded else "failed" if counts.failed else "cancelled")
    return {"status": status, "native_status": ",".join(sorted(statuses)),
            "stage": "OCR batch", "counts": asdict(counts),
            "progress_done": counts.succeeded + counts.failed + counts.cancelled,
            "progress_total": counts.total,
            "capabilities": ["cancel"] if counts.pending else []}


class JobProjectionStore:
    """SQLite event log scoped by workspace, app instance and generation."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not self.path.parent.is_dir():
            raise ProjectionUnavailable("Projection directory is unavailable")
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projected_jobs (
                    platform_job_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    link_id TEXT NOT NULL, app_id TEXT NOT NULL,
                    module_project_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation>=1),
                    operation_id TEXT NOT NULL, input_manifest_hash TEXT NOT NULL,
                    native_ids TEXT NOT NULL, event_seq INTEGER NOT NULL CHECK(event_seq>=0),
                    payload TEXT NOT NULL,
                    UNIQUE(workspace_id, operation_id));
                CREATE TABLE IF NOT EXISTS projection_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_job_id TEXT NOT NULL REFERENCES projected_jobs(platform_job_id),
                    workspace_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(platform_job_id, event_seq));
                CREATE INDEX IF NOT EXISTS projection_events_workspace
                    ON projection_events(workspace_id, cursor);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _context(context: ResolvedContext) -> None:
        if (not isinstance(context, ResolvedContext)
                or context.app_id not in {"ocr", "classification"}
                or any(not _valid_id(value) for value in (
                    context.workspace_id, context.link_id, context.app_id,
                    context.module_project_id, context.instance_id))):
            raise ProjectionConflict("Invalid trusted context")

    @staticmethod
    def _view(row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload"])
        return {"platform_job_id": row["platform_job_id"],
                "workspace_id": row["workspace_id"], "link_id": row["link_id"],
                "app_id": row["app_id"], "module_project_id": row["module_project_id"],
                "instance_id": row["instance_id"], "generation": row["generation"],
                "operation_id": row["operation_id"],
                "input_manifest_hash": row["input_manifest_hash"],
                "native_job_ids": json.loads(row["native_ids"]),
                "last_event_seq": row["event_seq"], **payload}

    def register(self, context: ResolvedContext, *, platform_job_id: str,
                 operation_id: str, input_manifest_hash: str,
                 native_job_ids: Iterable[str], generation: int) -> dict:
        self._context(context)
        if context.role not in {"owner", "editor"}:
            raise ProjectionConflict("Job registration requires an editing role")
        ids = tuple(native_job_ids)
        if (not _valid_id(platform_job_id) or not _valid_id(operation_id)
                or not isinstance(input_manifest_hash, str)
                or _HASH.fullmatch(input_manifest_hash) is None
                or type(generation) is not int or generation < 1
                or not ids or len(ids) > 16_000 or len(set(ids)) != len(ids)
                or any(not _valid_id(value) for value in ids)
                or (context.app_id == "classification" and len(ids) != 1)):
            raise ProjectionConflict("Invalid job registration")
        initial = {"status": "unknown", "native_status": None, "stage": "",
                   "counts": asdict(Counts(len(ids) if context.app_id == "ocr" else None,
                                            0, 0, 0, len(ids) if context.app_id == "ocr" else 0)),
                   "progress_done": None, "progress_total": None,
                   "capabilities": [], "availability": "not_ready", "updated_at": _utc()}
        immutable = (context.workspace_id, context.link_id, context.app_id,
                     context.module_project_id, context.instance_id, generation,
                     operation_id, input_manifest_hash, json.dumps(ids))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM projected_jobs WHERE platform_job_id=?",
                                      (platform_job_id,)).fetchone()
                if existing is not None:
                    actual = tuple(existing[name] for name in (
                        "workspace_id", "link_id", "app_id", "module_project_id",
                        "instance_id", "generation", "operation_id",
                        "input_manifest_hash", "native_ids"))
                    if actual != immutable:
                        raise ProjectionConflict("Job registration identity changed")
                    db.execute("COMMIT")
                    return self._view(existing)
                try:
                    db.execute("INSERT INTO projected_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (platform_job_id, *immutable[:8], immutable[8], 0,
                                json.dumps(initial, separators=(",", ":"))))
                except sqlite3.IntegrityError:
                    raise ProjectionConflict("Job operation identity is already registered") from None
                db.execute("INSERT INTO projection_events(platform_job_id,workspace_id,event_seq,payload) "
                           "VALUES(?,?,?,?)", (platform_job_id, context.workspace_id, 0,
                                                json.dumps(initial, separators=(",", ":"))))
                row = db.execute("SELECT * FROM projected_jobs WHERE platform_job_id=?",
                                 (platform_job_id,)).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self._view(row)

    def get(self, workspace_id: str, platform_job_id: str) -> dict:
        if not _valid_id(workspace_id) or not _valid_id(platform_job_id):
            raise ProjectionUnavailable("Invalid projection lookup")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM projected_jobs WHERE workspace_id=? AND platform_job_id=?",
                             (workspace_id, platform_job_id)).fetchone()
        if row is None:
            raise ProjectionUnavailable("Projection does not exist")
        return self._view(row)

    def observe(self, context: ResolvedContext, platform_job_id: str, *,
                generation: int, expected_event_seq: int,
                native: ClassificationStatus | OcrBatchStatus | None = None,
                items: Iterable[ReviewItem] | None = None,
                failure: str | None = None) -> dict:
        self._context(context)
        if ((native is None) == (failure is None) or
                (failure is not None and failure not in _FAILURES) or
                type(expected_event_seq) is not int or expected_event_seq < 0 or
                type(generation) is not int or generation < 1):
            raise ProjectionConflict("Invalid observation")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM projected_jobs WHERE platform_job_id=?",
                                 (platform_job_id,)).fetchone()
                if row is None or any(row[key] != value for key, value in (
                    ("workspace_id", context.workspace_id), ("link_id", context.link_id),
                    ("app_id", context.app_id), ("module_project_id", context.module_project_id),
                    ("instance_id", context.instance_id), ("generation", generation))):
                    raise ProjectionConflict("Observation belongs to another native generation")
                if row["event_seq"] != expected_event_seq:
                    raise ProjectionConflict("Observation cursor is stale")
                previous = json.loads(row["payload"])
                if failure is not None:
                    projected = {**previous, "availability": failure,
                                 "capabilities": []}
                    if previous["status"] not in _TERMINAL:
                        projected["status"] = "unknown"
                else:
                    ids = tuple(json.loads(row["native_ids"]))
                    if context.app_id == "classification":
                        if not isinstance(native, ClassificationStatus) or native.native_job_id != ids[0]:
                            raise ProjectionConflict("Classification native ID changed")
                        projected = _classification(native, items)
                    else:
                        if not isinstance(native, OcrBatchStatus) or items is not None:
                            raise ProjectionConflict("Invalid OCR observation")
                        projected = _ocr(native, ids)
                    if previous["status"] in _TERMINAL and projected["status"] != previous["status"]:
                        raise ProjectionConflict("Native terminal state cannot be overwritten")
                    projected["capabilities"] = (projected["capabilities"]
                                                  if context.role in {"owner", "editor"} else [])
                    projected["availability"] = "ready"
                projected["updated_at"] = _utc()
                encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
                next_seq = expected_event_seq + 1
                db.execute("UPDATE projected_jobs SET event_seq=?,payload=? WHERE platform_job_id=?",
                           (next_seq, encoded, platform_job_id))
                db.execute("INSERT INTO projection_events(platform_job_id,workspace_id,event_seq,payload) "
                           "VALUES(?,?,?,?)", (platform_job_id, context.workspace_id, next_seq, encoded))
                row = db.execute("SELECT * FROM projected_jobs WHERE platform_job_id=?",
                                 (platform_job_id,)).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self._view(row)

    def events_since(self, workspace_id: str, cursor: int = 0, *, limit: int = 200) -> tuple[dict, ...]:
        if (not _valid_id(workspace_id) or type(cursor) is not int or cursor < 0
                or type(limit) is not int or not 1 <= limit <= 200):
            raise ProjectionUnavailable("Invalid event query")
        with closing(self._connect()) as db:
            rows = db.execute("SELECT e.cursor,e.event_seq,e.payload,j.platform_job_id,j.app_id,"
                              "j.instance_id,j.generation FROM projection_events e "
                              "JOIN projected_jobs j ON j.platform_job_id=e.platform_job_id "
                              "WHERE e.workspace_id=? AND e.cursor>? ORDER BY e.cursor LIMIT ?",
                              (workspace_id, cursor, limit)).fetchall()
        return tuple({"cursor": row["cursor"], "platform_job_id": row["platform_job_id"],
                      "app_id": row["app_id"], "instance_id": row["instance_id"],
                      "generation": row["generation"], "event_seq": row["event_seq"],
                      **json.loads(row["payload"])} for row in rows)
