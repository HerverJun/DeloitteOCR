"""Candidate install records follow the current directory after relocation."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from scripts.candidate_bootstrap import configure
from workbench_platform.catalog import routes


def small_candidate(tmp_path):
    root = tmp_path / "candidate"
    config = root / "app/config/apps"
    config.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "config/apps"
    sealed = {}
    for app_id in ("ocr", "classification"):
        shutil.copy2(source / f"{app_id}.json", config / f"{app_id}.json")
        bundle = root / "bundles" / app_id
        bundle.mkdir(parents=True)
        launcher = b"@echo off\r\n"
        declaration = json.dumps({"app_id": app_id, "files": [
            {"path": "launch.cmd", "sha256": digest(launcher)}]}).encode()
        for name, content in (("launch.cmd", launcher), ("bundle.manifest.json", declaration),
                              ("runtime.bin", f"runtime for {app_id}".encode())):
            (bundle / name).write_bytes(content)
            sealed[f"bundles/{app_id}/{name}"] = {"sha256": digest(content), "bytes": len(content)}
    (root / "candidate-manifest.json").write_text(json.dumps({"format": 3, "files": sealed}), encoding="utf-8")
    return root, sealed


def restore_environment(monkeypatch):
    monkeypatch.setattr(routes, "_registry", routes._registry)
    for name in ("WORKBENCH_DATA_DIR", "WORKBENCH_DATA_PYTHON", "WORKBENCH_PORTAL_DIST",
                 "WORKBENCH_OCR_TOKEN_FILE", "WORKBENCH_INSTALL_DATA_DIR", "PYTHONUTF8"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("WORKBENCH_FORCE_VERIFY", "")
    monkeypatch.setenv("WORKBENCH_PORT", "8765")
    for name in ("TEMP", "TMP", "CLEAR_PADDLE_TEMP", "APPDATA", "LOCALAPPDATA",
                 "PIP_CACHE_DIR", "npm_config_cache", "npm_config_store_dir", "PNPM_HOME",
                 "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                 "PADDLE_PDX_CACHE_HOME", "PADDLEOCR_HOME", "MPLCONFIGDIR",
                 "HYPOTHESIS_STORAGE_DIRECTORY", "JUPYTER_CONFIG_DIR", "JUPYTER_DATA_DIR",
                 "PLAYWRIGHT_BROWSERS_PATH", "PYTHONDONTWRITEBYTECODE",
                 "PIP_DISABLE_PIP_VERSION_CHECK", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.delenv(name, raising=False)


def wait_for_verification(registry):
    deadline = time.monotonic() + 10
    while registry._snapshot.verifying and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not registry._snapshot.verifying, "background verification did not finish"


@pytest.mark.parametrize("corrupt_runtime", [False, True])
def test_portal_and_data_remain_available_while_every_payload_is_checked(tmp_path, monkeypatch, corrupt_runtime):
    from scripts import candidate_bootstrap as bootstrap
    from workbench_platform.app import app

    restore_environment(monkeypatch)
    root, sealed = small_candidate(tmp_path)
    old = configure(root)
    assert all(item.installed for item in old.installations.values())
    target = root / "bundles/classification/runtime.bin"
    if corrupt_runtime:
        previous = target.stat()
        target.write_bytes(b"x" * previous.st_size)
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    entered, release = threading.Event(), threading.Event()
    observed = set()
    original_hash = bootstrap._hash

    def delayed_hash(path):
        observed.add(path.relative_to(root).as_posix())
        if path == target:
            entered.set()
            assert release.wait(20), "test did not release the final payload check"
        return original_hash(path)

    monkeypatch.setattr(bootstrap, "_hash", delayed_hash)
    registry = configure(root, background=True, force_verify=True)
    try:
        assert entered.wait(10)
        revision = registry.snapshot_revision
        assert "bundles/ocr/runtime.bin" in observed
        assert not any(item.installed for item in registry.installations.values())
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            assert client.get("/api/platform/v1/health").json()["service"] == "ready"
            session = client.post("/api/platform/v1/data/sessions", json={"name": "校验期间可继续作业"},
                                  headers={"Origin": "http://127.0.0.1:8765"})
            assert session.status_code == 201
            for app_id in ("ocr", "classification"):
                status = client.get(f"/api/platform/v1/apps/{app_id}/installation").json()
                assert (status["state"], status["installed"], status["launch_ready"]) == ("verifying", False, False)
                feedback = status["verification"]
                assert feedback["total_files"] == 3
                assert feedback["checked_files"] <= feedback["total_files"]
                assert feedback["phase"] == ("passed" if app_id == "ocr" else "checking")
                assert feedback["elapsed_seconds"] >= 0
                launch = client.post(f"/api/platform/v1/apps/{app_id}/launches/lchecking{app_id}",
                                     headers={"Origin": "http://127.0.0.1:8765"})
                assert launch.status_code == 503
            assert not app.state.supervisor._entries
            assert registry.snapshot_revision == revision
            release.set()
            wait_for_verification(registry)
            assert registry.snapshot_revision == revision + 1
            assert set(sealed).issubset(observed)
            assert client.get("/api/platform/v1/apps/ocr/installation").json()["launch_ready"] is True
            classification = client.get("/api/platform/v1/apps/classification/installation").json()
            assert classification["launch_ready"] is (not corrupt_runtime)
            if corrupt_runtime:
                assert "校验失败" in registry.health("classification").service.reason
                assert classification["verification"]["phase"] == "failed"
    finally:
        release.set()
        wait_for_verification(registry)


def test_background_publication_error_is_visible_and_never_allows_launch(tmp_path, monkeypatch):
    from scripts import candidate_bootstrap as bootstrap

    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    original_write = bootstrap._write_json

    def fail_publication(path, value):
        if value.get("installed"):
            raise OSError("cannot persist trusted result")
        original_write(path, value)

    monkeypatch.setattr(bootstrap, "_write_json", fail_publication)
    registry = configure(root, background=True)
    wait_for_verification(registry)
    assert not any(item.installed for item in registry.installations.values())
    assert "未能完成" in registry.health("ocr").service.reason
    assert not (root / "data/startup-verification.json").exists()


def test_repeated_start_reuses_success_across_data_directories_without_payload_scan(tmp_path, monkeypatch):
    from scripts import candidate_bootstrap as bootstrap

    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    first = configure(root)
    assert all(item.installed for item in first.installations.values())
    observed = []
    original = bootstrap._hash

    def traced(path):
        observed.append(path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix())
        return original(path)

    monkeypatch.setattr(bootstrap, "_hash", traced)
    monkeypatch.setenv("WORKBENCH_INSTALL_DATA_DIR", str(tmp_path / "another-data"))
    second = configure(root, background=True)
    assert not second._snapshot.verifying
    assert all(item.installed for item in second.installations.values())
    assert all(second.verification(app_id).phase == "cached" for app_id in ("ocr", "classification"))
    assert not any(name.endswith("runtime.bin") for name in observed)
    assert second.config_dir == tmp_path / "another-data/config/apps"


@pytest.mark.parametrize("invalid", ["missing", "malformed", "policy", "candidate", "relocated", "incomplete", "timestamp"])
def test_invalid_or_stale_cache_requires_full_payload_check(tmp_path, monkeypatch, invalid):
    from scripts import candidate_bootstrap as bootstrap

    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    configure(root)
    cache = root / "data/startup-verification.json"
    if invalid == "missing":
        cache.unlink()
    elif invalid == "malformed":
        cache.write_text('{"unfinished":', encoding="utf-8")
    elif invalid == "policy":
        value = json.loads(cache.read_text(encoding="utf-8"))
        value["policy"] += 1
        cache.write_text(json.dumps(value), encoding="utf-8")
    elif invalid == "candidate":
        manifest = root / "candidate-manifest.json"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif invalid in {"incomplete", "timestamp"}:
        value = json.loads(cache.read_text(encoding="utf-8"))
        for success in value["apps"].values():
            if invalid == "incomplete":
                success["checked_files"] -= 1
            else:
                success["verified_at"] = "invalid timestamp"
        cache.write_text(json.dumps(value), encoding="utf-8")
    else:
        moved = tmp_path / "relocated"
        shutil.copytree(root, moved)
        root = moved
    observed = []
    original = bootstrap._hash

    def traced(path):
        observed.append(path.relative_to(root).as_posix())
        return original(path)

    monkeypatch.setattr(bootstrap, "_hash", traced)
    result = configure(root)
    assert all(item.installed for item in result.installations.values())
    assert {"bundles/ocr/runtime.bin", "bundles/classification/runtime.bin"}.issubset(observed)
    assert all(result.verification(app_id).phase == "passed" for app_id in ("ocr", "classification"))


def test_failed_full_check_never_saves_success_and_only_failed_app_retries(tmp_path, monkeypatch):
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    target = root / "bundles/classification/runtime.bin"
    content = target.read_bytes()
    target.write_bytes(b"corrupt")
    first = configure(root)
    assert not first.installations["classification"].installed
    cache = root / "data/startup-verification.json"
    assert set(json.loads(cache.read_text(encoding="utf-8"))["apps"]) == {"ocr"}
    target.write_bytes(content)
    second = configure(root)
    assert second.installations["classification"].installed
    assert second.verification("ocr").phase == "cached"
    assert second.verification("classification").phase == "passed"


def test_forced_check_detects_payload_mutation_and_invalidates_previous_success(tmp_path, monkeypatch):
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    configure(root)
    target = root / "bundles/classification/runtime.bin"
    before = target.stat()
    target.write_bytes(b"x" * before.st_size)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    # Normal reuse checks entry points, not the content of all model/runtime files.
    reused = configure(root)
    assert reused.verification("classification").phase == "cached"
    forced = configure(root, force_verify=True)
    assert not forced.installations["classification"].installed
    cache = json.loads((root / "data/startup-verification.json").read_text(encoding="utf-8"))
    assert "classification" not in cache["apps"]
    retried = configure(root)
    assert not retried.installations["classification"].installed
    assert retried.verification("classification").phase == "failed"


def test_native_launch_reuses_completed_startup_and_keeps_entry_and_generation_guards(tmp_path, monkeypatch):
    from workbench_platform.supervisor import processes

    restore_environment(monkeypatch)
    root, sealed = small_candidate(tmp_path)
    declaration_path = root / "bundles/ocr/bundle.manifest.json"
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    declaration["files"].append({"path": "runtime.bin", "sha256": sealed["bundles/ocr/runtime.bin"]["sha256"]})
    content = json.dumps(declaration).encode()
    declaration_path.write_bytes(content)
    sealed["bundles/ocr/bundle.manifest.json"] = {"sha256": digest(content), "bytes": len(content)}
    (root / "candidate-manifest.json").write_text(json.dumps({"format": 3, "files": sealed}), encoding="utf-8")
    registry = configure(root)
    install = registry.installations["ocr"]
    observed = []
    original = processes._file_sha256

    def traced(path):
        observed.append(path.name)
        return original(path)

    monkeypatch.setattr(processes, "_file_sha256", traced)
    processes._verify_bundle(registry, registry._snapshot, "ocr", install)
    assert "runtime.bin" not in observed
    assert "launch.cmd" in observed
    (root / "bundles/ocr/runtime.bin").write_bytes(b"changed payload")
    processes._verify_bundle(registry, registry._snapshot, "ocr", install)
    # Reload/reinstallation revokes the completed-check proof for this generation.
    registry.reload()
    with pytest.raises(processes.LaunchFailure, match="payload integrity"):
        processes._verify_bundle(registry, registry._snapshot, "ocr", registry.installations["ocr"])
    # A current startup proof never permits a changed launcher.
    registry = configure(root)
    (root / "bundles/ocr/launch.cmd").write_bytes(b"changed entry")
    with pytest.raises(processes.LaunchFailure, match="entry point"):
        processes._verify_bundle(registry, registry._snapshot, "ocr", registry.installations["ocr"])


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_invalid_port_and_occupied_port_do_not_publish_configuration(tmp_path, monkeypatch):
    from scripts.candidate_bootstrap import candidate_instance
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    configure(root)
    pointer = root / "data/install-data.json"
    before = pointer.read_bytes()
    monkeypatch.setenv("WORKBENCH_INSTALL_DATA_DIR", str(tmp_path / "other-data"))
    monkeypatch.setenv("WORKBENCH_PORT", "70000")
    with pytest.raises(ValueError, match="between"):
        configure(root)
    assert pointer.read_bytes() == before
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setenv("WORKBENCH_PORT", str(occupied.getsockname()[1]))
        with pytest.raises(OSError):
            with candidate_instance(root):
                pytest.fail("occupied port must stop before configuration")
    assert pointer.read_bytes() == before
    assert not (tmp_path / "other-data").exists()


def test_candidate_os_lease_rejects_concurrent_process_and_preserves_pointer(tmp_path, monkeypatch):
    from scripts.candidate_bootstrap import CandidateBusy
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    configure(root)
    before = (root / "data/install-data.json").read_bytes()
    source = Path(__file__).resolve().parents[2]
    child_code = ("import sys;sys.path[:0]=[sys.argv[1],sys.argv[1]+'/src'];"
                  "from scripts.candidate_bootstrap import _FileLease;"
                  "lease=_FileLease(__import__('pathlib').Path(sys.argv[2]));"
                  "assert lease.acquire();print('LOCKED',flush=True);sys.stdin.readline();lease.close()")
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", child_code, str(source),
                              str(root / "data/candidate-instance.lock")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=os.environ.copy(), cwd=tmp_path)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(CandidateBusy):
            configure(root)
        assert (root / "data/install-data.json").read_bytes() == before
    finally:
        child.communicate("close\n", timeout=10)
        assert child.returncode == 0


def test_active_same_package_other_data_or_port_is_rejected(tmp_path, monkeypatch):
    from scripts.candidate_bootstrap import CandidateBusy, candidate_instance, _write_json
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    configure(root)
    before = (root / "data/install-data.json").read_bytes()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKBENCH_PORT", str(port))
    with candidate_instance(root) as active:
        _write_json(root / "data/candidate-instance.json", active["record"])
        for name, value in (("WORKBENCH_INSTALL_DATA_DIR", str(tmp_path / "other")),
                            ("WORKBENCH_PORT", str(port + 1 if port < 65535 else port - 1))):
            with monkeypatch.context() as changed:
                changed.setenv(name, value)
                with pytest.raises(CandidateBusy, match="another data directory or port"):
                    with candidate_instance(root):
                        pytest.fail("must reject different active instance arguments")
        with pytest.raises(CandidateBusy):
            configure(root)
    assert (root / "data/install-data.json").read_bytes() == before


def test_duplicate_candidate_reuses_only_verified_owned_page(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from scripts.candidate_bootstrap import candidate_instance, _write_json, CandidateBusy
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKBENCH_PORT", str(port))
    with candidate_instance(root) as active:
        record = active["record"]
        _write_json(root / "data/candidate-instance.json", record)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(record).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = active["listener"]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with candidate_instance(root) as duplicate:
                assert duplicate == {"reused_url": f"http://127.0.0.1:{port}/"}
            changed = {**record, "nonce": "wrong"}
            _write_json(root / "data/candidate-instance.json", changed)
            with pytest.raises(CandidateBusy, match="identity"):
                with candidate_instance(root):
                    pytest.fail("an unverified page must not be reused")
        finally:
            server.shutdown()
            thread.join(5)
            assert not thread.is_alive()


def test_bootstrap_main_identity_precedes_actual_portal_catchall(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import importlib.util
    import uvicorn
    from scripts import candidate_bootstrap as bootstrap
    import workbench_platform.app as application

    restore_environment(monkeypatch)
    portal = tmp_path / 'portal'
    portal.mkdir()
    (portal / 'index.html').write_text('<html>candidate portal</html>', encoding='utf-8')
    monkeypatch.setenv('WORKBENCH_PORTAL_DIST', str(portal))
    spec = importlib.util.spec_from_file_location('workbench_platform._candidate_route_test', application.__file__)
    composed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(composed)
    monkeypatch.setattr(application, 'app', composed.app)
    record = {'program_root': str(tmp_path), 'data_root': str(tmp_path / 'data'),
              'port': 8765, 'pid': os.getpid(), 'creation_time': 'fixed-process-time', 'nonce': 'fixed-nonce'}
    @contextmanager
    def instance(_root):
        yield {'record': record, 'anchor': tmp_path, 'listener': object()}
    monkeypatch.setattr(bootstrap, 'candidate_instance', instance)
    monkeypatch.setattr(bootstrap, 'configure', lambda *args, **kwargs: None)
    observed = []
    class ObserveServer:
        def __init__(self, config):
            self.app = config.app
        def run(self, **kwargs):
            # Routing is the real composition; no native or ASGI lifespan starts.
            client = TestClient(self.app)
            response = client.get('/api/platform/v1/candidate-identity')
            assert response.status_code == 200 and response.json() == record
            assert client.get('/api/platform/v1/unregistered').status_code == 404
            assert 'candidate portal' in client.get('/batches').text
            observed.append(response.json())
    monkeypatch.setattr(uvicorn, 'Server', ObserveServer)
    bootstrap.main()
    assert observed == [record]
    assert json.loads((tmp_path / 'candidate-instance.json').read_text('utf-8')) == record


def test_chinese_space_paths_and_real_child_environment_have_only_f_write_roots(tmp_path, monkeypatch):
    from workbench_platform.supervisor.processes import storage_environment
    restore_environment(monkeypatch)
    root = tmp_path / "中文 空格" / "data"
    monkeypatch.delenv("CLEAR_PADDLE_TEMP", raising=False)
    before = {name: os.environ.get(name) for name in ("HOME", "CODEX_HOME", "USERPROFILE")}
    env = {**os.environ, **storage_environment(root)}
    result = subprocess.run([sys.executable, "-I", "-B", "-c",
        "import json,os,tempfile;print(json.dumps({'env':dict(os.environ),'temp':tempfile.gettempdir()}))"],
        cwd=root, env=env, capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    assert Path(actual["temp"]).drive.upper() == "F:"
    assert str(actual["temp"]).isascii()
    for name, value in storage_environment(root).items():
        assert actual["env"][name.upper()] == value
        if name.endswith("HOME") or name in {"TEMP", "TMP", "APPDATA", "LOCALAPPDATA"}:
            assert Path(value).resolve().drive.upper() == "F:"
    assert {name: actual["env"].get(name) for name in before} == before


def test_outside_drive_and_junction_escape_block_before_pointer_write(tmp_path, monkeypatch):
    from scripts.candidate_bootstrap import configure
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    configure(root)
    before = (root / "data/install-data.json").read_bytes()
    monkeypatch.setenv("WORKBENCH_INSTALL_DATA_DIR", "C:/forbidden-project-data")
    with pytest.raises(ValueError, match="local F"):
        configure(root)
    junction = tmp_path / "escape"
    command = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), os.environ["SystemRoot"]],
                             cwd=tmp_path, env=os.environ.copy(), capture_output=True)
    assert command.returncode == 0, command.stderr
    try:
        monkeypatch.setenv("WORKBENCH_INSTALL_DATA_DIR", str(junction / "never-write"))
        with pytest.raises(ValueError, match="local F"):
            configure(root)
    finally:
        assert junction.is_junction()
        os.rmdir(junction)
    assert (root / "data/install-data.json").read_bytes() == before


def test_unwritable_registered_temp_does_not_fallback(tmp_path, monkeypatch):
    restore_environment(monkeypatch)
    root, _ = small_candidate(tmp_path)
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"keep")
    monkeypatch.setenv("CLEAR_PADDLE_TEMP", str(blocked))
    with pytest.raises(OSError):
        configure(root)
    assert blocked.read_bytes() == b"keep"
    assert not (root / "data/install-data.json").exists()


def test_optional_app_registration_is_local_and_missing_app_stays_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_registry", routes._registry)
    for name in ("WORKBENCH_DATA_DIR", "WORKBENCH_DATA_PYTHON",
                 "WORKBENCH_PORTAL_DIST", "WORKBENCH_OCR_TOKEN_FILE",
                 "WORKBENCH_INSTALL_DATA_DIR", "PYTHONUTF8"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    source = Path(__file__).resolve().parents[2] / "config" / "apps"
    candidate = tmp_path / "candidate"
    config = candidate / "app" / "config" / "apps"
    config.mkdir(parents=True)
    for app_id in ("ocr", "classification"):
        shutil.copy2(source / f"{app_id}.json", config / f"{app_id}.json")
    (candidate / "candidate-manifest.json").write_text(
        json.dumps({"format": 2, "files": {}}), encoding="utf-8")
    first = configure(candidate)
    assert not first.installations["ocr"].installed
    assert not first.installations["classification"].installed
    assert not first.failures

    bundle = candidate / "bundles" / "ocr"
    bundle.mkdir(parents=True)
    runtime = bundle / "runtimes" / "control" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixed-runtime")
    executable = b"@echo off\r\n"
    (bundle / "launch.cmd").write_bytes(executable)
    declaration = json.dumps({"app_id": "ocr", "files": [
        {"path": "launch.cmd", "sha256": digest(executable)}]}).encode()
    (bundle / "bundle.manifest.json").write_bytes(declaration)
    sealed = {"bundles/ocr/launch.cmd": {"sha256": digest(executable)},
              "bundles/ocr/bundle.manifest.json": {"sha256": digest(declaration)},
              "bundles/ocr/runtimes/control/python.exe": {
                  "sha256": digest(b"fixed-runtime"), "bytes": len(b"fixed-runtime")}}
    (candidate / "candidate-manifest.json").write_text(
        json.dumps({"format": 2, "files": sealed}), encoding="utf-8")
    installed = configure(candidate)
    assert installed.installations["ocr"].installed
    assert not installed.installations["classification"].installed
    assert installed.installations["ocr"].executable == bundle / "launch.cmd"

    class_bundle = candidate / "bundles" / "classification"
    class_bundle.mkdir()
    class_executable = b"@echo class\r\n"
    (class_bundle / "launch.cmd").write_bytes(class_executable)
    class_declaration = json.dumps({"app_id": "classification", "files": [
        {"path": "launch.cmd", "sha256": digest(class_executable)}]}).encode()
    (class_bundle / "bundle.manifest.json").write_bytes(class_declaration)
    class_runtime = class_bundle / "runtimes" / "control" / "python.exe"
    class_runtime.parent.mkdir(parents=True)
    class_runtime.write_bytes(b"class-runtime")
    sealed.update({
        "bundles/classification/launch.cmd": {"sha256": digest(class_executable)},
        "bundles/classification/bundle.manifest.json": {"sha256": digest(class_declaration)},
        "bundles/classification/runtimes/control/python.exe": {
            "sha256": digest(b"class-runtime"), "bytes": len(b"class-runtime")},
    })
    (candidate / "candidate-manifest.json").write_text(
        json.dumps({"format": 2, "files": sealed}), encoding="utf-8")
    both = configure(candidate)
    assert both.installations["ocr"].installed and both.installations["classification"].installed

    moved = tmp_path / "moved"
    shutil.copytree(candidate, moved)
    relocated = configure(moved)
    assert relocated.installations["ocr"].executable == moved / "bundles/ocr/launch.cmd"
    assert relocated.installations["classification"].executable == moved / "bundles/classification/launch.cmd"
    shared = tmp_path / "shared-data"
    monkeypatch.setenv("WORKBENCH_INSTALL_DATA_DIR", str(shared))
    external = configure(moved)
    assert external.config_dir == shared / "config/apps"
    assert json.loads((moved / "data/install-data.json").read_text(encoding="utf-8"))[
        "data_root"] == str(shared)
    assert external.installations["ocr"].installed and external.installations["classification"].installed
    (moved / "bundles/ocr/runtimes/control/python.exe").write_bytes(b"tampered")
    unavailable = configure(moved)
    assert not unavailable.installations["ocr"].installed
    assert unavailable.installations["classification"].installed
    assert not unavailable.failures
