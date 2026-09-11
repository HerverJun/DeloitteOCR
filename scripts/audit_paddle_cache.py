"""Measure an unused CUDA cache release without changing weights or decoding.

An exploratory engineering run, not a replacement for the full benchmark.
Invoke with the packaged paddlevl Python after other GPU audits have stopped.
"""

import argparse
import gc
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_load_cycles import gpu
from audit_fidelity import differences
from ocr_workbench.offline import configure, install_guard
from ocr_workbench.engine_host import Resident
from ocr_workbench.worker import run_image


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--benchmark", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--empty-cache", action="store_true")
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("Fresh diagnostic output required")
    a.output.mkdir(parents=True, exist_ok=True)
    configure(a.output)
    install_guard(a.output / "network-blocked.log")
    samples = {s["id"]: s for s in json.loads(a.benchmark.read_text("utf-8"))["samples"]}
    lock_path = Path(os.environ["LOCALAPPDATA"]) / "OfflineOCR/gpu.lock"
    stop = threading.Event()
    readings = []
    def monitor():
        while not stop.is_set():
            readings.append(gpu()["used_mib"])
            stop.wait(0.5)
    report = {"complete": False, "empty_cache": a.empty_cache, "scope": "Ten preselected complex/varied table crops. Same precision, pixels, weights and generation parameters. Compare raw model output to the completed baseline; measure live vs reserved Paddle memory. Not a full 200-image rerun or A4000 test.", "records": []}
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        import paddle
        session = None
        thread = threading.Thread(target=monitor)
        thread.start()
        try:
            session = Resident(a.bundle, "paddlevl", a.output)
            for key in ["0161", "0162", "0163", "0164", "0165", "0169", "0172", "0174", "0179", "0195"]:
                image = a.benchmark.parent / samples[key]["image"]
                assert hashlib.sha256(image.read_bytes()).hexdigest() == samples[key]["image_sha256"]
                out = a.output / key
                started = time.monotonic()
                run_image(SimpleNamespace(bundle=a.bundle, engine="paddlevl", image=image, output=out), session)
                before = {"allocated": paddle.device.cuda.memory_allocated(), "reserved": paddle.device.cuda.memory_reserved(), "device_mib": gpu()["used_mib"]}
                if a.empty_cache:
                    gc.collect()
                    paddle.device.cuda.empty_cache()
                after = {"allocated": paddle.device.cuda.memory_allocated(), "reserved": paddle.device.cuda.memory_reserved(), "device_mib": gpu()["used_mib"]}
                baseline = json.loads((a.baseline / "paddlevl" / key / "result.json").read_text("utf-8"))["raw"]
                result = json.loads((out / "result.json").read_text("utf-8"))["raw"]
                diff = differences(baseline, result)
                record = {"id": key, "seconds": time.monotonic()-started, "before": before, "after": after, "raw_differences": diff}
                report["records"].append(record)
                print(json.dumps({k:v for k,v in record.items() if k!='raw_differences'}), flush=True)
            report["complete"] = True
        finally:
            if session:
                session.close()
            stop.set()
            thread.join(15)
            report["peak_device_mib"] = max(readings, default=None)
            report["gpu_samples"] = readings
            (a.output / "cache-diagnostic.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    main()
