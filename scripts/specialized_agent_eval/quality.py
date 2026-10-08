"""Independent full-denominator content/triage scoring; never selects model candidates."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import itertools
import re
import unicodedata
from pathlib import Path

from common import FORMAT, digest, fingerprint, fpath, frozen_json, indexed, read_json


def norm(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", "" if value is None else str(value)))


def tables(candidate):
    for part in [candidate.get("edited"), candidate.get("original"), candidate]:
        if isinstance(part, dict) and isinstance(part.get("tables"), list):
            return part["tables"]
    return []


def raw(candidate):
    return candidate.get("original") if isinstance(candidate.get("original"), dict) else candidate


def key(cell):
    return int(cell["row"]), int(cell["column"])


def span(cell):
    return int(cell.get("row_span", 1)), int(cell.get("column_span", 1))


def is_critical(cell):
    return "critical" in cell.get("tags", []) or bool(re.search(r"\d", str(cell.get("text", ""))))


def match_tables(expected, actual):
    # Whole-table permutation is an offline scoring alignment, never a candidate selector.
    def similarity(want, got):
        got_map = {key(c): c for c in got.get("cells", [])}
        return sum(bool(norm(c.get("text"))) and norm(got_map.get(key(c), {}).get("text")) == norm(c.get("text"))
                   for c in want["cells"]) - abs(len(want["cells"]) - len(got_map)) * .05
    if not actual:
        return [(i, want, None, {"cells": []}) for i, want in enumerate(expected)], []
    if max(len(expected), len(actual)) <= 6:
        indices = range(len(actual))
        length = min(len(expected), len(actual))
        assignments = ((wanted, got) for wanted in itertools.combinations(range(len(expected)), length)
                       for got in itertools.permutations(indices, length))
        wanted, got = max(assignments, key=lambda p: sum(similarity(expected[i], actual[j]) for i, j in zip(*p)))
        mapping = dict(zip(wanted, got))
        pairs = [(i, want, mapping.get(i), actual[mapping[i]] if i in mapping else {"cells": []})
                 for i, want in enumerate(expected)]
    else:
        remaining = set(range(len(actual)))
        pairs = []
        for i, want in enumerate(expected):
            j = max(remaining, key=lambda n: similarity(want, actual[n])) if remaining else None
            if j is not None:
                remaining.remove(j)
            pairs.append((i, want, j, actual[j] if j is not None else {"cells": []}))
    used = {j for _, _, j, _ in pairs if j is not None}
    return pairs, [j for j in range(len(actual)) if j not in used]


def polygon_center(part):
    points = part.get("polygon")
    if isinstance(points, list) and points and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in points):
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
    box = part.get("box")
    if isinstance(box, list) and len(box) == 4:
        return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return None


def point_in_polygon(point, polygon):
    if point is None or not polygon:
        return False
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if (a[1] > y) != (b[1] > y):
            crossing = (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
            if x < crossing:
                inside = not inside
    return inside


def metadata(candidate):
    answer = [{"kind": "title", "text": t["caption"], "table_index": i, "source": "table_caption"}
              for i, t in enumerate(tables(candidate)) if norm(t.get("caption"))]
    answer.extend(deepcopy(raw(candidate).get("screen", {}).get("business_metadata", [])))
    return answer


def metadata_kind(value):
    return {"caption": "title", "table_title": "title", "doc_title": "title",
            "footnote": "note", "table_note": "note"}.get(str(value), str(value))


def review_keys(record, actual_tables):
    """Count only explicit triage coverage; generic reason flags are not local detection."""
    covered = set()
    positions = {(ti, *key(c)) for ti, t in enumerate(actual_tables) for c in t.get("cells", [])}
    invalid = []
    if record.get("review_image"):
        covered.update(positions)
    for issue in record.get("review_regions", []):
        scope = issue.get("scope")
        if scope == "image":
            covered.update(positions)
        elif scope == "table" and isinstance(issue.get("table_index"), int):
            covered.update(p for p in positions if p[0] == issue["table_index"])
        elif scope == "cell" and issue.get("reliable") is True and issue.get("evidence_ref"):
            try:
                position = int(issue["table_index"]), int(issue["row"]), int(issue["column"])
            except (KeyError, TypeError, ValueError):
                invalid.append(issue)
                continue
            # Missing cells may have a valid explicit grid position even if not in candidate.
            covered.add(position)
        else:
            invalid.append(issue)
    return covered, invalid


def score_sample(truth, record=None):
    record = record or {"status": "missing"}
    status = record.get("status", "missing")
    success = status == "success"
    candidate = record.get("candidate") or {} if success else {}
    expected, actual = truth.get("tables", []), tables(candidate)
    pairs, unmatched = match_tables(expected, actual)
    counts = Counter(images=1, expected_tables=len(expected), actual_tables=len(actual),
                     missing_images=int(status == "missing"), failed_images=int(status in {"failed", "cancelled", "truncated_output"}),
                     pending_images=int(status in {"running", "pending"}),
                     not_attempted_images=int(status == "not_attempted"),
                     interrupted_unknown_images=int(status == "interrupted_unknown"), successful_images=int(success),
                     missing_tables=max(0, len(expected) - len(actual)), extra_tables=len(unmatched))
    review, invalid_regions = review_keys(record, actual)
    errors, expected_positions, actual_to_expected = [], set(), {}
    full_review = record.get("review_image") is True or any(i.get("scope") == "image" for i in record.get("review_regions", []))
    for ti, want, ai, got in pairs:
        if ai is not None:
            actual_to_expected[ai] = ti
        cells = got.get("cells", [])
        observed = {key(c): c for c in cells}
        ref_keys = {key(c) for c in want["cells"]}
        counts["duplicate_positions"] += len(cells) - len(observed)
        counts["extra_cells"] += sum(key(c) not in ref_keys for c in cells)
        counts["shape_errors"] += int(want.get("rows") != got.get("rows") or want.get("columns") != got.get("columns"))
        for cell in want["cells"]:
            position = ti, *key(cell)
            expected_positions.add(position)
            found = observed.get(key(cell))
            exact = found is not None and norm(found.get("text")) == norm(cell["text"]) and span(found) == span(cell)
            visible = cell.get("visibility", "visible") == "visible"
            critical = is_critical(cell)
            counts["all_reference_cells"] += 1
            counts["all_reference_exact_cells"] += exact
            counts["visible_reference_cells"] += visible
            counts["visible_exact_cells"] += visible and exact
            counts["visible_critical_cells"] += visible and critical
            counts["visible_critical_exact_cells"] += visible and critical and exact
            counts["uncertain_reference_cells"] += not visible
            if not visible:
                counts["uncertain_nonempty_outputs"] += bool(found and norm(found.get("text")))
            actual_position = (ai, *key(cell)) if ai is not None else position
            covered = full_review or actual_position in review
            counts["review_reference_cells"] += covered
            counts["review_visible_reference_cells"] += visible and covered
            if not exact:
                errors.append({"table_index": ti, "actual_table_index": ai, "row": cell["row"], "column": cell["column"],
                               "expected": cell["text"], "actual": found.get("text") if found else None,
                               "expected_span": list(span(cell)), "actual_span": list(span(found)) if found else None,
                               "visibility": cell.get("visibility", "visible"), "critical": critical, "reviewed": covered})
                if visible:
                    counts["visible_error_cells"] += 1
                    counts["visible_error_cells_reviewed"] += covered
                    counts["visible_critical_error_cells"] += critical
                    counts["visible_critical_error_cells_reviewed"] += critical and covered
    counts["extra_cells"] += sum(len(actual[j].get("cells", [])) for j in unmatched)
    local = {(actual_to_expected.get(i.get("table_index"), i.get("table_index")), i.get("row"), i.get("column"))
             for i in record.get("review_regions", []) if i.get("scope") == "cell" and i.get("reliable") is True and i.get("evidence_ref")}
    visible_error_positions = {(e["table_index"], e["row"], e["column"]) for e in errors if e["visibility"] == "visible"}
    counts["local_review_cells"] = len(local)
    counts["local_error_hits"] = len(local & visible_error_positions)
    counts["local_false_flags"] = len((local & expected_positions) - visible_error_positions)
    counts["local_unmapped_flags"] = len(local - expected_positions)
    counts["invalid_review_regions"] = len(invalid_regions)
    got_meta = metadata(candidate)
    targets = truth.get("context_targets", [])
    visible_targets = [t for t in targets if t.get("visibility", "visible") == "visible"]
    wanted = Counter((metadata_kind(t["kind"]), norm(t["text"])) for t in visible_targets)
    produced = Counter((metadata_kind(t.get("kind")), norm(t.get("text"))) for t in got_meta if norm(t.get("text")))
    counts["metadata_visible_targets"] = sum(wanted.values())
    counts["metadata_exact_targets"] = sum((wanted & produced).values())
    counts["metadata_missing_targets"] = sum((wanted - produced).values())
    # Extra/duplicate metadata is fully measurable only for synthetic source targets.
    full_metadata_truth = truth.get("cohort") == "chinese"
    counts["metadata_full_truth_images"] = full_metadata_truth
    counts["metadata_extra_parts"] = sum((produced - wanted).values()) if full_metadata_truth else 0
    expected_sequence = [(metadata_kind(t["kind"]), norm(t["text"])) for t in visible_targets]
    observed_sequence = [(metadata_kind(t.get("kind")), norm(t.get("text"))) for t in got_meta if norm(t.get("text"))]
    counts["metadata_sequence_exact_images"] = int(full_metadata_truth and expected_sequence == observed_sequence)
    business_texts = {norm(c["text"]) for t in expected for c in t["cells"] if norm(c["text"])}
    context_texts = {norm(t["text"]) for t in targets}
    counts["metadata_duplicate_table_text_parts"] = sum(norm(t.get("text")) in business_texts and norm(t.get("text")) not in context_texts for t in got_meta)
    output_parts = [c for t in actual for c in t.get("cells", [])] + got_meta
    ui_markers = [m for m in truth.get("ui_markers", []) if norm(m) not in business_texts | context_texts
                  and any(norm(m) in norm(p.get("text")) for p in output_parts)]
    geometric_ui = [p for p in output_parts if norm(p.get("text")) and any(
        point_in_polygon(polygon_center(p), region.get("polygon", [])) for region in truth.get("ui_regions", []))]
    counts["ui_marker_leaks"] = len(ui_markers)
    counts["ui_geometric_parts"] = len(geometric_ui)
    counts["ui_geometry_unavailable_parts"] = sum(polygon_center(p) is None for p in output_parts if norm(p.get("text")))
    counts["no_table_correct_images"] = int(success and truth.get("expect_no_table", False) and not actual)
    table_ok = success and not any(counts[k] for k in ["missing_tables", "extra_tables", "shape_errors", "duplicate_positions", "extra_cells", "visible_error_cells"])
    metadata_ok = counts["metadata_missing_targets"] == 0 and (not full_metadata_truth or
                  (counts["metadata_extra_parts"] == 0 and counts["metadata_sequence_exact_images"] == 1))
    image_exact = bool(table_ok and metadata_ok and not ui_markers and not geometric_ui and not counts["uncertain_reference_cells"])
    counts["visible_table_exact_images"] = table_ok
    counts["annotated_content_exact_images"] = image_exact
    automatic = success and record.get("automatic_delivery") is True and record.get("human_confirmed") is not True
    review_image = bool(full_review or record.get("review_regions"))
    counts["review_images"] = review_image
    counts["automatic_images"] = automatic
    counts["automatic_error_images"] = automatic and not image_exact
    counts["automatic_critical_error_cells"] = counts["visible_critical_error_cells"] if automatic else 0
    counts["automatic_critical_error_images"] = automatic and counts["visible_critical_error_cells"] > 0
    counts["automatic_uncertain_images"] = automatic and counts["uncertain_reference_cells"] > 0
    unflagged = counts["visible_critical_error_cells"] - counts["visible_critical_error_cells_reviewed"]
    counts["unflagged_visible_critical_error_cells"] = unflagged if success else 0
    counts["silent_critical_error_cells"] = unflagged if automatic else 0
    counts["human_confirmed_images"] = record.get("human_confirmed") is True
    counts["contradictory_automatic_and_review_images"] = automatic and review_image
    for metric in ("all_reference_cells all_reference_exact_cells visible_reference_cells visible_exact_cells "
                   "visible_critical_cells visible_critical_exact_cells uncertain_reference_cells uncertain_nonempty_outputs "
                   "review_reference_cells review_visible_reference_cells visible_error_cells visible_error_cells_reviewed "
                   "visible_critical_error_cells visible_critical_error_cells_reviewed shape_errors duplicate_positions extra_cells").split():
        counts.setdefault(metric, 0)
    return {"id": truth["id"], "status": status, "cohort": truth.get("cohort"), "level": truth.get("level"),
            "group_id": truth.get("group_id"), "source_id": truth.get("source_id"), "challenge": truth.get("challenge"),
            "metrics": dict(counts), "errors": errors, "ui_marker_leaks": ui_markers,
            "metadata_expected": expected_sequence, "metadata_observed": observed_sequence}


def summarize(records):
    totals = Counter()
    for record in records:
        totals.update(record["metrics"])
    ratios = {"visible_table_exact_rate": ("visible_table_exact_images", "images"),
              "annotated_content_exact_rate": ("annotated_content_exact_images", "images"),
              "visible_critical_exact_rate": ("visible_critical_exact_cells", "visible_critical_cells"),
              "visible_cell_exact_rate": ("visible_exact_cells", "visible_reference_cells"),
              "local_error_recall": ("local_error_hits", "visible_error_cells"),
              "local_flag_precision": ("local_error_hits", "local_review_cells"),
              "review_image_fraction": ("review_images", "images"),
              "review_reference_cell_fraction": ("review_reference_cells", "all_reference_cells"),
              "automatic_coverage": ("automatic_images", "images"),
              "automatic_error_image_rate": ("automatic_error_images", "automatic_images"),
              "automatic_critical_error_image_rate": ("automatic_critical_error_images", "automatic_images"),
              "metadata_visible_exact_rate": ("metadata_exact_targets", "metadata_visible_targets")}
    return {"totals": dict(totals), "ratios": {name: totals[a] / totals[b] if totals[b] else None for name, (a, b) in ratios.items()},
            "independent_source_count": len({r["source_id"] for r in records}), "group_count": len({r["group_id"] for r in records})}


def evaluate(truths, records):
    expected = indexed(truths, "id", "truth sample")
    actual = indexed(records, "id", "observed sample")
    if actual.keys() - expected.keys():
        raise ValueError("Unknown observed samples; do not mix datasets")
    for sid, record in actual.items():
        if record.get("image_sha256") != expected[sid]["image_sha256"]:
            raise ValueError(f"Observed image binding differs: {sid}")
        if record.get("status") == "success" and (not record.get("result_id") or not isinstance(record.get("result_revision"), int)):
            raise ValueError(f"Successful result lacks native result/revision identity: {sid}")
    scored = [score_sample(truth, actual.get(sid)) for sid, truth in expected.items()]
    report = {"format": FORMAT, "scope": "fixed full-denominator simulated content and triage evaluation; not real-business accuracy",
              "normalization": "NFC plus all whitespace removed; numbers, punctuation, leading zeros and sign glyphs never repaired",
              "all_expected_images_in_denominator": True, "summary": summarize(scored), "records": scored,
              "limitations": ["UI markers and geometry are annotated probes, not exhaustive UI recognition.",
                             "Public crop sources have no external title/note reference; full business metadata evaluated only on synthetic sources.",
                             "Human review time requires separately observed timed work, never inferred from flagged-cell counts.",
                             "Readability-uncertain/hidden references are diagnostic; not expected to be guessed. Automatic release of them is an error."]}
    for field in ["cohort", "group_id", "source_id", "challenge"]:
        report[field + "_grouped"] = {str(value): summarize([r for r in scored if r[field] == value]) for value in sorted({r[field] for r in scored}, key=str)}
    return report


def paired_report(before, after):
    left, right = indexed(before["records"], "id", "baseline sample"), indexed(after["records"], "id", "route sample")
    if left.keys() != right.keys():
        raise ValueError("Paired evaluation differs in full sample denominator")
    counts = Counter({name: 0 for name in ["fixed_visible_cells", "new_visible_error_cells",
                     "fixed_visible_critical_cells", "new_visible_critical_error_cells", "new_silent_critical_release_errors",
                     "review_images_delta", "review_reference_cells_delta"]})
    for sid in left:
        a, b = left[sid], right[sid]
        positions = lambda r: {(e["table_index"], e["row"], e["column"]): e for e in r["errors"] if e["visibility"] == "visible"}
        old, new = positions(a), positions(b)
        fixed, added = old.keys() - new.keys(), new.keys() - old.keys()
        counts["fixed_visible_cells"] += len(fixed)
        counts["new_visible_error_cells"] += len(added)
        counts["fixed_visible_critical_cells"] += sum(old[p]["critical"] for p in fixed)
        counts["new_visible_critical_error_cells"] += sum(new[p]["critical"] for p in added)
        # Includes existing uncorrected critical errors moved out of review.
        for position, error in new.items():
            if error["critical"] and not error["reviewed"] and b["metrics"].get("automatic_images"):
                prior = old.get(position)
                if prior is None or prior["reviewed"]:
                    counts["new_silent_critical_release_errors"] += 1
        counts["review_images_delta"] += b["metrics"].get("review_images", 0) - a["metrics"].get("review_images", 0)
        counts["review_reference_cells_delta"] += b["metrics"].get("review_reference_cells", 0) - a["metrics"].get("review_reference_cells", 0)
    counts["net_visible_cell_repairs"] = counts["fixed_visible_cells"] - counts["new_visible_error_cells"]
    residuals = after["summary"]["totals"]
    gate = (counts["net_visible_cell_repairs"] > 0 and counts["new_silent_critical_release_errors"] == 0
            and residuals.get("automatic_error_images", 0) == 0
            and residuals.get("contradictory_automatic_and_review_images", 0) == 0)
    return {"totals": dict(counts), "content_gate_passes": gate,
            "note": "Content gate only. Requires engineering gates, correct automatic subset and measured workload/time benefit before adoption."}


def score_files(truth_path, observations_path):
    truth_path, observations_path = fpath(truth_path), fpath(observations_path)
    frozen = read_json(truth_path.parent / "freeze.json")
    if digest(truth_path) != frozen["truth_sha256"] or digest(truth_path.parent / "inputs.json") != frozen["inputs_sha256"]:
        raise ValueError("Dataset frozen manifests changed")
    observations = read_json(observations_path)
    if observations.get("inputs_sha256") != frozen["inputs_sha256"]:
        raise ValueError("Observation inputs binding differs")
    report = evaluate(read_json(truth_path)["samples"], observations["records"])
    report["bindings"] = {"truth_sha256": digest(truth_path), "observations_sha256": digest(observations_path),
                          "inputs_sha256": frozen["inputs_sha256"], "evidence_kind": observations.get("evidence_kind", "unspecified")}
    report["actual_model_evidence"] = observations.get("evidence_kind") in {"native_model_receipts", "raw_engine_receipts"}
    if observations.get("partial_run"):
        report.update(partial_run=True, complete_engine_comparison=False, default_route_selection_eligible=False,
                      termination_evidence=observations.get("termination_evidence"),
                      partial_metric_interpretation="Fixed-denominator output availability/content coverage, not recognition quality of unavailable outputs",
                      unavailable_output_recognition_accuracy_estimated=False)
        report["limitations"].append("Partial externally terminated run: fixed denominators measure available output coverage, not the recognition accuracy of unattempted/interrupted/unloaded images; no complete engine or default-route conclusion.")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = score_files(args.truth, args.observations)
    frozen_json(args.output, report)
    print(report["summary"])
