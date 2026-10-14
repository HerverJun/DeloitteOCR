"""Workspace-scoped asset library HTTP endpoints."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import sqlite3
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from packages.contracts.models import AssetSetItem, Error, SourceLocator
from packages.contracts.uploads import UploadBinding, UploadStatus
from workbench_platform.assets.registry import AssetChanged, AssetRegistry, AssetUnavailable, UploadConflict, UploadMissing
from workbench_platform.assets.sets import AssetSetConflict, AssetSetMissing, AssetSetStore
from workbench_platform.workspaces.routes import ID, _loopback_host, _same_loopback_origin

router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/assets", tags=["assets"])
MAX_UPLOAD = 128 * 1024 * 1024


def configure_assets(app) -> None:
    configured = os.environ.get("WORKBENCH_DATA_DIR")
    if not configured:
        app.state.asset_library = None
        return
    root = Path(configured).resolve()
    uploads, blobs = root / "asset-uploads", root / "asset-blobs"
    uploads.mkdir(exist_ok=True)
    blobs.mkdir(exist_ok=True)
    data_root = root / "data-workbench"
    roots = {"uploads": uploads}
    if data_root.is_dir():
        roots["data"] = data_root
    registry = AssetRegistry(root / "assets.sqlite3", blobs, roots)
    app.state.asset_library = (registry, AssetSetStore(registry), uploads)
    data = getattr(app.state, 'data_workbench', None)
    if data is not None:
        data.asset_registry = registry


def library(request: Request, workspace_id: str, *, write: bool = False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, "请从本机平台页面操作")
    value = getattr(request.app.state, "asset_library", None)
    store = getattr(request.app.state, "workspace_store", None)
    if value is None or store is None:
        raise HTTPException(503, "资料库存储尚未配置")
    if not ID.fullmatch(workspace_id) or store.get_workspace(workspace_id) is None:
        raise HTTPException(404, "工作空间不存在")
    return value


async def json_body(request: Request, maximum: int = 4096):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(400, "请提交 JSON 参数")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise HTTPException(413, "参数过大")
        raw.extend(chunk)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "参数无效") from None


@router.get("")
def list_assets(workspace_id: str, request: Request, keyword: str = "", media_type: str = "",
                tag: str = "", offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=200)):
    registry, _, _ = library(request, workspace_id)
    try:
        items, total = registry.list(workspace_id, keyword=keyword, media_type=media_type,
                                     tag=tag, offset=offset, limit=limit)
    except AssetUnavailable:
        raise HTTPException(400, "资料查询参数无效") from None
    return {"items": items, "total": total, "next_offset": offset + limit if offset + limit < total else None,
            "facets": registry.facets(workspace_id)}


@router.post("/uploads", status_code=201)
async def upload_asset(workspace_id: str, request: Request,
                       name: str = Query(..., min_length=1, max_length=240)):
    registry, _, uploads = library(request, workspace_id, write=True)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                *(f"LPT{n}" for n in range(1, 10))}
    if (name != name.strip() or name in {".", ".."} or name.endswith(".")
            or name.split(".", 1)[0].upper() in reserved
            or any(char in name for char in ("/", "\\", ":", "\x00"))
            or any(ord(char) < 32 for char in name)):
        raise HTTPException(400, "文件名无效")
    folder = uploads / ("u" + uuid.uuid4().hex)
    folder.mkdir()
    source = folder / name
    size = 0
    try:
        with source.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "文件超过 128 MB")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if size == 0:
            raise HTTPException(400, "文件为空")
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        item = registry.register(workspace_id, "uploads", f"{folder.name}/{name}",
                                 media_type=media_type, storage_mode="managed")
        return registry.describe(workspace_id, item.asset_id)
    except AssetUnavailable:
        source.unlink(missing_ok=True)
        folder.rmdir()
        raise HTTPException(400, "文件无法登记") from None
    except BaseException:
        source.unlink(missing_ok=True)
        folder.rmdir()
        raise


def upload_error(status, code, message, request_id, *, retryable=False):
    identity = request_id if ID.fullmatch(request_id) else "u" + uuid.uuid4().hex
    error = Error(schema_version="1.0", code=code, message=message, retryable=retryable,
                  request_id=identity, details={})
    return JSONResponse(status_code=status, content=error.model_dump(mode="json"))


@router.get("/uploads/{request_id}", response_model=UploadStatus)
def get_upload(workspace_id: str, request_id: str, request: Request):
    if not ID.fullmatch(request_id) or request.query_params:
        return upload_error(400, "VALIDATION_FAILED", "上传请求身份或查询参数无效", request_id)
    try:
        registry, _, _ = library(request, workspace_id)
        return registry.upload_status(workspace_id, request_id)
    except HTTPException as error:
        return upload_error(error.status_code, "NOT_FOUND" if error.status_code == 404 else
            "APP_UNAVAILABLE" if error.status_code == 503 else "VALIDATION_FAILED", str(error.detail), request_id)
    except UploadMissing:
        return upload_error(404, "NOT_FOUND", "尚未接收此上传请求；可以按原身份提交原文件", request_id)
    except (AssetUnavailable, sqlite3.Error, OSError, ValueError):
        return upload_error(503, "APP_UNAVAILABLE", "上传状态暂不可读取；请保留原请求身份并查询恢复", request_id, retryable=True)


@router.post("/uploads/{request_id}", response_model=UploadStatus, status_code=201)
async def upload_recoverable(workspace_id: str, request_id: str, request: Request,
                             name: str = Query(..., min_length=1, max_length=240),
                             sha256: str = Query(...), size_bytes: int = Query(..., ge=1)):
    try:
        registry, _, uploads = library(request, workspace_id, write=True)
    except HTTPException as error:
        return upload_error(error.status_code, "NOT_FOUND" if error.status_code == 404 else
            "APP_UNAVAILABLE" if error.status_code == 503 else "VALIDATION_FAILED", str(error.detail), request_id)
    permitted = {"name", "sha256", "size_bytes"}
    if (set(request.query_params) != permitted
            or any(len(request.query_params.getlist(key)) != 1 for key in permitted)
            or request.query_params["size_bytes"] != str(size_bytes)):
        return upload_error(400, "VALIDATION_FAILED", "上传参数必须完整且不重复", request_id)
    if size_bytes > MAX_UPLOAD:
        return upload_error(413, "VALIDATION_FAILED", "文件超过 128 MB", request_id)
    try:
        binding = UploadBinding(schema_version="1.0", workspace_id=workspace_id, request_id=request_id,
                                name=name, sha256=sha256, size_bytes=size_bytes)
    except ValidationError:
        return upload_error(400, "VALIDATION_FAILED", "上传文件名、内容身份或大小无效", request_id)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/octet-stream":
        return upload_error(400, "VALIDATION_FAILED", "请提交原文件二进制内容", request_id)
    folder = uploads / (".incoming-" + uuid.uuid4().hex)
    staged = folder / "body"
    created_folder = False
    try:
        await run_in_threadpool(registry.reserve_upload, binding)
        folder.mkdir()
        created_folder = True
        size, digest = 0, hashlib.sha256()
        with staged.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD or size > binding.size_bytes:
                    await run_in_threadpool(registry.reject_upload, binding, "文件实际大小与原上传身份不符")
                    return upload_error(413 if size > MAX_UPLOAD else 409, "INTEGRITY_MISMATCH",
                                        "文件实际大小与原上传身份不符；请重试原文件", request_id)
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if (size, digest.hexdigest()) != (binding.size_bytes, binding.sha256):
            await run_in_threadpool(registry.reject_upload, binding, "文件实际内容与原上传身份不符")
            return upload_error(409, "INTEGRITY_MISMATCH", "文件实际内容与原上传身份不符；请重试原文件", request_id)
        media_type = mimetypes.guess_type(binding.name)[0] or "application/octet-stream"
        status, created = await run_in_threadpool(registry.commit_upload, binding, staged, media_type=media_type)
        return JSONResponse(status_code=201 if created else 200, content=status.model_dump(mode="json"))
    except UploadConflict:
        return upload_error(409, "IDEMPOTENCY_CONFLICT", "此上传请求已绑定其他文件；保留原请求，不能替换内容", request_id)
    except AssetChanged:
        return upload_error(409, "INTEGRITY_MISMATCH", "上传内容或固定原件需核实；请查询原请求后恢复", request_id)
    except (AssetUnavailable, sqlite3.Error, OSError):
        return upload_error(503, "APP_UNAVAILABLE", "上传未完成；请保留原文件与请求身份，先查询状态再恢复", request_id,
                            retryable=True)
    finally:
        if created_folder:
            staged.unlink(missing_ok=True)
            folder.rmdir()


@router.post("/sets", status_code=201)
async def create_set(workspace_id: str, request: Request):
    registry, sets, _ = library(request, workspace_id, write=True)
    payload = await json_body(request, 16384)
    asset_ids = payload.get("asset_ids") if isinstance(payload, dict) else None
    if (not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 200
            or any(not isinstance(value, str) for value in asset_ids)
            or len(set(asset_ids)) != len(asset_ids)):
        raise HTTPException(400, "请选择 1 至 200 份不重复的资料")
    try:
        items = []
        for asset_id in asset_ids:
            asset = registry.get(workspace_id, asset_id)
            locator = SourceLocator(schema_version="1.0", kind="file", asset_id=asset.asset_id,
                                    asset_revision=asset.revision, blob_hash=asset.blob_hash,
                                    precision="unknown")
            items.append(AssetSetItem(schema_version="1.0", item_id="i" + uuid.uuid4().hex,
                                      asset_id=asset.asset_id, asset_revision=asset.revision,
                                      blob_hash=asset.blob_hash, source_locator=locator))
        return sets.create(workspace_id, items).model_dump(mode="json")
    except (AssetUnavailable, AssetSetConflict):
        raise HTTPException(409, "资料已变化或不可用，请刷新后重新选择") from None


@router.get("/sets/{set_id}")
def get_set(workspace_id: str, set_id: str, request: Request, revision: int = Query(1, ge=1)):
    registry, sets, _ = library(request, workspace_id)
    try:
        asset_set = sets.get(workspace_id, set_id, revision)
        return {**asset_set.model_dump(mode="json"),
                "members": [registry.describe(workspace_id, item.asset_id) for item in asset_set.items]}
    except (AssetSetMissing, AssetSetConflict):
        raise HTTPException(404, "输入集合不存在") from None


@router.get("/{asset_id}")
def get_asset(workspace_id: str, asset_id: str, request: Request):
    registry, _, _ = library(request, workspace_id)
    try:
        return registry.describe(workspace_id, asset_id)
    except AssetUnavailable:
        raise HTTPException(404, "资料不存在") from None


@router.put("/{asset_id}/tags")
async def update_tags(workspace_id: str, asset_id: str, request: Request):
    registry, _, _ = library(request, workspace_id, write=True)
    payload = await json_body(request)
    try:
        tags = registry.set_tags(workspace_id, asset_id,
                                 payload.get("tags") if isinstance(payload, dict) else None)
        return {"tags": tags}
    except AssetUnavailable:
        raise HTTPException(400, "标签无效或资料不存在") from None


@router.get("/{asset_id}/content")
def asset_content(workspace_id: str, asset_id: str, request: Request,
                  revision: int = Query(..., ge=1), download: bool = False):
    registry, _, _ = library(request, workspace_id)
    stack = ExitStack()
    try:
        item = registry.get(workspace_id, asset_id)
        path = stack.enter_context(registry.stage_verified(
            workspace_id, asset_id, revision=revision, blob_hash=item.blob_hash))
    except AssetUnavailable:
        stack.close()
        raise HTTPException(409, "资料原件已变化或不可用") from None

    def chunks():
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk
        finally:
            stack.close()

    inline_types = {"application/pdf", "text/plain", "image/png", "image/jpeg", "image/gif", "image/webp"}
    disposition = "attachment" if download or item.media_type not in inline_types else "inline"
    return StreamingResponse(chunks(), media_type=item.media_type,
                             headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.original_name)}",
                                      "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
