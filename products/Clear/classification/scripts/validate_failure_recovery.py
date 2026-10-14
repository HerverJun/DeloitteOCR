"""Check mixed real-ONNX batches and failed-item retries in an isolated temporary database."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from image_platform.api import create_app
from image_platform.datasets import register_asset
from image_platform.images import file_sha
from image_platform.runtime import validate_package
from image_platform.settings import Settings
from image_platform.store import Store, dump, now, uid
from image_platform.worker import run_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    metadata = validate_package(package)
    original = args.image.read_bytes()
    scratch = Path(".test-data")
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mixed-batch-", dir=scratch) as temp:
        settings = Settings(Path(temp).resolve())
        store = Store(settings)
        project_id, model_id = metadata["project_id"], metadata["model_version_id"]
        with store.connect(write=True) as con:
            con.execute("INSERT INTO projects(id,name,labels,created_at) VALUES(?,?,?,?)", (project_id, "isolated integration", dump(metadata["labels"]), now()))
            con.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?)", (model_id, project_id, "verification", "verified model", str(package), dump(metadata), "{}", now()))
        for name in ("good", "changed", "missing"):
            path = settings.data_dir / f"{name}.png"
            path.write_bytes(original)
            register_asset(store, project_id, path, None, "integration")
        items = store.rows("SELECT id,path,sha256 FROM assets WHERE project_id=?", (project_id,))
        (settings.data_dir / "changed.png").write_bytes(b"changed since registration")
        (settings.data_dir / "missing.png").unlink()
        bad = settings.data_dir / "corrupt.png"
        bad.write_bytes(b"not an image")
        items.append({"id": uid("asset"), "path": str(bad), "sha256": file_sha(bad)})
        params = {"model_id": model_id, "model_sha256": metadata["provenance"]["model_onnx_sha256"], "device": "cuda", "batch_size": 4, "auto_accept": False}
        first_id = store.submit(project_id, "inference", params, items)
        run_job(store, store.claim("isolated-verification"))
        first = store.job(first_id)
        assert (first["state"], first["done"], first["failed"]) == ("completed_with_errors", 1, 3), first
        prior = store.rows("SELECT id,prediction,attempts FROM job_items WHERE job_id=? AND state='succeeded'", (first_id,))
        for name in ("changed", "missing"):
            (settings.data_dir / f"{name}.png").write_bytes(original)
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8765", headers={"X-Platform-Request": "1"}) as client:
            response = client.post(f"/api/v1/jobs/{first_id}/actions", json={"action": "retry_failed"})
            assert response.status_code == 200, response.text
            retry_id = response.json()["job_id"]
            assert store.job(retry_id)["total"] == 3
            run_job(store, store.claim("isolated-verification"))
            retried = store.job(retry_id)
            assert (retried["state"], retried["done"], retried["failed"]) == ("completed_with_errors", 2, 1), retried
            assert prior == store.rows("SELECT id,prediction,attempts FROM job_items WHERE job_id=? AND state='succeeded'", (first_id,))
            exported = client.get(f"/api/v1/jobs/{retry_id}/export?format=jsonl").text.strip().splitlines()
            assert len(exported) == 3
        report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed", "device": "cuda", "model_id": model_id,
                  "initial": {"inputs": 4, "succeeded": 1, "failed": 3, "failures": ["content changed", "missing file", "invalid image"]},
                  "retry": {"inputs": 3, "succeeded": 2, "failed": 1, "successful_original_row_unchanged": True},
                  "scope": "Real ONNX inference with synthetic input copies and an isolated disposable database."}
        Path("reports/mixed-failure-recovery.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
