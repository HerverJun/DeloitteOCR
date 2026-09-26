"""Durable, item-addressed imports from the trusted local platform.

The native image and its platform item mapping commit in one SQLite
transaction. The platform may query a request after any lost HTTP response;
replaying an imported item returns the original native image ID.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from ocr_workbench.imaging import SUPPORTED
from ocr_workbench.store import Conflict, now, uid

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,119}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
MAX_ITEM_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1_048_576


class ImportConflict(Conflict):
    """An existing request key belongs to a different immutable manifest."""


class AlreadyAccepted(Exception):
    """A competing request committed this item while it was being decoded."""


def migrate_v15(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS platform_import_requests (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
        manifest TEXT NOT NULL, created TEXT NOT NULL,
        PRIMARY KEY(project_id, request_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_import_items (
        project_id TEXT NOT NULL, request_id TEXT NOT NULL,
        item_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        asset_id TEXT NOT NULL, blob_hash TEXT NOT NULL,
        size_bytes INTEGER NOT NULL, name TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','imported')),
        target_image_id TEXT, target_version_id TEXT,
        accepted TEXT,
        PRIMARY KEY(project_id, request_id, item_id),
        UNIQUE(project_id, request_id, ordinal),
        FOREIGN KEY(project_id, request_id) REFERENCES platform_import_requests(project_id, request_id)
          ON DELETE CASCADE)""")
    db.execute("CREATE INDEX IF NOT EXISTS platform_import_target ON platform_import_items(target_image_id)")


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _identity(project_id: str, request_id: str) -> None:
    if (not isinstance(project_id, str) or not _ID.fullmatch(project_id)
            or not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)):
        raise ValueError("Invalid import identity")


def _manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "asset_set_id", "asset_set_revision", "manifest_hash", "import_mode", "items"
    } or value["schema_version"] != "1.0" or value["import_mode"] != "copy":
        raise ValueError("Invalid import manifest")
    if (not isinstance(value["asset_set_id"], str) or not _ID.fullmatch(value["asset_set_id"])
            or type(value["asset_set_revision"]) is not int or value["asset_set_revision"] < 1
            or not isinstance(value["manifest_hash"], str) or not _HASH.fullmatch(value["manifest_hash"])):
        raise ValueError("Invalid asset-set identity")
    items = value["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError("Invalid import item count")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "item_id", "asset_id", "blob_hash", "size_bytes", "name"
        }:
            raise ValueError("Invalid import item")
        if (any(not isinstance(item[key], str) or not _ID.fullmatch(item[key])
                for key in ("item_id", "asset_id"))
                or not isinstance(item["blob_hash"], str) or not _HASH.fullmatch(item["blob_hash"])
                or type(item["size_bytes"]) is not int or not 1 <= item["size_bytes"] <= MAX_ITEM_BYTES
                or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 256
                or "/" in item["name"] or "\\" in item["name"] or "\x00" in item["name"]
                or Path(item["name"]).suffix.lower() not in SUPPORTED):
            raise ValueError("Invalid import item identity")
        if item["item_id"] in seen:
            raise ValueError("Duplicate import item")
        seen.add(item["item_id"])
    return value


class PlatformImports:
    def __init__(self, store, import_one):
        self.store, self.import_one = store, import_one

    def begin(self, project_id: str, request_id: str, payload: object) -> dict:
        _identity(project_id, request_id)
        manifest = _manifest(payload)
        canonical = _canonical(manifest)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError("项目不存在")
            old = db.execute("SELECT payload_hash FROM platform_import_requests "
                             "WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
            if old is not None:
                if old["payload_hash"] != digest:
                    raise ImportConflict("请求标识已绑定另一份导入清单")
            else:
                db.execute("INSERT INTO platform_import_requests VALUES(?,?,?,?,?)",
                           (project_id, request_id, digest, canonical, now()))
                for ordinal, item in enumerate(manifest["items"]):
                    db.execute("INSERT INTO platform_import_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (project_id, request_id, item["item_id"], ordinal,
                                item["asset_id"], item["blob_hash"], item["size_bytes"],
                                item["name"], "pending", None, None, None))
        return self.status(project_id, request_id)

    def status(self, project_id: str, request_id: str) -> dict:
        _identity(project_id, request_id)
        with self.store.transaction() as db:
            row = db.execute("SELECT payload_hash FROM platform_import_requests "
                             "WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
            if row is None:
                raise KeyError("导入请求不存在")
            records = db.execute("SELECT i.*, m.project_id AS actual_project, "
                                 "v.image_id AS actual_version_image FROM platform_import_items i "
                                 "LEFT JOIN images m ON m.id=i.target_image_id "
                                 "LEFT JOIN versions v ON v.id=i.target_version_id "
                                 "WHERE i.project_id=? AND i.request_id=? ORDER BY i.ordinal",
                                 (project_id, request_id)).fetchall()
            items = []
            for record in records:
                accepted = (record["status"] == "imported" and
                            record["actual_project"] == project_id and
                            record["actual_version_image"] == record["target_image_id"])
                items.append({"item_id": record["item_id"], "status": (
                    "imported" if accepted else "missing" if record["status"] == "imported" else "pending"),
                    "target_asset_id": record["target_image_id"] if accepted else None,
                    "target_version_id": record["target_version_id"] if accepted else None})
        statuses = {item["status"] for item in items}
        state = ("unknown" if "missing" in statuses else "completed" if statuses == {"imported"}
                 else "accepted" if "imported" in statuses else "prepared")
        return {"request_id": request_id, "payload_hash": row["payload_hash"],
                "state": state, "items": items}

    def expected(self, project_id: str, request_id: str, item_id: str) -> dict:
        _identity(project_id, request_id)
        if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
            raise ValueError("Invalid item identity")
        with self.store.transaction() as db:
            row = db.execute("SELECT blob_hash,size_bytes,name,status FROM platform_import_items "
                             "WHERE project_id=? AND request_id=? AND item_id=?",
                             (project_id, request_id, item_id)).fetchone()
        if row is None:
            raise KeyError("导入条目不存在")
        return dict(row)

    def accept(self, project_id: str, request_id: str, item_id: str, temporary: Path) -> dict:
        expected = self.expected(project_id, request_id, item_id)
        if expected["status"] == "imported":
            return self.status(project_id, request_id)

        def on_accept(db, image_id, version_id):
            changed = db.execute("UPDATE platform_import_items SET status='imported',"
                                 "target_image_id=?,target_version_id=?,accepted=? "
                                 "WHERE project_id=? AND request_id=? AND item_id=? AND status='pending'",
                                 (image_id, version_id, now(), project_id, request_id, item_id)).rowcount
            if changed != 1:
                raise AlreadyAccepted()

        try:
            self.import_one(project_id, expected["name"], temporary, on_accept=on_accept)
        except AlreadyAccepted:
            pass
        return self.status(project_id, request_id)


def register_platform_import_routes(app, store, import_one) -> None:
    imports = PlatformImports(store, import_one)
    app.state.platform_imports = imports

    @app.post("/api/platform/imports/{project_id}/{request_id}")
    async def begin_import(project_id: str, request_id: str, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("JSON required")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_MANIFEST_BYTES:
                raise ValueError("Import manifest is too large")
            body.extend(chunk)
        try:
            payload = json.loads(body, object_pairs_hook=_unique)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid import manifest") from None
        return await run_in_threadpool(imports.begin, project_id, request_id, payload)

    @app.get("/api/platform/imports/{project_id}/{request_id}")
    def import_status(project_id: str, request_id: str):
        return imports.status(project_id, request_id)

    @app.put("/api/platform/imports/{project_id}/{request_id}/items/{item_id}")
    async def import_item(project_id: str, request_id: str, item_id: str, request: Request):
        expected = imports.expected(project_id, request_id, item_id)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/octet-stream":
            raise ValueError("Binary image required")
        length = request.headers.get("content-length", "")
        if not length.isdecimal() or int(length) != expected["size_bytes"]:
            raise ValueError("Image length differs from manifest")
        inbox = store.root / "inbox"
        inbox.mkdir(exist_ok=True)
        temporary = inbox / ("platform-" + uid())
        try:
            count = 0
            digest = hashlib.sha256()
            with temporary.open("xb") as target:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > expected["size_bytes"] or count > MAX_ITEM_BYTES:
                        raise ValueError("Image exceeds manifest size")
                    digest.update(chunk)
                    target.write(chunk)
            if count != expected["size_bytes"] or digest.hexdigest() != expected["blob_hash"]:
                raise ValueError("Image content differs from manifest")
            return await run_in_threadpool(imports.accept, project_id, request_id, item_id, temporary)
        finally:
            temporary.unlink(missing_ok=True)
