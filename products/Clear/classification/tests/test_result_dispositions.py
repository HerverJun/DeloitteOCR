import json
from io import BytesIO

from PIL import Image

from image_platform.store import dump


ACTOR = {"id": "localReviewer", "display_name": "本机复核员", "source": "local"}


def decision(client, item_ids, state="confirmed", label_id="b", revision=0, *, identity="decision", intent="manual"):
    return {"item_ids": item_ids, "state": state, "label_id": label_id if state == "confirmed" else None,
            "note": "checked", "expected_revisions": {item: revision for item in item_ids},
            "request_id": "request_" + identity, "operation_id": "operation_" + identity,
            "instance_id": client.get("/api/v1/health").json()["instance_id"], "actor": ACTOR, "intent": intent}


def lookup(client, operation_id, original_instance=None):
    current = client.get("/api/v1/health").json()["instance_id"]
    return client.get(f"/api/v1/projects/p/disposition-operations/{operation_id}", params={
        "instance_id": current, "operation_instance_id": original_instance or current})


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
        response = client.post(url + "/dispositions", json=decision(client, [row["id"]], state,
            revision=revision, identity="state" + str(revision)))
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
    assert exported["effective_label_id"] == "b" and exported["current_annotation_label_id"] == "b"


def test_explicit_bulk_range_rolls_back_and_stale_decisions_are_rejected(client, store):
    job_id, _, prediction = finished_result(client, store)
    url = f"/api/v1/jobs/{job_id}"
    row = client.get(url + "/results").json()["items"][0]
    payload = decision(client, [row["id"], "missing"], label_id="a")
    assert client.post(url + "/dispositions", json=payload).status_code == 404
    assert client.get(url + "/results").json()["items"][0]["disposition_revision"] == 0
    payload["item_ids"] = [row["id"]]
    payload["expected_revisions"] = {row["id"]: 0}
    assert client.post(url + "/dispositions", json=payload).status_code == 200
    payload["request_id"] = "request_stale"
    payload["operation_id"] = "operation_stale"
    assert client.post(url + "/dispositions", json=payload).status_code == 409
    assert json.loads(store.one("SELECT prediction FROM job_items WHERE id=?", (row["id"],))["prediction"]) == prediction


def test_lost_response_lookup_identifies_existing_submission(client, store):
    url = "/api/v1/projects/p/inference-requests/request-once"
    assert client.get(url).json() == {"submitted": False, "job_id": None}
    job_id, _, _ = finished_result(client, store, "request-once")
    assert client.get(url).json() == {"submitted": True, "job_id": job_id}


def test_a_to_b_adoption_retry_and_old_suggestion_preserve_b(client, store):
    job_id, asset_id, prediction = finished_result(client, store)
    url = f"/api/v1/jobs/{job_id}"
    row = client.get(url + "/results").json()["items"][0]
    manual = decision(client, [row["id"]], identity="manualB")
    assert client.post(url + "/dispositions", json=manual).status_code == 200
    adoption = decision(client, [row["id"]], revision=1, identity="adoptB", intent="adopt")
    response = client.post(url + "/dispositions", json=adoption)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["revisions"] == {row["id"]: 2}
    assert receipt["items"][0]["previous"]["human_label_id"] == "b"
    assert receipt["items"][0]["prediction"] == prediction
    assert receipt["items"][0]["original_sha256"] == store.one("SELECT sha256 FROM assets WHERE id=?", (asset_id,))["sha256"]
    # A lost response is queried by its original operation and replayed before CAS.
    assert lookup(client, "operation_adoptB").json() == receipt
    assert client.post(url + "/dispositions", json=adoption).json() == receipt
    changed = {**adoption, "note": "different"}
    assert client.post(url + "/dispositions", json=changed).status_code == 409
    accept_old = decision(client, [row["id"]], label_id="a", revision=2, identity="oldA", intent="accept_prediction")
    assert client.post(url + "/dispositions", json=accept_old).status_code == 409
    current = client.get(url + "/results").json()["items"][0]
    assert current["human_label_id"] == "b" and current["disposition_revision"] == 2
    assert current["prediction"] == prediction
    assert store.one("SELECT label_id FROM assets WHERE id=?", (asset_id,))["label_id"] == "b"
    history = client.get(url + f"/results/{row['id']}/dispositions").json()["items"]
    assert len(history) == 2 and history[-1]["operation_id"] == "operation_adoptB"
    assert history[-1]["actor"] == ACTOR


def test_conditional_identity_is_required_and_different_window_cannot_overwrite(client, store):
    job_id, _, _ = finished_result(client, store)
    url = f"/api/v1/jobs/{job_id}"
    row = client.get(url + "/results").json()["items"][0]
    original = decision(client, [row["id"]], identity="window1")
    for name in ("actor", "operation_id", "request_id", "expected_revisions", "instance_id"):
        missing = {key: value for key, value in original.items() if key != name}
        assert client.post(url + "/dispositions", json=missing).status_code == 422
    assert client.post(url + "/dispositions", json={**original, "instance_id": "otherInstance"}).status_code == 409
    assert client.post(url + "/dispositions", json=original).status_code == 200
    stale = decision(client, [row["id"]], label_id="a", identity="window2")
    assert client.post(url + "/dispositions", json=stale).status_code == 409
    reused_operation = {**original, "request_id": "request_other"}
    assert client.post(url + "/dispositions", json=reused_operation).status_code == 409
    assert lookup(client, "neverExecuted").json() == {
        "operation_id": "neverExecuted", "state": "not_found"}
    assert store.one("SELECT COUNT(*) count FROM result_disposition_events")["count"] == 1


def test_modified_original_stops_adoption_without_event(client, store):
    from pathlib import Path
    job_id, asset_id, _ = finished_result(client, store)
    url = f"/api/v1/jobs/{job_id}"
    row = client.get(url + "/results").json()["items"][0]
    assert client.post(url + "/dispositions", json=decision(client, [row["id"]])).status_code == 200
    asset = store.one("SELECT * FROM assets WHERE id=?", (asset_id,))
    Path(asset["path"]).write_bytes(b"modified original")
    response = client.post(url + "/dispositions", json=decision(client, [row["id"]],
                           revision=1, identity="changedSource", intent="adopt"))
    assert response.status_code == 409
    assert store.one("SELECT COUNT(*) count FROM result_disposition_events")["count"] == 1
    assert store.disposition_operation("p", "operation_changedSource") is None


def test_restarted_instance_queries_original_receipt_without_authorizing_old_post(client, store):
    from fastapi.testclient import TestClient
    from image_platform.api import create_app
    job_id, _, _ = finished_result(client, store)
    url = f"/api/v1/jobs/{job_id}"
    item = client.get(url + "/results").json()["items"][0]
    payload = decision(client, [item["id"]], identity="beforeRestart")
    receipt = client.post(url + "/dispositions", json=payload).json()
    with TestClient(create_app(store.settings), base_url="http://127.0.0.1:8765",
                    headers={"X-Platform-Request": "1"}) as restarted:
        assert restarted.get("/api/v1/health").json()["instance_id"] != payload["instance_id"]
        readback = lookup(restarted, "operation_beforeRestart", payload["instance_id"])
        assert readback.status_code == 200 and readback.json() == receipt
        assert lookup(restarted, "operation_beforeRestart").status_code == 409
        assert restarted.post(url + "/dispositions", json=payload).status_code == 409
    assert store.one("SELECT COUNT(*) count FROM result_disposition_events")["count"] == 1
