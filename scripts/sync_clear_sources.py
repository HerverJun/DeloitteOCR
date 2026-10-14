"""Capture authored Clear working-tree sources without moving live repositories.

Run from F:\\OCR with Python -B. Runtime/model/data directories never enter the
snapshot. The source repositories remain the development authority.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "output/clear-automation-dev-20261002/worktrees"
DESTINATION = ROOT / "products/Clear"
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".hypothesis", ".cache", "cache", "build", "dist",
    "temp", "runtimes", "wheelhouse", "dependencies",
    "weights", "checkpoints", "workspace", ".local-data",
    ".test-data", ".e2e-data", ".benchmark-data", "playwright-report", "test-results",
}
EXCLUDED_ROOTS = {"output", "audit", "research", "models", "data", "workspaces"}
EXCLUDED_SUFFIXES = {
    ".pyc", ".pyo", ".tsbuildinfo", ".sqlite", ".sqlite3", ".db", ".log",
    ".bin", ".safetensors", ".onnx", ".pt", ".pth", ".gguf", ".ggml",
    ".pdparams", ".pdiparams", ".pdmodel", ".whl", ".zip", ".7z", ".exe", ".dll",
}


def local_f(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if resolved.drive.lower() != "f:" or str(resolved).startswith("\\\\"):
        raise ValueError(f"Not a local F-drive path: {path}")
    return resolved


def destination_path(path: Path) -> Path:
    """Check existing ancestors and the prospective target before any write."""
    absolute = path.absolute()
    resolved = absolute.resolve(strict=False)
    if resolved.drive.lower() != "f:" or not resolved.is_relative_to(ROOT):
        raise ValueError(f"Destination escapes the local F-drive project: {path}")
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError(f"Destination uses a redirected path: {ancestor}")
    return absolute


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={source.as_posix()}", "-C", str(source), *args],
        encoding="utf-8", errors="strict",
    )


def included(relative: Path) -> bool:
    if relative.parts[0].lower() in EXCLUDED_ROOTS:
        return False
    if any(part.lower() in EXCLUDED_PARTS for part in relative.parts):
        return False
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if relative.name.startswith(".env") and relative.name != ".env.example":
        return False
    return not relative.name.endswith(("-wal", "-shm"))


def capture(platform_source: Path | None = None) -> dict:
    local_f(ROOT)
    destination_path(DESTINATION).mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": "clear-authored-source-snapshot-v1",
        "captured_on": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        "release_pointer": "release/Clear/current.json",
        "purpose": "Reviewable authored working-tree sources; not an offline installation package.",
        "exclusions": {"root_directories": sorted(EXCLUDED_ROOTS), "path_parts": sorted(EXCLUDED_PARTS), "suffixes": sorted(EXCLUDED_SUFFIXES)},
        "sources": [],
    }
    for name in ("platform", "classification", "ocr"):
        source = local_f(platform_source if name == "platform" and platform_source is not None else SOURCE_ROOT / name)
        metadata = Path(git(source, "rev-parse", "--absolute-git-dir").strip())
        local_f(metadata)
        paths = sorted(set(git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")) - {""})
        files = []
        excluded_count = 0
        for value in paths:
            relative = Path(value)
            if not included(relative):
                excluded_count += 1
                continue
            original = source / relative
            if not original.is_file():
                # A tracked deletion is represented by omission, not by stale bytes.
                continue
            actual = local_f(original)
            if not actual.is_relative_to(source):
                raise ValueError(f"Source escapes its repository: {original}")
            saved_relative = relative.with_name(".gitattributes.source") if relative.name == ".gitattributes" else relative
            target = destination_path(DESTINATION / name / saved_relative)
            destination_path(target.parent).mkdir(parents=True, exist_ok=True)
            parent = local_f(target.parent)
            if not parent.is_relative_to(DESTINATION.resolve()):
                raise ValueError(f"Destination escapes snapshot: {target}")
            if target.exists():
                if target.is_symlink() or local_f(target) != target.absolute():
                    raise ValueError(f"Destination is redirected: {target}")
            content = actual.read_bytes()
            if len(content) > 50 * 1024 * 1024:
                raise ValueError(f"Unexpected large authored file: {original}")
            target.write_bytes(content)
            files.append({"path": f"{name}/{saved_relative.as_posix()}", "source_path": relative.as_posix(), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        manifest["sources"].append({
            "name": name, "source_path": source.as_posix(),
            "source_git_dir": metadata.as_posix(),
            "head": git(source, "rev-parse", "HEAD").strip(),
            "branch": git(source, "branch", "--show-current").strip(),
            "working_tree_status": git(source, "-c", "core.quotepath=false", "status", "--short").splitlines(),
            "excluded_count": excluded_count, "file_count": len(files), "files": files,
        })
    destination_path(DESTINATION / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify() -> dict:
    manifest = json.loads((DESTINATION / "source-manifest.json").read_text(encoding="utf-8"))
    checked = 0
    for source in manifest["sources"]:
        expected_paths = {file["path"] for file in source["files"]}
        actual_paths = {
            path.relative_to(DESTINATION).as_posix()
            for path in (DESTINATION / source["name"]).rglob("*")
            if path.is_file() and included(path.relative_to(DESTINATION / source["name"]))
        }
        if actual_paths != expected_paths:
            raise ValueError(f"Snapshot file inventory mismatch: {source['name']}")
        for file in source["files"]:
            target = local_f(DESTINATION / file["path"])
            if not target.is_relative_to(DESTINATION.resolve()):
                raise ValueError(f"Snapshot path escapes root: {target}")
            content = target.read_bytes()
            if len(content) != file["bytes"] or hashlib.sha256(content).hexdigest() != file["sha256"]:
                raise ValueError(f"Snapshot hash mismatch: {file['path']}")
            checked += 1
    return {"status": "passed", "verified_files": checked}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Only verify saved snapshot hashes")
    parser.add_argument("--platform-source", type=Path,
                        help="Explicit local F-drive platform worktree; other sources keep their registered locations")
    args = parser.parse_args()
    if args.verify and args.platform_source is not None:
        parser.error("--platform-source applies to capture only")
    if args.verify:
        print(json.dumps(verify()))
    else:
        result = capture(args.platform_source)
        print(json.dumps({"sources": [{"name": item["name"], "files": item["file_count"], "excluded": item["excluded_count"]} for item in result["sources"]]}))
