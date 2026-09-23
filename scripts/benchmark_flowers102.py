"""Run the frozen public-data protocol with persistent jobs and resumable phases.

Stop the interactive worker before running: this process owns the same worker lease.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import time

import numpy as np

from image_platform.datasets import load_dataset
from image_platform.images import file_sha
from image_platform.runtime import WORKER_SESSIONS
from image_platform.settings import Settings
from image_platform.store import Store
from image_platform import worker

PROJECT = "project_flowers102_official"
STATE_PATH = Path("reports/flowers102-progress.json")


def save(state):
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def archived_module(name, path):
    spec = importlib.util.spec_from_file_location("image_platform." + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(store, state, key, kind, params, items=(), runner=worker):
    previous = state["runs"].get(key)
    if previous:
        job = store.job(previous["job_id"])
        if job["state"] == "succeeded":
            return job
        if job["state"] != "queued":
            raise RuntimeError(f"Existing {key} is {job['state']}: {job['error']}; inspect before retrying")
        job_id = job["id"]
    else:
        job_id = store.submit(PROJECT, kind, params, items)
        # All repetitions consume the identical SHA-sorted order, independent of UUIDs.
        if kind == "inference":
            with store.connect(write=True) as con:
                for index, item in enumerate(items):
                    con.execute("UPDATE job_items SET id=? WHERE job_id=? AND asset_id=?", (f"{job_id}_{index:06d}", job_id, item["id"]))
        state["runs"][key] = {"job_id": job_id, "params": params, "source_sha256": state.get("active_source_sha256")}
        save(state)
    job = store.claim("flowers102-protocol")
    if not job or job["id"] != job_id:
        raise RuntimeError("Another worker/job owns the queue; benchmark cannot claim expected job")
    print("START", key, job_id, flush=True)
    started = time.perf_counter()
    runner.run_job(store, job)
    elapsed = time.perf_counter() - started
    result = store.job(job_id)
    if result["state"] != "succeeded":
        raise RuntimeError(f"{key}: {result['error']}; see .local-data/logs/{job_id}.log")
    state["runs"][key]["wall_seconds"] = elapsed
    state["runs"][key]["state"] = result["state"]
    if kind == "train":
        state["runs"][key]["model_id"] = result["result"]["model_id"]
        val = result["result"]["metrics"]["validation"]
        print(f"DONE {key}: validation accuracy={val['accuracy']:.4f}, macroF1={val['macro_f1']:.4f}", flush=True)
    else:
        if result["done"] != len(items) or result["failed"]:
            raise AssertionError("Incomplete inference ledger")
        state["runs"][key]["result"] = result["result"]
        print(f"DONE {key}: {len(items) / elapsed:.1f} images/s including startup", flush=True)
    save(state)
    return result


def train_models(store, state, encoders, method, seeds):
    dataset = store.one("SELECT id FROM datasets WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (PROJECT,))
    for encoder in encoders:
        for seed in seeds:
            key = f"train/{encoder}/{method}/{seed}"
            execute(store, state, key, "train", {"dataset_id": dataset["id"], "encoder": encoder, "head_method": method,
                    "input_size": 224, "device": "cuda", "epochs": 100, "batch_size": 32, "seed": seed, "minimum_precision": 0.95})


def benchmark_speed(store, state, encoders):
    old = archived_module("baseline_worker", "benchmarks/baseline-v0.1/worker.py")
    old.preprocess = archived_module("baseline_images", "benchmarks/baseline-v0.1/images.py").preprocess
    dataset = store.one("SELECT id FROM datasets WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (PROJECT,))
    _, snapshot = load_dataset(store, dataset["id"])
    items = sorted((item for item in snapshot["items"] if item["split"] == "test"), key=lambda item: item["sha256"])[:1024]
    # Dataset manifests use asset_id; store.submit expects id.
    items = [{**item, "id": item.get("asset_id", item.get("id"))} for item in items]
    state["performance_inputs"] = [item["sha256"] for item in items]
    for encoder in encoders:
        model_id = state["runs"][f"train/{encoder}/adamw/42"]["model_id"]
        row = store.one("SELECT * FROM models WHERE id=?", (model_id,))
        metadata = json.loads(row["metadata"])
        params = {"model_id": model_id, "model_sha256": metadata["provenance"]["model_onnx_sha256"],
                  "batch_size": 32, "device": "cuda", "auto_accept": True, "scope": "protocol_fixed_test_subset"}
        reference = None
        for mode, runner in [("archived", old), ("optimized_cold", worker), ("optimized_warm", worker)]:
            for repetition in range(3):
                if mode != "optimized_warm":
                    WORKER_SESSIONS.clear()
                elif repetition == 0:
                    # Explicit warmup also makes a resumed run reproducible.
                    WORKER_SESSIONS.get(Path(row["package_path"]) / "model.onnx", params["model_sha256"], True)
                key = f"speed/{encoder}/{mode}/{repetition}"
                result = execute(store, state, key, "inference", params, items, runner)
                predictions = [(item["asset_id"], json.loads(item["prediction"])) for item in store.rows("SELECT asset_id,prediction FROM job_items WHERE job_id=? ORDER BY asset_id", (result["id"],))]
                if reference is None:
                    reference = predictions
                deviations = []
                for (asset, prediction), (ref_asset, ref) in zip(predictions, reference, strict=True):
                    assert asset == ref_asset and prediction["label_id"] == ref["label_id"] and prediction["status"] == ref["status"]
                    assert np.allclose(prediction["scores"], ref["scores"], atol=1e-6, rtol=1e-4)
                    deviations.append(float(np.max(np.abs(np.array(prediction["scores"]) - ref["scores"]))))
                state["runs"][key]["output_parity"] = {"labels_equal": True, "acceptance_equal": True, "max_score_difference": max(deviations)}
                save(state)
    WORKER_SESSIONS.clear()


def report(store, state):
    models = []
    for key, run in state["runs"].items():
        if not key.startswith("train/"):
            continue
        row = store.one("SELECT * FROM models WHERE id=?", (run["model_id"],))
        metrics = json.loads(row["metrics"])
        models.append({"key": key, "model_id": row["id"], "onnx_mib": (Path(row["package_path"]) / "model.onnx").stat().st_size / 1024**2,
                       "params": run["params"], "training_wall_seconds": run["wall_seconds"], "metrics": metrics})
    # Freeze recommendations using validation before reading test metrics for comparison.
    recommendations = {}
    for encoder in ["resnet18", "dinov2", "mobilenetv4", "repvit"]:
        candidates = [m for m in models if m["params"]["encoder"] == encoder]
        # Macro-F1 is predeclared quality metric, used for model-level recommendation.
        winner = max(candidates, key=lambda m: (m["metrics"]["validation"]["macro_f1"], m["metrics"]["validation"]["accuracy"]))
        recommendations[encoder] = winner["model_id"]
    performance = []
    for encoder in ["resnet18", "dinov2", "mobilenetv4", "repvit"]:
        for mode in ["archived", "optimized_cold", "optimized_warm"]:
            runs = [r for key, r in state["runs"].items() if key.startswith(f"speed/{encoder}/{mode}/")]
            rates = [1024 / r["wall_seconds"] for r in runs]
            performance.append({"encoder": encoder, "mode": mode, "whole_job_images_per_second": rates,
                                "median_images_per_second": statistics.median(rates), "runs": runs})
    output = {"protocol_sha256": file_sha("benchmarks/flowers102-protocol.json"), "dataset": json.loads(Path("reports/flowers102-import.json").read_text(encoding="utf-8")),
              "recommendation_rule": "Highest validation macro F1; ties use validation accuracy. Test never selects a candidate.",
              "recommendations": recommendations, "models": models, "performance": performance,
              "performance_source_sha256": state.get("active_source_sha256"),
              "limitations": json.loads(Path("benchmarks/flowers102-protocol.json").read_text(encoding="utf-8"))["limits"]}
    Path("reports/flowers102-benchmark.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Final aggregate report: reports/flowers102-benchmark.json", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["all", "baseline", "speed-baseline", "lightweight", "speed-lightweight", "accuracy", "report"], default="all")
    args = parser.parse_args()
    store = Store(Settings.from_env())
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"protocol_sha256": file_sha("benchmarks/flowers102-protocol.json"), "runs": {}}
    assert state["protocol_sha256"] == file_sha("benchmarks/flowers102-protocol.json")
    state["active_source_sha256"] = {name: file_sha(Path('src/image_platform') / name) for name in ['images.py', 'worker.py', 'runtime.py', 'training.py', 'evaluation.py', 'learning.py']}
    phases = {"baseline": lambda: train_models(store, state, ["resnet18", "dinov2"], "adamw", [42, 43, 44]),
              "speed-baseline": lambda: benchmark_speed(store, state, ["resnet18", "dinov2"]),
              "lightweight": lambda: train_models(store, state, ["mobilenetv4", "repvit"], "adamw", [42, 43, 44]),
              "speed-lightweight": lambda: benchmark_speed(store, state, ["mobilenetv4", "repvit"]),
              "accuracy": lambda: train_models(store, state, ["resnet18", "dinov2", "mobilenetv4", "repvit"], "logistic_cv", [42]),
              "report": lambda: report(store, state)}
    for name, operation in phases.items():
        if args.phase in ("all", name):
            operation()


if __name__ == "__main__":
    main()
