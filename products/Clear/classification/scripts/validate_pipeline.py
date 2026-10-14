"""Run real training, ONNX verification and inference using explicitly synthetic data."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from image_platform.demo import create_demo
from image_platform.settings import Settings
from image_platform.store import Store
from image_platform.worker import run_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder", choices=["resnet18", "dinov2"], default="resnet18")
    parser.add_argument("--size", type=int, choices=[224, 336], default=224)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--epochs", type=int, default=60)
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings)
    demo = create_demo(settings)
    project_id = demo["project_id"]
    dataset = store.one("SELECT id FROM datasets WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,))
    job_id = store.submit(project_id, "train", {"dataset_id": dataset["id"], "encoder": args.encoder, "input_size": args.size,
                          "device": args.device, "epochs": args.epochs, "batch_size": 16, "seed": 42, "minimum_precision": 0.95})
    print(f"Training {job_id} {args.encoder} {args.size} on {args.device}", flush=True)
    job = store.claim("pipeline-validation")
    if not job or job["id"] != job_id:
        raise RuntimeError("验证前请确保没有其他排队或运行中的任务")
    run_job(store, job)
    trained = store.job(job_id)
    if trained["state"] != "succeeded":
        raise RuntimeError(f"Training failed: {trained['error']}")
    model_id = trained["result"]["model_id"]
    model = store.one("SELECT * FROM models WHERE id=?", (model_id,))
    metadata = json.loads(model["metadata"])
    assets = store.rows("SELECT id,path,sha256 FROM assets WHERE project_id=? AND archived=0", (project_id,))
    inference_id = store.submit(project_id, "inference", {"model_id": model_id, "model_sha256": metadata["provenance"]["model_onnx_sha256"], "batch_size": 16, "device": args.device, "auto_accept": False, "scope": "all"}, assets)
    job = store.claim("pipeline-validation")
    if not job or job["id"] != inference_id:
        raise RuntimeError("推理验证无法取得预期任务租约")
    run_job(store, job)
    inferred = store.job(inference_id)
    assert inferred["state"] == "succeeded", inferred["error"]
    assert inferred["done"] == len(assets)
    assert inferred["failed"] == 0
    results = store.rows("SELECT prediction FROM job_items WHERE job_id=?", (inference_id,))
    assert all(json.loads(row["prediction"])["status"] == "needs_review" for row in results)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "synthetic": True,
              "warning": "This report validates software integration only. It is not evidence of business accuracy.",
              "encoder": args.encoder, "input_size": args.size, "device": args.device,
              "training_job_id": job_id, "inference_job_id": inference_id, "model_id": model_id,
              "metrics": json.loads(model["metrics"]), "inference": inferred["result"],
              "input_count": len(assets), "result_count": len(results), "status": "passed"}
    destination = Path("reports") / f"pipeline-{args.encoder}-{args.size}-{args.device}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "model_id": model_id, "report": str(destination), "inference": inferred["result"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
