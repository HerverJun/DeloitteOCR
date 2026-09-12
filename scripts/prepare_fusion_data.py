"""Freeze auditable, document-grouped public replay inputs. No OCR inference."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ocr_workbench.tables import parse_tables
from ocr_workbench.editing import validate_edit


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def document_key(sample):
    """Conservatively group pages of identifiable documents, never by crop ID."""
    name = sample["page_info"]["image_path"]
    for pattern in (
        r"^(notes_[0-9a-f]+)_\d+\.[^.]+$",
        r"^(.+\.pdf)[_-]\d+\.[^.]+$",
        r"^(.+?)[_-]page[_-]?\d+\.[^.]+$",
        r"^(.+?)[_-]\d+\.[^.]+$",
    ):
        match = re.match(pattern, name, re.I)
        if match:
            return match[1].lower(), "document prefix in upstream page filename"
    # UUID pages do not establish distinct documents. Group unknown origins by
    # upstream source class, preventing unproven independence across splits.
    source = sample["page_info"].get("page_attribute", {}).get("data_source", "unknown")
    return "untraced-source:" + source, "unknown document identity; conservative source-class group"


def prepare(benchmark, evaluation, output, seed):
    benchmark, evaluation = benchmark.resolve(), evaluation.resolve()
    b = json.loads(benchmark.read_text("utf-8"))
    e = json.loads(evaluation.read_text("utf-8"))
    if e["benchmark_sha256"] != digest(benchmark):
        raise ValueError("Evaluation does not reference the supplied benchmark bytes")
    samples = b["samples"]
    if len({s["id"] for s in samples}) != len(samples):
        raise ValueError("Duplicate sample IDs")
    records = {(engine, r["id"]): r for engine, group in e["engines"].items() for r in group["records"]}
    if len(records) != 4 * len(samples):
        raise ValueError("Expected exactly one evaluation record per sample and engine")
    # Union source documents and exact duplicates; record all grouping evidence.
    parent = {s["id"]: s["id"] for s in samples}

    def find(key):
        while key != parent[key]:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    seen = {}
    entries, annotations = [], []
    for sample in samples:
        image = benchmark.parent / sample["image"]
        if digest(image) != sample["image_sha256"]:
            raise ValueError("Image hash mismatch: " + sample["id"])
        doc, reason = document_key(sample)
        tokens = ["doc:" + doc, "source:" + sample["source_image_sha256"], "crop:" + sample["image_sha256"]]
        for token in tokens:
            if token in seen:
                parent[find(sample["id"])] = find(seen[token])
            seen[token] = sample["id"]
        reference = sample["reference"]
        parsed, warnings = [], []
        if sample["kind"] == "table":
            parsed = parse_tables(reference, warnings=warnings)
            validate_edit({"text": reference, "tables": parsed})
            if not parsed or warnings:
                raise ValueError("Reference requires structural correction: " + sample["id"])
        annotations.append({
            "id": sample["id"], "kind": sample["kind"], "original": reference,
            "revised": reference, "changes": [], "upstream_annotation": sample["annotation"],
            "reference_sha256": hashlib.sha256(reference.encode()).hexdigest(),
            "structural_checks": {"parsed_tables": len(parsed), "warnings": warnings,
                "topologies": [{"rows": t["rows"], "columns": t["columns"], "cells": len(t["cells"]),
                    "empty_cells": sum(c["text"] == "" for c in t["cells"]),
                    "merged_cells": sum(c["row_span"] > 1 or c["column_span"] > 1 for c in t["cells"])} for t in parsed]},
            "visual_review": "pending", "transcription_verified": False,
        })
        sources = []
        for engine in e["engines"]:
            record = records[(engine, sample["id"])]
            item = {"engine": engine, "status": "succeeded" if record["succeeded"] else "failed",
                    "record": record, "parsing_source": None}
            if record["succeeded"]:
                result_path = Path(record["result"])
                if digest(result_path) != record["result_sha256"]:
                    raise ValueError("Result hash mismatch: " + str(result_path))
                result = json.loads(result_path.read_text("utf-8"))
                if result["engine"] != engine:
                    raise ValueError("Result engine mismatch")
                item.update(result_path=str(result_path), result_sha256=record["result_sha256"],
                            parsing_source="original unified result.json: text, tables, blocks; raw retained",
                            model_revisions=result.get("model_revisions"), image=result.get("image"))
            sources.append(item)
        entries.append({"id": sample["id"], "kind": sample["kind"], "image": str(image),
                        "image_sha256": sample["image_sha256"], "source_image_sha256": sample["source_image_sha256"],
                        "source_url": sample["source_url"], "page": sample["page_info"]["image_path"],
                        "document": doc, "grouping_reason": reason, "sources": sources})
    groups = defaultdict(list)
    for entry in entries:
        groups[find(entry["id"])].append(entry)
    kinds = ["print", "handwriting", "table"]
    totals = Counter(s["kind"] for s in samples)
    ratios = {"development": .6, "validation": .2, "holdout": .2}
    counts = {split: Counter() for split in ratios}
    rng = random.Random(seed)
    ordered = list(groups.values())
    rng.shuffle(ordered)
    ordered.sort(key=len, reverse=True)
    for group in ordered:
        amount = Counter(s["kind"] for s in group)
        def cost(split):
            return sum(((counts[other][k] + (amount[k] if other == split else 0)
                         - totals[k] * ratio) / max(1, totals[k])) ** 2
                       for other, ratio in ratios.items() for k in kinds)
        chosen = min(ratios, key=cost)
        counts[chosen].update(amount)
        group_id = hashlib.sha256("|".join(sorted(s["id"] for s in group)).encode()).hexdigest()[:20]
        for entry in group:
            entry.update(split=chosen, group_id=group_id)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "created": datetime.now(timezone.utc).isoformat(),
                "benchmark": str(benchmark), "benchmark_sha256": digest(benchmark),
                "evaluation": str(evaluation), "evaluation_sha256": digest(evaluation),
                "upstream": {k: v for k, v in b.items() if k != "samples"}, "seed": seed,
                "target_ratios": ratios, "counts": counts, "document_groups": len(groups),
                "split_algorithm": "seeded, largest-document-first minimum stratified squared deviation; exact hash unions",
                "prior_exposure": "All 200 samples previously evaluated on all engines and used for runtime/quality analysis. Retrospective validation only; no independent holdout claim.",
                "duplicates": "Exact crop and source hashes unioned. Filename document groups retained. Unknown document IDs grouped conservatively by source class; visual near-duplicate review pending.",
                "samples": entries}
    write(output / "manifest.json", manifest)
    write(output / "annotations.json", annotations)
    write(output / "split.json", {"seed": seed, "counts": counts, "samples": [
        {k: s[k] for k in ("id", "kind", "group_id", "split", "document", "grouping_reason")} for s in entries]})
    print(json.dumps({"samples": len(entries), "records": len(records), "successful_outputs": sum(r["succeeded"] for r in records.values()),
                      "groups": len(groups), "counts": counts, "annotation_visual_review": "pending"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260912)
    args = parser.parse_args()
    prepare(args.benchmark, args.evaluation, args.output, args.seed)
