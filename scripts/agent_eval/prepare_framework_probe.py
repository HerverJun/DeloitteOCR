"""Create a hash-locked, offline-installed service runtime copy for B00."""
from __future__ import annotations

import email
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
RUN = "ocr-agent-20260921-langgraph"


def main():
    build = ROOT / "build" / RUN
    audit = ROOT / "audit" / RUN
    source = Path(sys.argv[1]).resolve()
    runtime = build / "langgraph-probe" / "运行时 中文" / "service"
    if runtime.exists() and "--resume-install" not in sys.argv:
        raise SystemExit("Runtime copy already exists; refusing to overlay")
    wheels = []
    requirements = []
    license_root = audit / "framework-licenses"
    for path in sorted((build / "wheelhouse").glob("*.whl")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with zipfile.ZipFile(path) as archive:
            metadata = email.message_from_bytes(archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))))
            name, version = metadata["Name"], metadata["Version"]
            licenses = []
            for entry in archive.namelist():
                if ".dist-info/" in entry and any(word in entry.lower() for word in ("license", "copying", "notice")) and not entry.endswith("/"):
                    target = license_root / name / entry.split(".dist-info/", 1)[1]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(entry))
                    licenses.append(str(target.relative_to(ROOT)).replace("\\", "/"))
            wheels.append({"name": name, "version": version, "file": path.name, "sha256": digest,
                           "license": metadata.get("License-Expression") or metadata.get("License"),
                           "license_files": licenses, "requires_dist": metadata.get_all("Requires-Dist", [])})
            requirements.append(f"{name}=={version} --hash=sha256:{digest}")
    if not wheels:
        raise SystemExit("No wheels")
    lock = audit / "framework-requirements.txt"
    lock.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    if not runtime.exists():
        shutil.copytree(source, runtime)
    command = [sys.executable, "-m", "pip", "--python", str(runtime / "python.exe"), "install", "--no-index",
               "--find-links", str(build / "wheelhouse"), "--require-hashes", "-r", str(lock), "--disable-pip-version-check"]
    installed = subprocess.run(command, capture_output=True, text=True)
    (audit / "framework-offline-install.log").write_text(installed.stdout + installed.stderr, encoding="utf-8")
    installed.check_returncode()
    check = subprocess.run([sys.executable, "-m", "pip", "--python", str(runtime / "python.exe"), "check"], capture_output=True, text=True)
    (audit / "framework-pip-check.log").write_text(check.stdout + check.stderr, encoding="utf-8")
    baseline = subprocess.run([sys.executable, "-m", "pip", "--python", str(source / "python.exe"), "check"], capture_output=True, text=True)
    (audit / "source-runtime-pip-check.log").write_text(baseline.stdout + baseline.stderr, encoding="utf-8")
    added_errors = sorted(set(check.stdout.splitlines()) - set(baseline.stdout.splitlines())) if check.returncode else []
    if added_errors:
        raise RuntimeError(f"New dependency errors: {added_errors}")
    (audit / "framework-lock-candidate.json").write_text(json.dumps({
        "status": "candidate_installed", "runtime": str(runtime), "source_runtime": str(source),
        "offline_install": True, "production_runtime_modified": False,
        "graph_version": "ocr-agent-graph-v1", "wheels": wheels,
        "preexisting_pip_check_errors": baseline.stdout.splitlines() if baseline.returncode else [],
        "new_dependency_errors": added_errors,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(runtime)


if __name__ == "__main__":
    main()
