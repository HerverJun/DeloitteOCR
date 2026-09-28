from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
import uuid

from .settings import Settings


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


MIGRATION_1 = """
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 labels TEXT NOT NULL, active_model_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE assets (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL,
 name TEXT NOT NULL, sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 label_id TEXT, group_id TEXT NOT NULL, source_id TEXT NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(project_id,path,sha256)
);
CREATE INDEX assets_project ON assets(project_id,archived,label_id);
CREATE TABLE annotations (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), old_label TEXT,
 label_id TEXT, origin TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE datasets (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 labels TEXT NOT NULL, manifest_path TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 stats TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE models (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), job_id TEXT NOT NULL,
 name TEXT NOT NULL, package_path TEXT NOT NULL, metadata TEXT NOT NULL, metrics TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
 state TEXT NOT NULL, params TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
 total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
 error TEXT, result TEXT, owner TEXT, lease_token TEXT, lease_until REAL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX jobs_queue ON jobs(state,created_at);
CREATE TABLE job_items (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), asset_id TEXT,
 path TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 prediction TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(job_id,asset_id)
);
CREATE INDEX job_items_pending ON job_items(job_id,state);
CREATE TABLE idempotency (
 scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, job_id TEXT NOT NULL,
 PRIMARY KEY(scope,key)
);
CREATE TABLE leases (resource TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
PRAGMA user_version=1;
"""

MIGRATION_2 = """
DROP INDEX job_items_pending;
CREATE INDEX job_items_pending ON job_items(job_id,state,id);
PRAGMA user_version=2;
"""

MIGRATION_3 = """
CREATE TABLE platform_import_requests (
 project_id TEXT NOT NULL REFERENCES projects(id), request_id TEXT NOT NULL,
 payload_hash TEXT NOT NULL, manifest TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(project_id,request_id)
);
CREATE TABLE platform_import_items (
 project_id TEXT NOT NULL, request_id TEXT NOT NULL, item_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, platform_asset_id TEXT NOT NULL,
 blob_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, name TEXT NOT NULL,
 label_id TEXT, status TEXT NOT NULL CHECK(status IN ('pending','imported')),
 target_asset_id TEXT REFERENCES assets(id), accepted_at TEXT,
 PRIMARY KEY(project_id,request_id,item_id),
 UNIQUE(project_id,request_id,ordinal),
 FOREIGN KEY(project_id,request_id) REFERENCES platform_import_requests(project_id,request_id)
);
CREATE INDEX platform_import_target ON platform_import_items(target_asset_id);
PRAGMA user_version=3;
"""


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.prepare()
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
        with self.connect(write=True) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in MIGRATION_1.split(";"):
                    if statement.strip():
                        con.execute(statement)
                version = 1
            if version == 1:
                for statement in MIGRATION_2.split(";"):
                    if statement.strip():
                        con.execute(statement)
                version = 2
            if version == 2:
                for statement in MIGRATION_3.split(";"):
                    if statement.strip():
                        con.execute(statement)
            elif version != 3:
                raise RuntimeError(f"Unsupported database version {version}")

    @contextmanager
    def connect(self, write=False):
        con = sqlite3.connect(self.settings.database, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def rows(self, sql, params=()):
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def project(self, project_id):
        result = self.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not result:
            raise LookupError("项目不存在")
        result["labels"] = json.loads(result["labels"])
        return result

    def job(self, job_id):
        result = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not result:
            raise LookupError("任务不存在")
        for key in ("params", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def submit(self, project_id, kind, params, items=(), key=None):
        self.project(project_id)
        scope = f"{project_id}:{kind}"
        request_hash = fingerprint(params)
        with self.connect(write=True) as con:
            if key:
                prior = con.execute("SELECT * FROM idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise ValueError("幂等键已用于不同的请求")
                    return prior["job_id"]
            job_id = uid("job")
            con.execute("INSERT INTO jobs(id,project_id,kind,state,params,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?)",
                        (job_id, project_id, kind, dump(params), now(), now()))
            count = 0
            for item in items:
                con.execute("INSERT INTO job_items(id,job_id,asset_id,path,sha256) VALUES(?,?,?,?,?)",
                            (uid("item"), job_id, item["id"], item["path"], item["sha256"]))
                count += 1
            con.execute("UPDATE jobs SET total=? WHERE id=?", (count, job_id))
            if key:
                con.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (scope, key, request_hash, job_id))
        return job_id

    def recover(self):
        with self.connect(write=True) as con:
            expired = con.execute("SELECT * FROM jobs WHERE lease_until<? AND state IN ('running','pausing','cancelling')", (time.time(),)).fetchall()
            for row in expired:
                if row["state"] == "cancelling":
                    state, error = "cancelled", "工作进程中断后已取消"
                    con.execute("UPDATE job_items SET state='cancelled' WHERE job_id=? AND state='pending'", (row["id"],))
                elif row["kind"] == "inference":
                    state = "paused" if row["state"] == "pausing" else "queued"
                    error = None
                else:
                    state, error = "failed", "工作进程中断；此类型任务需要重新创建，已有资产仍保留"
                con.execute("UPDATE jobs SET state=?,error=?,owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                            (state, error, now(), row["id"]))

    def claim(self, owner):
        self.recover()
        with self.connect(write=True) as con:
            lease = con.execute("SELECT * FROM leases WHERE resource='worker' AND expires>?", (time.time(),)).fetchone()
            if lease:
                return None
            row = con.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            until = time.time() + self.settings.lease_seconds
            con.execute("INSERT INTO leases VALUES('worker',?,?) ON CONFLICT(resource) DO UPDATE SET token=excluded.token,expires=excluded.expires", (token, until))
            con.execute("UPDATE jobs SET state='running',owner=?,lease_token=?,lease_until=?,updated_at=? WHERE id=?", (owner, token, until, now(), row["id"]))
        return self.job(row["id"])

    def heartbeat(self, job_id, token):
        until = time.time() + self.settings.lease_seconds
        with self.connect(write=True) as con:
            changed = con.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND lease_token=? AND lease_until>?", (until, now(), job_id, token, time.time())).rowcount
            if changed:
                con.execute("UPDATE leases SET expires=? WHERE resource='worker' AND token=?", (until, token))
        return bool(changed)

    def assert_owner(self, con, job_id, token):
        row = con.execute("SELECT * FROM jobs WHERE id=? AND lease_token=? AND lease_until>?", (job_id, token, time.time())).fetchone()
        if not row:
            raise RuntimeError("任务租约已失效，拒绝写入")
        return row

    def progress(self, job_id, token, stage, done=None, total=None):
        with self.connect(write=True) as con:
            self.assert_owner(con, job_id, token)
            con.execute("UPDATE jobs SET stage=?,done=COALESCE(?,done),total=COALESCE(?,total),updated_at=? WHERE id=?", (stage, done, total, now(), job_id))

    def finish(self, job_id, token, state, result=None, error=None):
        with self.connect(write=True) as con:
            row = self.assert_owner(con, job_id, token)
            if row["state"] == "cancelling":
                state = "cancelled"
            if state == "cancelled":
                con.execute("UPDATE job_items SET state='cancelled' WHERE job_id=? AND state='pending'", (job_id,))
            con.execute("UPDATE jobs SET state=?,result=?,error=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (state, dump(result) if result is not None else None, error, now(), job_id))
            con.execute("DELETE FROM leases WHERE resource='worker' AND token=?", (token,))

    def action(self, job_id, action):
        with self.connect(write=True) as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise LookupError("任务不存在")
            state = row["state"]
            if action == "pause" and row["kind"] != "inference":
                raise ValueError("只有批量分类任务支持暂停")
            mappings = {
                "pause": {"running": "pausing", "queued": "paused", "pausing": "pausing", "paused": "paused"},
                "resume": {"paused": "queued", "queued": "queued"},
                "cancel": {"queued": "cancelled", "paused": "cancelled", "running": "cancelling", "pausing": "cancelling", "cancelling": "cancelling", "cancelled": "cancelled"},
            }
            target = mappings.get(action, {}).get(state)
            if not target:
                raise ValueError(f"当前状态 {state} 无法执行 {action}")
            con.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?", (target, now(), job_id))
            if target == "cancelled":
                con.execute("UPDATE job_items SET state='cancelled' WHERE job_id=? AND state='pending'", (job_id,))
        return self.job(job_id)
