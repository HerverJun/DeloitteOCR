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
             "workbench_platform-0.1.0.dist-info",
             "image_classification_platform-0.2.0.dist-info"}
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
            if (path.is_symlink() or name.endswith((".pyc", ".pth"))
                    or name in SKIP_FILES):
                continue
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def _runtime(base: Path, site: Path, destination: Path,
             *, imports: tuple[str, ...] = ("../../app", "../../app/src")) -> None:
    if not (base / "python.exe").is_file() or not (base / "python312.zip").is_file():
        raise ValueError("A complete local embedded Python 3.12 runtime is required")
    destination.mkdir(parents=True)
    for entry in base.iterdir():
        if entry.is_file() and entry.name != "python312._pth":
            shutil.copy2(entry, destination / entry.name)
    _copy_tree(site, destination / "Lib" / "site-packages")
    (destination / "python312._pth").write_text(
        "\n".join(("python312.zip", ".", "Lib/site-packages", *imports, "import site", "")),
        encoding="utf-8")


def _ocr_bundle(stage: Path, source: Path, control_site: Path, pdf_runtime: Path,
                python_base: Path) -> None:
    bundle = stage / "bundles" / "ocr"
    _copy_tree(source / "src" / "ocr_workbench", bundle / "src" / "ocr_workbench")
    _copy_tree(source / "config", bundle / "config")
    _copy_tree(source / "frontend" / "dist", bundle / "web")
    imports = ("../../src", "../../../../app", "../../../../app/src")
    _runtime(python_base, control_site, bundle / "runtimes" / "control", imports=imports)
    _copy_tree(pdf_runtime, bundle / "runtimes" / "pdf")
    (bundle / "runtimes" / "pdf" / "python312._pth").write_text(
        "\n".join(("python312.zip", ".", "Lib/site-packages", *imports, "import site", "")),
        encoding="utf-8")
    server = '''import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
installation = root.parent.parent
data = installation / "data" / "native" / "ocr"
local = installation / "data" / "native-local"
data.mkdir(parents=True, exist_ok=True)
local.mkdir(parents=True, exist_ok=True)
os.environ["LOCALAPPDATA"] = str(local)
os.environ["USERPROFILE"] = str(local)
from ocr_workbench.service import main
sys.argv = ["ocr-workbench", "--bundle", str(root), "--data", str(data),
            "--token-file", str(installation / "data" / "ocr-token.txt"), "--review-only"]
main()
'''
    (bundle / "server.py").write_text(server, encoding="utf-8")
    launcher = ("@echo off\r\nset ROOT=%~dp0\r\n"
                "\"%ROOT%runtimes\\control\\python.exe\" -I -B -u \"%ROOT%server.py\"\r\n")
    (bundle / "launch.cmd").write_bytes(launcher.encode("ascii"))
    declared = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == "bundle.manifest.json":
            continue
        relative = path.relative_to(bundle).as_posix()
        if not relative.startswith("runtimes/") or relative in {
                "runtimes/control/python.exe", "runtimes/pdf/python.exe"}:
            declared.append({"path": relative, "sha256": _hash(path)})
    declaration = json.dumps({"app_id": "ocr", "files": declared}, separators=(",", ":")).encode()
    if len(declaration) > 1_048_576:
        raise ValueError("OCR bundle declaration exceeds the catalog limit")
    (bundle / "bundle.manifest.json").write_bytes(declaration)


def _classification_bundle(stage: Path, source: Path, site: Path, python_base: Path) -> None:
    bundle = stage / "bundles" / "classification"
    _copy_tree(source / "src" / "image_platform", bundle / "src" / "image_platform")
    _copy_tree(source / "schemas", bundle / "schemas")
    _copy_tree(source / "apps" / "web" / "dist", bundle / "apps" / "web" / "dist")
    if (source / "resources").is_dir():
        _copy_tree(source / "resources", bundle / "resources")
    imports = ("../../src", "../../../../app", "../../../../app/src")
    _runtime(python_base, site, bundle / "runtimes" / "control", imports=imports)
    server = '''import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
installation = root.parent.parent
data = installation / "data" / "native" / "classification"
local = installation / "data" / "native-local"
data.mkdir(parents=True, exist_ok=True)
local.mkdir(parents=True, exist_ok=True)
os.environ["ICP_DATA_DIR"] = str(data)
os.environ["LOCALAPPDATA"] = str(local)
os.environ["USERPROFILE"] = str(local)
from image_platform.cli import main
sys.argv = ["image-platform", "serve"]
with (data / "worker.log").open("ab") as log:
    worker = subprocess.Popen(
        [sys.executable, "-I", "-B", "-u", str(root / "worker.py")],
        cwd=root, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        main()
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=15)
'''
    (bundle / "server.py").write_text(server, encoding="utf-8")
    (bundle / "worker.py").write_text(
        'import sys\nfrom image_platform.cli import main\n'
        'sys.argv = ["image-platform", "worker"]\nmain()\n', encoding="utf-8")
    launcher = ("@echo off\r\nset ROOT=%~dp0\r\n"
                "\"%ROOT%runtimes\\control\\python.exe\" -I -B -u \"%ROOT%server.py\"\r\n")
    (bundle / "launch.cmd").write_bytes(launcher.encode("ascii"))
    declared = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == "bundle.manifest.json":
            continue
        relative = path.relative_to(bundle).as_posix()
        if not relative.startswith("runtimes/") or relative == "runtimes/control/python.exe":
            declared.append({"path": relative, "sha256": _hash(path)})
    declaration = json.dumps({"app_id": "classification", "files": declared},
                             separators=(",", ":")).encode()
    if len(declaration) > 1_048_576:
        raise ValueError("Classification bundle declaration exceeds the catalog limit")
    (bundle / "bundle.manifest.json").write_bytes(declaration)


def _licenses(site: Path) -> list[dict]:
    packages = []
    for metadata in sorted(site.glob("*.dist-info")):
        if not metadata.is_dir():
            continue
        license_files = [path.relative_to(site).as_posix() for path in metadata.rglob("*")
                         if path.is_file() and any(
                             word in part.lower() for part in path.relative_to(metadata).parts
                             for word in ("licen", "copying", "notice"))]
        packages.append({"distribution": metadata.name,
                         "license_files": sorted(license_files)})
    return packages


def build(output: Path, python_base: Path, platform_site: Path, data_site: Path,
          *, source: Path = ROOT, ocr_source: Path | None = None,
          ocr_site: Path | None = None, pdf_runtime: Path | None = None,
          classification_source: Path | None = None,
          classification_site: Path | None = None) -> dict:
    output = output.resolve()
    source, python_base = source.resolve(), python_base.resolve()
    platform_site, data_site = platform_site.resolve(), data_site.resolve()
    if any(value is not None for value in (ocr_source, ocr_site, pdf_runtime)):
        if not all(value is not None for value in (ocr_source, ocr_site, pdf_runtime)):
            raise ValueError("OCR bundle requires source, control site packages and PDF runtime")
        ocr_source, ocr_site, pdf_runtime = (
            ocr_source.resolve(), ocr_site.resolve(), pdf_runtime.resolve())
    if any(value is not None for value in (classification_source, classification_site)):
        if classification_source is None or classification_site is None:
            raise ValueError("Classification bundle requires source and runtime site packages")
        classification_source, classification_site = (
            classification_source.resolve(), classification_site.resolve())
    inputs = (source, python_base, platform_site, data_site)
    if ocr_source is not None:
        inputs += (ocr_source, ocr_site, pdf_runtime)
    if classification_source is not None:
        inputs += (classification_source, classification_site)
    if output.exists() or any(output.is_relative_to(path) or path.is_relative_to(output)
                              for path in inputs):
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
        shutil.copy2(source / "scripts" / "candidate_bootstrap.py", app / "bootstrap.py")
        (app / "config" / "apps").mkdir(parents=True)
        for name in ("ocr.json", "classification.json"):
            shutil.copy2(source / "config" / "apps" / name, app / "config" / "apps" / name)
        (app / "config" / "runtime-locks").mkdir(parents=True)
        shutil.copy2(source / "config/runtime-locks/data.json",
                     app / "config/runtime-locks/data.json")
        _runtime(python_base, platform_site, stage / "runtimes" / "platform")
        _runtime(python_base, data_site, stage / "runtimes" / "data")
        if ocr_source is not None:
            _ocr_bundle(stage, ocr_source, ocr_site, pdf_runtime, python_base)
        if classification_source is not None:
            _classification_bundle(stage, classification_source, classification_site, python_base)
        (stage / "data").mkdir()
        launcher = ("@echo off\r\nsetlocal\r\nset ROOT=%~dp0\r\n"
                    "cd /d \"%ROOT%app\"\r\n"
                    "\"%ROOT%runtimes\\platform\\python.exe\" -I -B "
                    "\"%ROOT%app\\bootstrap.py\"\r\n")
        (stage / "launch.cmd").write_bytes(launcher.encode("ascii"))
        inventory = {name: _licenses(site) for name, site in (
            ("platform", stage / "runtimes/platform/Lib/site-packages"),
            ("data", stage / "runtimes/data/Lib/site-packages"))}
        if ocr_source is not None:
            inventory["ocr_control"] = _licenses(
                stage / "bundles/ocr/runtimes/control/Lib/site-packages")
            inventory["ocr_pdf"] = _licenses(
                stage / "bundles/ocr/runtimes/pdf/Lib/site-packages")
        if classification_source is not None:
            inventory["classification_control"] = _licenses(
                stage / "bundles/classification/runtimes/control/Lib/site-packages")
        (stage / "dependency-inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        files = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file() and not path.is_relative_to(stage / "data"):
                files[path.relative_to(stage).as_posix()] = {"sha256": _hash(path),
                                                                "bytes": path.stat().st_size}
        manifest = {"format": 2, "qualification": "engineering_preview",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "components": ["base_portal", "data_workbench"],
                    "optional_apps": {"ocr": "review_and_pdf_native" if ocr_source else "not_installed",
                                      "classification": "runtime_included" if classification_source else "not_installed"},
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
    if (manifest.get("format") not in {1, 2} or not isinstance(files, dict)
            or manifest.get("qualification") != "engineering_preview"):
        raise ValueError("Unsupported candidate manifest")
    required = {"launch.cmd", "app/apps/portal/dist/index.html",
                "runtimes/platform/python.exe", "runtimes/data/python.exe",
                "dependency-inventory.json"}
    if manifest["format"] >= 2:
        required.add("app/bootstrap.py")
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
    parser.add_argument("--ocr-source", type=Path)
    parser.add_argument("--ocr-site", type=Path)
    parser.add_argument("--pdf-runtime", type=Path)
    parser.add_argument("--classification-source", type=Path)
    parser.add_argument("--classification-site", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        print(json.dumps(verify(args.verify), ensure_ascii=False))
        return
    if not all((args.output, args.python_base, args.platform_site, args.data_site)):
        parser.error("Building requires output and all three runtime inputs")
    print(json.dumps(build(args.output, args.python_base, args.platform_site, args.data_site,
                           ocr_source=args.ocr_source, ocr_site=args.ocr_site,
                           pdf_runtime=args.pdf_runtime,
                           classification_source=args.classification_source,
                           classification_site=args.classification_site),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
