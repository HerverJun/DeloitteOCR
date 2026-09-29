"""Candidate verification detects changed program bytes while allowing data writes."""

import hashlib
import json

import pytest

from scripts.build_base_candidate import (
    _classification_bundle, _copy_tree, _licenses, _ocr_bundle, _supplement_licenses, verify,
)
from scripts.candidate_verify import verify as bundled_verify


def test_candidate_manifest_rejects_program_tampering(tmp_path):
    candidate = tmp_path / "candidate"
    files = {}
    for name in ("launch.cmd", "verify.cmd", "verify.py", "app/bootstrap.py",
                 "app/apps/portal/dist/index.html",
                 "runtimes/platform/python.exe", "runtimes/data/python.exe",
                 "dependency-inventory.json"):
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        target.write_bytes(content)
        files[name] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (candidate / "candidate-manifest.json").write_text(
        json.dumps({"format": 3, "qualification": "engineering_preview", "files": files}),
        encoding="utf-8")
    (candidate / "data").mkdir()
    (candidate / "data" / "user.txt").write_text("later user data", encoding="utf-8")
    assert verify(candidate)["files"] == len(files)
    assert bundled_verify(candidate)["files"] == len(files)
    (candidate / "launch.cmd").write_bytes(b"modified launcher")
    with pytest.raises(ValueError, match="changed"):
        verify(candidate)
    with pytest.raises(ValueError, match="changed"):
        bundled_verify(candidate)


def test_license_inventory_includes_spdx_named_files_in_license_directory(tmp_path):
    folder = tmp_path / "sample-1.0.dist-info" / "licenses"
    folder.mkdir(parents=True)
    (folder / "BSD-3-Clause.txt").write_text("upstream notice", encoding="utf-8")
    assert _licenses(tmp_path)[0]["license_files"] == [
        "sample-1.0.dist-info/licenses/BSD-3-Clause.txt"]


def test_classification_bundle_owns_and_declares_its_worker(tmp_path):
    source = tmp_path / "source"
    for name in ("src/image_platform/__init__.py", "schemas/project.schema.json",
                 "apps/web/dist/index.html"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"python")
    (runtime / "python312.zip").write_bytes(b"stdlib")
    site = tmp_path / "site"
    site.mkdir()
    stage = tmp_path / "candidate"
    _classification_bundle(stage, source, site, runtime)
    bundle = stage / "bundles/classification"
    declared = {item["path"] for item in json.loads(
        (bundle / "bundle.manifest.json").read_text(encoding="utf-8"))["files"]}
    assert {"launch.cmd", "server.py", "worker.py",
            "runtimes/control/python.exe"} <= declared
    server = (bundle / "server.py").read_text(encoding="utf-8")
    assert "import subprocess" in server and "subprocess.Popen" in server
    assert 'pointer = installation / "data" / "install-data.json"' in server
    compile(server, str(bundle / "server.py"), "exec")
    worker = (bundle / "worker.py").read_text(encoding="utf-8")
    assert "install_weights(root," in worker
    assert "install_weights(root," not in server
    compile(worker, str(bundle / "worker.py"), "exec")


def test_runtime_copy_excludes_development_pth(tmp_path):
    source = tmp_path / "site"
    source.mkdir()
    (source / "distutils-precedence.pth").write_text("import local_dev", encoding="utf-8")
    (source / "module.py").write_text("value = 1", encoding="utf-8")
    target = tmp_path / "runtime"
    _copy_tree(source, target)
    assert (target / "module.py").is_file()
    assert not (target / "distutils-precedence.pth").exists()


def test_ocr_bundle_reads_shared_data_pointer(tmp_path):
    source = tmp_path / "ocr-source"
    for name in ("src/ocr_workbench/__init__.py", "config/engines.json",
                 "frontend/dist/index.html"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"python")
    (runtime / "python312.zip").write_bytes(b"stdlib")
    site = tmp_path / "site"
    site.mkdir()
    stage = tmp_path / "candidate"
    _ocr_bundle(stage, source, site, runtime, runtime)
    server = (stage / "bundles/ocr/server.py").read_text(encoding="utf-8")
    assert 'pointer = installation / "data" / "install-data.json"' in server
    assert 'str(data_root / "ocr-token.txt")' in server
    compile(server, str(stage / "bundles/ocr/server.py"), "exec")


def test_full_ocr_bundle_seals_models_and_uses_recognition_mode(tmp_path):
    source = tmp_path / "ocr-source"
    for name in ("src/ocr_workbench/__init__.py", "frontend/dist/index.html"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    config = source / "config/model-lock.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"sample": {"repo": "example/sample", "revision": "locked"}}), encoding="utf-8")
    full = tmp_path / "full-source"
    files = {}
    for name, value in {
        "models/sample/source-manifest.json": json.dumps({"repo": "example/sample", "revision": "locked"}),
        "models/sample/weight.bin": "weight bytes",
        "licenses/frontend/@example/LICENSE": "license text",
        "runtimes/llama/llama-server.exe": "llama",
        **{f"runtimes/{engine}/python.exe": engine for engine in
           ("ppocr", "paddlevl", "glm", "hunyuan")},
    }.items():
        path = full / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        files[name] = {"path": name, "bytes": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (full / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": list(files.values())}), encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"python")
    (runtime / "python312.zip").write_bytes(b"stdlib")
    site = tmp_path / "site"
    site.mkdir()
    stage = tmp_path / "candidate"
    _ocr_bundle(stage, source, site, runtime, runtime, full)
    bundle = stage / "bundles/ocr"
    assert "--review-only" not in (bundle / "server.py").read_text(encoding="utf-8")
    assert (bundle / "models/sample/weight.bin").read_text(encoding="utf-8") == "weight bytes"
    assert "../../src" in (bundle / "runtimes/ppocr/python312._pth").read_text(encoding="utf-8")
    app_files = {item["path"] for item in json.loads(
        (bundle / "manifest.json").read_text(encoding="utf-8"))["files"]}
    assert {"bundle.manifest.json", "models/sample/weight.bin",
            "licenses/frontend/@example/LICENSE"} <= app_files
    launch_files = {item["path"] for item in json.loads(
        (bundle / "bundle.manifest.json").read_text(encoding="utf-8"))["files"]}
    assert "licenses/frontend/@example/LICENSE" not in launch_files
    from workbench_platform.catalog.registry import _bundle_path
    assert all(_bundle_path(path) for path in launch_files)


def test_upstream_license_cache_is_hash_pinned(tmp_path):
    cache = tmp_path / "cache"
    source = cache / "sample-1.0" / "LICENSE"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"upstream license")
    record = {"url": "https://raw.githubusercontent.com/example/repo/v1/LICENSE",
              "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "bytes": source.stat().st_size}
    (cache / "manifest.json").write_text(
        json.dumps({"format": 1, "files": {"sample-1.0/LICENSE": record}}), encoding="utf-8")
    site = tmp_path / "site"
    (site / "sample-1.0.dist-info").mkdir(parents=True)
    assert _supplement_licenses(cache, {"control": site}) == {"sample-1.0/LICENSE": record}
    assert _licenses(site)[0]["license_files"] == [
        "sample-1.0.dist-info/licenses/upstream/LICENSE"]
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        _supplement_licenses(cache, {"control": site})
