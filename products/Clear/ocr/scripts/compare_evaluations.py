"""Compare frozen benchmark receipts without distributing research text."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_fidelity import differences


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before, after = [json.loads(p.read_text("utf-8")) for p in [args.before, args.after]]
    if not all(d.get("complete") is True for d in [before, after]):
        raise ValueError("Both evaluations must be complete")
    if before["benchmark_sha256"] != after["benchmark_sha256"]:
        raise ValueError("Benchmark identities differ")
    report = {
        "passed": False,
        "scope": "Raw model output comparison; float absolute tolerance 1e-5. Hunyuan request identifiers/timestamps/timings excluded. Matching failures are reported separately, not counted as successful output comparisons. Difference values omitted to keep licensed transcriptions out of distribution.",
        "before_evaluation_sha256": sha(args.before),
        "after_evaluation_sha256": sha(args.after),
        "before_bundle_manifest_sha256": before["bundle_manifest_sha256"],
        "after_bundle_manifest_sha256": after["bundle_manifest_sha256"],
        "benchmark_sha256": before["benchmark_sha256"],
        "engines": {},
    }
    for engine in ["ppocr", "paddlevl", "glm", "hunyuan"]:
        groups = [d["engines"][engine] for d in [before, after]]
        rows = [{r["id"]: r for r in g["records"]} for g in groups]
        if set(rows[0]) != set(rows[1]) or any(len(r) != len(g["records"]) for r, g in zip(rows, groups)):
            raise ValueError("Sample identities or duplicate records differ")
        comparison = {
            "before_peak_device_mib": groups[0]["sampled_peak_device_mib"],
            "after_peak_device_mib": groups[1]["sampled_peak_device_mib"],
            "before_images_per_minute": groups[0]["summary"]["images_per_minute_including_loads_and_failures"],
            "after_images_per_minute": groups[1]["summary"]["images_per_minute_including_loads_and_failures"],
            "records": [],
        }
        for identifier in rows[0]:
            left, right = [r[identifier] for r in rows]
            if left["image_sha256"] != right["image_sha256"]:
                raise ValueError("Image changed: " + identifier)
            record = {"id": identifier, "status_equal": left["succeeded"] == right["succeeded"], "both_failed": not left["succeeded"] and not right["succeeded"], "raw_compared": False}
            if left["succeeded"] and right["succeeded"]:
                values = []
                for r in [left, right]:
                    path = Path(r["result"])
                    if sha(path) != r["result_sha256"]:
                        raise ValueError("Frozen result changed: " + str(path))
                    raw = json.loads(path.read_text("utf-8"))["raw"]
                    if engine == "hunyuan":
                        raw = {k: raw["choices"][0][k] for k in ["message", "finish_reason"]}
                    values.append(raw)
                diff = differences(*values)
                record.update(raw_compared=True, raw_equal=not diff, difference_count=len(diff), difference_paths=[d["path"] for d in diff[:100]])
            record["metrics_equal"] = left["metrics"] == right["metrics"]
            comparison["records"].append(record)
        comparison["passed"] = all(r["status_equal"] and r["metrics_equal"] and (r["both_failed"] or r.get("raw_equal")) for r in comparison["records"])
        comparison["successful_raw_comparisons"] = sum(r["raw_compared"] for r in comparison["records"])
        comparison["matching_failures"] = [r["id"] for r in comparison["records"] if r["both_failed"]]
        report["engines"][engine] = comparison
    report["passed"] = all(g["passed"] for g in report["engines"].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({e: {k: v for k, v in g.items() if k != "records"} for e, g in report["engines"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
