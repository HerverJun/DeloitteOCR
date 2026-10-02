"""User-facing OCR project discovery and workspace association."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
from dataclasses import replace
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.jobs.native_ocr import OcrUnavailable
from workbench_platform.jobs.ocr_navigation import NavigationUnavailable, issue_navigation
from workbench_platform.store.workspaces import RevisionConflict
from workbench_platform.workspaces.routes import ID, _loopback_host, _same_loopback_origin

router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/ocr", tags=["ocr-projects"])
NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class OcrProjects:
    def __init__(self, jobs):
        self.jobs = jobs

    def open_workbench(self, launch_id: str) -> dict:
        receipt = self.jobs.launches.get("ocr", launch_id)
        # Native service identity alone does not establish a browser session.
        # Verify the administrator-provided credential against the live service
        # before using the native launcher's existing fragment handoff.
        self._request("GET", "/api/state")
        token = self.jobs._token()
        current = self.jobs.launches.get("ocr", launch_id)
        if (current.instance_id != receipt.instance_id or current.base_url != receipt.base_url
                or not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,512}", token) is None):
            raise OcrUnavailable()
        return {"url": receipt.base_url.rstrip("/") + "/#token=" + token}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        receipt = self.jobs.launches.current("ocr")
        parsed = urlsplit(receipt.base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
                or parsed.netloc != f"127.0.0.1:{parsed.port}" or parsed.path not in ("", "/")):
            raise OcrUnavailable()
        token = self.jobs._token()
        if (not isinstance(token, str) or not 32 <= len(token) <= 512
                or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None):
            raise OcrUnavailable()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload else None
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=5)
        try:
            connection.request(method, path, body=body,
                               headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                        **({"Content-Type": "application/json"} if body else {})})
            response = connection.getresponse()
            if response.status != 200 or int(response.getheader("Content-Length", "0")) > 1_048_576:
                raise OcrUnavailable()
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise OcrUnavailable()
            value = json.loads(raw)
            current = self.jobs.launches.current("ocr")
            if (not isinstance(value, dict) or current.instance_id != receipt.instance_id
                    or current.base_url != receipt.base_url):
                raise OcrUnavailable()
            return value
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()

    def projects(self, workspace_id: str) -> dict:
        if self.jobs.workspaces.get_workspace(workspace_id) is None:
            raise KeyError("Workspace not found")
        state = self._request("GET", "/api/state")
        rows = state.get("projects")
        if not isinstance(rows, list) or len(rows) > 10_000:
            raise OcrUnavailable()
        links = {link.module_project_id: link for link in self.jobs.workspaces.list_links(workspace_id)
                 if link.app_id == "ocr"}
        items = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not NATIVE_ID.fullmatch(row["id"])
                    or not isinstance(row.get("name"), str) or not row["name"].strip()):
                raise OcrUnavailable()
            link = links.get(row["id"])
            items.append({"project_id": row["id"], "name": row["name"],
                          "link_id": link.link_id if link else None,
                          "role": link.role if link else None})
        return {"items": items}

    def link(self, workspace_id: str, project_id: str) -> dict:
        if not isinstance(project_id, str) or not NATIVE_ID.fullmatch(project_id):
            raise ValueError("Invalid native project")
        listing = self.projects(workspace_id)["items"]
        project = next((item for item in listing if item["project_id"] == project_id), None)
        if project is None:
            raise KeyError("Native project not found")
        if project["link_id"]:
            return project
        workspace = self.jobs.workspaces.get_workspace(workspace_id)
        link = self.jobs.workspaces.add_link(workspace_id, "ocr", project_id,
                                             role="owner", expected_revision=workspace.revision)
        return {**project, "link_id": link.link_id, "role": link.role}

    def create(self, workspace_id: str, name: str) -> dict:
        if (not isinstance(name, str) or not name.strip() or len(name) > 120
                or "\x00" in name):
            raise ValueError("Invalid project name")
        if self.jobs.workspaces.get_workspace(workspace_id) is None:
            raise KeyError("Workspace not found")
        project = self._request("POST", "/api/projects", {"name": name.strip()})
        project_id = project.get("id")
        if not isinstance(project_id, str) or not NATIVE_ID.fullmatch(project_id):
            raise OcrUnavailable()
        return self.link(workspace_id, project_id)

    def open(self, workspace_id: str, link_id: str, platform_origin: str) -> dict:
        context = replace(self.jobs._context(workspace_id, "ocr", link_id),
                          return_route="/apps/ocr/workflow")
        if not any(item["project_id"] == context.module_project_id
                   for item in self.projects(workspace_id)["items"]):
            raise KeyError("Linked native project no longer exists")
        receipt = self.jobs.launches.current("ocr")
        address = issue_navigation(context, platform_origin=platform_origin,
                                   launch_nonce=receipt.handshake_nonce)
        return {"url": address}


def _service(request: Request, workspace_id: str, *, write: bool = False) -> OcrProjects:
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, "请从本机平台页面操作")
    if not ID.fullmatch(workspace_id):
        raise HTTPException(400, "工作空间标识无效")
    jobs = getattr(request.app.state, "job_service", None)
    if jobs is None:
        raise HTTPException(503, "OCR 项目服务尚未配置")
    return OcrProjects(jobs)


async def _body(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(400, "请提交 JSON 参数")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise HTTPException(413, "项目参数过大")
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "项目参数无效") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "项目参数无效")
    return value


def _failed(error: Exception):
    if isinstance(error, KeyError):
        raise HTTPException(404, "项目或工作空间不存在") from None
    if isinstance(error, (OcrUnavailable, LaunchStatus, NavigationUnavailable)):
        raise HTTPException(503, "OCR 应用尚未就绪") from None
    if isinstance(error, (RevisionConflict, sqlite3.IntegrityError)):
        raise HTTPException(409, "项目关联已变化，请刷新后重试") from None
    raise HTTPException(400, "项目请求无效") from None


@router.get("/projects")
async def list_projects(workspace_id: str, request: Request):
    service = _service(request, workspace_id)
    try:
        return await run_in_threadpool(service.projects, workspace_id)
    except (KeyError, OcrUnavailable, LaunchStatus, ValueError) as error:
        _failed(error)


@router.post("/links", status_code=201)
async def link_project(workspace_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"project_id"}:
        raise HTTPException(400, "请选择原生项目")
    try:
        return await run_in_threadpool(service.link, workspace_id, payload["project_id"])
    except (KeyError, OcrUnavailable, LaunchStatus, RevisionConflict,
            sqlite3.IntegrityError, ValueError) as error:
        _failed(error)


@router.post("/projects", status_code=201)
async def create_project(workspace_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"name"}:
        raise HTTPException(400, "请输入项目名称")
    try:
        return await run_in_threadpool(service.create, workspace_id, payload["name"])
    except (KeyError, OcrUnavailable, LaunchStatus, RevisionConflict,
            sqlite3.IntegrityError, ValueError) as error:
        _failed(error)


@router.post("/links/{link_id}/navigation")
async def open_project(workspace_id: str, link_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    if not ID.fullmatch(link_id) or request.query_params:
        raise HTTPException(400, "项目关联无效")
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, "打开项目不接受参数")
    try:
        origin = "http://" + request.headers["host"]
        return await run_in_threadpool(service.open, workspace_id, link_id, origin)
    except (KeyError, OcrUnavailable, NavigationUnavailable, LaunchStatus, ValueError) as error:
        _failed(error)
