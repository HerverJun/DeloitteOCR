import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ocr_workbench.store import Store, Conflict, SCHEMA_VERSION
from ocr_workbench.agent.store import AgentStore
from ocr_workbench.agent.checkpoints import Checkpoints, restore_backup


class AgentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "workspace")
        self.agent = AgentStore(self.store)
        self.project = self.store.project("测试项目")["id"]
        self.other = self.store.project("另一项目")["id"]
        self.session = self.agent.create_session(self.project, {"client_request_id": "new", "title": "助手"})

    def create_run(self, text="读取页面", request="request"):
        return self.agent.create_run(self.project, self.session["id"], {"client_request_id": request, "content": text},
                                     context={}, config={}, limits={})

    def test_request_idempotency_and_atomic_events(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            runs = list(pool.map(lambda _: self.create_run(), range(12)))
        self.assertEqual(len({r["id"] for r in runs}), 1)
        self.assertEqual([e["seq"] for e in self.agent.events(self.project, self.session["id"])], [1, 2])
        with self.assertRaises(Conflict):
            self.create_run("不同目标")
        with self.assertRaises(Conflict):
            self.create_run(request="second")
        with self.assertRaises(KeyError):
            self.agent.run(self.other, runs[0]["id"])
        with self.assertRaises(KeyError):
            self.agent.events(self.other, self.session["id"])
        with self.assertRaises(Conflict):
            self.agent.create_session(self.project, {"client_request_id": "new", "title": "改名"})

    def test_event_conflict_rolls_back_state_and_sequence(self):
        run = self.create_run()
        self.agent.transition(self.project, run["id"], 1, "running", event_key="state")
        with self.assertRaises(Conflict):
            self.agent.transition(self.project, run["id"], 1, "waiting_user", event_key="state")
        self.assertEqual(self.agent.run(self.project, run["id"])["status"], "running")
        self.assertEqual(len(self.agent.events(self.project, self.session["id"])), 3)

    def test_model_log_unknown_receipt_and_generation(self):
        run = self.create_run()
        first, fresh = self.agent.begin_model_request(self.project, run["id"], 1, 0, "input")
        self.assertTrue(fresh)
        replay, fresh = self.agent.begin_model_request(self.project, run["id"], 1, 0, "input")
        self.assertFalse(fresh)
        self.assertEqual(replay["state"], "sent")
        self.agent.interrupt_on_startup()
        self.assertEqual(self.agent.run(self.project, run["id"])["status"], "interrupted")
        with self.assertRaises(Conflict):
            self.agent.finish_model_request(self.project, run["id"], 1, first["request_id"], {"text": "late"})
        replay, fresh = self.agent.begin_model_request(self.project, run["id"], 2, 0, "input")
        self.assertEqual(replay["state"], "unknown")
        self.assertFalse(fresh)

    def test_received_model_reply_reused(self):
        run = self.create_run()
        first, _ = self.agent.begin_model_request(self.project, run["id"], 1, 0, "input")
        self.agent.finish_model_request(self.project, run["id"], 1, first["request_id"], {"text": "ok"})
        replay, fresh = self.agent.begin_model_request(self.project, run["id"], 1, 0, "input")
        self.assertFalse(fresh)
        self.assertEqual(replay["state"], "received")
        with self.assertRaises(Conflict):
            self.agent.begin_model_request(self.project, run["id"], 1, 0, "changed")

    def test_migration_failure_and_future_rejection(self):
        root = Path(self.temp.name) / "v12"
        root.mkdir()
        connection = sqlite3.connect(root / "workbench.sqlite3")
        connection.row_factory = sqlite3.Row
        with connection:
            for n in range(1, 13):
                Store._migrate(connection, n)
            connection.execute("PRAGMA user_version=12")
        connection.close()
        upgraded = Store(root)
        self.assertEqual(upgraded.schema_version, SCHEMA_VERSION)
        self.assertTrue(upgraded.migration_backup.exists())
        self.assertFalse(upgraded.rows("PRAGMA foreign_key_check"))
        tables = {r["name"] for r in upgraded.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("agent_messages", tables)
        self.assertNotIn("agent_summaries", tables)
        with upgraded.transaction() as db:
            db.execute("PRAGMA user_version=999")
        before = (root / "workbench.sqlite3").read_bytes()
        with self.assertRaises(ValueError):
            Store(root)
        self.assertEqual((root / "workbench.sqlite3").read_bytes(), before)

    def test_backup_half_failure_restore_and_cleanup(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            try:
                from langgraph.graph import StateGraph, START, END
                from langgraph.types import interrupt
                from typing import TypedDict
                class S(TypedDict):
                    value: str
                graph = StateGraph(S)
                graph.add_node("ask", lambda s: {"value": interrupt("继续")})
                graph.add_edge(START, "ask")
                graph.add_edge("ask", END)
                compiled = graph.compile(checkpointer=cp.saver)
                config = {"configurable": {"thread_id": self.session["graph_thread_id"]}}
                async with cp.activity(self.session["graph_thread_id"]):
                    await compiled.ainvoke({"value": "合成数据"}, config)
                bad = Path(self.temp.name) / "bad"
                def fault(index):
                    if index == 0:
                        raise OSError("disk full injection")
                with self.assertRaises(OSError):
                    await cp.backup(bad, fault=fault)
                self.assertFalse(bad.exists())
                good = Path(self.temp.name) / "complete"
                await cp.backup(good)
                restored = Path(self.temp.name) / "restored"
                restore_backup(good, restored)
                restored_cp = await Checkpoints(Store(restored)).open()
                try:
                    self.assertIsNotNone(await restored_cp.saver.aget_tuple(config))
                finally:
                    await restored_cp.close()
                with self.store.transaction() as db:
                    db.execute("DELETE FROM projects WHERE id=?", (self.project,))
                self.assertEqual(len(self.store.rows("SELECT * FROM agent_checkpoint_cleanup")), 1)
                await cp.cleanup_deleted_threads()
                self.assertIsNone(await cp.saver.aget_tuple(config))
                self.assertFalse(self.store.rows("SELECT * FROM agent_checkpoint_cleanup"))
                self.assertEqual(len(self.store.rows("SELECT * FROM projects")), 1)
            finally:
                await cp.close()
        asyncio.run(scenario())

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing errors")
    def test_backup_publish_retries_one_windows_sharing_error(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            try:
                destination = Path(self.temp.name) / "retry-backup"
                replace = os.replace
                calls = []
                def transient(source, target):
                    calls.append((source, target))
                    if len(calls) == 1:
                        raise PermissionError(13, "temporarily busy", str(source), 5)
                    return replace(source, target)
                with patch("ocr_workbench.agent.checkpoints.os.replace", side_effect=transient):
                    manifest = await cp.backup(destination)
                self.assertEqual(len(calls), 2)
                self.assertTrue((destination / "manifest.json").is_file())
                self.assertEqual(set(manifest["files"]), {"workbench.sqlite3", "agent-checkpoints.sqlite3"})
                self.assertFalse(list(Path(self.temp.name).glob("retry-backup.staging-*")))
            finally:
                await cp.close()
        asyncio.run(scenario())

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing errors")
    def test_restore_publish_retries_repeated_windows_sharing_errors(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            try:
                source = Path(self.temp.name) / "retry-source"
                await cp.backup(source)
            finally:
                await cp.close()
            destination = Path(self.temp.name) / "retry-restored"
            replace = os.replace
            calls = []
            def transient(staging, target):
                calls.append((staging, target))
                if len(calls) <= 3:
                    raise PermissionError(13, "temporarily busy", str(staging), 32)
                return replace(staging, target)
            with patch("ocr_workbench.agent.checkpoints.os.replace", side_effect=transient):
                restore_backup(source, destination)
            self.assertEqual(len(calls), 4)
            for name in ("workbench.sqlite3", "agent-checkpoints.sqlite3"):
                self.assertEqual((source / name).read_bytes(), (destination / name).read_bytes())
            self.assertFalse(list(Path(self.temp.name).glob("retry-restored.staging-*")))
        asyncio.run(scenario())

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing errors")
    def test_restore_publish_timeout_raises_original_and_cleans_staging(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            try:
                source = Path(self.temp.name) / "timeout-source"
                await cp.backup(source)
            finally:
                await cp.close()
            destination = Path(self.temp.name) / "timeout-restored"
            error = PermissionError(13, "persistent busy", str(destination), 33)
            with patch("ocr_workbench.agent.checkpoints._PUBLISH_RETRY_SECONDS", 0.12), \
                    patch("ocr_workbench.agent.checkpoints.os.replace", side_effect=error) as replace:
                with self.assertRaises(PermissionError) as caught:
                    restore_backup(source, destination)
            self.assertIs(caught.exception, error)
            self.assertGreaterEqual(replace.call_count, 2)
            self.assertFalse(destination.exists())
            self.assertFalse(list(Path(self.temp.name).glob("timeout-restored.staging-*")))
        asyncio.run(scenario())

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing errors")
    def test_restore_publish_other_permission_error_is_not_retried(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            try:
                source = Path(self.temp.name) / "other-error-source"
                await cp.backup(source)
            finally:
                await cp.close()
            destination = Path(self.temp.name) / "other-error-restored"
            error = PermissionError(13, "not a sharing error", str(destination), 87)
            with patch("ocr_workbench.agent.checkpoints.os.replace", side_effect=error) as replace:
                with self.assertRaises(PermissionError) as caught:
                    restore_backup(source, destination)
            self.assertIs(caught.exception, error)
            self.assertEqual(replace.call_count, 1)
            self.assertFalse(destination.exists())
            self.assertFalse(list(Path(self.temp.name).glob("other-error-restored.staging-*")))
        asyncio.run(scenario())

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing errors")
    def test_restore_publish_destination_race_does_not_overwrite(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            try:
                source = Path(self.temp.name) / "race-source"
                await cp.backup(source)
            finally:
                await cp.close()
            destination = Path(self.temp.name) / "race-restored"
            def competitor(staging, target):
                target.mkdir()
                (target / "other-owner.txt").write_text("keep", encoding="utf-8")
                raise PermissionError(13, "destination became busy", str(staging), 5)
            with patch("ocr_workbench.agent.checkpoints.os.replace", side_effect=competitor) as replace:
                with self.assertRaises(FileExistsError):
                    restore_backup(source, destination)
            self.assertEqual(replace.call_count, 1)
            self.assertEqual((destination / "other-owner.txt").read_text(encoding="utf-8"), "keep")
            self.assertFalse(list(Path(self.temp.name).glob("race-restored.staging-*")))
        asyncio.run(scenario())

    def test_checkpoint_version_rejected(self):
        async def scenario():
            cp = await Checkpoints(self.store).open()
            await cp.conn.execute("UPDATE ocr_agent_format SET value='future' WHERE key='graph'")
            await cp.conn.commit()
            await cp.close()
            with self.assertRaises(ValueError):
                await Checkpoints(self.store).open()
        asyncio.run(scenario())
