"""Validate the development plan and select work. Never execute task commands."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys


DEFAULT_PLAN = Path(__file__).resolve().parents[1] / "docs/platform-automation/tasks.json"
STATUSES = {"pending", "running", "verifying", "done", "blocked", "failed"}
SHA = re.compile(r"^[0-9a-f]{64}$")


class PlanError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PlanError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    def reject_constant(value):
        raise PlanError(f"Invalid JSON constant: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=unique_object, parse_constant=reject_constant)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(value):
    require(isinstance(value, str) and bool(value.strip()), "Empty relative path")
    value = value.replace("\\", "/")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    require(not posix.is_absolute() and not windows.drive and ":" not in value,
            f"Absolute/drive path is not a relative artifact: {value}")
    require(".." not in posix.parts and "\x00" not in value,
            f"Path traversal is forbidden: {value}")
    require(value not in {".", "./"}, "File path must name a file")
    return value


def contained(root, name):
    root = Path(root).resolve()
    path = (root / relative_path(name)).resolve()
    require(path.is_relative_to(root), f"Artifact escapes its root: {name}")
    return path


def topo(items, field, label):
    lookup = {item["id"]: item for item in items}
    visiting, done, ordered = set(), set(), []

    def visit(key):
        require(key in lookup, f"Unknown {label} dependency: {key}")
        require(key not in visiting, f"Cycle in {label} dependencies at {key}")
        if key in done:
            return
        visiting.add(key)
        for parent in lookup[key].get(field, []):
            visit(parent)
        visiting.remove(key)
        done.add(key)
        ordered.append(key)

    for key in lookup:
        visit(key)
    return ordered


def validate_plan(plan, checks):
    require(plan.get("schema_version") == 1 and checks.get("schema_version") == 1,
            "Unsupported plan/check schema")
    require(plan.get("plan_id") == checks.get("plan_id"), "Plan/check identity mismatch")
    tasks, definitions = plan.get("tasks", []), checks.get("checks", [])
    require(tasks and definitions, "Plan and checks must be nonempty")
    for items, label in ((tasks, "task"), (definitions, "check"),
                         (plan.get("phases", []), "phase"), (plan.get("gates", []), "gate")):
        ids = [item.get("id") for item in items]
        require(all(isinstance(key, str) and key for key in ids), f"Invalid {label} ID")
        require(len(ids) == len(set(ids)), f"Duplicate {label} ID")
    task_map = {t["id"]: t for t in tasks}
    check_map = {c["id"]: c for c in definitions}
    phases = {p["id"] for p in plan["phases"]}
    resources = set(plan["resources"])
    repositories = set(plan["repositories"])
    order = topo(tasks, "depends_on", "task")
    ancestors = {}
    for key in order:
        task = task_map[key]
        parents = set(task["depends_on"])
        ancestors[key] = parents | set().union(*(ancestors[p] for p in parents))
        require(task["phase"] in phases, f"{key}: unknown phase")
        require(type(task.get("priority")) is int, f"{key}: invalid priority")
        require(type(task.get("max_attempts")) is int and task["max_attempts"] > 0,
                f"{key}: invalid max_attempts")
        require(task.get("steps") and task.get("acceptance") and task.get("outputs"),
                f"{key}: steps, acceptance and outputs are required")
        require(set(task.get("resources", [])) <= resources, f"{key}: unknown resource")
        require(task.get("required_checks"), f"{key}: missing required checks")
        require(set(task["required_checks"]) <= check_map.keys(), f"{key}: unknown check")
        require(len(task["depends_on"]) == len(parents), f"{key}: duplicate dependency")
        require(set(task.get("observes_tasks", [])) <= task_map.keys(), f"{key}: unknown observation")
        for repo, paths in task.get("write_scopes", {}).items():
            require(repo in repositories, f"{key}: unknown write repository {repo}")
            for path in paths:
                relative_path(path)
        for output in task["outputs"]:
            require(output["repo"] in repositories | {"run"}, f"{key}: unknown output repository")
            path = relative_path(output["path"])
            if output["repo"] != "run":
                scopes = task.get("write_scopes", {}).get(output["repo"], [])
                require(any(path == relative_path(scope).rstrip("/")
                            or (scope.endswith("/") and path.startswith(relative_path(scope)))
                            for scope in scopes), f"{key}: output outside declared write scope: {path}")
        for check_id in task["required_checks"]:
            producer = check_map[check_id].get("producer_task")
            require(producer is None or producer == key or producer in ancestors[key],
                    f"{key}: check {check_id} is produced by an unmet dependency {producer}")
    for check in definitions:
        require(check.get("producer_task") is None or check["producer_task"] in task_map,
                f"{check['id']}: unknown producer")
        require(type(check.get("timeout_seconds")) is int and check["timeout_seconds"] > 0,
                f"{check['id']}: invalid timeout")
        require(check.get("evidence_kind"), f"{check['id']}: missing evidence kind")
        if check.get("kind") == "command":
            command = check.get("command")
            require(isinstance(command, dict) and isinstance(command.get("args"), list),
                    f"{check['id']}: command must have an argument array")
            require(all(isinstance(a, str) for a in command["args"]), "Non-string command argument")
    for gate in plan["gates"]:
        require(set(gate.get("tasks", [])) <= task_map.keys(), f"{gate['id']}: unknown task")
    topo(plan["gates"], "requires_gates", "gate")
    return order


def load_bundle(plan_path, checks_path=None):
    plan_path = Path(plan_path)
    checks_path = Path(checks_path) if checks_path else plan_path.with_name("checks.json")
    plan, checks = read_json(plan_path), read_json(checks_path)
    validate_plan(plan, checks)
    digest = hashlib.sha256(canonical({"plan": plan, "checks": checks})).hexdigest()
    return plan, checks, digest


def new_state(plan, digest, run_id):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id) is not None,
            "run_id must contain only letters, digits, dot, underscore or hyphen")
    return {"schema_version": 1, "plan_id": plan["plan_id"], "plan_digest": digest,
            "run_id": run_id, "revision": 0, "authorization_reference": "",
            "repositories": {key: value["default_root"] for key, value in plan["repositories"].items()},
            "resources": {name: {"available": name == "source", "reason": "Needs runtime inventory"}
                          for name in plan["resources"]},
            "tasks": {task["id"]: {"status": "pending", "attempt": 0,
                                  "source_inputs": [], "outputs": [], "check_results": [],
                                  "blockers": []} for task in plan["tasks"]}}


def binding_digest(digest, task_id, record):
    def normalized(name):
        return sorted(({"repo": entry["repo"], "path": relative_path(entry["path"]),
                        "sha256": entry["sha256"]} for entry in record.get(name, [])),
                      key=lambda entry: (entry["repo"], entry["path"]))
    return hashlib.sha256(canonical({"plan_digest": digest, "task_id": task_id,
                                    "source_inputs": normalized("source_inputs"),
                                    "outputs": normalized("outputs")})).hexdigest()


def verify_file(entry, roots):
    require(entry.get("repo") in roots, f"Unknown file repository: {entry.get('repo')}")
    require(isinstance(entry.get("sha256"), str) and SHA.fullmatch(entry["sha256"]),
            "File SHA256 must be 64 lowercase hex digits")
    path = contained(roots[entry["repo"]], entry["path"])
    require(path.is_file(), f"Missing file: {entry['repo']}:{entry['path']}")
    require(file_hash(path) == entry["sha256"], f"Stale/hash mismatch: {entry['repo']}:{entry['path']}")
    return path


def requires_test_counts(check):
    """Recognize the supported test runners; custom checks can opt in explicitly."""
    args = check.get("command", {}).get("args", []) if check.get("command") else []
    return (check.get("requires_test_counts") is True
            or "pytest" in args or "unittest" in args
            or args[:2] == ["run", "test"]
            or any(arg.replace("\\", "/").startswith("tests/") for arg in args))


def verify_done(task, record, definitions, roots, run_root, digest):
    require(record.get("source_inputs"), f"{task['id']}: missing source inputs")
    require(record.get("outputs"), f"{task['id']}: missing outputs")
    actual = {(entry["repo"], relative_path(entry["path"])) for entry in record["outputs"]}
    expected = {(entry["repo"], entry["path"]) for entry in task["outputs"]}
    require(expected <= actual, f"{task['id']}: required outputs not recorded")
    for group in ("source_inputs", "outputs"):
        entries = record[group]
        identities = [(entry["repo"], relative_path(entry["path"])) for entry in entries]
        require(len(identities) == len(set(identities)), f"{task['id']}: duplicate {group}")
        for entry in entries:
            verify_file(entry, roots)
    binding = binding_digest(digest, task["id"], record)
    results = record.get("check_results", [])
    check_ids = [entry.get("check_id") for entry in results]
    require(len(check_ids) == len(set(check_ids)), f"{task['id']}: duplicate check receipt")
    require(set(task["required_checks"]) <= set(check_ids), f"{task['id']}: missing check receipt")
    for item in results:
        check_id = item["check_id"]
        require(check_id in definitions, f"Unknown check receipt: {check_id}")
        receipt_path = verify_file({"repo": "run", "path": item["path"], "sha256": item["sha256"]}, roots)
        receipt = read_json(receipt_path)
        require(receipt.get("task_id") == task["id"] and receipt.get("check_id") == check_id,
                f"{task['id']}: receipt identity mismatch")
        require(receipt.get("status") == "passed" and type(receipt.get("exit_code")) is int
                and receipt["exit_code"] == 0, f"{task['id']}/{check_id}: check did not pass")
        require(receipt.get("evidence_kind") == definitions[check_id]["evidence_kind"],
                f"{task['id']}/{check_id}: evidence kind mismatch")
        require(receipt.get("binding_digest") == binding, f"{task['id']}/{check_id}: stale check binding")
        log = receipt.get("log", {})
        log_path = verify_file({"repo": "run", "path": log.get("path"),
                                "sha256": log.get("sha256")}, roots)
        require(log_path.stat().st_size > 0, f"{task['id']}/{check_id}: empty raw log")
        counts = receipt.get("test_counts")
        require(counts is not None or not requires_test_counts(definitions[check_id]),
                f"{task['id']}/{check_id}: missing test_counts for a test check")
        if counts is not None:
            require(all(type(counts.get(key)) is int and counts[key] >= 0
                        for key in ("passed", "failed", "skipped")), "Invalid test_counts")
            require(counts["failed"] == 0 and counts["passed"] > 0,
                    f"{task['id']}/{check_id}: no passed assertions or failed assertions remain")


def inspect_state(plan, checks, digest, state, run_root):
    require(state.get("schema_version") == 1, "Unsupported state schema")
    require(state.get("plan_id") == plan["plan_id"] and state.get("plan_digest") == digest,
            "State belongs to a different plan revision; reconcile explicitly before continuing")
    task_map = {task["id"]: task for task in plan["tasks"]}
    require(set(state.get("tasks", {})) == set(task_map), "State task IDs differ from plan")
    require(set(state.get("repositories", {})) == set(plan["repositories"]), "State repository map differs")
    require(set(state.get("resources", {})) <= set(plan["resources"]), "State has unknown resources")
    for repo, root in state["repositories"].items():
        require(isinstance(root, str) and Path(root).is_absolute(), f"{repo}: repository root must be absolute")
    roots = {**state["repositories"], "run": str(Path(run_root).resolve())}
    definitions = {check["id"]: check for check in checks["checks"]}
    verified, issues, active = set(), {}, []
    for key in topo(plan["tasks"], "depends_on", "task"):
        record, task = state["tasks"][key], task_map[key]
        require(record.get("status") in STATUSES, f"{key}: invalid status")
        require(type(record.get("attempt")) is int and record["attempt"] >= 0, f"{key}: invalid attempt")
        if record["status"] in {"running", "verifying"}:
            active.append(key)
        if record["status"] != "done":
            continue
        try:
            require(record["attempt"] > 0, f"{key}: done without an attempt")
            require(set(task["depends_on"]) <= verified, f"{key}: dependencies are not verified")
            verify_done(task, record, definitions, roots, run_root, digest)
            verified.add(key)
        except (PlanError, OSError, ValueError, KeyError, TypeError) as error:
            issues[key] = str(error)
    require(len(active) <= plan["execution"]["maximum_active_tasks"], "Too many active tasks")
    gates = {}
    gate_map = {gate["id"]: gate for gate in plan["gates"]}
    for key in topo(plan["gates"], "requires_gates", "gate"):
        gate = gate_map[key]
        missing = [item for item in gate.get("tasks", []) if item not in verified]
        parents = [item for item in gate.get("requires_gates", []) if not gates[item]["passed"]]
        gates[key] = {"passed": not missing and not parents, "missing_tasks": missing,
                      "missing_gates": parents, "meaning": gate.get("meaning", "")}
    return {"verified": verified, "issues": issues, "active": active, "gates": gates}


def select_next(plan, checks, digest, state, run_root, available=(), limit=5):
    inspected = inspect_state(plan, checks, digest, state, run_root)
    require(set(available) <= set(plan["resources"]), "Unknown available resource")
    resources = set(available)
    for name, value in state.get("resources", {}).items():
        require(isinstance(value, dict) and type(value.get("available")) is bool,
                f"{name}: resource availability must be boolean")
        if value["available"]:
            resources.add(name)
    ready, blocked, reverify = [], [], []
    for task in sorted(plan["tasks"], key=lambda item: (item["priority"], item["id"])):
        key, record = task["id"], state["tasks"][task["id"]]
        if key in inspected["verified"]:
            continue
        parents = [parent for parent in task["depends_on"] if parent not in inspected["verified"]]
        missing = sorted(set(task["resources"]) - resources)
        if key in inspected["issues"]:
            reverify.append({"id": key, "reason": inspected["issues"][key],
                             "unmet_dependencies": parents, "missing_resources": missing})
        elif record["status"] == "pending" and not parents and not missing and record["attempt"] < task["max_attempts"]:
            ready.append({"id": key, "phase": task["phase"], "title": task["title"],
                          "checks": task["required_checks"]})
        elif record["status"] not in {"running", "verifying"}:
            blocked.append({"id": key, "status": record["status"], "unmet_dependencies": parents,
                            "missing_resources": missing, "blockers": record.get("blockers", []),
                            "attempt_limit_reached": record["attempt"] >= task["max_attempts"]})
    return {"advisory_only": True, "resume": inspected["active"],
            "ready": [] if inspected["active"] else ready[:limit], "reverify": reverify,
            "blocked": blocked, "verified_count": len(inspected["verified"]),
            "total_tasks": len(plan["tasks"]), "gates": inspected["gates"],
            "all_done": len(inspected["verified"]) == len(plan["tasks"])}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--checks", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("validate")
    init = sub.add_parser("init-state")
    init.add_argument("--run-dir", type=Path, required=True)
    init.add_argument("--run-id", required=True)
    following = sub.add_parser("next")
    following.add_argument("--state", type=Path)
    following.add_argument("--available", action="append", default=[])
    following.add_argument("--limit", type=int, default=5)
    following.add_argument("--details", action="store_true")
    verify = sub.add_parser("verify-state")
    verify.add_argument("--state", type=Path, required=True)
    binding = sub.add_parser("binding")
    binding.add_argument("--state", type=Path, required=True)
    binding.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    try:
        plan, checks, digest = load_bundle(args.plan, args.checks)
        if args.action == "validate":
            result = {"valid": True, "plan_digest": digest, "tasks": len(plan["tasks"]),
                      "checks": len(checks["checks"]), "gates": len(plan["gates"])}
        elif args.action == "init-state":
            require(not args.run_dir.exists(), "Run directory already exists; refusing to overwrite")
            state = new_state(plan, digest, args.run_id)
            args.run_dir.mkdir(parents=True, exist_ok=False)
            path = args.run_dir / "state.json"
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            result = {"state": str(path.resolve()), "tasks_started": 0,
                      "note": "State initialized only; no application commands executed."}
        else:
            state_path = args.state
            state = read_json(state_path) if state_path else new_state(plan, digest, "preview")
            root = state_path.parent if state_path else Path.cwd()
            if args.action == "next":
                require(1 <= args.limit <= 100, "limit must be 1..100")
                result = select_next(plan, checks, digest, state, root, args.available, args.limit)
                if not args.details:
                    result["blocked_count"] = len(result.pop("blocked"))
                    result["gates"] = {key: {"passed": value["passed"],
                                             "missing_task_count": len(value["missing_tasks"]),
                                             "missing_gates": value["missing_gates"]}
                                       for key, value in result["gates"].items()}
            elif args.action == "binding":
                require(args.task in state["tasks"], "Unknown task")
                require(state.get("plan_digest") == digest, "State plan mismatch")
                result = {"task_id": args.task,
                          "binding_digest": binding_digest(digest, args.task, state["tasks"][args.task])}
            else:
                inspected = inspect_state(plan, checks, digest, state, root)
                result = {"valid": not inspected["issues"], "verified_count": len(inspected["verified"]),
                          "issues": inspected["issues"], "gates": inspected["gates"]}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["valid"] else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PlanError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
