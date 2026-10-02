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

MIGRATION_4 = """
CREATE TABLE result_dispositions (
 item_id TEXT PRIMARY KEY REFERENCES job_items(id), state TEXT NOT NULL
 CHECK(state IN ('confirmed','deferred','reopened')), label_id TEXT,
 note TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE result_disposition_events (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 item_id TEXT NOT NULL REFERENCES job_items(id), state TEXT NOT NULL,
 label_id TEXT, note TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(item_id,revision)
);
PRAGMA user_version=4;
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
                version = 3
            if version == 3:
                for statement in MIGRATION_4.split(";"):
                    if statement.strip():
                        con.execute(statement)
            elif version != 4:
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

    def dispose_results(self, job_id, item_ids, state, label_id=None, note="", expected_revisions=None):
        """Persist decisions for this run; asset annotations never imply review."""
        job = self.job(job_id)
        if job["kind"] != "inference" or state not in {"confirmed", "deferred", "reopened"}:
            raise ValueError("仅分类结果可记录此处置")
        if not item_ids or len(item_ids) > 500 or len(set(item_ids)) != len(item_ids):
            raise ValueError("请明确选择 1 至 500 项结果")
        labels = {row["label_id"] for row in self.project(job["project_id"])["labels"]}
        if state == "confirmed" and label_id not in labels:
            raise ValueError("确认类别不属于此项目")
        if state != "confirmed" and label_id is not None:
            raise ValueError("暂缓或重开不能设置确认类别")
        if expected_revisions is not None and set(expected_revisions) != set(item_ids):
            raise ValueError("处置版本必须覆盖全部所选结果")
        with self.connect(write=True) as con:
            for item_id in item_ids:
                row = con.execute("SELECT j.*,d.revision disposition_revision FROM job_items j "
                                  "LEFT JOIN result_dispositions d ON d.item_id=j.id "
                                  "WHERE j.id=? AND j.job_id=?", (item_id, job_id)).fetchone()
                if row is None:
                    raise LookupError("结果不存在或不属于此任务")
                revision = row["disposition_revision"] or 0
                if expected_revisions is not None and expected_revisions[item_id] != revision:
                    raise ValueError("处置记录已变化，请刷新后重试")
                if state == "confirmed" and (row["state"] != "succeeded" or not row["prediction"]):
                    raise ValueError("执行失败或没有预测的结果不能确认为已复核")
                stamp = now()
                con.execute("INSERT INTO result_dispositions VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(item_id) DO UPDATE SET state=excluded.state,label_id=excluded.label_id,"
                            "note=excluded.note,revision=excluded.revision,updated_at=excluded.updated_at",
                            (item_id, state, label_id, note, revision + 1, stamp))
                con.execute("INSERT INTO result_disposition_events VALUES(?,?,?,?,?,?,?,?)",
                            (uid("decision"), job_id, item_id, state, label_id, note, revision + 1, stamp))
                if state == "confirmed" and row["asset_id"]:
                    asset = con.execute("SELECT label_id FROM assets WHERE id=? AND project_id=?",
                                        (row["asset_id"], job["project_id"])).fetchone()
                    if asset is None:
                        raise LookupError("结果原图不属于此项目")
                    con.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?)",
                                (uid("annotation"), row["asset_id"], asset["label_id"], label_id,
                                 "result_confirmed:" + job_id, stamp))
                    con.execute("UPDATE assets SET label_id=? WHERE id=?", (label_id, row["asset_id"]))
        return {"updated": len(item_ids), "state": state}

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
