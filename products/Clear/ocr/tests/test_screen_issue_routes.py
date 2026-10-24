"""HTTP reentry and export identity over real SQLite; no model is started."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ocr_workbench.screen_routes import register_screen_routes
import test_screen_resolution as fixture


class ScreenIssueRouteTests(unittest.TestCase):
    setUp = fixture.ResolutionTests.setUp
    tearDown = fixture.ResolutionTests.tearDown
    body = fixture.ResolutionTests.body

    def client(self, recognition):
        app = FastAPI()

        def require_recognition():
            if not recognition[0]:
                raise HTTPException(403, "Recognition is unavailable in this scope")

        register_screen_routes(app, self.manager, require_recognition)
        return TestClient(app)

    def base(self):
        return f"/api/projects/{self.project}/screen-batches/{self.batch}"

    def batch_count(self):
        return len(self.store.rows("SELECT id FROM screen_batches"))

    def test_new_precision_requires_recognition_but_lookup_never_submits(self):
        body = self.body()
        before = self.batch_count()
        recognition = [False]
        with self.client(recognition) as client:
            denied = client.post(self.base() + "/resolve", json=body)
            self.assertEqual(denied.status_code, 403)
            lookup = client.post(self.base() + "/resolve-lookup", json=body)
            self.assertEqual(lookup.status_code, 200)
            self.assertIsNone(lookup.json())
        self.assertEqual(self.batch_count(), before)
        self.assertEqual(self.store.rows("SELECT request_id FROM screen_actions WHERE batch_id=?", (self.batch,)), [])

    def test_lost_submit_response_is_recoverable_without_another_precision_child(self):
        body = self.body()
        recognition = [True]
        before = self.batch_count()
        original = deepcopy(self.store.result(body["targets"][0]["result_id"]))
        with self.client(recognition) as client:
            submitted = client.post(self.base() + "/resolve", json=body)
            self.assertEqual(submitted.status_code, 200)
            self.assertEqual(submitted.json()["status"], "submitted")
            recognition[0] = False
            recovered = client.post(self.base() + "/resolve-lookup", json=body)
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.json()["batch_id"], submitted.json()["batch_id"])
            recognition[0] = True
            repeated = client.post(self.base() + "/resolve", json=body)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json()["batch_id"], submitted.json()["batch_id"])
        self.assertEqual(self.batch_count(), before + 1)
        self.assertEqual(self.store.result(original["id"]), original)
        self.assertEqual(len(self.store.rows("SELECT request_id FROM screen_actions WHERE batch_id=?", (self.batch,))), 1)

    def test_delivery_snapshot_changes_after_edit_with_the_same_input_count(self):
        with self.client([False]) as client:
            first = client.get(self.base() + "/delivery-snapshot")
            self.assertEqual(first.status_code, 200)
            repeated = client.get(self.base() + "/delivery-snapshot")
            self.assertEqual(repeated.json(), first.json())
            item = self.manager._items(self.batch)[0]
            result = self.store.result(item["result_id"])
            edited = deepcopy(result["edited"])
            edited["tables"][0]["cells"][-1]["text"] = "-20.00"
            self.store.save(result["id"], edited, result["revision"])
            current = client.get(self.base() + "/delivery-snapshot")
            self.assertEqual(current.status_code, 200)
        self.assertEqual(len(current.json()["items"]), len(first.json()["items"]))
        self.assertNotEqual(current.json()["snapshot_id"], first.json()["snapshot_id"])
        self.assertFalse(current.json()["fresh"])

    def test_status_remains_readable_while_preparation_holds_batch_and_file_guards(self):
        entered = threading.Event()
        release = threading.Event()

        def preparing():
            with self.manager.guard, self.store.file_lock:
                entered.set()
                release.wait(10)

        holder = threading.Thread(target=preparing)
        holder.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.client([False]) as client, ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.get, self.base())
                try:
                    response = future.result(timeout=2)
                finally:
                    release.set()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["counts"]["total"], 2)
            self.assertEqual(response.json()["counts"]["needs_review"], 2)
            self.assertEqual(response.json()["stale_item_count"], 0)
        finally:
            release.set()
            holder.join(timeout=2)
        self.assertFalse(holder.is_alive())


if __name__ == "__main__":
    unittest.main()
