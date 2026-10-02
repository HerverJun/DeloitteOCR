"""Copy an OCR experiment tree to another NTFS volume, preserving hard links.

This intentionally never deletes or modifies the source tree. Run ``verify``
after ``copy`` before considering source removal.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import time
from pathlib import Path


def files(root: Path):
    for directory, dirs, names in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in dirs + names:
            item = current / name
            if item.is_symlink() or item.is_junction():
                raise RuntimeError(f"Refusing reparse point: {item}")
        for name in names:
            item = current / name
            mode = item.stat().st_mode
            if not stat.S_ISREG(mode):
                raise RuntimeError(f"Refusing non-regular file: {item}")
            yield item.relative_to(root), item


def digest(path: Path) -> bytes:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.digest()


def run(source: Path, destination: Path, verify: bool) -> None:
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if source.drive.upper() != "E:" or destination.drive.upper() != "F:":
        raise RuntimeError("Expected an E: source and F: destination")
    if source.name != destination.name or source.name != "DeloitteOCR-Agent-Experimental-20260921":
        raise RuntimeError("Unexpected source or destination folder")
    if not verify and destination.exists():
        raise RuntimeError(f"Destination already exists: {destination}")
    if verify and not destination.is_dir():
        raise RuntimeError(f"Destination is missing: {destination}")
    if not verify:
        destination.mkdir()

    linked: dict[tuple[int, int], Path] = {}
    count = unique = total = 0
    start = time.monotonic()
    for relative, original in files(source):
        target = destination / relative
        info = original.stat()
        key = (info.st_dev, info.st_ino)
        if verify:
            if not target.is_file() or target.is_symlink() or target.is_junction():
                raise RuntimeError(f"Missing or invalid destination: {target}")
            other = target.stat()
            if other.st_size != info.st_size:
                raise RuntimeError(f"Size mismatch: {target}")
            if key in linked:
                if other.st_ino != linked[key].stat().st_ino:
                    raise RuntimeError(f"Hard-link identity mismatch: {target}")
            else:
                if digest(original) != digest(target):
                    raise RuntimeError(f"SHA-256 mismatch: {target}")
                unique += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if key in linked:
                os.link(linked[key], target)
            else:
                shutil.copy2(original, target)
                unique += 1
        linked.setdefault(key, target)
        count += 1
        total += info.st_size
        if count % 10000 == 0:
            print(f"{count} files, {unique} unique, {total / 2**30:.2f} logical GiB, {time.monotonic() - start:.0f}s", flush=True)

    if verify:
        expected = {str(path) for path, _ in files(source)}
        actual = {str(path) for path, _ in files(destination)}
        extra = actual - expected
        if extra:
            raise RuntimeError(f"Unexpected destination files: {sorted(extra)[:5]}")
    print(f"{'VERIFIED' if verify else 'COPIED'}: {count} files, {unique} unique, {total / 2**30:.2f} logical GiB", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("copy", "verify"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        run(args.source, args.destination, args.mode == "verify")
    except Exception as exc:
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
