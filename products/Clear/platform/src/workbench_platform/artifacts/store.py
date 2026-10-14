"""Atomic artifact publication and byte-verified download staging.

The platform owns metadata and managed blobs. Native references retain only
the native application/instance/artifact identity, never a browser-supplied
path. Every download checks the pinned revision and SHA256 before yielding a
path; unavailable content is never reconstructed under the same artifact ID.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator

from pydantic import ValidationError

from packages.contracts.models import Artifact, JobRef, SourceLocator

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+\Z")
_CHUNK = 1024 * 1024
_MAX_BYTES = 9_007_199_254_740_991


class ArtifactUnavailable(ValueError):
    def __init__(self, code: str):
        super().__init__("Artifact is unavailable")
        self.code = code


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _metadata(job_ref: JobRef | dict, source_refs: Iterable[SourceLocator | dict],
              media_type: str, application_version: str, rule_or_model_version: str) -> tuple[JobRef, list[SourceLocator]]:
    try:
        job = JobRef.model_validate(job_ref)
        refs = [SourceLocator.model_validate(ref) for ref in source_refs]
    except (ValidationError, ValueError, TypeError):
        raise ArtifactUnavailable("VALIDATION_FAILED") from None
    if (not isinstance(media_type, str) or len(media_type) > 256
            or _MEDIA.fullmatch(media_type) is None
            or not isinstance(application_version, str) or not 1 <= len(application_version) <= 256
            or not isinstance(rule_or_model_version, str)
            or not 1 <= len(rule_or_model_version) <= 256
            or len(refs) > 10_000):
        raise ArtifactUnavailable("VALIDATION_FAILED")
    return job, refs


def _copy(stream: BinaryIO, target: BinaryIO) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    try:
        while chunk := stream.read(_CHUNK):
            if not isinstance(chunk, bytes):
                raise ArtifactUnavailable("SOURCE_FAILED")
            size += len(chunk)
            if size > _MAX_BYTES:
                raise ArtifactUnavailable("SOURCE_FAILED")
            target.write(chunk)
            digest.update(chunk)
        target.flush()
        os.fsync(target.fileno())
    except (OSError, ValueError):
        raise ArtifactUnavailable("SOURCE_FAILED") from None
    return digest.hexdigest(), size


def _file_hash(path: Path) -> tuple[str, int]:
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactUnavailable("CORRUPT")
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ) or size != after.st_size:
                raise ArtifactUnavailable("CORRUPT")
            return digest.hexdigest(), size
    except OSError:
        raise ArtifactUnavailable("MISSING") from None


class ArtifactStore:
    def __init__(self, db_path: str | Path, blob_root: str | Path):
        self.db_path = Path(db_path).resolve()
        self.blob_root = Path(blob_root).resolve()
        if not self.db_path.parent.is_dir() or not self.blob_root.is_dir():
            raise ArtifactUnavailable("STORAGE_UNAVAILABLE")
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS published_artifact (
                    artifact_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>=1),
                    availability TEXT NOT NULL CHECK(availability IN
                        ('available','expired','missing','corrupt')),
                    storage_mode TEXT NOT NULL CHECK(storage_mode IN ('managed','referenced')),
                    blob_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    media_type TEXT NOT NULL, application_version TEXT NOT NULL,
                    rule_or_model_version TEXT NOT NULL, input_manifest_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, job_ref TEXT NOT NULL, source_refs TEXT NOT NULL,
                    origin_app_id TEXT NOT NULL, origin_instance_id TEXT NOT NULL,
                    native_artifact_id TEXT, expires_at TEXT);
                CREATE INDEX IF NOT EXISTS published_artifact_workspace
                    ON published_artifact(workspace_id, artifact_id);
                CREATE INDEX IF NOT EXISTS published_artifact_content
                    ON published_artifact(blob_hash);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextmanager
    def publication_lock(self, identity: str):
        """An OS lock serializes publishers and releases on process death."""
        if not _valid_id(identity):
            raise ArtifactUnavailable('VALIDATION_FAILED')
        folder = self.blob_root / '.publication-locks'
        folder.mkdir(exist_ok=True)
        path = folder / identity
        with path.open('a+b') as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ArtifactUnavailable('PUBLISHING') from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _blob(self, digest: str) -> Path:
        if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
            raise ArtifactUnavailable("VALIDATION_FAILED")
        return self.blob_root / digest[:2] / digest

    def _publish_blob(self, temp: Path, digest: str, size: int) -> None:
        target = self._blob(digest)
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            if target.is_symlink() or _file_hash(target) != (digest, size):
                raise ArtifactUnavailable("CORRUPT")
            return
        try:
            os.link(temp, target)
        except FileExistsError:
            if target.is_symlink() or _file_hash(target) != (digest, size):
                raise ArtifactUnavailable("CORRUPT")
        except OSError:
            raise ArtifactUnavailable("STORAGE_UNAVAILABLE") from None

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(schema_version="1.0", artifact_id=row["artifact_id"],
                        workspace_id=row["workspace_id"],
                        job_ref=JobRef.model_validate_json(row["job_ref"]),
                        revision=row["revision"], media_type=row["media_type"],
                        blob_hash=row["blob_hash"], size_bytes=row["size_bytes"],
                        input_manifest_hash=row["input_manifest_hash"],
                        application_version=row["application_version"],
                        rule_or_model_version=row["rule_or_model_version"],
                        created_at=row["created_at"], availability=row["availability"],
                        source_refs=[SourceLocator.model_validate(item) for item in json.loads(row["source_refs"])])

    def _record(self, workspace_id: str, artifact_id: str) -> sqlite3.Row:
        if not _valid_id(workspace_id) or not _valid_id(artifact_id):
            raise ArtifactUnavailable("VALIDATION_FAILED")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM published_artifact WHERE workspace_id=? AND artifact_id=?",
                             (workspace_id, artifact_id)).fetchone()
        if row is None:
            raise ArtifactUnavailable("NOT_FOUND")
        return row

    def get(self, workspace_id: str, artifact_id: str) -> Artifact:
        return self._artifact(self._record(workspace_id, artifact_id))

    def provenance(self, workspace_id: str, artifact_id: str) -> dict:
        row = self._record(workspace_id, artifact_id)
        return {key: row[key] for key in ("storage_mode", "origin_app_id",
                                          "origin_instance_id", "native_artifact_id", "expires_at")}

    def _insert(self, workspace_id: str, job: JobRef, refs: list[SourceLocator],
                *, digest: str, size: int, media_type: str, application_version: str,
                rule_or_model_version: str, storage_mode: str,
                native_artifact_id: str | None, expires_at: str | None,
                on_insert: Callable | None = None) -> Artifact:
        artifact_id = "a" + uuid.uuid4().hex
        created_at = _utc()
        artifact = Artifact(schema_version="1.0", artifact_id=artifact_id,
                            workspace_id=workspace_id, job_ref=job, revision=1,
                            media_type=media_type, blob_hash=digest, size_bytes=size,
                            input_manifest_hash=job.input_manifest_hash,
                            application_version=application_version,
                            rule_or_model_version=rule_or_model_version,
                            created_at=created_at, availability="available", source_refs=refs)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO published_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (artifact_id, workspace_id, 1, "available", storage_mode,
                            digest, size, media_type, application_version, rule_or_model_version,
                            job.input_manifest_hash, created_at,
                            job.model_dump_json(), json.dumps([ref.model_dump(mode="json") for ref in refs],
                                                           ensure_ascii=False, separators=(",", ":")),
                            job.app_id, job.instance_id, native_artifact_id, expires_at))
                if on_insert is not None:
                    on_insert(db, artifact)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return artifact

    def publish_managed(self, workspace_id: str, stream: BinaryIO, *,
                        expected_hash: str, expected_size: int,
                        job_ref: JobRef | dict, source_refs: Iterable[SourceLocator | dict],
                        media_type: str, application_version: str,
                        rule_or_model_version: str, on_insert: Callable | None = None) -> Artifact:
        if (not _valid_id(workspace_id) or not isinstance(expected_hash, str)
                or _HASH.fullmatch(expected_hash) is None
                or type(expected_size) is not int or not 0 <= expected_size <= _MAX_BYTES):
            raise ArtifactUnavailable("VALIDATION_FAILED")
        job, refs = _metadata(job_ref, source_refs, media_type,
                              application_version, rule_or_model_version)
        try:
            with tempfile.NamedTemporaryFile(dir=self.blob_root, prefix=".incoming-",
                                             suffix=".tmp", delete=False) as target:
                temp = Path(target.name)
                digest, size = _copy(stream, target)
            if (digest, size) != (expected_hash, expected_size):
                raise ArtifactUnavailable("INPUT_CHANGED")
            self._publish_blob(temp, digest, size)
            return self._insert(workspace_id, job, refs, digest=digest, size=size,
                                media_type=media_type, application_version=application_version,
                                rule_or_model_version=rule_or_model_version,
                                storage_mode="managed", native_artifact_id=None, expires_at=None, on_insert=on_insert)
        finally:
            if "temp" in locals():
                temp.unlink(missing_ok=True)

    def register_reference(self, workspace_id: str, native_artifact_id: str,
                           open_native: Callable[[str, str, str], BinaryIO], *,
                           expected_hash: str, expected_size: int, expires_at: str,
                           job_ref: JobRef | dict, source_refs: Iterable[SourceLocator | dict],
                           media_type: str, application_version: str,
                           rule_or_model_version: str) -> Artifact:
        if (not _valid_id(workspace_id) or not isinstance(native_artifact_id, str)
                or _NATIVE_ID.fullmatch(native_artifact_id) is None
                or not callable(open_native) or not isinstance(expected_hash, str)
                or _HASH.fullmatch(expected_hash) is None
                or type(expected_size) is not int or not 0 <= expected_size <= _MAX_BYTES):
            raise ArtifactUnavailable("VALIDATION_FAILED")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo != timezone.utc or expiry <= datetime.now(timezone.utc):
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise ArtifactUnavailable("VALIDATION_FAILED") from None
        job, refs = _metadata(job_ref, source_refs, media_type,
                              application_version, rule_or_model_version)
        try:
            with closing(open_native(job.app_id, job.instance_id, native_artifact_id)) as source:
                with tempfile.NamedTemporaryFile(dir=self.blob_root, prefix=".verify-",
                                                 suffix=".tmp", delete=False) as target:
                    temp = Path(target.name)
                    digest, size = _copy(source, target)
            if (digest, size) != (expected_hash, expected_size):
                raise ArtifactUnavailable("INPUT_CHANGED")
            return self._insert(workspace_id, job, refs, digest=digest, size=size,
                                media_type=media_type, application_version=application_version,
                                rule_or_model_version=rule_or_model_version,
                                storage_mode="referenced", native_artifact_id=native_artifact_id,
                                expires_at=expires_at)
        except ArtifactUnavailable:
            raise
        except (OSError, ValueError, TypeError):
            raise ArtifactUnavailable("SOURCE_FAILED") from None
        finally:
            if "temp" in locals():
                temp.unlink(missing_ok=True)

    def _transition(self, row: sqlite3.Row, availability: str) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE published_artifact SET availability=?,revision=revision+1 "
                           "WHERE artifact_id=? AND revision=? AND availability='available'",
                           (availability, row["artifact_id"], row["revision"]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def expire(self, workspace_id: str, artifact_id: str, *, expected_revision: int) -> Artifact:
        row = self._record(workspace_id, artifact_id)
        if type(expected_revision) is not int or row["revision"] != expected_revision:
            raise ArtifactUnavailable("REVISION_CONFLICT")
        if row["availability"] != "available":
            raise ArtifactUnavailable(row["availability"].upper())
        self._transition(row, "expired")
        return self.get(workspace_id, artifact_id)

    @contextmanager
    def stage_download(self, workspace_id: str, artifact_id: str, *,
                       expected_revision: int, expected_hash: str,
                       open_native: Callable[[str, str, str], BinaryIO] | None = None) -> Iterator[Path]:
        row = self._record(workspace_id, artifact_id)
        if (type(expected_revision) is not int or expected_revision != row["revision"]
                or expected_hash != row["blob_hash"]):
            raise ArtifactUnavailable("REVISION_CONFLICT")
        if row["availability"] != "available":
            raise ArtifactUnavailable(row["availability"].upper())
        if row["expires_at"] is not None:
            expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expiry <= datetime.now(timezone.utc):
                self._transition(row, "expired")
                raise ArtifactUnavailable("EXPIRED")
        if row["storage_mode"] == "managed":
            path = self._blob(row["blob_hash"])
            if not path.is_file() or path.is_symlink():
                self._transition(row, "missing")
                raise ArtifactUnavailable("MISSING")
            try:
                actual = _file_hash(path)
            except ArtifactUnavailable as error:
                self._transition(row, "missing" if error.code == "MISSING" else "corrupt")
                raise
            if actual != (row["blob_hash"], row["size_bytes"]):
                self._transition(row, "corrupt")
                raise ArtifactUnavailable("CORRUPT")
            yield path
            return
        if open_native is None:
            raise ArtifactUnavailable("SOURCE_UNAVAILABLE")
        temp = None
        try:
            try:
                with closing(open_native(row["origin_app_id"], row["origin_instance_id"],
                                         row["native_artifact_id"])) as source:
                    with tempfile.NamedTemporaryFile(dir=self.blob_root, prefix=".download-",
                                                     suffix=".tmp", delete=False) as target:
                        temp = Path(target.name)
                        actual = _copy(source, target)
            except ArtifactUnavailable:
                raise
            except (OSError, ValueError, TypeError):
                self._transition(row, "missing")
                raise ArtifactUnavailable("MISSING") from None
            if actual != (row["blob_hash"], row["size_bytes"]):
                self._transition(row, "corrupt")
                raise ArtifactUnavailable("CORRUPT")
            yield temp
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
