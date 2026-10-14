"""Verify the fixed candidate with its own Python and source revision binding."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


SOURCE_MAP = (
    ("app/bootstrap.py", "platform", "scripts/candidate_bootstrap.py"),
    ("verify.py", "platform", "scripts/candidate_verify.py"),
    ("app/src/workbench_platform/", "platform", "src/workbench_platform/"),
    ("app/modules/data_workbench/", "platform", "modules/data_workbench/"),
    ("app/packages/contracts/", "platform", "packages/contracts/"),
    ("app/apps/portal/dist/", "platform", "apps/portal/dist/"),
    ("app/config/apps/", "platform", "config/apps/"),
    ("bundles/ocr/src/ocr_workbench/", "ocr", "src/ocr_workbench/"),
    ("bundles/ocr/config/", "ocr", "config/"),
    ("bundles/ocr/web/", "ocr", "frontend/dist/"),
    ("bundles/classification/src/image_platform/platform_weights.py",
     "platform", "scripts/candidate_class_weights.py"),
    ("bundles/classification/src/image_platform/", "classification", "src/image_platform/"),
    ("bundles/classification/schemas/", "classification", "schemas/"),
    ("bundles/classification/apps/web/dist/", "classification", "apps/web/dist/"),
    ("bundles/classification/resources/third-party/", "classification", "resources/third-party/"),
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(run_root):
    index = json.loads((run_root / "portal/fixed-candidate-index.json").read_text(encoding="utf-8"))
    state = json.loads((run_root / "state.json").read_text(encoding="utf-8"))
    root = Path(index["candidate_path"]).resolve(strict=True)
    if digest(root / "candidate-manifest.json") != index["candidate_manifest_sha256"]:
        raise ValueError("Candidate manifest differs from the run index")
    candidate = json.loads((root / "candidate-manifest.json").read_text(encoding="utf-8"))
    verification_timeout = 1800 if candidate.get("optional_apps", {}).get("ocr") == "runtime_and_models" else 360
    verifier = subprocess.run([str(root / "runtimes/platform/python.exe"), "-I", "-B",
                               str(root / "verify.py")], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", timeout=verification_timeout)
    if verifier.returncode:
        raise ValueError("Candidate self-verification failed: " + verifier.stderr[-1000:])
    result = json.loads(verifier.stdout)
    if result["candidate"] != str(root) or result["files"] < 40_000:
        raise ValueError("Candidate program coverage differs")
    checked = 0
    sealed = json.loads((root / "candidate-manifest.json").read_text(encoding="utf-8"))["files"]
    for name, identity in sealed.items():
        mapping = next((item for item in SOURCE_MAP if name.startswith(item[0])), None)
        if mapping is None:
            continue
        prefix, repository, original_prefix = mapping
        source = Path(state["repositories"][repository]) / original_prefix / name[len(prefix):]
        if not source.is_file() or digest(source) != identity["sha256"]:
            raise ValueError(f"Candidate source bytes changed: {name}")
        checked += 1
    if checked < 150:
        raise ValueError("Candidate source coverage is incomplete")
    return {"status": "passed", "candidate_path": str(root),
            "candidate_manifest_sha256": index["candidate_manifest_sha256"],
            "program_files": result["files"], "source_files_checked": checked,
            "source_revisions": index["source_revisions"],
            "checks": ["bundled verifier passed", "program source bytes unchanged",
                       "optional native source bytes unchanged"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = check(args.run_root.resolve())
    target = args.output or args.run_root / "portal/candidate-verification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
