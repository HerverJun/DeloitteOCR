import copy
from pathlib import Path
import tempfile
import unittest
import sqlite3
import shutil
from contextlib import closing
from unittest.mock import patch
from ocr_workbench.store import SCHEMA_VERSION, history_decoded
from ocr_workbench.store import Store, Conflict
from ocr_workbench.imaging import add_image
from PIL import Image


def drop_post_v15_tables(db):
    # Build a real legacy schema rather than retaining later-version tables.
    for table in ('screen_cache', 'screen_contexts', 'screen_actions',
                  'screen_final_tasks', 'screen_tasks', 'screen_items',
                  'screen_batches', 'platform_page_requests'):
        db.execute('DROP TABLE IF EXISTS ' + table)


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

    def adoption_body(self, key, revision):
        result = self.store.result_snapshot(key, revision)
        return {"project_id": self.project["id"], "instance_id": "instanceA", "result_id": key,
                "task_id": result["task_id"], "image_id": self.image["id"],
                "version_id": self.image["active_version"], "revision": revision,
                "original_sha256": self.image["sha256"],
                "actor": {"id": "localUser", "display_name": "本机编制人", "source": "local"},
                "note": "采用明确校对版本"}

    def test_monotonic_snapshots_survive_undo_branch_and_restart(self):
        key = self.completed()
        self.store.save(key, {"text": "revision 1", "tables": []}, 0)
        self.store.save(key, {"text": "revision 2", "tables": []}, 1)
        self.store.history(key, -1, 2)
        self.store.save(key, {"text": "new branch", "tables": []}, 3)
        reopened = Store(self.store.root)
        expected = [self.raw["text"], "revision 1", "revision 2", "revision 1", "new branch"]
        for revision, text in enumerate(expected):
            value = reopened.result_snapshot(key, revision)
            self.assertEqual(value["edited"]["text"], text)
            self.assertEqual(value["original"], self.raw)
            self.assertEqual(value["revision"], revision)
        page = reopened.result_history(key, 3, 2)
        self.assertEqual([item["revision"] for item in page["items"]], [3, 4])
        self.assertEqual((page["total"], page["unavailable_revisions"]), (5, 0))
        with self.assertRaises(sqlite3.IntegrityError):
            with reopened.transaction() as db:
                db.execute("UPDATE result_revisions SET edited=? WHERE result_id=? AND revision=2", (b"changed", key))
        self.assertEqual(reopened.result_snapshot(key, 2)["edited"]["text"], "revision 2")

    def test_revision_trigger_captures_other_edit_paths_and_rejects_skipped_revision(self):
        key = self.completed()
        with self.store.transaction() as db:
            db.execute("UPDATE results SET edited=?,revision=revision+1 WHERE id=?",
                       ('{"text":"other native editor","tables":[]}', key))
        self.assertEqual(self.store.result_snapshot(key, 1)["edited"]["text"], "other native editor")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute("UPDATE results SET edited=? WHERE id=?", ('{"text":"silent overwrite","tables":[]}', key))
        self.assertEqual(self.store.result(key)["revision"], 1)

    def test_adoption_replay_is_before_cas_and_conflicting_payload_is_rejected(self):
        key = self.completed()
        self.store.save(key, {"text": "first correction", "tables": []}, 0)
        self.store.save(key, {"text": "second correction", "tables": []}, 1)
        body = self.adoption_body(key, 2)
        receipt = self.store.adopt_result("operation_123", body)
        self.assertEqual(receipt["revision"], 2)
        self.store.save(key, {"text": "later correction", "tables": []}, 2)
        reopened = Store(self.store.root)
        self.assertEqual(reopened.adopt_result("operation_123", body), receipt)
        self.assertEqual(reopened.adoption_operation("operation_123", self.project["id"], "instanceA"), receipt)
        self.assertEqual(len(reopened.rows("SELECT * FROM platform_adoptions")), 1)
        with self.assertRaises(Conflict):
            reopened.adopt_result("operation_123", {**body, "note": "different request"})
        with self.assertRaises(Conflict):
            reopened.adopt_result("operation_456", body)
        with self.assertRaises(Conflict):
            reopened.adoption_operation("operation_123", self.project["id"], "otherInstance")
        self.assertIsNone(reopened.adoption_operation("operation_none", self.project["id"], "instanceA"))

    def test_adoption_rechecks_native_project_result_version_and_original_bytes(self):
        key = self.completed()
        body = self.adoption_body(key, 0)
        for field in ("project_id", "task_id", "image_id", "version_id", "original_sha256"):
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.store.adopt_result("operation_bad", {**body, field: "a" * 64 if field == "original_sha256" else "otherIdentity"})
        original = self.store.file(self.store.one("images", self.image["id"])["original_path"])
        original_bytes = original.read_bytes()
        original.write_bytes(b"changed original")
        with self.assertRaises(Conflict):
            self.store.adopt_result("operation_bad", body)
        original.write_bytes(original_bytes)
        version = self.store.one("versions", self.image["active_version"])
        self.store.file(version["path"]).write_bytes(b"changed image version")
        with self.assertRaises(Conflict):
            self.store.adopt_result("operation_bad", body)
        self.assertEqual(self.store.rows("SELECT * FROM platform_adoptions"), [])

    def test_v15_migration_retains_known_snapshots_and_explicitly_marks_missing_revisions(self):
        key = self.completed()
        self.store.save(key, {"text": "old first", "tables": []}, 0)
        self.store.save(key, {"text": "old second", "tables": []}, 1)
        self.store.history(key, -1, 2)
        with self.store.transaction() as db:
            drop_post_v15_tables(db)
            for name in ("result_revision_initial", "result_revision_guard", "result_revision_snapshot",
                         "result_revision_immutable", "platform_adoption_immutable"):
                db.execute("DROP TRIGGER " + name)
            db.execute("DROP TABLE platform_adoptions")
            db.execute("DROP TABLE result_revisions")
            db.execute("PRAGMA user_version=15")
        migrated = Store(self.store.root)
        self.assertEqual(migrated.result_snapshot(key, 0)["edited"]["text"], self.raw["text"])
        self.assertEqual(migrated.result_snapshot(key, 3)["edited"]["text"], "old first")
        missing = migrated.result_snapshot(key, 1)
        self.assertFalse(missing["available"])
        self.assertEqual(missing["reason"], "legacy_revision_unavailable")
        self.assertNotIn("edited", missing)
        self.assertEqual(migrated.result_history(key)["unavailable_revisions"], 2)
        migrated.save(key, {"text": "newly tracked", "tables": []}, 3)
        self.assertEqual(migrated.result_snapshot(key, 4)["parent_revision"], 3)
        with closing(sqlite3.connect(migrated.migration_backup)) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 15)

    def test_adoption_http_routes_enforce_managed_instance_and_read_back_lost_reply(self):
        from fastapi.testclient import TestClient
        from ocr_workbench.service import create_app
        bundle = self.root / "bundle"
        (bundle / "config").mkdir(parents=True)
        (bundle / "config/engines.json").write_text('{"ppocr":{"name":"Synthetic","models":[]}}', "utf-8")
        with patch.dict("os.environ", {"WORKBENCH_APP_ID": "ocr", "WORKBENCH_INSTANCE_ID": "instanceA",
                                      "WORKBENCH_NONCE": "n" * 32}):
            app = create_app(bundle, self.store.root, "t" * 32, start_queue=False, review_only=True)
        key = self.completed()
        body = self.adoption_body(key, 0)
        client = TestClient(app)
        headers = {"Authorization": "Bearer " + "t" * 32}
        path = "/api/platform/adoptions/operation_http"
        self.assertEqual(client.post(path, json=body).status_code, 401)
        self.assertEqual(client.post(path, headers=headers, json={**body, "instance_id": "otherInstance"}).status_code, 409)
        response = client.post(path, headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        lookup = client.get(path, headers=headers, params={"project_id": self.project["id"], "instance_id": "instanceA"})
        self.assertEqual(lookup.json()["receipt"], response.json())
        snapshot = client.get(f"/api/results/{key}/revisions/0", headers=headers).json()
        self.assertEqual(snapshot["original_sha256"], self.image["sha256"])
        self.assertEqual(snapshot["edited"]["text"], self.raw["text"])

    def test_cold_restored_instance_can_query_original_operation_without_changing_its_identity(self):
        from fastapi.testclient import TestClient
        from ocr_workbench.service import create_app
        key = self.completed()
        self.store.save(key, {"text": "first correction", "tables": []}, 0)
        self.store.save(key, {"text": "second correction", "tables": []}, 1)
        body = self.adoption_body(key, 2)
        old_receipt = self.store.adopt_result("operation_restore", body)
        backup = self.root / "cold-backup"
        backup.mkdir()
        with closing(sqlite3.connect(self.store.root / "workbench.sqlite3")) as source_db:
            with closing(sqlite3.connect(backup / "workbench.sqlite3")) as backup_db:
                source_db.backup(backup_db)
        shutil.copytree(self.store.root / "projects", backup / "projects")
        restored_root = self.root / "new restored workspace"
        shutil.copytree(backup, restored_root)
        bundle = self.root / "restore-bundle"
        (bundle / "config").mkdir(parents=True)
        (bundle / "config/engines.json").write_text('{"ppocr":{"name":"Synthetic","models":[]}}', "utf-8")
        with patch.dict("os.environ", {"WORKBENCH_APP_ID": "ocr", "WORKBENCH_INSTANCE_ID": "instanceB",
                                      "WORKBENCH_NONCE": "b" * 32}):
            app = create_app(bundle, restored_root, "b" * 32, start_queue=False, review_only=True)
        client = TestClient(app)
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer " + "b" * 32}
        path = "/api/platform/adoptions/operation_restore"
        current = {"project_id": self.project["id"], "instance_id": "instanceB"}
        # A current connection cannot implicitly treat a historical A receipt as B.
        self.assertEqual(client.get(path, headers=headers, params=current).status_code, 409)
        pinned = {**current, "operation_instance_id": "instanceA"}
        recovered = client.get(path, headers=headers, params=pinned)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["receipt"], old_receipt)
        for changes in ({"instance_id": "instanceA"}, {"operation_instance_id": "wrongOrigin"},
                        {"project_id": "wrongProject"}):
            with self.subTest(changes=changes):
                self.assertEqual(client.get(path, headers=headers, params={**pinned, **changes}).status_code, 409)
        # POST remains bound to B. A historical receipt cannot be re-written as B.
        self.assertEqual(client.post(path, headers=headers, json=body).status_code, 409)
        self.assertEqual(client.post(path, headers=headers, json={**body, "instance_id": "instanceB"}).status_code, 409)
        restored = app.state.store
        self.assertEqual(len(restored.rows("SELECT * FROM platform_adoptions")), 1)
        self.assertEqual(restored.adoption_operation("operation_restore", self.project["id"], "instanceA"), old_receipt)
        self.assertEqual(restored.result_snapshot(key, 2)["edited"]["text"], "second correction")
        with self.assertRaises(sqlite3.IntegrityError):
            with restored.transaction() as db:
                db.execute("UPDATE platform_adoptions SET instance_id='instanceB' WHERE operation_id='operation_restore'")

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
            drop_post_v15_tables(db)
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall():
                db.execute('DROP TRIGGER "' + row["name"] + '"')
            db.execute("DROP TABLE reviews")
            db.execute("DROP TABLE project_revisions")
            db.execute("DROP TABLE ocr_submissions")
            db.execute("DROP TABLE platform_adoptions")
            db.execute("DROP TABLE result_revisions")
            for table in ("agent_mutations", "agent_selections", "agent_evidence", "agent_inbox", "agent_resume_intents", "agent_artifact_leases", "agent_artifacts", "agent_grants",
                          "agent_decisions", "agent_job_links", "agent_calls", "agent_operations", "agent_model_requests",
                          "agent_events", "agent_runs", "agent_sessions", "agent_checkpoint_cleanup"):
                db.execute("DROP TABLE " + table)
            for table in ("multimodal_decisions", "multimodal_proposals", "multimodal_requests"):
                db.execute("DROP TABLE " + table)
            for table in ("structure_decisions", "structure_checks", "structure_proposals", "structure_candidates"):
                db.execute("DROP TABLE " + table)
            for table in ("document_conflict_decisions", "review_timings", "page_ocr_inputs", "geometry_requests", "geometry_evidence", "document_stages",
                          "regions", "page_versions", "pages", "documents"):
                db.execute("DROP TABLE " + table)
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
