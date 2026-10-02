import json
from io import BytesIO

from PIL import Image

from image_platform.store import dump


def finished_result(client, store, key=None):
    image = BytesIO()
    Image.new("RGB", (20, 20), "blue").save(image, format="PNG")
    asset_id = client.post("/api/v1/projects/p/uploads", files=[("files", ("review.png", image.getvalue(), "image/png"))]).json()["asset_ids"][0]
    asset = store.one("SELECT * FROM assets WHERE id=?", (asset_id,))
    job_id = store.submit("p", "inference", {"model_id": "model-test"}, [asset], key)
    prediction = {"label_id": "a", "score": 0.9, "status": "needs_review", "model_version_id": "model-test"}
    with store.connect(write=True) as con:
        con.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?", (dump(prediction), job_id))
    return job_id, asset_id, prediction


def test_annotation_is_not_this_runs_decision_and_reopen_preserves_prediction(client, store):
    job_id, asset_id, prediction = finished_result(client, store)
    assert client.post("/api/v1/projects/p/annotations", json={"asset_ids": [asset_id], "label_id": "b"}).status_code == 200
    url = f"/api/v1/jobs/{job_id}"
    row = client.get(url + "/results?status=needs_review").json()["items"][0]
    assert row["confirmed_label"] == "b"
    assert row["human_label_id"] is None and row["disposition_state"] == "unreviewed"
    for revision, state in enumerate(("confirmed", "deferred", "reopened")):
        response = client.post(url + "/dispositions", json={
            "item_ids": [row["id"]], "state": state, "label_id": "b" if state == "confirmed" else None,
            "note": "checked", "expected_revisions": {row["id"]: revision}})
        assert response.status_code == 200
        current = client.get(url + "/results").json()
        assert current["disposition_counts"] == {state: 1}
        assert current["items"][0]["prediction"] == prediction
    assert client.get(url + "/results?status=needs_review").json()["total"] == 1
    history = client.get(url + f"/results/{row['id']}/dispositions").json()["items"]
    assert [event["state"] for event in history] == ["confirmed", "deferred", "reopened"]
    assert history[0]["label_id"] == "b"
    exported = json.loads(client.get(url + "/export?format=jsonl").text)
    assert exported["label_id"] == "a" and exported["disposition_state"] == "reopened"


def test_explicit_bulk_range_rolls_back_and_stale_decisions_are_rejected(client, store):
    job_id, _, prediction = finished_result(client, store)
    url = f"/api/v1/jobs/{job_id}"
    row = client.get(url + "/results").json()["items"][0]
    payload = {"item_ids": [row["id"], "missing"], "state": "confirmed", "label_id": "a"}
    assert client.post(url + "/dispositions", json=payload).status_code == 404
    assert client.get(url + "/results").json()["items"][0]["disposition_revision"] == 0
    payload["item_ids"] = [row["id"]]
    payload["expected_revisions"] = {row["id"]: 0}
    assert client.post(url + "/dispositions", json=payload).status_code == 200
    assert client.post(url + "/dispositions", json=payload).status_code == 409
    assert json.loads(store.one("SELECT prediction FROM job_items WHERE id=?", (row["id"],))["prediction"]) == prediction


def test_lost_response_lookup_identifies_existing_submission(client, store):
    url = "/api/v1/projects/p/inference-requests/request-once"
    assert client.get(url).json() == {"submitted": False, "job_id": None}
    job_id, _, _ = finished_result(client, store, "request-once")
    assert client.get(url).json() == {"submitted": True, "job_id": job_id}
