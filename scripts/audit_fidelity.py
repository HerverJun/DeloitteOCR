"""Compare wrapped output to independent direct upstream APIs with identical inputs."""

import argparse
import hashlib
import json
import math
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
from ocr_workbench.adapter import EngineAdapter
from ocr_workbench.processes import ProcessJob


def differences(left, right, path="$"):
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return [
                {
                    "path": path,
                    "reason": "keys differ",
                    "left": sorted(left),
                    "right": sorted(right),
                }
            ]
        return [
            d
            for key in left
            for d in differences(left[key], right[key], path + "." + key)
        ]
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [
                {
                    "path": path,
                    "reason": "length differs",
                    "left": len(left),
                    "right": len(right),
                }
            ]
        return [
            d
            for i, (a, b) in enumerate(zip(left, right))
            for d in differences(a, b, f"{path}[{i}]")
        ]
    if (
        isinstance(left, float)
        and isinstance(right, (int, float))
        and math.isclose(left, right, rel_tol=0, abs_tol=1e-5)
    ):
        return []
    return [] if left == right else [{"path": path, "left": left, "right": right}]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("Fresh evidence directory required")
    a.output.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "scope": "Two engineering fixtures, all four engines. Direct upstream APIs with identical local weights, fixed precision, image pixels, prompt and generation settings; not a claim of upstream default-parameter accuracy.",
        "comparison": "All Paddle raw fields and GLM SDK regions/generated/formatted results; float tolerance 1e-5 only. Hunyuan content and finish_reason compared; request ID, timestamp and timings are nondeterministic metadata.",
        "reference_script_sha256": hashlib.sha256(
            Path(__file__).with_name("official_reference.py").read_bytes()
        ).hexdigest(),
        "bundle_manifest_sha256": hashlib.sha256(
            (a.bundle / "manifest.json").read_bytes()
        ).hexdigest(),
        "engines": [],
    }
    try:
        for engine in ["ppocr", "paddlevl", "glm", "hunyuan"]:
            root = a.output / engine
            root.mkdir()
            adapter = EngineAdapter(a.bundle, engine, a.output / "sessions")
            wrapped = []
            images = []
            try:
                adapter.load()
                for i, name in enumerate(["printed", "table"]):
                    out = root / f"wrapped-{i}"
                    out.mkdir()
                    result = adapter.recognize(
                        a.bundle / "fixtures" / f"{name}.png", out
                    )
                    wrapped.append(result["raw"])
                    images.append(out / "input.png")
            finally:
                adapter.unload()
            reference = root / "reference"
            reference.mkdir()
            runtime = a.bundle / "runtimes" / engine / "python.exe"
            env = os.environ.copy()
            env["PATH"] = (
                str(runtime.parent)
                + os.pathsep
                + str(Path(os.environ["SystemRoot"]) / "System32")
            )
            for key in ["PYTHONHOME", "PYTHONPATH", "CUDA_HOME", "CUDA_PATH"]:
                env.pop(key, None)
            command = [
                str(runtime),
                "-B",
                "-X",
                "utf8",
                "-I",
                str(Path(__file__).with_name("official_reference.py")),
                "--bundle",
                str(a.bundle),
                "--engine",
                engine,
                "--images",
                *map(str, images),
                "--output",
                str(reference),
            ]
            lock_path = Path(os.environ["LOCALAPPDATA"]) / "OfflineOCR/gpu.lock"
            with lock_path.open("a+b") as lock, (root / "reference.log").open(
                "w", encoding="utf-8"
            ) as log:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                job = ProcessJob()
                try:
                    process = subprocess.Popen(
                        command,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    job.assign(process)
                    code = process.wait(1200)
                    if code:
                        raise RuntimeError(
                            f'{engine} direct reference failed: {root / "reference.log"}'
                        )
                finally:
                    job.close()
            record = {"engine": engine, "passed": True, "samples": []}
            for i, left in enumerate(wrapped):
                right = json.loads((reference / f"{i}.json").read_text("utf-8"))
                if engine == "hunyuan":
                    left = {
                        k: left["choices"][0][k] for k in ["message", "finish_reason"]
                    }
                    right = {
                        k: right["choices"][0][k] for k in ["message", "finish_reason"]
                    }
                diff = differences(left, right)
                record["samples"].append(
                    {
                        "image_sha256": hashlib.sha256(
                            images[i].read_bytes()
                        ).hexdigest(),
                        "differences": diff,
                        "passed": not diff,
                    }
                )
                record["passed"] &= not diff
            report["engines"].append(record)
            print(
                json.dumps(
                    {
                        "engine": engine,
                        "passed": record["passed"],
                        "differences": sum(
                            len(s["differences"]) for s in record["samples"]
                        ),
                    }
                ),
                flush=True,
            )
        report["passed"] = all(e["passed"] for e in report["engines"])
    finally:
        (a.output / "fidelity.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
        )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
