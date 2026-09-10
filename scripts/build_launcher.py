"""Build onedir with a sanitized DLL search path; do not vendor Windows ICU/UCRT."""

import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess

p = argparse.ArgumentParser()
p.add_argument(
    "--python",
    type=Path,
    required=True,
    help="Build venv with PySide6-Essentials and PyInstaller",
)
p.add_argument("--work", type=Path, required=True)
p.add_argument("--bundle", type=Path, required=True)
p.add_argument("--msvc-redist", type=Path, required=True)
a = p.parse_args()
source = Path(__file__).resolve().parents[1]
work = a.work.resolve()
work.mkdir(parents=True, exist_ok=True)
build_source = work / "source"
shutil.copytree(
    source / "src",
    build_source,
    dirs_exist_ok=True,
    ignore=shutil.ignore_patterns("__pycache__"),
)
environment = os.environ.copy()
environment["PATH"] = (
    str(a.python.resolve().parent)
    + os.pathsep
    + str(Path(os.environ["SystemRoot"]) / "System32")
    + os.pathsep
    + os.environ["SystemRoot"]
)
for name in ["PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"]:
    environment.pop(name, None)
command = [
    str(a.python.resolve()),
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name",
    "OfflineOCRLauncher",
    "--paths",
    str(build_source),
    "--distpath",
    str(work / "dist"),
    "--workpath",
    str(work / "build"),
    "--specpath",
    str(work),
    str(build_source / "ocr_workbench/launcher.py"),
]
with (work / "build.log").open("w", encoding="utf-8") as log:
    subprocess.run(
        command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True
    )
built = work / "dist/OfflineOCRLauncher"
for path in built.rglob("*.dll"):
    if path.name.lower() in {
        "icuuc.dll",
        "ucrtbase.dll",
    } or path.name.lower().startswith("api-ms-win-"):
        raise RuntimeError(
            f"Unexpected copied Windows system DLL: {path}; check build PATH"
        )
for relative in ["_internal", "_internal/PySide6", "_internal/shiboken6"]:
    for dll in a.msvc_redist.glob("*.dll"):
        shutil.copy2(dll, built / relative / dll.name)
target = a.bundle.resolve() / "launcher"
if target.exists():
    if (
        target.parent != a.bundle.resolve()
        or target.is_symlink()
        or target.is_junction()
    ):
        raise ValueError("Unsafe launcher replacement")
    shutil.rmtree(target)
shutil.copytree(built, target)
info = subprocess.check_output(
    [
        str(a.python),
        "-c",
        'import sys,importlib.metadata as m,json;print(json.dumps({"python":sys.version,"packages":{x:m.version(x) for x in ["PySide6-Essentials","shiboken6","pyinstaller"]}}))',
    ],
    env=environment,
    text=True,
)
(a.bundle / "config/launcher-build.json").write_text(info, encoding="utf-8")
(a.bundle / "启动工作台.cmd").write_text(
    '@echo off\r\nstart "" "%~dp0launcher\\OfflineOCRLauncher.exe"\r\n',
    encoding="ascii",
)
print(
    json.dumps(
        {
            "launcher": str(target / "OfflineOCRLauncher.exe"),
            "build_log": str(work / "build.log"),
        }
    )
)
