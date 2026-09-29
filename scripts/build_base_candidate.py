"""Build a Windows base-portal candidate from fixed, local runtime inputs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", "build",
             "workbench_platform-0.1.0.dist-info"}
SKIP_FILES = {"__editable__.workbench_platform-0.1.0.pth", "direct_url.json"}
CHUNK = 1024 * 1024


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(source)
    for current, dirs, files in os.walk(source, followlinks=False):
        parent = Path(current)
        for name in dirs:
            path = parent / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError(f"Linked candidate source: {path}")
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            path = parent / name
            if path.is_symlink() or name.endswith(".pyc") or name in SKIP_FILES:
                continue
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def _runtime(base: Path, site: Path, destination: Path) -> None:
    if not (base / "python.exe").is_file() or not (base / "python312.zip").is_file():
        raise ValueError("A complete local embedded Python 3.12 runtime is required")
    destination.mkdir(parents=True)
    for entry in base.iterdir():
        if entry.is_file() and entry.name != "python312._pth":
            shutil.copy2(entry, destination / entry.name)
    _copy_tree(site, destination / "Lib" / "site-packages")
    (destination / "python312._pth").write_text(
        "python312.zip\n.\nLib/site-packages\n../../app\n../../app/src\nimport site\n",
        encoding="utf-8")


def _licenses(site: Path) -> list[dict]:
    packages = []
    for metadata in sorted(site.glob("*.dist-info")):
        if not metadata.is_dir():
            continue
        license_files = [path.relative_to(site).as_posix() for path in metadata.rglob("*")
                         if path.is_file() and ("licen" in path.name.lower()
                                                or "copying" in path.name.lower()
                                                or "notice" in path.name.lower())]
        packages.append({"distribution": metadata.name,
                         "license_files": sorted(license_files)})
    return packages


def build(output: Path, python_base: Path, platform_site: Path, data_site: Path,
          *, source: Path = ROOT) -> dict:
    output = output.resolve()
    source, python_base = source.resolve(), python_base.resolve()
    platform_site, data_site = platform_site.resolve(), data_site.resolve()
    if output.exists() or any(output.is_relative_to(path) or path.is_relative_to(output)
                              for path in (source, python_base, platform_site, data_site)):
        raise ValueError("Candidate output must be a new directory outside all inputs")
    if not (source / "apps/portal/dist/index.html").is_file():
        raise FileNotFoundError("Build the portal before creating the candidate")
    stage = output.with_name(output.name + ".pending-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        app = stage / "app"
        _copy_tree(source / "src" / "workbench_platform", app / "src" / "workbench_platform")
        _copy_tree(source / "packages" / "contracts", app / "packages" / "contracts")
        _copy_tree(source / "modules" / "data_workbench", app / "modules" / "data_workbench")
        _copy_tree(source / "apps" / "portal" / "dist", app / "apps" / "portal" / "dist")
        (app / "config" / "apps").mkdir(parents=True)
        for name in ("ocr.json", "classification.json"):
            shutil.copy2(source / "config" / "apps" / name, app / "config" / "apps" / name)
        (app / "config" / "runtime-locks").mkdir(parents=True)
        shutil.copy2(source / "config/runtime-locks/data.json",
                     app / "config/runtime-locks/data.json")
        _runtime(python_base, platform_site, stage / "runtimes" / "platform")
        _runtime(python_base, data_site, stage / "runtimes" / "data")
        (stage / "data").mkdir()
        launcher = ("@echo off\r\nsetlocal\r\nset ROOT=%~dp0\r\n"
                    "set WORKBENCH_DATA_DIR=%ROOT%data\r\n"
                    "set WORKBENCH_DATA_PYTHON=%ROOT%runtimes\\data\\python.exe\r\n"
                    "set WORKBENCH_PORTAL_DIST=%ROOT%app\\apps\\portal\\dist\r\n"
                    "set PYTHONUTF8=1\r\nset PORT=8765\r\n"
                    "if not \"%~1\"==\"\" set PORT=%~1\r\n"
                    "cd /d \"%ROOT%app\"\r\n"
                    "\"%ROOT%runtimes\\platform\\python.exe\" -I -B -m uvicorn "
                    "workbench_platform.app:app --host 127.0.0.1 --port %PORT%\r\n")
        (stage / "launch.cmd").write_bytes(launcher.encode("ascii"))
        inventory = {name: _licenses(site) for name, site in (
            ("platform", stage / "runtimes/platform/Lib/site-packages"),
            ("data", stage / "runtimes/data/Lib/site-packages"))}
        (stage / "dependency-inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        files = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file() and not path.is_relative_to(stage / "data"):
                files[path.relative_to(stage).as_posix()] = {"sha256": _hash(path),
                                                                "bytes": path.stat().st_size}
        manifest = {"format": 1, "qualification": "engineering_preview",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "components": ["base_portal", "data_workbench"],
                    "optional_apps": {"ocr": "not_installed", "classification": "not_installed"},
                    "files": files}
        (stage / "candidate-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        verify(stage)
        os.replace(stage, output)
        return {"output": str(output), "files": len(files),
                "license_inventory": sum(len(rows) for rows in inventory.values())}
    except BaseException:
        shutil.rmtree(stage)
        raise


def verify(candidate: Path) -> dict:
    candidate = candidate.resolve()
    manifest = json.loads((candidate / "candidate-manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files")
    if (manifest.get("format") != 1 or not isinstance(files, dict)
            or manifest.get("qualification") != "engineering_preview"):
        raise ValueError("Unsupported candidate manifest")
    required = {"launch.cmd", "app/apps/portal/dist/index.html",
                "runtimes/platform/python.exe", "runtimes/data/python.exe",
                "dependency-inventory.json"}
    if not required <= files.keys():
        raise ValueError("Candidate is missing a required component")
    actual = set()
    for path in candidate.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Linked candidate member: {path}")
        if path.is_file() and not path.is_relative_to(candidate / "data"):
            relative = path.relative_to(candidate).as_posix()
            if relative != "candidate-manifest.json":
                actual.add(relative)
    if actual != files.keys():
        raise ValueError("Candidate file set differs from its manifest")
    for name, identity in files.items():
        path = Path(name)
        if (path.is_absolute() or path.drive or path.as_posix() != name
                or any(part in {".", ".."} for part in path.parts)):
            raise ValueError(f"Unsafe candidate member: {name}")
        member = candidate / path
        if (not member.is_file() or member.stat().st_size != identity["bytes"]
                or _hash(member) != identity["sha256"]):
            raise ValueError(f"Candidate member changed: {name}")
    return {"candidate": str(candidate), "files": len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python-base", type=Path)
    parser.add_argument("--platform-site", type=Path)
    parser.add_argument("--data-site", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        print(json.dumps(verify(args.verify), ensure_ascii=False))
        return
    if not all((args.output, args.python_base, args.platform_site, args.data_site)):
        parser.error("Building requires output and all three runtime inputs")
    print(json.dumps(build(args.output, args.python_base, args.platform_site, args.data_site),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
