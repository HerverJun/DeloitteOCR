"""Guarded task claim/complete/recover operations for future platform checks."""

import argparse
import json
import copy
import subprocess
import sys
import uuid
from pathlib import Path

from run_check import atomic_state, entry, read, sha, write_new


def planner(run_root, action, *, state=None):
    script = run_root / "workspaces/ocr/scripts/platform_plan.py"
    command = [sys.executable, "-X", "utf8", str(script), action,
               "--state", str(state or run_root / "state.json")]
    if action == "next":
        command += ["--limit", "50"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    # verify-state returns 1 for a well-formed report with stale done records.
    if result.returncode not in ({0, 1} if action == "verify-state" else {0}):
        raise RuntimeError(f"Planning validator failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def action(run_root, task_id, mode, *, sources=(), outputs=()):
    run_root = Path(run_root).resolve()
    state_path = run_root / "state.json"
    original = state_path.read_bytes()
    state = json.loads(original)
    record = state["tasks"][task_id]
    if mode == "recover":
        receipts = []
        for item in record["check_results"]:
            path = run_root / item["path"]
            valid = path.is_file() and sha(path) == item["sha256"]
            receipts.append({"check_id": item["check_id"], "path": item["path"], "hash_valid": valid,
                             "original_status": read(path).get("status") if valid else None})
        return {"task_id": task_id, "status": record["status"], "attempt": record["attempt"],
                "receipts": receipts, "next_action": "inspect native ledger and existing attempt; never blindly rerun"}
    if mode == "claim":
        ready = planner(run_root, "next")
        if (record["status"] != "pending" or any(value["status"] in {"running", "verifying"}
              for value in state["tasks"].values()) or not any(item["id"] == task_id for item in ready.get("ready", []))):
            raise ValueError("Task not pending and ready")
        record["status"] = "running"
        record["attempt"] += 1
    elif mode == "suspend":
        if record["status"] not in {"running", "verifying"}:
            raise ValueError("Only active task can be suspended")
        record["suspended_from"] = record["status"]
        record["status"] = "blocked"
    elif mode == "resume":
        if record["status"] != "blocked" or record.get("suspended_from") not in {"running", "verifying"}:
            raise ValueError("Task lacks a suspended checkpoint")
        active = [key for key, value in state["tasks"].items()
                  if value["status"] in {"running", "verifying"}]
        if active:
            raise ValueError(f"Finish active task before resume: {active}")
        record["status"] = record.pop("suspended_from")
    elif mode == "retry":
        if record["status"] != "failed":
            raise ValueError("Only failed task can open a new attempt")
        plan = read(run_root / "workspaces/ocr/docs/platform-automation/tasks.json")
        task = next(item for item in plan["tasks"] if item["id"] == task_id)
        if record["attempt"] >= task["max_attempts"] or any(
                value["status"] in {"running", "verifying"} for value in state["tasks"].values()):
            raise ValueError("Retry attempt limit reached or another task is active")
        record["status"] = "running"
        record["attempt"] += 1
        record["check_results"] = []
    elif mode == "reverify":
        if record["status"] != "done":
            raise ValueError("Reverify requires an existing done task")
        active = [key for key, value in state["tasks"].items()
                  if value["status"] in {"running", "verifying"}]
        if active:
            raise ValueError(f"Finish active task before reverify: {active}")
        plan = read(run_root / "workspaces/ocr/docs/platform-automation/tasks.json")
        task = next(item for item in plan["tasks"] if item["id"] == task_id)
        if record["attempt"] >= task["max_attempts"]:
            raise ValueError("Reverify attempt limit reached")
        # Archive the exact former ledger, including hashes of the original
        # receipts. Existing attempt files and old revision snapshots remain.
        history = run_root / "reverify-history" / task_id
        history.mkdir(parents=True, exist_ok=True)
        archive = history / f"revision-{state['revision']:03d}.json"
        archived = {"task_id": task_id, "state_revision": state["revision"],
                    "record": copy.deepcopy(record), "state_sha256": __import__("hashlib").sha256(original).hexdigest()}
        try:
            write_new(archive, archived)
        except FileExistsError:
            if read(archive) != archived:
                raise RuntimeError("Conflicting reverify history archive")
        record["status"] = "running"
        record["attempt"] += 1
        record["source_inputs"] = []
        record["outputs"] = []
        record["check_results"] = []
    elif mode == "freeze":
        if record["status"] != "running" or record["check_results"]:
            raise ValueError("Freeze requires running task without signed receipts; open a new attempt to change inputs")
        roots = {name: Path(path) for name, path in state["repositories"].items()} | {"run": run_root}

        def parse(identity):
            repo, separator, path = identity.partition(":")
            if not separator or repo not in roots:
                raise ValueError("Expected registered repo:relative/path")
            return entry(roots[repo], repo, path)

        if not sources:
            raise ValueError("At least one explicit source input is required")
        record["source_inputs"] = [parse(item) for item in sources]
        record["outputs"] = [parse(item) for item in outputs]
        for group in ("source_inputs", "outputs"):
            identities = [(item["repo"], item["path"]) for item in record[group]]
            if len(identities) != len(set(identities)):
                raise ValueError("Duplicate frozen file identity")
    elif mode == "complete":
        if record["status"] != "verifying":
            raise ValueError("Task must be verifying")
        before = planner(run_root, "verify-state")
        record["status"] = "done"
        candidate = run_root / f"state.validation-{uuid.uuid4().hex}.json"
        candidate.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            checked = planner(run_root, "verify-state", state=candidate)
            prior_issues = before["issues"]
            remaining = {key: reason for key, reason in checked["issues"].items() if key != task_id}
            if task_id in checked["issues"] or checked["verified_count"] < before["verified_count"] + 1 or any(
                    key not in prior_issues for key in remaining):
                raise ValueError(f"Candidate done state did not verify: {checked['issues']}")
        finally:
            candidate.unlink(missing_ok=True)
    else:
        raise ValueError("Unknown task action")
    atomic_state(state_path, original, state, run_root)
    return {"task_id": task_id, "status": record["status"], "attempt": record["attempt"],
            "revision": state["revision"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("claim", "retry", "reverify", "suspend", "resume", "freeze", "complete", "recover"))
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--output", action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(action(args.run_root, args.task, args.mode, sources=args.source, outputs=args.output), ensure_ascii=False))
