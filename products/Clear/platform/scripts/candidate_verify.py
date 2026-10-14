"""Verify every sealed candidate program file with the bundled Python runtime."""

import hashlib
import json
from pathlib import Path


def verify(root: Path) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "candidate-manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files")
    if (manifest.get("format") not in {2, 3} or not isinstance(files, dict)
            or manifest.get("qualification") != "engineering_preview"):
        raise ValueError("Unsupported candidate manifest")
    required = {"launch.cmd", "app/bootstrap.py", "app/apps/portal/dist/index.html",
                "runtimes/platform/python.exe", "runtimes/data/python.exe",
                "dependency-inventory.json"}
    if manifest["format"] >= 3:
        required.update({"verify.cmd", "verify.py"})
    if 'image_forensics' in manifest.get('components', []):
        required.update({'app/config/forensics.json', 'app/modules/image_forensics/worker.py',
                         'app/modules/image_forensics/source-manifest.json', 'runtimes/forensics/python.exe'})
    if not required <= files.keys():
        raise ValueError("Candidate is missing a required component")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Linked candidate member: {path}")
        if path.is_file() and not path.is_relative_to(root / "data"):
            relative = path.relative_to(root).as_posix()
            if relative != "candidate-manifest.json":
                actual.add(relative)
    if actual != files.keys():
        raise ValueError("Candidate file set differs from its manifest")
    for name, identity in files.items():
        member = Path(name)
        if (member.is_absolute() or member.drive or member.as_posix() != name
                or any(part in {".", ".."} for part in member.parts)):
            raise ValueError(f"Unsafe candidate member: {name}")
        target = root / member
        if not target.is_file() or target.stat().st_size != identity["bytes"]:
            raise ValueError(f"Candidate member changed: {name}")
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != identity["sha256"]:
            raise ValueError(f"Candidate member changed: {name}")
    return {"candidate": str(root), "files": len(files)}


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parent), ensure_ascii=False))
