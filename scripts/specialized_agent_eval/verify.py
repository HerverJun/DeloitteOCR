"""CPU-only dataset qualification, including all source ancestry and immutable bytes."""
from __future__ import annotations
import argparse
from collections import Counter
import itertools
from pathlib import Path
from PIL import Image
import numpy as np
from common import FORMAT, digest, fpath, frozen_json, indexed, read_json


def verify(root):
    root = fpath(root)
    registry = read_json(root / "source-registry.json")
    top = read_json(root / "freeze.json")
    if digest(root / "source-registry.json") != top["source_registry_sha256"]:
        raise ValueError("Source registry frozen binding differs")
    old = read_json(registry["old_registry"])
    if digest(registry["old_registry"]) != registry["old_registry_sha256"]:
        raise ValueError("Old source ancestry changed")
    old_public = {s["group_id"] for s in old["sources"] if s["cohort"] == "public"}
    old_hashes = {s["source_sha256"] for s in old["sources"]}
    old_families = {s.get("family") for s in old["sources"] if s["cohort"] == "chinese"}
    sources = indexed(registry["sources"], "id", "source")
    source_bytes = 0
    for source in sources.values():
        path = fpath(source["path"])
        if digest(path) != source["source_sha256"]:
            raise ValueError("Source image changed")
        source_bytes += path.stat().st_size
        if source["source_sha256"] in old_hashes:
            raise ValueError("Old source image reused in independent dataset")
        if source["cohort"] == "public" and source["group_id"] in old_public:
            raise ValueError("Old public document reused")
        if source["cohort"] == "chinese" and source["family"] in old_families:
            raise ValueError("Old Chinese family reused")
        for path_key, hash_key in [("source_manifest", "source_manifest_sha256"), ("annotation_path", "annotation_sha256")]:
            if path_key in source and digest(source[path_key]) != source[hash_key]:
                raise ValueError("Source manifest/annotation changed")
    groups = {split: {s["group_id"] for s in sources.values() if s["split"] == split}
              for split in ["development", "validation", "holdout"]}
    if any(groups[a] & groups[b] for a, b in itertools.combinations(groups, 2)):
        raise ValueError("Document/template split leakage")
    sets, file_count = {}, 0
    for name, count in [("development", 60), ("validation", 40), ("holdout", 240), ("challenge", 24)]:
        directory = root / name
        freeze = read_json(directory / "freeze.json")
        if digest(directory / "freeze.json") != top["sets"][name]["freeze_sha256"]:
            raise ValueError("Split freeze changed")
        for label in ["inputs", "truth"]:
            if digest(directory / (label + ".json")) != freeze[label + "_sha256"]:
                raise ValueError("Frozen input/truth manifest changed")
        if freeze["count"] != count:
            raise ValueError("Wrong fixed sample count")
        from common import ROOT
        if digest(ROOT / "scripts/specialized_agent_eval/generate.py") != freeze["generator_sha256"]:
            raise ValueError("Generator bytes differ from frozen source")
        if digest(ROOT / "scripts/screen_benchmark/generate.py") != freeze["screen_renderer_sha256"]:
            raise ValueError("Historical screen renderer changed")
        inputs = indexed(read_json(directory / "inputs.json")["samples"], "id", "input")
        truth = indexed(read_json(directory / "truth.json")["samples"], "id", "truth")
        if inputs.keys() != truth.keys() or len(inputs) != count:
            raise ValueError("Input/truth cardinality differs")
        critical, visible, total_cells, context_count = 0, 0, 0, 0
        for sid, item in inputs.items():
            if {"tables", "fixed_edit", "targets", "truth", "text"} & item.keys():
                raise ValueError("Truth leaked into inference input")
            target = truth[sid]
            source = sources[target["source_id"]]
            if target["group_id"] != source["group_id"] or target["source_sha256"] != source["source_sha256"]:
                raise ValueError("Wrong source ancestry")
            image = fpath(item["image"])
            if digest(image) != item["image_sha256"] or target["image_sha256"] != item["image_sha256"]:
                raise ValueError("Rendered image changed")
            with Image.open(image) as im:
                if im.size != (item["width"], item["height"]):
                    raise ValueError("Rendered image dimensions changed")
            for label in ["occlusion_mask", "visibility_mask"]:
                path = fpath(target[label])
                if digest(path) != target[label + "_sha256"]:
                    raise ValueError("Visibility evidence changed")
                file_count += 1
            matrix = np.asarray(target["source_to_image"])
            if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
                raise ValueError("Invalid original-to-image mapping")
            for table in target["tables"]:
                occupied = set()
                for cell in table["cells"]:
                    r, c, rs, cs = cell["row"], cell["column"], cell["row_span"], cell["column_span"]
                    slots = {(y, x) for y in range(r, r + rs) for x in range(c, c + cs)}
                    if r < 0 or c < 0 or rs < 1 or cs < 1 or r + rs > table["rows"] or c + cs > table["columns"] or slots & occupied:
                        raise ValueError("Bad truth cell grid/span")
                    occupied |= slots
                    if not isinstance(cell["text"], str):
                        raise ValueError("Truth cells must remain literal strings")
                    total_cells += 1
                    visible += cell["visibility"] == "visible"
                    critical += "critical" in cell.get("tags", []) or any(ch.isdigit() for ch in cell["text"])
                    if cell["visibility"] == "visible" and any(x < -.01 or y < -.01 or x > item["width"] + .01 or y > item["height"] + .01 for x, y in cell["polygon"]):
                        raise ValueError("Visible reference outside image")
            context_count += len(target["context_targets"])
            if target["expect_no_table"] and (target["tables"] or target["context_targets"]):
                raise ValueError("No-table challenge contains hidden expected business table")
            for region in target["ui_regions"]:
                if region["kind"] == "column_ruler":
                    h = np.asarray(target["screen_to_image"])
                    inverse = np.linalg.inv(h)
                    points = []
                    for x, y in region["polygon"]:
                        p = inverse @ [x, y, 1]
                        points.append([p[0] / p[2], p[1] / p[2]])
                    if abs(max(p[0] for p in points) - target["parameters"]["screen_size"][0]) > .02:
                        raise ValueError("Column-ruler UI annotation uses unscaled source width")
            file_count += 1
        sets[name] = {"images": count, "source_count": freeze["source_count"], "group_count": freeze["group_count"],
                      "cohorts": freeze["cohorts"], "reference_cells": total_cells, "visible_reference_cells": visible,
                      "critical_reference_cells": critical, "business_metadata_targets": context_count,
                      "inputs_sha256": freeze["inputs_sha256"], "truth_sha256": freeze["truth_sha256"]}
    for entry in top["old_sets"].values():
        if digest(entry["path"]) != entry["freeze_sha256"]:
            raise ValueError("Historical regression freeze changed")
    return {"format": FORMAT, "status": "passed", "scope": "CPU simulation data/source qualification only; no OCR/Agent/GPU evaluation",
            "sets": sets, "rendered_image_and_mask_files_verified": file_count,
            "source_images": len(sources), "source_image_bytes": source_bytes,
            "old_public_document_overlap": 0, "old_source_hash_overlap": 0, "old_chinese_family_overlap": 0,
            "development_validation_holdout_group_overlap": 0, "prior_exposure": registry["prior_exposure"],
            "limitations": ["Holdout and challenge share four new Chinese template families; source images are distinct.",
                            "Chinese variants remain correlated; 240 images are not 240 independent business samples.",
                            "Same historical screen renderer/UI is deliberately reused; this is source/template independence, not new UI-domain independence.",
                            "Public sources lack external metadata truth; real-photo performance and human time are untested."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = verify(args.dataset)
    frozen_json(args.output, report)
    print(report["status"], report["sets"])
