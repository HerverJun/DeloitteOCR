"""Transactional project, queue and edit history storage outside the application."""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import hashlib
import re
from pathlib import Path
import sqlite3
import threading
import uuid
import zlib
from ocr_workbench.fusion_store import FusionStoreMixin
from ocr_workbench.document_store import DocumentStoreMixin, migrate_v9


SCHEMA_VERSION = 18
HISTORY_MAGIC = b"OCRZ1\0"


def history_encoded(value):
    return HISTORY_MAGIC + zlib.compress(encoded(value).encode("utf-8"), level=6)


def history_decoded(value):
    if isinstance(value, bytes) and value.startswith(HISTORY_MAGIC):
        return zlib.decompress(value[len(HISTORY_MAGIC) :]).decode("utf-8")
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Conflict(ValueError):
    pass


class Store(DocumentStoreMixin, FusionStoreMixin):
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.file_lock = threading.RLock()
        # A future schema must be rejected before WAL or any schema write occurs.
        path = self.root / "workbench.sqlite3"
        exists = path.is_file()
        if exists:
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            ) as probe:
                version = probe.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"项目数据库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}；"
                    "请使用创建此项目的新版工作台，原数据库未修改"
                )
        else:
            version = 0
        if version < SCHEMA_VERSION:
            with closing(sqlite3.connect(path, timeout=15)) as db:
                db.row_factory = sqlite3.Row
                db.create_function("ocr_history", 1, lambda value: history_encoded(json.loads(value)))
                if exists:
                    backups = self.root / "database-backups"
                    backups.mkdir(exist_ok=True)
                    self.migration_backup = (
                        backups
                        / f"before-v{version}-to-v{SCHEMA_VERSION}-{uid()}.sqlite3"
                    )
                    with closing(sqlite3.connect(self.migration_backup)) as backup:
                        db.backup(backup)
                try:
                    db.execute("PRAGMA foreign_keys=ON")
                    db.execute("BEGIN IMMEDIATE")
                    for target in range(version + 1, SCHEMA_VERSION + 1):
                        self._migrate(db, target)
                        db.execute(f"PRAGMA user_version={target}")
                    if db.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("数据库迁移外键检查失败，升级已回滚")
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("数据库迁移完整性检查失败，升级已回滚")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        self.schema_version = SCHEMA_VERSION

    @staticmethod
    def _migrate(db, version):
        if version == 1:
            schema = """
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created TEXT,updated TEXT);
                CREATE TABLE IF NOT EXISTS images(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                    name TEXT,original_path TEXT,sha256 TEXT,active_version TEXT,created TEXT);
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,image_id TEXT REFERENCES images(id) ON DELETE CASCADE,
                    parent_id TEXT,path TEXT,width INTEGER,height INTEGER,sha256 TEXT,operations TEXT,created TEXT);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                    image_id TEXT REFERENCES images(id),version_id TEXT REFERENCES versions(id),engine TEXT,batch TEXT,
                    status TEXT,phase TEXT,error TEXT,created TEXT,started TEXT,finished TEXT,result_id TEXT);
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status,batch,engine,created);
                CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,task_id TEXT UNIQUE REFERENCES tasks(id),
                    original TEXT NOT NULL,edited TEXT NOT NULL,cursor INTEGER NOT NULL,revision INTEGER NOT NULL,updated TEXT);
                CREATE TABLE IF NOT EXISTS edits(result_id TEXT REFERENCES results(id) ON DELETE CASCADE,
                    position INTEGER,value TEXT,created TEXT,PRIMARY KEY(result_id,position));
                CREATE TABLE IF NOT EXISTS selections(image_id TEXT PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
                    result_id TEXT REFERENCES results(id));
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
        elif version in {2, 3, 4}:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            additions = {
                2: {"kind": "TEXT NOT NULL DEFAULT 'ocr'", "result_version_id": "TEXT"},
                3: {
                    "preprocess": "TEXT NOT NULL DEFAULT '[]'",
                    "input_version_id": "TEXT",
                },
                4: {"engine_package": "TEXT NOT NULL DEFAULT 'builtin'"},
            }
            for name, definition in additions[version].items():
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        elif version == 5:
            for name, table, columns in (
                ("images_project_created", "images", "project_id,created"),
                ("tasks_project_created", "tasks", "project_id,created"),
                ("tasks_image_created", "tasks", "image_id,created"),
                ("versions_image_created", "versions", "image_id,created"),
                ("versions_parent_operations", "versions", "parent_id,operations"),
                ("projects_updated", "projects", "updated"),
            ):
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
            db.execute(
                "CREATE TABLE project_revisions(project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, revision INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("INSERT INTO project_revisions SELECT id,0 FROM projects")
            db.execute(
                "CREATE TRIGGER project_revision_insert AFTER INSERT ON projects BEGIN INSERT INTO project_revisions VALUES(NEW.id,0); END"
            )
            db.execute(
                "CREATE TRIGGER project_revision_update AFTER UPDATE ON projects BEGIN UPDATE project_revisions SET revision=revision+1 WHERE project_id=NEW.id; END"
            )
            for table in ("images", "tasks", "versions", "results", "selections"):
                Store._revision_triggers(db, table)
        elif version == 6:
            cursor = db.execute("SELECT result_id,position,value FROM edits")
            while rows := cursor.fetchmany(100):
                for row in rows:
                    db.execute(
                        "UPDATE edits SET value=? WHERE result_id=? AND position=?",
                        (
                            history_encoded(json.loads(history_decoded(row["value"]))),
                            row["result_id"],
                            row["position"],
                        ),
                    )
        elif version == 7:
            db.execute(
                "CREATE TABLE reviews(image_id TEXT PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,result_id TEXT REFERENCES results(id) ON DELETE CASCADE,version_id TEXT,revision INTEGER,status TEXT NOT NULL CHECK(status IN ('pending','confirmed','question')),updated TEXT)"
            )
            Store._revision_triggers(db, "reviews")
        elif version == 8:
            db.execute("ALTER TABLE tasks ADD COLUMN fusion_config TEXT")
            for statement in (
                "CREATE TABLE fusion_inputs(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,snapshot BLOB NOT NULL,fingerprint TEXT NOT NULL)",
                "CREATE TABLE fusion_dependencies(task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,parent_task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,PRIMARY KEY(task_id,parent_task_id))",
                "CREATE TABLE fusion_submissions(project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,request_id TEXT,payload_hash TEXT,task_ids TEXT,created TEXT,PRIMARY KEY(project_id,request_id))",
                "CREATE TABLE fusion_evidence(result_id TEXT REFERENCES results(id) ON DELETE CASCADE,ordinal INTEGER,definition TEXT NOT NULL,PRIMARY KEY(result_id,ordinal))",
                "CREATE TABLE fusion_issues(id TEXT PRIMARY KEY,result_id TEXT REFERENCES results(id) ON DELETE CASCADE,ordinal INTEGER,category TEXT,state TEXT,definition TEXT,target TEXT,basis TEXT,current_value TEXT,decision_id TEXT,updated TEXT)",
                "CREATE INDEX fusion_issues_query ON fusion_issues(result_id,state,category,ordinal)",
                "CREATE TABLE fusion_decisions(result_id TEXT REFERENCES results(id) ON DELETE CASCADE,request_id TEXT,issue_id TEXT REFERENCES fusion_issues(id) ON DELETE CASCADE,payload_hash TEXT,action TEXT,response BLOB,previous TEXT,created TEXT,PRIMARY KEY(result_id,request_id))",
                "CREATE TABLE fusion_progress(result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,issue_id TEXT,updated TEXT)",
            ):
                db.execute(statement)
            db.execute("""CREATE TRIGGER fusion_version_changed AFTER UPDATE OF active_version ON images
                WHEN OLD.active_version IS NOT NEW.active_version BEGIN
                UPDATE reviews SET status='pending' WHERE image_id=NEW.id;
                UPDATE fusion_issues SET state='stale' WHERE result_id IN
                  (SELECT r.id FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.image_id=NEW.id);
                END""")
            db.execute("""CREATE TRIGGER fusion_selection_changed AFTER UPDATE OF result_id ON selections
                WHEN OLD.result_id IS NOT NEW.result_id BEGIN
                UPDATE reviews SET status='pending' WHERE image_id=NEW.image_id;
                UPDATE fusion_issues SET state='stale' WHERE result_id=OLD.result_id;
                END""")

        elif version == 9:
            migrate_v9(db)
        elif version == 10:
            from ocr_workbench.structure_store import migrate_v10
            migrate_v10(db)
        elif version == 11:
            from ocr_workbench.multimodal_store import migrate_v11
            migrate_v11(db)
        elif version == 12:
            db.execute("ALTER TABLE multimodal_requests ADD COLUMN backend TEXT NOT NULL DEFAULT 'local' CHECK(backend IN ('local','external'))")
            db.execute("CREATE INDEX multimodal_requests_backend ON multimodal_requests(backend,task_id)")
        elif version == 13:
            from ocr_workbench.agent.migrations import migrate_v13
            migrate_v13(db)
        elif version == 14:
            from ocr_workbench.agent.migrations import migrate_v14
            migrate_v14(db)
        elif version == 15:
            from ocr_workbench.platform_imports import migrate_v15
            migrate_v15(db)
        elif version == 16:
            db.execute("""CREATE TABLE result_revisions(
                result_id TEXT REFERENCES results(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision>=0), edited BLOB NOT NULL,
                cursor INTEGER NOT NULL, parent_revision INTEGER, created TEXT NOT NULL,
                origin TEXT NOT NULL, PRIMARY KEY(result_id,revision))""")
            db.execute("""CREATE TABLE platform_adoptions(
                operation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                instance_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                receipt TEXT NOT NULL, created TEXT NOT NULL)""")
            db.execute("CREATE INDEX platform_adoptions_project ON platform_adoptions(project_id,created)")
            # The undo cursor is not a revision. Legacy middle revisions cannot
            # be reconstructed, especially after branching; only known values
            # are migrated and gaps remain explicitly unavailable.
            for row in db.execute("SELECT * FROM results"):
                original = json.loads(row["original"])
                initial = {"text": original["text"], "tables": original["tables"]}
                db.execute("INSERT INTO result_revisions VALUES(?,?,?,?,?,?,?)",
                           (row["id"], 0, history_encoded(initial), 0, None,
                            now(), "migrated_original"))
                if row["revision"]:
                    db.execute("INSERT INTO result_revisions VALUES(?,?,?,?,?,?,?)",
                               (row["id"], row["revision"], history_encoded(json.loads(row["edited"])),
                                row["cursor"], None, row["updated"], "migrated_current"))
            db.execute("""CREATE TRIGGER result_revision_initial AFTER INSERT ON results BEGIN
                INSERT INTO result_revisions VALUES(NEW.id,NEW.revision,ocr_history(NEW.edited),
                    NEW.cursor,NULL,NEW.updated,'initial'); END""")
            db.execute("""CREATE TRIGGER result_revision_guard BEFORE UPDATE ON results
                WHEN NEW.id IS NOT OLD.id OR NEW.task_id IS NOT OLD.task_id OR NEW.original IS NOT OLD.original
                  OR ((NEW.edited IS NOT OLD.edited OR NEW.cursor IS NOT OLD.cursor OR NEW.revision IS NOT OLD.revision)
                      AND NEW.revision != OLD.revision+1)
                BEGIN SELECT RAISE(ABORT,'immutable result identity or nonmonotonic revision'); END""")
            db.execute("""CREATE TRIGGER result_revision_snapshot AFTER UPDATE ON results
                WHEN NEW.revision != OLD.revision BEGIN
                INSERT INTO result_revisions VALUES(NEW.id,NEW.revision,ocr_history(NEW.edited),
                    NEW.cursor,OLD.revision,NEW.updated,'revision'); END""")
            db.execute("""CREATE TRIGGER result_revision_immutable BEFORE UPDATE ON result_revisions
                BEGIN SELECT RAISE(ABORT,'immutable result revision'); END""")
            db.execute("""CREATE TRIGGER platform_adoption_immutable BEFORE UPDATE ON platform_adoptions
                BEGIN SELECT RAISE(ABORT,'immutable adoption receipt'); END""")

        elif version == 17:
            db.execute('''CREATE TABLE platform_page_requests(
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,body TEXT NOT NULL,
                stage_ids TEXT NOT NULL,owned_stage_ids TEXT NOT NULL,created TEXT NOT NULL,
                PRIMARY KEY(document_id,request_id))''')
        elif version == 18:
            from ocr_workbench.screen_schema import migrate_screen_schema
            migrate_screen_schema(db)

    @staticmethod
    def _revision_triggers(db, table):
        for action in ("INSERT", "UPDATE", "DELETE"):
            ref = "OLD" if action == "DELETE" else "NEW"
            if table in {"images", "tasks"}:
                project = f"{ref}.project_id"
            elif table in {"versions", "selections", "reviews"}:
                project = f"(SELECT project_id FROM images WHERE id={ref}.image_id)"
            else:
                project = f"(SELECT project_id FROM tasks WHERE id={ref}.task_id)"
            db.execute(
                f"CREATE TRIGGER revision_{table}_{action.lower()} AFTER {action} ON {table} BEGIN UPDATE project_revisions SET revision=revision+1 WHERE project_id={project}; END"
            )

    def project_revision(self, key):
        rows = self.rows(
            "SELECT revision FROM project_revisions WHERE project_id=?", (key,)
        )
        if not rows:
            raise KeyError("项目不存在")
        return rows[0]["revision"]

    def project_snapshot(self, key, since_revision=None):
        with self.transaction() as db:
            # SQLite's first SELECT otherwise runs outside a transaction. Pin
            # one WAL snapshot so its revision describes every returned row.
            db.execute("BEGIN")
            row = db.execute(
                "SELECT revision FROM project_revisions WHERE project_id=?", (key,)
            ).fetchone()
            if not row:
                raise KeyError("项目不存在")
            revision = row["revision"]
            if revision == since_revision:
                return {"revision": revision, "unchanged": True}
            reviews = self._review_states(db, key)
            images = [
                dict(row)
                for row in db.execute(
                    "SELECT i.*,s.result_id selected_result,p.document_id,p.page_number,d.kind document_kind FROM images i LEFT JOIN selections s ON s.image_id=i.id LEFT JOIN pages p ON p.image_id=i.id LEFT JOIN documents d ON d.id=p.document_id WHERE i.project_id=? ORDER BY i.created,i.id",
                    (key,),
                )
            ]
            for image in images:
                image["review_status"] = reviews[image["id"]]["status"]
                image["review_state"] = reviews[image["id"]]
            return {
                "project": dict(
                    db.execute("SELECT * FROM projects WHERE id=?", (key,)).fetchone()
                ),
                "revision": revision,
                "unchanged": False,
                "reviews": reviews,
                "images": images,
                "documents": [dict(row) for row in db.execute(
                    "SELECT * FROM documents WHERE project_id=? ORDER BY created,id", (key,))],
                "versions": [
                    dict(row)
                    for row in db.execute(
                        "SELECT v.* FROM versions v JOIN images i ON i.id=v.image_id WHERE i.project_id=? ORDER BY v.created,v.id",
                        (key,),
                    )
                ],
                "tasks": [
                    dict(row)
                    for row in db.execute(
                        """SELECT t.*, mr.result_id AS review_result_id, mr.backend AS review_backend FROM tasks t
                        LEFT JOIN multimodal_requests mr ON mr.task_id=t.id
                        WHERE t.project_id=? ORDER BY t.created,t.id""",
                        (key,),
                    )
                ],
            }

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(self.root / "workbench.sqlite3", timeout=15)
            db.row_factory = sqlite3.Row
            db.create_function("ocr_history", 1, lambda value: history_encoded(json.loads(value)))
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            except sqlite3.OperationalError as error:
                if "full" in str(error).lower():
                    raise ValueError(
                        "项目磁盘空间不足，写入未完成；请清理磁盘后重试，保留当前校对草稿"
                    ) from error
                raise
            finally:
                db.close()

    def rows(self, sql, params=()):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def one(self, table, key):
        if table not in {"projects", "images", "versions", "tasks", "results", "documents", "pages", "regions", "document_stages", "geometry_evidence"}:
            raise ValueError("Unknown entity")
        rows = self.rows(f"SELECT * FROM {table} WHERE id=?", (key,))
        if not rows:
            raise KeyError("记录不存在")
        return rows[0]

    def file(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("无效的项目文件路径")
        return path

    def project(self, name, request_id=None):
        import re
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', request_id)):
            raise ValueError('创建请求无效')
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("项目名称应为 1–120 个字符")
        key = uid()
        with self.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS project_create_requests(request_id TEXT PRIMARY KEY, name TEXT NOT NULL, project_id TEXT NOT NULL)')
            if request_id:
                prior = db.execute('SELECT name,project_id FROM project_create_requests WHERE request_id=?', (request_id,)).fetchone()
                if prior:
                    if prior['name'] != name:
                        raise ValueError('创建请求已用于其他内容')
                    row = db.execute('SELECT * FROM projects WHERE id=?', (prior['project_id'],)).fetchone()
                    if row is None:
                        raise ValueError('原请求创建的项目已删除')
                    return dict(row)
            db.execute(
                "INSERT INTO projects VALUES(?,?,?,?)", (key, name, now(), now())
            )
            if request_id:
                db.execute('INSERT INTO project_create_requests VALUES(?,?,?)', (request_id, name, key))
        return self.one("projects", key)

    def queue_scope(self, fusion=False, external=False):
        if self.schema_version < 12:
            return '0' if external else "kind='fusion'" if fusion else "kind!='fusion'"
        remote = "EXISTS (SELECT 1 FROM multimodal_requests m WHERE m.task_id=tasks.id AND m.backend='external')"
        if external:
            return remote
        return "kind='fusion'" if fusion else "kind!='fusion' AND NOT " + remote

    def recover(self, fusion=None, external=False):
        # Both queued and formerly running work require deliberate resume on launch.
        with self.transaction() as db:
            scope = "" if fusion is None and not external else " AND " + self.queue_scope(fusion, external)
            db.execute(
                "UPDATE tasks SET status='interrupted',phase='等待继续',error='上次程序退出时任务未完成' WHERE status='running'" + scope
            )
            db.execute(
                "UPDATE tasks SET status='paused',phase='等待继续' WHERE status='queued'" + scope
            )

    def enqueue(
        self, project_id, versions, engines, preprocess=None, engine_packages=None, fusion_policy=None, request_id=None
    ):
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._enqueue(db, project_id, versions, engines, preprocess, engine_packages, fusion_policy, request_id)

    def _enqueue(
        self, db, project_id, versions, engines, preprocess=None, engine_packages=None, fusion_policy=None, request_id=None
    ):
        from ocr_workbench.imaging import validate_preset

        preprocess = validate_preset(preprocess or [])
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise KeyError("项目不存在")
        packages = engine_packages or {
            e: "builtin" for e in ["ppocr", "paddlevl", "glm", "hunyuan"]
        }
        if not versions or len(versions) > 1000 or not engines or len(engines) > 16:
            raise ValueError("请选择图片与引擎；单次最多 1000 张")
        batch = now() + "-" + uid()
        tasks = []
        if request_id is not None and fusion_policy is None:
            if not isinstance(request_id, str) or not 8 <= len(request_id) <= 120:
                raise ValueError("识别批次缺少有效请求标识")
            from ocr_workbench.fusion_alignment import fingerprint
            signature = fingerprint({"versions": versions, "engines": engines, "preprocess": preprocess, "packages": packages})
            previous = db.execute("SELECT input_hash,task_ids FROM ocr_submissions WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
            if previous:
                if previous["input_hash"] != signature:
                    raise Conflict("请求编号已用于其他识别参数")
                return json.loads(previous["task_ids"])
        if fusion_policy is not None:
            from ocr_workbench.fusion_alignment import fingerprint
            if not isinstance(request_id, str) or not 8 <= len(request_id) <= 120:
                raise ValueError("融合批次缺少有效请求标识")
            signature = fingerprint(
                {"versions": versions, "engines": engines, "preprocess": preprocess, "packages": packages, "policy": fusion_policy})
            prior = self._fusion_submission(db, project_id, request_id, signature)
            if prior is not None:
                return prior
        for engine in dict.fromkeys(engines):
            if engine not in packages:
                raise ValueError("未知识别引擎")
            for version_id in dict.fromkeys(versions):
                version = db.execute(
                    "SELECT v.*,i.project_id FROM versions v JOIN images i ON i.id=v.image_id WHERE v.id=?",
                    (version_id,),
                ).fetchone()
                if not version or version["project_id"] != project_id:
                    raise ValueError("图片版本不属于当前项目")
                key = uid()
                db.execute(
                    "INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,preprocess,input_version_id,engine_package) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        project_id,
                        version["image_id"],
                        version_id,
                        engine,
                        batch,
                        "queued",
                        "等待识别",
                        now(),
                        encoded(preprocess),
                        version_id,
                        packages[engine],
                    ),
                )
                tasks.append(key)
        if fusion_policy is not None:
            tasks.extend(self.attach_batch_fusion(db, project_id, tasks, fusion_policy))
            db.execute("INSERT INTO fusion_submissions VALUES(?,?,?,?,?)", (project_id, request_id, signature, encoded(tasks), now()))
        if request_id is not None and fusion_policy is None:
            db.execute("INSERT INTO ocr_submissions VALUES(?,?,?,?,?)", (project_id, request_id, signature, encoded(tasks), now()))
        return tasks

    def claim(self, fusion=False, external=False):
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            scope = self.queue_scope(fusion, external)
            if fusion:
                scope += " AND NOT EXISTS (SELECT 1 FROM fusion_dependencies d JOIN tasks p ON p.id=d.parent_task_id WHERE d.task_id=tasks.id AND p.status NOT IN ('succeeded','failed','cancelled'))"
            task = db.execute(
                "SELECT * FROM tasks WHERE status='queued' AND " + scope + " ORDER BY batch,engine,created,id LIMIT 1"
            ).fetchone()
            if not task:
                return None
            db.execute(
                "UPDATE tasks SET status='running',phase=?,started=?,error=NULL WHERE id=? AND status='queued'",
                ("融合计算中" if fusion else "加载引擎", now(), task["id"]),
            )
            return dict(task)

    def enqueue_dewarp(self, version_id):
        version = self.one("versions", version_id)
        image = self.one("images", version["image_id"])
        key = uid()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    image["project_id"],
                    image["id"],
                    version_id,
                    "dewarp",
                    now() + "-" + uid(),
                    "queued",
                    "等待去弯曲",
                    now(),
                    "dewarp",
                ),
            )
        return key

    def complete(self, task_id, data):
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task["status"] != "running":
                return False
            key = uid()
            units = []
            if task["kind"] == "fusion":
                from copy import deepcopy
                data = deepcopy(data)
                units = data["fusion"].pop("units")
                data["fusion"]["evidence_units"] = len(units)
            edit = {"text": data["text"], "tables": data["tables"]}
            db.execute(
                "INSERT INTO results VALUES(?,?,?,?,?,?,?)",
                (key, task_id, encoded(data), encoded(edit), 0, 0, now()),
            )
            db.execute(
                "INSERT INTO edits VALUES(?,?,?,?)",
                (key, 0, history_encoded(edit), now()),
            )
            db.execute(
                "UPDATE tasks SET status='succeeded',phase='已完成',finished=?,result_id=? WHERE id=?",
                (now(), key, task_id),
            )
            if task["kind"] != "fusion":
                db.execute(
                    "INSERT OR IGNORE INTO selections VALUES(?,?)", (task["image_id"], key)
                )
            if task["kind"] == "fusion":
                db.executemany("INSERT INTO fusion_evidence VALUES(?,?,?)", [(key, i, encoded(unit)) for i, unit in enumerate(units)])
                self.persist_fusion_issues(db, key, units, edit)
            db.execute(
                "UPDATE projects SET updated=? WHERE id=?", (now(), task["project_id"])
            )
            return True

    def result(self, key):
        data = self.one("results", key)
        data["original"] = json.loads(data["original"])
        data["edited"] = json.loads(data["edited"])
        data["can_undo"] = data["cursor"] > 0
        data["can_redo"] = bool(
            self.rows(
                "SELECT 1 FROM edits WHERE result_id=? AND position>?",
                (key, data["cursor"]),
            )
        )
        return data

    @staticmethod
    def _result_identity(db, key):
        row = db.execute("""SELECT r.*, t.project_id,t.image_id,t.status,
                COALESCE(json_extract(r.original,'$.project_image_version'),t.version_id) version_id,
                i.active_version,i.sha256 original_sha256,i.original_path
                FROM results r JOIN tasks t ON t.id=r.task_id JOIN images i ON i.id=t.image_id
                WHERE r.id=? AND i.project_id=t.project_id""", (key,)).fetchone()
        if row is None:
            raise KeyError("识别结果不存在或来源身份无效")
        return row

    def result_snapshot(self, key, revision=None):
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("修订版本无效")
        with self.transaction() as db:
            db.execute("BEGIN")
            result = self._result_identity(db, key)
            revision = result["revision"] if revision is None else revision
            if revision > result["revision"]:
                raise KeyError("修订版本不存在")
            row = db.execute("SELECT * FROM result_revisions WHERE result_id=? AND revision=?",
                             (key, revision)).fetchone()
            identity = {"id": key, "task_id": result["task_id"], "project_id": result["project_id"],
                        "image_id": result["image_id"], "version_id": result["version_id"],
                        "original_sha256": result["original_sha256"], "revision": revision,
                        "current_revision": result["revision"], "available": row is not None}
            if row is None:
                return {**identity, "reason": "legacy_revision_unavailable"}
            return {**identity, "edited": json.loads(history_decoded(row["edited"])),
                    "original": json.loads(result["original"]), "cursor": row["cursor"],
                    "parent_revision": row["parent_revision"], "created": row["created"],
                    "origin": row["origin"]}

    def result_history(self, key, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("修订历史分页无效")
        with self.transaction() as db:
            db.execute("BEGIN")
            result = self._result_identity(db, key)
            total = db.execute("SELECT COUNT(*) FROM result_revisions WHERE result_id=?", (key,)).fetchone()[0]
            rows = db.execute("SELECT revision,parent_revision,cursor,created,origin FROM result_revisions "
                              "WHERE result_id=? ORDER BY revision LIMIT ? OFFSET ?", (key, limit, offset)).fetchall()
            return {"id": key, "task_id": result["task_id"], "project_id": result["project_id"],
                    "image_id": result["image_id"], "version_id": result["version_id"],
                    "current_revision": result["revision"], "total": total, "offset": offset,
                    "limit": limit, "unavailable_revisions": result["revision"]+1-total,
                    "items": [dict(row) for row in rows]}

    @staticmethod
    def _adoption_request(operation_id, body):
        native_id = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
        request_id = re.compile(r"[A-Za-z0-9_-]{8,120}\Z")
        fields = {"project_id", "instance_id", "result_id", "task_id", "image_id", "version_id",
                  "revision", "original_sha256", "actor", "note"}
        actor = body.get("actor") if isinstance(body, dict) else None
        if (not isinstance(operation_id, str) or not request_id.fullmatch(operation_id)
                or not isinstance(body, dict) or set(body) != fields
                or any(not isinstance(body[key], str) or not native_id.fullmatch(body[key])
                       for key in ("project_id", "instance_id", "result_id", "task_id", "image_id", "version_id"))
                or type(body["revision"]) is not int or body["revision"] < 0
                or not isinstance(body["original_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", body["original_sha256"])
                or not isinstance(actor, dict) or set(actor) != {"id", "display_name", "source"}
                or not isinstance(actor["id"], str) or not native_id.fullmatch(actor["id"])
                or not isinstance(actor["display_name"], str) or not 1 <= len(actor["display_name"]) <= 100
                or actor["source"] != "local" or not isinstance(body["note"], str) or len(body["note"]) > 2000):
            raise ValueError("采用操作必须提供明确版本、来源身份、本机操作人及稳定操作标识")
        return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

    def adoption_operation(self, operation_id, project_id, instance_id):
        if (not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,120}", operation_id)):
            raise ValueError("采用操作标识无效")
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)
               for value in (project_id, instance_id)):
            raise ValueError("原采用操作的项目或实例标识无效")
        rows = self.rows("SELECT * FROM platform_adoptions WHERE operation_id=?", (operation_id,))
        if not rows:
            return None
        if (rows[0]["project_id"], rows[0]["instance_id"]) != (project_id, instance_id):
            raise Conflict("采用操作属于其他项目或实例")
        return json.loads(rows[0]["receipt"])

    def adopt_result(self, operation_id, body):
        digest = self._adoption_request(operation_id, body)
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM platform_adoptions WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise Conflict("同一采用操作标识已用于不同内容")
                return json.loads(existing["receipt"])
            row = self._result_identity(db, body["result_id"])
            if (row["revision"] != body["revision"] or row["status"] != "succeeded"
                    or any(row[key] != body[key] for key in
                           ("task_id", "project_id", "image_id", "version_id", "original_sha256"))
                    or row["active_version"] != body["version_id"]):
                raise Conflict("采用的结果修订、图像版本或来源身份已变化")
            version = db.execute("SELECT image_id,path,sha256 FROM versions WHERE id=?", (body["version_id"],)).fetchone()
            if not version or version["image_id"] != body["image_id"]:
                raise Conflict("采用的图像版本不属于当前原件")
            try:
                with self.file(row["original_path"]).open("rb") as source:
                    actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
                with self.file(version["path"]).open("rb") as source:
                    version_hash = hashlib.file_digest(source, "sha256").hexdigest()
            except OSError:
                raise Conflict("原件不可核对，未采用修订") from None
            if actual_hash != body["original_sha256"] or version_hash != version["sha256"]:
                raise Conflict("原件内容已变化，未采用修订")
            previous = self._review_target(db, body["image_id"])
            db.execute("INSERT INTO selections VALUES(?,?) ON CONFLICT(image_id) DO UPDATE SET result_id=excluded.result_id",
                       (body["image_id"], body["result_id"]))
            # Adoption records the selected revision. Native review confirmation
            # remains its own deliberate action, including fusion issue checks.
            receipt = {"operation_id": operation_id, "payload_hash": digest, "state": "committed",
                       **body, "previous": {"result_id": previous["result_id"],
                                            "revision": previous["revision"], "version_id": previous["version_id"]},
                       "created": now()}
            db.execute("INSERT INTO platform_adoptions VALUES(?,?,?,?,?,?)",
                       (operation_id, body["project_id"], body["instance_id"], digest, encoded(receipt), receipt["created"]))
        return receipt

    def edit_operation(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,120}', operation_id):
            raise ValueError('编辑操作标识无效')
        if not self.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='platform_edits'"):
            return None
        rows = self.rows('SELECT receipt FROM platform_edits WHERE operation_id=?', (operation_id,))
        return json.loads(rows[0]['receipt']) if rows else None

    def save(self, key, edit, expected_revision, *, operation=None):
        from ocr_workbench.editing import validate_edit

        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("必须提供用户所见的修订版本")
        validate_edit(edit)
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if operation:
                db.execute('CREATE TABLE IF NOT EXISTS platform_edits (operation_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)')
                old = db.execute('SELECT receipt FROM platform_edits WHERE operation_id=?', (operation['operation_id'],)).fetchone()
                if old:
                    receipt = json.loads(old['receipt'])
                    if receipt['payload_hash'] != operation['payload_hash'] or receipt['result_id'] != key:
                        raise Conflict('编辑身份已绑定不同内容')
                    return receipt
            row = db.execute("SELECT * FROM results WHERE id=?", (key,)).fetchone()
            if not row:
                raise KeyError("识别结果不存在")
            if row["revision"] != expected_revision:
                raise Conflict("该结果已在其他窗口更新，请重新加载后编辑")
            fusion = json.loads(row["original"]).get("origin") == "fusion"
            if fusion:
                from ocr_workbench.fusion_alignment import canonical_edit
                edit = canonical_edit(edit)
            if json.loads(row["edited"]) == edit:
                if operation:
                    receipt = {**operation, 'state': 'committed', 'result_id': key, 'revision': row['revision']}
                    db.execute('INSERT INTO platform_edits VALUES(?,?)', (operation['operation_id'], encoded(receipt)))
                    return receipt
                return self.result(key)
            cursor = row["cursor"] + 1
            db.execute(
                "DELETE FROM edits WHERE result_id=? AND position>?",
                (key, row["cursor"]),
            )
            db.execute(
                "INSERT INTO edits VALUES(?,?,?,?)",
                (key, cursor, history_encoded(edit), now()),
            )
            db.execute(
                "UPDATE results SET edited=?,cursor=?,revision=revision+1,updated=? WHERE id=?",
                (encoded(edit), cursor, now(), key),
            )
            if fusion:
                from ocr_workbench.review_issues import reconcile
                reconcile(db, key, json.loads(row["edited"]), edit)
            from ocr_workbench.geometry import reconcile_geometry
            reconcile_geometry(db, key, json.loads(row['edited']), edit)
            from ocr_workbench.structure_store import reconcile_structure
            reconcile_structure(db, key, json.loads(row['edited']), edit)
            from ocr_workbench.multimodal_store import reconcile_review
            reconcile_review(db, key)
            if operation:
                receipt = {**operation, 'state': 'committed', 'result_id': key, 'revision': expected_revision+1}
                db.execute('INSERT INTO platform_edits VALUES(?,?)', (operation['operation_id'], encoded(receipt)))
        if operation:
            return receipt
        return self.result(key)

    def history(self, key, direction, expected_revision):
        if type(direction) is not int or direction not in {-1, 1}:
            raise ValueError("无效历史方向")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("必须提供用户所见的修订版本")
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM results WHERE id=?", (key,)).fetchone()
            if not row:
                raise KeyError("识别结果不存在")
            if row["revision"] != expected_revision:
                raise Conflict("结果已更新，请重新加载")
            cursor = row["cursor"] + direction
            edit = db.execute(
                "SELECT value FROM edits WHERE result_id=? AND position=?",
                (key, cursor),
            ).fetchone()
            if not edit:
                raise ValueError("没有可撤销或重做的记录")
            db.execute(
                "UPDATE results SET edited=?,cursor=?,revision=revision+1,updated=? WHERE id=?",
                (history_decoded(edit["value"]), cursor, now(), key),
            )
            if json.loads(row["original"]).get("origin") == "fusion":
                from ocr_workbench.review_issues import reconcile
                reconcile(db, key, json.loads(row["edited"]), json.loads(history_decoded(edit["value"])))
                # An undo/redo of a keep/question event has unchanged content.
                # Its own decision still must expire, without expiring others.
                position = row["cursor"] if direction == -1 else cursor
                for decision in db.execute("SELECT request_id,response FROM fusion_decisions WHERE result_id=?", (key,)).fetchall():
                    response = json.loads(history_decoded(decision["response"]))
                    if response["cursor"] == position:
                        db.execute("UPDATE fusion_issues SET state='stale',updated=? WHERE result_id=? AND decision_id=?", (now(), key, decision["request_id"]))
            from ocr_workbench.geometry import reconcile_geometry
            reconcile_geometry(db, key, json.loads(row['edited']), json.loads(history_decoded(edit['value'])))
            from ocr_workbench.structure_store import reconcile_structure
            reconcile_structure(db, key, json.loads(row['edited']), json.loads(history_decoded(edit['value'])))
            from ocr_workbench.multimodal_store import reconcile_review
            reconcile_review(db, key)
        return self.result(key)

    @staticmethod
    def _review_target(db, image_id):
        return db.execute(
            """SELECT i.active_version, r.id result_id, r.revision,
                      COALESCE(json_extract(r.original,'$.project_image_version'),t.version_id) version_id
               FROM images i LEFT JOIN selections s ON s.image_id=i.id
               LEFT JOIN results r ON r.id=COALESCE(s.result_id,
                 (SELECT result_id FROM tasks WHERE image_id=i.id AND status='succeeded' AND kind!='fusion'
                  AND result_id IS NOT NULL ORDER BY created DESC,id DESC LIMIT 1))
               LEFT JOIN tasks t ON t.id=r.task_id WHERE i.id=?""",
            (image_id,),
        ).fetchone()

    def set_review(self, image_id, status, result_id, revision, version_id):
        if status not in {"pending", "confirmed", "question"}:
            raise ValueError("未知复核状态")
        with self.transaction() as db:
            # Lock before reading the adopted result and its revision.
            db.execute("BEGIN IMMEDIATE")
            target = self._review_target(db, image_id)
            if not target:
                raise KeyError("图片不存在")
            if (
                not result_id
                or target["result_id"] != result_id
                or target["revision"] != revision
                or target["active_version"] != version_id
                or target["version_id"] != version_id
            ):
                raise Conflict("采用结果、图像版本或校对内容已变化，请重新加载后复核")
            db.execute(
                "INSERT INTO reviews VALUES(?,?,?,?,?,?) ON CONFLICT(image_id) DO UPDATE SET result_id=excluded.result_id,version_id=excluded.version_id,revision=excluded.revision,status=excluded.status,updated=excluded.updated",
                (image_id, result_id, version_id, revision, status, now()),
            )
        return {
            "image_id": image_id,
            "status": status,
            "result_id": result_id,
            "version_id": version_id,
            "revision": revision,
        }

    def review_states(self, project_id):
        with self.transaction() as db:
            return self._review_states(db, project_id)

    @staticmethod
    def _review_states(db, project_id):
        rows = db.execute(
            """SELECT i.id image_id,i.active_version,rv.status,rv.result_id,
                       rv.version_id,rv.revision,rv.updated,r.revision current_revision,
                       r.id current_result_id,
                       COALESCE(json_extract(r.original,'$.project_image_version'),t.version_id) result_version_id
                   FROM images i LEFT JOIN reviews rv ON rv.image_id=i.id
                   LEFT JOIN selections s ON s.image_id=i.id
                   LEFT JOIN results r ON r.id=COALESCE(s.result_id,
                     (SELECT result_id FROM tasks WHERE image_id=i.id AND status='succeeded' AND kind!='fusion'
                      AND result_id IS NOT NULL ORDER BY created DESC,id DESC LIMIT 1))
                   LEFT JOIN tasks t ON t.id=r.task_id WHERE i.project_id=?""",
            (project_id,),
        ).fetchall()
        states = {}
        for row in rows:
            current = (
                row["result_id"] is not None
                and row["result_id"] == row["current_result_id"]
                and row["revision"] == row["current_revision"]
                and row["version_id"]
                == row["active_version"]
                == row["result_version_id"]
            )
            states[row["image_id"]] = {
                "status": row["status"] if current else "pending",
                "result_id": row["current_result_id"],
                "revision": row["current_revision"],
                "version_id": row["active_version"],
                "updated": row["updated"],
                "stale": bool(row["status"] and not current),
            }
        return states
