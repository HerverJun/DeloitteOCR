"""Real Windows Job Object, frozen command, evidence and recovery counterexamples."""

import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_check import atomic_state, canonical, environment, execute_owned, read, run, sha, test_counts as parse_counts
from windows_job import OwnedJob


def fake_run(tmp_path, script, *, output=True, checks=("fake",), timeout=3):
    repository = tmp_path / "repo"
    repository.mkdir(parents=True)
    (repository / "input.txt").write_text("stable", encoding="utf-8")
    if output:
        (repository / "result.txt").write_text("artifact", encoding="utf-8")
    task = {"id": "TX", "required_checks": list(checks), "max_attempts": 3,
            "outputs": [{"repo": "platform", "path": "result.txt"}]}
    definitions = [{"id": name, "kind": "command", "producer_task": "TX", "scope": "platform",
                    "command": {"runtime": "platform_python", "cwd": "platform", "args": ["-c", script]},
                    "timeout_seconds": timeout, "evidence_kind": "native_cpu", "requires_test_counts": True} for name in checks]
    plan, catalog = {"tasks": [task]}, {"checks": definitions}
    (tmp_path / "tasks.json").write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "checks.json").write_text(json.dumps(catalog), encoding="utf-8")
    record = {"status": "running", "attempt": 1,
              "source_inputs": [{"repo": "platform", "path": "input.txt", "sha256": sha(repository / "input.txt")}],
              "outputs": [], "check_results": [], "blockers": []}
    state = {"revision": 0, "plan_digest": __import__("hashlib").sha256(canonical({"plan": plan, "checks": catalog})).hexdigest(),
             "repositories": {"platform": str(repository)},
             "runtimes": {"platform_python": {"executable": sys.executable},
                          "node": {"executable": sys.executable}}, "tasks": {"TX": record}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    claim_dir = tmp_path / "attempts/TX/001"
    claim_dir.mkdir(parents=True)
    (claim_dir / "claim.json").write_text('{"claimed":true}', encoding="utf-8")
    return {"run_root": tmp_path, "task_id": "TX", "check_id": checks[0],
            "plan_path": tmp_path / "tasks.json", "checks_path": tmp_path / "checks.json"}


def test_success_and_two_checks_append_without_overwriting_claim(tmp_path):
    args = fake_run(tmp_path, "print('1 passed')", checks=("fake", "second"))
    assert run(**args)["status"] == "passed"
    assert run(**(args | {"check_id": "second"}))["status"] == "passed"
    state = read(tmp_path / "state.json")
    assert state["tasks"]["TX"]["status"] == "verifying"
    assert [item["check_id"] for item in state["tasks"]["TX"]["check_results"]] == ["fake", "second"]
    assert (tmp_path / "attempts/TX/001/claim.json").is_file()
    assert (tmp_path / "attempts/TX/001/checks/fake/run-001/raw.log").is_file()
    assert (tmp_path / "attempts/TX/001/checks/second/run-001/raw.log").is_file()
    with pytest.raises(ValueError, match="already registered"):
        run(**args)


@pytest.mark.parametrize("script,output", [
    ("print('1 failed'); print('0 failed')", True),
    ("print('1 passed')", False),
    ("print('0 passed, 1 skipped')", True),
    ("print('no tests ran')", True),
])
def test_false_success_and_missing_output_fail_with_distinct_retry(tmp_path, script, output):
    args = fake_run(tmp_path, script, output=output)
    result = run(**args)
    assert result["status"] == "failed"
    first = tmp_path / "attempts/TX/001/checks/fake/run-001/receipt.json"
    assert first.is_file()
    assert read(tmp_path / "state.json")["tasks"]["TX"]["status"] == "failed"
    if not output:
        (tmp_path / "repo/result.txt").write_text("artifact", encoding="utf-8")
    # Change the frozen check in a controlled synthetic plan and rebind its digest.
    checks = read(args["checks_path"])
    checks["checks"][0]["command"]["args"] = ["-c", "print('1 passed')"]
    args["checks_path"].write_text(json.dumps(checks), encoding="utf-8")
    state = read(tmp_path / "state.json")
    state["plan_digest"] = __import__("hashlib").sha256(canonical({"plan": read(args["plan_path"]), "checks": checks})).hexdigest()
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert run(**args, retry=True)["status"] == "passed"
    assert first.is_file() and (tmp_path / "attempts/TX/002/checks/fake/run-001/receipt.json").is_file()


def test_source_change_and_deleted_source_are_unattached_failures(tmp_path):
    args = fake_run(tmp_path, f"from pathlib import Path;Path({str(tmp_path / 'repo/input.txt')!r}).write_text('changed');print('1 passed')")
    assert run(**args)["status"] == "failed"
    assert read(tmp_path / "state.json")["tasks"]["TX"]["status"] == "failed"
    args2 = fake_run(tmp_path / "other", f"from pathlib import Path;Path({str(tmp_path / 'other/repo/input.txt')!r}).unlink();print('1 passed')")
    with pytest.raises(FileNotFoundError):
        run(**args2)
    assert read(tmp_path / "other/state.json")["tasks"]["TX"]["status"] == "failed"
    assert (tmp_path / "other/attempts/TX/001/checks/fake/run-001/failure.json").is_file()


def test_junit_is_authoritative_and_unittest_terminal_is_strict(tmp_path):
    pytest_check = {"command": {"args": ["-m", "pytest", "tests/harness"]}}
    report = tmp_path / "pytest.xml"
    report.write_text('<testsuites><testsuite tests="2" failures="0" errors="1" skipped="0"/></testsuites>', encoding="utf-8")
    counts, okay = parse_counts("2 passed in 0.01s\n0 failed in 0.01s", pytest_check, report)
    assert not okay and counts["failed"] == 1
    report.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="2"/></testsuites>', encoding="utf-8")
    assert not parse_counts("2 passed", pytest_check, report)[1]
    assert not parse_counts("2 passed", pytest_check, tmp_path / "missing.xml")[1]
    unittest_check = {"command": {"args": ["-m", "unittest"]}}
    assert not parse_counts("OK\nRan 2 tests in 0.01s\nFAILED (failures=1)\n", unittest_check)[1]
    assert not parse_counts("Ran 0 tests in 0.01s\nOK\n", unittest_check)[1]


def job_env(tmp_path):
    system = os.environ.get("SystemRoot", r"C:\Windows")
    return {"SystemRoot": system, "WINDIR": system, "PATH": str(Path(system) / "System32"),
            "TEMP": str(tmp_path), "TMP": str(tmp_path), "_BROKER_PYTHON": sys.executable}


def test_real_orphan_grandchild_and_timeout_leave_unrelated_sentinel(tmp_path):
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"],
                                creationflags=0x08000000)
    try:
        spawn = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);print('1 passed')"
        orphan = execute_owned([sys.executable, "-c", spawn], tmp_path, job_env(tmp_path), 5, tmp_path / "orphan.log")
        assert orphan["exit_code"] == 0 and orphan["lingering"] > 0
        assert sentinel.poll() is None
        timed = execute_owned([sys.executable, "-c", "import time;time.sleep(20)"], tmp_path,
                              job_env(tmp_path), 0.3, tmp_path / "timeout.log")
        assert timed["timed_out"] and timed["protocol_valid"] is False
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


def test_minimal_profile_environment_blocks_inherited_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("PYTHONPATH", "C:/malicious")
    monkeypatch.setenv("NODE_OPTIONS", "--require=malicious")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--disable-warnings")
    monkeypatch.setenv("USERPROFILE", "C:/private")
    check = {"scope": "platform", "id": "fake"}
    state = {"runtimes": {"node": {"executable": sys.executable}}, "repositories": {}}
    check_dir = tmp_path / "含 空格" / "check"
    env = environment(tmp_path, check_dir, check, state)
    assert "OPENAI_API_KEY" not in env and "NODE_OPTIONS" not in env and "PYTHONPATH" not in env
    assert env["USERPROFILE"].startswith(str(tmp_path)) and env["APPDATA"].startswith(str(tmp_path))
    assert "malicious" not in env["PYTEST_ADDOPTS"]
    assert shlex.split(env["PYTEST_ADDOPTS"]) == [
        f"--basetemp={(check_dir / 'temp/pytest').as_posix()}",
        f"--junitxml={(check_dir / 'pytest.xml').as_posix()}",
    ]


def test_atomic_revision_has_one_winner_and_preserves_snapshot(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"revision":0}', encoding="utf-8")
    original = state_path.read_bytes()

    def write():
        try:
            atomic_state(state_path, original, {"revision": 0, "winner": True}, tmp_path)
            return True
        except (RuntimeError, FileExistsError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: write(), range(2))) == [False, True]
    assert read(state_path)["revision"] == 1
    assert (tmp_path / "state.revision-000.json").read_bytes() == original


def test_atomic_revision_recovers_after_replace_failure(tmp_path, monkeypatch):
    import run_check
    state_path = tmp_path / "state.json"
    state_path.write_text('{"revision":0}', encoding="utf-8")
    original = state_path.read_bytes()
    real_replace = run_check.os.replace

    def fail_once(*args):
        monkeypatch.setattr(run_check.os, "replace", real_replace)
        raise OSError("simulated replace failure")

    monkeypatch.setattr(run_check.os, "replace", fail_once)
    with pytest.raises(OSError, match="replace failure"):
        atomic_state(state_path, original, {"revision": 0}, tmp_path)
    assert state_path.read_bytes() == original
    assert (tmp_path / "state.revision-000.json").read_bytes() == original
    atomic_state(state_path, original, {"revision": 0, "recovered": True}, tmp_path)
    assert read(state_path) == {"revision": 1, "recovered": True}
    with pytest.raises(RuntimeError, match="Conflicting prior revision snapshot"):
        state_path.write_text('{"revision":0,"different":true}', encoding="utf-8")
        atomic_state(state_path, state_path.read_bytes(), {"revision": 0}, tmp_path)


def test_concurrent_writer_does_not_get_marked_failed_by_other_run(tmp_path):
    args = fake_run(tmp_path, "import time;time.sleep(0.6);print('1 passed')")

    def check():
        with pytest.raises(RuntimeError, match="State changed"):
            run(**args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(check)
        deadline = time.monotonic() + 5
        while not (tmp_path / "attempts/TX/001/checks/fake/run-001/owner.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        original = (tmp_path / "state.json").read_bytes()
        state = json.loads(original)
        state["external_writer"] = True
        atomic_state(tmp_path / "state.json", original, state, tmp_path)
        future.result(timeout=5)
    state = read(tmp_path / "state.json")
    assert state["external_writer"] and state["tasks"]["TX"]["status"] == "running"
    assert (tmp_path / "attempts/TX/001/checks/fake/run-001/failure.json").is_file()


def test_windows_cmd_is_shell_safe_only_for_frozen_simple_args(tmp_path):
    args = fake_run(tmp_path, "unused")
    command = tmp_path / "known.cmd"
    command.write_bytes(b"@echo off\r\necho 1 passed\r\n")
    checks = read(args["checks_path"])
    checks["checks"][0]["command"] = {"runtime": "pnpm", "cwd": "platform", "args": ["run", "build"]}
    args["checks_path"].write_text(json.dumps(checks), encoding="utf-8")
    state = read(tmp_path / "state.json")
    state["runtimes"]["pnpm"] = {"executable": str(command)}
    state["plan_digest"] = __import__("hashlib").sha256(canonical({"plan": read(args["plan_path"]), "checks": checks})).hexdigest()
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert run(**args)["status"] == "passed"
    checks["checks"][0]["command"]["args"] = ["run", "build&echo secret"]
    args["checks_path"].write_text(json.dumps(checks), encoding="utf-8")
    state = read(tmp_path / "state.json")
    state["status"] = "synthetic new check"
    state["tasks"]["TX"].update(status="running", attempt=2, check_results=[])
    state["plan_digest"] = __import__("hashlib").sha256(canonical({"plan": read(args["plan_path"]), "checks": checks})).hexdigest()
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe .cmd argument"):
        run(**args)
    assert read(tmp_path / "state.json")["tasks"]["TX"]["status"] == "failed"


def test_broker_assignment_failure_terminates_own_gated_process(tmp_path, monkeypatch):
    def reject(self, process):
        raise OSError("simulated assignment failure")

    monkeypatch.setattr(OwnedJob, "assign", reject)
    with pytest.raises(OSError, match="assignment failure"):
        execute_owned([sys.executable, "-c", "print('1 passed')"], tmp_path,
                      job_env(tmp_path), 2, tmp_path / "assignment.log")


def test_legacy_import_keeps_original_identity_and_rejects_tampering(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import import_legacy
    monkeypatch.setattr(import_legacy, "validate_run", lambda _: None)
    log = tmp_path / "old.log"
    log.write_text("actual old check\n", encoding="utf-8")
    receipt = tmp_path / "old.json"
    state = {"revision": 7, "plan_digest": "plan-identity", "repositories": {"platform": str(tmp_path)},
             "tasks": {task: {"status": "done", "check_results": [], "source_inputs": [], "outputs": []} for task in
                                       ("T001", "T002", "T003", "T004", "T005")}}
    from run_check import binding
    receipt.write_text(json.dumps({"task_id": "T001", "check_id": "inventory", "status": "passed",
                                   "exit_code": 0, "evidence_kind": "source", "binding_digest": binding("plan-identity", "T001", state["tasks"]["T001"]),
                                   "log": {"path": "old.log", "sha256": sha(log)},
                                   "source_revision": {"platform": "original-head"}}), encoding="utf-8")
    state["tasks"]["T001"]["check_results"] = [{"check_id": "inventory", "path": "old.json", "sha256": sha(receipt)}]
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    catalog = {"checks": [{"id": "inventory", "evidence_kind": "source"}]}
    (tmp_path / "workspaces/ocr/docs/platform-automation").mkdir(parents=True)
    (tmp_path / "workspaces/ocr/docs/platform-automation/checks.json").write_text(json.dumps(catalog), encoding="utf-8")
    imported = import_legacy.import_receipts(tmp_path, tmp_path / "import.json")
    assert len(imported["receipts"]) == 1
    assert imported["receipts"][0]["execution_mode"] == "manual_pre_T006"
    assert imported["receipts"][0]["original_receipt_sha256"] == sha(receipt)
    assert imported["receipts"][0]["original_binding_digest"] == binding("plan-identity", "T001", state["tasks"]["T001"])
    assert sha(receipt) == read(tmp_path / "state.json")["tasks"]["T001"]["check_results"][0]["sha256"]
    altered = read(receipt)
    altered["binding_digest"] = "wrong"
    receipt.write_text(json.dumps(altered), encoding="utf-8")
    state["tasks"]["T001"]["check_results"][0]["sha256"] = sha(receipt)
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="binding invalid"):
        import_legacy.import_receipts(tmp_path, tmp_path / "tampered.json")
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    state["tasks"]["T001"]["source_inputs"] = [{"repo": "platform", "path": "source.txt", "sha256": sha(source)}]
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    source.write_text("after", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale original"):
        import_legacy.import_receipts(tmp_path, tmp_path / "stale.json")


def test_claim_freeze_multi_check_recover_complete_workflow(tmp_path, monkeypatch):
    import state_tasks
    args = fake_run(tmp_path, "print('1 passed')", checks=("fake", "second"))
    state = read(tmp_path / "state.json")
    state["tasks"]["TX"].update(status="pending", attempt=0, source_inputs=[], outputs=[])
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def planner(_, mode, *, state=None):
        if mode == "next":
            return {"ready": [{"id": "TX"}]}
        candidate = read(state or tmp_path / "state.json")
        return {"valid": True, "issues": {}, "verified_count": 1 if candidate["tasks"]["TX"]["status"] == "done" else 0}

    monkeypatch.setattr(state_tasks, "planner", planner)
    assert state_tasks.action(tmp_path, "TX", "claim")["attempt"] == 1
    state_tasks.action(tmp_path, "TX", "freeze", sources=["platform:input.txt"], outputs=["platform:result.txt"])
    assert run(**args)["status"] == "passed"
    assert run(**(args | {"check_id": "second"}))["status"] == "passed"
    assert state_tasks.action(tmp_path, "TX", "recover")["receipts"][0]["hash_valid"]
    assert state_tasks.action(tmp_path, "TX", "complete")["status"] == "done"
    assert read(tmp_path / "state.json")["tasks"]["TX"]["status"] == "done"
    assert sorted(path.name for path in tmp_path.glob("state.revision-*.json")) == [
        "state.revision-000.json", "state.revision-001.json", "state.revision-002.json",
        "state.revision-003.json", "state.revision-004.json"]


def test_reverify_waits_for_other_active_task_and_archives_old_evidence(tmp_path):
    import state_tasks
    args = fake_run(tmp_path, "print('1 passed')")
    docs = tmp_path / "workspaces/ocr/docs/platform-automation"
    docs.mkdir(parents=True)
    (docs / "tasks.json").write_bytes(args["plan_path"].read_bytes())
    state_path = tmp_path / "state.json"
    state = read(state_path)
    old = {"check_id": "fake", "path": "old-receipt.json", "sha256": "a" * 64}
    state["tasks"]["TX"].update(status="done", check_results=[old])
    state["tasks"]["TY"] = {"status": "running", "attempt": 1}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Finish active task"):
        state_tasks.action(tmp_path, "TX", "reverify")
    assert not (tmp_path / "reverify-history").exists()
    state["tasks"]["TY"]["status"] = "pending"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = state_tasks.action(tmp_path, "TX", "reverify")
    assert result["status"] == "running" and result["attempt"] == 2
    archived = read(tmp_path / "reverify-history/TX/revision-000.json")
    assert archived["record"]["check_results"] == [old]
    assert read(state_path)["tasks"]["TX"]["check_results"] == []


def test_retry_and_runner_enforce_attempt_limit_and_single_active(tmp_path):
    import state_tasks
    args = fake_run(tmp_path, "print('1 passed')")
    docs = tmp_path / "workspaces/ocr/docs/platform-automation"
    docs.mkdir(parents=True)
    plan = read(args["plan_path"])
    plan["tasks"][0]["max_attempts"] = 1
    (docs / "tasks.json").write_text(json.dumps(plan), encoding="utf-8")
    args["plan_path"].write_text(json.dumps(plan), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = read(state_path)
    state["plan_digest"] = __import__("hashlib").sha256(canonical({"plan": plan, "checks": read(args["checks_path"])})).hexdigest()
    state["tasks"]["TX"].update(status="failed", attempt=1)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="attempt limit"):
        state_tasks.action(tmp_path, "TX", "retry")
    with pytest.raises(ValueError, match="attempt limit"):
        run(**args, retry=True)
    state = read(state_path)
    state["tasks"]["TX"].update(status="running", attempt=1)
    state["tasks"]["TY"] = {"status": "verifying", "attempt": 1}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Another task is active"):
        run(**args)
