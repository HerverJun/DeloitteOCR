"""Small transactional ledger; each command has a fixed replayable receipt."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import threading

from workbench_platform.batches.store import encoded, todo_identity
from workbench_platform.data_api.service import DataError


class AuditStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'audit.sqlite3'
        self.lock = threading.RLock()
        with closing(self.db()) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in {0, 1} or version == 0 and tables:
                raise DataError('审计领域数据库版本未知；原文件已保留，不能自动改写', 503)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(project_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS project_workspace ON projects(workspace_id,project_id);
                CREATE TABLE IF NOT EXISTS audit_requests(request_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,payload_hash TEXT NOT NULL,state TEXT NOT NULL,
                    receipt TEXT,snapshot TEXT,created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS audit_publication_reservation
                    ON audit_requests(project_id) WHERE state<>'committed';
                CREATE TABLE IF NOT EXISTS audit_history(event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,project_revision INTEGER NOT NULL,body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS history_project ON audit_history(project_id,project_revision,event_id);
                CREATE TABLE IF NOT EXISTS evidence_snapshots(evidence_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_todo_identity(
                    todo_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,finding_id TEXT NOT NULL,
                    UNIQUE(project_id,finding_id));
                CREATE TABLE IF NOT EXISTS audit_todo_revisions(
                    workspace_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO audit_todo_revisions SELECT DISTINCT workspace_id,1 FROM projects;
                CREATE TRIGGER IF NOT EXISTS audit_todo_generation_insert AFTER INSERT ON projects BEGIN
                    INSERT INTO audit_todo_revisions VALUES(NEW.workspace_id,1)
                    ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS audit_todo_generation_update AFTER UPDATE ON projects BEGIN
                    UPDATE audit_todo_revisions SET revision=revision+1 WHERE workspace_id=NEW.workspace_id;
                END;
                CREATE TRIGGER IF NOT EXISTS audit_todo_generation_delete AFTER DELETE ON projects BEGIN
                    UPDATE audit_todo_revisions SET revision=revision+1 WHERE workspace_id=OLD.workspace_id;
                    DELETE FROM audit_todo_identity WHERE project_id=OLD.project_id;
                END;
                CREATE VIEW IF NOT EXISTS audit_procedure_batches AS
                    SELECT p.workspace_id,p.project_id,p.revision AS project_revision,u.key AS procedure_id,
                        json_extract(r.value,'$.batch_id') AS batch_id,
                        json_extract(r.value,'$.batch_revision') AS batch_revision
                    FROM projects p,json_each(p.body,'$.procedures') u,json_each(u.value,'$.batches') r;
                CREATE VIEW IF NOT EXISTS audit_finding_todos AS
                    SELECT t.todo_id,p.workspace_id,p.project_id,p.revision AS project_revision,
                        f.key AS finding_id,json_extract(f.value,'$.procedure_id') AS procedure_id,
                        json_extract(f.value,'$.finding.title') AS title,
                        json_extract(f.value,'$.finding.priority') AS priority,
                        json_extract(f.value,'$.finding.status') AS state,
                        json_extract(f.value,'$.updated_at') AS updated_at,
                        json_extract(f.value,'$.evidence_refs') AS evidence_refs,
                        json_extract(p.body,'$.project.title') AS project_title,
                        json_extract(p.body,'$.procedures.'||json_extract(f.value,'$.procedure_id')||'.procedure.title') AS procedure_title
                    FROM projects p,json_each(p.body,'$.findings') f JOIN audit_todo_identity t
                        ON t.project_id=p.project_id AND t.finding_id=f.key;
                PRAGMA user_version=1;
            ''')
            db.create_function('todo_identity', 3, todo_identity, deterministic=True)
            db.execute("INSERT OR IGNORE INTO audit_todo_identity SELECT todo_identity('finding',p.project_id,f.key),"
                       "p.project_id,f.key FROM projects p,json_each(p.body,'$.findings') f")

    def db(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        return db

    @contextmanager
    def transaction(self):
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def project(db, project_id):
        row = db.execute('SELECT body FROM projects WHERE project_id=?', (project_id,)).fetchone()
        if row is None:
            raise DataError('审计项目不存在', 404)
        return json.loads(row['body'])

    @staticmethod
    def save(db, value):
        db.execute('INSERT INTO projects VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE '
                   'SET revision=excluded.revision,body=excluded.body',
                   (value['project_id'], value['workspace_id'], value['revision'], encoded(value)))
        for finding_id in value['findings']:
            db.execute('INSERT OR IGNORE INTO audit_todo_identity VALUES(?,?,?)',
                       (todo_identity('finding', value['project_id'], finding_id), value['project_id'], finding_id))

    @staticmethod
    def request(db, request_id, payload_hash=None, project_id=None):
        row = db.execute('SELECT * FROM audit_requests WHERE request_id=?', (request_id,)).fetchone()
        if row and (payload_hash is not None and row['payload_hash'] != payload_hash
                    or project_id is not None and row['project_id'] != project_id):
            raise DataError('同一请求身份不能替换审计内容', 409)
        return row

    @staticmethod
    def conditional(db, project, expected_revision):
        pending = db.execute("SELECT request_id FROM audit_requests WHERE project_id=? AND state<>'committed'",
                             (project['project_id'],)).fetchone()
        if pending:
            raise DataError('底稿正在发布；请查询或重试原请求后继续编辑', 409)
        if project['revision'] != expected_revision:
            raise DataError('审计项目版本冲突，请刷新所见内容后重新保存', 409)
