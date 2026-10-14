"""Validate dataset identity, labels and split isolation using only the stdlib."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys

REQUIRED = {"item_id", "relative_path", "content_sha256", "label_id", "group_id", "split", "source_id", "annotation_status"}
SPLITS = {"train", "val", "test", "pool"}


def validate(manifest: Path, project: Path, root: Path | None = None, check_files: bool = False, verify_sha256: bool = False) -> dict:
    config = json.loads(project.read_text(encoding="utf-8-sig"))
    labels = [item["label_id"] for item in config["labels"]]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("Project label IDs must be nonempty and unique.")
    allowed = set(labels)
    errors: list[str] = []
    warnings: list[str] = []
    counts: Counter = Counter()
    class_counts: Counter = Counter()
    ids: set[str] = set()
    paths: set[str] = set()
    groups: dict[str, set[str]] = defaultdict(set)
    content_splits: dict[str, set[str]] = defaultdict(set)
    content_labels: dict[str, set[str]] = defaultdict(set)
    class_groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    base = root.resolve() if root else None
    if (check_files or verify_sha256) and base is None:
        raise ValueError("--root is required for filesystem checks.")
    with manifest.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV header is missing or contains duplicate columns.")
        missing = REQUIRED - set(reader.fieldnames)
        if missing:
            raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
        for line, raw in enumerate(reader, 2):
            if None in raw or any(value is None for value in raw.values()):
                errors.append(f"line {line}: malformed CSV column count")
                continue
            row = {key: value.strip() for key, value in raw.items()}
            item_id, rel, digest = row["item_id"], row["relative_path"], row["content_sha256"].lower()
            label, group, split, status = row["label_id"], row["group_id"], row["split"], row["annotation_status"]
            prefix = f"line {line} ({item_id or 'missing id'}): "
            counts[split] += 1
            if not item_id or item_id in ids:
                errors.append(prefix + "empty or duplicate item_id")
            ids.add(item_id)
            posix, windows = PurePosixPath(rel), PureWindowsPath(rel)
            unsafe = not rel or rel == "." or "\\" in rel or posix.is_absolute() or bool(windows.drive) or ".." in posix.parts or ":" in rel or any(ord(c) < 32 for c in rel)
            canonical_path = str(posix)
            if unsafe:
                errors.append(prefix + "relative_path must be a safe POSIX relative path")
            if canonical_path in paths:
                errors.append(prefix + "duplicate relative_path")
            paths.add(canonical_path)
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                errors.append(prefix + "invalid content_sha256")
            if split not in SPLITS:
                errors.append(prefix + "invalid split")
            if not group or not row["source_id"]:
                errors.append(prefix + "group_id and source_id are required")
            if split == "pool":
                if label or status != "unlabeled":
                    errors.append(prefix + "pool must have empty label_id and unlabeled status")
            elif split in SPLITS:
                if label not in allowed or status != "human_confirmed":
                    errors.append(prefix + "train/val/test require an allowed label and human_confirmed status")
            groups[group].add(split)
            content_splits[digest].add(split)
            if label:
                content_labels[digest].add(label)
                class_counts[(split, label)] += 1
                class_groups[(split, label)].add(group)
            if base and not unsafe and (check_files or verify_sha256):
                try:
                    target = (base / Path(*posix.parts)).resolve()
                    target.relative_to(base)
                    if not target.is_file():
                        errors.append(prefix + "file does not exist")
                    elif verify_sha256:
                        hasher = hashlib.sha256()
                        with target.open("rb") as image_stream:
                            for block in iter(lambda: image_stream.read(1024 * 1024), b""):
                                hasher.update(block)
                        if hasher.hexdigest() != digest:
                            errors.append(prefix + "content hash mismatch")
                except ValueError:
                    errors.append(prefix + "resolved path escapes root")
                except OSError as exc:
                    errors.append(prefix + "file read failed: " + str(exc))
    if not counts:
        errors.append("Manifest has no data rows.")
    for group, splits in groups.items():
        if len(splits) > 1:
            errors.append(f"group {group!r} crosses splits: {sorted(splits)}")
    for digest, splits in content_splits.items():
        if len(splits) > 1:
            errors.append(f"content {digest} crosses splits: {sorted(splits)}")
    for digest, values in content_labels.items():
        if len(values) > 1:
            errors.append(f"content {digest} has conflicting labels: {sorted(values)}")
    for split in ("train", "val", "test"):
        for label in labels:
            count = class_counts[(split, label)]
            if count == 0:
                warnings.append(f"{split}/{label}: no examples; this manifest is not ready for a complete supervised experiment")
            elif count < 10:
                warnings.append(f"{split}/{label}: only {count} examples; insufficient evidence for low error rates")
    warnings.append("No image decoding, perceptual deduplication or source-group discovery was performed.")
    return {
        "valid": not errors, "rows": sum(counts.values()), "split_counts": dict(counts),
        "class_counts": {f"{split}/{label}": count for (split, label), count in sorted(class_counts.items())},
        "independent_groups": {f"{split}/{label}": len(values) for (split, label), values in sorted(class_groups.items())},
        "checked_files": check_files or verify_sha256, "verified_sha256": verify_sha256,
        "errors": errors, "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--verify-sha256", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = validate(args.manifest, args.project, args.root, args.check_files, args.verify_sha256)
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        report = {"valid": False, "errors": [str(exc)]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
