"""Project storage accounting and recoverable, project-scoped cleanup."""

import json
import re
import shutil
import threading
from pathlib import Path
from ocr_workbench.store import uid


class ProjectMaintenance:
    def __init__(self, store, queue):
        self.store, self.queue = store, queue
        self.guard = threading.RLock()
        self.trash = store.root / "cleanup"
        self.trash.mkdir(exist_ok=True)
        self.recover()

    def paths(self, key):
        self.store.one("projects", key)
        paths = [self.store.root / "projects" / key]
        paths.extend(
            self.store.root / "task-results" / row["id"]
            for row in self.store.rows(
                "SELECT id FROM tasks WHERE project_id=?", (key,)
            )
        )
        paths.extend(
            self.store.root / "thumbnails" / (row["id"] + ".jpg")
            for row in self.store.rows(
                "SELECT v.id FROM versions v JOIN images i ON i.id=v.image_id WHERE i.project_id=?",
                (key,),
            )
        )
        return [path for path in paths if path.exists()]

    def safe(self, relative, key):
        path = self.store.file(relative)
        parts = Path(relative).parts
        valid = (
            (len(parts) == 2 and parts[0] == "projects" and parts[1] == key)
            or (
                len(parts) == 2
                and parts[0] == "task-results"
                and re.fullmatch("[a-f0-9]{32}", parts[1])
            )
            or (
                len(parts) == 2
                and parts[0] == "thumbnails"
                and re.fullmatch(r"[a-f0-9]{32}\.jpg", parts[1])
            )
        )
        if not valid or path.is_symlink() or path.is_junction():
            raise ValueError("清理路径不符合项目目录规则")
        return path

    def usage(self, key):
        with self.guard:
            paths = self.paths(key)
            total = 0
            files = 0
            for path in paths:
                for file in [path] if path.is_file() else path.rglob("*"):
                    if file.is_file():
                        total += file.stat().st_size
                        files += 1
            return {
                "bytes": total,
                "files": files,
                "scope": "原图、处理版本、任务输出和缩略图；共享引擎与诊断日志保留",
            }

    def recover(self):
        for folder in self.trash.iterdir():
            if not folder.is_dir() or not re.fullmatch("[a-f0-9]{32}", folder.name):
                continue
            ledger = folder / "ledger.json"
            if not ledger.exists():
                continue
            job = json.loads(ledger.read_text("utf-8"))
            key = job["project_id"]
            exists = bool(self.store.rows("SELECT id FROM projects WHERE id=?", (key,)))
            if exists:
                for index, relative in enumerate(job["paths"]):
                    original = self.safe(relative, key)
                    staged = folder / str(index)
                    if staged.exists():
                        if original.exists():
                            raise RuntimeError(
                                "项目清理恢复发生路径冲突，请保留数据并查看日志"
                            )
                        original.parent.mkdir(parents=True, exist_ok=True)
                        staged.replace(original)
            shutil.rmtree(folder)

    def delete(self, key, confirmation):
        with self.guard:
            project = self.store.one("projects", key)
            if confirmation != project["name"]:
                raise ValueError("请输入完整项目名称以确认清理")
            task_ids = {
                r["id"]
                for r in self.store.rows(
                    "SELECT id FROM tasks WHERE project_id=?", (key,)
                )
            }
            if self.queue.status()["task_id"] in task_ids:
                raise ValueError("项目任务仍在运行，请先取消并等待引擎退出")
            paths = self.paths(key)
            usage = self.usage(key)
            folder = self.trash / uid()
            folder.mkdir()
            relatives = [str(path.relative_to(self.store.root)) for path in paths]
            (folder / "ledger.json").write_text(
                json.dumps({"project_id": key, "paths": relatives}), "utf-8"
            )
            try:
                with self.store.transaction() as db:
                    if db.execute(
                        "SELECT 1 FROM tasks WHERE project_id=? AND status IN ('queued','running')",
                        (key,),
                    ).fetchone():
                        raise ValueError("请先暂停或取消该项目的等待任务")
                    for index, relative in enumerate(relatives):
                        self.safe(relative, key).replace(folder / str(index))
                    db.execute(
                        "DELETE FROM selections WHERE image_id IN (SELECT id FROM images WHERE project_id=?)",
                        (key,),
                    )
                    db.execute(
                        "DELETE FROM results WHERE task_id IN (SELECT id FROM tasks WHERE project_id=?)",
                        (key,),
                    )
                    db.execute("DELETE FROM tasks WHERE project_id=?", (key,))
                    db.execute("DELETE FROM projects WHERE id=?", (key,))
            except BaseException:
                self.recover()
                raise
            pending = False
            try:
                shutil.rmtree(folder)
            except OSError:
                pending = True
            return {
                "deleted": True,
                "bytes": usage["bytes"],
                "cleanup_pending": pending,
            }
