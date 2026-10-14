"""Real planner/runner CLI scenario: two stale parents and an active descendant."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--planner", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = root / "repository"
    repo.mkdir()
    planner_script = root / "workspaces/ocr/scripts/platform_plan.py"
    planner_script.parent.mkdir(parents=True)
    shutil.copyfile(args.planner, planner_script)
    docs = planner_script.parent.parent / "docs/platform-automation"
    docs.mkdir(parents=True)
    checks = []
    tasks = []
    for key, parents in (("A", []), ("B", []), ("C", ["A", "B"])):
        (repo / f"{key}.source").write_text("original\n", encoding="utf-8")
        (repo / f"{key}.output").write_text("artifact\n", encoding="utf-8")
        checks.append({"id": key, "producer_task": key, "kind": "command", "scope": "platform",
                       "evidence_kind": "native_cpu", "timeout_seconds": 15, "requires_test_counts": True,
                       "command": {"runtime": "platform_python", "cwd": "platform",
                                   "args": ["-c", "print('1 passed')"]}})
        tasks.append({"id": key, "title": key, "phase": "phase", "priority": len(tasks),
                      "max_attempts": 2, "depends_on": parents, "observes_tasks": [],
                      "resources": ["source"], "steps": ["run"], "acceptance": ["checks"],
                      "outputs": [{"repo": "platform", "path": f"{key}.output"}],
                      "write_scopes": {"platform": [f"{key}.output"]},
                      "required_checks": [key] if key != "C" else ["A", "C"]})
    plan = {"schema_version": 1, "plan_id": "serial-reverify", "repositories": {"platform": {"default_root": str(repo)}},
            "resources": {"source": {}}, "phases": [{"id": "phase"}],
            "execution": {"maximum_active_tasks": 1},
            "gates": [{"id": "gate", "tasks": ["A", "B", "C"], "requires_gates": []}], "tasks": tasks}
    catalog = {"schema_version": 1, "plan_id": plan["plan_id"], "checks": checks}
    (docs / "tasks.json").write_text(json.dumps(plan), encoding="utf-8")
    (docs / "checks.json").write_text(json.dumps(catalog), encoding="utf-8")
    trace = {"planner_sha256": hashlib.sha256(planner_script.read_bytes()).hexdigest(),
             "planner_source": str(args.planner.resolve()), "steps": []}

    def call(label, argv, expected=0):
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", cwd=HERE.parent)
        trace["steps"].append({"label": label, "argv": argv, "exit_code": result.returncode,
                               "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode != expected:
            raise AssertionError(f"{label}: {result.returncode}, expected {expected}: {result.stderr}")
        return json.loads(result.stdout) if result.stdout.startswith("{") else result.stderr

    planner = [sys.executable, "-X", "utf8", str(planner_script)]
    actions = [sys.executable, str(HERE / "state_tasks.py")]
    runner = [sys.executable, str(HERE / "run_check.py")]
    state_path = root / "state.json"

    def task_action(key, mode, extra=(), expected=0):
        return call(f"{key}-{mode}", actions + [mode, "--run-root", str(root), "--task", key] + list(extra), expected)

    def check(key, name):
        return call(f"{key}-check-{name}", runner + ["--run-root", str(root), "--task", key, "--check", name])

    try:
        call("validate", planner + ["validate"])
        initial = root / "initial"
        call("init", planner + ["init-state", "--run-dir", str(initial), "--run-id", "serial-reverify"])
        state = json.loads((initial / "state.json").read_text(encoding="utf-8"))
        state["runtimes"] = {"platform_python": {"executable": sys.executable},
                             "node": {"executable": sys.executable}}
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        for key in ("A", "B"):
            task_action(key, "claim")
            task_action(key, "freeze", ["--source", f"platform:{key}.source", "--output", f"platform:{key}.output"])
            check(key, key)
            task_action(key, "complete")
        task_action("C", "claim")
        task_action("C", "freeze", ["--source", "platform:C.source", "--output", "platform:C.output"])
        for key in ("A", "B"):
            (repo / f"{key}.source").write_text("changed\n", encoding="utf-8")
        stale = call("two-stale", planner + ["verify-state", "--state", str(state_path)], 1)
        assert set(stale["issues"]) == {"A", "B"}
        assert "Finish active task" in task_action("A", "reverify", expected=1)
        task_action("C", "suspend")
        for key in ("A", "B"):
            task_action(key, "reverify")
            task_action(key, "freeze", ["--source", f"platform:{key}.source", "--output", f"platform:{key}.output"])
            check(key, key)
            task_action(key, "complete")
        assert "attempt limit" in task_action("A", "reverify", expected=1)
        task_action("C", "resume")
        check("C", "A")  # A is a dependency's shared producer check.
        check("C", "C")
        task_action("C", "complete")
        final = call("verify-final", planner + ["verify-state", "--state", str(state_path)])
        assert final["valid"] and final["verified_count"] == 3
        assert json.loads(state_path.read_text(encoding="utf-8"))["tasks"]["C"]["attempt"] == 1
        trace["result"] = "passed"
    except BaseException as exc:
        trace["result"] = "failed"
        trace["error"] = repr(exc)
        raise
    finally:
        (root / "cli-transcript.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"result": "passed", "root": str(root), "steps": len(trace["steps"])}))


if __name__ == "__main__":
    main()
