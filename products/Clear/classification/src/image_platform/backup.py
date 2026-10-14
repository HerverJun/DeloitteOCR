"""Cold, verified backups and non-destructive relocation of a local workspace.

Stop the API and worker before making a backup. SQLite's backup API captures WAL
transactions; stopping writers also keeps referenced model/image files consistent.
"""

import argparse
from contextlib import closing, contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid


EXCLUDED_DIRS = {"cache", "features", "thumbnails"}  # Regenerated as needed.
EXCLUDED_FILES = {"platform.sqlite3", "platform.sqlite3-wal", "platform.sqlite3-shm", "launcher.pid"}


def _f_path(value, label):
    requested = Path(value).absolute()
    resolved = requested.resolve()
    if requested.drive.upper() != "F:" or resolved.drive.upper() != "F:":
        raise ValueError(f"{label} must remain on the local F drive: {requested}")
    return resolved


@contextmanager
def connection(path):
    # sqlite3.Connection.__exit__ commits but does not close; Windows cannot
    # promote/delete the staging directory until every handle is closed.
    with closing(sqlite3.connect(path)) as instance:
        with instance:
            yield instance


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_file(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"Refusing linked file/directory: {path}")
    if not path.is_file():
        raise FileNotFoundError(path)


def relative_entry(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe backup path: {value}")
    return path


def check_database(path):
    with connection(path) as database:
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not database.execute("PRAGMA foreign_key_check").fetchall()


def record_file(root, relative, records):
    path = root / relative_entry(relative)
    safe_file(path)
    records[relative] = {"bytes": path.stat().st_size, "sha256": digest(path)}


def backup(data_dir, destination):
    data_dir = _f_path(data_dir, "Backup data directory")
    destination = _f_path(destination, "Backup output directory")
    if not (data_dir / "platform.sqlite3").is_file():
        raise FileNotFoundError("No platform.sqlite3 in the selected data directory")
    if destination.exists() or destination.is_relative_to(data_dir) or data_dir.is_relative_to(destination):
        raise ValueError("Backup destination must be a new directory outside the workspace")
    stage = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        db_copy = stage / "data" / "platform.sqlite3"
        db_copy.parent.mkdir()
        with connection(data_dir / "platform.sqlite3") as source, connection(db_copy) as target:
            source.backup(target)
        check_database(db_copy)
        with connection(db_copy) as database:
            active = database.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','pausing','paused','cancelling')").fetchone()[0]
            if active:
                raise ValueError(f"Stop/cancel {active} active jobs before taking a portable backup")
            originals = {}
            for original, sha in database.execute("SELECT path,sha256 FROM assets"):
                if original in originals and originals[original] != sha:
                    raise ValueError(f"Conflicting source identities: {original}")
                originals[original] = sha
            for (manifest,) in database.execute("SELECT manifest_path FROM datasets"):
                if digest(manifest) != database.execute("SELECT manifest_sha256 FROM datasets WHERE manifest_path=?", (manifest,)).fetchone()[0]:
                    raise ValueError(f"Modified dataset manifest: {manifest}")
                for item in json.loads(Path(manifest).read_text(encoding="utf-8"))["items"]:
                    original, sha = item["path"], item["sha256"]
                    if original in originals and originals[original] != sha:
                        raise ValueError(f"Conflicting frozen source identities: {original}")
                    originals[original] = sha
        records = {}
        record_file(stage, "data/platform.sqlite3", records)
        for root, directories, files in os.walk(data_dir, followlinks=False):
            parent = Path(root)
            directories[:] = [name for name in directories if name not in EXCLUDED_DIRS]
            for name in directories:
                child = parent / name
                if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                    raise ValueError(f"Refusing linked directory: {child}")
            for name in files:
                if name in EXCLUDED_FILES:
                    continue
                source = parent / name
                safe_file(source)
                relative = "data/" + source.relative_to(data_dir).as_posix()
                target = stage / relative_entry(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                record_file(stage, relative, records)
        external = {}
        for original, expected_sha in sorted(originals.items()):
            source = Path(original)
            if source.is_relative_to(data_dir):
                safe_file(source)
                if digest(source) != expected_sha:
                    raise ValueError(f"Managed image changed: {source}")
                continue
            safe_file(source)
            if digest(source) != expected_sha:
                raise ValueError(f"External image changed: {source}")
            relative = "external/" + expected_sha + source.suffix.lower()
            if relative not in records:
                target = stage / relative_entry(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                record_file(stage, relative, records)
            external[original] = relative
        metadata = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "source_data_dir": str(data_dir), "files": records, "external_paths": external,
                    "excluded_regenerable_dirs": sorted(EXCLUDED_DIRS)}
        (stage / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        verify(stage)
        os.replace(stage, destination)
        return metadata
    except BaseException:
        shutil.rmtree(stage)
        raise


def verify(backup_dir):
    backup_dir = _f_path(backup_dir, "Backup directory")
    metadata = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    if metadata.get("format") != 1 or "data/platform.sqlite3" not in metadata["files"]:
        raise ValueError("Unsupported or incomplete backup")
    for relative, identity in metadata["files"].items():
        path = backup_dir / relative_entry(relative)
        if not path.resolve().is_relative_to(backup_dir):
            raise ValueError(f"Backup path escaped its directory: {relative}")
        safe_file(path)
        if path.stat().st_size != identity["bytes"] or digest(path) != identity["sha256"]:
            raise ValueError(f"Backup integrity failure: {relative}")
    check_database(backup_dir / "data" / "platform.sqlite3")
    return metadata


def restore(backup_dir, destination):
    backup_dir = _f_path(backup_dir, "Backup directory")
    destination = _f_path(destination, "Restore output directory")
    if destination.exists() or destination.is_relative_to(backup_dir) or backup_dir.is_relative_to(destination):
        raise ValueError("Restore only into a new directory outside the backup")
    metadata = verify(backup_dir)
    original_root = Path(metadata["source_data_dir"])
    stage = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        for relative in metadata["files"]:
            if not relative.startswith("data/"):
                continue
            target = stage / "data" / relative_entry(relative.removeprefix("data/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_dir / relative_entry(relative), target)
        relocated_external = {}
        for original, archived in metadata["external_paths"].items():
            # Content is deduplicated in the backup, but source path identity
            # must be preserved: assets has UNIQUE(project_id,path,sha256).
            unique = hashlib.sha256(original.encode("utf-8")).hexdigest()
            relative = relative_entry("external/paths/" + unique + "/" + Path(original).name)
            physical = stage / "data" / relative
            physical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_dir / relative_entry(archived), physical)
            relocated_external[original] = str(destination / relative)

        def mapped(original):
            if original in relocated_external:
                return relocated_external[original]
            path = Path(original)
            if path.is_relative_to(original_root):
                return str(destination / path.relative_to(original_root))
            return original  # Historic job items may refer to deleted assets.

        db = stage / "data" / "platform.sqlite3"
        with connection(db) as database:
            for table, field in (("assets", "path"), ("job_items", "path"),
                                 ("models", "package_path"), ("datasets", "manifest_path")):
                for row_id, old in database.execute(f"SELECT id,{field} FROM {table}").fetchall():
                    new = mapped(old)
                    if new != old:
                        database.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (new, row_id))
            for dataset_id, original in database.execute("SELECT id,manifest_path FROM datasets").fetchall():
                physical = stage / "data" / Path(original).relative_to(destination)
                snapshot = json.loads(physical.read_text(encoding="utf-8"))
                for item in snapshot["items"]:
                    item["path"] = mapped(item["path"])
                physical.write_text(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                database.execute("UPDATE datasets SET manifest_sha256=? WHERE id=?", (digest(physical), dataset_id))
                csv_path = physical.with_suffix(".csv")
                if csv_path.exists():
                    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                        reader = csv.DictReader(stream)
                        fields, rows = reader.fieldnames, list(reader)
                    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                        writer = csv.DictWriter(stream, fieldnames=fields)
                        writer.writeheader()
                        for row in rows:
                            row["path"] = mapped(row["path"])
                            writer.writerow(row)
        check_database(db)
        with connection(db) as database:
            for original, sha in database.execute("SELECT path,sha256 FROM assets"):
                physical = stage / "data" / Path(original).relative_to(destination)
                if not physical.exists() or digest(physical) != sha:
                    raise ValueError(f"Restored asset failed verification: {original}")
            for original, sha in database.execute("SELECT manifest_path,manifest_sha256 FROM datasets"):
                physical = stage / "data" / Path(original).relative_to(destination)
                if digest(physical) != sha:
                    raise ValueError(f"Restored dataset failed verification: {original}")
            for (model_path,) in database.execute("SELECT package_path FROM models"):
                physical = stage / "data" / Path(model_path).relative_to(destination)
                if not (physical / "model.onnx").is_file():
                    raise ValueError(f"Missing restored model: {model_path}")
        os.replace(stage / "data", destination)
        stage.rmdir()
        return {"destination": str(destination), "files": len(metadata["files"]),
                "external_paths": len(metadata["external_paths"])}
    except BaseException:
        shutil.rmtree(stage)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Back up a stopped workspace and referenced external images")
    create.add_argument("--data-dir", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("verify", help="Check every backup hash and SQLite integrity")
    validate.add_argument("--backup", type=Path, required=True)
    recover = commands.add_parser("restore", help="Restore to an empty, separate directory")
    recover.add_argument("--backup", type=Path, required=True)
    recover.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "create":
        result = backup(args.data_dir, args.output)
        print(json.dumps({"output": str(args.output.resolve()), "files": len(result["files"]),
                          "external_paths": len(result["external_paths"])}, ensure_ascii=False))
    elif args.command == "verify":
        result = verify(args.backup)
        print(json.dumps({"verified": str(args.backup.resolve()), "files": len(result["files"])}, ensure_ascii=False))
    else:
        print(json.dumps(restore(args.backup, args.target), ensure_ascii=False))


if __name__ == "__main__":
    main()
