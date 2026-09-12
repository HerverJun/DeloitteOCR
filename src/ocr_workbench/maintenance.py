"""Project storage accounting and recoverable, project-scoped cleanup."""

import json
import re
import shutil
import os
from pathlib import Path
from ocr_workbench.store import uid, now
from ocr_workbench.atomic_files import write_json


class ProjectMaintenance:
    def __init__(self, store, queue, fusion_queue=None):
        self.fusion_queue = fusion_queue
        self.store, self.queue = store, queue
        self.guard = store.file_lock
        self.trash = store.root / "cleanup"
        self.trash.mkdir(exist_ok=True)
        self.recover()
        # Only inventory crash leftovers; never silently delete user files.
        self.orphan_report = self.orphans()

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
                for file in self._files(path):
                    total += file.stat().st_size
                    files += 1
            result_bytes = self.store.rows(
                "SELECT COALESCE(SUM(length(CAST(r.original AS BLOB))+length(CAST(r.edited AS BLOB))),0) bytes FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.project_id=?",
                (key,),
            )[0]["bytes"]
            history_bytes = self.store.rows(
                "SELECT COALESCE(SUM(length(CAST(e.value AS BLOB))),0) bytes,COUNT(*) count FROM edits e JOIN results r ON r.id=e.result_id JOIN tasks t ON t.id=r.task_id WHERE t.project_id=?",
                (key,),
            )[0]
            fusion_bytes = 0
            for table, columns, owner in (
                ('fusion_inputs', ('snapshot', 'fingerprint'), 'task_id'),
                ('fusion_dependencies', ('parent_task_id',), 'task_id'),
                ('fusion_evidence', ('definition',), 'result_id'),
                ('fusion_issues', ('definition', 'target', 'current_value', 'basis'), 'result_id'),
                ('fusion_decisions', ('response', 'previous', 'payload_hash'), 'result_id'),
                ('fusion_progress', ('issue_id', 'updated'), 'result_id'),
            ):
                expression = '+'.join(f'COALESCE(length(CAST(f.{column} AS BLOB)),0)' for column in columns)
                joins = 'JOIN tasks t ON t.id=f.task_id' if owner == 'task_id' else 'JOIN results r ON r.id=f.result_id JOIN tasks t ON t.id=r.task_id'
                fusion_bytes += self.store.rows(f'SELECT COALESCE(SUM({expression}),0) bytes FROM {table} f {joins} WHERE t.project_id=?', (key,))[0]['bytes']
            fusion_bytes += self.store.rows('SELECT COALESCE(SUM(length(request_id)+length(payload_hash)+length(task_ids)+length(created)),0) bytes FROM fusion_submissions WHERE project_id=?', (key,))[0]['bytes']
            return {
                "bytes": total + result_bytes + history_bytes["bytes"] + fusion_bytes,
                "file_bytes": total,
                "database_payload_bytes": result_bytes + history_bytes["bytes"] + fusion_bytes,
                "fusion_bytes": fusion_bytes,
                "result_bytes": result_bytes,
                "history_bytes": history_bytes["bytes"],
                "history_entries": history_bytes["count"],
                "files": files,
                "scope": "项目文件、结果、融合快照/证据/决策及压缩撤销历史的逻辑字节数；共享数据库实际占用另列，含其他项目、索引和空闲页，不能按项目精确分摊；删除后数据库文件未必立即缩小",
                **self.disk_status(),
            }

    def disk_status(self):
        disk = shutil.disk_usage(self.store.root)

        def size(path):
            try:
                return path.stat().st_size
            except FileNotFoundError:
                # A closed SQLite connection may checkpoint and unlink its WAL.
                return 0

        database = sum(
            size(path)
            for path in (
                self.store.root / "workbench.sqlite3",
                self.store.root / "workbench.sqlite3-wal",
                self.store.root / "workbench.sqlite3-shm",
            )
        )
        backups = self.store.root / "database-backups"
        return {
            "workspace_database_bytes": database,
            "migration_backup_bytes": sum(
                p.stat().st_size for p in backups.glob("*.sqlite3") if p.is_file()
            ),
            "free_bytes": disk.free,
            "low_space": disk.free < 2 * 1024**3,
            "warning": (
                "项目磁盘剩余空间不足 2 GB，请先导出重要结果并清理磁盘或迁移工作区；撤销历史仍保留"
                if disk.free < 2 * 1024**3
                else None
            ),
        }

    @staticmethod
    def _files(root):
        """Walk regular managed paths without following Windows reparse points."""
        if not root.exists() or root.is_symlink() or root.is_junction():
            return
        if root.is_file():
            yield root
            return
        stack = [root]
        while stack:
            for entry in os.scandir(stack.pop()):
                path = Path(entry.path)
                if path.is_symlink() or path.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path

    def orphans(self):
        with self.guard:
            referenced = {
                self.store.file(row["path"])
                for row in self.store.rows(
                    "SELECT original_path path FROM images UNION SELECT path FROM versions"
                )
            }
            entries = []
            staging = self.store.root / ".image-staging"
            if (
                staging.is_dir()
                and not staging.is_symlink()
                and not staging.is_junction()
            ):
                for folder in staging.iterdir():
                    if (
                        re.fullmatch("[a-f0-9]{32}", folder.name)
                        and folder.is_dir()
                        and not folder.is_symlink()
                        and not folder.is_junction()
                    ):
                        entries.append(
                            {
                                "path": folder.relative_to(self.store.root).as_posix(),
                                "bytes": sum(
                                    p.stat().st_size for p in self._files(folder)
                                ),
                                "reason": "未完成的图像暂存",
                            }
                        )
            for path in self._files(self.store.root / "projects"):
                parts = path.relative_to(self.store.root).parts
                if (
                    len(parts) == 5
                    and parts[2] == "images"
                    and re.fullmatch("[a-f0-9]{32}", parts[1])
                    and re.fullmatch("[a-f0-9]{32}", parts[3])
                    and (
                        re.fullmatch(r"[a-f0-9]{32}\.png", parts[4])
                        or parts[4].startswith("original.")
                    )
                    and path.resolve() not in referenced
                ):
                    entries.append(
                        {
                            "path": path.relative_to(self.store.root).as_posix(),
                            "bytes": path.stat().st_size,
                            "reason": "没有图片或版本记录引用",
                        }
                    )
            return {
                "files": entries,
                "bytes": sum(entry["bytes"] for entry in entries),
                "count": len(entries),
                "scanned_at": now(),
                "policy": "仅列出；经明确选择后移入隔离目录并保留恢复清单",
            }

    def quarantine_orphans(self, paths):
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > 1000
            or any(not isinstance(p, str) for p in paths)
        ):
            raise ValueError("请选择最多 1000 个待隔离的孤儿路径")
        with self.guard, self.store.lock:
            available = {item["path"]: item for item in self.orphans()["files"]}
            chosen = list(dict.fromkeys(paths))
            if any(path not in available for path in chosen):
                raise ValueError("孤儿清单已变化或路径仍被使用，请重新扫描后选择")
            folder = self.store.root / "orphan-quarantine" / uid()
            folder.mkdir(parents=True)
            ledger = {
                "created": now(),
                "paths": chosen,
                "files": [available[p] for p in chosen],
            }
            write_json(folder / "ledger.json", ledger)
            moved = []
            try:
                for index, relative in enumerate(chosen):
                    original = self.store.file(relative)
                    original.replace(folder / str(index))
                    moved.append((index, original))
            except BaseException:
                for index, original in reversed(moved):
                    (folder / str(index)).replace(original)
                raise
            self.orphan_report = self.orphans()
            return {
                "quarantined": len(chosen),
                "bytes": sum(available[p]["bytes"] for p in chosen),
                "directory": str(folder),
                "ledger": str(folder / "ledger.json"),
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
            if self.queue.status()["task_id"] in task_ids or (self.fusion_queue and self.fusion_queue.status()["task_id"] in task_ids):
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
