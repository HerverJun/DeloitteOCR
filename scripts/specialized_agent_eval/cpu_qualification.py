"""Dataset plus full-manifest scoring-interface qualification; submits no inference."""
from __future__ import annotations
import argparse
import tempfile
from pathlib import Path
from common import FORMAT, digest, fpath, frozen_json, read_json
from quality import score_files
from verify import verify


def qualify(dataset, output_root, output):
    dataset, output_root = fpath(dataset), fpath(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    data = verify(dataset)
    checks = []
    with tempfile.TemporaryDirectory(dir=output_root) as temporary:
        for split in ["development", "validation", "holdout", "challenge"]:
            directory = dataset / split
            freeze = read_json(directory / "freeze.json")
            empty = Path(temporary) / (split + "-not-attempted.json")
            frozen_json(empty, {"format": FORMAT, "evidence_kind": "cpu_missing-output_contract_fixture",
                        "inputs_sha256": freeze["inputs_sha256"], "records": []})
            score = score_files(directory / "truth.json", empty)
            actual = score["summary"]["totals"]
            if actual["images"] != freeze["count"] or actual["missing_images"] != freeze["count"] or actual["automatic_images"] != 0 or score["actual_model_evidence"]:
                raise AssertionError("Fixed denominator/missing output semantics differ")
            checks.append({"split": split, "images": actual["images"], "missing_images": actual["missing_images"],
                           "all_reference_cells": actual["all_reference_cells"], "visible_critical_cells": actual["visible_critical_cells"],
                           "automatic_images": 0, "actual_model_evidence": False, "status": "passed"})
    report = {"format": FORMAT, "status": "passed", "scope": "CPU dataset and empty-observation contract qualification, not OCR quality results",
              "dataset": str(dataset), "dataset_freeze_sha256": digest(dataset / "freeze.json"), "dataset_qualification": data,
              "full_denominator_interface_checks": checks, "gpu_calls": 0, "controller_calls": 0}
    frozen_json(output, report)
    print(report["status"], checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    qualify(args.dataset, args.output_root, args.output)
