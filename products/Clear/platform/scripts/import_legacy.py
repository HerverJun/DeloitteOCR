"""Index pre-T006 manual receipts without rewriting their provenance or state."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from run_check import binding, contained, read, sha, write_new


def validate_run(run_root):
    planner = run_root / "workspaces/ocr/scripts/platform_plan.py"
    result = json.loads(subprocess.check_output([sys.executable, "-X", "utf8", str(planner), "verify-state",
                                                  "--state", str(run_root / "state.json")], text=True, encoding="utf-8"))
    if not result["valid"] or result["issues"] or result["verified_count"] < 5:
        raise ValueError("Original task state is not independently verified")


def import_receipts(run_root, destination):
    run_root = Path(run_root).resolve()
    state = read(run_root / "state.json")
    validate_run(run_root)
    checks = read(run_root / "workspaces/ocr/docs/platform-automation/checks.json")
    definitions = {item["id"]: item for item in checks["checks"]}
    records = []
    for task_id in ("T001", "T002", "T003", "T004", "T005"):
        task = state["tasks"][task_id]
        if task["status"] != "done":
            raise ValueError(f"Prior task not verified: {task_id}")
        for group in ("source_inputs", "outputs"):
            for item in task[group]:
                root = run_root if item["repo"] == "run" else Path(state["repositories"][item["repo"]])
                if sha(contained(root, item["path"])) != item["sha256"]:
                    raise ValueError(f"Stale original {group}: {task_id}")
        for identity in task["check_results"]:
            path = run_root / identity["path"]
            if sha(path) != identity["sha256"]:
                raise ValueError(f"Prior receipt changed: {path}")
            receipt = read(path)
            expected = definitions[identity["check_id"]]
            if (receipt["task_id"] != task_id or receipt["check_id"] != identity["check_id"]
                    or receipt["status"] != "passed" or type(receipt["exit_code"]) is not int
                    or receipt["exit_code"] != 0 or receipt["evidence_kind"] != expected["evidence_kind"]
                    or receipt["binding_digest"] != binding(state["plan_digest"], task_id, task)):
                raise ValueError(f"Original receipt identity or binding invalid: {task_id}")
            counts = receipt.get("test_counts")
            if counts is not None and (counts["passed"] <= 0 or counts["failed"] != 0):
                raise ValueError(f"Original test counts invalid: {task_id}")
            log = run_root / receipt["log"]["path"]
            if sha(log) != receipt["log"]["sha256"]:
                raise ValueError(f"Prior log changed: {log}")
            records.append({"task_id": task_id, "check_id": identity["check_id"],
                            "execution_mode": "manual_pre_T006", "original_receipt_path": identity["path"],
                            "original_receipt_sha256": identity["sha256"],
                            "original_binding_digest": receipt["binding_digest"],
                            "original_log_path": receipt["log"]["path"],
                            "original_log_sha256": receipt["log"]["sha256"],
                            "original_source_revision": receipt["source_revision"]})
    output = {"schema_version": 1, "provenance": "read-only import of genuine manual checks",
              "state_revision_observed": state["revision"], "receipts": records}
    write_new(destination, output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    output = import_receipts(args.run_root, args.destination)
    print(json.dumps({"imported_receipts": len(output["receipts"]),
                      "destination": str(args.destination)}, ensure_ascii=False))
