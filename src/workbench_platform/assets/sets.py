"""Immutable, ordered asset-set snapshots with explicit source lineage."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Iterable, Mapping

from pydantic import ValidationError

from packages.contracts.models import AssetSet, AssetSetItem
from .registry import AssetChanged, AssetRegistry, AssetUnavailable

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class AssetSetConflict(ValueError):
    """The requested revision, member or lineage does not match trusted state."""


class AssetSetMissing(ValueError):
    """The requested workspace snapshot is not available."""


def _id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


class AssetSetStore:
    def __init__(self, registry: AssetRegistry):
        if not isinstance(registry, AssetRegistry):
            raise AssetSetConflict("Trusted asset registry is required")
        self.registry = registry
        self.path = Path(registry.db_path)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS asset_set_versions (
                    asset_set_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
                    workspace_id TEXT NOT NULL, manifest_hash TEXT NOT NULL,
                    PRIMARY KEY(asset_set_id, revision));
                CREATE TABLE IF NOT EXISTS asset_set_members (
                    asset_set_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal>=0), item_id TEXT NOT NULL,
                    payload TEXT NOT NULL, parent_set_id TEXT, parent_revision INTEGER,
                    parent_item_id TEXT,
                    PRIMARY KEY(asset_set_id, revision, ordinal),
                    UNIQUE(asset_set_id, revision, item_id),
                    FOREIGN KEY(asset_set_id, revision)
                        REFERENCES asset_set_versions(asset_set_id, revision),
                    CHECK((parent_set_id IS NULL AND parent_revision IS NULL AND parent_item_id IS NULL)
                       OR (parent_set_id IS NOT NULL AND parent_revision IS NOT NULL AND parent_item_id IS NOT NULL)));
                CREATE TRIGGER IF NOT EXISTS asset_set_versions_no_update
                    BEFORE UPDATE ON asset_set_versions BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS asset_set_versions_no_delete
                    BEFORE DELETE ON asset_set_versions BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS asset_set_members_no_update
                    BEFORE UPDATE ON asset_set_members BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS asset_set_members_no_delete
                    BEFORE DELETE ON asset_set_members BEGIN SELECT RAISE(ABORT, 'immutable'); END;
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _items(items: Iterable[AssetSetItem | dict]) -> tuple[AssetSetItem, ...]:
        try:
            result = tuple(AssetSetItem.model_validate(item) for item in items)
        except (ValidationError, TypeError, ValueError):
            raise AssetSetConflict("Invalid asset-set item") from None
        if not 1 <= len(result) <= 10_000 or len({item.item_id for item in result}) != len(result):
            raise AssetSetConflict("Asset-set items are empty, duplicated or too numerous")
        return result

    @staticmethod
    def _parents(parents: Mapping[str, tuple[str, int, str]] | None,
                 item_ids: set[str]) -> dict[str, tuple[str, int, str]]:
        if parents is None:
            return {}
        if not isinstance(parents, Mapping) or not set(parents) <= item_ids:
            raise AssetSetConflict("Invalid derivation target")
        normalized = {}
        for item_id, parent in parents.items():
            if (not isinstance(parent, tuple) or len(parent) != 3
                    or not _id(parent[0]) or type(parent[1]) is not int or parent[1] < 1
                    or not _id(parent[2])):
                raise AssetSetConflict("Invalid derivation source")
            normalized[item_id] = parent
        return normalized

    @staticmethod
    def _manifest(items: tuple[AssetSetItem, ...], parents: dict[str, tuple[str, int, str]]) -> str:
        ordered = [{"item": item.model_dump(mode="json"),
                    "derived_from": parents.get(item.item_id)} for item in items]
        return hashlib.sha256(_canonical({"schema_version": "1.0", "items": ordered}).encode("utf-8")).hexdigest()

    def create(self, workspace_id: str, items: Iterable[AssetSetItem | dict], *,
               parents: Mapping[str, tuple[str, int, str]] | None = None) -> AssetSet:
        return self._write(workspace_id, self._items(items), parents, None, None)

    def revise(self, workspace_id: str, asset_set_id: str, expected_revision: int,
               items: Iterable[AssetSetItem | dict], *,
               parents: Mapping[str, tuple[str, int, str]] | None = None) -> AssetSet:
        if not _id(asset_set_id) or type(expected_revision) is not int or expected_revision < 1:
            raise AssetSetConflict("Invalid set revision")
        return self._write(workspace_id, self._items(items), parents,
                           asset_set_id, expected_revision)

    def _write(self, workspace_id: str, items: tuple[AssetSetItem, ...],
               parents: Mapping[str, tuple[str, int, str]] | None,
               asset_set_id: str | None, expected_revision: int | None) -> AssetSet:
        if not _id(workspace_id):
            raise AssetSetConflict("Invalid workspace")
        lineage = self._parents(parents, {item.item_id for item in items})
        # Verify source bytes before fixing the input manifest. Consumers must
        # verify them again when processing a referenced asset later.
        checked = set()
        for item in items:
            key = (item.asset_id, item.asset_revision, item.blob_hash)
            if key in checked:
                continue
            try:
                with self.registry.stage_verified(workspace_id, item.asset_id,
                                                  revision=item.asset_revision,
                                                  blob_hash=item.blob_hash):
                    pass
            except (AssetChanged, AssetUnavailable):
                raise AssetSetConflict("Asset content is no longer available") from None
            checked.add(key)
        set_id = asset_set_id or "s" + uuid.uuid4().hex
        digest = self._manifest(items, lineage)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if asset_set_id is None:
                    revision = 1
                else:
                    row = db.execute("SELECT MAX(revision) FROM asset_set_versions "
                                     "WHERE asset_set_id=? AND workspace_id=?",
                                     (set_id, workspace_id)).fetchone()
                    if row[0] != expected_revision:
                        raise AssetSetConflict("Asset-set revision changed")
                    revision = expected_revision + 1
                for item in items:
                    row = db.execute("SELECT revision,blob_hash FROM registered_asset "
                                     "WHERE workspace_id=? AND asset_id=?",
                                     (workspace_id, item.asset_id)).fetchone()
                    if row is None or (row["revision"], row["blob_hash"]) != (
                        item.asset_revision, item.blob_hash
                    ):
                        raise AssetSetConflict("Asset identity changed")
                for parent_set_id, parent_revision, parent_item_id in lineage.values():
                    parent = db.execute("SELECT 1 FROM asset_set_members m "
                                        "JOIN asset_set_versions v ON v.asset_set_id=m.asset_set_id "
                                        "AND v.revision=m.revision WHERE v.workspace_id=? "
                                        "AND m.asset_set_id=? AND m.revision=? AND m.item_id=?",
                                        (workspace_id, parent_set_id,
                                         parent_revision, parent_item_id)).fetchone()
                    if parent is None:
                        raise AssetSetConflict("Derivation source is unavailable")
                db.execute("INSERT INTO asset_set_versions VALUES(?,?,?,?)",
                           (set_id, revision, workspace_id, digest))
                for ordinal, item in enumerate(items):
                    parent = lineage.get(item.item_id)
                    db.execute("INSERT INTO asset_set_members VALUES(?,?,?,?,?,?,?,?)",
                               (set_id, revision, ordinal, item.item_id,
                                _canonical(item.model_dump(mode="json")),
                                *(parent if parent else (None, None, None))))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return AssetSet(schema_version="1.0", asset_set_id=set_id, revision=revision,
                        manifest_hash=digest, items=list(items))

    def get(self, workspace_id: str, asset_set_id: str, revision: int) -> AssetSet:
        if (not _id(workspace_id) or not _id(asset_set_id)
                or type(revision) is not int or revision < 1):
            raise AssetSetMissing("Invalid asset-set lookup")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            try:
                row = db.execute("SELECT manifest_hash FROM asset_set_versions "
                                 "WHERE workspace_id=? AND asset_set_id=? AND revision=?",
                                 (workspace_id, asset_set_id, revision)).fetchone()
                if row is None:
                    raise AssetSetMissing("Asset-set revision does not exist")
                members = db.execute("SELECT payload,parent_set_id,parent_revision,parent_item_id "
                                     "FROM asset_set_members WHERE asset_set_id=? "
                                     "AND revision=? ORDER BY ordinal", (asset_set_id, revision)).fetchall()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        try:
            items = tuple(AssetSetItem.model_validate_json(member["payload"])
                          for member in members)
        except (ValidationError, ValueError):
            raise AssetSetConflict("Stored asset-set item is invalid") from None
        parents = {item.item_id: (member["parent_set_id"], member["parent_revision"],
                                  member["parent_item_id"])
                   for item, member in zip(items, members) if member["parent_set_id"] is not None}
        if self._manifest(items, parents) != row["manifest_hash"]:
            raise AssetSetConflict("Stored asset-set manifest changed")
        return AssetSet(schema_version="1.0", asset_set_id=asset_set_id,
                        revision=revision, manifest_hash=row["manifest_hash"], items=list(items))

    def lineage(self, workspace_id: str, asset_set_id: str, revision: int) -> dict[str, tuple[str, int, str]]:
        self.get(workspace_id, asset_set_id, revision)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT item_id,parent_set_id,parent_revision,parent_item_id "
                              "FROM asset_set_members WHERE asset_set_id=? AND revision=? "
                              "AND parent_set_id IS NOT NULL ORDER BY ordinal",
                              (asset_set_id, revision)).fetchall()
        return {row["item_id"]: (row["parent_set_id"], row["parent_revision"],
                                 row["parent_item_id"]) for row in rows}
