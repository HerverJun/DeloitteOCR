from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
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


MIGRATION_5 = """
ALTER TABLE result_disposition_events ADD COLUMN operation_id TEXT;
ALTER TABLE result_disposition_events ADD COLUMN request_id TEXT;
ALTER TABLE result_disposition_events ADD COLUMN actor_json TEXT;
CREATE TABLE result_disposition_requests (
 project_id TEXT NOT NULL REFERENCES projects(id), request_id TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL REFERENCES jobs(id),
 instance_id TEXT NOT NULL, payload_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(project_id,request_id)
);
PRAGMA user_version=5;
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
                version = 4
            if version == 4:
                for statement in MIGRATION_5.split(";"):
                    if statement.strip():
                        con.execute(statement)
            elif version != 5:
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

    def create_project(self, name, description, labels, request_id=None):
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', request_id)):
            raise ValueError('创建请求无效')
        project_id = uid('project')
        payload = dump({'name': name, 'description': description, 'labels': labels})
        with self.connect(write=True) as con:
            con.execute('CREATE TABLE IF NOT EXISTS project_create_requests(request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, project_id TEXT NOT NULL)')
            if request_id:
                prior = con.execute('SELECT payload,project_id FROM project_create_requests WHERE request_id=?', (request_id,)).fetchone()
                if prior:
                    if prior['payload'] != payload:
                        raise ValueError('创建请求已用于其他内容')
                    row = con.execute('SELECT * FROM projects WHERE id=?', (prior['project_id'],)).fetchone()
                    if row is None:
                        raise ValueError('原请求创建的项目已删除')
                    result = dict(row)
                    result['labels'] = json.loads(result['labels'])
                    return result
            con.execute('INSERT INTO projects(id,name,description,labels,created_at) VALUES(?,?,?,?,?)',
                        (project_id, name, description, dump(labels), now()))
            if request_id:
                con.execute('INSERT INTO project_create_requests VALUES(?,?,?)', (request_id, payload, project_id))
        return self.project(project_id)

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

    def disposition_operation(self, project_id, operation_id):
        self.project(project_id)
        row = self.one("SELECT receipt_json FROM result_disposition_requests WHERE project_id=? AND operation_id=?",
                       (project_id, operation_id))
        return json.loads(row["receipt_json"]) if row else None

    def dispose_results(self, job_id, item_ids, state, label_id=None, note="", expected_revisions=None,
                        *, operation_id=None, request_id=None, actor=None, instance_id=None, intent="manual"):
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
        identity = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
        if any(not isinstance(value, str) or not identity.fullmatch(value)
               for value in (operation_id, request_id, instance_id)):
            raise ValueError("处置必须携带稳定操作、请求及实例身份")
        if (not isinstance(actor, dict) or set(actor) != {"id", "display_name", "source"}
                or actor.get("source") != "local" or not identity.fullmatch(str(actor.get("id", "")))
                or not isinstance(actor.get("display_name"), str) or not 1 <= len(actor["display_name"]) <= 100):
            raise ValueError("处置必须明确本机操作身份；不代表组织认证")
        if (not isinstance(note, str) or len(note) > 2000
                or intent not in {"manual", "accept_prediction", "adopt"}):
            raise ValueError("处置说明或操作意图无效")
        if intent == "adopt" and len(item_ids) != 1:
            raise ValueError("采用修订必须明确指定一项结果")
        if (not isinstance(expected_revisions, dict) or set(expected_revisions) != set(item_ids)
                or any(type(value) is not int or value < 0 for value in expected_revisions.values())):
            raise ValueError("处置版本必须覆盖全部所选结果")
        payload = {"job_id": job_id, "item_ids": sorted(item_ids), "state": state,
                   "label_id": label_id, "note": note, "expected_revisions": expected_revisions,
                   "actor": actor, "instance_id": instance_id, "intent": intent}
        payload_hash = fingerprint(payload)
        with self.connect(write=True) as con:
            prior = con.execute("SELECT * FROM result_disposition_requests WHERE "
                                "(project_id=? AND request_id=?) OR operation_id=?",
                                (job["project_id"], request_id, operation_id)).fetchone()
            if prior:
                if (prior["payload_hash"] != payload_hash or prior["request_id"] != request_id
                        or prior["operation_id"] != operation_id or prior["project_id"] != job["project_id"]):
                    raise ValueError("操作或请求身份已用于不同内容")
                return json.loads(prior["receipt_json"])
            changes = []
            stamp = now()
            for item_id in item_ids:
                row = con.execute("SELECT j.*,d.revision disposition_revision,d.state disposition_state,"
                                  "d.label_id human_label_id FROM job_items j "
                                  "LEFT JOIN result_dispositions d ON d.item_id=j.id "
                                  "WHERE j.id=? AND j.job_id=?", (item_id, job_id)).fetchone()
                if row is None:
                    raise LookupError("结果不存在或不属于此任务")
                revision = row["disposition_revision"] or 0
                if expected_revisions[item_id] != revision:
                    raise ValueError("处置记录已变化，请刷新后重试")
                if state == "confirmed" and (row["state"] != "succeeded" or not row["prediction"]):
                    raise ValueError("执行失败或没有预测的结果不能确认为已复核")
                prediction = json.loads(row["prediction"]) if row["prediction"] else None
                asset = None
                if row["asset_id"]:
                    asset = con.execute("SELECT * FROM assets WHERE id=? AND project_id=?",
                                        (row["asset_id"], job["project_id"])).fetchone()
                    if asset is None or asset["sha256"] != row["sha256"] or asset["path"] != row["path"]:
                        raise ValueError("结果原图来源身份已变化")
                    from pathlib import Path
                    try:
                        with Path(row["path"]).open("rb") as source:
                            if hashlib.file_digest(source, "sha256").hexdigest() != row["sha256"]:
                                raise ValueError("结果原图内容已变化")
                    except OSError as error:
                        raise ValueError("结果原图不可读取，处置未写入") from error
                if intent == "accept_prediction" and state == "confirmed":
                    if (not prediction or label_id != prediction.get("label_id")
                            or (row["human_label_id"] is not None and row["human_label_id"] != label_id)
                            or (asset and asset["label_id"] is not None and asset["label_id"] != label_id)):
                        raise ValueError("原建议不能覆盖已有人工判断；请明确采用人工类别")
                if intent == "adopt" and (state != "confirmed" or row["disposition_state"] != "confirmed"
                                          or row["human_label_id"] != label_id):
                    raise ValueError("采用必须对应所见版本的原生人工类别")
                con.execute("INSERT INTO result_dispositions VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(item_id) DO UPDATE SET state=excluded.state,label_id=excluded.label_id,"
                            "note=excluded.note,revision=excluded.revision,updated_at=excluded.updated_at",
                            (item_id, state, label_id, note, revision + 1, stamp))
                con.execute("INSERT INTO result_disposition_events "
                            "(id,job_id,item_id,state,label_id,note,revision,created_at,operation_id,request_id,actor_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (uid("decision"), job_id, item_id, state, label_id, note, revision + 1, stamp,
                             operation_id, request_id, dump(actor)))
                if state == "confirmed" and row["asset_id"]:
                    con.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?)",
                                (uid("annotation"), row["asset_id"], asset["label_id"], label_id,
                                 "result_confirmed:" + job_id, stamp))
                    con.execute("UPDATE assets SET label_id=? WHERE id=?", (label_id, row["asset_id"]))
                changes.append({"item_id": item_id, "asset_id": row["asset_id"], "path": row["path"],
                                "original_sha256": row["sha256"], "prediction": prediction,
                                "human_label_id": label_id, "revision": revision + 1,
                                "previous": {"revision": revision, "human_label_id": row["human_label_id"],
                                             "state": row["disposition_state"] or "unreviewed"}})
            receipt = {"operation_id": operation_id, "request_id": request_id, "payload_hash": payload_hash,
                       "state": "committed", "project_id": job["project_id"], "instance_id": instance_id,
                       "job_id": job_id, "updated": len(item_ids), "disposition_state": state,
                       "human_label_id": label_id, "revisions": {item["item_id"]: item["revision"] for item in changes},
                       "items": changes, "actor": actor, "note": note, "intent": intent, "created": stamp}
            con.execute("INSERT INTO result_disposition_requests VALUES(?,?,?,?,?,?,?,?)",
                        (job["project_id"], request_id, operation_id, job_id, instance_id,
                         payload_hash, dump(receipt), stamp))
        return receipt

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
