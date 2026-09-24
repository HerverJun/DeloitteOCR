"""Build and independently verify a candidate layer over a complete base ZIP.

The layer is not a standalone candidate. Keep both ZIPs; never extract untrusted
members without a separate safe extraction implementation. All file I/O here is
streamed, including SHA-256 and the ZIP reader's CRC check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import zipfile


PREFIX = "OfflineOCR/"
MANIFEST = PREFIX + "manifest.json"
METADATA = "delta-metadata.json"
CANDIDATE07_SHA256 = "9754d2fbb861b88d66e443721d89f5f31ead9ee3dd15594ec45400360a1ed648"
CHUNK = 4 * 1024 * 1024
MAX_FILES = 200_000
MAX_TOTAL_BYTES = 1024 ** 4  # Safety ceiling, not a promise of available disk space.
MAX_CONTROL_BYTES = 64 * 1024 * 1024


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256_stream(stream)


def sha256_stream(stream) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(CHUNK):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def same_open_file(stream, path: Path) -> None:
    opened, named = os.fstat(stream.fileno()), path.stat(follow_symlinks=False)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns):
        raise ValueError(f"Archive changed or path replaced during verification: {path}")
    ordinary_path(path, file=True)


def checked_name(name: str) -> str:
    if (not isinstance(name, str) or not name or name.startswith("/") or "\\" in name
            or ":" in name or "\x00" in name or any(
                part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError(f"Unsafe path: {name!r}")
    return name


def unique_paths(names: list[str]) -> None:
    folded = set()
    for name in names:
        checked_name(name)
        key = name.casefold()
        if key in folded:
            raise ValueError(f"Duplicate/case-colliding path: {name}")
        folded.add(key)
    # A file must not also serve as a directory, even with different casing.
    if any("/".join(name.split("/")[:n]).casefold() in folded
           for name in names for n in range(1, len(name.split("/")))):
        raise ValueError("File/directory path collision")


def parse_manifest(data: bytes) -> dict[str, dict]:
    if len(data) > MAX_CONTROL_BYTES:
        raise ValueError("Candidate manifest too large")
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise ValueError("Invalid candidate manifest")
    records = value["files"]
    if len(records) > MAX_FILES:
        raise ValueError("Too many candidate files")
    names = []
    total = 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid manifest record")
        name, size, digest = record.get("path"), record.get("bytes"), record.get("sha256")
        checked_name(name)
        if name == "manifest.json" or type(size) is not int or size < 0 or not isinstance(digest, str) \
                or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError(f"Invalid manifest record: {name}")
        names.append(name)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Candidate manifest exceeds total byte ceiling")
    unique_paths(names + ["manifest.json"])
    return {record["path"]: record for record in records}


def ordinary(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Non-ordinary path: {path}")
    if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError(f"Reparse point: {path}")


def ordinary_path(path: Path, file: bool) -> None:
    for parent in reversed(path.parents):
        ordinary(parent)
        if not parent.is_dir():
            raise ValueError(f"Non-directory ancestor: {parent}")
    ordinary(path)
    if file and not path.is_file() or not file and not path.is_dir():
        raise ValueError(f"Unexpected path type: {path}")


def candidate_file(root: Path, name: str) -> Path:
    path = root
    for part in name.split("/"):
        path = path / part
        ordinary(path)
    if not path.is_file():
        raise ValueError(f"Expected ordinary file: {name}")
    return path


def candidate_inventory(root: Path, records: dict[str, dict]) -> None:
    ordinary_path(root, file=False)
    found = []
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            ordinary(path)
            if name in dirs and not path.is_dir() or name in files and not path.is_file():
                raise ValueError(f"Unexpected directory entry: {path}")
            if name in files:
                found.append(path.relative_to(root).as_posix())
    unique_paths(found)
    if set(found) != set(records) | {"manifest.json"}:
        raise ValueError("Candidate file inventory differs from manifest")


def zip_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_FILES + 2:
        raise ValueError("Too many ZIP entries")
    names = [entry.filename for entry in entries]
    unique_paths(names)
    for entry in entries:
        name = entry.filename
        mode = entry.external_attr >> 16
        if name.endswith("/") or entry.is_dir() or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG)) \
                or entry.external_attr & (0x400 << 16) or entry.external_attr & 0x400:
            raise ValueError(f"Non-ordinary ZIP entry: {name}")
        if entry.flag_bits & 1:
            raise ValueError(f"Encrypted ZIP entry: {name}")
    return {entry.filename: entry for entry in entries}


def read_member(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, expected: dict | None = None) -> tuple[int, str, bytes | None]:
    digest = hashlib.sha256()
    size = 0
    captured = bytearray() if expected is None else None
    if captured is not None and entry.file_size > MAX_CONTROL_BYTES:
        raise ValueError(f"Control entry too large: {entry.filename}")
    if expected is not None and entry.file_size != expected["bytes"]:
        raise ValueError(f"ZIP member size differs from manifest: {entry.filename}")
    with archive.open(entry) as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            if size > (MAX_CONTROL_BYTES if expected is None else expected["bytes"]):
                raise ValueError(f"ZIP member exceeds declared size: {entry.filename}")
            if captured is not None:
                captured.extend(chunk)
    result = digest.hexdigest()
    if size != entry.file_size or expected is not None and (size != expected["bytes"] or result != expected["sha256"]):
        raise ValueError(f"ZIP member differs from manifest: {entry.filename}")
    return size, result, bytes(captured) if captured is not None else None


def verify_zip(archive: zipfile.ZipFile, expected_names: set[str], records: dict[str, dict],
               control: str, label: str) -> dict:
    entries = zip_entries(archive)
    if set(entries) != expected_names:
        raise ValueError(f"{label} ZIP inventory differs from its manifest/delta")
    count = total = 0
    last = time.monotonic()
    for name, record in records.items():
        size, _, _ = read_member(archive, entries[PREFIX + name], record)
        count += 1
        total += size
        if time.monotonic() - last > 20:
            print(json.dumps({"verifying": label, "files": count, "bytes": total}), flush=True)
            last = time.monotonic()
    _, control_hash, control_bytes = read_member(archive, entries[control])
    return {"files": count, "bytes": total, "manifest_sha256": control_hash,
            "manifest_bytes": control_bytes}


def inspect_base(path: Path, expected_sha256: str) -> tuple[dict[str, dict], dict]:
    ordinary_path(path, file=True)
    with path.open("rb") as stream:
        same_open_file(stream, path)
        actual = sha256_stream(stream)
        if actual != expected_sha256:
            raise ValueError(f"Base ZIP SHA-256 mismatch: {actual}")
        with zipfile.ZipFile(stream) as archive:
            entries = zip_entries(archive)
            if MANIFEST not in entries:
                raise ValueError("Base manifest is missing")
            _, _, manifest = read_member(archive, entries[MANIFEST])
            records = parse_manifest(manifest)
            report = verify_zip(archive, {MANIFEST} | {PREFIX + name for name in records}, records,
                                MANIFEST, "base")
        if sha256_stream(stream) != expected_sha256:
            raise ValueError("Base ZIP changed during member verification")
        same_open_file(stream, path)
    del report["manifest_bytes"]
    return records, report


def inspect_candidate(root: Path) -> tuple[dict[str, dict], bytes]:
    ordinary_path(root, file=False)
    manifest_path = candidate_file(root, "manifest.json")
    if manifest_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Candidate manifest too large")
    manifest = manifest_path.read_bytes()
    records = parse_manifest(manifest)
    candidate_inventory(root, records)
    last = time.monotonic()
    for index, (name, record) in enumerate(records.items(), 1):
        path = candidate_file(root, name)
        if path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise ValueError(f"Candidate differs from manifest: {name}")
        if time.monotonic() - last > 20:
            print(json.dumps({"verifying": "candidate", "files": index, "total": len(records)}), flush=True)
            last = time.monotonic()
    if candidate_file(root, "manifest.json").read_bytes() != manifest:
        raise ValueError("Candidate manifest changed during verification")
    return records, manifest


def metadata_for(base: dict[str, dict], target: dict[str, dict], base_sha: str,
                 base_manifest_sha: str, target_manifest: bytes) -> dict:
    changed = sorted(name for name, record in target.items() if name not in base or
                     (record["bytes"], record["sha256"]) != (base[name]["bytes"], base[name]["sha256"]))
    deleted = sorted(set(base) - set(target))
    return {"format": "offlineocr-candidate-delta-v1", "base_archive_sha256": base_sha,
            "base_manifest_sha256": base_manifest_sha,
            "target_manifest_sha256": hashlib.sha256(target_manifest).hexdigest(),
            "changed": changed, "deleted": deleted}


def verify_layer(base_path: Path, layer_path: Path, expected_base_sha256: str) -> dict:
    base, base_report = inspect_base(base_path, expected_base_sha256)
    ordinary_path(layer_path, file=True)
    with layer_path.open("rb") as stream:
        same_open_file(stream, layer_path)
        before_sha = sha256_stream(stream)
        with zipfile.ZipFile(stream) as archive:
            entries = zip_entries(archive)
            if MANIFEST not in entries or METADATA not in entries:
                raise ValueError("Layer control entries are missing")
            _, _, manifest = read_member(archive, entries[MANIFEST])
            _, _, raw_metadata = read_member(archive, entries[METADATA])
            target = parse_manifest(manifest)
            metadata = json.loads(raw_metadata)
            expected = metadata_for(base, target, expected_base_sha256, base_report["manifest_sha256"], manifest)
            if metadata != expected:
                raise ValueError("Layer metadata/combined candidate identity mismatch")
            changed = {name: target[name] for name in expected["changed"]}
            report = verify_zip(archive, {MANIFEST, METADATA} | {PREFIX + name for name in changed},
                                changed, MANIFEST, "layer")
        after_sha = sha256_stream(stream)
        same_open_file(stream, layer_path)
        if before_sha != after_sha:
            raise ValueError("Layer ZIP changed during member verification")
    if report["manifest_sha256"] != expected["target_manifest_sha256"]:
        raise ValueError("Target manifest changed during verification")
    # Unchanged identities are inherited only from the fully verified base ZIP.
    if any((base[name]["bytes"], base[name]["sha256"]) != (record["bytes"], record["sha256"])
           for name, record in target.items() if name not in changed):
        raise ValueError("Combined identity mismatch")
    del report["manifest_bytes"]
    return {"status": "pass", "base_archive": str(base_path), "base_archive_sha256": expected_base_sha256,
            "layer_archive": str(layer_path), "layer_archive_sha256": after_sha,
            "base": base_report, "layer": report, "target_manifest_sha256": expected["target_manifest_sha256"],
            "combined_files": len(target), "combined_bytes": sum(r["bytes"] for r in target.values()),
            "inherited_files": len(target) - len(changed), "changed_files": len(changed),
            "deleted_files": len(expected["deleted"]),
            "method": "Each base and layer member streamed through ZIP CRC and SHA-256; exact inventory, base ZIP identity and full target manifest identity checked",
            "limit": "Logical two-piece identity, not an independently runnable full ZIP or target-machine qualification"}


def write_json(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def space_preflight(destination: Path, changed: dict[str, dict], manifest: bytes, metadata: bytes) -> None:
    # ZIP_STORED needs the source length; DEFLATE can expand slightly.
    required = sum(record["bytes"] + record["bytes"] // 100 + 1024 for record in changed.values())
    required += len(manifest) + len(metadata) + 1024 * (len(changed) + 2) + 16 * 1024 * 1024
    if shutil.disk_usage(destination.parent).free < required + 256 * 1024 * 1024:
        raise OSError(f"Insufficient space for bounded delta ZIP plus 256 MiB reserve: {required} bytes")


def cleanup_created(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (info.st_dev, info.st_ino) == identity and stat.S_ISREG(info.st_mode):
        path.unlink()


def build(base_path: Path, root: Path, layer_path: Path, receipt_path: Path,
          base_sha: str) -> dict:
    partial = layer_path.with_name(layer_path.name + ".partial")
    receipt_partial = receipt_path.with_name(receipt_path.name + ".partial")
    if any(path.exists() or path.is_symlink() for path in (layer_path, partial, receipt_path, receipt_partial)):
        raise ValueError("Output, partial archive and receipt paths must all be new")
    if len({p.resolve() for p in (base_path, root, layer_path, partial, receipt_path, receipt_partial)}) != 6:
        raise ValueError("Input and output paths must differ")
    if any(path.resolve().is_relative_to(root.resolve()) for path in (layer_path, partial, receipt_path, receipt_partial)):
        raise ValueError("Outputs must be outside candidate directory")
    ordinary_path(layer_path.parent, file=False)
    ordinary_path(receipt_path.parent, file=False)
    base, base_report = inspect_base(base_path, base_sha)
    target, manifest = inspect_candidate(root)
    metadata = metadata_for(base, target, base_sha, base_report["manifest_sha256"], manifest)
    raw_metadata = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
    changed = {name: target[name] for name in metadata["changed"]}
    space_preflight(layer_path, changed, manifest, raw_metadata)
    owned: dict[Path, tuple[int, int] | None] = {path: None for path in
                                                  (partial, receipt_partial, layer_path, receipt_path)}
    try:
        # Exclusive creation; a failed build retains neither a partial nor a final file.
        with zipfile.ZipFile(partial, "x", allowZip64=True) as archive:
            info = partial.stat()
            owned[partial] = (info.st_dev, info.st_ino)
            archive.writestr(MANIFEST, manifest, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr(METADATA, raw_metadata, compress_type=zipfile.ZIP_DEFLATED)
            for name in metadata["changed"]:
                source = candidate_file(root, name)
                entry = zipfile.ZipInfo.from_file(source, PREFIX + name)
                entry.compress_type = zipfile.ZIP_STORED if target[name]["bytes"] >= 16 * 1024 * 1024 else zipfile.ZIP_DEFLATED
                entry._compresslevel = 1
                digest = hashlib.sha256()
                size = 0
                with source.open("rb") as stream, archive.open(entry, "w", force_zip64=True) as destination:
                    while chunk := stream.read(CHUNK):
                        size += len(chunk)
                        digest.update(chunk)
                        destination.write(chunk)
                if size != target[name]["bytes"] or digest.hexdigest() != target[name]["sha256"]:
                    raise ValueError(f"Candidate changed during layer writing: {name}")
                candidate_file(root, name)
        if candidate_file(root, "manifest.json").read_bytes() != manifest:
            raise ValueError("Candidate manifest changed during layer writing")
        with partial.open("rb") as stream:
            same_open_file(stream, partial)
            layer_sha = sha256_stream(stream)
            with zipfile.ZipFile(stream) as archive:
                entries = zip_entries(archive)
                layer_report = verify_zip(archive, {MANIFEST, METADATA} | {PREFIX + name for name in changed},
                                          changed, MANIFEST, "layer")
                _, _, verified_metadata = read_member(archive, entries[METADATA])
                if verified_metadata != raw_metadata or layer_report["manifest_bytes"] != manifest:
                    raise ValueError("Layer control entries changed")
            if sha256_stream(stream) != layer_sha:
                raise ValueError("Layer changed during member verification")
            same_open_file(stream, partial)
        receipt = {"status": "pass", "base_archive": str(base_path), "base_archive_sha256": base_sha,
                   "layer_archive": str(layer_path), "layer_archive_sha256": layer_sha,
                   "base": base_report, "target_manifest_sha256": metadata["target_manifest_sha256"],
                   "combined_files": len(target), "combined_bytes": sum(r["bytes"] for r in target.values()),
                   "changed_files": len(changed), "deleted_files": len(metadata["deleted"]),
                   "base_and_candidate_fully_hashed": True, "layer_members_crc_and_sha256_verified": True,
                   "limit": "Requires the complete base ZIP and this layer; no standalone full target ZIP or target-machine qualification"}
        with receipt_partial.open("x", encoding="utf-8") as stream:
            info = receipt_partial.stat()
            owned[receipt_partial] = (info.st_dev, info.st_ino)
            json.dump(receipt, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # A hard-link publishes a new name exclusively without replacing another user's file.
        os.link(partial, layer_path)
        owned[layer_path] = owned[partial]
        if sha256_file(layer_path) != layer_sha:
            raise ValueError("Published layer differs from verified layer")
        os.link(receipt_partial, receipt_path)
        owned[receipt_path] = owned[receipt_partial]
        cleanup_created(partial, owned[partial])
        cleanup_created(receipt_partial, owned[receipt_partial])
        return receipt
    except BaseException:
        for path in (receipt_path, layer_path, receipt_partial, partial):
            cleanup_created(path, owned[path])
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "verify"):
        p = sub.add_parser(command)
        p.add_argument("--base-archive", required=True, type=Path)
        p.add_argument("--layer-archive", required=True, type=Path)
        p.add_argument("--expected-base-sha256", default=CANDIDATE07_SHA256)
        p.add_argument("--receipt", required=True, type=Path)
        if command == "build":
            p.add_argument("--candidate", "--candidate08", dest="candidate", required=True, type=Path,
                           help="Frozen target candidate directory; --candidate08 remains an alias for old commands")
    args = parser.parse_args()
    if len(args.expected_base_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in args.expected_base_sha256):
        parser.error("Expected base SHA-256 must be lowercase hexadecimal")
    if args.command == "build":
        report = build(args.base_archive, args.candidate, args.layer_archive, args.receipt,
                       args.expected_base_sha256)
    else:
        if args.receipt.exists() or args.receipt.resolve() in (args.base_archive.resolve(), args.layer_archive.resolve()):
            raise ValueError("Receipt must be a new separate path")
        report = verify_layer(args.base_archive, args.layer_archive, args.expected_base_sha256)
        write_json(args.receipt, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
