"""Workspace-scoped asset identity with verified local file access.

Source identity is a trusted root plus a canonical relative path. Blob identity
is SHA256 of the bytes. Neither a browser path nor an asset ID becomes a file
path. Referenced sources are copied and checked before processing; managed
blobs are content-addressed, immutable to this registry, and checked on read.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Iterator, Mapping

from packages.contracts.models import Asset

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+\Z")
_CHUNK = 1024 * 1024


class AssetUnavailable(ValueError):
    """The source, root, or registered asset cannot be accessed safely."""


class AssetChanged(AssetUnavailable):
    """Source bytes or registration revision differ from the approved asset."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _digest(path: Path) -> tuple[str, int]:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetUnavailable("Source is not a regular file")
            hash_value, size = hashlib.sha256(), 0
            while chunk := stream.read(_CHUNK):
                hash_value.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ) or size != after.st_size:
                raise AssetChanged("Source changed during verification")
            return hash_value.hexdigest(), size
    except (OSError, IsADirectoryError) as error:
        raise AssetUnavailable("Source is unavailable") from None


def _copy_snapshot(source: Path, destination: Path) -> tuple[str, int]:
    try:
        with source.open("rb") as original, destination.open("wb") as target:
            before = os.fstat(original.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetUnavailable("Source is not a regular file")
            hash_value, size = hashlib.sha256(), 0
            while chunk := original.read(_CHUNK):
                target.write(chunk)
                hash_value.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
            after = os.fstat(original.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ) or size != after.st_size:
                raise AssetChanged("Source changed during verification")
            return hash_value.hexdigest(), size
    except (OSError, IsADirectoryError):
        raise AssetUnavailable("Source is unavailable") from None


class AssetRegistry:
    def __init__(self, db_path: str | Path, blob_root: str | Path,
                 allowed_roots: Mapping[str, str | Path]):
        self.db_path = Path(db_path).resolve()
        self.blob_root = Path(blob_root).resolve()
        if (not self.db_path.parent.is_dir() or not self.blob_root.is_dir()
                or not allowed_roots):
            raise AssetUnavailable("Asset storage is unavailable")
        roots = {}
        for root_id, raw in allowed_roots.items():
            if not _id(root_id):
                raise AssetUnavailable("Invalid trusted root identity")
            path = Path(raw)
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise AssetUnavailable("Linked trusted root is not allowed")
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                raise AssetUnavailable("Trusted root is unavailable") from None
            if not resolved.is_dir():
                raise AssetUnavailable("Trusted root is not a directory")
            roots[root_id] = resolved
        self.roots = roots
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS registered_asset (
                    asset_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL, original_name TEXT NOT NULL,
                    media_type TEXT NOT NULL, storage_mode TEXT NOT NULL
                        CHECK(storage_mode IN ('managed','referenced')),
                    blob_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, source_id));
                CREATE INDEX IF NOT EXISTS registered_asset_workspace
                    ON registered_asset(workspace_id, asset_id);
                CREATE INDEX IF NOT EXISTS registered_asset_content
                    ON registered_asset(blob_hash);
                CREATE TABLE IF NOT EXISTS asset_tag (
                    asset_id TEXT NOT NULL, tag TEXT NOT NULL,
                    PRIMARY KEY(asset_id, tag),
                    FOREIGN KEY(asset_id) REFERENCES registered_asset(asset_id));
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _source(self, root_id: str, relative_path: str) -> tuple[Path, str]:
        if (not _id(root_id) or root_id not in self.roots
                or not isinstance(relative_path, str)
                or not relative_path or len(relative_path) > 1024
                or "\\" in relative_path or "\x00" in relative_path
                or PureWindowsPath(relative_path).is_absolute()
                or PureWindowsPath(relative_path).drive):
            raise AssetUnavailable("Invalid source location")
        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} or part.endswith(" ") or part.endswith(".") for part in parts):
            raise AssetUnavailable("Source path is not canonical")
        root = self.roots[root_id]
        source = root
        for part in parts:
            source = source / part
            if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
                raise AssetUnavailable("Linked source path is not allowed")
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            raise AssetUnavailable("Source is unavailable") from None
        if not resolved.is_file():
            raise AssetUnavailable("Source is not a regular file")
        canonical = os.path.normcase(relative_path)
        return resolved, canonical

    def _blob(self, digest: str) -> Path:
        if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
            raise AssetUnavailable("Invalid content hash")
        return self.blob_root / digest[:2] / digest

    def _install_blob(self, temp: Path, digest: str, size: int) -> None:
        target = self._blob(digest)
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            if target.is_symlink() or _digest(target) != (digest, size):
                raise AssetChanged("Managed blob is corrupt")
            return
        try:
            # Atomic no-replace publication. Existing content is never changed.
            os.link(temp, target)
        except FileExistsError:
            if target.is_symlink() or _digest(target) != (digest, size):
                raise AssetChanged("Managed blob is corrupt")
        except OSError:
            raise AssetUnavailable("Managed blob cannot be stored") from None

    @staticmethod
    def _asset(row: sqlite3.Row) -> Asset:
        return Asset(schema_version="1.0", asset_id=row["asset_id"],
                     workspace_id=row["workspace_id"], source_id=row["source_id"],
                     original_name=row["original_name"], media_type=row["media_type"],
                     storage_mode=row["storage_mode"], blob_hash=row["blob_hash"],
                     size_bytes=row["size_bytes"], revision=row["revision"],
                     created_at=row["created_at"])

    def register(self, workspace_id: str, root_id: str, relative_path: str, *,
                 media_type: str, storage_mode: str,
                 original_name: str | None = None) -> Asset:
        if (not _id(workspace_id) or not isinstance(storage_mode, str)
                or storage_mode not in {"managed", "referenced"}
                or not isinstance(media_type, str) or len(media_type) > 256
                or _MEDIA.fullmatch(media_type) is None):
            raise AssetUnavailable("Invalid asset registration")
        source, canonical = self._source(root_id, relative_path)
        display_name = source.name if original_name is None else original_name
        if (not isinstance(display_name, str) or not display_name.strip()
                or len(display_name) > 256 or any(ord(char) < 32 for char in display_name)):
            raise AssetUnavailable("Source name is too long")
        source_id = "s" + hashlib.sha256(f"{root_id}\0{canonical}".encode("utf-8")).hexdigest()[:32]
        temp = None
        try:
            if storage_mode == "managed":
                with tempfile.NamedTemporaryFile(dir=self.blob_root, prefix=".incoming-",
                                                 suffix=".tmp", delete=False) as stream:
                    temp = Path(stream.name)
                digest, size = _copy_snapshot(source, temp)
                if size > 9_007_199_254_740_991:
                    raise AssetUnavailable("Source is too large")
                self._install_blob(temp, digest, size)
            else:
                digest, size = _digest(source)
                if size > 9_007_199_254_740_991:
                    raise AssetUnavailable("Source is too large")
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute("SELECT * FROM registered_asset WHERE workspace_id=? AND source_id=?",
                                     (workspace_id, source_id)).fetchone()
                    if row is None:
                        asset_id = "a" + uuid.uuid4().hex
                        db.execute("INSERT INTO registered_asset VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (asset_id, workspace_id, source_id, root_id, relative_path,
                                    display_name, media_type, storage_mode, digest, size, 1, _utc()))
                    elif (row["blob_hash"], row["size_bytes"], row["media_type"],
                          row["storage_mode"], row["original_name"]) != (
                              digest, size, media_type, storage_mode, display_name):
                        db.execute("UPDATE registered_asset SET original_name=?,media_type=?,"
                                   "storage_mode=?,blob_hash=?,size_bytes=?,revision=revision+1 "
                                   "WHERE asset_id=?", (display_name, media_type, storage_mode,
                                                        digest, size, row["asset_id"]))
                        asset_id = row["asset_id"]
                    else:
                        asset_id = row["asset_id"]
                    row = db.execute("SELECT * FROM registered_asset WHERE asset_id=?", (asset_id,)).fetchone()
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            return self._asset(row)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _record(self, workspace_id: str, asset_id: str) -> sqlite3.Row:
        if not _id(workspace_id) or not _id(asset_id):
            raise AssetUnavailable("Invalid asset lookup")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM registered_asset WHERE workspace_id=? AND asset_id=?",
                             (workspace_id, asset_id)).fetchone()
        if row is None:
            raise AssetUnavailable("Asset is not registered here")
        return row

    def get(self, workspace_id: str, asset_id: str) -> Asset:
        return self._asset(self._record(workspace_id, asset_id))

    def source(self, workspace_id: str, asset_id: str) -> dict[str, str]:
        row = self._record(workspace_id, asset_id)
        return {"root_id": row["root_id"], "relative_path": row["relative_path"]}

    def describe(self, workspace_id: str, asset_id: str) -> dict:
        row = self._record(workspace_id, asset_id)
        with closing(self._connect()) as db:
            tags = [item["tag"] for item in db.execute(
                "SELECT tag FROM asset_tag WHERE asset_id=? ORDER BY tag", (asset_id,))]
        return {**self._asset(row).model_dump(mode="json"), "tags": tags,
                "source": {"root_id": row["root_id"],
                           "relative_path": row["relative_path"]}}

    def list(self, workspace_id: str, *, keyword: str = "", media_type: str = "",
             tag: str = "", offset: int = 0, limit: int = 30) -> tuple[list[dict], int]:
        if (not _id(workspace_id) or not isinstance(keyword, str) or len(keyword) > 100
                or not isinstance(media_type, str) or len(media_type) > 256
                or not isinstance(tag, str) or len(tag) > 40
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 200):
            raise AssetUnavailable("Invalid asset query")
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM registered_asset WHERE workspace_id=? "
                              "ORDER BY created_at DESC, asset_id DESC", (workspace_id,)).fetchall()
            tags = db.execute("SELECT t.asset_id,t.tag FROM asset_tag t JOIN registered_asset a "
                              "ON a.asset_id=t.asset_id WHERE a.workspace_id=? ORDER BY t.tag",
                              (workspace_id,)).fetchall()
        by_asset: dict[str, list[str]] = {}
        for item in tags:
            by_asset.setdefault(item["asset_id"], []).append(item["tag"])
        needle = keyword.strip().casefold()
        results = []
        for row in rows:
            labels = by_asset.get(row["asset_id"], [])
            if media_type and row["media_type"] != media_type:
                continue
            if tag and tag.casefold() not in {label.casefold() for label in labels}:
                continue
            if needle and needle not in " ".join((row["original_name"], row["relative_path"],
                                                  *labels)).casefold():
                continue
            results.append({**self._asset(row).model_dump(mode="json"), "tags": labels,
                            "source": {"root_id": row["root_id"],
                                       "relative_path": row["relative_path"]}})
        return results[offset:offset + limit], len(results)

    def set_tags(self, workspace_id: str, asset_id: str, tags: list[str]) -> list[str]:
        if (not isinstance(tags, list) or len(tags) > 16
                or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 40
                       or any(ord(char) < 32 for char in tag) for tag in tags)):
            raise AssetUnavailable("Invalid asset tags")
        normalized = list(dict.fromkeys(tag.strip() for tag in tags))
        if len({tag.casefold() for tag in normalized}) != len(normalized):
            raise AssetUnavailable("Duplicate asset tag")
        self._record(workspace_id, asset_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM asset_tag WHERE asset_id=?", (asset_id,))
                db.executemany("INSERT INTO asset_tag(asset_id,tag) VALUES(?,?)",
                               [(asset_id, tag) for tag in normalized])
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return normalized

    def facets(self, workspace_id: str) -> dict[str, list[str]]:
        if not _id(workspace_id):
            raise AssetUnavailable("Invalid workspace")
        with closing(self._connect()) as db:
            media = [row[0] for row in db.execute(
                "SELECT DISTINCT media_type FROM registered_asset WHERE workspace_id=? "
                "ORDER BY media_type", (workspace_id,))]
            tags = [row[0] for row in db.execute(
                "SELECT DISTINCT t.tag FROM asset_tag t JOIN registered_asset a "
                "ON a.asset_id=t.asset_id WHERE a.workspace_id=? ORDER BY t.tag",
                (workspace_id,))]
        return {"media_types": media, "tags": tags}

    @contextmanager
    def stage_verified(self, workspace_id: str, asset_id: str, *,
                       revision: int, blob_hash: str) -> Iterator[Path]:
        """Yield bytes verified against the *current* asset identity.

        Referenced sources are copied to a private temporary file before the
        path is yielded. A changed or missing source never yields an old blob.
        The caller must finish using a referenced path within this context.
        """
        row = self._record(workspace_id, asset_id)
        if (type(revision) is not int or revision != row["revision"]
                or blob_hash != row["blob_hash"]):
            raise AssetChanged("Asset revision or hash changed")
        if row["storage_mode"] == "managed":
            path = self._blob(row["blob_hash"])
            if path.is_symlink() or not path.is_file() or _digest(path) != (
                row["blob_hash"], row["size_bytes"]
            ):
                raise AssetChanged("Managed blob is unavailable or corrupt")
            current = self._record(workspace_id, asset_id)
            if (current["revision"], current["blob_hash"]) != (revision, blob_hash):
                raise AssetChanged("Asset changed during verification")
            yield path
            return
        source, _ = self._source(row["root_id"], row["relative_path"])
        with tempfile.NamedTemporaryFile(dir=self.blob_root, prefix=".stage-",
                                         suffix=".tmp", delete=False) as stream:
            staged = Path(stream.name)
        try:
            digest, size = _copy_snapshot(source, staged)
            if (digest, size) != (row["blob_hash"], row["size_bytes"]):
                raise AssetChanged("Referenced source changed")
            current = self._record(workspace_id, asset_id)
            if (current["revision"], current["blob_hash"]) != (revision, blob_hash):
                raise AssetChanged("Asset changed during verification")
            yield staged
        finally:
            staged.unlink(missing_ok=True)
