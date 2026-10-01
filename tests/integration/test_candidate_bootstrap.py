"""Candidate install records follow the current directory after relocation."""

import hashlib
import json
import os
from pathlib import Path
import shutil
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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
