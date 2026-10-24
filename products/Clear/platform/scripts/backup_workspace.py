"""Cold backup and new-directory restore for platform or native OCR data.

Stop the platform and both native applications before creating a backup. This
tool covers one data root at a time. The installation workflow combines it
with the classification application's own backup and relocation utility.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid


PLATFORM_FILES = ("assets.sqlite3", "workspaces.sqlite3", "jobs.sqlite3",
                  "recent-work.sqlite3", "ocr-handoffs.sqlite3",
                  "classification-handoffs.sqlite3", "screen-batches.sqlite3", "product-reader-requirements.json")
PLATFORM_DIRS = ("asset-blobs", "asset-uploads", "artifact-center", "data-workbench", "batches", "audit", "forensics", "agent", "manual-analysis")
REQUIRED = {"platform": ("assets.sqlite3", "workspaces.sqlite3"),
            "ocr": ("workbench.sqlite3",)}
OCR_TRANSIENT_DIRS = frozenset({".image-staging", "pdf-ipc"})
# These drivers persist recovery state and receipts in operations.sqlite3;
# they never create a legacy graph checkpoint database.
DIRECT_BACKUP_DRIVERS = frozenset({"image-text-v1", "pdf-text-v1", "data-action-v1",
                                  "classification-predict-v1"})
FORMAT = 1
CHUNK = 1024 * 1024


def _f_path(value: Path, label: str) -> Path:
    """Validate the requested drive and junction target before creating files."""
    requested = Path(value).absolute()
    resolved = requested.resolve()
    if requested.drive.upper() != "F:" or resolved.drive.upper() != "F:":
        raise ValueError(f"{label} must remain on the local F drive: {requested}")
    return resolved


def _safe(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"Linked backup path is not allowed: {path}")


def _relative(name: str) -> Path:
    path = Path(name)
    if (not name or path.is_absolute() or path.drive or not path.parts
            or any(part in {".", "..", ""} for part in path.parts)
            or path.as_posix() != name):
        raise ValueError(f"Invalid backup member: {name}")
    return path


def _hash(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_database(path: Path):
    """Observe committed WAL without acquiring source checkpoint authority."""
    wal = path.with_name(path.name + '-wal')
    _safe(wal)
    # Immutable reads are safe only without pending WAL. Using them for an
    # orphan WAL would silently omit its last committed business rows.
    options = '?mode=ro' if wal.is_file() and wal.stat().st_size else '?mode=ro&immutable=1'
    db = sqlite3.connect(path.as_uri() + options, uri=True)
    db.execute('PRAGMA temp_store=MEMORY')
    return db


def _check_db(path: Path) -> None:
    with closing(_read_database(path)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {path}")


def _members(root: Path, kind: str) -> list[Path]:
    if kind not in REQUIRED:
        raise ValueError("Unknown backup component")
    if kind == "ocr":
        result = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            current = Path(directory)
            for entry in dirs:
                _safe(current / entry)
            dirs[:] = [entry for entry in dirs if entry not in OCR_TRANSIENT_DIRS]
            for entry in files:
                source = current / entry
                _safe(source)
                if (entry == "service.lock" and current == root) or entry.endswith(
                        (".sqlite3-wal", ".sqlite3-shm")):
                    continue
                if not source.is_file():
                    raise ValueError(f"Unsupported storage entry: {source}")
                result.append(source)
        if not (root / "workbench.sqlite3").is_file():
            raise FileNotFoundError(root / "workbench.sqlite3")
        return sorted(result)
    result = []
    _check_agent_pair(root)
    for name in PLATFORM_FILES:
        path = root / name
        _safe(path)
        if path.is_file():
            result.append(path)
        elif name in REQUIRED["platform"]:
            raise FileNotFoundError(path)
    for name in PLATFORM_DIRS:
        parent = root / name
        _safe(parent)
        if not parent.exists():
            continue
        if not parent.is_dir():
            raise ValueError(f"Expected a directory: {parent}")
        for directory, dirs, files in os.walk(parent, followlinks=False):
            current = Path(directory)
            for entry in dirs:
                _safe(current / entry)
            for entry in files:
                source = current / entry
                _safe(source)
                if not source.is_file():
                    raise ValueError(f"Unsupported storage entry: {source}")
                if source.name.endswith((".sqlite3-wal", ".sqlite3-shm")):
                    continue
                result.append(source)
    return sorted(result)


def _check_agent_pair(root: Path) -> None:
    operations = root / 'agent/operations.sqlite3'
    checkpoints = root / 'agent/checkpoints.sqlite3'
    if checkpoints.exists() and not operations.is_file():
        raise ValueError('Agent backup requires operations for its checkpoints')
    if operations.is_file():
        with closing(_read_database(operations)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'runs' not in tables:
                return
            columns = {row[1] for row in db.execute('PRAGMA table_info(runs)')}
            driver_column = 'driver' if 'driver' in columns else "'harness'"
            for run_id, session_id, driver in db.execute('SELECT id,session_id,' + driver_column + ' FROM runs'):
                owned = ('harness_runs' in tables and db.execute('SELECT 1 FROM harness_runs WHERE run_id=?', (run_id,)).fetchone())
                migrated = ('harness_materialized' in tables and db.execute('SELECT 1 FROM harness_materialized WHERE session_id=?', (session_id,)).fetchone())
                started = any(name in tables and db.execute('SELECT 1 FROM ' + name + ' WHERE run_id=? LIMIT 1', (run_id,)).fetchone()
                              for name in ('operations', 'model_requests'))
                if (started and driver not in DIRECT_BACKUP_DRIVERS
                        and not checkpoints.is_file() and not (owned or migrated)):
                    raise ValueError('Legacy Agent backup requires its checkpoint database')
                projected = ('harness_events' in tables and db.execute('SELECT 1 FROM harness_events WHERE session_id=? LIMIT 1', (session_id,)).fetchone())
                if projected or migrated:
                    logs = [path for path in (root / 'agent/harness/sessions').rglob('session.v4.jsonl')
                            if path.parent.name == session_id]
                    if len(logs) != 1:
                        raise ValueError('Harness backup requires one durable session log: ' + session_id)


def _screen_reader_required(root: Path) -> bool:
    path = root / 'screen-batches.sqlite3'
    if not path.is_file():
        return False
    from workbench_platform.reader_requirements import read_database
    with closing(read_database(path)) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screen_batch_refs'").fetchone()
        return bool(table and db.execute('SELECT 1 FROM screen_batch_refs LIMIT 1').fetchone())


def _screen_requirements(root: Path, *, create_missing: bool = False) -> None:
    from workbench_platform.reader_requirements import ensure_reader_requirements
    ensure_reader_requirements(root, create_missing=create_missing)


def _copy_member(source: Path, target: Path, *, immutable_archive: bool = False) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    is_db = source.name.endswith(".sqlite3")
    if is_db and immutable_archive:
        # Migration archives are byte evidence pinned by their own manifest.
        # SQLite backup preserves rows but may change these sealed bytes.
        if any(source.with_name(source.name + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError(f'Immutable migration database has journal files: {source}')
        with source.open('rb') as stream:
            header = stream.read(20)
        if header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
            raise ValueError(f'Immutable migration database has a WAL or unsupported header: {source}')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as input_db:
            if (input_db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                    or input_db.execute('PRAGMA quick_check').fetchone()[0] != 'ok'):
                raise ValueError(f'Immutable migration database is not a sealed DELETE snapshot: {source}')
        shutil.copy2(source, target)
    elif is_db:
        with closing(_read_database(source)) as input_db, closing(sqlite3.connect(target)) as output_db:
            input_db.backup(output_db)
        _check_db(target)
    else:
        shutil.copy2(source, target)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Source changed during backup: {source}")
    digest, size = _hash(target)
    if (not is_db or immutable_archive) and (digest, size) != _hash(source):
        raise ValueError(f"Source changed during backup: {source}")
    return {"sha256": digest, "bytes": size, "sqlite": is_db}


def _distinct(source: Path, destination: Path) -> None:
    if (destination.exists() or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise ValueError("Destination must be a new directory outside the source")


def create(source: Path, destination: Path, *, kind: str = "platform") -> dict:
    _safe(source)
    source = _f_path(source, "Backup data directory")
    destination = _f_path(destination, "Backup output directory")
    if not source.is_dir():
        raise FileNotFoundError(source)
    _distinct(source, destination)
    members = _members(source, kind)
    stage = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        records = {}
        for item in members:
            relative = item.relative_to(source).as_posix()
            records[relative] = _copy_member(item, stage / relative,
                immutable_archive=kind == 'platform' and relative.startswith('agent/harness/legacy/'))
        if _members(source, kind) != members:
            raise ValueError("Source file set changed during backup")
        _screen_requirements(stage, create_missing=True)
        marker = stage / 'product-reader-requirements.json'
        if marker.is_file():
            digest, size = _hash(marker)
            records[marker.name] = {'sha256': digest, 'bytes': size, 'sqlite': False}
        manifest = {"format": FORMAT, "kind": kind,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "files": records}
        if kind == 'ocr':
            manifest['source_data_dir'] = str(source)
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        verify(stage)
        os.replace(stage, destination)
        return manifest
    except BaseException:
        shutil.rmtree(stage)
        raise


def verify(backup: Path) -> dict:
    _safe(backup)
    backup = _f_path(backup, "Backup directory")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files")
    kind = manifest.get("kind", "platform")
    if (manifest.get("format") != FORMAT or not isinstance(files, dict)
            or kind not in REQUIRED
            or not all(name in files for name in REQUIRED[kind])):
        raise ValueError("Unsupported or incomplete platform backup")
    if 'source_data_dir' in manifest:
        root = manifest['source_data_dir']
        if not isinstance(root, str) or not Path(root).is_absolute() or '..' in Path(root).parts:
            raise ValueError('Invalid backup source data directory')
        _f_path(Path(root), 'Backup source data directory')
    for name, recorded in files.items():
        relative = _relative(name)
        target = backup / relative
        if not target.resolve().is_relative_to(backup):
            raise ValueError(f"Backup member escapes directory: {name}")
        _safe(target)
        if not target.is_file() or _hash(target) != (recorded["sha256"], recorded["bytes"]):
            raise ValueError(f"Backup member changed: {name}")
        if recorded["sqlite"]:
            _check_db(target)
    _check_agent_pair(backup)
    from workbench_platform.reader_requirements import infer_reader_requirements
    if infer_reader_requirements(backup)['reader_features'] and 'product-reader-requirements.json' not in files:
        raise ValueError('Actual-record reader capabilities are missing from the backup manifest')
    _screen_requirements(backup)
    return manifest


def _relocate_screen_paths(stage: Path, destination: Path, manifest: dict) -> dict:
    """Move only live preparation/task references, preserving OCR history JSON."""
    counts = {'screen_input_paths_relocated': 0, 'screen_prepared_paths_relocated': 0,
              'screen_task_metadata_paths_relocated': 0}
    if manifest.get('kind', 'platform') != 'ocr':
        return counts
    database = stage / 'workbench.sqlite3'
    source_root = Path(manifest['source_data_dir']) if 'source_data_dir' in manifest else None
    members = manifest['files']

    def component(value):
        if (not isinstance(value, str) or not value or value in {'.', '..'}
                or Path(value).drive or Path(value).is_absolute() or len(Path(value).parts) != 1
                or '/' in value or '\\' in value):
            raise ValueError('Invalid screen path ownership identity')
        return value

    def relative(value):
        if not isinstance(value, str) or not value or '..' in Path(value).parts:
            raise ValueError('Invalid live screen path')
        path = Path(value)
        if path.is_absolute():
            if source_root is None:
                raise ValueError('Absolute screen paths require a backup source data directory')
            try:
                path = path.relative_to(source_root)
            except ValueError as error:
                raise ValueError('Live screen path is outside its recorded source root') from error
        elif path.drive:
            raise ValueError('Invalid live screen path drive')
        return _relative(path.as_posix())

    def member(path, expected_hash=None):
        record = members.get(path.as_posix())
        copied = stage / path
        if (record is None or record['sqlite'] or not copied.is_file()
                or not copied.resolve().is_relative_to(stage)):
            raise ValueError('Live screen path has no owned backup file')
        if expected_hash is not None and record['sha256'] != expected_hash:
            raise ValueError('Live screen file content identity differs')

    def mapped(value, expected, expected_hash=None):
        if relative(value) != expected:
            raise ValueError('Live screen path belongs to a different version or crop')
        member(expected, expected_hash)
        return str(destination / expected)

    with closing(sqlite3.connect(database)) as db, db:
        db.row_factory = sqlite3.Row
        tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'screen_items' not in tables:
            return counts
        images = {}
        for row in db.execute('''SELECT s.batch_id,s.input_version_id,s.image_id,s.source_hash,s.preparation,
                v.path AS version_path,v.sha256 AS version_hash,v.image_id AS version_image
                FROM screen_items s LEFT JOIN versions v ON v.id=s.input_version_id''').fetchall():
            batch, version = component(row['batch_id']), component(row['input_version_id'])
            if row['version_image'] != row['image_id'] or row['source_hash'] != row['version_hash']:
                raise ValueError('Screen item lost its source version identity')
            if not row['preparation']:
                continue
            preparation = json.loads(row['preparation'])
            if not isinstance(preparation, dict) or not isinstance(preparation.get('prepared_images'), list):
                raise ValueError('Invalid screen preparation')
            version_path = relative(row['version_path'])
            member(version_path, row['version_hash'])
            if 'input_path' in preparation:
                if preparation.get('input_sha256', row['source_hash']) != row['source_hash']:
                    raise ValueError('Preparation input content identity differs')
                preparation['input_path'] = mapped(preparation['input_path'], version_path, row['source_hash'])
                counts['screen_input_paths_relocated'] += 1
            for image in preparation['prepared_images']:
                if not isinstance(image, dict):
                    raise ValueError('Invalid prepared crop')
                ident = component(image.get('id'))
                key = (batch, version, ident)
                expected = Path('screen-batches') / batch / version / 'prepared' / (ident + '.png')
                if key in images:
                    raise ValueError('Duplicate prepared crop identity')
                image['path'] = mapped(image.get('path'), expected)
                images[key] = expected
                counts['screen_prepared_paths_relocated'] += 1
            db.execute('UPDATE screen_items SET preparation=? WHERE batch_id=? AND input_version_id=?',
                       (json.dumps(preparation, ensure_ascii=False, separators=(',', ':')), batch, version))
        if 'screen_tasks' in tables:
            for row in db.execute('SELECT task_id,batch_id,input_version_id,prepared_id,prepared_path,metadata FROM screen_tasks').fetchall():
                key = (component(row['batch_id']), component(row['input_version_id']), component(row['prepared_id']))
                expected = images.get(key)
                metadata = json.loads(row['metadata'])
                if (expected is None or relative(row['prepared_path']) != expected or not isinstance(metadata, dict)
                        or (metadata.get('batch_id'), metadata.get('input_version_id'), metadata.get('id')) != key):
                    raise ValueError('Screen task lost its version or crop identity')
                if not isinstance(metadata.get('prepared_pixel_sha256'), str):
                    raise ValueError('Screen task lost its prepared file content identity')
                member(expected, metadata.get('prepared_pixel_sha256'))
                if 'path' in metadata:
                    metadata['path'] = mapped(metadata['path'], expected, metadata.get('prepared_pixel_sha256'))
                    db.execute('UPDATE screen_tasks SET metadata=? WHERE task_id=?',
                               (json.dumps(metadata, ensure_ascii=False, separators=(',', ':')), row['task_id']))
                    counts['screen_task_metadata_paths_relocated'] += 1
    _check_db(database)
    return counts


def restore(backup: Path, destination: Path) -> dict:
    backup = _f_path(backup, "Backup directory")
    destination = _f_path(destination, "Restore output directory")
    _distinct(backup, destination)
    manifest = verify(backup)
    stage = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        for name in manifest["files"]:
            relative = _relative(name)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / relative, target)
        shutil.copy2(backup / "manifest.json", stage / "manifest.json")
        verify(stage)
        (stage / "manifest.json").unlink()
        screen_paths = _relocate_screen_paths(stage, destination, manifest)
        agent_db = stage / 'agent/operations.sqlite3'
        relocated = 0
        if agent_db.is_file():
            with closing(sqlite3.connect(agent_db)) as db, db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='derived_inputs'").fetchone():
                    for asset_id, old_path, sha in db.execute('SELECT asset_id,provenance_path,provenance_hash FROM derived_inputs').fetchall():
                        old = Path(old_path)
                        if old.is_absolute():
                            relative = Path('bridges') / old.name
                            member = stage / 'agent' / relative
                            if not member.is_file() or _hash(member)[0] != sha:
                                raise ValueError('Agent derived-source file is missing or changed')
                            db.execute('UPDATE derived_inputs SET provenance_path=? WHERE asset_id=?', (relative.as_posix(), asset_id))
                            relocated += 1
            _check_db(agent_db)
        os.replace(stage, destination)
        return {"destination": str(destination), "files": len(manifest["files"]),
                'agent_paths_relocated': relocated, **screen_paths}
    except BaseException:
        shutil.rmtree(stage)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create")
    make.add_argument("--data-dir", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    make.add_argument("--kind", choices=sorted(REQUIRED), default="platform")
    check = commands.add_parser("verify")
    check.add_argument("--backup", type=Path, required=True)
    recover = commands.add_parser("restore")
    recover.add_argument("--backup", type=Path, required=True)
    recover.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "create":
        result = create(args.data_dir, args.output, kind=args.kind)
        print(json.dumps({"output": str(args.output.resolve()), "files": len(result["files"])},
                         ensure_ascii=False))
    elif args.command == "verify":
        result = verify(args.backup)
        print(json.dumps({"verified": str(args.backup.resolve()), "files": len(result["files"])},
                         ensure_ascii=False))
    else:
        print(json.dumps(restore(args.backup, args.target), ensure_ascii=False))


if __name__ == "__main__":
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'src')]
    main()
