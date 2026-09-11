"""Transactional project, queue and edit history storage outside the application."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.transaction() as db:
            db.executescript(
                """
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
                PRAGMA user_version=1;
            """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "kind" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'ocr'"
                )
            if "result_version_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN result_version_id TEXT")
            if "preprocess" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN preprocess TEXT NOT NULL DEFAULT '[]'"
                )
            if "input_version_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN input_version_id TEXT")
            if "engine_package" not in columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN engine_package TEXT NOT NULL DEFAULT 'builtin'"
                )
            db.execute("PRAGMA user_version=4")

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(self.root / "workbench.sqlite3", timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def rows(self, sql, params=()):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def one(self, table, key):
        if table not in {"projects", "images", "versions", "tasks", "results"}:
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

    def project(self, name):
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("项目名称应为 1–120 个字符")
        key = uid()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO projects VALUES(?,?,?,?)", (key, name, now(), now())
            )
        return self.one("projects", key)

    def recover(self):
        # Both queued and formerly running work require deliberate resume on launch.
        with self.transaction() as db:
            db.execute(
                "UPDATE tasks SET status='interrupted',phase='等待继续',error='上次程序退出时任务未完成' WHERE status='running'"
            )
            db.execute(
                "UPDATE tasks SET status='paused',phase='等待继续' WHERE status='queued'"
            )

    def enqueue(
        self, project_id, versions, engines, preprocess=None, engine_packages=None
    ):
        from ocr_workbench.imaging import validate_preset

        preprocess = validate_preset(preprocess or [])
        self.one("projects", project_id)
        packages = engine_packages or {
            e: "builtin" for e in ["ppocr", "paddlevl", "glm", "hunyuan"]
        }
        if not versions or len(versions) > 1000 or not engines or len(engines) > 16:
            raise ValueError("请选择图片与引擎；单次最多 1000 张")
        batch = now() + "-" + uid()
        tasks = []
        with self.transaction() as db:
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
        return tasks

    def claim(self):
        with self.transaction() as db:
            task = db.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY batch,engine,created LIMIT 1"
            ).fetchone()
            if not task:
                return None
            db.execute(
                "UPDATE tasks SET status='running',phase='加载引擎',started=?,error=NULL WHERE id=? AND status='queued'",
                (now(), task["id"]),
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
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task["status"] != "running":
                return False
            key = uid()
            edit = {"text": data["text"], "tables": data["tables"]}
            db.execute(
                "INSERT INTO results VALUES(?,?,?,?,?,?,?)",
                (key, task_id, encoded(data), encoded(edit), 0, 0, now()),
            )
            db.execute(
                "INSERT INTO edits VALUES(?,?,?,?)", (key, 0, encoded(edit), now())
            )
            db.execute(
                "UPDATE tasks SET status='succeeded',phase='已完成',finished=?,result_id=? WHERE id=?",
                (now(), key, task_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO selections VALUES(?,?)", (task["image_id"], key)
            )
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

    def save(self, key, edit, expected_revision):
        from ocr_workbench.editing import validate_edit

        validate_edit(edit)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM results WHERE id=?", (key,)).fetchone()
            if not row:
                raise KeyError("识别结果不存在")
            if row["revision"] != expected_revision:
                raise Conflict("该结果已在其他窗口更新，请重新加载后编辑")
            if json.loads(row["edited"]) == edit:
                return self.result(key)
            cursor = row["cursor"] + 1
            db.execute(
                "DELETE FROM edits WHERE result_id=? AND position>?",
                (key, row["cursor"]),
            )
            db.execute(
                "INSERT INTO edits VALUES(?,?,?,?)", (key, cursor, encoded(edit), now())
            )
            db.execute(
                "UPDATE results SET edited=?,cursor=?,revision=revision+1,updated=? WHERE id=?",
                (encoded(edit), cursor, now(), key),
            )
        return self.result(key)

    def history(self, key, direction, expected_revision):
        if direction not in {-1, 1}:
            raise ValueError("无效历史方向")
        with self.transaction() as db:
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
                (edit["value"], cursor, now(), key),
            )
        return self.result(key)
