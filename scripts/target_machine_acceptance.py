"""Run bounded, same-machine checks on an explicitly selected Agent candidate.

Example (from any directory, using a NEW output path)::

    & 'D:\\candidate-02\\runtimes\\service\\python.exe' -X utf8 -B -I `
        'D:\\candidate-02\\tools\\target_machine_acceptance.py' `
        --bundle 'D:\\candidate-02' --output 'D:\\acceptance-02'

This never modifies the candidate. It cannot establish a clean Windows install,
physical network disconnection, another user's DPAPI access, or real OCR quality.
The package regression/upgrade suite remains scripts/audit_agent_bundle.py.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import time
import winreg


SCRIPT = Path(__file__).resolve()
TOKEN = "synthetic-target-machine-token"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def contained_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("Unsafe manifest path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Unsafe manifest path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("Manifest path crosses a link: " + name)
    if not current.is_file() or not current.resolve().is_relative_to(root):
        raise ValueError("Missing or escaped manifest file: " + name)
    return current


def verify_manifest(bundle: Path) -> dict:
    manifest = contained_file(bundle, "manifest.json")
    records = json.loads(manifest.read_text("utf-8"))["files"]
    names = set()
    size = 0
    before = {}
    for record in records:
        name = record["path"]
        if name in names or name == "manifest.json":
            raise ValueError("Duplicate or self-indexed manifest entry: " + name)
        names.add(name)
        path = contained_file(bundle, name)
        stat = path.stat()
        if stat.st_size != record["bytes"] or digest(path) != record["sha256"]:
            raise ValueError("Candidate content differs from manifest: " + name)
        size += stat.st_size
        before[name] = (stat.st_size, record["sha256"])
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Candidate contains link: " + str(path))
        if path.is_file():
            actual.add(path.relative_to(bundle).as_posix())
    if actual != names | {"manifest.json"}:
        raise ValueError("Candidate file membership differs from manifest")
    return {"status": "pass", "files": len(names), "bytes": size,
            "manifest_sha256": digest(manifest), "file_stats": before}


def verify_unchanged(bundle: Path, original: dict) -> dict:
    if digest(bundle / "manifest.json") != original["manifest_sha256"]:
        raise ValueError("Candidate manifest changed during checks")
    actual = {}
    for path in bundle.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Candidate gained a link during checks")
        if path.is_file() and path.relative_to(bundle).as_posix() != "manifest.json":
            stat = path.stat()
            actual[path.relative_to(bundle).as_posix()] = (stat.st_size, digest(path))
    if actual != original["file_stats"]:
        raise ValueError("Candidate files changed during checks")
    return {"status": "pass", "files": len(actual),
            "method": "full pre/post SHA256, membership, size and manifest SHA256"}


def check_layout(bundle: Path, names: set[str]) -> dict:
    required = {"app/ocr_workbench/service.py", "config/agent-policy.json",
                "config/runtime-locks/agent.json", "config/runtime-locks/agent.txt",
                "runtimes/service/python.exe", "tools/start_agent_candidate.py"}
    missing = sorted(required - names)
    if missing:
        raise ValueError("Missing Agent candidate files: " + repr(missing))
    policy = json.loads((bundle / "config/agent-policy.json").read_text("utf-8"))
    lock = json.loads((bundle / "config/runtime-locks/agent.json").read_text("utf-8"))
    if policy["feature_flags"]["agent_enabled"] is not False:
        raise ValueError("Agent policy is not default-off")
    if lock["tracing"] != "disabled" or lock["pickle_fallback"] is not False:
        raise ValueError("Agent lock permits tracing or pickle fallback")
    wheels = []
    for package in lock["packages"]:
        name = "wheelhouse/agent/" + package["file"]
        if name not in names or digest(contained_file(bundle, name)) != package["sha256"]:
            raise ValueError("Missing or changed offline wheel: " + name)
        wheels.append(name)
    entry = (bundle / "tools/start_agent_candidate.py").read_text("utf-8")
    if "agent_enabled=True" not in entry or "--data" not in entry:
        raise ValueError("Explicit entry no longer matches expected opt-in/isolation contract")
    return {"status": "pass", "policy_default_off": True, "explicit_entry": "tools/start_agent_candidate.py",
            "entry_sha256": digest(bundle / "tools/start_agent_candidate.py"),
            "locked_wheels_present_and_hashed": len(wheels), "lock_sha256": digest(bundle / "config/runtime-locks/agent.json"),
            "scope": "package files and declared entry source; runtime behavior checked separately"}


def isolated_environment(output: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "SYSTEMDRIVE") if key in os.environ}
    windows = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = str(windows / "System32") + os.pathsep + str(windows)
    for key, name in (("LOCALAPPDATA", "local"), ("APPDATA", "roaming"),
                      ("USERPROFILE", "profile"), ("TEMP", "temp"), ("TMP", "temp")):
        target = output / "isolated-user" / name
        target.mkdir(parents=True, exist_ok=True)
        env[key] = str(target)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONUTF8="1",
               LANGSMITH_TRACING="true", LANGCHAIN_TRACING_V2="true",
               LANGSMITH_API_KEY=TOKEN, LANGSMITH_ENDPOINT="https://target-audit.invalid")
    return env


async def worker(bundle: Path, output: Path) -> dict:
    if Path(sys.executable).resolve() != (bundle / "runtimes/service/python.exe").resolve():
        raise ValueError("Worker is not running candidate Python")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(bundle / "app"))
    attempts = []

    def network_guard(event, values):
        if event == "socket.connect":
            address = values[1]
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in {"127.0.0.1", "::1", "localhost"}:
                attempts.append({"event": event, "host": host})
                raise OSError("Target check blocks non-loopback Python socket")
        elif event == "socket.getaddrinfo":
            host = values[0]
            if host not in {None, "127.0.0.1", "::1", "localhost"}:
                attempts.append({"event": event, "host": str(host)})
                raise OSError("Target check blocks external Python DNS")

    sys.addaudithook(network_guard)
    import httpx
    import ocr_workbench.service
    from ocr_workbench.service import create_app
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    origin = Path(ocr_workbench.service.__file__).resolve()
    if not origin.is_relative_to(bundle / "app"):
        raise ValueError("Application was imported outside candidate")
    lock = json.loads((bundle / "config/runtime-locks/agent.json").read_text("utf-8"))
    dependencies = {}
    site = bundle / "runtimes/service/Lib/site-packages"
    for item in lock["packages"]:
        dist = importlib.metadata.distribution(item["name"])
        path = Path(dist.locate_file("")).resolve()
        if dist.version != item["version"] or not path.is_relative_to(site):
            raise ValueError("Dependency is not installed from candidate: " + item["name"])
        dependencies[item["name"]] = {"version": dist.version, "origin": str(path)}
    results = {}
    for enabled in (False, True):
        data = output / ("default-workspace" if not enabled else "opt-in-workspace")
        data.mkdir()
        app = create_app(bundle, data, TOKEN, start_queue=False, **({"agent_enabled": True} if enabled else {}))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver",
                                         headers={"Authorization": "Bearer " + TOKEN}) as client:
                health = await client.get("/api/health")
                home = await client.get("/")
                status_response = await client.get("/api/agent/status")
                status = status_response.json()
                if health.status_code != 200 or home.status_code != 200 or "<html" not in home.text:
                    raise ValueError("Local application health/static UI failed")
                if status_response.status_code != 200 or status["enabled"] is not enabled:
                    raise ValueError("Agent default/opt-in status differs: " + repr(status))
                if enabled:
                    manager = app.state.agent_runtime
                    if not status["available"] or manager is None or not isinstance(manager.checkpoints.saver, AsyncSqliteSaver):
                        raise ValueError("Opt-in official persistent saver unavailable")
                    if not manager.checkpoints.path.is_file():
                        raise ValueError("Opt-in saver did not create isolated checkpoint database")
                    if manager.checkpoints.saver.serde.pickle_fallback:
                        raise ValueError("Saver permits pickle fallback")
                    if os.environ.get("LANGSMITH_TRACING") != "false" or "LANGSMITH_API_KEY" in os.environ:
                        raise ValueError("Tracing credentials survived saver startup")
                    results["opt_in"] = {"status": "pass", "agent": status,
                                         "saver": "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver",
                                         "pickle_fallback": False, "checkpoint_created": manager.checkpoints.path.is_file(),
                                         "workspace": str(data)}
                else:
                    if (data / "agent-checkpoints.sqlite3").exists() or app.state.agent_runtime is not None:
                        raise ValueError("Default startup created Agent checkpoint/runtime")
                    results["default"] = {"status": "pass", "agent": status,
                                          "checkpoint_created": False, "workspace": str(data)}
    if attempts:
        raise ValueError("External Python network attempt occurred: " + repr(attempts))
    return {"status": "pass", "python": sys.version, "executable": sys.executable,
            "application_origin": str(origin), "locked_dependencies": dependencies,
            "network_guard": "process-local Python audit hook; physical network remains unverified",
            "external_python_network_attempts": attempts, **results}


def machine() -> dict:
    identity = subprocess.run(["whoami", "/user"], capture_output=True, text=True, timeout=10)
    return {"system": platform.system(), "release": platform.release(), "version": platform.version(),
            "machine": platform.machine(), "python_driver": sys.executable,
            "identity_observation": identity.stdout.strip() if identity.returncode == 0 else "not_tested",
            "clean_install": "not_tested", "different_windows_user_dpapi": "not_tested",
            "physical_disconnection": "not_tested", "prior_machine_comparison": "not_tested"}


def legacy_machine_identity() -> str:
    """Identity used by the pre-existing clean-Windows/A4000 receipt contract."""
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
        value = winreg.QueryValueEx(key, "MachineGuid")[0]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def legacy_environment() -> dict:
    command = "$cpu=Get-CimInstance Win32_Processor;$pc=Get-CimInstance Win32_ComputerSystem;@{cpu=($cpu.Name -join ', ');ram_bytes=$pc.TotalPhysicalMemory}|ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("无法读取 Windows 硬件信息")
    return {"machine_sha256": legacy_machine_identity(), "windows": platform.platform(),
            **json.loads(result.stdout), "development_commands": {
                name: shutil.which(name) for name in
                ["python", "python3", "py", "node", "npm", "nvcc", "docker", "git"]}}


# Keep the old import surface as well as its command-line contract.
machine_identity = legacy_machine_identity
environment = legacy_environment


def legacy_main() -> int:
    """Preserve the existing, opt-in external-machine qualification entry."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--role", choices=["clean-windows", "a4000"], required=True)
    parser.add_argument("--confirm-no-development-environment", action="store_true",
                        help="Operator attests this is a separate machine without development dependencies")
    parser.add_argument("--os-isolation-evidence", type=Path, required=True,
                        help="Completed offline_guard --application output directory on this same machine")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("请使用新的验收输出目录")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "role": args.role, "errors": [], "checks": [],
              "bundle": str(args.bundle.resolve())}
    try:
        report["environment"] = legacy_environment()
        baseline = json.loads((args.bundle / "config/development-machine.json").read_text("utf-8"))
        if baseline["machine_sha256"] == report["environment"]["machine_sha256"]:
            report["errors"].append("当前仍是开发机，不能作为另一台电脑的证据")
        sys.path.insert(0, str(SCRIPT.parent))
        from audit_load_cycles import gpu
        report["gpu"] = gpu()
        if args.role == "a4000" and "RTX A4000" not in report["gpu"]["name"]:
            report["errors"].append("未检测到要求的 RTX A4000")
        if args.role == "a4000" and (
            "W5-2455X" not in report["environment"]["cpu"].upper()
            or report["environment"]["ram_bytes"] < 60 * 1024**3
        ):
            report["errors"].append("目标 CPU/RAM 与 Xeon W5-2455X、64 GB 配置不符")
        if args.role == "clean-windows":
            if not args.confirm_no_development_environment:
                report["errors"].append("缺少操作者关于无开发环境的明确确认")
            commands = report["environment"]["development_commands"]
            found = {key: value for key, value in commands.items() if value
                     and "WindowsApps" not in value
                     and not Path(value).is_relative_to(args.bundle.resolve())}
            if found:
                report["errors"].append("发现开发命令路径，不能标记为干净机器：" + ", ".join(found))
        isolation = json.loads((args.os_isolation_evidence / "os-isolation-result.json").read_text("utf-8"))
        current_manifest = digest(args.bundle / "manifest.json")
        report["bundle_manifest_sha256"] = current_manifest
        if (not isolation.get("passed") or
            isolation.get("machine_sha256") != report["environment"]["machine_sha256"] or
            isolation.get("bundle_manifest_sha256") != current_manifest):
            report["errors"].append("系统禁网证据不通过，或不属于当前机器和应用文件版本")
        report["os_isolation"] = isolation
        if not report["errors"]:
            runtime = args.bundle / "runtimes/service/python.exe"
            scripts = ["audit_application.py"]
            if args.role == "a4000":
                scripts += ["audit_load_cycles.py", "audit_batch_stress.py"]
            for name in scripts:
                target = args.output / name.removesuffix(".py")
                started = time.monotonic()
                result = subprocess.run(
                    [str(runtime), "-B", "-X", "utf8", "-I", str(args.bundle / "tools" / name),
                     "--bundle", str(args.bundle), "--output", str(target)], timeout=3 * 3600)
                report["checks"].append({"script": name, "exit_code": result.returncode,
                                         "seconds": time.monotonic() - started})
                if result.returncode:
                    report["errors"].append(name + " 未通过")
                    break
            if args.role == "a4000" and not report["errors"]:
                cycles = json.loads((args.output / "audit_load_cycles/load-cycles.json").read_text("utf-8"))
                batch = json.loads((args.output / "audit_batch_stress/batch-stress.json").read_text("utf-8"))
                if (cycles.get("passed") is not True or len(cycles.get("cycles", [])) != 20
                    or batch.get("passed") is not True or batch.get("completed_tasks") != 500):
                    report["errors"].append("目标机缺少完整的 20 次循环或 500 张队列证据")
                else:
                    peaks = [cycle.get("sampled_peak_gpu_mib") for cycle in cycles["cycles"]]
                    peaks.append(batch.get("sampled_peak_gpu_mib"))
                    if any(type(peak) is not int for peak in peaks):
                        report["errors"].append("目标机显存采样缺失")
                    else:
                        peak = max(peaks)
                        headroom = report["gpu"]["total_mib"] - peak
                        report["gpu_headroom"] = {
                            "scope": "20 fixture cycles and 500 synthetic PP-OCR tasks only; intranet image workload must be measured separately",
                            "sampled_peak_device_mib": peak, "minimum_free_mib": headroom,
                            "target_free_mib": 2048, "passed": headroom >= 2048}
                        if headroom < 2048:
                            report["errors"].append("本次目标机测量未达到约 2 GB 显存余量目标")
            report["passed"] = not report["errors"]
    except Exception as error:
        report["errors"].append(str(error))
    write(args.output / "target-machine.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 2


def self_test(output: Path) -> None:
    if output.exists():
        raise ValueError("Self-test output must be a new directory")
    output.mkdir(parents=True)
    fixture = output / "synthetic-candidate"
    fixture.mkdir()
    (fixture / "payload.txt").write_text("合成验证\n", encoding="utf-8")
    write(fixture / "manifest.json", {"files": [{"path": "payload.txt", "bytes": (fixture / "payload.txt").stat().st_size,
                                                 "sha256": digest(fixture / "payload.txt")}]})
    result = verify_manifest(fixture)
    unchanged = verify_unchanged(fixture, result)
    original_stat = (fixture / "payload.txt").stat()
    original_size = original_stat.st_size
    tamper_rejected = False
    (fixture / "payload.txt").write_bytes(b"x" * original_size)
    os.utime(fixture / "payload.txt", ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    same_metadata_tamper_rejected = False
    try:
        verify_unchanged(fixture, result)
    except ValueError:
        same_metadata_tamper_rejected = True
    try:
        verify_manifest(fixture)
    except ValueError:
        tamper_rejected = True
    traversal_rejected = False
    try:
        contained_file(fixture, "../outside")
    except ValueError:
        traversal_rejected = True
    receipt = {"status": "pass" if tamper_rejected and same_metadata_tamper_rejected and traversal_rejected else "fail",
               "scope": "small synthetic manifest, unchanged check, same-size/mtime tamper and traversal rejection; no candidate runtime started",
               "manifest": {k: v for k, v in result.items() if k != "file_stats"},
               "unchanged": unchanged, "tamper_rejected": tamper_rejected,
               "same_metadata_tamper_rejected": same_metadata_tamper_rejected,
               "traversal_rejected": traversal_rejected, "clean_windows": "not_tested"}
    write(output / "self-test.json", receipt)
    print(json.dumps({"receipt": str(output / "self-test.json"), "status": receipt["status"]}, ensure_ascii=False))
    if receipt["status"] != "pass":
        raise SystemExit(1)


def agent_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, help="Explicit experimental candidate directory, read-only")
    parser.add_argument("--output", type=Path, required=True, help="NEW receipt directory outside the candidate")
    parser.add_argument("--self-test", action="store_true", help="Small synthetic checks; does not qualify a candidate")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.self_test:
        if args.bundle:
            parser.error("--self-test does not use --bundle")
        self_test(output)
        return
    if not args.bundle:
        parser.error("--bundle is required except with --self-test")
    bundle = args.bundle.resolve()
    if platform.system() != "Windows" or not bundle.is_dir() or args.bundle.is_symlink() or args.bundle.is_junction():
        parser.error("Expected an existing, ordinary Windows candidate directory")
    if output.exists() or output.is_relative_to(bundle) or bundle.is_relative_to(output):
        parser.error("--output must be new and disjoint from --bundle")
    bundled_script = bundle / "tools/target_machine_acceptance.py"
    if SCRIPT != bundled_script.resolve() or not bundled_script.is_file():
        parser.error("Run the target acceptance entry frozen inside --bundle")
    output.mkdir(parents=True)
    receipt = {"status": "running", "candidate": str(bundle), "output": str(output),
               "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "script_sha256": digest(SCRIPT), "machine": machine(),
               "path_observation": {"candidate": str(bundle), "isolated_output": str(output),
                                    "candidate_has_non_ascii": any(ord(ch) > 127 for ch in str(bundle)),
                                    "output_has_non_ascii": any(ord(ch) > 127 for ch in str(output)),
                                    "relocation_from_prior_path": "not_tested"},
               "checks": {}, "limitations": ["No clean-install attestation from a single local process.",
                                                 "No physical-disconnection or Windows firewall test.",
                                                 "No cross-user DPAPI test or prior-release executable rollback.",
                                                 "No real controller, OCR, GPU or long-duration stability run."]}
    try:
        verified = verify_manifest(bundle)
        receipt["checks"]["manifest"] = {key: value for key, value in verified.items() if key != "file_stats"}
        receipt["checks"]["layout"] = check_layout(bundle, set(verified["file_stats"]))
        env = isolated_environment(output)
        command = [bundle / "runtimes/service/python.exe", "-X", "utf8", "-B", "-I", bundled_script,
                   "--bundle", bundle, "--output", output, "--worker"]
        with (output / "candidate-runtime.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run([str(x) for x in command], cwd=output, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, timeout=180)
        receipt["checks"]["runtime_process"] = {"exit_code": completed.returncode,
                                                  "command": [str(x) for x in command],
                                                  "log": "candidate-runtime.log"}
        if completed.returncode != 0:
            raise RuntimeError("Candidate runtime failed; inspect candidate-runtime.log")
        runtime = json.loads((output / "candidate-runtime.json").read_text("utf-8"))
        if (runtime.get("status") != "pass" or
            runtime.get("default", {}).get("status") != "pass" or
            runtime.get("opt_in", {}).get("status") != "pass" or
            runtime.get("external_python_network_attempts") != []):
            raise ValueError("Candidate runtime receipt is incomplete or failed")
        receipt["checks"]["runtime"] = runtime
        receipt["checks"]["candidate_unchanged"] = verify_unchanged(bundle, verified)
        receipt["status"] = "observed_checks_passed_with_qualification_gaps"
    except Exception as error:
        receipt.update(status="fail", error_type=type(error).__name__, error=str(error))
        if "manifest" in receipt["checks"]:
            try:
                receipt["checks"]["candidate_unchanged"] = verify_unchanged(bundle, verified)
            except Exception as unchanged_error:
                receipt["checks"]["candidate_unchanged"] = {"status": "fail", "error": str(unchanged_error)}
    receipt["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write(output / "target-environment-results.json", receipt)
    write(output / "upgrade-rollback-results.json", {
        "status": "not_tested", "candidate": str(bundle),
        "actual_package_graph_interrupt_upgrade": "not_tested",
        "two_database_backup_restore": "not_tested", "checkpoint_project_delete": "not_tested",
        "old_release_executable_rollback": "not_tested", "fault_recovery": "not_tested",
        "reason": "This bounded target-machine entry tests fresh isolated startup. Run the separate frozen package regression audit for upgrade/rollback evidence; historical receipts do not transfer to a new candidate."
    })
    print(json.dumps({"status": receipt["status"], "receipt": str(output / "target-environment-results.json")}, ensure_ascii=False))
    if receipt["status"] == "fail":
        raise SystemExit(1)


def main() -> int | None:
    if any(arg == "--role" or arg.startswith("--role=") for arg in sys.argv[1:]):
        return legacy_main()
    agent_main()
    return None


if __name__ == "__main__":
    # Internal worker only runs after the parent validated the candidate manifest.
    if "--worker" in sys.argv:
        internal = argparse.ArgumentParser()
        internal.add_argument("--bundle", type=Path, required=True)
        internal.add_argument("--output", type=Path, required=True)
        internal.add_argument("--worker", action="store_true")
        worker_args = internal.parse_args()
        write(worker_args.output / "candidate-runtime.json",
              asyncio.run(worker(worker_args.bundle.resolve(), worker_args.output.resolve())))
    else:
        raise SystemExit(main())
