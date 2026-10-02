"""Copy and verify the Git-managed and untracked development files to F:."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path


def digest(path: Path) -> bytes:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.digest()


def selected_paths(root: Path) -> list[Path]:
    output = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    return [Path(item.decode("utf-8")) for item in output.split(b"\0") if item]


def run(mode: str, source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=True)
    if str(source).casefold() != r"c:\users\a\desktop\ocr" or str(destination).casefold() != r"f:\ocr":
        raise RuntimeError("Unexpected source or destination")
    paths = selected_paths(source)
    count = copied = 0
    for relative in paths:
        original = source / relative
        target = destination / relative
        if original.is_symlink() or original.is_junction():
            raise RuntimeError(f"Unexpected tracked reparse point: {original}")
        if not original.is_file():
            continue
        if mode == "copy":
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or original.stat().st_size != target.stat().st_size or digest(original) != digest(target):
                shutil.copy2(original, target)
                copied += 1
        else:
            if not target.is_file() or original.stat().st_size != target.stat().st_size or digest(original) != digest(target):
                raise RuntimeError(f"Missing or mismatched: {relative}")
        count += 1
        if count % 500 == 0:
            print(f"{count}/{len(paths)} development files checked", flush=True)
    print(f"{mode.upper()}: {count} development files; {copied} copied", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("copy", "verify"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        run(args.mode, args.source, args.destination)
    except Exception as exc:
        print(f"SOURCE MIGRATION FAILED: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
