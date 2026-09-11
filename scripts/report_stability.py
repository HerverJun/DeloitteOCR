"""Plot actual queue memory and load/unload measurements, with source hashes."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--cycles", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    batch = json.loads(a.batch.read_text("utf-8"))
    cycles = json.loads(a.cycles.read_text("utf-8"))
    assert batch["passed"] and cycles["passed"]
    assert batch["completed_tasks"] == 500 and len(cycles["cycles"]) == 20
    a.output.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    samples = batch["samples"]
    x = [s["completed"] for s in samples]
    for axis, field, label in [(axes[0, 0], "tree_private", "Process tree private memory (MiB)"), (axes[0, 1], "service_private", "Service private memory (MiB)")]:
        axis.plot(x, [s[field]/1024**2 for s in samples], color="#286b8c", linewidth=1.5)
        axis.axvline(100, color="#888", linestyle=":", label="Warm window begins")
        axis.set_xlabel("Completed images")
        axis.set_ylabel(label)
        axis.grid(alpha=0.2)
    axes[0, 0].legend()
    series = cycles["cycles"]
    axes[1, 0].plot([s["index"] for s in series], [s["sampled_peak_gpu_mib"] for s in series], "o-", color="#286b8c", label="Sampled peak")
    axes[1, 0].plot([s["index"] for s in series], [s["after_unload_gpu_mib"] for s in series], "o-", color="#b87036", label="After unload")
    axes[1, 0].set_ylabel("Total device memory (MiB)")
    axes[1, 0].set_xlabel("Sequential load/unload cycle")
    axes[1, 0].legend()
    engines = ["ppocr", "paddlevl", "glm", "hunyuan"]
    axes[1, 1].bar(engines, [statistics.median(s["load_seconds"] for s in series if s["engine"] == e) for e in engines], color="#286b8c")
    axes[1, 1].set_ylabel("Median reported model load (seconds)")
    fig.suptitle("500 synthetic queue images / 20 real engine cycles\nDevelopment RTX 4070 Ti SUPER; GPU readings include desktop")
    fig.savefig(a.output / "stability.png", dpi=180)
    plt.close(fig)
    report = {
        "passed": True,
        "scope": "Visualization of the supplied actual measurement receipts only; does not extend their machine, code-version, or sample coverage.",
        "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [a.batch, a.cycles]},
        "throughput_images_per_minute": batch["throughput_images_per_minute"],
        "warmed_tree_growth_mib": batch["warmed_tree_growth_bytes"]/1024**2,
        "warmed_service_growth_mib": batch["warmed_service_growth_bytes"]/1024**2,
        "engines": {e: {"cycles": 5, "peak_device_mib": max(s["sampled_peak_gpu_mib"] for s in series if s["engine"] == e), "after_unload_max_mib": max(s["after_unload_gpu_mib"] for s in series if s["engine"] == e)} for e in engines},
    }
    (a.output / "stability-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
