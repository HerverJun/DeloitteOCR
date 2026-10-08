"""Read actual context/run.py receipts offline; keeps diagnostic runs distinct from Agent delivery."""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import sys
from common import ROOT, FORMAT, digest, fpath, frozen_json, indexed, read_json

sys.dont_write_bytecode = True


def source_hash(root):
    rows = [[path.relative_to(root).as_posix(), digest(path)] for path in sorted(root.rglob("*.py"))]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def utc_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("External termination time is invalid") from error
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("External termination time must be explicit UTC")
    return result


def verify_termination(path, run, inputs, worker_source, binding, binding_hash, before):
    """Verify externally observed ownership/exit; never stop or inspect live processes."""
    path = fpath(path)
    before[str(path)] = digest(path)
    receipt = read_json(path)
    if (receipt.get("format") != "owned-benchmark-termination-v1"
            or receipt.get("engine") != binding["engine"]
            or fpath(receipt.get("run_root", "")) != run
            or receipt.get("inputs_sha256") != binding["inputs_sha256"]
            or receipt.get("run_binding_sha256") != binding_hash
            or receipt.get("ownership_verified") is not True
            or receipt.get("exit_observed") is not True):
        raise ValueError("Partial run lacks matching observed owned-process termination")
    for field in ("worker_source_sha256", "engine_config_sha256"):
        if field in receipt and receipt[field] != binding[field]:
            raise ValueError("External termination optional source/config binding differs")
    original_path = fpath(receipt.get("original_evidence_file", ""))
    if not original_path.is_relative_to(run) or original_path == path:
        raise ValueError("External termination needs distinct original evidence inside the run")
    before[str(original_path)] = digest(original_path)
    if before[str(original_path)] != receipt.get("original_evidence_sha256"):
        raise ValueError("External termination original evidence bytes changed")
    original = read_json(original_path)
    process = receipt.get("benchmark_process") or {}
    pid = process.get("pid")
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or original.get("format") != "screen-benchmark-external-termination-v1"
            or original.get("process_id") != pid
            or original.get("binding_sha256") != binding_hash
            or fpath(original.get("run", "")) != run
            or original.get("process_running_after_stop") is not False):
        raise ValueError("External termination original process/run/exit identity differs")
    created, requested, stopped = [utc_time(receipt.get(key) if key != "creation_time_utc" else process.get(key))
                                  for key in ("creation_time_utc", "stop_requested_at_utc", "stopped_at_utc")]
    if not created <= requested <= stopped:
        raise ValueError("External termination chronological order differs")
    for actual, key in zip((created, requested, stopped), ("created_at", "stop_requested_at", "stopped_at")):
        if actual != utc_time(original.get(key)):
            raise ValueError("External termination original time differs")
    for original_key, receipt_key in (("created_at", "creation_time_utc"), ("stop_requested_at", "stop_requested_at_utc"), ("stopped_at", "stopped_at_utc")):
        value = process.get(receipt_key) if receipt_key == "creation_time_utc" else receipt.get(receipt_key)
        if original.get(original_key) != value:
            raise ValueError("External termination exact recorded UTC precision differs")
    command = original.get("command_line")
    if not isinstance(command, str) or hashlib.sha256(command.encode("utf-8")).hexdigest() != process.get("command_line_sha256"):
        raise ValueError("External termination command-line identity differs")
    try:
        tokens = [token[1:-1] if token.startswith('"') and token.endswith('"') else token
                  for token in shlex.split(command, posix=False)]
    except ValueError as error:
        raise ValueError("External termination command line is invalid") from error
    if not tokens or fpath(tokens[0]) != fpath(original.get("executable", "")):
        raise ValueError("External termination executable identity differs")
    def argument(name):
        if tokens.count(name) != 1 or tokens.index(name) + 1 >= len(tokens):
            raise ValueError("External termination command has absent/duplicate binding arguments")
        return tokens[tokens.index(name) + 1]
    for name, target in (("--output", run), ("--inputs", inputs), ("--worker-source", worker_source), ("--bundle", fpath(binding["bundle"]))):
        if fpath(argument(name)) != target:
            raise ValueError("External termination command target differs")
    if argument("--engine") != binding["engine"]:
        raise ValueError("External termination command engine differs")
    if receipt.get("completed_at_stop") != original.get("completed_at_stop") or receipt.get("total") != original.get("total"):
        raise ValueError("External termination count binding differs")
    return {"receipt": receipt, "receipt_path": str(path), "receipt_sha256": before[str(path)],
            "original_evidence_sha256": before[str(original_path)]}


def collect(run, inputs, worker_source, *, partial_run=False, termination_receipt=None):
    run, inputs, worker_source = fpath(run), fpath(inputs), fpath(worker_source)
    binding_path = run / "binding.json"
    before = {str(binding_path): digest(binding_path), str(inputs): digest(inputs)}
    binding = read_json(binding_path)
    manifest = read_json(inputs)
    if (manifest.get("truth_available_to_inference") is not False
            or binding.get("labels_or_ideal_regions_provided") is not False
            or binding["inputs_sha256"] != digest(inputs)
            or binding["worker_source_sha256"] != source_hash(worker_source)):
        raise ValueError("Raw-run source/input/reference isolation differs")
    config = fpath(Path(binding["bundle"]) / "config/engines.json")
    if digest(config) != binding["engine_config_sha256"]:
        raise ValueError("Raw-run engine registration changed")
    before[str(config)] = digest(config)
    if partial_run != bool(termination_receipt):
        raise ValueError("Partial mode requires a termination receipt, used only with partial_run")
    termination = verify_termination(termination_receipt, run, inputs, worker_source, binding,
                                    before[str(binding_path)], before) if partial_run else None
    samples = manifest["samples"]
    if binding.get("include_sample_ids") is not None:
        requested = set(binding["include_sample_ids"])
        if not requested <= {s["id"] for s in samples}:
            raise ValueError("Unknown raw-run diagnostic sample IDs")
        samples = [s for s in samples if s["id"] in requested]
    else:
        offset, limit = binding.get("offset", 0), binding.get("limit")
        samples = samples[offset:offset + limit if limit is not None else None]
    indexed(samples, "id", "raw input")
    if termination and termination["receipt"].get("total") != len(samples):
        raise ValueError("External termination total differs from bound sample scope")
    records, context_summary = [], None
    context_adapter, normalize = None, None
    if binding.get("context_run"):
        context_root = fpath(binding["context_run"])
        context_binding_path = context_root / "binding.json"
        if digest(context_binding_path) != binding["context_binding_sha256"]:
            raise ValueError("Raw-run consumed context binding changed")
        context_binding = read_json(context_binding_path)
        if context_binding["inputs_sha256"] != binding["inputs_sha256"] or context_binding["engine_config_sha256"] != binding["engine_config_sha256"] or context_binding.get("labels_or_ideal_regions_provided") is not False:
            raise ValueError("Raw-run context reference/input/config binding differs")
        if binding.get("context_source_policy", "exact") == "exact" and context_binding["worker_source_sha256"] != binding["worker_source_sha256"]:
            raise ValueError("Raw-run exact context worker differs")
        if binding.get("context_source_policy") == "recorded" and context_binding["worker_source_sha256"] != binding.get("context_worker_source_sha256"):
            raise ValueError("Raw-run historical context worker differs")
        spec = importlib.util.spec_from_file_location("specialized_readonly_context_validator", ROOT / "scripts/screen_benchmark/context.py")
        context_adapter = importlib.util.module_from_spec(spec)
        # context.py imports run helpers but does not start adapters/models outside main.
        sys.path.insert(0, str(ROOT / "scripts/screen_benchmark"))
        spec.loader.exec_module(context_adapter)
        before[str(context_binding_path)] = digest(context_binding_path)
        summary_path = context_root / "run-summary.json"
        if summary_path.exists():
            context_summary = read_json(summary_path)
            before[str(summary_path)] = digest(summary_path)
    for sample in samples:
        image_path = fpath(sample["image"])
        if digest(image_path) != sample["image_sha256"]:
            raise ValueError("Raw-run original input bytes changed")
        before[str(image_path)] = sample["image_sha256"]
        folder = fpath(run / sample["id"])
        if not folder.is_relative_to(run):
            raise ValueError("Unsafe sample output path")
        record_path = folder / "record.json"
        if not record_path.exists():
            records.append({"id": sample["id"], "image_sha256": sample["image_sha256"],
                            "status": "not_attempted" if partial_run else "missing", "source_status": "no_native_record",
                            "automatic_delivery": False, "human_confirmed": False})
            continue
        before[str(record_path)] = digest(record_path)
        record = read_json(record_path)
        if record.get("id") != sample["id"] or record.get("image_sha256") != sample["image_sha256"] or record.get("engine") != binding["engine"]:
            raise ValueError("Raw-run record image/engine/sample identity differs")
        status = record["status"]
        if status not in {"success", "failed", "cancelled", "running"}:
            raise ValueError("Unsupported native raw record status")
        source_status = status
        if partial_run and status == "running":
            status = "interrupted_unknown"
        candidate, result_id = {}, None
        for prepared in record.get("prepared_images", []):
            prepared_path = fpath(prepared["path"])
            if digest(prepared_path) != prepared["sha256"]:
                raise ValueError("Raw-run prepared input bytes changed")
            before[str(prepared_path)] = prepared["sha256"]
        if context_adapter and (source_status == "success" or record.get("context_sha256") or record.get("context_evidence_sha256")):
            context_folder = context_root / sample["id"]
            context_record, _, _ = context_adapter.verify_context_record(context_folder, sample, normalize=None)
            if record.get("context_sha256") != context_record["context_sha256"] or record.get("context_evidence_sha256") != context_record["evidence_sha256"]:
                raise ValueError("Raw-run consumed context receipt differs")
            for context_path in context_folder.rglob("*"):
                if context_path.is_file():
                    before[str(context_path)] = digest(context_path)
        if status == "success":
            result_path = folder / "result.json"
            if digest(result_path) != record["result_sha256"]:
                raise ValueError("Raw-run result bytes changed")
            candidate = read_json(result_path)
            before[str(result_path)] = digest(result_path)
            # This is an immutable file identity, explicitly not a fabricated DB result ID.
            result_id = "raw-output-sha256:" + record["result_sha256"]
        quality = record.get("quality") or candidate.get("quality") or {}
        issues = quality.get("issues", [])
        regions = [deepcopy(i) for i in issues if isinstance(i, dict) and i.get("scope") in {"image", "table", "cell"}]
        review_required = quality.get("review_required", True)
        records.append({"id": sample["id"], "image_sha256": sample["image_sha256"], "status": status,
                        "source_status": source_status, "source_record": record,
                        "error_type": record.get("error_type"), "error": record.get("error"),
                        "failure_phase": record.get("failure_phase", record.get("phase")),
                        "result_id": result_id, "result_revision": 0 if status == "success" else None,
                        "result_identity_kind": "immutable_raw_output_sha256; not product DB revision",
                        "candidate": candidate, "review_image": bool(review_required and not regions), "review_regions": regions,
                        "automatic_delivery": False, "human_confirmed": False, "raw_quality": quality,
                        "automatic_delivery_scope": "diagnostic extraction has no product delivery; always false",
                        "record_sha256": before[str(record_path)], "result_sha256": record.get("result_sha256"),
                        "attempt": record.get("attempt"), "engine": binding["engine"],
                        "inference_seconds": record.get("inference_seconds"), "preprocessing_seconds": record.get("preprocessing_seconds")})
    summary_path = run / "run-summary.json"
    summary = read_json(summary_path) if summary_path.exists() else None
    if summary:
        before[str(summary_path)] = digest(summary_path)
    if source_hash(worker_source) != binding["worker_source_sha256"]:
        raise ValueError("Raw-run worker source changed during offline collection")
    for path, expected_hash in before.items():
        if digest(path) != expected_hash:
            raise ValueError("Raw-run receipts changed during offline collection")
    complete = len(records) == len(samples) and all(r["status"] in {"success", "failed", "cancelled"} for r in records)
    if termination:
        completed = sum(r["source_status"] in {"success", "failed", "cancelled"} for r in records)
        if completed != termination["receipt"].get("completed_at_stop"):
            raise ValueError("External termination completed count differs from native records")
        complete = False
    return {"format": FORMAT, "evidence_kind": "raw_engine_receipts", "inputs_sha256": binding["inputs_sha256"],
            "source_binding": binding, "source_binding_sha256": before[str(binding_path)], "records": records,
            "scope": "real diagnostic engine receipts, not platform Agent/XLSX delivery or independent full holdout",
            "complete_snapshot": complete, "sample_count": len(samples), "partial_run": partial_run,
            "complete_engine_comparison": False if partial_run else complete,
            "default_route_selection_eligible": False, "termination_evidence": termination,
            "performance": {"ocr_attempt_wall_seconds": summary.get("attempt_wall_seconds") if summary else None,
                            "context_attempt_wall_seconds": context_summary.get("attempt_wall_seconds") if context_summary else None,
                            "cache_resume_separate": bool(summary and summary.get("cache_resume_separate")),
                            "engine_session_loads": summary.get("loads") if summary else None,
                            "full_batch_wall_seconds": None,
                            "note": "Measured invocation walls are separate. Shared context charged once externally; no contiguous full-batch or provider-thinking time inferred."},
            "truth_read": False, "gpu_calls_by_bridge": 0, "controller_calls_by_bridge": 0,
            "limitations": ["Raw table results may not yet include platform ancillary metadata/export formatting; absent metadata is not filled from truth or context.",
                            "Record/immutable raw SHA identity does not substitute for native task/result/selection identity.",
                            "Automatic coverage remains zero because this extraction experiment did not release product files."]
                           + (["Externally interrupted partial run: in-flight outcome unknown and absent native records not attempted; fixed full scope retained.",
                               "Not a complete engine comparison, no OCR-quality conclusion for unavailable outputs, and not eligible to select a default route.",
                               "Only native error/phase fields are preserved; a generic TimeoutError never establishes cold-load or inference phase by itself."] if partial_run else [])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--worker-source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--partial-run", action="store_true")
    parser.add_argument("--termination-receipt")
    args = parser.parse_args()
    report = collect(args.run, args.inputs, args.worker_source, partial_run=args.partial_run, termination_receipt=args.termination_receipt)
    frozen_json(args.output, report)
    print(report["sample_count"], report["complete_snapshot"], report["scope"])
