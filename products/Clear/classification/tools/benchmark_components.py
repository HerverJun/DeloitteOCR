"""Read-only warm component benchmark for the existing synthetic demo models.

Measures ORT session startup, host-array-to-host-array runs, and image preprocessing
separately. Does not measure real-business accuracy or full application throughput.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time

import numpy as np

from image_platform.images import preprocess
from image_platform.runtime import session_for, validate_package
from image_platform.settings import Settings
from image_platform.store import Store


def main():
    store = Store(Settings.from_env())
    active = store.rows("SELECT id FROM jobs WHERE state IN ('running','queued')")
    if active:
        raise RuntimeError("Run this benchmark with no queued or running platform jobs")
    output = {"checked_at": datetime.now(timezone.utc).isoformat(), "precision": "FP32, TF32 disabled",
              "runtime": "ONNX Runtime CUDA; ordinary session.run with NumPy input/output",
              "warmup_runs": 5, "measured_runs": 30, "models": [],
              "limitations": ["Synthetic images only; warm OS file cache.", "ORT run includes host/device copies but excludes decoding, database writes and queue wait.",
                              "Sequential experiment on one machine, not a comparison to downloaded competitor weights.", "GPU memory and long-run latency have not been measured."]}
    for report in sorted(Path("reports").glob("pipeline-*.json")):
        original = json.loads(report.read_text(encoding="utf-8"))
        package = store.settings.data_dir / "models" / original["model_id"]
        metadata = validate_package(package)
        assets = store.rows("SELECT path,sha256 FROM assets WHERE project_id=? AND archived=0 ORDER BY id", (metadata["project_id"],))
        assert len(assets) == 60, "This benchmark expects the 60-image synthetic demo"
        size = metadata["input"]["height"]

        def prepare(item):
            return preprocess(item["path"], item["sha256"], size, size, metadata["preprocessing"])

        prep = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for _ in range(6):
                started = time.perf_counter()
                arrays = list(pool.map(prepare, assets))
                prep.append(time.perf_counter() - started)
        before = time.perf_counter()
        session = session_for(package / "model.onnx", require_gpu=True)
        cold_session_seconds = time.perf_counter() - before
        batches = []
        for batch_size in (1, 16, 32):
            batch = np.stack(arrays[:batch_size])
            for _ in range(5):
                session.run(["logits"], {"images": batch})
            times = []
            for _ in range(30):
                started = time.perf_counter()
                logits = session.run(["logits"], {"images": batch})[0]
                assert np.isfinite(logits).all()
                times.append(time.perf_counter() - started)
            batches.append({"batch_size": batch_size, "p50_batch_ms": float(np.quantile(times, 0.5) * 1000),
                            "p95_batch_ms": float(np.quantile(times, 0.95) * 1000),
                            "aggregate_images_per_second": batch_size * len(times) / sum(times)})
        record = {"model_id": original["model_id"], "encoder": original["encoder"], "input_size": size,
                  "onnx_bytes": (package / "model.onnx").stat().st_size,
                  "session_initialization_seconds": cold_session_seconds,
                  "preprocess_60_p50_seconds": statistics.median(prep[1:]), "batches": batches}
        output["models"].append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        del session
    destination = Path("reports/component-benchmark.json")
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
