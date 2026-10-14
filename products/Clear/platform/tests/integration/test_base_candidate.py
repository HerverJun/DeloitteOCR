"""Candidate verification detects changed program bytes while allowing data writes."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.build_base_candidate import (
    _classification_bundle, _copy_tree, _licenses, _ocr_bundle, _seal_ocr_manifest,
    _supplement_licenses, verify,
)
from scripts.candidate_verify import verify as bundled_verify
from scripts import build_base_candidate as candidate_builder


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


LEGACY_LICENSE_CASES = [
    ("glm", "sentencepiece-0.2.2"), ("glm", "tokenizers-0.23.2"),
    ("paddlevl", "bce_python_sdk-0.9.79"), ("paddlevl", "latex2mathml-3.81.1"),
    ("paddlevl", "sentencepiece-0.2.2"), ("paddlevl", "tokenizers-0.23.2"),
    ("ppocr", "bce_python_sdk-0.9.79"), ("ppocr", "latex2mathml-3.81.1"),
    ("ppocr", "sentencepiece-0.2.2"), ("ppocr", "tokenizers-0.23.2"),
]


def legacy_ocr_fixture(tmp_path, cases=None):
    cases = LEGACY_LICENSE_CASES if cases is None else cases
    source, full, cache = (tmp_path / name for name in ("ocr-source", "old-ocr", "cache"))
    for name, value in {
        "src/ocr_workbench/__init__.py": "# small CPU fixture",
        "frontend/dist/index.html": "<html>fixture</html>",
        "config/model-lock.json": json.dumps({"sample": {"repo": "example/sample", "revision": "locked"}}),
    }.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    members = {
        "models/sample/source-manifest.json": json.dumps({"repo": "example/sample", "revision": "locked"}),
        "models/sample/weight.bin": "locked weight bytes",
        "licenses/frontend/@example/LICENSE": "registered upstream license",
        "runtimes/llama/llama-server.exe": "locked llama bytes",
        **{f"runtimes/{engine}/python.exe": engine for engine in ("ppocr", "paddlevl", "glm", "hunyuan")},
        **{f"runtimes/{engine}/Lib/site-packages/{package}.dist-info/METADATA": package for engine, package in cases},
    }
    records = []
    for name, value in members.items():
        path = full / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        records.append({"path": name, "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (full / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": records}), encoding="utf-8")
    cached = {}
    old_licenses = []
    for engine, package in cases:
        key = f"{package}/LICENSE"
        value = ("current pinned upstream notice: " + package).encode()
        path = cache / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        cached[key] = {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest(),
                       "url": f"https://raw.githubusercontent.com/example/{package}/locked/LICENSE"}
        relative = f"runtimes/{engine}/Lib/site-packages/{package}.dist-info/licenses/upstream/LICENSE"
        old = full / relative
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(value)
        old_licenses.append(relative)
    (cache / "manifest.json").write_text(json.dumps({"format": 1, "files": cached}), encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"fixture python")
    (runtime / "python312.zip").write_bytes(b"fixture stdlib")
    site = tmp_path / "site"
    site.mkdir()
    return SimpleNamespace(source=source, full=full, cache=cache, cached=cached, runtime=runtime,
                           site=site, stage=tmp_path / "stage", old_licenses=old_licenses)


def copy_legacy_ocr(fixture):
    _ocr_bundle(fixture.stage, fixture.source, fixture.site, fixture.runtime, fixture.runtime,
                fixture.full, license_cache=fixture.cache, finalize_manifest=False)
    return fixture.stage / "bundles/ocr"


@pytest.mark.parametrize("engine,package", LEGACY_LICENSE_CASES)
def test_exact_legacy_ocr_license_is_rebuilt_from_current_cache(tmp_path, engine, package):
    fixture = legacy_ocr_fixture(tmp_path, [(engine, package)])
    before = {path.relative_to(fixture.full).as_posix(): path.read_bytes()
              for path in fixture.full.rglob("*") if path.is_file()}
    bundle = copy_legacy_ocr(fixture)
    relative = fixture.old_licenses[0]
    assert not (bundle / relative).exists(), "Do not reuse the unregistered old license copy"
    assert not (bundle / "manifest.json").exists()
    site = bundle / f"runtimes/{engine}/Lib/site-packages"
    assert _supplement_licenses(fixture.cache, {engine: site}) == fixture.cached
    _seal_ocr_manifest(bundle)
    records = {item["path"]: item for item in json.loads((bundle / "manifest.json").read_text())["files"]}
    expected = fixture.cached[f"{package}/LICENSE"]
    assert records[relative]["bytes"] == expected["bytes"]
    assert records[relative]["sha256"] == expected["sha256"]
    assert (bundle / relative).read_bytes() == (fixture.cache / f"{package}/LICENSE").read_bytes()
    assert before == {path.relative_to(fixture.full).as_posix(): path.read_bytes()
                      for path in fixture.full.rglob("*") if path.is_file()}


@pytest.mark.parametrize("relative", [
    "runtimes/glm/Lib/site-packages/unregistered.dll",
    "models/sample/unregistered.bin",
    "licenses/frontend/unknown/LICENSE",
    "runtimes/glm/Lib/site-packages/sentencepiece-0.2.2.dist-info/licenses/upstream/NOTICE",
])
def test_unregistered_ocr_runtime_model_or_other_license_is_rejected(tmp_path, relative):
    fixture = legacy_ocr_fixture(tmp_path)
    path = fixture.full / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unregistered input")
    with pytest.raises(ValueError, match="OCR source bundle member changed"):
        copy_legacy_ocr(fixture)


@pytest.mark.parametrize("changed", ["old-license", "cache-bytes", "cache-hash", "missing-cache-record"])
def test_legacy_ocr_license_allowance_requires_both_old_and_current_cache_identity(tmp_path, changed):
    fixture = legacy_ocr_fixture(tmp_path)
    key = "sentencepiece-0.2.2/LICENSE"
    manifest = fixture.cache / "manifest.json"
    if changed == "old-license":
        (fixture.full / fixture.old_licenses[0]).write_bytes(b"untrusted old notice")
    elif changed == "cache-bytes":
        (fixture.cache / key).write_bytes(b"untrusted current cache")
    else:
        contents = json.loads(manifest.read_text())
        if changed == "cache-hash": contents["files"][key]["sha256"] = "0" * 64
        else: del contents["files"][key]
        manifest.write_text(json.dumps(contents), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        copy_legacy_ocr(fixture)


@pytest.mark.parametrize("relative", [
    "runtimes/glm/python.exe", "models/sample/weight.bin", "licenses/frontend/@example/LICENSE",
])
def test_registered_ocr_runtime_model_and_license_hashes_remain_strict(tmp_path, relative):
    fixture = legacy_ocr_fixture(tmp_path)
    path = fixture.full / relative
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ValueError, match="OCR source bundle member changed"):
        copy_legacy_ocr(fixture)


def test_registered_ocr_member_cannot_disappear_from_source(tmp_path):
    fixture = legacy_ocr_fixture(tmp_path)
    (fixture.full / "models/sample/weight.bin").unlink()
    with pytest.raises(ValueError, match="OCR source bundle member missing"):
        copy_legacy_ocr(fixture)


@pytest.mark.parametrize("bad_path", ["models/../outside.bin", "models/sample/weight.bin"])
def test_ocr_source_manifest_rejects_unsafe_or_duplicate_members(tmp_path, bad_path):
    fixture = legacy_ocr_fixture(tmp_path)
    manifest = fixture.full / "manifest.json"
    contents = json.loads(manifest.read_text())
    contents["files"].append({"path": bad_path, "bytes": 0, "sha256": "0" * 64})
    manifest.write_text(json.dumps(contents), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid OCR model bundle member declaration"):
        copy_legacy_ocr(fixture)


def test_ocr_native_manifest_is_sealed_after_all_supplemental_licenses(tmp_path):
    fixture = legacy_ocr_fixture(tmp_path)
    bundle = copy_legacy_ocr(fixture)
    assert not (bundle / "manifest.json").exists()
    assert all(not (bundle / relative).exists() for relative in fixture.old_licenses)
    sites = {engine: bundle / f"runtimes/{engine}/Lib/site-packages"
             for engine in ("ppocr", "paddlevl", "glm", "hunyuan")}
    assert _supplement_licenses(fixture.cache, sites) == fixture.cached
    _seal_ocr_manifest(bundle)
    records = {item["path"]: item for item in json.loads((bundle / "manifest.json").read_text())["files"]}
    actual = {path.relative_to(bundle).as_posix(): path for path in bundle.rglob("*")
              if path.is_file() and path != bundle / "manifest.json"}
    assert records.keys() == actual.keys()
    assert len(fixture.old_licenses) == 10 and set(fixture.old_licenses) <= records.keys()
    assert "manifest.json" not in records and "bundle.manifest.json" in records
    for relative, path in actual.items():
        assert records[relative]["bytes"] == path.stat().st_size
        assert records[relative]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    launch = json.loads((bundle / "bundle.manifest.json").read_text())
    declared = {item["path"] for item in launch["files"]}
    assert not set(fixture.old_licenses) & declared
    from workbench_platform.catalog.registry import _bundle_path
    assert all(_bundle_path(relative) for relative in declared)


def test_supplemental_license_copy_rechecks_pinned_identity(tmp_path, monkeypatch):
    fixture = legacy_ocr_fixture(tmp_path, [("glm", "sentencepiece-0.2.2")])
    bundle = copy_legacy_ocr(fixture)
    original_copy = candidate_builder.shutil.copy2

    def changed_copy(source, target):
        result = original_copy(source, target)
        path = Path(target)
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        return result

    monkeypatch.setattr(candidate_builder.shutil, "copy2", changed_copy)
    with pytest.raises(ValueError, match="Upstream license changed during copy"):
        _supplement_licenses(fixture.cache, {"glm": bundle / "runtimes/glm/Lib/site-packages"})


def test_copied_ocr_member_read_denial_reopens_and_hashes_from_zero(tmp_path, monkeypatch, capsys):
    fixture = legacy_ocr_fixture(tmp_path, [("glm", "sentencepiece-0.2.2")])
    bundle = fixture.stage / "bundles/ocr"
    target = bundle / "models/sample/weight.bin"
    expected = (fixture.full / "models/sample/weight.bin").read_bytes()
    original_open = Path.open
    attempts, waits = [], []
    denial = PermissionError(13, "temporary read denial", str(target))

    class Reader:
        def __init__(self, stream):
            self.stream = stream
            self.record = {"initial_position": stream.tell(), "bytes": b"", "closed": False}
            attempts.append(self.record)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()
            self.record["closed"] = self.stream.closed

        def read(self, size):
            if len(attempts) == 1 and self.record["bytes"]:
                raise denial
            value = self.stream.read(size)
            self.record["bytes"] += value
            return value

    def controlled_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return Reader(stream) if path == target and mode == "rb" else stream

    monkeypatch.setattr(candidate_builder, "CHUNK", 4)
    monkeypatch.setattr(Path, "open", controlled_open)
    monkeypatch.setattr(candidate_builder.time, "sleep", waits.append)
    candidate_builder._copy_ocr_model_members(fixture.full, bundle, fixture.cache)
    assert len(attempts) == 2 and waits == [1]
    assert attempts[0]["bytes"] == expected[:4]
    assert attempts[1]["bytes"] == expected
    assert all(row["initial_position"] == 0 and row["closed"] for row in attempts)
    stderr = capsys.readouterr().err
    assert str(target) in stderr and "attempt=1/3" in stderr and "PermissionError" in stderr
    assert (fixture.full / "models/sample/weight.bin").read_bytes() == expected


@pytest.mark.parametrize("broken_logger", [False, True])
def test_persistent_copied_ocr_member_read_denial_preserves_original_error(tmp_path, monkeypatch, capsys, broken_logger):
    fixture = legacy_ocr_fixture(tmp_path, [("glm", "sentencepiece-0.2.2")])
    bundle = fixture.stage / "bundles/ocr"
    target = bundle / "models/sample/weight.bin"
    original_open = Path.open
    attempts, waits = [], []
    denial = PermissionError(13, "persistent target denial", str(target))

    def denied_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            attempts.append(path)
            raise denial
        return original_open(path, mode, *args, **kwargs)

    class BrokenLogger:
        def write(self, _):
            raise ValueError("stderr is unavailable")

        def flush(self):
            raise ValueError("stderr is unavailable")

    monkeypatch.setattr(Path, "open", denied_open)
    monkeypatch.setattr(candidate_builder.time, "sleep", waits.append)
    if broken_logger:
        monkeypatch.setattr(candidate_builder.sys, "stderr", BrokenLogger())
    with pytest.raises(PermissionError) as caught:
        candidate_builder._copy_ocr_model_members(fixture.full, bundle, fixture.cache)
    assert caught.value is denial
    assert len(attempts) == 3 and waits == [1, 3]
    if not broken_logger:
        stderr = capsys.readouterr().err
        assert all(f"attempt={number}/3" in stderr for number in (1, 2, 3))
        assert stderr.count(str(target)) >= 3
    assert not (bundle / "manifest.json").exists()


@pytest.mark.parametrize("changed", ["same-size-hash", "size"])
def test_copied_ocr_member_mismatch_never_retries(tmp_path, monkeypatch, changed):
    fixture = legacy_ocr_fixture(tmp_path, [("glm", "sentencepiece-0.2.2")])
    bundle = fixture.stage / "bundles/ocr"
    target = bundle / "models/sample/weight.bin"
    original_copy, original_hash = candidate_builder.shutil.copy2, candidate_builder._hash
    target_hashes, waits = [], []

    def changed_copy(source, destination):
        result = original_copy(source, destination)
        if Path(destination) == target:
            content = target.read_bytes()
            target.write_bytes(bytes([content[0] ^ 1]) + content[1:] if changed == "same-size-hash" else content + b"changed")
        return result

    def tracked_hash(path):
        if path == target:
            target_hashes.append(path)
        return original_hash(path)

    monkeypatch.setattr(candidate_builder.shutil, "copy2", changed_copy)
    monkeypatch.setattr(candidate_builder, "_hash", tracked_hash)
    monkeypatch.setattr(candidate_builder.time, "sleep", waits.append)
    with pytest.raises(ValueError, match="changed during copy"):
        candidate_builder._copy_ocr_model_members(fixture.full, bundle, fixture.cache)
    assert waits == []
    assert len(target_hashes) == (1 if changed == "same-size-hash" else 0)


def test_copied_ocr_member_nonpermission_read_error_never_retries(tmp_path, monkeypatch):
    fixture = legacy_ocr_fixture(tmp_path, [("glm", "sentencepiece-0.2.2")])
    bundle = fixture.stage / "bundles/ocr"
    target = bundle / "models/sample/weight.bin"
    original_open = Path.open
    attempts, waits = [], []
    failure = OSError(5, "target I/O failure", str(target))

    def failed_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            attempts.append(path)
            raise failure
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failed_open)
    monkeypatch.setattr(candidate_builder.time, "sleep", waits.append)
    with pytest.raises(OSError) as caught:
        candidate_builder._copy_ocr_model_members(fixture.full, bundle, fixture.cache)
    assert caught.value is failure and len(attempts) == 1 and waits == []


def test_copied_ocr_member_changed_after_denial_fails_without_third_attempt(tmp_path, monkeypatch):
    fixture = legacy_ocr_fixture(tmp_path, [("glm", "sentencepiece-0.2.2")])
    bundle = fixture.stage / "bundles/ocr"
    target = bundle / "models/sample/weight.bin"
    original_open = Path.open
    attempts, waits = [], []

    def denied_once(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError(13, "target briefly unavailable", str(target))
        return original_open(path, mode, *args, **kwargs)

    def changed_during_wait(seconds):
        waits.append(seconds)
        with original_open(target, "rb") as stream:
            content = stream.read()
        with original_open(target, "wb") as stream:
            stream.write(bytes([content[0] ^ 1]) + content[1:])

    monkeypatch.setattr(Path, "open", denied_once)
    monkeypatch.setattr(candidate_builder.time, "sleep", changed_during_wait)
    with pytest.raises(ValueError, match="changed during copy"):
        candidate_builder._copy_ocr_model_members(fixture.full, bundle, fixture.cache)
    assert len(attempts) == 2 and waits == [1]
