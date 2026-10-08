"""Score only completed development experiments; no model or Agent runs."""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path
from common import FORMAT, digest, fpath, frozen_json, read_json
from quality import norm, paired_report, score_files
from raw_bridge import collect


def analyze(run_root, evaluation, worker_source, stem, partial_termination_receipt=None):
    run_root, evaluation, worker_source = fpath(run_root), fpath(evaluation), fpath(worker_source)
    inputs = evaluation / "dev-probe-12-inputs.json"
    truth_path = evaluation / "dev-probe-12-scoring/truth.json"
    scores, observations, incomplete, partial_runs = {}, {}, [], []
    for engine in ["hunyuan", "glm", "paddlevl"]:
        root = run_root / engine
        summary_path = root / "run-summary.json"
        if engine == "paddlevl" and partial_termination_receipt:
            observation_path = evaluation / (stem + "-paddlevl-partial-observations.json")
            score_path = evaluation / (stem + "-paddlevl-partial-coverage.json")
            observed = collect(root, inputs, worker_source, partial_run=True, termination_receipt=partial_termination_receipt)
            frozen_json(observation_path, observed)
            score = score_files(truth_path, observation_path)
            frozen_json(score_path, score)
            metrics = score["summary"]["totals"]
            partial_runs.append({"engine": engine, "status": "externally_terminated_partial_not_complete_engine_comparison",
                                 "images_in_fixed_denominator": metrics["images"], "native_status_counts": dict(Counter(r["status"] for r in observed["records"])),
                                 "reference_visible_cells": metrics["visible_reference_cells"], "reference_critical_cells": metrics["visible_critical_cells"],
                                 "visible_output_coverage_cells": metrics["visible_exact_cells"], "automatic_images": metrics["automatic_images"],
                                 "complete_engine_comparison": False, "default_route_selection_eligible": False,
                                 "recognition_accuracy_of_unavailable_outputs_estimated": False,
                                 "termination_evidence": observed["termination_evidence"],
                                 "native_failures": [{"id": r["id"], "error_type": r.get("error_type"), "error": r.get("error"),
                                                      "failure_phase": r.get("failure_phase"), "record_sha256": r.get("record_sha256")}
                                                     for r in observed["records"] if r["status"] == "failed"],
                                 "observations_path": str(observation_path), "observations_sha256": digest(observation_path),
                                 "coverage_path": str(score_path), "coverage_sha256": digest(score_path)})
            incomplete.append({"engine": engine, "status": "partial_lifecycle_and_full_denominator_output_coverage_only"})
            continue
        if not summary_path.exists():
            incomplete.append({"engine": engine, "status": "not_scored_no_terminal_run_summary"})
            continue
        summary = read_json(summary_path)
        if summary.get("completed") != 12 or summary.get("total") != 12:
            incomplete.append({"engine": engine, "status": "not_scored_incomplete", "completed": summary.get("completed")})
            continue
        observation_path = evaluation / (stem + "-" + engine + "-observations.json")
        score_path = evaluation / (stem + "-" + engine + "-quality.json")
        observed = collect(root, inputs, worker_source)
        if not observed["complete_snapshot"]:
            raise ValueError("Terminal summary disagrees with actual receipt completeness")
        frozen_json(observation_path, observed)
        score = score_files(truth_path, observation_path)
        frozen_json(score_path, score)
        observations[engine], scores[engine] = observed, score
    rows, examples = [], {}
    for engine, score in scores.items():
        metrics, ratios = score["summary"]["totals"], score["summary"]["ratios"]
        observed = observations[engine]
        kinds = Counter()
        missed = []
        for record in score["records"]:
            for error in record["errors"]:
                if error["critical"]:
                    if norm(error["expected"]).replace("−", "-") == norm(error["actual"]):
                        kind = "minus_glyph_only_same_numeric_sign"
                    elif norm(error["expected"]) == norm(error["actual"]):
                        kind = "same_text_wrong_span"
                    else:
                        kind = "other_literal_or_position_difference"
                    kinds[kind] += 1
                    if not error["reviewed"]:
                        missed.append({"id": record["id"], "classification": kind, **error})
        examples[engine] = {"critical_error_classes": dict(kinds), "unflagged_critical_errors": missed}
        rows.append({"engine": engine, "images": metrics["images"], "failed": metrics["failed_images"], "missing": metrics["missing_images"],
                     "table_exact_images": metrics["visible_table_exact_images"], "table_exact_rate": ratios["visible_table_exact_rate"],
                     "critical_exact_cells": metrics["visible_critical_exact_cells"], "critical_reference_cells": metrics["visible_critical_cells"],
                     "critical_exact_rate": ratios["visible_critical_exact_rate"], "visible_exact_cells": metrics["visible_exact_cells"],
                     "reference_cells": metrics["visible_reference_cells"], "visible_exact_rate": ratios["visible_cell_exact_rate"],
                     "shape_errors": metrics["shape_errors"], "extra_tables": metrics["extra_tables"], "extra_cells": metrics["extra_cells"],
                     "metadata_exact_targets": metrics["metadata_exact_targets"], "metadata_targets": metrics["metadata_visible_targets"],
                     "ui_marker_leaks": metrics["ui_marker_leaks"], "ui_geometry_unavailable_parts": metrics["ui_geometry_unavailable_parts"],
                     "review_images": metrics["review_images"], "local_error_hits": metrics["local_error_hits"],
                     "unflagged_critical_cells": metrics["unflagged_visible_critical_error_cells"], "automatic_images": metrics["automatic_images"],
                     "performance": observed["performance"],
                     "cohorts": {cohort: group["summary"] if "summary" in group else group for cohort, group in score["cohort_grouped"].items()},
                     "observations_sha256": digest(evaluation / (stem + "-" + engine + "-observations.json")),
                     "quality_sha256": digest(evaluation / (stem + "-" + engine + "-quality.json"))})
    paired = {"hunyuan_to_" + engine: paired_report(scores["hunyuan"], score) for engine, score in scores.items()
              if engine != "hunyuan" and "hunyuan" in scores}
    report = {"format": FORMAT, "status": "completed_engines_scored_with_external_partial" if partial_runs else ("completed_engines_scored" if incomplete else "all_three_completed_scored"),
              "scope": "preselected 12 development sources, not full development/validation/holdout or production delivery",
              "rows": rows, "paired": paired, "error_examples": examples, "not_scored": incomplete, "partial_runs": partial_runs,
              "probe_inputs_sha256": digest(inputs), "selection_receipt_sha256": digest(evaluation / "dev-probe-12-selection-receipt.json"),
              "scoring_truth_sha256": digest(truth_path), "worker_source": str(worker_source),
              "gpu_calls_by_analysis": 0, "controller_calls_by_analysis": 0,
              "limitations": ["All completed and failed samples retain the fixed twelve-image denominator.",
                              "Raw experiments do not release product workbooks; automatic coverage is zero, no replacement/human-time claim.",
                              "Missing raw business metadata is a stage limitation, not proof final platform context omitted it; no truth/context filled into outputs.",
                              "UI marker result does not prove UI cleanup when most cells have no usable original coordinates.",
                              "Minus-glyph-only errors remain strict failures but are not altered amounts or opposite signs.",
                              "Invocation walls include distinct model-load costs; no old-harness comparison or contiguous total wall inferred.",
                              "Final holdout/challenge scores were not read or used for this route exploration."]}
    if partial_runs:
        report["limitations"].append("Paddle external interruption is only lifecycle/output-availability evidence, not a completed recognition-quality comparison; no default route selected from this probe.")
    frozen_json(evaluation / (stem + "-comparison.json"), report)
    print(report["status"])
    for row in rows:
        print(row["engine"], row["table_exact_images"], row["critical_exact_cells"], row["critical_reference_cells"], row["performance"]["ocr_attempt_wall_seconds"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--worker-source", required=True)
    parser.add_argument("--stem", required=True)
    parser.add_argument("--partial-termination-receipt")
    args = parser.parse_args()
    analyze(args.run_root, args.evaluation, args.worker_source, args.stem, args.partial_termination_receipt)
