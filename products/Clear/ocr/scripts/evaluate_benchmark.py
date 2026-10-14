"""Run every annotated image with each real engine; durable per-image evidence."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import threading
import time
from ocr_workbench.adapter import EngineAdapter
from ocr_workbench.tables import parse_tables

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_load_cycles import gpu
from benchmark_metrics import text_metrics, table_metrics


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--benchmark", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--engines", nargs="+", default=["ppocr", "paddlevl", "glm", "hunyuan"]
    )
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(a.benchmark.read_text("utf-8"))
    samples = manifest["samples"]
    assert len(samples) >= 200 and len({s["image_sha256"] for s in samples}) == len(
        samples
    )
    assert all(s["kind"] in {"print", "handwriting", "table"} for s in samples)
    for s in samples:
        assert digest(a.benchmark.parent / s["image"]) == s["image_sha256"]
    binding = {
        "benchmark_sha256": digest(a.benchmark),
        "bundle_manifest_sha256": digest(a.bundle / "manifest.json"),
    }
    path = a.output / "evaluation.json"
    report = (
        json.loads(path.read_text("utf-8"))
        if path.exists()
        else {
            **binding,
            "complete": False,
            "gpu": gpu(),
            "scope": manifest["scope"],
            "engines": {},
            "metrics": "CER = corpus Levenshtein edits/reference characters after NFC and removal of Unicode whitespace; strict NFC CER also retained. No punctuation/case/model-output repairs. Text fields are predeclared numeric tokens >=4 digits. Table fields are numeric-containing cells matched by table/row/column. Span F1 matches exact (table,row,column,rowspan,colspan); not TEDS. Failed text items receive empty hypothesis; failed structured items empty tables. PP-OCR structural capability is unsupported, not scored.",
            "limitations": "Region crops, original public annotations, no independent transcription correction; some annotation boxes include neighboring fragments. Research-only dataset kept separate from distributable bundle. Not intranet, smartphone-photo or A4000 acceptance.",
        }
    )
    assert all(
        report[k] == v for k, v in binding.items()
    ), "Cannot mix evidence from different versions"

    def save():
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        pending.replace(path)

    for engine in a.engines:
        group = report["engines"].setdefault(engine, {"records": [], "loads": []})
        done = {r["id"] for r in group["records"]}
        pending = [s for s in samples if s["id"] not in done]
        if not pending:
            continue
        adapter = EngineAdapter(a.bundle, engine, a.output / "sessions")
        stop = threading.Event()
        measured = []

        def monitor():
            while not stop.is_set():
                try:
                    measured.append(gpu()["used_mib"])
                except Exception:
                    pass
                stop.wait(0.5)

        monitor_thread = threading.Thread(target=monitor)
        monitor_thread.start()
        try:
            for s in pending:
                output = a.output / engine / s["id"]
                output.mkdir(parents=True, exist_ok=True)
                record = {
                    "id": s["id"],
                    "kind": s["kind"],
                    "image_sha256": s["image_sha256"],
                    "succeeded": False,
                }
                started = time.monotonic()
                result = {}
                try:
                    if not adapter.ready:
                        cold = time.monotonic()
                        adapter.load()
                        group["loads"].append(
                            {
                                "wall_seconds": time.monotonic() - cold,
                                "model_load_seconds": adapter.load_seconds,
                            }
                        )
                    inference = time.monotonic()
                    result = adapter.recognize(a.benchmark.parent / s["image"], output)
                    record.update(
                        succeeded=True,
                        inference_wall_seconds=time.monotonic() - inference,
                        result_sha256=digest(output / "result.json"),
                        result=str(output / "result.json"),
                    )
                except Exception as error:
                    record["error"] = str(error)
                    adapter.unload()
                    adapter = EngineAdapter(a.bundle, engine, a.output / "sessions")
                record["wall_seconds"] = time.monotonic() - started
                if s["kind"] == "table":
                    record["metrics"] = (
                        table_metrics(
                            parse_tables(s["reference"]), result.get("tables", [])
                        )
                        if engine != "ppocr"
                        else {"unsupported": True}
                    )
                else:
                    record["metrics"] = text_metrics(
                        s["reference"], result.get("text", "")
                    )
                group["records"].append(record)
                group["sampled_peak_device_mib"] = max(
                    [group.get("sampled_peak_device_mib") or 0, *measured]
                )
                save()
                if len(group["records"]) % 10 == 0 or not record["succeeded"]:
                    print(
                        json.dumps(
                            {
                                "engine": engine,
                                "completed": len(group["records"]),
                                "total": len(samples),
                                "id": s["id"],
                                "success": record["succeeded"],
                            }
                        ),
                        flush=True,
                    )
        finally:
            adapter.unload()
            stop.set()
            monitor_thread.join(15)
            group.setdefault("gpu_sample_runs", []).append(measured)
            group["after_unload_device_mib"] = gpu()["used_mib"]
            save()
    report["complete"] = all(
        len(report["engines"].get(e, {}).get("records", [])) == len(samples)
        for e in ["ppocr", "paddlevl", "glm", "hunyuan"]
    )
    for engine, group in report["engines"].items():
        summary = {}
        for kind in ["print", "handwriting", "table"]:
            rows = [r for r in group["records"] if r["kind"] == kind]
            metrics = [r["metrics"] for r in rows]
            item = {
                "count": len(rows),
                "failures": sum(not r["succeeded"] for r in rows),
            }
            if kind == "table":
                if engine == "ppocr":
                    item["unsupported"] = True
                else:
                    item["mean_span_f1"] = statistics.mean(
                        m["span_f1"] for m in metrics
                    )
                    item["structure_exact_rate"] = statistics.mean(
                        m["structure_exact"] for m in metrics
                    )
            else:
                item["cer"] = sum(m["edit_distance"] for m in metrics) / sum(
                    m["reference_characters"] for m in metrics
                )
                item["strict_cer"] = sum(
                    m["strict_edit_distance"] for m in metrics
                ) / sum(m["strict_reference_characters"] for m in metrics)
            total = sum(m.get("numeric_fields", 0) for m in metrics)
            item["numeric_fields"] = total
            item["numeric_field_exact_rate"] = (
                sum(m.get("numeric_fields_correct", 0) for m in metrics) / total
                if total
                else None
            )
            summary[kind] = item
        durations = [
            r["inference_wall_seconds"] for r in group["records"] if r["succeeded"]
        ]
        summary["median_inference_seconds"] = (
            statistics.median(durations) if durations else None
        )
        summary["images_per_minute_including_loads_and_failures"] = (
            60
            * len(group["records"])
            / sum(r["wall_seconds"] for r in group["records"])
        )
        group["summary"] = summary
    save()


if __name__ == "__main__":
    main()
