"""Exercise a small independent run against an unmodified platform_plan.py.

Usage: python scripts/real_cli_smoke.py --planner <original platform_plan.py> --evidence-root <new directory>
Creates an exclusively new directory; prints and saves every real CLI result.
"""

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
    parser.add_argument("--pytest-check", action="store_true", help="Run the actual harness suite for each check")
    args = parser.parse_args()
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    worktree = root / "workspaces/ocr"
    script = worktree / "scripts/platform_plan.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(args.planner, script)
    repository = root / "repository"
    repository.mkdir()
    (repository / "input.txt").write_text("first\n", encoding="utf-8")
    (repository / "result.txt").write_text("artifact\n", encoding="utf-8")
    test_args = (["-m", "pytest", str(HERE.parent / "tests/harness"), "-q"] if args.pytest_check
                 else ["-c", "print('1 passed')"])
    definitions = [{"id": name, "producer_task": "TX", "kind": "command", "scope": "platform",
                    "evidence_kind": "native_cpu", "timeout_seconds": 15, "requires_test_counts": True,
                    "command": {"runtime": "platform_python", "cwd": "platform",
                                "args": test_args}} for name in ("first", "second")]
    plan = {"schema_version": 1, "plan_id": "real-cli-smoke", "repositories": {"platform": {"default_root": str(repository)}},
            "resources": {"source": {}}, "phases": [{"id": "phase"}],
            "execution": {"maximum_active_tasks": 1},
            "gates": [{"id": "gate", "tasks": ["TX"], "requires_gates": [], "meaning": "smoke"}],
            "tasks": [{"id": "TX", "title": "CLI smoke", "phase": "phase", "priority": 1, "max_attempts": 3,
                       "depends_on": [], "observes_tasks": [], "resources": ["source"],
                       "steps": ["run"], "acceptance": ["both checks pass"],
                       "outputs": [{"repo": "platform", "path": "result.txt"}],
                       "write_scopes": {"platform": ["result.txt"]},
                       "required_checks": ["first", "second"]}]}
    catalog = {"schema_version": 1, "plan_id": plan["plan_id"], "checks": definitions}
    docs = worktree / "docs/platform-automation"
    docs.mkdir(parents=True)
    (docs / "tasks.json").write_text(json.dumps(plan), encoding="utf-8")
    (docs / "checks.json").write_text(json.dumps(catalog), encoding="utf-8")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    transcript = {"planner_sha256": digest, "planner_source": str(args.planner.resolve()), "steps": []}

    def invoke(label, command, expected=0):
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", cwd=HERE.parent)
        transcript["steps"].append({"label": label, "argv": command, "exit_code": result.returncode,
                                    "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode != expected:
            raise AssertionError(f"{label}: exit {result.returncode}, expected {expected}: {result.stderr}")
        return json.loads(result.stdout)

    planner = [sys.executable, "-X", "utf8", str(script)]
    tasks = [sys.executable, str(HERE / "state_tasks.py")]
    checker = [sys.executable, str(HERE / "run_check.py")]
    common = ["--run-root", str(root), "--task", "TX"]
    try:
        invoke("validate", planner + ["validate"])
        # Init-state is the planner's official creation path. Runtimes are
        # inventory registration for this independent fixture only.
        init_dir = root / "initial"
        invoke("init-state", planner + ["init-state", "--run-dir", str(init_dir), "--run-id", "real-cli-smoke"])
        state = json.loads((init_dir / "state.json").read_text(encoding="utf-8"))
        state["runtimes"] = {"platform_python": {"executable": sys.executable},
                             "node": {"executable": sys.executable}}
        (root / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        invoke("claim", tasks + ["claim"] + common)
        freeze = ["--source", "platform:input.txt", "--output", "platform:result.txt"]
        invoke("freeze", tasks + ["freeze"] + common + freeze)
        for name in ("first", "second"):
            invoke("check-" + name, checker + common + ["--check", name])
        invoke("complete", tasks + ["complete"] + common)
        assert invoke("verify-done", planner + ["verify-state", "--state", str(root / "state.json")])["verified_count"] == 1
        old_receipt = root / "attempts/TX/001/checks/first/run-001/receipt.json"
        old_hash = hashlib.sha256(old_receipt.read_bytes()).hexdigest()
        (repository / "input.txt").write_text("second\n", encoding="utf-8")
        invalid = invoke("verify-stale", planner + ["verify-state", "--state", str(root / "state.json")], expected=1)
        assert not invalid["valid"] and "TX" in invalid["issues"]
        invoke("reverify", tasks + ["reverify"] + common)
        invoke("freeze-again", tasks + ["freeze"] + common + freeze)
        for name in ("first", "second"):
            invoke("recheck-" + name, checker + common + ["--check", name])
        invoke("complete-again", tasks + ["complete"] + common)
        assert invoke("verify-again", planner + ["verify-state", "--state", str(root / "state.json")])["verified_count"] == 1
        archive = next((root / "reverify-history/TX").glob("*.json"))
        assert json.loads(archive.read_text(encoding="utf-8"))["record"]["check_results"][0]["sha256"] == old_hash
        assert hashlib.sha256(old_receipt.read_bytes()).hexdigest() == old_hash
        transcript["result"] = "passed"
    except BaseException as exc:
        transcript["result"] = "failed"
        transcript["error"] = repr(exc)
        raise
    finally:
        (root / "cli-transcript.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"result": "passed", "root": str(root), "steps": len(transcript["steps"])}))


if __name__ == "__main__":
    main()
