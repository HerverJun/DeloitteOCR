import copy
from pathlib import Path
import tempfile
import unittest
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
