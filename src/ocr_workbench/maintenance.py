"""Project storage accounting and recoverable, project-scoped cleanup."""

import json
import re
import shutil
import os
import time
from pathlib import Path
from ocr_workbench.store import uid, now
from ocr_workbench.atomic_files import write_json


class ProjectMaintenance:
    def __init__(self, store, queue, fusion_queue=None, external_queue=None):
        self.fusion_queue = fusion_queue
        self.external_queue = external_queue
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
        paths.append(self.store.root / "agent-artifacts" / key)
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
            or (len(parts) == 2 and parts[0] == "agent-artifacts" and parts[1] == key)
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
            document_bytes = 0
            for table, columns, joins, owner in (
                ('documents', ('name','original_path','metadata'), '', 'f.project_id'),
                ('pages', ('crop_box','render_parameters','native_result'), 'JOIN documents d ON d.id=f.document_id', 'd.project_id'),
                ('page_versions', ('pdf_to_pixel','parent_to_pixel'), 'JOIN pages p ON p.id=f.page_id JOIN documents d ON d.id=p.document_id', 'd.project_id'),
                ('regions', ('polygon','pdf_polygon','metadata'), 'JOIN pages p ON p.id=f.page_id JOIN documents d ON d.id=p.document_id', 'd.project_id'),
                ('document_stages', ('request_key','parameters','error','output'), 'JOIN pages p ON p.id=f.page_id JOIN documents d ON d.id=p.document_id', 'd.project_id'),
                ('geometry_evidence', ('target','polygon','details','model_version'), 'JOIN results r ON r.id=f.result_id JOIN tasks t ON t.id=r.task_id', 't.project_id'),
                ('geometry_requests', ('snapshot','regions'), 'JOIN tasks t ON t.id=f.task_id', 't.project_id'),
                ('page_ocr_inputs', ('crop_box','output'), 'JOIN tasks t ON t.id=f.task_id', 't.project_id'),
                ('document_conflict_decisions', ('conflict_id','edited_sha256'), 'JOIN results r ON r.id=f.result_id JOIN tasks t ON t.id=r.task_id', 't.project_id'),
                ('review_timings', ('target','active_ms','action'), 'JOIN results r ON r.id=f.result_id JOIN tasks t ON t.id=r.task_id', 't.project_id'),
            ):
                expression = '+'.join(f'COALESCE(length(CAST(f.{column} AS BLOB)),0)' for column in columns)
                document_bytes += self.store.rows(f'SELECT COALESCE(SUM({expression}),0) bytes FROM {table} f {joins} WHERE {owner}=?', (key,))[0]['bytes']
            agent_tables = self.agent_usage(key)
            agent_bytes = sum(item['bytes'] for item in agent_tables.values())
            return {
                "bytes": total + result_bytes + history_bytes["bytes"] + fusion_bytes + document_bytes + agent_bytes,
                "file_bytes": total,
                "database_payload_bytes": result_bytes + history_bytes["bytes"] + fusion_bytes + document_bytes + agent_bytes,
                "agent_database_payload_bytes": agent_bytes,
                "agent_sessions": agent_tables['agent_sessions']['rows'],
                "agent_artifacts": agent_tables['agent_artifacts']['rows'],
                "agent_tables": agent_tables,
                "document_bytes": document_bytes,
                "fusion_bytes": fusion_bytes,
                "result_bytes": result_bytes,
                "history_bytes": history_bytes["bytes"],
                "history_entries": history_bytes["count"],
                "files": files,
                "scope": "项目文件、助手产物、结果、融合与文档快照/证据/决策/计时、压缩撤销历史及助手业务表的逻辑字节数；助手表按各字段的字节表示统计，共享凭据不计入项目；共享业务库与 checkpoint 库实际占用另列，含其他项目、索引和空闲页，不能按项目精确分摊；删除后数据库文件未必立即缩小",
                **self.disk_status(),
            }

    def agent_usage(self, key):
        """Count project-owned business rows, never apportion the shared saver DB."""
        owned = {
            'agent_sessions': ('', 'f.project_id'),
            'agent_operations': ('', 'f.project_id'),
            'agent_grants': ('', 'f.project_id'),
            'agent_artifacts': ('', 'f.project_id'),
            'agent_selections': ('', 'f.project_id'),
            'agent_checkpoint_cleanup': ('', 'f.project_id'),
        }
        for table in ('agent_runs', 'agent_events', 'agent_mutations'):
            owned[table] = ('JOIN agent_sessions s ON s.id=f.session_id', 's.project_id')
        for table in ('agent_model_requests', 'agent_calls', 'agent_decisions',
                      'agent_resume_intents', 'agent_inbox', 'agent_evidence'):
            owned[table] = ('JOIN agent_runs r ON r.id=f.run_id JOIN agent_sessions s ON s.id=r.session_id', 's.project_id')
        owned['agent_job_links'] = ('JOIN agent_operations o ON o.id=f.operation_id', 'o.project_id')
        owned['agent_artifact_leases'] = ('JOIN agent_artifacts a ON a.id=f.artifact_id', 'a.project_id')
        result = {}
        with self.store.transaction() as db:
            for table, (joins, owner) in owned.items():
                # Table names come from this fixed schema allowlist, not requests.
                columns = [row['name'] for row in db.execute(f'PRAGMA table_info({table})')]
                expression = '+'.join(f'COALESCE(length(CAST(f."{column}" AS BLOB)),0)' for column in columns)
                row = db.execute(f'SELECT COUNT(*) rows,COALESCE(SUM({expression}),0) bytes FROM {table} f {joins} WHERE {owner}=?', (key,)).fetchone()
                result[table] = dict(row)
        return result

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
                self.store.root / "agent-checkpoints.sqlite3",
                self.store.root / "agent-checkpoints.sqlite3-wal",
                self.store.root / "agent-checkpoints.sqlite3-shm",
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
                    "SELECT original_path path FROM images UNION SELECT path FROM versions UNION SELECT original_path path FROM documents UNION SELECT native_result path FROM pages WHERE native_result IS NOT NULL"
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
                    and parts[2] in ("images", "documents")
                    and re.fullmatch("[a-f0-9]{32}", parts[1])
                    and re.fullmatch("[a-f0-9]{32}", parts[3])
                    and (
                        re.fullmatch(r"[a-f0-9]{32}\.png", parts[4])
                        or parts[4].startswith("original.")
                        or parts[4] == "native.json"
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
            artifacts = self.store.root / 'agent-artifacts'
            known = {(row['project_id'], row['id']): row['status'] for row in self.store.rows(
                'SELECT a.project_id,a.id,r.status FROM agent_artifacts a JOIN agent_runs r ON r.id=a.run_id')}
            if artifacts.is_dir() and not artifacts.is_symlink() and not artifacts.is_junction():
                for project in artifacts.iterdir():
                    if not re.fullmatch('[a-f0-9]{32}', project.name) or not project.is_dir() or project.is_symlink() or project.is_junction():
                        continue
                    for folder in project.iterdir():
                        match = re.fullmatch(r'([a-f0-9]{32})(\.staging-[a-f0-9]{32})?', folder.name)
                        if not match or not folder.is_dir() or folder.is_symlink() or folder.is_junction():
                            continue
                        status = known.get((project.name, match[1]))
                        # The artifact lifecycle owns published/known directories.
                        # Preserve temporary work belonging to an unfinished run.
                        if status is not None and (not match[2] or status not in {'completed', 'failed', 'cancelled'}):
                            continue
                        entries.append({'path': folder.relative_to(self.store.root).as_posix(),
                                        'bytes': sum(p.stat().st_size for p in self._files(folder)),
                                        'reason': '未完成的助手产物暂存' if match[2] else '没有助手产物记录引用'})
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
            if any(worker and worker.status()['task_id'] in task_ids for worker in (self.queue, self.fusion_queue, self.external_queue)):
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
                    if db.execute("""SELECT 1 FROM agent_runs r JOIN agent_sessions s ON s.id=r.session_id
                        WHERE s.project_id=? AND r.status NOT IN ('completed','failed','cancelled')""", (key,)).fetchone():
                        raise ValueError("请先停止该项目的助手运行，再删除项目")
                    if db.execute("""SELECT 1 FROM agent_artifact_leases l JOIN agent_artifacts a ON a.id=l.artifact_id
                        WHERE a.project_id=? AND l.expires>?""", (key, time.time())).fetchone():
                        raise ValueError("项目导出产物正在下载，请等待下载结束")
                    if db.execute("SELECT 1 FROM document_stages s JOIN pages p ON p.id=s.page_id JOIN documents d ON d.id=p.document_id WHERE d.project_id=? AND s.status IN ('queued','running','waiting_gpu')", (key,)).fetchone():
                        raise ValueError("请先暂停或取消该项目的文档处理任务")
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

    def stop_agent_runs(self, key, confirmation):
        """Fence one project's runs before cleanup, without cancelling OCR jobs.

        A live runtime also cancels and drains its associated async tasks. The
        same fencing remains available when the optional runtime is disabled.
        """
        from ocr_workbench.agent.store import AgentStore
        agent = AgentStore(self.store)
        with self.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            project = db.execute('SELECT name FROM projects WHERE id=?', (key,)).fetchone()
            if not project:
                raise KeyError('项目不存在')
            if confirmation != project['name']:
                raise ValueError('请输入完整项目名称以确认清理')
            rows = db.execute('''SELECT r.*,s.graph_thread_id FROM agent_runs r
                JOIN agent_sessions s ON s.id=r.session_id WHERE s.project_id=?''', (key,)).fetchall()
            for row in rows:
                if row['status'] in {'completed', 'failed', 'cancelled'}:
                    continue
                generation = row['generation'] + 1
                db.execute("UPDATE agent_runs SET status='cancelled',generation=?,fencing_token=fencing_token+1,owner=NULL,lease_expires=NULL,updated=? WHERE id=?",
                           (generation, now(), row['id']))
                agent.discard_inbox(db, row['id'])
                db.execute("UPDATE agent_model_requests SET state='unknown' WHERE run_id=? AND state='sent'", (row['id'],))
                db.execute("UPDATE agent_resume_intents SET state='obsolete' WHERE run_id=? AND state IN ('pending','claimed')", (row['id'],))
                db.execute("UPDATE agent_decisions SET status='obsolete' WHERE run_id=? AND status='pending'", (row['id'],))
                agent._event(db, row['session_id'], row['id'], generation,
                             f"{row['id']}:{generation}:project-cleanup", 'run_state',
                             {'status': 'cancelled', 'mode': 'stop_agent', 'reason': 'project_cleanup'})
            return [dict(row) for row in rows]

    def delete_without_runtime(self, key, confirmation):
        self.stop_agent_runs(key, confirmation)
        result = self.delete(key, confirmation)
        result['checkpoint_cleanup_pending'] = bool(self.store.rows(
            'SELECT 1 FROM agent_checkpoint_cleanup WHERE project_id=? LIMIT 1', (key,)))
        return result
