from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ocr_workbench.editing import export_markdown, tables_html
from ocr_workbench.imaging import add_image
from ocr_workbench.multimodal_contract import build_targets
from ocr_workbench.multimodal_store import (complete_review, decide_review, enqueue_review,
    prepare_review, review_sources, view_review)
from ocr_workbench.store import Conflict, SCHEMA_VERSION, Store
from ocr_workbench.tables import parse_tables


class MultimodalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/"项目")
        self.project = self.store.project("通用文档")
        path = Path(self.temp.name)/"image.png"
        Image.new("RGB", (200,120), "white").save(path)
        self.image = add_image(self.store,self.project["id"],"image.png",path)
        self.version = self.store.one("versions",self.image["active_version"])
        self.raw = {"engine":"ppocr","text":"编号 00001\n金额 -0.01\n名称 张三", "tables":[],"blocks":[]}
        self.result_id = self.result(self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def result(self, raw):
        task = self.store.enqueue(self.project["id"],[self.version["id"]],["ppocr"])[0]
        self.assertEqual(self.store.claim()["id"],task)
        self.store.complete(task,raw)
        return self.store.one("tasks",task)["result_id"]

    def request(self, request="review-001", target=None, **overrides):
        body = {"revision":self.store.result(self.result_id)["revision"],"version_id":self.version["id"],
                "model_id":"small-visual","scope":"target" if target else "page","request_id":request}
        if target is not None:
            body["target"] = target
        return {**body,**overrides}

    def enqueue(self, **kwargs):
        return enqueue_review(self.store,self.result_id,self.request(**kwargs),{"model":{"id":"small-visual","sha256":"fixed"}})

    def generated(self, replacements=None, request="review-001"):
        task = self.enqueue(request=request)
        self.assertEqual(self.store.claim()["id"],task)
        snapshot = prepare_review(self.store,task)
        replacements = replacements or {}
        response = {"items":[{"target_id":t["id"],"decision":"replace" if t["before"] in replacements else "keep",
            "after":replacements.get(t["before"],t["before"]),"reason":"对照原图"} for t in snapshot["targets"]],"summary":"逐项审校",
            "identity":{"model_sha256":"fixed"},"timing":{"elapsed_ms":10},"artifact":"artifacts/review.json"}
        self.assertTrue(complete_review(self.store,task,response))
        return task, snapshot, view_review(self.store,self.result_id)["proposals"]

    def decide(self, proposal, action="accept", request="decision-001", **overrides):
        body = {"action":action,"revision":self.store.result(self.result_id)["revision"],
                "version_id":self.version["id"],"request_id":request,**overrides}
        return decide_review(self.store,self.result_id,proposal["id"],body)

    def test_literal_accept_preserves_original_and_undo_is_stale(self):
        _, _, proposals = self.generated({"编号 00001":"编号 000001"})
        original = self.store.result(self.result_id)["original"]
        p = next(p for p in proposals if p["status"] == "replace")
        saved = self.decide(p)
        self.assertIn("000001",saved["edited"]["text"])
        self.assertEqual(saved["original"],original)
        self.assertFalse(self.store.rows("SELECT 1 FROM reviews"))
        undone = self.store.history(self.result_id,-1,saved["revision"])
        self.assertEqual(undone["edited"]["text"],original["text"])
        self.assertEqual(view_review(self.store,self.result_id)["counts"]["stale"],3)
        redone = self.store.history(self.result_id,1,undone["revision"])
        self.assertEqual(redone["edited"],saved["edited"])
        self.assertEqual(redone["original"],original)

    def test_sequential_text_rebases_and_expires_other_requests(self):
        _, _, first = self.generated({"编号 00001":"编号 000000001","金额 -0.01":"金额 -0.10"})
        self.generated(request="another-batch")
        a = next(p for p in first if p["before"] == "编号 00001")
        b = next(p for p in first if p["before"] == "金额 -0.01")
        self.decide(a)
        view = view_review(self.store,self.result_id)
        new_b = next(p for p in view["proposals"] if p["id"] == b["id"])
        self.assertEqual(new_b["state"],"pending")
        self.assertEqual(new_b["target"]["start"],b["target"]["start"]+4)
        self.assertEqual(new_b["original_target"],b["target"])
        self.assertEqual(view["counts"]["stale"],3)
        final = self.decide(new_b,request="decision-002")
        self.assertIn("编号 000000001\n金额 -0.10",final["edited"]["text"])
        self.assertEqual(view_review(self.store,self.result_id)["counts"]["accepted"],2)

    def test_question_and_reject_are_undoable_without_expiring_siblings(self):
        _, _, proposals = self.generated()
        questioned = self.decide(proposals[0],"question")
        self.assertEqual(questioned["edited"]["text"],self.raw["text"])
        self.assertEqual(view_review(self.store,self.result_id)["counts"]["pending"],2)
        self.decide(proposals[1],"reject",request="reject-001")
        view = view_review(self.store,self.result_id)
        self.assertEqual(view["counts"]["question"],1)
        self.decide(proposals[0],request="answer-001")
        self.assertEqual(view_review(self.store,self.result_id)["counts"]["accepted"],1)

    def test_decision_idempotence_and_payload_binding(self):
        _, _, proposals = self.generated()
        body = {"action":"accept","revision":0,"version_id":self.version["id"],"request_id":"same"}
        a = decide_review(self.store,self.result_id,proposals[0]["id"],body)
        b = decide_review(self.store,self.result_id,proposals[0]["id"],body)
        self.assertEqual(a,b)
        with self.assertRaises(Conflict):
            decide_review(self.store,self.result_id,proposals[0]["id"],{**body,"action":"reject"})
        self.assertEqual(self.store.result(self.result_id)["revision"],1)

    def test_submission_idempotence_and_concurrent_old_revision(self):
        body = self.request()
        task = enqueue_review(self.store,self.result_id,body,{})
        self.assertEqual(task,enqueue_review(self.store,self.result_id,body,{"later":"changed"}))
        with self.assertRaises(Conflict):
            enqueue_review(self.store,self.result_id,{**body,"scope":"target","target":{"kind":"text","start":0,"end":1}}, {})
        self.store.claim()
        snapshot = prepare_review(self.store,task)
        complete_review(self.store,task,{"items":[{"target_id":t["id"],"decision":"keep","after":t["before"],"reason":"一致"} for t in snapshot["targets"]]})
        proposals = view_review(self.store,self.result_id)["proposals"]
        def update(index):
            separate = Store(self.store.root)
            try:
                return decide_review(separate,self.result_id,proposals[index]["id"],
                    {"revision":0,"version_id":self.version["id"],"request_id":f"parallel-{index}","action":"accept"})
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(update,(0,1)))
        self.assertEqual(sum(v == "conflict" for v in values),1)
        self.assertEqual(self.store.result(self.result_id)["revision"],1)

    def test_cancelled_and_failed_work_never_produces_proposals(self):
        for status in ("cancelled","failed"):
            task = self.enqueue(request=status)
            self.store.claim()
            snapshot = prepare_review(self.store,task)
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status=? WHERE id=?",(status,task))
            with self.assertRaises(Conflict):
                prepare_review(self.store,task)
            self.assertEqual(prepare_review(self.store,task,require_running=False),snapshot)
            self.assertFalse(complete_review(self.store,task,{"items":[]}))
        self.assertFalse(view_review(self.store,self.result_id)["proposals"])

    def test_edit_during_inference_rejects_late_result(self):
        task = self.enqueue()
        self.store.claim()
        prepare_review(self.store,task)
        self.store.save(self.result_id,{"text":"人工值","tables":[]},0)
        self.assertFalse(complete_review(self.store,task,{"items":[]}))
        self.assertEqual(self.store.one("tasks",task)["status"],"cancelled")
        self.assertFalse(view_review(self.store,self.result_id)["proposals"])

    def test_selection_switch_back_does_not_revive_snapshot(self):
        task = self.enqueue()
        self.store.claim()
        other = self.result(self.raw)
        with self.store.transaction() as db:
            db.execute("UPDATE selections SET result_id=? WHERE image_id=?",(other,self.image["id"]))
            db.execute("UPDATE selections SET result_id=? WHERE image_id=?",(self.result_id,self.image["id"]))
        with self.assertRaises(Conflict):
            prepare_review(self.store,task)
        self.assertFalse(complete_review(self.store,task,{"items":[]}))

    def test_version_and_real_file_changes_reject_decision(self):
        _, _, proposals = self.generated()
        with self.store.transaction() as db:
            db.execute("UPDATE images SET active_version=NULL WHERE id=?",(self.image["id"],))
            db.execute("UPDATE images SET active_version=? WHERE id=?",(self.version["id"],self.image["id"]))
        with self.assertRaises(Conflict):
            self.decide(proposals[0])
        _, _, proposals = self.generated(request="fresh")
        fresh = next(p for p in proposals if p["state"] == "pending")
        Image.new("RGB",(200,120),"red").save(self.store.file(self.version["path"]))
        with self.assertRaises(Conflict):
            self.decide(fresh)

    def test_html_not_duplicated_and_cell_values_remain_literal(self):
        table = {"rows":1,"columns":2,"cells":[{"row":0,"column":c,"row_span":1,"column_span":1,"text":v} for c,v in enumerate(("00001","-0.01"))]}
        markup = tables_html([table])
        text = "前文😀\n"+markup+"\n后文"
        table["source"] = parse_tables(text)[0]["source"]
        self.result_id = self.result({"engine":"ppocr","text":text,"tables":[table],"blocks":[]})
        with self.store.transaction() as db:
            db.execute("UPDATE selections SET result_id=? WHERE image_id=?",(self.result_id,self.image["id"]))
        _, snapshot, proposals = self.generated({"00001":"000001","-0.01":"-0.10"})
        self.assertEqual(len(snapshot["targets"]),4)
        self.assertFalse(any("<table>" in t["before"] for t in snapshot["targets"]))
        cells = [p for p in proposals if p["target"]["kind"] == "cell"]
        self.decide(cells[0])
        saved = self.decide(cells[1],request="cell-002")
        self.assertIn("000001",export_markdown(saved["edited"]))
        self.assertIn("-0.10",export_markdown(saved["edited"]))
        self.assertEqual(saved["original"]["tables"][0]["cells"][0]["text"],"00001")
        with self.assertRaises(ValueError):
            self.enqueue(request="markup",target={"kind":"text","start":text.index("<table>"),"end":text.index("</table>")+8})

    def test_invalid_targets_response_and_uncertain_cannot_replace(self):
        with self.assertRaises(ValueError):
            self.enqueue(target={"kind":"text","start":False,"end":5})
        task = self.enqueue()
        self.store.claim()
        snapshot = prepare_review(self.store,task)
        response = {"items":[{"target_id":t["id"],"decision":"uncertain","after":t["before"],"reason":"不清楚"} for t in snapshot["targets"]]}
        bad = deepcopy(response)
        bad["items"][0]["target_id"] = "invented-target"
        with self.assertRaises(ValueError):
            complete_review(self.store,task,bad)
        self.assertFalse(view_review(self.store,self.result_id)["proposals"])
        bad = deepcopy(response)
        bad["items"][0]["after"] = 1
        with self.assertRaises(ValueError):
            complete_review(self.store,task,bad)
        self.assertTrue(complete_review(self.store,task,response))
        p = view_review(self.store,self.result_id)["proposals"][0]
        with self.assertRaises(ValueError):
            self.decide(p)

    def test_sources_include_immutable_snapshots_response_and_rebase(self):
        _, snapshot, proposals = self.generated({"编号 00001":"编号 000001"})
        self.decide(next(p for p in proposals if p["status"] == "replace"))
        with self.store.transaction() as db:
            sources = review_sources(db,self.store.result(self.result_id))
        self.assertFalse(sources["automatic_adoption"])
        self.assertFalse(sources["contributes_to_votes"])
        self.assertEqual(sources["requests"][0]["snapshot"]["targets"],snapshot["targets"])
        self.assertEqual(sources["requests"][0]["response"]["identity"]["model_sha256"],"fixed")
        self.assertTrue(sources["proposals"][0]["rebase_history"])
        self.assertNotIn("image_path",sources["requests"][0]["snapshot"])

    def test_original_text_geometry_requires_unique_exact_match(self):
        original = {"text":"唯一行", "tables":[], "blocks":[{"text":"唯一行","polygon":[[0,0],[50,0],[50,20],[0,20]]}]}
        targets = build_targets({"text":original["text"],"tables":[]},original,self.version,"page")
        self.assertEqual(targets[0]["evidence"]["level"],"text")
        changed = build_targets({"text":"修改后", "tables":[]},original,self.version,"page")
        self.assertIsNone(changed[0]["evidence"]["polygon"])
        self.assertEqual(changed[0]["evidence"]["level"],"page")

    def test_selected_span_offsets_are_unicode_code_points(self):
        edited = {"text":"😀编号 00001", "tables":[]}
        self.store.save(self.result_id,edited,0)
        task = self.enqueue(target={"kind":"text","start":4,"end":9})
        self.store.claim()
        snapshot = prepare_review(self.store,task)
        self.assertEqual(snapshot["targets"][0]["before"],"00001")
        self.assertEqual(len(snapshot["targets"]),1)

    def test_stale_geometry_is_full_page_and_manual_geometry_retains_semantics(self):
        edit = {"text":"文字", "tables":[]}
        target = {"kind":"text","start":0,"end":2}
        evidence = {"target":target,"version_id":self.version["id"],"image_sha256":self.version["sha256"],
            "source":"paddle","polygon":[[0,0],[50,0],[50,20],[0,20]],
            "details":{"anchor_snapshot_current":False,"level":"text","range_semantics":"text_extent"}}
        value = build_targets(edit,{},self.version,"target",target,[evidence])[0]
        self.assertEqual(value["evidence"]["level"],"page")
        evidence["source"] = "manual"
        value = build_targets(edit,{},self.version,"target",target,[evidence])[0]
        self.assertEqual(value["evidence"]["range_semantics"],"text_extent")
        evidence["polygon"][1][0] = 999
        value = build_targets(edit,{},self.version,"target",target,[evidence])[0]
        self.assertEqual(value["evidence"]["level"],"page")

    def test_empty_deletion_remains_accepted_through_sibling_rebase(self):
        _, _, proposals = self.generated({"编号 00001":"","金额 -0.01":"金额 -0.10"})
        self.decide(next(p for p in proposals if p["after"] == ""))
        self.decide(next(p for p in proposals if p["after"] == "金额 -0.10"),request="next")
        self.assertEqual(view_review(self.store,self.result_id)["counts"]["accepted"],2)

    def test_text_replacement_before_table_preserves_source_bindings(self):
        table = {"rows":1,"columns":1,"cells":[{"row":0,"column":0,"row_span":1,"column_span":1,"text":"00001"}]}
        text = "😀标题\n"+tables_html([table])+"\n尾注"
        table["source"] = parse_tables(text)[0]["source"]
        self.result_id = self.result({"engine":"ppocr","text":text,"tables":[table],"blocks":[]})
        with self.store.transaction() as db:
            db.execute("UPDATE selections SET result_id=? WHERE image_id=?",(self.result_id,self.image["id"]))
        _, _, proposals = self.generated({"😀标题":"😀很长的标题","00001":"000001"})
        self.decide(next(p for p in proposals if p["before"] == "😀标题"))
        saved = self.decide(next(p for p in proposals if p["before"] == "00001"),request="cell-after-title")
        rendered = export_markdown(saved["edited"])
        self.assertEqual(rendered.count("<table>"),1)
        self.assertIn("000001",rendered)


class MultimodalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"原有 v10 项目"
        # Build v10 through the genuine historical migration chain, never by
        # downgrading PRAGMA on a database that already contains v11 objects.
        with patch("ocr_workbench.store.SCHEMA_VERSION",10):
            self.legacy = Store(self.root)
        project = self.legacy.project("升级保留数据")
        path = Path(self.temp.name)/"page.png"
        Image.new("RGB",(80,50),"white").save(path)
        image = add_image(self.legacy,project["id"],"page.png",path)
        task = self.legacy.enqueue(project["id"],[image["active_version"]],["ppocr"])[0]
        self.legacy.claim()
        self.original = {"engine":"ppocr","text":"原始编号 00001","tables":[],"blocks":[]}
        self.legacy.complete(task,self.original)
        self.result_id = self.legacy.one("tasks",task)["result_id"]
        # The v10 writer predates this one v11 reconciliation hook; its edit
        # and compression/history paths otherwise remain the real Store code.
        with patch("ocr_workbench.multimodal_store.reconcile_review"):
            self.legacy.save(self.result_id,{"text":"人工第一次 000001","tables":[]},0)
            self.legacy.save(self.result_id,{"text":"人工第二次 -0.01","tables":[]},1)
        self.before_result = self.legacy.result(self.result_id)
        self.before_rows = self.legacy.rows("SELECT * FROM results ORDER BY id")
        self.before_history = self.legacy.rows("SELECT * FROM edits ORDER BY result_id,position")
        self.before_schema = self.legacy.rows("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
        self.assertEqual(self.legacy.rows("PRAGMA user_version")[0]["user_version"],10)
        self.assertFalse(self.legacy.rows("SELECT name FROM sqlite_master WHERE name LIKE 'multimodal_%'"))

    def verify_backup(self,path):
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],10)
            self.assertEqual([dict(r) for r in db.execute("SELECT * FROM results ORDER BY id")],self.before_rows)
            self.assertEqual([dict(r) for r in db.execute("SELECT * FROM edits ORDER BY result_id,position")],self.before_history)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'multimodal_%'").fetchall())
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")

    def test_real_v10_upgrade_keeps_backup_original_and_undo_history(self):
        upgraded = Store(self.root)
        self.assertEqual(upgraded.rows("PRAGMA user_version")[0]["user_version"],SCHEMA_VERSION)
        self.assertEqual(upgraded.result(self.result_id),self.before_result)
        backups = list((self.root/"database-backups").glob("before-v10-to-*.sqlite3"))
        self.assertEqual(len(backups),1)
        self.assertEqual(upgraded.migration_backup,backups[0])
        self.verify_backup(backups[0])
        self.assertEqual({r["name"] for r in upgraded.rows("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'multimodal_%'")},
                         {"multimodal_requests","multimodal_proposals","multimodal_decisions"})
        first = upgraded.history(self.result_id,-1,2)
        self.assertEqual(first["edited"]["text"],"人工第一次 000001")
        original = upgraded.history(self.result_id,-1,first["revision"])
        self.assertEqual(original["edited"]["text"],self.original["text"])
        self.assertEqual(original["original"],self.original)
        redone = upgraded.history(self.result_id,1,original["revision"])
        final = upgraded.history(self.result_id,1,redone["revision"])
        self.assertEqual(final["edited"],self.before_result["edited"])
        self.assertEqual(final["original"],self.original)
        self.assertEqual(upgraded.rows("PRAGMA foreign_key_check"),[])
        Store(self.root)
        self.assertEqual(len(list((self.root/"database-backups").glob("*.sqlite3"))),1)

    def test_failed_v11_migration_rolls_back_created_schema_and_data_then_recovers(self):
        from ocr_workbench.multimodal_store import migrate_v11
        def fail_after_migration(db):
            migrate_v11(db)
            # Prove both DDL and DML are rolled back even after the entire new
            # schema and triggers have been installed within the transaction.
            db.execute("UPDATE results SET edited=?,revision=revision+1 WHERE id=?",
                       (json.dumps({"text":"must roll back","tables":[]}),self.result_id))
            raise OSError("injected failure after v11 migration")
        with patch("ocr_workbench.multimodal_store.migrate_v11",side_effect=fail_after_migration):
            with self.assertRaisesRegex(OSError,"after v11 migration"):
                Store(self.root)
        self.assertEqual(self.legacy.rows("PRAGMA user_version")[0]["user_version"],10)
        self.assertEqual(self.legacy.rows("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"),self.before_schema)
        self.assertEqual(self.legacy.rows("SELECT * FROM results ORDER BY id"),self.before_rows)
        self.assertEqual(self.legacy.rows("SELECT * FROM edits ORDER BY result_id,position"),self.before_history)
        backups = list((self.root/"database-backups").glob("*.sqlite3"))
        self.assertEqual(len(backups),1)
        self.verify_backup(backups[0])
        repaired = Store(self.root)
        self.assertEqual(repaired.rows("PRAGMA user_version")[0]["user_version"],SCHEMA_VERSION)
        self.assertEqual(repaired.result(self.result_id),self.before_result)
        undone = repaired.history(self.result_id,-1,self.before_result["revision"])
        self.assertEqual(undone["edited"]["text"],"人工第一次 000001")
        self.assertEqual(undone["original"],self.original)


if __name__ == "__main__":
    unittest.main()
