"""Official saver lifecycle and complete, quiescent two-database backups."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

GRAPH_VERSION = "ocr-agent-graph-v1"
STATE_VERSION = 1
SERIALIZER_VERSION = "strict-jsonplus-v1"
# Retire completed graph histories at a run boundary. SQLite reuses freed pages;
# this is a live-payload limit, not an assertion about the file high-water mark.
THREAD_CHECKPOINT_LIMIT = 96
THREAD_PAYLOAD_LIMIT = 8 * 1024 * 1024
_PUBLISH_RETRY_SECONDS = 2.0
_PUBLISH_RETRY_INTERVAL = 0.05


def _publish_directory(staging, destination):
    """Publish a new directory after brief Windows sharing violations clear."""
    deadline = time.monotonic() + _PUBLISH_RETRY_SECONDS if os.name == "nt" else None
    while True:
        if os.path.lexists(destination):
            raise FileExistsError(f"备份/恢复目标已存在：{destination}")
        try:
            os.replace(staging, destination)
            return
        except PermissionError as error:
            if os.path.lexists(destination):
                raise FileExistsError(f"备份/恢复目标已存在：{destination}") from error
            if deadline is None or error.winerror not in (5, 32, 33):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_PUBLISH_RETRY_INTERVAL, remaining))


def disable_tracing():
    # A local workbench must not inherit a developer's cloud tracing settings.
    for key in tuple(os.environ):
        if key.startswith(("LANGSMITH_", "LANGCHAIN_")):
            del os.environ[key]
    os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false", LANGGRAPH_STRICT_MSGPACK="true")


class Checkpoints:
    def __init__(self, business):
        self.business = business
        self.path = business.root / "agent-checkpoints.sqlite3"
        self.condition = asyncio.Condition()
        self.maintenance_lock = asyncio.Lock()
        self.active = 0
        self.blocked = False
        self.closed = True
        self.thread_locks = {}

    async def open(self):
        expected = {"graph": GRAPH_VERSION, "state": str(STATE_VERSION), "serializer": SERIALIZER_VERSION}
        if self.path.exists():
            # Reject incompatible/unowned stores before saver setup or WAL writes.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and ("ocr_agent_format" not in tables or dict(db.execute("SELECT key,value FROM ocr_agent_format")) != expected):
                    raise ValueError("助手 checkpoint 版本不兼容；保留文件并核对旧任务后新建会话")
        disable_tracing()
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        if not self.closed:
            raise RuntimeError("Saver already open")
        self.conn = await aiosqlite.connect(self.path)
        try:
            self.saver = AsyncSqliteSaver(self.conn, serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None))
            await self.saver.setup()
            await self.conn.execute("CREATE TABLE IF NOT EXISTS ocr_agent_format(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            async with self.conn.execute("SELECT key,value FROM ocr_agent_format") as cursor:
                existing = dict(await cursor.fetchall())
            if existing and existing != expected:
                raise ValueError("助手 checkpoint 版本不兼容；保留文件并核对旧任务后新建会话")
            await self.conn.executemany("INSERT OR IGNORE INTO ocr_agent_format VALUES(?,?)", expected.items())
            await self.conn.commit()
            self.closed = False
            await self.cleanup_deleted_threads()
            return self
        except BaseException:
            await self.conn.close()
            raise

    @asynccontextmanager
    async def activity(self, thread_id):
        # No SQLite transaction or business/file/GPU lock survives this scope.
        async with self.condition:
            await self.condition.wait_for(lambda: not self.blocked or self.closed)
            if self.closed:
                raise RuntimeError("Saver closed")
            self.active += 1
            entry = self.thread_locks.setdefault(thread_id, [asyncio.Lock(), 0])
            entry[1] += 1
        try:
            async with entry[0]:
                yield self.saver
        finally:
            async with self.condition:
                entry[1] -= 1
                if not entry[1]:
                    del self.thread_locks[thread_id]
                self.active -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def quiesce(self):
        async with self.maintenance_lock:
            async with self.condition:
                self.blocked = True
            try:
                async with self.condition:
                    await self.condition.wait_for(lambda: self.active == 0)
                yield
            finally:
                async with self.condition:
                    self.blocked = False
                    self.condition.notify_all()

    async def close(self):
        async with self.quiesce():
            if not self.closed:
                self.closed = True
                await self.conn.close()

    async def cleanup_deleted_threads(self):
        # DELETE tombstone committed with business cascade; retries are harmless.
        async with self.quiesce():
            for row in self.business.rows("SELECT thread_id FROM agent_checkpoint_cleanup"):
                await self.saver.adelete_thread(row["thread_id"])
                with self.business.transaction() as db:
                    db.execute("DELETE FROM agent_checkpoint_cleanup WHERE thread_id=?", (row["thread_id"],))

    async def thread_size(self, thread_id):
        # Read only the official saver's public SQLite layout. All writes and
        # deletion continue to go through AsyncSqliteSaver.
        async with self.conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(checkpoint)),0) FROM checkpoints WHERE thread_id=?",
            (thread_id,),
        ) as cursor:
            count, payload = await cursor.fetchone()
        return count, payload

    async def drain_retired_thread(self, thread_id, project_id):
        # The business switch and tombstone commit together; failure here is
        # retried on startup or the next completed run.
        await self.saver.adelete_thread(thread_id)
        with self.business.transaction() as db:
            db.execute("DELETE FROM agent_checkpoint_cleanup WHERE thread_id=? AND project_id=?", (thread_id, project_id))

    async def cleanup_project_threads(self, project_id):
        """Drain deleted threads independently of other projects' live graphs."""
        for row in self.business.rows('SELECT thread_id FROM agent_checkpoint_cleanup WHERE project_id=?', (project_id,)):
            async with self.activity(row['thread_id']):
                await self.saver.adelete_thread(row['thread_id'])
                with self.business.transaction() as db:
                    db.execute('DELETE FROM agent_checkpoint_cleanup WHERE thread_id=? AND project_id=?', (row['thread_id'], project_id))

    async def backup(self, destination, *, fault=None):
        """Publish one complete set. Destination is service-selected, never model input.

        Quiesce graph execution, then acquire file_lock -> Store.lock. The backup
        API includes WAL data. A half-written staging directory is never published.
        """
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("备份目标已存在")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / (destination.name + ".staging-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            async with self.quiesce():
                with self.business.file_lock, self.business.lock:
                    manifest = {"graph": GRAPH_VERSION, "state": STATE_VERSION, "serializer": SERIALIZER_VERSION, "files": {}}
                    for index, name in enumerate(("workbench.sqlite3", "agent-checkpoints.sqlite3")):
                        with closing(sqlite3.connect((self.business.root / name).as_uri() + "?mode=ro", uri=True)) as source:
                            with closing(sqlite3.connect(staging / name)) as target:
                                source.backup(target)
                                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                                    raise ValueError("备份完整性检查失败")
                        manifest["files"][name] = hashlib.sha256((staging / name).read_bytes()).hexdigest()
                        if fault:
                            fault(index)
                    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
                    _publish_directory(staging, destination)
            return manifest
        except BaseException:
            shutil.rmtree(staging)
            raise


def restore_backup(source, destination):
    """Offline restore into a NEW workspace; never overwrite a running database."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("恢复必须使用新的空目录")
    manifest = json.loads((source / "manifest.json").read_text("utf-8"))
    if (manifest.get("graph"), manifest.get("state"), manifest.get("serializer")) != (GRAPH_VERSION, STATE_VERSION, SERIALIZER_VERSION):
        raise ValueError("备份版本不兼容")
    names = {"workbench.sqlite3", "agent-checkpoints.sqlite3"}
    if set(manifest.get("files", {})) != names:
        raise ValueError("两库备份集合不完整")
    for name in names:
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != manifest["files"][name]:
            raise ValueError("备份哈希不匹配")
    staging = destination.parent / (destination.name + ".staging-" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    try:
        for name in names:
            shutil.copy2(source / name, staging / name)
        _publish_directory(staging, destination)
    except BaseException:
        shutil.rmtree(staging)
        raise
