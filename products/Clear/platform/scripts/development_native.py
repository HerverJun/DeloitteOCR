"""Bind the source preview to F-drive native runtimes without changing old packages.

This is a development composition, not a standalone distribution. Heavy assets
and site-packages remain in the previously verified program; small Python
launch files are private copies so every child imports the current source.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from workbench_platform.catalog import routes
from workbench_platform.catalog.registry import AppRegistry, _bundle_path
from workbench_platform.supervisor.processes import require_f_path, storage_environment


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path, value: dict) -> None:
    require_f_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _link(path: Path, target: Path) -> None:
    path, target = require_f_path(path), require_f_path(target)
    if path.exists():
        if path.resolve() != target.resolve():
            raise ValueError("Development asset binding differs; no existing path was replaced")
        return
    if not target.is_dir():
        raise ValueError("Required native asset directory is missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    f"New-Item -ItemType Junction -Path {quote(path)} -Target {quote(target)} | Out-Null"],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if path.resolve() != target:
        raise ValueError("Native asset real target differs")


def _copy(source: Path, target: Path) -> None:
    source, target = require_f_path(source), require_f_path(target)
    if not target.exists() or _sha(source) != _sha(target):
        shutil.copyfile(source, target)


def runtime_source_record(asset: Path, aliases: list[str], sealed: dict, candidate_sha256: str) -> dict:
    """Bind linked runtime libraries to exact files in the sealed old candidate."""
    sources = {alias: {} for alias in aliases}
    prefix = 'bundles/ocr/'
    for name, item in sealed.items():
        if not name.startswith(prefix) or Path(name).suffix.lower() not in {'.dll', '.pyd', '.exe'}:
            continue
        relative = name[len(prefix):]
        for alias in aliases:
            if relative.startswith(alias + '/'):
                sources[alias][relative[len(alias) + 1:]] = {
                    'sha256': item['sha256'], 'bytes': item['bytes']}
    return {'schema_version': 1, 'source_bundle': str(asset), 'candidate_sha256': candidate_sha256,
            'sources': [{'path': alias, 'files': files} for alias, files in sorted(sources.items())]}


SERVER = '''import json, os, subprocess, sys
from pathlib import Path
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
binding = json.loads((root / "native-binding.json").read_text(encoding="utf-8"))
data_root = Path(binding["data_root"])
data = data_root / "native" / binding["app_id"]
data.mkdir(parents=True, exist_ok=True)
if binding["app_id"] == "ocr":
    from ocr_workbench.service import main
    sys.argv = ["ocr-workbench", "--bundle", str(root), "--data", str(data),
                "--token-file", str(data_root / "ocr-token.txt")]
    main()
else:
    os.environ["ICP_DATA_DIR"] = str(data)
    os.environ["HF_HOME"] = str(data / "cache/huggingface")
    from offline_weights import install_weights
    install_weights(Path(binding["asset_bundle"]), data)
    from image_platform.cli import main
    with (data / "worker.log").open("ab") as log:
        worker = subprocess.Popen([sys.executable, "-I", "-B", "-u", str(root / "worker.py")],
            cwd=root, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            sys.argv = ["image-platform", "serve"]
            main()
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=15)
'''

WORKER = '''import sys
from image_platform.cli import main
sys.argv = ["image-platform", "worker"]
main()
'''


def configure(source: Path, program: Path, data: Path) -> AppRegistry:
    source, program, data = (require_f_path(path) for path in (source, program, data))
    native_sources = {"ocr": source.parent / "ocr", "classification": source.parent / "classification"}
    for path in (*native_sources.values(), source / "apps/portal/dist", data):
        require_f_path(path)
    os.environ.update(storage_environment(data))
    declaration = (program / "candidate-manifest.json").read_bytes()
    sealed = json.loads(declaration)["files"]
    # A stale whole-package receipt cannot authorize reuse. Check the native
    # bundle declarations once and retain a separate, accurately scoped receipt.
    cache_path = data / "native-program/asset-verification.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    identity = {"program_root": str(program), "candidate_sha256": hashlib.sha256(declaration).hexdigest(),
                "scope": "declared_native_bundle_files", "schema": 1}
    successes = cache.get("apps", {}) if all(cache.get(key) == value for key, value in identity.items()) else {}
    bundle_root = data / "native-program/bundles"
    config = data / "config/apps"
    config.mkdir(parents=True, exist_ok=True)
    for app_id, native_source in native_sources.items():
        native_source = require_f_path(native_source)
        asset = require_f_path(program / "bundles" / app_id)
        prefix = f"bundles/{app_id}/"
        for relative in ("bundle.manifest.json", "launch.cmd", "server.py", "runtimes/control/python.exe",
                         "runtimes/control/python312._pth"):
            if _sha(asset / relative) != sealed[prefix + relative]["sha256"]:
                raise ValueError(f"Existing {app_id} startup files changed")
        native_declaration = json.loads((asset / "bundle.manifest.json").read_text(encoding="utf-8"))
        manifest_sha = _sha(asset / "bundle.manifest.json")
        if successes.get(app_id) != manifest_sha:
            print(f"Checking declared {app_id} assets once ({len(native_declaration['files'])} files)", flush=True)
            for item in native_declaration["files"]:
                member = asset.joinpath(*_bundle_path(item["path"]))
                if not member.resolve().is_relative_to(asset) or _sha(member) != item["sha256"]:
                    raise ValueError(f"Declared {app_id} asset changed: {item['path']}")
            successes[app_id] = manifest_sha
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            _json(cache_path, {**identity, "apps": successes})
        bundle = require_f_path(bundle_root / app_id)
        bundle.mkdir(parents=True, exist_ok=True)
        runtime_aliases = []
        for runtime in (asset / "runtimes").iterdir():
            if not runtime.is_dir():
                continue
            target = bundle / "runtimes" / runtime.name
            if not (runtime / "python.exe").is_file():
                _link(target, runtime)
                runtime_aliases.append(target.relative_to(bundle).as_posix())
                continue
            target.mkdir(parents=True, exist_ok=True)
            for member in runtime.iterdir():
                if member.is_file() and member.suffix != "._pth":
                    _copy(member, target / member.name)
                elif member.is_dir():
                    _link(target / member.name, member)
                    runtime_aliases.append((target / member.name).relative_to(bundle).as_posix())
            (target / "python312._pth").write_text("\n".join([
                "python312.zip", ".", str(native_source / "src"), str(source), str(source / "src"),
                "Lib/site-packages", "import site", ""]), encoding="utf-8")
        if app_id == "ocr":
            for name in ("config", "models", "fonts", "licenses"):
                _link(bundle / name, asset / name)
            _link(bundle / "web", native_source / "frontend/dist")
            _copy(asset / "manifest.json", bundle / "manifest.json")
        else:
            _copy(asset / "src/image_platform/platform_weights.py", bundle / "offline_weights.py")
            (bundle / "worker.py").write_text(WORKER, encoding="utf-8")
        _json(bundle / "native-binding.json", {"app_id": app_id, "data_root": str(data),
            "asset_bundle": str(asset), "native_source": str(native_source), "platform_source": str(source),
            "asset_candidate_sha256": identity['candidate_sha256'],
            "qualification": "source development; verified existing runtime assets reused read-only"})
        if app_id == 'ocr':
            _json(bundle / 'runtime-sources.json', runtime_source_record(
                asset, runtime_aliases, sealed, identity['candidate_sha256']))
        (bundle / "server.py").write_text(SERVER, encoding="utf-8")
        (bundle / "launch.cmd").write_text('@echo off\n"%~dp0runtimes\\control\\python.exe" -I -B -u "%~dp0server.py"\n', encoding="utf-8")
        # Declare private launch files only. Junction assets are bound above to
        # the previous program and its recorded complete verification identity.
        files = [path for path in bundle.iterdir() if path.is_file() and path.name != "bundle.manifest.json"]
        files += [path for runtime in (bundle / "runtimes").iterdir()
                  if not runtime.is_junction() for path in runtime.iterdir() if path.is_file()]
        _json(bundle / "bundle.manifest.json", {"app_id": app_id, "files": [
            {"path": path.relative_to(bundle).as_posix(), "sha256": _sha(path)} for path in sorted(files)]})
        _copy(source / f"config/apps/{app_id}.json", config / f"{app_id}.json")
        _json(config / f"{app_id}.install.json", {"app_id": app_id, "installed": True,
            "manifest_sha256": _sha(config / f"{app_id}.json"), "launch": {
                "bundle_root": str(bundle), "executable": str(bundle / "launch.cmd"),
                "working_directory": str(bundle), "allowed_arguments": [],
                "executable_sha256": _sha(bundle / "launch.cmd"),
                "bundle_manifest_sha256": _sha(bundle / "bundle.manifest.json")}})
    token = require_f_path(data / "ocr-token.txt")
    if not token.exists():
        token.write_text(secrets.token_urlsafe(36), encoding="utf-8")
    os.environ["WORKBENCH_OCR_TOKEN_FILE"] = str(token)
    registry = AppRegistry(config, install_root=bundle_root)
    if registry.failures or not all(value.installed for value in registry.installations.values()):
        raise ValueError("Native development registration failed")
    routes._registry = registry
    return registry
