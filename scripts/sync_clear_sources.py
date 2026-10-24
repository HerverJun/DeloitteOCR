"""Maintain the authored Clear source inventory without moving repositories.

Run from F:\\OCR with Python -B. Runtime/model/data directories never enter the
snapshot. Once integrated, this repository becomes the development authority;
the default command refreshes its inventory instead of overwriting it upstream.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "output/clear-automation-dev-20261002/worktrees"
DESTINATION = ROOT / "products/Clear"
INTEGRATED_FORMAT = "clear-integrated-source-v1"
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
    if relative.as_posix() == 'agent_harness/runtime-local.json' or relative.parts[:2] == ('agent_harness', 'node'):
        return False
    if relative.parts[0].lower() in EXCLUDED_ROOTS:
        return False
    if any(part.lower() in EXCLUDED_PARTS for part in relative.parts):
        return False
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if relative.name.startswith(".env") and relative.name != ".env.example":
        return False
    return not relative.name.endswith(("-wal", "-shm"))


def authored_paths(directory: Path):
    """Prune local dependencies before traversing their contents or links."""
    for current, directories, filenames in os.walk(directory, followlinks=False):
        parent = Path(current)
        directories[:] = sorted(
            name for name in directories
            if included((parent / name).relative_to(directory))
            and not (parent / name).is_symlink()
            and not (parent / name).is_junction()
        )
        for name in sorted(filenames):
            path = parent / name
            relative = path.relative_to(directory)
            if included(relative):
                actual = local_f(path)
                if not actual.is_relative_to(directory.resolve()):
                    raise ValueError(f"Authored source escapes its directory: {path}")
                yield relative, actual


def refresh_integrated() -> dict:
    """Record current main-branch bytes, retaining all historical import origins."""
    local_f(ROOT)
    saved = DESTINATION / "source-manifest.json"
    previous = json.loads(saved.read_text(encoding="utf-8"))
    originals = {item["name"]: item for item in previous["sources"]}
    manifest = {
        "format": INTEGRATED_FORMAT,
        "captured_on": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        "release_pointer": "release/Clear/current.json",
        "purpose": "Integrated authored sources maintained on the single project branch; runtime assets and deployed releases are separate.",
        "source_authority": {
            "repository": local_f(ROOT).as_posix(),
            "branch": git(ROOT, "branch", "--show-current").strip(),
            "integration_base_head": previous.get("source_authority", {}).get(
                "integration_base_head", git(ROOT, "rev-parse", "HEAD").strip()),
            "path": "products/Clear",
        },
        "exclusions": previous["exclusions"],
        "sources": [],
    }
    for name in ("platform", "classification", "ocr"):
        directory = local_f(DESTINATION / name)
        original = originals[name]
        origins = original.get("upstream_sources")
        if origins is None:
            origins = [{key: original[key] for key in
                        ("source_path", "source_git_dir", "head", "branch", "working_tree_status")
                        if key in original}]
        files = []
        for relative, actual in authored_paths(directory):
            content = actual.read_bytes()
            if len(content) > 50 * 1024 * 1024:
                raise ValueError(f"Unexpected large authored file: {actual}")
            files.append({"path": f"{name}/{relative.as_posix()}",
                          "source_path": relative.as_posix(), "bytes": len(content),
                          "sha256": hashlib.sha256(content).hexdigest()})
        manifest["sources"].append({
            "name": name, "source_kind": "integrated-main-branch",
            "source_path": directory.as_posix(),
            "source_git_dir": local_f(Path(git(ROOT, "rev-parse", "--absolute-git-dir").strip())).as_posix(),
            "branch": manifest["source_authority"]["branch"],
            "upstream_sources": origins,
            "file_count": len(files), "files": files,
        })
    destination_path(saved).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def capture(platform_source: Path | None = None) -> dict:
    local_f(ROOT)
    saved = DESTINATION / "source-manifest.json"
    if saved.exists() and json.loads(saved.read_text(encoding="utf-8")).get("format") == INTEGRATED_FORMAT:
        if platform_source is not None:
            raise ValueError("Clear sources are integrated on the main project branch; upstream capture would overwrite merged work. Import selected changes, then refresh the manifest.")
        return refresh_integrated()
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
        actual_paths = {f"{source['name']}/{relative.as_posix()}"
                        for relative, _ in authored_paths(DESTINATION / source["name"])}
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
    parser.add_argument("--refresh-manifest", action="store_true",
                        help="Maintain the integrated sources on this project branch; retain upstream origins")
    parser.add_argument("--platform-source", type=Path,
                        help="Explicit local F-drive platform worktree; other sources keep their registered locations")
    args = parser.parse_args()
    if (args.verify or args.refresh_manifest) and args.platform_source is not None:
        parser.error("--platform-source applies to legacy capture only")
    if args.verify and args.refresh_manifest:
        parser.error("Choose either verification or inventory refresh")
    if args.verify:
        print(json.dumps(verify()))
    else:
        result = refresh_integrated() if args.refresh_manifest else capture(args.platform_source)
        print(json.dumps({"sources": [{"name": item["name"], "files": item["file_count"], "excluded": item.get("excluded_count", 0)} for item in result["sources"]]}))
