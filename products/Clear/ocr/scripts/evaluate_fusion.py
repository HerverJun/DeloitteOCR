"""Reproduce baseline, development/validation experiments, freeze and holdout.

Explicit stages prevent accidentally tuning on holdout. All public data carries
the manifest's prior-exposure warning. Human annotation/flow review is separate.
"""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import statistics
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_metrics import text_metrics
from fusion_metrics import text_gains, evidence_metrics, evidence_summary
from ocr_workbench.fusion import ENGINES, STRUCTURAL_ENGINES, default_policy, fuse
from ocr_workbench.fusion_alignment import cell_key, field_kind, fingerprint, topology
from ocr_workbench.tables import parse_tables, export_xlsx


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inputs(data):
    manifest = json.loads((data / "manifest.json").read_text("utf-8"))
    annotations = {a["id"]: a for a in json.loads((data / "annotations.json").read_text("utf-8"))}
    return manifest, annotations


def sources(sample, manifest):
    result = []
    for record in sample["sources"]:
        original = None
        if record["status"] == "succeeded":
            path = Path(record["result_path"])
            if sha(path) != record["result_sha256"]:
                raise ValueError("Replay bytes changed: " + str(path))
            original = json.loads(path.read_text("utf-8"))
        result.append({"engine": record["engine"], "status": record["status"],
                       "result_id": "replay:" + sample["id"] + ":" + record["engine"],
                       "task_id": "evaluation:" + sample["id"] + ":" + record["engine"],
                       "image_id": sample["id"], "version_id": sample["image_sha256"],
                       "batch": "explicit-replay:" + manifest["evaluation_sha256"],
                       "batch_basis": "fixed retrospective evaluation manifest; not an inferred workbench task batch",
                       "model_package": record.get("model_revisions"), "fingerprint": fingerprint(original),
                       **({"original": original} if original is not None else {}),
                       "error": record["record"].get("error")})
    return result


def metrics(reference, output, kind):
    result = Counter(samples=1)
    if kind != "table":
        measured = text_metrics(reference, output.get("text", ""))
        result.update({k: v for k, v in measured.items() if isinstance(v, (int, bool))})
        # Numeric field categories are counted as whole literal tokens, separate
        # from CER; this covers short dates/amounts missing from historic metrics.
        import re
        pattern = r"[¥￥$€£]?[−-]?\d[\d,]*(?:[./-]\d+)*(?:[%％])?"
        expected = Counter(re.findall(pattern, reference))
        observed = Counter(re.findall(pattern, output.get("text", "")))
        for value, count in expected.items():
            category = field_kind(value)
            result[category+"_total"] += count
            result[category+"_correct"] += min(count, observed[value])
        # Levenshtein opcode accounting, including insertions/deletions, is
        # supplemental (SequenceMatcher alignment differs from CER DP).
        from difflib import SequenceMatcher
        for op, a, z, b, y in SequenceMatcher(None, reference, output.get("text", ""), autojunk=False).get_opcodes():
            if op == "insert": result["insertions"] += y-b
            elif op == "delete": result["deletions"] += z-a
        return dict(result)
    expected, predicted = parse_tables(reference), output.get("tables", [])
    result["expected_tables"], result["predicted_tables"] = len(expected), len(predicted)
    result["missing_tables"] = max(0, len(expected)-len(predicted))
    result["extra_tables"] = max(0, len(predicted)-len(expected))
    result["structure_exact"] = [topology(t) for t in expected] == [topology(t) for t in predicted]
    for i, table in enumerate(expected):
        candidate = predicted[i] if i < len(predicted) else None
        values = {cell_key(c): c["text"] for c in candidate["cells"]} if candidate else {}
        if candidate:
            result["dimension_errors"] += (table["rows"], table["columns"]) != (candidate["rows"], candidate["columns"])
            result["coverage_merge_errors"] += set(cell_key(c) for c in table["cells"]) != set(values)
        for cell in table["cells"]:
            value = cell["text"]
            correct = unicodedata.normalize("NFC", values.get(cell_key(cell), "\0")) == unicodedata.normalize("NFC", value)
            result["cells_total"] += 1
            result["cells_correct"] += correct
            category = field_kind(value)
            result[category+"_total"] += 1
            result[category+"_correct"] += correct
    return dict(result)


def aggregate(rows):
    totals = Counter()
    for row in rows:
        totals.update(row)
    value = dict(totals)
    for key in list(totals):
        if key.endswith("_total") and totals[key]:
            category = key[:-6]
            value[category+"_accuracy"] = totals[category+"_correct"] / totals[key]
    if totals["reference_characters"]:
        value["cer"] = totals["edit_distance"] / totals["reference_characters"]
        value["strict_cer"] = totals["strict_edit_distance"] / totals["strict_reference_characters"]
    if totals["expected_tables"]:
        value["structure_accuracy"] = totals["structure_exact"] / totals["samples"]
    return value


def primary(m, kind):
    if kind == "table":
        return (sum(m.get(k+"_correct", 0)-m.get(k+"_total", 0) for k in ("amount", "date", "identifier", "number")),
                m.get("cells_correct", 0), m.get("structure_exact", 0))
    return (-m.get("edit_distance", 0), -m.get("strict_edit_distance", 0), m.get("numeric_fields_correct", 0))


def baselines(data, output):
    manifest, annotations = inputs(data)
    rows, summaries, chosen, weights = [], {}, {}, {}
    for sample in manifest["samples"]:
        if sample["split"] != "development":
            continue
        for source in sources(sample, manifest):
            if sample["kind"] == "table" and source["engine"] == "ppocr":
                continue
            row = {"id": sample["id"], "kind": sample["kind"], "engine": source["engine"],
                   "status": source["status"], "metrics": metrics(annotations[sample["id"]]["revised"], source.get("original", {}), sample["kind"])}
            rows.append(row)
    for kind in ("print", "handwriting", "table"):
        summaries[kind] = {e: aggregate([r["metrics"] for r in rows if r["kind"] == kind and r["engine"] == e])
                           for e in (STRUCTURAL_ENGINES if kind == "table" else ENGINES)}
        chosen[kind] = max(summaries[kind], key=lambda e: primary(summaries[kind][e], kind))
        weights[kind] = {e: max(.01, round(m.get("cells_accuracy", 1-m.get("cer", 1)), 6)) for e, m in summaries[kind].items()}
    write(output / "baselines.json", {"manifest_sha256": sha(data/"manifest.json"), "reference_sha256": sha(data/"annotations.json"),
                                     "selection": "development-only fixed engine per category; numeric cell errors, total cells, topology for tables; CER then strict CER for text",
                                     "normalization": "NFC; text CER also removes whitespace; full-cell and numeric token values preserve literal content",
                                     "baseline": chosen, "weights": weights, "summaries": summaries, "records": rows,
                                     "annotation_review": "all 16 validation/holdout tables visually checked; revised annotations and uncertainties retained; no independent second reader", "independent": False})
    print(json.dumps({"baseline": chosen, "weights": weights}, ensure_ascii=False), flush=True)


def gains(reference, base, output, kind):
    if kind != "table":
        a, b = metrics(reference, base, kind), metrics(reference, output, kind)
        return {"character_error_reduction": a["edit_distance"]-b["edit_distance"],
                **text_gains(reference, base.get("text", ""), output["text"]),
                "baseline_correct_overwritten_samples": int(base.get("text", "") == reference and output["text"] != reference),
                "fixed_samples": int(base.get("text", "") != reference and output["text"] == reference)}
    expected = parse_tables(reference)
    corrected = introduced = 0
    for i, table in enumerate(expected):
        def values(value):
            ts = value.get("tables", [])
            return {cell_key(c): c["text"] for c in ts[i]["cells"]} if i < len(ts) else {}
        before, after = values(base), values(output)
        for cell in table["cells"]:
            k, value = cell_key(cell), cell["text"]
            corrected += before.get(k) != value and after.get(k) == value
            introduced += before.get(k) == value and after.get(k) != value
    return {"corrected": corrected, "introduced": introduced, "net_corrected": corrected-introduced}


def run_policy(samples, manifest, annotations, policy, save_to=None):
    rows = []
    for sample in samples:
        ss = sources(sample, manifest)
        base = next((s["original"] for s in ss if s["engine"] == policy["baseline"] and s["status"] == "succeeded"), {})
        result = fuse(ss, policy, "replay-"+sample["id"])
        reference = annotations[sample["id"]]["revised"]
        units = result["fusion"]["units"]
        rows.append({"id": sample["id"], "metrics": metrics(reference, result, sample["kind"]),
                     "gains": gains(reference, base, result, sample["kind"]),
                     "seconds": result["elapsed_seconds"], "units": len(units),
                     "evidence_metrics": evidence_metrics(reference, result, ss, sample["kind"]),
                     "automatic": sum(u["automatic"] for u in units), "unresolved": result["fusion"]["unresolved"],
                     "localization": dict(Counter(u["location"]["level"] for u in units)),
                     "coverage": result["fusion"]["coverage"],
                     "jointly_wrong": sum(all(s.get("original", {}).get("text") != reference for s in ss) for _ in [0]) if sample["kind"] != "table" else None})
        if save_to:
            write(save_to/(sample["id"]+".json"), result)
            if result["tables"]:
                export_xlsx(result["tables"], save_to/(sample["id"]+".xlsx"))
    total_gains = Counter()
    for row in rows:
        total_gains.update(row["gains"])
    summary = {"metrics": aggregate([r["metrics"] for r in rows]), "gains": dict(total_gains),
               "evidence_metrics": evidence_summary([r["evidence_metrics"] for r in rows]),
               "units": sum(r["units"] for r in rows), "automatic": sum(r["automatic"] for r in rows),
               "unresolved": sum(r["unresolved"] for r in rows),
               "median_seconds": statistics.median(r["seconds"] for r in rows) if rows else None,
               "max_seconds": max((r["seconds"] for r in rows), default=0), "records": rows}
    return summary


def experiments(data, output):
    manifest, annotations = inputs(data)
    baseline = json.loads((output/"baselines.json").read_text("utf-8"))
    if baseline["manifest_sha256"] != sha(data/"manifest.json") or baseline["reference_sha256"] != sha(data/"annotations.json"):
        raise ValueError("Baseline manifest/reference changed; recompute development baseline")
    for kind in ("table", "print", "handwriting"):
        samples = [s for s in manifest["samples"] if s["kind"] == kind and s["split"] == "validation"]
        candidates = []
        for algorithm, minimum_support, minimum_coverage, numeric in itertools.product(
                ("equal", "weighted", "baseline_evidence"), (0.5, 2/3, 1.0), (0.75, 1.0), (False, True)):
            policy = default_policy(kind)
            policy.update(baseline=baseline["baseline"][kind], weights=baseline["weights"][kind],
                          algorithm=algorithm, minimum_support=minimum_support, minimum_coverage=minimum_coverage,
                          automatic_replacement=True, allow_numeric_replacement=numeric)
            # baseline_evidence requires the expected-source denominator; the
            # simple vote comparators explicitly use valid-source support.
            policy["denominator"] = "expected_capable_sources" if algorithm == "baseline_evidence" else "valid_capable_sources"
            report = run_policy(samples, manifest, annotations, policy)
            candidates.append({"policy": policy, **report})
        base_policy = default_policy(kind)
        base_policy.update(baseline=baseline["baseline"][kind], weights=baseline["weights"][kind])
        reference = run_policy(samples, manifest, annotations, base_policy, output/"prototypes"/kind)
        def score(candidate, mode):
            gains_ = candidate["gains"]
            introduced = gains_.get("introduced", gains_.get("baseline_correct_overwritten_samples", 0))
            corrected = gains_.get("corrected", gains_.get("character_error_reduction", 0))
            return (-introduced, corrected, -candidate["unresolved"]) if mode == "conservative" else (corrected-introduced, -introduced, -candidate["unresolved"])
        chosen = {mode: max(range(len(candidates)), key=lambda i: score(candidates[i], mode)) for mode in ("conservative", "aggressive")}
        write(output/(kind+"-validation.json"), {"kind": kind, "samples": len(samples), "fixed_baseline": reference,
                                               "selection": "lexicographic per declared mode objective; no holdout samples accessed",
                                               "selected": chosen, "candidates": candidates})
        print(json.dumps({"kind": kind, "samples": len(samples), "selected": {m: {"index": i, "gains": candidates[i]["gains"]} for m, i in chosen.items()}}, ensure_ascii=False), flush=True)


def freeze(data, output, policy_path):
    manifest, annotations = inputs(data)
    baseline = json.loads((output/"baselines.json").read_text("utf-8"))
    policies = {}
    files = [ROOT/"src/ocr_workbench/fusion.py", ROOT/"src/ocr_workbench/fusion_alignment.py",
             ROOT/"src/ocr_workbench/editing.py", ROOT/"src/ocr_workbench/tables.py",
             ROOT/"scripts/benchmark_metrics.py", ROOT/"scripts/fusion_metrics.py", Path(__file__).resolve()]
    for kind in ("table", "print", "handwriting"):
        report = json.loads((output/(kind+"-validation.json")).read_text("utf-8"))
        policies[kind] = {}
        for mode, selected in report["selected"].items():
            candidate = report["candidates"][selected]
            policy = deepcopy(candidate["policy"])
            policy.update(version="public-retrospective-20260912-v5", mode=mode,
                          automatic_replacement=False, allow_numeric_replacement=False)
            policy["validation"] = {"status": "suggestions_only", "independent": False,
                "reason": "Automatic candidate replacement remains closed: aggressive handwriting introduced errors and other validated candidate strategies had no gain. Initial legal skeleton fallback requires whole-table review. Fifth retrospective replay adds real-region text association and separates source coordinates/scores from fused values; no independent accuracy or human-efficiency acceptance claim.",
                "development_manifest_sha256": sha(data/"split.json"), "reference_sha256": sha(data/"annotations.json"),
                "validation_report_sha256": sha(output/(kind+"-validation.json")),
                "samples": report["samples"], "selected_candidate": selected,
                "fixed_baseline": baseline["baseline"][kind]}
            policies[kind][mode] = policy
    spec = {"schema_version": 1, "frozen_at": datetime.now(timezone.utc).isoformat(),
            "scope": manifest["prior_exposure"], "policies": policies}
    write(policy_path, spec)
    receipt = {"created": spec["frozen_at"], "policy_path": str(policy_path.resolve()), "policy_sha256": sha(policy_path),
               "manifest_sha256": sha(data/"manifest.json"), "annotations_sha256": sha(data/"annotations.json"),
               "code": {str(p.relative_to(ROOT)): sha(p) for p in files}, "automatic_replacement_open": False}
    write(output/"freeze.json", receipt)
    print(json.dumps({"policy": str(policy_path), "sha256": receipt["policy_sha256"], "automatic_replacement": False}), flush=True)


def holdout(data, output, policy_path):
    receipt = json.loads((output/"freeze.json").read_text("utf-8"))
    for rel, expected in receipt["code"].items():
        if sha(ROOT/rel) != expected:
            raise ValueError("Frozen algorithm changed before holdout: " + rel)
    for path, expected in ((policy_path, receipt["policy_sha256"]), (data/"manifest.json", receipt["manifest_sha256"]),
                           (data/"annotations.json", receipt["annotations_sha256"])):
        if sha(path) != expected:
            raise ValueError("Frozen evaluation input changed: " + str(path))
    if (output/"holdout.json").exists():
        raise ValueError("Holdout report already exists; retain it and use a separately named evaluation for subsequent retrospective analysis")
    manifest, annotations = inputs(data)
    spec = json.loads(policy_path.read_text("utf-8"))
    reports = {}
    for kind in ("table", "print", "handwriting"):
        samples = [s for s in manifest["samples"] if s["kind"] == kind and s["split"] == "holdout"]
        reports[kind] = {"samples": len(samples), "modes": {}, "single_engines": {}}
        for engine in (STRUCTURAL_ENGINES if kind == "table" else ENGINES):
            rows = []
            for sample in samples:
                source = next(s for s in sources(sample, manifest) if s["engine"] == engine)
                rows.append(metrics(annotations[sample["id"]]["revised"], source.get("original", {}), kind))
            reports[kind]["single_engines"][engine] = aggregate(rows)
        for mode, policy in spec["policies"][kind].items():
            reports[kind]["modes"][mode] = run_policy(samples, manifest, annotations, policy, output/"holdout-results"/kind/mode)
    write(output/"holdout.json", {"started_after_freeze": receipt["created"], "finished": datetime.now(timezone.utc).isoformat(),
                                "freeze_sha256": sha(output/"freeze.json"), "independent": False,
                                "scope": manifest["prior_exposure"], "reports": reports,
                                "limitations": ["Handwriting holdout only two samples after document grouping", "16 validation/holdout tables visually reviewed, with corrections and unresolved glyph ambiguities; no independent second reader",
                                                 "No claim of improved OCR accuracy; no automatic replacement released", "Human 20-image workflow acceptance pending"]})
    print(json.dumps({"holdout_samples": {k: r["samples"] for k,r in reports.items()}, "report": str(output/"holdout.json"), "independent": False}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("baseline", "experiments", "freeze", "holdout"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=ROOT/"config/fusion-policy.json")
    args = parser.parse_args()
    if args.stage == "baseline": baselines(args.data, args.output)
    elif args.stage == "experiments": experiments(args.data, args.output)
    elif args.stage == "freeze": freeze(args.data, args.output, args.policy)
    else: holdout(args.data, args.output, args.policy)
