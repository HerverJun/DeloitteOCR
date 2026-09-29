"""Candidate install records follow the current directory after relocation."""

import hashlib
import json
import os
from pathlib import Path
import shutil

from scripts.candidate_bootstrap import configure
from workbench_platform.catalog import routes


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_optional_app_registration_is_local_and_missing_app_stays_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_registry", routes._registry)
    for name in ("WORKBENCH_DATA_DIR", "WORKBENCH_DATA_PYTHON",
                 "WORKBENCH_PORTAL_DIST", "WORKBENCH_OCR_TOKEN_FILE", "PYTHONUTF8"):
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
    (moved / "bundles/ocr/runtimes/control/python.exe").write_bytes(b"tampered")
    unavailable = configure(moved)
    assert not unavailable.installations["ocr"].installed
    assert unavailable.installations["classification"].installed
    assert not unavailable.failures
