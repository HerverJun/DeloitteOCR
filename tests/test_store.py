import copy
from pathlib import Path
import tempfile
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import patch
from ocr_workbench.store import SCHEMA_VERSION, history_decoded
from ocr_workbench.store import Store, Conflict
from ocr_workbench.imaging import add_image
from PIL import Image


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "项目 空格")
        self.project = self.store.project("验收项目")
        source = self.root / "sample.png"
        Image.new("RGB", (80, 50), "white").save(source)
        self.image = add_image(self.store, self.project["id"], "sample.png", source)
        self.raw = {
            "text": "原始 00123456789012345678",
            "tables": [],
            "blocks": [],
            "engine": "ppocr",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def completed(self):
        key = self.store.enqueue(
            self.project["id"], [self.image["active_version"]], ["ppocr"]
        )[0]
        self.assertEqual(self.store.claim()["id"], key)
        self.assertTrue(self.store.complete(key, self.raw))
        return self.store.one("tasks", key)["result_id"]

    def test_edit_history_persists_and_never_changes_original(self):
        key = self.completed()
        result = self.store.save(key, {"text": "人工校对", "tables": []}, 0)
        reopened = Store(self.store.root)
        self.assertEqual(reopened.result(key)["edited"]["text"], "人工校对")
        self.assertEqual(reopened.result(key)["original"], self.raw)
        undone = reopened.history(key, -1, result["revision"])
        self.assertEqual(undone["edited"]["text"], self.raw["text"])
        redone = reopened.history(key, 1, undone["revision"])
        self.assertEqual(redone["edited"]["text"], "人工校对")

    def test_conflict_and_branching_undo_history(self):
        key = self.completed()
        first = self.store.save(key, {"text": "第一次", "tables": []}, 0)
        with self.assertRaises(Conflict):
            self.store.save(key, {"text": "过期写入", "tables": []}, 0)
        back = self.store.history(key, -1, first["revision"])
        branch = self.store.save(
            key, {"text": "另一分支", "tables": []}, back["revision"]
        )
        self.assertFalse(branch["can_redo"])
        with self.assertRaises(ValueError):
            self.store.history(key, 1, branch["revision"])

    def test_recovery_requires_resume_and_preserves_success(self):
        complete = self.completed()
        ids = self.store.enqueue(
            self.project["id"], [self.image["active_version"]], ["paddlevl", "glm"]
        )
        running = self.store.claim()["id"]
        self.store.recover()
        states = {key: self.store.one("tasks", key)["status"] for key in ids}
        self.assertEqual(states[running], "interrupted")
        self.assertEqual(set(states.values()), {"interrupted", "paused"})
        self.assertEqual(self.store.result(complete)["original"], self.raw)
        self.assertIsNone(self.store.claim())

    def test_cancelled_task_cannot_commit_late_result(self):
        task = self.store.enqueue(
            self.project["id"], [self.image["active_version"]], ["ppocr"]
        )[0]
        self.store.claim()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
        self.assertFalse(self.store.complete(task, self.raw))
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])

    def test_cross_project_version_and_bad_table_rejected_atomically(self):
        other = self.store.project("其他项目")
        with self.assertRaises(ValueError):
            self.store.enqueue(other["id"], [self.image["active_version"]], ["ppocr"])
        self.assertEqual(self.store.rows("SELECT * FROM tasks"), [])
        result = self.completed()
        invalid = {
            "text": "x",
            "tables": [
                {
                    "rows": 1,
                    "columns": 1,
                    "cells": [
                        {
                            "row": 0,
                            "column": 0,
                            "row_span": 1,
                            "column_span": 2,
                            "text": "x",
                        }
                    ],
                }
            ],
        }
        with self.assertRaises(ValueError):
            self.store.save(result, invalid, 0)
        self.assertEqual(self.store.result(result)["revision"], 0)

    def test_batch_groups_by_engine_and_duplicate_complete_is_idempotent(self):
        ids = self.store.enqueue(
            self.project["id"],
            [self.image["active_version"]],
            ["hunyuan", "ppocr", "glm", "paddlevl"],
        )
        seen = []
        while task := self.store.claim():
            seen.append(task["engine"])
            self.store.complete(task["id"], self.raw)
            self.assertFalse(self.store.complete(task["id"], self.raw))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 4)

    def test_future_database_rejected_without_modifying_any_bytes(self):
        path = self.store.root / "workbench.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=99")
            db.commit()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "高于本程序支持"):
            Store(self.store.root)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def legacy_v4(self):
        key = self.completed()
        self.store.save(key, {"text": "旧版人工校对", "tables": []}, 0)
        with self.store.transaction() as db:
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall():
                db.execute('DROP TRIGGER "' + row["name"] + '"')
            db.execute("DROP TABLE reviews")
            db.execute("DROP TABLE project_revisions")
            for table in ("fusion_decisions", "fusion_issues", "fusion_progress", "fusion_evidence",
                          "fusion_inputs", "fusion_dependencies", "fusion_submissions"):
                db.execute("DROP TABLE " + table)
            db.execute("ALTER TABLE tasks DROP COLUMN fusion_config")
            for row in db.execute(
                "SELECT result_id,position,value FROM edits"
            ).fetchall():
                db.execute(
                    "UPDATE edits SET value=? WHERE result_id=? AND position=?",
                    (history_decoded(row["value"]), row["result_id"], row["position"]),
                )
            db.execute("PRAGMA user_version=4")
        return key

    def test_v4_migration_keeps_consistent_backup_and_all_history(self):
        key = self.legacy_v4()
        migrated = Store(self.store.root)
        backups = list((self.store.root / "database-backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                db.execute("SELECT typeof(value) FROM edits LIMIT 1").fetchone()[0],
                "text",
            )
        self.assertEqual(
            migrated.rows("PRAGMA user_version")[0]["user_version"], SCHEMA_VERSION
        )
        edited = migrated.result(key)
        undone = migrated.history(key, -1, edited["revision"])
        self.assertEqual(undone["edited"]["text"], self.raw["text"])
        self.assertEqual(
            migrated.history(key, 1, undone["revision"])["edited"]["text"],
            "旧版人工校对",
        )
        Store(self.store.root)
        self.assertEqual(
            len(list((self.store.root / "database-backups").glob("*.sqlite3"))), 1
        )

    def test_failed_migration_rolls_back_schema_and_version(self):
        self.legacy_v4()
        migrate = Store._migrate

        def fail(db, version):
            if version == 6:
                raise OSError("simulated migration failure")
            migrate(db, version)

        with patch.object(Store, "_migrate", staticmethod(fail)):
            with self.assertRaises(OSError):
                Store(self.store.root)
        self.assertEqual(self.store.rows("PRAGMA user_version")[0]["user_version"], 4)
        self.assertEqual(
            self.store.rows(
                "SELECT name FROM sqlite_master WHERE name='project_revisions'"
            ),
            [],
        )
        self.assertEqual(
            len(list((self.store.root / "database-backups").glob("*.sqlite3"))), 1
        )

    def test_compressed_long_history_can_undo_and_redo_every_edit_after_restart(self):
        self.raw["text"] = "长文档与编号001234 " * 7000
        key = self.completed()
        expected = [self.raw["text"]]
        result = self.store.result(key)
        for index in range(50):
            expected.append(self.raw["text"] + str(index))
            result = self.store.save(
                key, {"text": expected[-1], "tables": []}, result["revision"]
            )
        history_bytes = self.store.rows("SELECT SUM(length(value)) bytes FROM edits")[
            0
        ]["bytes"]
        self.assertLess(history_bytes, len(self.raw["text"].encode("utf-8")))
        reopened = Store(self.store.root)
        for value in reversed(expected[:-1]):
            result = reopened.history(key, -1, result["revision"])
            self.assertEqual(result["edited"]["text"], value)
        for value in expected[1:]:
            result = reopened.history(key, 1, result["revision"])
            self.assertEqual(result["edited"]["text"], value)

    def test_project_revision_tracks_external_writes_with_query_indexes(self):
        other = self.store.project("其他")
        initial = self.store.project_revision(self.project["id"])
        key = self.completed()
        self.assertGreater(self.store.project_revision(self.project["id"]), initial)
        initial = self.store.project_revision(self.project["id"])
        with self.store.transaction() as db:
            db.execute(
                "UPDATE selections SET result_id=? WHERE image_id=?",
                (key, self.image["id"]),
            )
        self.assertGreater(self.store.project_revision(self.project["id"]), initial)
        self.assertEqual(self.store.project_revision(other["id"]), 0)
        for table in ("images", "tasks"):
            plan = self.store.rows(
                f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE project_id=? ORDER BY created",
                (self.project["id"],),
            )
            self.assertTrue(
                any(table + "_project_created" in row["detail"] for row in plan)
            )

    def test_review_is_bound_to_adopted_result_revision_and_image_version(self):
        key = self.completed()
        photo, version = self.image["id"], self.image["active_version"]
        initial = self.store.project_revision(self.project["id"])
        self.store.set_review(photo, "confirmed", key, 0, version)
        self.assertGreater(self.store.project_revision(self.project["id"]), initial)
        self.assertEqual(
            self.store.review_states(self.project["id"])[photo]["status"], "confirmed"
        )
        self.store.save(key, {"text": "重新校对", "tables": []}, 0)
        state = Store(self.store.root).review_states(self.project["id"])[photo]
        self.assertEqual(state["status"], "pending")
        self.assertTrue(state["stale"])
        self.assertEqual(
            self.store.rows("SELECT status FROM reviews")[0]["status"], "confirmed"
        )
        with self.assertRaises(Conflict):
            self.store.set_review(photo, "confirmed", key, 0, version)
        self.store.set_review(photo, "question", key, 1, version)
        self.assertEqual(
            self.store.review_states(self.project["id"])[photo]["status"], "question"
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE images SET active_version='changed' WHERE id=?", (photo,)
            )
        self.assertEqual(
            self.store.review_states(self.project["id"])[photo]["status"], "pending"
        )

    def test_disk_full_rollback_has_actionable_save_error(self):
        before = self.store.one("projects", self.project["id"])["name"]
        with self.assertRaisesRegex(ValueError, "保留当前校对草稿"):
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE projects SET name='not committed' WHERE id=?",
                    (self.project["id"],),
                )
                raise sqlite3.OperationalError("database or disk is full")
        self.assertEqual(self.store.one("projects", self.project["id"])["name"], before)
