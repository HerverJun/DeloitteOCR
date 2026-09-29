"""Candidate verification detects changed program bytes while allowing data writes."""

import hashlib
import json

import pytest

from scripts.build_base_candidate import _classification_bundle, _copy_tree, _licenses, verify


def test_candidate_manifest_rejects_program_tampering(tmp_path):
    candidate = tmp_path / "candidate"
    files = {}
    for name in ("launch.cmd", "app/bootstrap.py", "app/apps/portal/dist/index.html",
                 "runtimes/platform/python.exe", "runtimes/data/python.exe",
                 "dependency-inventory.json"):
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        target.write_bytes(content)
        files[name] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (candidate / "candidate-manifest.json").write_text(
        json.dumps({"format": 2, "qualification": "engineering_preview", "files": files}),
        encoding="utf-8")
    (candidate / "data").mkdir()
    (candidate / "data" / "user.txt").write_text("later user data", encoding="utf-8")
    assert verify(candidate)["files"] == len(files)
    (candidate / "launch.cmd").write_bytes(b"modified launcher")
    with pytest.raises(ValueError, match="changed"):
        verify(candidate)


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
    assert "subprocess.Popen" in (bundle / "server.py").read_text(encoding="utf-8")


def test_runtime_copy_excludes_development_pth(tmp_path):
    source = tmp_path / "site"
    source.mkdir()
    (source / "distutils-precedence.pth").write_text("import local_dev", encoding="utf-8")
    (source / "module.py").write_text("value = 1", encoding="utf-8")
    target = tmp_path / "runtime"
    _copy_tree(source, target)
    assert (target / "module.py").is_file()
    assert not (target / "distutils-precedence.pth").exists()
