"""Twenty real load/infer/unload cycles, with process and sampled GPU evidence."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import psutil
from ocr_workbench.adapter import EngineAdapter


def gpu():
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=uuid,name,memory.total,memory.used,driver_version",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    columns = [x.strip() for x in result.stdout.splitlines()[0].split(",")]
    return {
        "uuid": columns[0],
        "name": columns[1],
        "total_mib": int(columns[2]),
        "used_mib": int(columns[3]),
        "driver": columns[4],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cycles", type=int, default=20)
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("Fresh output required")
    a.output.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "bundle_manifest_sha256": hashlib.sha256((a.bundle / "manifest.json").read_bytes()).hexdigest(),
        "gpu": gpu(),
        "cycles": [],
        "measurement": "GPU device total sampled every 0.5 s; includes desktop/other apps. Process private/RSS includes native descendants.",
    }
    for index in range(a.cycles):
        engine = ["ppocr", "paddlevl", "glm", "hunyuan"][index % 4]
        adapter = EngineAdapter(a.bundle, engine, a.output / "sessions")
        samples = []
        stop = threading.Event()

        def monitor():
            while not stop.is_set():
                try:
                    processes = []
                    if adapter.process and psutil.pid_exists(adapter.process.pid):
                        parent = psutil.Process(adapter.process.pid)
                        processes = [parent, *parent.children(recursive=True)]
                    memory = [p.memory_info() for p in processes]
                    samples.append(
                        {
                            "seconds": time.monotonic() - started,
                            "gpu": gpu()["used_mib"],
                            "rss": sum(m.rss for m in memory),
                            "private": sum(
                                getattr(m, "private", m.rss) for m in memory
                            ),
                        }
                    )
                except (psutil.Error, OSError, subprocess.SubprocessError) as error:
                    samples.append({"error": str(error)})
                stop.wait(0.5)

        started = time.monotonic()
        thread = threading.Thread(target=monitor)
        thread.start()
        record = {"index": index + 1, "engine": engine, "passed": False}
        try:
            adapter.load()
            record["load_seconds"] = adapter.load_seconds
            output = a.output / f"{index+1:02d}-{engine}"
            output.mkdir()
            result = adapter.recognize(a.bundle / "fixtures/table.png", output)
            process = psutil.Process(adapter.process.pid)
            pids = [process.pid, *[p.pid for p in process.children(recursive=True)]]
            assert "00123456789012345678" in result["text"]
            assert bool(result["tables"]) == (engine != "ppocr")
            record["inference_seconds"] = result["elapsed_seconds"]
            record["pids"] = pids
            adapter.unload()
            assert not any(
                psutil.pid_exists(pid) for pid in pids
            ), "Engine descendant remained after unload"
            record["after_unload_gpu_mib"] = gpu()["used_mib"]
            record["passed"] = True
        except BaseException as error:
            record["error"] = repr(error)
            raise
        finally:
            adapter.unload()
            stop.set()
            thread.join(15)
            record["wall_seconds"] = time.monotonic() - started
            record["samples"] = samples
            valid = [s for s in samples if "gpu" in s]
            record["sampled_peak_gpu_mib"] = max(
                (s["gpu"] for s in valid), default=None
            )
            record["sampled_peak_private_bytes"] = max(
                (s["private"] for s in valid), default=None
            )
            report["cycles"].append(record)
            report["passed"] = len(report["cycles"]) == a.cycles and all(
                c["passed"] for c in report["cycles"]
            )
            (a.output / "load-cycles.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
            )
            print(
                json.dumps(
                    {k: v for k, v in record.items() if k != "samples"},
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
