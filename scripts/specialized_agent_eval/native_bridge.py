"""Normalize validated native receipts offline, keeping truth out of receipt collection."""
from __future__ import annotations
import argparse
from copy import deepcopy
import importlib.util
import sys
from common import ROOT, FORMAT, digest, fpath, frozen_json, read_json
from native_diagnostics import collect_diagnostics

sys.dont_write_bytecode = True
ADAPTER = ROOT / "scripts/screen_native_report.py"
WHOLE_REVIEW_CODES = {"human_confirmation_required", "quality_review_required"}


def normalize_native_index(index, diagnostics=None):
    if index.get("format") != "actual-native-offline-receipt-index-v1" or index.get("truth_opened") is not False:
        raise ValueError("Requires validated native identity index collected without truth")
    output = []
    diagnostics = diagnostics or {}
    if not set(diagnostics) <= {row['id'] for row in index['rows']}:
        raise ValueError("Diagnostic scope includes an unbound native input")
    for row in index["rows"]:
        candidate = row.get("selected_candidate") or {}
        edited = row.get("edited_view") or candidate
        quality = candidate.get("quality") or {}
        issues = row.get("issues", [])
        regions = []
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("scope") not in {"image", "table", "cell"}:
                continue
            regions.append(deepcopy(issue))
        diagnostic = diagnostics.get(row['id'])
        if diagnostic:
            regions.extend(deepcopy(diagnostic['review_regions']))
        codes = {i.get("code") for i in issues if isinstance(i, dict)}
        review_image = row.get("native_status") == "needs_review" and (not regions or bool(codes & WHOLE_REVIEW_CODES))
        output.append({"id": row["id"], "image_sha256": row["input_sha256"], "status": row["scoring_status"],
                       "input_version_id": row.get("input_version_id"), "native_version_sha256": row.get("native_version_sha256"),
                       "result_id": row.get("result_id"), "result_revision": row.get("result_revision"),
                       "selected_candidate_id": row.get("selected_candidate_id"),
                       "candidate": {"original": deepcopy(candidate), "edited": deepcopy(edited)},
                       "review_image": review_image, "review_regions": regions,
                       "automatic_delivery": bool(quality.get("automatic_delivery")),
                       "human_confirmed": bool(row.get("human_accepted")), "issues": deepcopy(issues),
                       "original_result_sha256": row.get("result_original_sha256"),
                       "edited_result_sha256": row.get("result_edited_sha256"),
                       "source_receipts": deepcopy(row.get("source_receipts", [])),
                       "native_diagnostics": deepcopy(diagnostic)})
    return {"format": FORMAT, "evidence_kind": "native_model_receipts", "inputs_sha256": index["inputs_sha256"],
            "native_table_identity_sha256": index["native_table_identity_sha256"],
            "native_database": index["native_database"], "batch_id": index["batch_id"],
            "policy": index["actual_policy"], "records": output,
            "scope": "Actual validated native rows, no truth loaded and no model/Agent submissions by this bridge",
            "localization_contract": "Current detail diagnostics require unchanged native identity, DB preparation/evidence and frozen-source CPU replay. Cell location requires replayed observed full-cell source geometry; it provides no automatic acceptance or human review qualification. Generic human/quality requirement retains image review."}


def collect(run, inputs):
    run, inputs = fpath(run), fpath(inputs)
    spec = importlib.util.spec_from_file_location("specialized_native_identity_adapter", ADAPTER)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    index = adapter.load_native_index(run, inputs)
    tables = adapter.read_database(index["native_database"])
    if adapter.table_identity(tables) != index["native_table_identity_sha256"]:
        raise ValueError("Native DB changed during bridge collection")
    diagnostics = collect_diagnostics(index, tables)
    if adapter.table_identity(adapter.read_database(index["native_database"])) != index["native_table_identity_sha256"]:
        raise ValueError("Native DB changed during diagnostic collection")
    prepared_path = run / 'prepared.json'
    if digest(prepared_path) != index['prepared_sha256']:
        raise ValueError("Native prepared receipt changed after diagnostic collection")
    prepared = read_json(prepared_path)
    if adapter.source_inventory(prepared['native_source']) != prepared['native_source_files']:
        raise ValueError("Native frozen source changed after diagnostic collection")
    normalized = normalize_native_index(index, diagnostics)
    normalized["native_adapter_sha256"] = digest(ADAPTER)
    normalized["diagnostics_bridge_sha256"] = digest(ROOT / 'scripts/specialized_agent_eval/native_diagnostics.py')
    return index, normalized


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-run", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    index, normalized = collect(args.native_run, args.inputs)
    index_path = fpath(args.output).with_suffix(".native-index.json")
    frozen_json(index_path, index)
    normalized["native_index_sha256"] = digest(index_path)
    frozen_json(args.output, normalized)
    print(len(normalized["records"]), normalized["scope"])
