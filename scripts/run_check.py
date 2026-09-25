"""Run one frozen check, bind evidence, and atomically register its receipt.

Usage: python scripts/run_check.py --run-root <run> --task T010 --check platform_unit
The run's OCR worktree supplies the frozen tasks/checks bundle. Never accepts a
command, shell fragment, repository root, or executable from a request payload.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import msvcrt
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from windows_job import KERNEL, OwnedJob

ROOT = Path(__file__).resolve().parents[1]
BROKER = Path(__file__).with_name("harness_child.py")
SUMMARY = re.compile(r"(?<!\d)(\d+)\s+(passed|failed|skipped|errors?|deselected|xfailed|xpassed)\b", re.I)
CMD_ARG = re.compile(r"^[A-Za-z0-9_./:=+@-]+$")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def relative(value):
    if not isinstance(value, str) or not value or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValueError("Unsafe relative path")
    normalized = value.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts or ":" in normalized or "\x00" in normalized:
        raise ValueError("Path traversal or drive forbidden")
    return normalized


def contained(root, name):
    root = Path(root).resolve()
    target = (root / relative(name)).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Resolved path escapes repository")
    return target


def entry(root, repo, name):
    path = contained(root, name)
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"repo": repo, "path": relative(name), "sha256": sha(path)}


def binding(plan_digest, task_id, record):
    normalized = lambda group: sorted(({key: item[key] for key in ("repo", "path", "sha256")}
                                        for item in record[group]), key=lambda item: (item["repo"], item["path"]))
    return hashlib.sha256(canonical({"plan_digest": plan_digest, "task_id": task_id,
                                     "source_inputs": normalized("source_inputs"),
                                     "outputs": normalized("outputs")})).hexdigest()


def test_counts(output, check, report=None):
    args = check["command"]["args"]
    required = ("pytest" in args or "unittest" in args or args[:2] == ["run", "test"]
                or any(arg.replace("\\", "/").startswith("tests/") for arg in args)
                or check.get("requires_test_counts") is True)
    if not required:
        return None, True
    if "pytest" in args:
        if report is None or not Path(report).is_file():
            return {"passed": 0, "failed": 1, "skipped": 0}, False
        root = ET.parse(report).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is None:
            return {"passed": 0, "failed": 1, "skipped": 0}, False
        total = int(suite.attrib["tests"])
        failures = int(suite.attrib.get("failures", 0)) + int(suite.attrib.get("errors", 0))
        skipped = int(suite.attrib.get("skipped", 0))
        counts = {"passed": total - failures - skipped, "failed": failures, "skipped": skipped}
        return counts, counts["passed"] > 0 and failures == 0
    if "unittest" in args:
        ran = re.search(r"Ran (\d+) tests?\b", output)
        skips = re.search(r"skipped=(\d+)", output)
        terminal = [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("HARNESS_")]
        good = bool(terminal and re.fullmatch(r"OK(?: \(skipped=\d+\))?", terminal[-1]))
        errors = bool(re.search(r"^FAILED \(", output, re.M))
        total = int(ran.group(1)) if ran else 0
        skipped = int(skips.group(1)) if skips else 0
        counts = {"passed": max(0, total - skipped) if good and not errors else 0,
                  "failed": 0 if good and not errors else 1, "skipped": skipped}
        return counts, good and total > skipped and not errors
    lines = [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("HARNESS_")]
    terminal = next((line for line in reversed(lines) if SUMMARY.search(line)), "")
    found = {name.lower(): int(number) for number, name in SUMMARY.findall(terminal)}
    counts = {key: found.get(key, 0) for key in ("passed", "failed", "skipped")}
    errors = found.get("error", 0) + found.get("errors", 0)
    earlier_failures = any(int(number) > 0 and kind.lower() in {"failed", "error", "errors"}
                           for number, kind in SUMMARY.findall(output))
    return counts, counts["passed"] > 0 and counts["failed"] == 0 and errors == 0 and not earlier_failures


def environment(run_root, check_dir, check, state):
    root = Path(run_root).resolve()
    temp = check_dir / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    profile = check_dir / "profile"
    roaming = profile / "AppData/Roaming"
    local = profile / "AppData/Local"
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    system = os.environ.get("SystemRoot", r"C:\Windows")
    result = {"SystemRoot": system, "WINDIR": system, "COMSPEC": str(Path(system) / "System32/cmd.exe"),
              "PATH": str(Path(state["runtimes"]["node"]["executable"]).parent) + os.pathsep
                      + str(Path(system) / "System32") + os.pathsep + str(Path(system)),
              "PATHEXT": ".COM;.EXE;.BAT;.CMD", "TEMP": str(temp), "TMP": str(temp),
              "USERPROFILE": str(profile), "HOME": str(profile), "APPDATA": str(roaming),
              "LOCALAPPDATA": str(local), "npm_config_cache": str(root / "cache/npm"),
              "NPM_CONFIG_USERCONFIG": str(check_dir / "empty-npmrc"),
              "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
              "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": f"--basetemp={temp / 'pytest'} --junitxml={check_dir / 'pytest.xml'}"}
    (check_dir / "empty-npmrc").touch(exist_ok=False)
    if check["scope"] == "ocr":
        result["PYTHONPATH"] = str(Path(state["repositories"]["ocr"]) / "src")
    if check["scope"] == "classification":
        data = root / "workspaces/classification-data"
        data.mkdir(parents=True, exist_ok=True)
        result["ICP_DATA_DIR"] = str(data)
    if check["id"] == "fixtures":
        registered = state["runtimes"]["fixture_python"]
        candidate = Path(registered["executable"])
        if not candidate.is_file() or sha(contained(state["repositories"]["platform"], registered["lock_path"])) != registered["lock_sha256"]:
            raise ValueError("Registered fixture runtime or lock changed")
        result["WORKBENCH_FIXTURE_PYTHON"] = str(candidate)
    return result


def process_creation_identity(process):
    created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
    KERNEL.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    KERNEL.GetProcessTimes.restype = ctypes.c_int
    if not KERNEL.GetProcessTimes(process._handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
    return {"pid": process.pid, "creation_filetime": created.value}


def git_head(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def execute_owned(argv, cwd, env, timeout, log):
    """Broker cannot spawn until assigned; job includes children even after parent exits."""
    platform_python = env.pop("_BROKER_PYTHON")
    with Path(log).open("xb") as output, OwnedJob() as job:
        broker = subprocess.Popen([platform_python, "-I", "-S", "-u", str(BROKER)], cwd=ROOT, env=env,
                                  stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                  creationflags=0x08000000)  # CREATE_NO_WINDOW
        try:
            identity = process_creation_identity(broker)
            job.assign(broker)
            broker.stdin.write((json.dumps({"argv": argv, "cwd": str(cwd), "env": env}) + "\n").encode("utf-8"))
            broker.stdin.close()
            timed_out = False
            try:
                exit_code = broker.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
            if not timed_out:
                deadline = time.monotonic() + 0.25
                while job.active() and time.monotonic() < deadline:
                    time.sleep(0.02)
            if timed_out or job.active():
                lingering = job.active()
                job.terminate()
            else:
                lingering = 0
        except BaseException:
            # If assignment failed, the broker is still our Popen handle and
            # blocked on stdin; terminate/wait this exact process, not its PID.
            if broker.poll() is None:
                broker.terminate()
                broker.wait(timeout=10)
            if job.active():
                job.terminate()
            raise
    content = Path(log).read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"^HARNESS_RESULT:(\{.*\})$", content, re.M)
    protocol = len(matches) == 1 and not timed_out and exit_code is not None
    child_result = json.loads(matches[0]) if protocol else None
    protocol = protocol and child_result["exit_code"] == exit_code
    return {"exit_code": exit_code, "timed_out": timed_out, "lingering": lingering,
            "protocol_valid": protocol, "process_identity": identity, "output": content}


@contextmanager
def state_lock(run_root):
    lock = Path(run_root) / "state.lock"
    with lock.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another state writer holds the OS lock") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def atomic_state(path, original, state, run_root):
    with state_lock(run_root):
        if Path(path).read_bytes() != original:
            raise RuntimeError("State revision changed during check; evidence not attached")
        prior = Path(run_root) / f"state.revision-{state['revision']:03d}.json"
        try:
            with prior.open("xb") as stream:
                stream.write(original)
        except FileExistsError:
            # A previous atomic replacement may have failed after the snapshot.
            # Only the exact same predecessor can safely reuse that evidence.
            if prior.read_bytes() != original:
                raise RuntimeError("Conflicting prior revision snapshot")
        state["revision"] += 1
        temp = Path(run_root) / f"state.check-{state['revision']}-{uuid.uuid4().hex}.tmp"
        try:
            temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _run(run_root, task_id, check_id, *, plan_path=None, checks_path=None, retry=False, owner_token=None):
    run_root = Path(run_root).resolve()
    state_path = run_root / "state.json"
    plan_path = Path(plan_path or run_root / "workspaces/ocr/docs/platform-automation/tasks.json")
    checks_path = Path(checks_path or run_root / "workspaces/ocr/docs/platform-automation/checks.json")
    plan, checks = read(plan_path), read(checks_path)
    digest = hashlib.sha256(canonical({"plan": plan, "checks": checks})).hexdigest()
    original = state_path.read_bytes()
    state = json.loads(original)
    if state["plan_digest"] != digest:
        raise ValueError("Frozen plan/check digest mismatch")
    task = next(item for item in plan["tasks"] if item["id"] == task_id)
    check = next(item for item in checks["checks"] if item["id"] == check_id)
    task_map = {item["id"]: item for item in plan["tasks"]}
    def ancestors(key):
        parents = task_map[key].get("depends_on", [])
        return set(parents).union(*(ancestors(parent) for parent in parents))
    if (check_id not in task["required_checks"] or
            check.get("producer_task") not in {task_id, *ancestors(task_id)}
            or check["kind"] != "command" or not check["command"]):
        raise ValueError("Check producer is not this task or its dependency, or command is missing")
    record = state["tasks"][task_id]
    if retry and record["status"] == "failed":
        if record["attempt"] >= task.get("max_attempts", 3):
            raise ValueError("Retry attempt limit reached")
        record["attempt"] += 1
        record["status"] = "running"
        record["check_results"] = []
    if any(key != task_id and value["status"] in {"running", "verifying"}
           for key, value in state["tasks"].items()):
        raise ValueError("Another task is active")
    if record["attempt"] > task.get("max_attempts", record["attempt"]):
        raise ValueError("Task attempt limit exceeded")
    if record["status"] not in {"running", "verifying"} or record["attempt"] < 1 or not record["source_inputs"]:
        raise ValueError("Task must be claimed with complete source inputs")
    if any(item["check_id"] == check_id for item in record["check_results"]):
        raise ValueError("Check already registered; reverify using a new task attempt")
    attempt = run_root / "attempts" / task_id / f"{record['attempt']:03d}"
    check_dir = attempt / "checks" / check_id / "run-001"
    check_dir.mkdir(parents=True, exist_ok=False)
    write_new(check_dir / "owner.json", {"token": owner_token, "task": task_id, "check": check_id,
                                         "state_revision": state["revision"]})
    roots = {name: Path(path) for name, path in state["repositories"].items()} | {"run": run_root}
    source_before = [entry(roots[item["repo"]], item["repo"], item["path"]) for item in record["source_inputs"]]
    if source_before != record["source_inputs"]:
        raise ValueError("Source input changed before launch")
    outputs_before = [entry(roots[item["repo"]], item["repo"], item["path"]) for item in record["outputs"]
                      if contained(roots[item["repo"]], item["path"]).is_file()]
    write_new(check_dir / "before.json", {"source_inputs": source_before, "existing_outputs": outputs_before,
                                       "state_revision": state["revision"]})
    command = check["command"]
    runtime = Path(state["runtimes"][command["runtime"]]["executable"])
    if not runtime.is_absolute() or not runtime.is_file():
        raise ValueError("Registered absolute runtime missing")
    prefix, *parts = command["cwd"].replace("\\", "/").split("/")
    if prefix not in state["repositories"]:
        raise ValueError("Unknown cwd repository")
    cwd = contained(roots[prefix], "/".join(parts)) if parts else roots[prefix].resolve()
    if not cwd.is_dir():
        raise ValueError("Frozen cwd missing")
    args = command["args"]
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("Frozen args must be strings")
    argv = [str(runtime)] + [arg.replace("{run_root}", str(run_root)) for arg in args]
    if runtime.suffix.lower() == ".cmd" and not all(CMD_ARG.fullmatch(arg) for arg in argv[1:]):
        raise ValueError("Unsafe .cmd argument in frozen check")
    env = environment(run_root, check_dir, check, state)
    env["_BROKER_PYTHON"] = state["runtimes"]["platform_python"]["executable"]
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    outcome = execute_owned(argv, cwd, env, check["timeout_seconds"], check_dir / "raw.log")
    finished = dt.datetime.now(dt.timezone.utc).isoformat()
    source_after = [entry(roots[item["repo"]], item["repo"], item["path"]) for item in record["source_inputs"]]
    outputs_after = [entry(roots[item["repo"]], item["repo"], item["path"]) for item in outputs_before]
    write_new(check_dir / "after.json", {"source_inputs": source_after, "existing_outputs": outputs_after})
    required = {(item["repo"], item["path"]) for item in task["outputs"]}
    outputs = {(item["repo"], item["path"]) for item in record["outputs"]} | required
    missing = [(repo, path) for repo, path in outputs if not contained(roots[repo], path).is_file()]
    final_outputs = [entry(roots[repo], repo, path) for repo, path in sorted(outputs) if (repo, path) not in missing]
    counts, counts_ok = test_counts(outcome["output"], check, check_dir / "pytest.xml")
    unchanged = source_before == source_after and outputs_before == outputs_after
    passed = (outcome["exit_code"] == 0 and not outcome["timed_out"] and not outcome["lingering"]
              and outcome["protocol_valid"] and counts_ok and unchanged and not missing)
    final_binding = binding(digest, task_id, record | {"outputs": final_outputs})
    if any(read(contained(run_root, item["path"]))["binding_digest"] != final_binding
           for item in record["check_results"]):
        passed = False
    log_relative = (check_dir / "raw.log").relative_to(run_root).as_posix()
    receipt_relative = (check_dir / "receipt.json").relative_to(run_root).as_posix()
    result = {"task_id": task_id, "check_id": check_id, "status": "passed" if passed else "failed",
              "exit_code": outcome["exit_code"], "evidence_kind": check["evidence_kind"],
              "binding_digest": final_binding,
              "log": {"path": log_relative, "sha256": sha(check_dir / "raw.log")},
              "test_counts": counts, "started_at": started, "finished_at": finished,
              "argv": argv, "cwd": str(cwd), "runtime": str(runtime),
              "source_revision": {repo: git_head(root) for repo, root in state["repositories"].items()},
              "environment": {"broker_process": outcome["process_identity"], "job_object": True,
                              "timed_out": outcome["timed_out"], "lingering_processes": outcome["lingering"],
                              "protocol_valid": outcome["protocol_valid"], "source_unchanged": unchanged,
                              "missing_outputs": missing, "state_revision_before": state["revision"],
                              "prior_receipt_binding_unchanged": all(read(contained(run_root, item["path"]))["binding_digest"] == final_binding
                                                                     for item in record["check_results"]),
                              "environment_keys": sorted(key for key in env if key != "_BROKER_PYTHON")},
              "limitations": ["Check proves only the frozen command and declared outputs, not unlisted business behavior."]}
    write_new(check_dir / "receipt.json", result)
    if state_path.read_bytes() != original:
        raise RuntimeError("State changed during check; receipt remains unattached in attempt directory")
    record["status"] = "verifying" if passed else "failed"
    if passed:
        record["outputs"] = final_outputs
        record["check_results"].append({"check_id": check_id,
                                        "path": receipt_relative,
                                        "sha256": sha(check_dir / "receipt.json")})
    atomic_state(state_path, original, state, run_root)
    return result


def run(run_root, task_id, check_id, *, plan_path=None, checks_path=None, retry=False):
    run_root = Path(run_root).resolve()
    state_path = run_root / "state.json"
    expected_bytes = state_path.read_bytes()
    expected_state = json.loads(expected_bytes)
    attempt_number = expected_state["tasks"][task_id]["attempt"] + (1 if retry and expected_state["tasks"][task_id]["status"] == "failed" else 0)
    check_dir = run_root / "attempts" / task_id / f"{attempt_number:03d}" / "checks" / check_id / "run-001"
    token = uuid.uuid4().hex
    try:
        return _run(run_root, task_id, check_id, plan_path=plan_path, checks_path=checks_path,
                    retry=retry, owner_token=token)
    except Exception as exc:
        owner_file = check_dir / "owner.json"
        if owner_file.is_file() and read(owner_file)["token"] == token:
            write_new(check_dir / "failure.json", {"error_type": type(exc).__name__, "message": str(exc),
                                                   "status": "failed", "receipt_attached": False})
            state = json.loads(expected_bytes)
            record = state["tasks"][task_id]
            if state_path.read_bytes() == expected_bytes and record["status"] in {"running", "verifying", "failed"}:
                if retry and record["status"] == "failed":
                    record["attempt"] += 1
                record["status"] = "failed"
                atomic_state(state_path, expected_bytes, state, run_root)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--check", required=True)
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    result = run(args.run_root, args.task, args.check, retry=args.retry)
    print(json.dumps({"status": result["status"], "receipt": result["log"]["path"],
                      "binding_digest": result["binding_digest"]}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Check runner error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
