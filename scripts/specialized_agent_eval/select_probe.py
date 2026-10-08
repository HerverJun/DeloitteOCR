"""Freeze a deterministic development-only probe; no OCR quality-based selection."""
from __future__ import annotations
import argparse
from common import FORMAT, digest, fpath, frozen_json, indexed, read_json


def select(dataset, evaluation):
    dataset, evaluation = fpath(dataset), fpath(evaluation)
    inputs = read_json(dataset / "development/inputs.json")["samples"]
    truth = indexed(read_json(dataset / "development/truth.json")["samples"], "id", "development truth")
    # Public ordering was randomized by the generation seed before any inference.
    public = [i for i in inputs if truth[i["id"]]["cohort"] == "public"][:6]
    specifications = [("procurement_delivery_v3", 0, "normal"), ("reimbursement_matrix_v3", 0, "hard"),
                      ("project_hours_v3", 0, "hard"), ("payment_schedule_v3", 0, "hard"),
                      ("procurement_delivery_v3", 1, "hard"), ("reimbursement_matrix_v3", 1, "clean")]
    chinese = []
    for family, source_number, level in specifications:
        matches = [i for i in inputs if truth[i["id"]].get("family") == family
                   and truth[i["id"]]["source_id"].endswith(f"-{source_number:02d}")
                   and truth[i["id"]]["level"] == level]
        if len(matches) != 1:
            raise ValueError("Probe family/source/level ambiguous")
        chinese.append(matches[0])
    selected = public + chinese
    if len(selected) != 12 or len({truth[i["id"]]["source_id"] for i in selected}) != 12:
        raise ValueError("Probe must contain twelve distinct source images")
    model_samples = [{k: i[k] for k in ["id", "image", "image_sha256"]} for i in selected]
    payload = {"format": FORMAT, "name": "development-probe-12", "truth_available_to_inference": False, "samples": model_samples}
    output = evaluation / "dev-probe-12-inputs.json"
    frozen_json(output, payload)
    details = [{"id": i["id"], "source_id": truth[i["id"]]["source_id"], "group_id": truth[i["id"]]["group_id"],
                "cohort": truth[i["id"]]["cohort"], "family": truth[i["id"]].get("family"), "level": truth[i["id"]]["level"],
                "image_sha256": i["image_sha256"]} for i in selected]
    receipt = {"format": FORMAT, "scope": "development-only route exploration, not independent holdout or full development score",
               "selection_rule": "First six seed-ordered development public documents; fixed six Chinese family/source/level specifications. Chosen before any inference, never by OCR success, ideal values, truth-cell counts or best scores.",
               "coverage": ["six independent public documents with alternating normal/hard effects", "six Chinese sources across all four development families",
                            "wide six-column procurement/project layouts", "multi-level/row-merged reimbursement/project/payment structures",
                            "hard effects have 15 estimated character pixels; whole-screen UI/column-ruler/near-table-boundary context retained"],
               "limitations": ["Twelve diagnostic sources cannot establish model ranking for all business layouts.",
                               "No source-clipping/occlusion challenge in this probe; the untouched final challenge is reserved for frozen validation.",
                               "Template and prior public-corpus exposure limitations of v3-002 still apply."],
               "dataset_inputs_sha256": digest(dataset / "development/inputs.json"),
               "dataset_truth_sha256": digest(dataset / "development/truth.json"),
               "probe_inputs_sha256": digest(output), "selector_sha256": digest(__file__), "selected": details,
               "holdout_and_challenge_read": False, "gpu_calls": 0, "controller_calls": 0}
    frozen_json(evaluation / "dev-probe-12-selection-receipt.json", receipt)
    # Separate offline scoring view. Do not send this directory to the controller/model.
    scoring = evaluation / "dev-probe-12-scoring"
    frozen_json(scoring / "inputs.json", payload)
    frozen_json(scoring / "truth.json", {"format": FORMAT, "name": "development-probe-12", "samples": [truth[i["id"]] for i in selected]})
    frozen_json(scoring / "freeze.json", {"format": FORMAT, "count": 12, "inputs_sha256": digest(scoring / "inputs.json"),
                "truth_sha256": digest(scoring / "truth.json"), "selection_receipt_sha256": digest(evaluation / "dev-probe-12-selection-receipt.json"),
                "parent_development_freeze_sha256": digest(dataset / "development/freeze.json"), "scope": receipt["scope"]})
    print(str(output), len(selected), digest(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--evaluation", required=True)
    args = parser.parse_args()
    select(args.dataset, args.evaluation)
