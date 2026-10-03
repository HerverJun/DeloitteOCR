"""Real bounded loopback children; no OCR/classification models or GPU are started."""

import ctypes
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from workbench_platform.catalog.registry import AppRegistry, DEFAULT_CONFIG
from workbench_platform.supervisor import LaunchConflict, LaunchFailure, NotOwned, Supervisor
from workbench_platform.supervisor.processes import (
    _OwnedJob, _listener_pid, process_creation_time, storage_environment,
)
from workbench_platform.supervisor import broker as gate


SERVER = '''
import http.server, json, os, pathlib, socket, subprocess, sys, threading, time
root = pathlib.Path.cwd()
mode = (root / 'mode.txt').read_text().strip()
(root / 'starts.txt').open('a').write(str(os.getpid()) + '\\n')
(root / 'pid.txt').write_text(str(os.getpid()))
if mode == 'capture_env':
    (root / 'environment.json').write_text(json.dumps({key: os.environ.get(key) for key in
        ('WORKBENCH_APP_ID', 'WORKBENCH_INSTANCE_ID', 'WORKBENCH_LAUNCH_ID',
         'WORKBENCH_GPU_LEDGER', 'ProgramData', 'TEMP', 'TMP', 'CLEAR_PADDLE_TEMP',
         'APPDATA', 'LOCALAPPDATA', 'PIP_CACHE_DIR', 'npm_config_cache',
         'npm_config_store_dir', 'PNPM_HOME', 'HF_HOME', 'TORCH_HOME',
         'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'PADDLE_PDX_CACHE_HOME',
         'PADDLEOCR_HOME', 'MPLCONFIGDIR', 'HYPOTHESIS_STORAGE_DIRECTORY',
         'JUPYTER_CONFIG_DIR', 'JUPYTER_DATA_DIR', 'PLAYWRIGHT_BROWSERS_PATH',
         'PYTHONDONTWRITEBYTECODE', 'PYTHONUTF8', 'PIP_DISABLE_PIP_VERSION_CHECK',
         'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
         'HOME', 'CODEX_HOME', 'UNRELATED_PARENT_MARKER')}))
    (root / 'home-readonly.txt').write_text(str(pathlib.Path.home()))
if mode == 'exit': sys.exit(4)
if mode == 'silent': time.sleep(30); sys.exit(0)
if mode == 'tree':
    child = subprocess.Popen([sys.executable, '-B', '-u', 'child.py'], cwd=root)
    (root / 'child-pid.txt').write_text(str(child.pid))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/api/platform/v1/identity':
            self.send_error(404); return
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'http://example.invalid/identity')
            self.end_headers()
            return
        nonce = os.environ['WORKBENCH_NONCE']
        if mode == 'wrong': nonce = 'otherNonce'
        body = json.dumps({'app_id': os.environ['WORKBENCH_APP_ID'],
                           'instance_id': os.environ['WORKBENCH_INSTANCE_ID'],
                           'nonce': nonce, 'version': 'fixture-1'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
port = int((root / 'port.txt').read_text()) if (root / 'port.txt').exists() else 0
server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
if mode == 'noisy':
    for n in range(100): print('untrusted log ' + str(n), flush=True)
if mode == 'bad_port': print('READY 70000', flush=True)
print('READY ' + str(server.server_address[1]), flush=True)
if mode in ('short', 'main_exits_with_child'):
    threading.Timer(1.0, lambda: os._exit(0)).start()
server.serve_forever()
'''

CHILD = "import time\nwhile True: time.sleep(1)\n"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def eventually(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def bundle(tmp_path, app_id="ocr", mode="normal", port=None):
    root = tmp_path / "bundles" / app_id
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(sys.executable, scripts / "python.exe")
    venv_config = Path(sys.executable).parent.parent / "pyvenv.cfg"
    runtime_names = ["Scripts/python.exe"]
    if venv_config.is_file():
        shutil.copy2(venv_config, root / "pyvenv.cfg")
        runtime_names.append("pyvenv.cfg")
    else:
        # D00 registers an embedded runtime rather than a venv redirector.
        runtime = Path(sys.executable).parent
        for entry in runtime.iterdir():
            if entry.is_file() and entry.suffix in {".dll", ".pyd", ".zip"}:
                shutil.copy2(entry, scripts / entry.name)
                runtime_names.append("Scripts/" + entry.name)
        (scripts / "python312._pth").write_text("python312.zip\n.\n", encoding="utf-8")
        runtime_names.append("Scripts/python312._pth")
    (root / "server.py").write_text(SERVER, encoding="utf-8")
    (root / "child.py").write_text(CHILD, encoding="utf-8")
    (root / "mode.txt").write_text(mode, encoding="utf-8")
    names = [*runtime_names, "server.py", "child.py", "mode.txt"]
    if port is not None:
        (root / "port.txt").write_text(str(port), encoding="utf-8")
        names.append("port.txt")
    declaration = {"app_id": app_id, "files": [{"path": name, "sha256": digest(root / name)} for name in names]}
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    return root


def register(config, root, app_id="ocr"):
    record = {"app_id": app_id, "installed": True,
              "manifest_sha256": digest(config / f"{app_id}.json"),
              "launch": {"bundle_root": str(root), "executable": str(root / "Scripts/python.exe"),
                         "working_directory": str(root), "allowed_arguments": ["-B", "-u", "server.py"],
                         "executable_sha256": digest(root / "Scripts/python.exe"),
                         "bundle_manifest_sha256": digest(root / "bundle.manifest.json")}}
    (config / f"{app_id}.install.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture
def trusted(tmp_path):
    config = tmp_path / "config" / "apps"
    config.mkdir(parents=True)
    for item in DEFAULT_CONFIG.glob("*.json"):
        shutil.copy2(item, config / item.name)
    root = bundle(tmp_path)
    register(config, root)
    registry = AppRegistry(config, install_root=tmp_path / "bundles")
    assert registry.installations["ocr"].installed
    return tmp_path, config, root, registry


def make_supervisor(registry, **kwargs):
    data_root = kwargs.pop("data_root", None)
    if data_root is None:
        data_root = registry.config_dir.parent.parent / ("platform-data-" + uuid.uuid4().hex)
    data_root.mkdir(parents=True, exist_ok=True)
    return Supervisor(registry, data_root=data_root, **kwargs)


class ExternalService:
    def __init__(self, job, broker, pid):
        self.job, self.broker, self.pid = job, broker, pid

    def close(self):
        self.job.terminate()
        self.job.close()
        self.broker.wait(timeout=5)
        assert eventually(lambda: not alive(self.pid))


def external(tmp_path, *, app_id="ocr", instance_id="externalInstance", nonce="externalNonce"):
    root = tmp_path / "external"
    root.mkdir()
    (root / "server.py").write_text(SERVER, encoding="utf-8")
    (root / "mode.txt").write_text("normal", encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    environment.update(storage_environment(root / "data"))
    environment.update({"PATH": str(Path(sys.executable).parent), "WORKBENCH_APP_ID": app_id,
                        "WORKBENCH_INSTANCE_ID": instance_id, "WORKBENCH_NONCE": nonce,
                        "PYTHONDONTWRITEBYTECODE": "1"})
    job = _OwnedJob()  # Test-owned, entirely separate from Supervisor's Job.
    broker = subprocess.Popen([sys.executable, "-I", "-B", "-S", "-u", str(Path(gate.__file__))],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", creationflags=0x08000000,
                              env=environment, cwd=root)
    try:
        job.assign(broker)  # The broker cannot spawn the external service until this succeeds.
        broker.stdin.write(json.dumps({"argv": [sys.executable, "-B", "-u", "server.py"],
                                       "cwd": str(root), "environment": environment}) + "\n")
        broker.stdin.close()
        messages = queue.Queue()
        def read_lines():
            for _ in range(2):
                messages.put(broker.stdout.readline())
        threading.Thread(target=read_lines, daemon=True).start()
        assert messages.get(timeout=3).startswith("CHILD ")
        line = messages.get(timeout=3).strip()
        assert line.startswith("OUTPUT READY ")
        port = int(line[len("OUTPUT READY "):])
        pid = _listener_pid(port)
        assert pid == int((root / "pid.txt").read_text()) and job.contains(pid)
        return ExternalService(job, broker, pid), port
    except BaseException:
        job.terminate()
        job.close()
        broker.wait(timeout=5)
        raise


def test_dynamic_bound_port_identity_concurrent_idempotency_and_tree_cleanup(trusted):
    _, _, root, registry = trusted
    # The trusted declaration is updated before registry reload; no HTTP input
    # selects the command or port.
    (root / "mode.txt").write_text("tree", encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text())
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    config = registry.config_dir
    register(config, root)
    registry.reload()
    with make_supervisor(registry) as supervisor:
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: supervisor.start("ocr", "launchA"), range(4)))
        assert all(receipt == receipts[0] for receipt in receipts)
        receipt = receipts[0]
        assert receipt.ownership == "owned" and receipt.version == "fixture-1"
        assert receipt.base_url.startswith("http://127.0.0.1:") and not receipt.base_url.endswith(":0/")
        assert receipt.pid == int((root / "pid.txt").read_text())
        assert receipt.creation_time == process_creation_time(receipt.pid)
        assert (root / "starts.txt").read_text().splitlines() == [str(receipt.pid)]
        assert registry.health("ocr").service.state == "ready"
        assert eventually(lambda: (root / "child-pid.txt").exists())
        child_pid = int((root / "child-pid.txt").read_text())
        assert alive(child_pid)
        with pytest.raises(LaunchConflict):
            supervisor.start("classification", "launchA")
        supervisor.stop("launchA")
        assert eventually(lambda: not alive(receipt.pid) and not alive(child_pid))
        assert registry.health("ocr").service.state == "unavailable"


def test_managed_launch_passes_shared_gpu_ledger_and_local_lock_directory(trusted, monkeypatch):
    _, config, root, registry = trusted
    monkeypatch.setenv('UNRELATED_PARENT_MARKER', 'must-not-reach-native')
    (root / "mode.txt").write_text("capture_env", encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text(encoding="utf-8"))
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    register(config, root)
    registry.reload()
    data_root = root.parent.parent / "platform-data"
    data_root.mkdir()
    with make_supervisor(registry, data_root=data_root) as supervisor:
        receipt = supervisor.start("ocr", "gpuContextA")
        actual = json.loads((root / "environment.json").read_text(encoding="utf-8"))
        expected = {**storage_environment(data_root),
            "WORKBENCH_APP_ID": "ocr",
            "WORKBENCH_INSTANCE_ID": receipt.instance_id,
            "WORKBENCH_LAUNCH_ID": receipt.launch_id,
            "WORKBENCH_GPU_LEDGER": str(data_root / "gpu.sqlite3"),
            "LOCALAPPDATA": str(data_root / "native-local"),
        }
        expected["ProgramData"] = os.environ.get("ProgramData")
        expected.update({key: os.environ.get(key) for key in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH")})
        expected.update({"HOME": None, "CODEX_HOME": None, "UNRELATED_PARENT_MARKER": None})
        assert actual == expected
        assert (root / 'home-readonly.txt').read_text() == str(Path.home())
        for key, value in actual.items():
            if value is not None and (key in {"TEMP", "TMP", "CLEAR_PADDLE_TEMP", "APPDATA", "LOCALAPPDATA"}
                    or key.endswith(("_DIR", "_DIRECTORY", "_HOME", "_PATH"))
                    or key in {"PNPM_HOME", "npm_config_cache", "npm_config_store_dir"}):
                assert Path(value).resolve().drive.upper() == "F:", key
        assert (data_root / "native-local").is_dir()


@pytest.mark.parametrize("mode", ["wrong", "exit", "silent", "redirect", "bad_port"])
def test_failed_handshakes_are_cached_and_only_own_child_is_cleaned(trusted, mode):
    _, config, root, registry = trusted
    (root / "mode.txt").write_text(mode, encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text())
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    register(config, root)
    registry.reload()
    with make_supervisor(registry, handshake_timeout=0.7) as supervisor:
        def attempt(_):
            with pytest.raises(LaunchFailure):
                supervisor.start("ocr", "failedA")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(attempt, range(4)))
    assert (root / "starts.txt").read_text().count("\n") == 1
    pid = int((root / "pid.txt").read_text())
    assert eventually(lambda: not alive(pid))
    assert registry.health("ocr").service.state != "ready"


def test_occupied_port_does_not_capture_external_sentinel(trusted):
    tmp_path, config, root, registry = trusted
    sentinel, port = external(tmp_path)
    try:
        (root / "mode.txt").write_text("normal", encoding="utf-8")
        (root / "port.txt").write_text(str(port), encoding="utf-8")
        declaration = json.loads((root / "bundle.manifest.json").read_text())
        declaration["files"].append({"path": "port.txt", "sha256": digest(root / "port.txt")})
        (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
        register(config, root)
        registry.reload()
        with make_supervisor(registry, handshake_timeout=0.8) as supervisor:
            with pytest.raises(LaunchFailure):
                supervisor.start("ocr", "occupiedA")
        assert alive(sentinel.pid)
        assert eventually(lambda: not alive(int((root / "pid.txt").read_text())))
    finally:
        sentinel.close()


def test_discovered_identity_is_query_only_and_never_kills_external_service(trusted):
    tmp_path, _, _, registry = trusted
    sentinel, port = external(tmp_path)
    try:
        with make_supervisor(registry) as supervisor:
            service_pid = sentinel.pid
            creation = process_creation_time(service_pid)
            with pytest.raises(LaunchFailure):
                supervisor.discover("ocr", "discoveryBad", pid=service_pid, creation_time=creation,
                                    base_url=f"http://127.0.0.1:{port}/", instance_id="externalInstance", nonce="wrong")
            with pytest.raises(LaunchFailure, match="does not own"):
                supervisor.discover("ocr", "discoveryWrongPid", pid=sentinel.broker.pid,
                                    creation_time=process_creation_time(sentinel.broker.pid),
                                    base_url=f"http://127.0.0.1:{port}/", instance_id="externalInstance",
                                    nonce="externalNonce")
            receipt = supervisor.discover("ocr", "discoveryA", pid=service_pid, creation_time=creation,
                                          base_url=f"http://127.0.0.1:{port}/", instance_id="externalInstance",
                                          nonce="externalNonce")
            assert receipt.ownership == "discovered" and receipt.pid == service_pid
            registry.reload()
            assert receipt == supervisor.discover("ocr", "discoveryA", pid=service_pid, creation_time=creation,
                                                  base_url=f"http://127.0.0.1:{port}/", instance_id="externalInstance",
                                                  nonce="externalNonce")
            with pytest.raises(NotOwned):
                supervisor.stop("discoveryA")
            with pytest.raises(LaunchConflict):
                supervisor.discover("ocr", "discoveryA", pid=service_pid, creation_time=creation,
                                    base_url=f"http://127.0.0.1:{port}/", instance_id="otherInstance",
                                    nonce="externalNonce")
        assert alive(sentinel.pid)
    finally:
        sentinel.close()


def test_discovered_close_stops_monitoring_without_claiming_service_exited(trusted):
    tmp_path, _, _, registry = trusted
    sentinel, port = external(tmp_path)
    try:
        with make_supervisor(registry) as supervisor:
            supervisor.discover("ocr", "discoveryClose", pid=sentinel.pid,
                                creation_time=process_creation_time(sentinel.pid),
                                base_url=f"http://127.0.0.1:{port}/", instance_id="externalInstance",
                                nonce="externalNonce")
            assert registry.health("ocr").service.state == "ready"
        assert alive(sentinel.pid)
        assert registry.health("ocr").service.state == "unknown"
        assert registry.health("ocr").service.reason == "Supervisor stopped monitoring"
    finally:
        sentinel.close()


def test_bundle_payload_tamper_rejected_before_business_spawn(trusted):
    _, _, root, registry = trusted
    (root / "server.py").write_text(SERVER + "\n# changed", encoding="utf-8")
    with make_supervisor(registry) as supervisor:
        with pytest.raises(LaunchFailure, match="integrity"):
            supervisor.start("ocr", "tamperedA")
    assert not (root / "starts.txt").exists()


def test_trusted_manifest_change_after_reload_is_rejected(trusted):
    _, config, root, registry = trusted
    manifest = json.loads((config / "ocr.json").read_text())
    manifest["display_name"] = "Changed after catalog load"
    (config / "ocr.json").write_text(json.dumps(manifest), encoding="utf-8")
    record = json.loads((config / "ocr.install.json").read_text())
    record["manifest_sha256"] = digest(config / "ocr.json")
    (config / "ocr.install.json").write_text(json.dumps(record), encoding="utf-8")
    with make_supervisor(registry) as supervisor:
        with pytest.raises(LaunchFailure, match="declaration changed"):
            supervisor.start("ocr", "manifestChanged")
    assert not (root / "starts.txt").exists()


def test_noisy_child_cannot_evict_critical_ready_protocol(trusted):
    _, config, root, registry = trusted
    (root / "mode.txt").write_text("noisy", encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text())
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    register(config, root)
    registry.reload()
    with make_supervisor(registry) as supervisor:
        receipt = supervisor.start("ocr", "noisyLaunch")
        assert receipt.ownership == "owned"


def test_exited_service_changes_health_and_old_stop_cannot_hide_new_instance(trusted):
    _, config, root, registry = trusted
    with make_supervisor(registry) as supervisor:
        old = supervisor.start("ocr", "oldLaunch")
        newer = supervisor.start("ocr", "newLaunch")
        assert newer.pid != old.pid and registry.health("ocr").service.state == "ready"
        supervisor.stop("oldLaunch")
        assert registry.health("ocr").service.state == "ready"
        supervisor.stop("newLaunch")
        assert registry.health("ocr").service.state == "unavailable"
    (root / "mode.txt").write_text("short", encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text())
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    register(config, root)
    registry.reload()
    with make_supervisor(registry) as supervisor:
        receipt = supervisor.start("ocr", "shortLaunch")
        assert registry.health("ocr").service.state == "ready"
        assert eventually(lambda: not alive(receipt.pid) and registry.health("ocr").service.state == "unavailable", 4)


def test_main_exit_with_stdout_held_by_descendant_still_reaps_owned_tree(trusted):
    _, config, root, registry = trusted
    (root / "mode.txt").write_text("main_exits_with_child", encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text())
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    # This mode spawns a descendant with inherited stdout before the main exits.
    server = (root / "server.py").read_text().replace("if mode == 'tree':", "if mode in ('tree', 'main_exits_with_child'):")
    (root / "server.py").write_text(server, encoding="utf-8")
    next(item for item in declaration["files"] if item["path"] == "server.py")["sha256"] = digest(root / "server.py")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    register(config, root)
    registry.reload()
    with make_supervisor(registry) as supervisor:
        receipt = supervisor.start("ocr", "exitsWithChild")
        child_pid = int((root / "child-pid.txt").read_text())
        assert alive(child_pid)
        assert eventually(lambda: not alive(receipt.pid) and not alive(child_pid), 4)
        assert registry.health("ocr").service.state == "unavailable"


def test_same_launch_id_retry_survives_catalog_reload(trusted):
    _, _, _, registry = trusted
    with make_supervisor(registry) as supervisor:
        receipt = supervisor.start("ocr", "stableLaunch")
        registry.reload()
        assert supervisor.start("ocr", "stableLaunch") == receipt
        supervisor.stop("stableLaunch")


def test_discovered_exit_becomes_unavailable_without_supervisor_kill(trusted):
    tmp_path, _, _, registry = trusted
    sentinel, port = external(tmp_path)
    try:
        with make_supervisor(registry) as supervisor:
            supervisor.discover("ocr", "discoveryExit", pid=sentinel.pid,
                                creation_time=process_creation_time(sentinel.pid),
                                base_url=f"http://127.0.0.1:{port}/", instance_id="externalInstance",
                                nonce="externalNonce")
            assert registry.health("ocr").service.state == "ready"
            sentinel.close()  # The test owner stops it; Supervisor has no Job handle for it.
            assert eventually(lambda: registry.health("ocr").service.state == "unavailable")
    finally:
        if sentinel.job.handle:
            sentinel.close()


def test_stop_and_close_wait_for_pending_failure_without_deadlock(trusted):
    _, config, root, registry = trusted
    (root / "mode.txt").write_text("silent", encoding="utf-8")
    declaration = json.loads((root / "bundle.manifest.json").read_text())
    next(item for item in declaration["files"] if item["path"] == "mode.txt")["sha256"] = digest(root / "mode.txt")
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    register(config, root)
    registry.reload()
    supervisor = make_supervisor(registry, handshake_timeout=0.7)
    with ThreadPoolExecutor(max_workers=3) as pool:
        starting = pool.submit(supervisor.start, "ocr", "pendingLaunch")
        assert eventually(lambda: (root / "pid.txt").exists())
        stopping = pool.submit(supervisor.stop, "pendingLaunch")
        closing = pool.submit(supervisor.close)
        with pytest.raises(LaunchFailure):
            starting.result(timeout=5)
        with pytest.raises(LaunchFailure):
            stopping.result(timeout=5)
        closing.result(timeout=5)
    assert eventually(lambda: not alive(int((root / "pid.txt").read_text())))
    with pytest.raises(LaunchFailure, match="closed"):
        supervisor.start("ocr", "lateLaunch")
