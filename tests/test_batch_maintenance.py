import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
from ocr_workbench.imaging import add_image, prepare_task, thumbnail, validate_preset
from ocr_workbench.maintenance import ProjectMaintenance
from ocr_workbench.store import Store
from ocr_workbench.errors import friendly_engine_error


class BatchMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.project = self.store.project("可清理项目")
        file = self.root / "source.png"
        Image.new("RGB", (320, 200), "red").save(file)
        self.photo = add_image(self.store, self.project["id"], "source.png", file)
        self.queue = SimpleNamespace(status=lambda: {"task_id": None})
        self.maintenance = ProjectMaintenance(self.store, self.queue)

    def tearDown(self):
        self.temp.cleanup()

    def test_preset_persists_and_reuses_one_version_across_engines(self):
        ops = [{"kind": "rotate", "degrees": 90}, {"kind": "contrast", "factor": 1.3}]
        ids = self.store.enqueue(
            self.project["id"], [self.photo["active_version"]], ["ppocr", "glm"], ops
        )
        first = prepare_task(self.store, self.store.claim())
        self.store.complete(first["id"], {"text": "x", "tables": []})
        second = prepare_task(self.store, self.store.claim())
        self.assertEqual(first["version_id"], second["version_id"])
        self.assertNotEqual(first["version_id"], self.photo["active_version"])
        self.assertEqual(self.store.one("versions", first["version_id"])["width"], 200)
        self.assertEqual(
            json.loads(Store(self.store.root).one("tasks", ids[0])["preprocess"]), ops
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM versions")), 2)

    def test_cancelled_preprocess_does_not_publish_version(self):
        key = self.store.enqueue(
            self.project["id"],
            [self.photo["active_version"]],
            ["ppocr"],
            [{"kind": "contrast", "factor": 1.3}],
        )[0]
        task = self.store.claim()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (key,))
        self.assertIsNone(prepare_task(self.store, task))
        self.assertEqual(len(self.store.rows("SELECT * FROM versions")), 1)

    def test_thumbnail_and_invalid_preset(self):
        path = thumbnail(self.store, self.photo["active_version"])
        with Image.open(path) as im:
            self.assertEqual(im.size, (160, 100))
        self.assertEqual(thumbnail(self.store, self.photo["active_version"]), path)
        for value in [
            [{"kind": "contrast", "factor": float("nan")}],
            [{"kind": "crop", "box": [0, 0, 10, 10]}],
            [{"kind": "rotate", "degrees": 45}],
        ]:
            with self.assertRaises(ValueError):
                validate_preset(value)

    def test_delete_isolated_project_and_edited_results(self):
        other = self.store.project("保留项目")
        key = self.store.enqueue(
            self.project["id"], [self.photo["active_version"]], ["ppocr"]
        )[0]
        self.store.claim()
        self.store.complete(key, {"text": "001234", "tables": []})
        thumbnail(self.store, self.photo["active_version"])
        output = self.store.root / "task-results" / key
        output.mkdir(parents=True)
        (output / "raw.json").write_text("{}")
        self.assertGreater(self.maintenance.usage(self.project["id"])["bytes"], 0)
        with self.assertRaises(ValueError):
            self.maintenance.delete(self.project["id"], "错名")
        result = self.maintenance.delete(self.project["id"], "可清理项目")
        self.assertTrue(result["deleted"])
        self.assertFalse(result["cleanup_pending"])
        self.assertEqual(self.store.one("projects", other["id"])["name"], "保留项目")
        self.assertEqual(self.store.rows("SELECT * FROM images"), [])
        self.assertEqual(self.store.rows("SELECT * FROM edits"), [])
        self.assertFalse(output.exists())

    def test_running_and_queued_project_cannot_delete(self):
        key = self.store.enqueue(
            self.project["id"], [self.photo["active_version"]], ["ppocr"]
        )[0]
        with self.assertRaises(ValueError):
            self.maintenance.delete(self.project["id"], "可清理项目")
        self.store.claim()
        self.queue.status = lambda: {"task_id": key}
        with self.assertRaises(ValueError):
            self.maintenance.delete(self.project["id"], "可清理项目")
        self.assertTrue(self.store.file(self.photo["original_path"]).exists())

    def test_file_failure_restores_original_and_database(self):
        thumbnail(self.store, self.photo["active_version"])
        original_replace = Path.replace

        def fail(source, target):
            if source.suffix == ".jpg":
                raise OSError("simulated disk error")
            return original_replace(source, target)

        with patch.object(Path, "replace", fail):
            with self.assertRaises(OSError):
                self.maintenance.delete(self.project["id"], "可清理项目")
        self.assertTrue(self.store.file(self.photo["original_path"]).exists())
        self.assertEqual(
            self.store.one("projects", self.project["id"])["name"], "可清理项目"
        )
        self.assertEqual(list(self.maintenance.trash.iterdir()), [])

    def test_restart_completes_cleanup_after_database_commit(self):
        with patch(
            "ocr_workbench.maintenance.shutil.rmtree", side_effect=OSError("busy")
        ):
            result = self.maintenance.delete(self.project["id"], "可清理项目")
        self.assertTrue(result["cleanup_pending"])
        ProjectMaintenance(self.store, self.queue)
        self.assertEqual(list(self.maintenance.trash.iterdir()), [])

    def test_oom_message_does_not_claim_a_quality_fallback(self):
        self.assertIn(
            "未降低识别质量", friendly_engine_error(RuntimeError("CUDA out of memory"))
        )


if __name__ == "__main__":
    unittest.main()
