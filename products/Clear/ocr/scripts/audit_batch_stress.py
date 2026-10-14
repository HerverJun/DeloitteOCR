"""500 distinct synthetic images through the real durable service queue."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import psutil
from PIL import Image, ImageDraw, ImageEnhance

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_application import Application, until
from audit_load_cycles import gpu


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=500)
    p.add_argument("--engine", default="ppocr")
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("Fresh evidence directory required")
    a.output.mkdir(parents=True)
    fixtures = a.output / "fixtures"
    fixtures.mkdir()
    app = Application(a.bundle.resolve(), a.output.resolve())
    report = {
        "passed": False,
        "bundle_manifest_sha256": hashlib.sha256((a.bundle / "manifest.json").read_bytes()).hexdigest(),
        "count": a.count,
        "engine": a.engine,
        "samples": [],
        "input_sha256": [],
        "sample_kind": "distinct synthetic stress fixtures, not the annotated real-image accuracy dataset",
        "gpu": gpu(),
    }
    started = time.monotonic()
    try:
        app.start()
        project = app.api("/projects", "POST", {"name": "500 张压力验收"})["id"]
        versions = []
        with Image.open(a.bundle / "fixtures/table.png") as base:
            for i in range(a.count):
                image = ImageEnhance.Contrast(base.convert("RGB")).enhance(
                    0.9 + (i % 21) / 100
                )
                ImageDraw.Draw(image).text((20, 20), f"STRESS {i+1:04d}", fill="black")
                path = fixtures / f"{i+1:04d}.png"
                image.save(path)
                report["input_sha256"].append(
                    hashlib.sha256(path.read_bytes()).hexdigest()
                )
                value = app.import_file(project, path)
                assert not value["errors"]
                versions.append(value["images"][0]["active_version"])
        assert len(set(report["input_sha256"])) == a.count
        corrupt = fixtures / "corrupt.png"
        corrupt.write_bytes(b"corrupt-image")
        assert app.import_file(project, corrupt)["errors"]
        # A PNG header declaring over 80 MP is enough to reject before allocation.
        import struct, zlib

        def chunk(kind, body):
            return (
                struct.pack(">I", len(body))
                + kind
                + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
            )

        oversized = fixtures / "oversized.png"
        oversized.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 10000, 9000, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b""))
            + chunk(b"IEND", b"")
        )
        assert app.import_file(project, oversized)["errors"]
        report["invalid_imports_rejected"] = True
        tasks = app.api(
            "/projects/" + project + "/tasks",
            "POST",
            {
                "version_ids": versions,
                "engines": [a.engine],
                "preprocess": [{"kind": "contrast", "factor": 1.1}],
            },
        )["task_ids"]
        queue_started = time.monotonic()
        deadline = queue_started + 7200
        last = -1
        while time.monotonic() < deadline:
            state = app.api("/projects/" + project)
            done = sum(t["status"] == "succeeded" for t in state["tasks"])
            failed = [t for t in state["tasks"] if t["status"] == "failed"]
            parent = psutil.Process(app.state["service_pid"])
            children = parent.children(recursive=True)
            memory = []
            for process in [parent, *children]:
                try:
                    memory.append((process.pid, process.memory_info()))
                except psutil.Error:
                    pass
            sample = {
                "seconds": time.monotonic() - queue_started,
                "completed": done,
                "failed": len(failed),
                "service_private": getattr(
                    parent.memory_info(), "private", parent.memory_info().rss
                ),
                "tree_private": sum(getattr(m, "private", m.rss) for _, m in memory),
                "gpu_mib": gpu()["used_mib"],
                "processes": [pid for pid, _ in memory],
            }
            report["samples"].append(sample)
            if done // 25 != last:
                print(json.dumps(sample), flush=True)
                last = done // 25
                (a.output / "stress-progress.json").write_text(
                    json.dumps(
                        {"completed": done, "failed": len(failed), "count": a.count}
                    ),
                    "utf-8",
                )
            if done + len(failed) == a.count:
                break
            time.sleep(1)
        else:
            raise TimeoutError("500-image queue deadline exceeded")
        report["queue_seconds"] = time.monotonic() - queue_started
        assert done == a.count, [(t["id"], t["error"]) for t in failed]
        assert len({t["result_id"] for t in state["tasks"]}) == a.count
        for task in state["tasks"]:
            result = app.api("/results/" + task["result_id"])
            assert "00123456789012345678" in result["original"]["text"]
        # Compare warmed-up 50-image windows; retain the raw series for slope review.
        windows = []
        for start in range(100, a.count - 49, 50):
            group = [
                s for s in report["samples"] if start <= s["completed"] < start + 50
            ]
            if group:
                windows.append(
                    {
                        "from": start,
                        "tree_private_median": statistics.median(
                            s["tree_private"] for s in group
                        ),
                        "service_private_median": statistics.median(
                            s["service_private"] for s in group
                        ),
                    }
                )
        report["memory_windows"] = windows
        if len(windows) >= 4:
            growth = (
                windows[-1]["tree_private_median"] - windows[0]["tree_private_median"]
            )
            service_growth = (
                windows[-1]["service_private_median"]
                - windows[0]["service_private_median"]
            )
            report["warmed_tree_growth_bytes"] = growth
            report["warmed_service_growth_bytes"] = service_growth
            report["memory_growth_threshold_bytes"] = 128 * 1024 * 1024
            assert (
                growth < 128 * 1024 * 1024 and service_growth < 32 * 1024 * 1024
            ), "Sustained memory growth requires investigation"
        report["throughput_images_per_minute"] = 60 * a.count / report["queue_seconds"]
        report["sampled_peak_gpu_mib"] = max(s["gpu_mib"] for s in report["samples"])
        until(lambda: app.api("/state")["queue"]["engine"] is None, 30)
        final_project = app.api("/projects/" + project)
        report["project_id"] = project
        report["completed_tasks"] = len(
            [t for t in final_project["tasks"] if t["status"] == "succeeded"]
        )
        app.stop()
        report["passed"] = True
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        try:
            app.stop()
        finally:
            report["wall_seconds"] = time.monotonic() - started
            (a.output / "batch-stress.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
            )


if __name__ == "__main__":
    main()
