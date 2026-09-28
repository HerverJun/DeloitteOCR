"""Workspace artifact catalog and verified downloads."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from workbench_platform.assets.registry import AssetUnavailable
from workbench_platform.assets.routes import json_body, library
from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.artifacts.store import ArtifactUnavailable
from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.jobs.native_classification import ClassificationUnavailable
from workbench_platform.jobs.projection import ProjectionConflict, ProjectionUnavailable

router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/artifacts", tags=["artifacts"])


def configure_artifacts(app) -> None:
    configured = os.environ.get("WORKBENCH_DATA_DIR")
    assets = getattr(app.state, "asset_library", None)
    app.state.artifact_center = (
        ArtifactCenter(Path(configured).resolve() / "artifact-center", assets[0], assets[1],
                       getattr(app.state, "data_workbench", None),
                       getattr(app.state, "classification_handoff", None))
        if configured and assets else None)


def center(request: Request, workspace_id: str, *, write: bool = False) -> ArtifactCenter:
    library(request, workspace_id, write=write)
    value = getattr(request.app.state, "artifact_center", None)
    if value is None:
        raise HTTPException(503, "成果中心尚未配置")
    return value


@router.get("")
def list_artifacts(workspace_id: str, request: Request,
                   offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
    return center(request, workspace_id).list(workspace_id, offset, limit)


@router.post("/data-exports", status_code=201)
async def publish_data_export(workspace_id: str, request: Request):
    value = center(request, workspace_id, write=True)
    payload = await json_body(request)
    if (not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str)
            or not isinstance(payload.get("export_id"), str)
            or set(payload) != {"session_id", "export_id"}):
        raise HTTPException(400, "归集请求无效")
    try:
        return value.publish_data_export(workspace_id, payload["session_id"], payload["export_id"])
    except (AssetUnavailable, AssetSetConflict, ArtifactUnavailable):
        raise HTTPException(409, "资料或成果版本不可用，请刷新后重试") from None


@router.post("/classification-exports", status_code=201)
async def publish_classification_export(workspace_id: str, request: Request):
    value = center(request, workspace_id, write=True)
    payload = await json_body(request)
    if (not isinstance(payload, dict) or set(payload) != {"handoff_id"}
            or not isinstance(payload["handoff_id"], str)):
        raise HTTPException(400, "请选择已完成的分类交接")
    try:
        return await run_in_threadpool(value.publish_classification_result,
                                       workspace_id, payload["handoff_id"])
    except KeyError:
        raise HTTPException(404, "分类交接不存在") from None
    except (ClassificationUnavailable, LaunchStatus):
        raise HTTPException(503, "原生分类服务暂不可用") from None
    except (AssetUnavailable, AssetSetConflict, ArtifactUnavailable,
            ProjectionConflict, ProjectionUnavailable, ValueError):
        raise HTTPException(409, "分类结果或来源版本不可用") from None


@router.get("/{bundle_id}")
def get_artifact_bundle(workspace_id: str, bundle_id: str, request: Request):
    try:
        return center(request, workspace_id).get(workspace_id, bundle_id)
    except ArtifactUnavailable:
        raise HTTPException(404, "成果包不存在") from None


@router.get("/{bundle_id}/files/{name}")
def download_artifact(workspace_id: str, bundle_id: str, name: str, request: Request):
    value = center(request, workspace_id)
    stack = ExitStack()
    try:
        artifact = value.file(workspace_id, bundle_id, name)
        path = stack.enter_context(value.store.stage_download(
            workspace_id, artifact.artifact_id,
            expected_revision=artifact.revision, expected_hash=artifact.blob_hash))
    except ArtifactUnavailable as error:
        stack.close()
        status = 404 if error.code == "NOT_FOUND" else 409
        raise HTTPException(status, "成果不存在或版本已不可用") from None

    def chunks():
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk
        finally:
            stack.close()

    return StreamingResponse(chunks(), media_type=artifact.media_type,
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
