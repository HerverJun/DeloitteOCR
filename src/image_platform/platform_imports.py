"""Durable item-addressed imports from a trusted local platform."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .datasets import register_asset
from .images import SUPPORTED, file_sha
from .store import dump, now, uid

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,119}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
MAX_MANIFEST_BYTES = 1_048_576
MAX_ITEM_BYTES = 100 * 1024 * 1024


class ImportConflict(ValueError):
    """A request key already belongs to a different immutable manifest."""


class AlreadyAccepted(Exception):
    """A competing transaction committed this item first."""


def _identity(project_id, request_id):
    if (not isinstance(project_id, str) or not ID.fullmatch(project_id)
            or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
        raise ValueError("Invalid classification import identity")


def _manifest(value, labels):
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "asset_set_id", "asset_set_revision", "manifest_hash",
            "import_mode", "items"} or value["schema_version"] != "1.0"
            or value["import_mode"] != "copy"
            or not isinstance(value["asset_set_id"], str)
            or not ID.fullmatch(value["asset_set_id"])
            or type(value["asset_set_revision"]) is not int or value["asset_set_revision"] < 1
            or not isinstance(value["manifest_hash"], str)
            or not HASH.fullmatch(value["manifest_hash"])):
        raise ValueError("Invalid classification import manifest")
    items = value["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError("Invalid classification import size")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {
                "item_id", "asset_id", "blob_hash", "size_bytes", "name", "label_id"}
                or any(not isinstance(item[key], str) or not ID.fullmatch(item[key])
                       for key in ("item_id", "asset_id"))
                or not isinstance(item["blob_hash"], str) or not HASH.fullmatch(item["blob_hash"])
                or type(item["size_bytes"]) is not int
                or not 1 <= item["size_bytes"] <= MAX_ITEM_BYTES
                or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 256
                or any(char in item["name"] for char in ("/", "\\", "\x00"))
                or Path(item["name"]).suffix.lower() not in SUPPORTED
                or item["label_id"] is not None and item["label_id"] not in labels):
            raise ValueError("Invalid classification import item")
        if item["item_id"] in seen:
            raise ValueError("Duplicate classification import item")
        seen.add(item["item_id"])
    return value


class PlatformImports:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()

    def begin(self, project_id, request_id, payload):
        _identity(project_id, request_id)
        project = self.store.project(project_id)
        labels = {item["label_id"] for item in project["labels"]}
        manifest = _manifest(payload, labels)
        canonical = dump(manifest)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.store.connect(write=True) as db:
            previous = db.execute("SELECT payload_hash FROM platform_import_requests "
                                  "WHERE project_id=? AND request_id=?",
                                  (project_id, request_id)).fetchone()
            if previous:
                if previous["payload_hash"] != digest:
                    raise ImportConflict("导入请求标识已绑定另一份清单")
            else:
                db.execute("INSERT INTO platform_import_requests VALUES(?,?,?,?,?)",
                           (project_id, request_id, digest, canonical, now()))
                for ordinal, item in enumerate(manifest["items"]):
                    db.execute("INSERT INTO platform_import_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (project_id, request_id, item["item_id"], ordinal,
                                item["asset_id"], item["blob_hash"], item["size_bytes"],
                                item["name"], item["label_id"], "pending", None, None))
        return self.status(project_id, request_id)

    def status(self, project_id, request_id):
        _identity(project_id, request_id)
        with self.store.connect() as db:
            row = db.execute("SELECT payload_hash FROM platform_import_requests "
                             "WHERE project_id=? AND request_id=?",
                             (project_id, request_id)).fetchone()
            if row is None:
                raise LookupError("导入请求不存在")
            records = db.execute("SELECT i.*,a.project_id AS actual_project,a.sha256 AS actual_hash,"
                                 "a.source_id AS actual_source,a.archived AS actual_archived "
                                 "FROM platform_import_items i "
                                 "LEFT JOIN assets a ON a.id=i.target_asset_id "
                                 "WHERE i.project_id=? AND i.request_id=? ORDER BY i.ordinal",
                                 (project_id, request_id)).fetchall()
        items = []
        for record in records:
            accepted = (record["status"] == "imported"
                        and record["actual_project"] == project_id
                        and record["actual_hash"] == record["blob_hash"]
                        and record["actual_source"] == record["platform_asset_id"]
                        and record["actual_archived"] == 0)
            items.append({"item_id": record["item_id"],
                          "status": "imported" if accepted else
                                    "missing" if record["status"] == "imported" else "pending",
                          "target_asset_id": record["target_asset_id"] if accepted else None})
        states = {item["status"] for item in items}
        state = ("unknown" if "missing" in states else "completed" if states == {"imported"}
                 else "accepted" if "imported" in states else "prepared")
        return {"request_id": request_id, "payload_hash": row["payload_hash"],
                "state": state, "items": items}

    def expected(self, project_id, request_id, item_id):
        _identity(project_id, request_id)
        if not isinstance(item_id, str) or not ID.fullmatch(item_id):
            raise ValueError("Invalid import item")
        with self.store.connect() as db:
            row = db.execute("SELECT blob_hash,size_bytes,name,label_id,platform_asset_id,status "
                             "FROM platform_import_items WHERE project_id=? AND request_id=? AND item_id=?",
                             (project_id, request_id, item_id)).fetchone()
        if row is None:
            raise LookupError("导入条目不存在")
        return dict(row)

    def accept(self, project_id, request_id, item_id, temporary: Path):
        with self.lock:
            expected = self.expected(project_id, request_id, item_id)
            if expected["status"] == "imported":
                return self.status(project_id, request_id)
            if (temporary.stat().st_size != expected["size_bytes"]
                    or file_sha(temporary) != expected["blob_hash"]):
                raise ValueError("图片内容与导入清单不一致")
            suffix = Path(expected["name"]).suffix.lower()
            folder = self.store.settings.data_dir / "uploads" / project_id
            folder.mkdir(exist_ok=True)
            destination = folder / f"platform-{request_id}-{item_id}{suffix}"
            created = False
            if not destination.exists():
                try:
                    with temporary.open("rb") as original, destination.open("xb") as target:
                        created = True
                        shutil.copyfileobj(original, target, 1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                except FileExistsError:
                    pass
                except BaseException:
                    if created:
                        destination.unlink(missing_ok=True)
                    raise
            if (not destination.is_file() or destination.is_symlink()
                    or destination.stat().st_size != expected["size_bytes"]
                    or file_sha(destination) != expected["blob_hash"]):
                raise ValueError("已暂存图片与导入清单不一致")

            def accepted(db, asset_id):
                changed = db.execute("UPDATE platform_import_items SET status='imported',"
                                     "target_asset_id=?,accepted_at=? WHERE project_id=? "
                                     "AND request_id=? AND item_id=? AND status='pending'",
                                     (asset_id, now(), project_id, request_id, item_id)).rowcount
                if changed != 1:
                    raise AlreadyAccepted()
                db.execute("UPDATE assets SET name=? WHERE id=?",
                           (expected["name"], asset_id))

            try:
                _, inserted = register_asset(self.store, project_id, destination,
                                             expected["label_id"],
                                             source_id=expected["platform_asset_id"],
                                             on_accept=accepted)
                if not inserted and self.expected(project_id, request_id, item_id)["status"] != "imported":
                    raise ValueError("导入映射未与资产一同提交")
            except AlreadyAccepted:
                pass
            except BaseException:
                if created and self.store.one("SELECT id FROM assets WHERE path=?", (str(destination),)) is None:
                    destination.unlink(missing_ok=True)
                raise
            return self.status(project_id, request_id)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def register_platform_import_routes(app, store):
    imports = PlatformImports(store)
    app.state.platform_imports = imports

    @app.post("/api/v1/platform/imports/{project_id}/{request_id}", status_code=201)
    async def begin_import(project_id: str, request_id: str, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(400, "JSON import manifest required")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_MANIFEST_BYTES:
                raise HTTPException(413, "Import manifest is too large")
            raw.extend(chunk)
        try:
            payload = json.loads(raw, object_pairs_hook=_unique)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid import manifest") from None
        return await run_in_threadpool(imports.begin, project_id, request_id, payload)

    @app.get("/api/v1/platform/imports/{project_id}/{request_id}")
    def import_status(project_id: str, request_id: str):
        return imports.status(project_id, request_id)

    @app.put("/api/v1/platform/imports/{project_id}/{request_id}/items/{item_id}")
    async def import_item(project_id: str, request_id: str, item_id: str, request: Request):
        expected = imports.expected(project_id, request_id, item_id)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/octet-stream":
            raise HTTPException(400, "Binary image required")
        length = request.headers.get("content-length", "")
        if not length.isdecimal() or int(length) != expected["size_bytes"]:
            raise HTTPException(400, "Image length differs from manifest")
        inbox = store.settings.data_dir / "uploads" / ".platform-inbox"
        inbox.mkdir(exist_ok=True)
        temporary = inbox / uid("incoming")
        try:
            size, digest = 0, hashlib.sha256()
            with temporary.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > expected["size_bytes"]:
                        raise HTTPException(413, "Image exceeds manifest size")
                    stream.write(chunk)
                    digest.update(chunk)
            if (size, digest.hexdigest()) != (expected["size_bytes"], expected["blob_hash"]):
                raise ValueError("Image content differs from manifest")
            return await run_in_threadpool(imports.accept, project_id, request_id, item_id, temporary)
        finally:
            temporary.unlink(missing_ok=True)
