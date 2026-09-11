"""Curate explicit audit artifacts; omit tokens, projects and research images."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--build", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):
        raise ValueError("Use a new curated evidence directory")
    a.output.mkdir(parents=True, exist_ok=True)
    checks = {
        "unit-tests/result.json": "passed",
        "ui-audit-final/result.json": "passed",
        "ui-audit-final/export-verification.json": "passed",
        "ui-feature-audit-final/result.json": "passed",
        "ui-feature-audit-final/export-verification.json": "passed",
        "failure-recovery-fixed/failure-recovery.json": "passed",
        "service-reproduction/result.json": "passed",
        "fidelity-final/fidelity.json": "passed",
        "public-evaluation-final/baseline-comparison.json": "passed",
        "load-cycles-final/load-cycles.json": "passed",
        "application-regression-final/application-audit.json": "passed",
        "engine-update/engine-update.json": "passed",
        "batch-stress-final/batch-stress.json": "passed",
        "table-scenarios/table-scenarios.json": "engineering_passed",
        "os-offline-final/os-isolation-result.json": "passed",
        "os-offline-final/os-network-probes.json": "passed",
        "os-offline-final/inference/application-audit.json": "passed",
        "external-clean-windows/target-machine.json": "passed",
        "external-a4000/target-machine.json": "passed",
        "intranet-evaluation/review.json": "passed",
        "release/archive-verification.json": "passed",
        "release/source-verification.json": "passed",
        "release/tested-components.json": "passed",
    }
    # These earlier receipts retain their actual scope/version in the audit report.
    additional = [
        "development-machine-rejection/target-machine.json",
        "stability-report-final/stability.png",
        "stability-report-final/stability-summary.json",
        "public-report-final/公开评测报告.md",
        "public-report-final/accuracy.png",
        "public-report-final/failure-selection.json",
        "public-report-final/table-slices.json",
        "public-evaluation/layout-failure-analysis.json",
        "paddle-cache-release/cache-diagnostic.json",
        "tested-build/manifest.json",
        "tested-build/application-manifest.json",
        "load-cycles/load-cycles.json",
        "application-regression-fixed/application-audit.json",
    ]
    for name in ["01-import.png", "02-comparison.png", "03-table.png", "04-perspective.png", "05-laptop.png"]:
        additional.append("ui-audit-final/" + name)
    for extension in ["xlsx", "txt", "md", "json"]:
        additional.append("ui-audit-final/edited." + extension)
    for name in ["batch-separate.zip", "batch-aggregate.xlsx", "07-project-storage.png"]:
        additional.append("ui-feature-audit-final/" + name)
    report = {"final_release_passed": False, "scope": "Explicit evidence collection only. No user databases, live tokens, diagnostic logs or licensed public images/transcriptions copied. Source receipts retain their actual machine and build scope.", "checks": [], "files": []}
    for relative in [*checks, *additional, "dependency-audit.json"]:
        source = a.build / relative
        if relative in checks:
            value = json.loads(source.read_text("utf-8")) if source.is_file() else {}
            ok = value.get(checks[relative]) is True
            if relative == "batch-stress-final/batch-stress.json":
                ok = ok and value.get("completed_tasks") == 500 and len(set(value.get("input_sha256", []))) == 500
            if relative == "load-cycles-final/load-cycles.json":
                cycles = value.get("cycles", [])
                ok = ok and len(cycles) == 20 and all(c.get("passed") is True for c in cycles)
                ok = ok and all(sum(c.get("engine") == engine for c in cycles) == 5 for engine in ["ppocr", "paddlevl", "glm", "hunyuan"])
            if relative == "application-regression-final/application-audit.json":
                ok = ok and value.get("quick") is False
            if relative == "table-scenarios/table-scenarios.json":
                records = value.get("records", [])
                expected = {(engine, scenario) for engine in ["paddlevl", "glm", "hunyuan"] for scenario in ["multiple", "perspective", "perspective-corrected", "real-multiple"]}
                ok = ok and len(records) == 12 and {(r["engine"], r["scenario"]) for r in records} == expected
            if relative == "fidelity-final/fidelity.json":
                engines = value.get("engines", [])
                ok = ok and len(engines) == 4 and {e["engine"] for e in engines} == {"ppocr", "paddlevl", "glm", "hunyuan"}
                ok = ok and all(len(e.get("samples", [])) == 2 and all(s.get("passed") is True for s in e["samples"]) for e in engines)
            if relative.startswith("external-"):
                role = "a4000" if "a4000" in relative else "clean-windows"
                ok = ok and value.get("role") == role
            report["checks"].append({"path": relative, "passed": ok, "exists": source.is_file()})
        if not source.is_file():
            continue
        target = a.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        report["files"].append({"path": relative, "bytes": target.stat().st_size, "sha256": digest})
    dependency = a.build / "dependency-audit.json"
    dependencies = json.loads(dependency.read_text("utf-8")) if dependency.is_file() else []
    ok = (len(dependencies) == 6
          and {r.get("runtime") for r in dependencies} == {"control", "service", "ppocr", "paddlevl", "glm", "hunyuan"}
          and all(r.get("status") == "passed" for r in dependencies))
    report["checks"].append({"path": "dependency-audit.json", "passed": ok})
    evaluation_file = a.build / "public-evaluation-final/evaluation.json"
    if evaluation_file.is_file():
        evaluation = json.loads(evaluation_file.read_text("utf-8"))
        # Per-image rows include only identifiers, hashes and numeric measurements;
        # keep research text/HTML and absolute output paths out of distribution.
        curated = {k: v for k, v in evaluation.items() if k != "engines"}
        curated["source_evaluation_sha256"] = hashlib.sha256(evaluation_file.read_bytes()).hexdigest()
        curated["engines"] = {}
        for engine, group in evaluation["engines"].items():
            curated["engines"][engine] = {k: v for k, v in group.items() if k != "records"}
            curated["engines"][engine]["records"] = [{k: v for k, v in r.items() if k not in {"result", "error"}} for r in group["records"]]
        target = a.output / "public-evaluation-summary.json"
        target.write_text(json.dumps(curated, ensure_ascii=False, indent=2), "utf-8")
        report["files"].append({"path": target.name, "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        rows = [evaluation.get("engines", {}).get(e, {}).get("records", []) for e in ["ppocr", "paddlevl", "glm", "hunyuan"]]
        sample_ids = [{r["id"] for r in records} for records in rows]
        hashes = [{r["image_sha256"] for r in records} for records in rows]
        complete = (evaluation.get("complete") is True and all(len(records) >= 200 for records in rows)
                    and all(len(ids) == len(records) for ids, records in zip(sample_ids, rows))
                    and all(len(values) == len(rows[0]) and values == hashes[0] for values in hashes)
                    and all(ids == sample_ids[0] for ids in sample_ids))
        report["checks"].append({"path": "public-evaluation-final/evaluation.json", "passed": complete})
    else:
        report["checks"].append({"path": "public-evaluation-final/evaluation.json", "passed": False})
    report["final_release_passed"] = all(c["passed"] for c in report["checks"])
    report["not_passed"] = [c["path"] for c in report["checks"] if not c["passed"]]
    (a.output / "collection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"files": len(report["files"]), "final_release_passed": report["final_release_passed"], "not_passed": report["not_passed"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
