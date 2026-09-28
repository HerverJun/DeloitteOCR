"""Native classification project discovery and workspace association."""

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
from workbench_platform.jobs.native_classification import ClassificationUnavailable
from workbench_platform.jobs.classification_navigation import NavigationUnavailable, issue_navigation
from workbench_platform.store.workspaces import RevisionConflict
from workbench_platform.workspaces.routes import ID, _loopback_host, _same_loopback_origin

router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/classification",
                   tags=["classification-projects"])
NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class ClassificationProjects:
    def __init__(self, jobs):
        self.jobs = jobs

    def _request(self, method: str, path: str, payload: dict | None = None):
        receipt = self.jobs.launches.current("classification")
        parsed = urlsplit(receipt.base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
                or parsed.netloc != f"127.0.0.1:{parsed.port}" or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.username):
            raise ClassificationUnavailable()
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if payload is not None else None)
        if body is not None and len(body) > 65536:
            raise ValueError("Classification project definition is too large")
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=5)
        try:
            connection.request(method, path, body=body,
                               headers={"Accept": "application/json", "X-Platform-Request": "1",
                                        **({"Content-Type": "application/json"} if body is not None else {})})
            response = connection.getresponse()
            if response.status not in {200, 201} or int(response.getheader("Content-Length", "0")) > 1_048_576:
                raise ClassificationUnavailable()
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ClassificationUnavailable()
            value = json.loads(raw)
            current = self.jobs.launches.current("classification")
            if (current.instance_id != receipt.instance_id
                    or current.base_url != receipt.base_url):
                raise ClassificationUnavailable()
            return value
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                http.client.HTTPException):
            raise ClassificationUnavailable() from None
        finally:
            connection.close()

    def projects(self, workspace_id: str) -> dict:
        if self.jobs.workspaces.get_workspace(workspace_id) is None:
            raise KeyError("Workspace not found")
        rows = self._request("GET", "/api/v1/projects")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ClassificationUnavailable()
        links = {link.module_project_id: link for link in self.jobs.workspaces.list_links(workspace_id)
                 if link.app_id == "classification"}
        items = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not NATIVE_ID.fullmatch(row["id"])
                    or not isinstance(row.get("name"), str) or not row["name"].strip()
                    or not isinstance(row.get("labels"), list)):
                raise ClassificationUnavailable()
            labels = []
            for label in row["labels"]:
                if (not isinstance(label, dict) or not isinstance(label.get("label_id"), str)
                        or not NATIVE_ID.fullmatch(label["label_id"])
                        or not isinstance(label.get("display_name"), str)):
                    raise ClassificationUnavailable()
                labels.append({"label_id": label["label_id"],
                               "display_name": label["display_name"]})
            link = links.get(row["id"])
            items.append({"project_id": row["id"], "name": row["name"],
                          "labels": labels, "link_id": link.link_id if link else None,
                          "role": link.role if link else None})
        return {"items": items}

    def link(self, workspace_id: str, project_id: str) -> dict:
        if not isinstance(project_id, str) or not NATIVE_ID.fullmatch(project_id):
            raise ValueError("Invalid native project")
        project = next((item for item in self.projects(workspace_id)["items"]
                        if item["project_id"] == project_id), None)
        if project is None:
            raise KeyError("Native project not found")
        if project["link_id"]:
            return project
        workspace = self.jobs.workspaces.get_workspace(workspace_id)
        link = self.jobs.workspaces.add_link(workspace_id, "classification", project_id,
                                             role="owner", expected_revision=workspace.revision)
        return {**project, "link_id": link.link_id, "role": link.role}

    def create(self, workspace_id: str, name: str, categories: list[str]) -> dict:
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or any(ord(char) < 32 for char in name)
                or not isinstance(categories, list) or not 2 <= len(categories) <= 1000):
            raise ValueError("Invalid classification project")
        names = []
        for raw in categories:
            if (not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 60
                    or any(ord(char) < 32 for char in raw)):
                raise ValueError("Invalid category name")
            names.append(raw.strip())
        if len({item.casefold() for item in names}) != len(names):
            raise ValueError("Duplicate category name")
        if self.jobs.workspaces.get_workspace(workspace_id) is None:
            raise KeyError("Workspace not found")
        native = self._request("POST", "/api/v1/projects", {"name": name.strip(),
            "labels": [{"label_id": f"class_{index + 1}", "display_name": item,
                        "definition": item} for index, item in enumerate(names)]})
        if (not isinstance(native, dict) or not isinstance(native.get("id"), str)
                or not NATIVE_ID.fullmatch(native["id"])):
            raise ClassificationUnavailable()
        return self.link(workspace_id, native["id"])

    def open(self, workspace_id: str, link_id: str, platform_origin: str) -> dict:
        context = replace(self.jobs._context(workspace_id, "classification", link_id),
                          return_route="/apps/classification/workflow")
        if not any(item["project_id"] == context.module_project_id
                   for item in self.projects(workspace_id)["items"]):
            raise KeyError("Linked native project no longer exists")
        receipt = self.jobs.launches.current("classification")
        address = issue_navigation(context, platform_origin=platform_origin,
                                   launch_nonce=receipt.handshake_nonce)
        return {"url": address}


def _service(request: Request, workspace_id: str, *, write: bool = False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, "请从本机平台页面操作")
    if not ID.fullmatch(workspace_id) or request.query_params:
        raise HTTPException(400, "工作空间请求无效")
    jobs = getattr(request.app.state, "job_service", None)
    if jobs is None:
        raise HTTPException(503, "分类项目服务尚未配置")
    return ClassificationProjects(jobs)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate field")
        value[key] = item
    return value


async def _body(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(400, "请提交 JSON 参数")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 65536:
            raise HTTPException(413, "项目参数过大")
        raw.extend(chunk)
    try:
        payload = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "项目参数无效") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "项目参数无效")
    return payload


def _failed(error: Exception):
    if isinstance(error, KeyError):
        raise HTTPException(404, "项目或工作空间不存在") from None
    if isinstance(error, (ClassificationUnavailable, LaunchStatus, NavigationUnavailable)):
        raise HTTPException(503, "分类应用尚未就绪") from None
    if isinstance(error, (RevisionConflict, sqlite3.IntegrityError)):
        raise HTTPException(409, "项目关联已变化，请刷新后重试") from None
    raise HTTPException(400, "项目请求无效") from None


@router.get("/projects")
async def list_projects(workspace_id: str, request: Request):
    service = _service(request, workspace_id)
    try:
        return await run_in_threadpool(service.projects, workspace_id)
    except (KeyError, ClassificationUnavailable, LaunchStatus, ValueError) as error:
        _failed(error)


@router.post("/links", status_code=201)
async def link_project(workspace_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"project_id"}:
        raise HTTPException(400, "请选择原生项目")
    try:
        return await run_in_threadpool(service.link, workspace_id, payload["project_id"])
    except (KeyError, ClassificationUnavailable, LaunchStatus, RevisionConflict,
            sqlite3.IntegrityError, ValueError) as error:
        _failed(error)


@router.post("/projects", status_code=201)
async def create_project(workspace_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"name", "categories"}:
        raise HTTPException(400, "请输入项目名称和类别")
    try:
        return await run_in_threadpool(service.create, workspace_id,
                                       payload["name"], payload["categories"])
    except (KeyError, ClassificationUnavailable, LaunchStatus, RevisionConflict,
            sqlite3.IntegrityError, ValueError) as error:
        _failed(error)


@router.post("/links/{link_id}/navigation")
async def open_project(workspace_id: str, link_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    if not ID.fullmatch(link_id):
        raise HTTPException(400, "项目关联无效")
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, "打开项目不接受参数")
    try:
        origin = "http://" + request.headers["host"]
        return await run_in_threadpool(service.open, workspace_id, link_id, origin)
    except (KeyError, ClassificationUnavailable, NavigationUnavailable, LaunchStatus,
            ValueError) as error:
        _failed(error)
