"""Persistent single-owner GPU reservation; expiry alone never frees ownership.

The trusted launcher supplies an ownership verifier at grant time and a
resource witness at release/recovery. This module never kills a PID or infers
GPU quiescence from elapsed time. It is not a GPU availability qualification.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Callable

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class ResourceBusy(ValueError):
    pass


class StaleOwner(ValueError):
    pass


class ReleaseUnconfirmed(ValueError):
    pass


@dataclass(frozen=True)
class Owner:
    requester: str
    launch_id: str
    instance_id: str
    pid: int
    creation_time: str

    def __post_init__(self):
        if (not all(isinstance(value, str) and IDENTIFIER.fullmatch(value)
                    for value in (self.requester, self.launch_id, self.instance_id))
                or type(self.pid) is not int or self.pid <= 0
                or not isinstance(self.creation_time, str) or not 10 <= len(self.creation_time) <= 80
                or "\x00" in self.creation_time):
            raise ValueError("Invalid owner identity")


@dataclass(frozen=True)
class Lease:
    request_id: str
    generation: int
    token: str
    owner: Owner
    expires_at: float


@dataclass(frozen=True)
class ResourceEvidence:
    exact_identity_checked: bool
    owner_active: bool | None
    children_active: bool | None
    gpu_released: bool | None

    @property
    def release_confirmed(self) -> bool:
        return (self.exact_identity_checked is True and self.children_active is False
                and self.gpu_released is True)

    @property
    def reclaim_confirmed(self) -> bool:
        return self.release_confirmed and self.owner_active is False


@dataclass(frozen=True)
class Snapshot:
    generation: int
    owner_request_id: str | None
    owner: Owner | None
    expires_at: float | None
    pending: tuple[str, ...]


class GpuCoordinator:
    def __init__(self, path: str | Path, *, ttl_seconds: float = 30,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path).resolve()
        if (not self.path.parent.is_dir() or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 1 <= ttl_seconds <= 300
                or not callable(clock)):
            raise ValueError("Invalid coordinator configuration")
        self.ttl_seconds, self.clock = ttl_seconds, clock
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS gpu_state (id INTEGER PRIMARY KEY CHECK(id=1), "
                       "generation INTEGER NOT NULL, request_id TEXT, owner_json TEXT, "
                       "token_sha256 TEXT, expires_at REAL)")
            db.execute("INSERT OR IGNORE INTO gpu_state VALUES(1,1,NULL,NULL,NULL,NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS gpu_requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "request_id TEXT UNIQUE NOT NULL, requester TEXT NOT NULL, "
                       "status TEXT NOT NULL CHECK(status IN ('pending','granted','completed','cancelled')))")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _owner(row) -> Owner | None:
        return Owner(**json.loads(row["owner_json"])) if row["owner_json"] else None

    @staticmethod
    def _validate_request(request_id: str, requester: str):
        if not all(isinstance(value, str) and IDENTIFIER.fullmatch(value)
                   for value in (request_id, requester)):
            raise ValueError("Invalid resource request")

    def request(self, request_id: str, requester: str) -> int:
        """Durably enqueue once; return zero for the current owner, otherwise FIFO position."""
        self._validate_request(request_id, requester)
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM gpu_requests WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["requester"] != requester or existing["status"] in {"completed", "cancelled"}:
                    raise ValueError("Resource request cannot be reused")
            else:
                count = db.execute("SELECT COUNT(*) FROM gpu_requests WHERE status='pending'").fetchone()[0]
                if count >= 128:
                    raise ResourceBusy("Resource queue is full")
                db.execute("INSERT INTO gpu_requests(request_id,requester,status) VALUES(?,?,'pending')",
                           (request_id, requester))
            state = db.execute("SELECT * FROM gpu_state WHERE id=1").fetchone()
            if state["request_id"] == request_id:
                return 0
            pending = [row[0] for row in db.execute(
                "SELECT request_id FROM gpu_requests WHERE status='pending' ORDER BY seq")]
            return pending.index(request_id) + (1 if state["request_id"] else 0) + 1

    def grant(self, request_id: str, owner: Owner, *, owned: Callable[[Owner], bool],
              resume_token: str | None = None) -> Lease:
        if (not isinstance(owner, Owner) or not callable(owned)
                or not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id)):
            raise ValueError("Invalid grant")
        with self._transaction() as db:
            item = db.execute("SELECT * FROM gpu_requests WHERE request_id=?", (request_id,)).fetchone()
            if not item or item["requester"] != owner.requester or item["status"] not in {"pending", "granted"}:
                raise StaleOwner("Request is not pending for this owner")
            state = db.execute("SELECT * FROM gpu_state WHERE id=1").fetchone()
            if state["request_id"]:
                if (state["request_id"] == request_id and self._owner(state) == owner
                        and isinstance(resume_token, str) and secrets.compare_digest(
                            hashlib.sha256(resume_token.encode()).hexdigest(), state["token_sha256"])):
                    return Lease(request_id, state["generation"], resume_token, owner, state["expires_at"])
                # Even if expired or the parent PID vanished, only a witnessed
                # release/reclaim may clear this persistent ownership.
                raise ResourceBusy("GPU ownership remains reserved")
            head = db.execute("SELECT request_id FROM gpu_requests WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()
            if not head or head[0] != request_id:
                raise ResourceBusy("Earlier GPU request is pending")
            try:
                verified = owned(owner)
            except Exception:
                raise StaleOwner("Owner proof unavailable") from None
            if verified is not True:
                raise StaleOwner("Owner is not in a trusted process tree")
            token = secrets.token_urlsafe(32)
            expiry = self.clock() + self.ttl_seconds
            db.execute("UPDATE gpu_state SET request_id=?,owner_json=?,token_sha256=?,expires_at=? WHERE id=1",
                       (request_id, json.dumps(owner.__dict__, sort_keys=True),
                        hashlib.sha256(token.encode()).hexdigest(), expiry))
            db.execute("UPDATE gpu_requests SET status='granted' WHERE request_id=?", (request_id,))
            return Lease(request_id, state["generation"], token, owner, expiry)

    @staticmethod
    def _match(state, lease: Lease):
        if (not isinstance(lease, Lease) or not isinstance(lease.request_id, str)
                or type(lease.generation) is not int or not isinstance(lease.owner, Owner)
                or state["request_id"] != lease.request_id
                or state["generation"] != lease.generation or GpuCoordinator._owner(state) != lease.owner
                or not isinstance(lease.token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", lease.token)
                or not secrets.compare_digest(
                    state["token_sha256"] or "", hashlib.sha256(lease.token.encode()).hexdigest())):
            raise StaleOwner("GPU lease identity changed")

    def heartbeat(self, lease: Lease) -> Lease:
        with self._transaction() as db:
            state = db.execute("SELECT * FROM gpu_state WHERE id=1").fetchone()
            self._match(state, lease)
            expiry = self.clock() + self.ttl_seconds
            db.execute("UPDATE gpu_state SET expires_at=? WHERE id=1", (expiry,))
            return Lease(lease.request_id, lease.generation, lease.token, lease.owner, expiry)

    @staticmethod
    def _evidence(owner: Owner, witness: Callable[[Owner], ResourceEvidence]) -> ResourceEvidence:
        try:
            result = witness(owner)
        except Exception:
            raise ReleaseUnconfirmed("GPU release witness unavailable") from None
        if not isinstance(result, ResourceEvidence):
            raise ReleaseUnconfirmed("GPU release witness is invalid")
        return result

    @staticmethod
    def _clear(db, request_id: str):
        db.execute("UPDATE gpu_requests SET status='completed' WHERE request_id=?", (request_id,))
        db.execute("UPDATE gpu_state SET generation=generation+1,request_id=NULL,owner_json=NULL,"
                   "token_sha256=NULL,expires_at=NULL WHERE id=1")

    def release(self, lease: Lease, *, witness: Callable[[Owner], ResourceEvidence]) -> int:
        """Only exact current owner plus confirmed child/GPU release can advance generation."""
        with self._transaction() as db:
            state = db.execute("SELECT * FROM gpu_state WHERE id=1").fetchone()
            self._match(state, lease)
            if not self._evidence(lease.owner, witness).release_confirmed:
                raise ReleaseUnconfirmed("GPU or owned children remain active")
            self._clear(db, lease.request_id)
            return state["generation"] + 1

    def reclaim(self, generation: int, *, witness: Callable[[Owner], ResourceEvidence]) -> int:
        """Recovery after restart: expiry + exact dead-owner and quiescence proof."""
        with self._transaction() as db:
            state = db.execute("SELECT * FROM gpu_state WHERE id=1").fetchone()
            owner = self._owner(state)
            if owner is None or type(generation) is not int or state["generation"] != generation:
                raise StaleOwner("GPU generation changed")
            if state["expires_at"] > self.clock():
                raise ResourceBusy("Lease has not expired")
            if not self._evidence(owner, witness).reclaim_confirmed:
                raise ReleaseUnconfirmed("Old owner or child/GPU resource remains active")
            self._clear(db, state["request_id"])
            return state["generation"] + 1

    def cancel_pending(self, request_id: str, requester: str):
        self._validate_request(request_id, requester)
        with self._transaction() as db:
            item = db.execute("SELECT * FROM gpu_requests WHERE request_id=?", (request_id,)).fetchone()
            if not item or item["requester"] != requester or item["status"] != "pending":
                raise StaleOwner("Request is not pending")
            db.execute("UPDATE gpu_requests SET status='cancelled' WHERE request_id=?", (request_id,))

    def snapshot(self) -> Snapshot:
        with self._transaction() as db:
            state = db.execute("SELECT * FROM gpu_state WHERE id=1").fetchone()
            pending = tuple(row[0] for row in db.execute(
                "SELECT request_id FROM gpu_requests WHERE status='pending' ORDER BY seq"))
            return Snapshot(state["generation"], state["request_id"], self._owner(state),
                            state["expires_at"], pending)
