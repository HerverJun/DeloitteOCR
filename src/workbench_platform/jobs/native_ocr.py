"""Packaged OCR native adapter over an already-authorized bridge context.

The caller must consume ContextBridge authorization first, then supply a live
Supervisor verifier and a trusted native session-token provider. Browser input
never chooses a host, credentials, native project, or arbitrary route.
Mirrors adapters/ocr/adapter.py until the shared adapter package is distributed.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import re
from typing import Callable
from urllib.parse import urlsplit

from workbench_platform.bridge.context import ResolvedContext

NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,120}\Z")
MAX_JSON = 8 * 1024 * 1024
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
KNOWN_STATES = frozenset({"queued", "running", "paused", "interrupted", *TERMINAL})


class OcrUnavailable(ValueError):
    def __init__(self):
        super().__init__("OCR adapter is unavailable")


@dataclass(frozen=True)
class Capabilities:
    review: bool
    export: bool
    recognition: bool
    native_extraction: bool
    export_formats: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True)
class TaskStatus:
    native_task_id: str
    native_status: str
    phase: str
    result_id: str | None
    image_id: str
    version_id: str
    error: str | None


@dataclass(frozen=True)
class BatchStatus:
    tasks: tuple[TaskStatus, ...]
    succeeded: int
    failed: int
    cancelled: int
    pending: int
    status: str
    unfinished_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResultArtifact:
    native_result_id: str
    native_task_id: str
    image_id: str
    version_id: str
    selected: bool
    locator: dict[str, object]


def _port(base_url: str) -> int:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
                or not 1 <= port <= 65535 or parsed.netloc != f"127.0.0.1:{port}"
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username):
            raise ValueError()
        return port
    except (TypeError, ValueError):
        raise OcrUnavailable() from None


class OcrAdapter:
    def __init__(self, context: ResolvedContext, *, token: Callable[[], str],
                 live: Callable[[ResolvedContext], bool], timeout: float = 3.0):
        if (not isinstance(context, ResolvedContext) or context.app_id != "ocr"
                or not all(_is_native_id(value) for value in (context.workspace_id, context.link_id,
                                                                context.instance_id, context.module_project_id))
                or context.role not in {"owner", "editor", "viewer"}
                or not callable(token) or not callable(live)
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10):
            raise OcrUnavailable()
        self.context, self.token, self.live, self.timeout = context, token, live, timeout
        self.port = _port(context.base_url)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            if not self.live(self.context):
                raise OcrUnavailable()
            credential = self.token()
        except Exception:
            raise OcrUnavailable() from None
        if (not isinstance(credential, str) or len(credential) < 32 or len(credential) > 512
                or not re.fullmatch(r"[A-Za-z0-9_-]+", credential)):
            raise OcrUnavailable()
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        if payload is not None and len(payload) > 8192:
            raise OcrUnavailable()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=payload,
                               headers={"Authorization": "Bearer " + credential,
                                        "Accept": "application/json",
                                        **({"Content-Type": "application/json"} if payload is not None else {})})
            response = connection.getresponse()
            if response.status != 200 or int(response.getheader("Content-Length", "0")) > MAX_JSON:
                raise OcrUnavailable()
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise OcrUnavailable()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise OcrUnavailable()
            return value
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()

    def capabilities(self) -> Capabilities:
        health = self._request("GET", "/api/health")
        review_only = health.get("review_only")
        queue = health.get("queue")
        if type(review_only) is not bool or not isinstance(queue, dict):
            raise OcrUnavailable()
        recognition = not review_only and queue.get("healthy") is True and health.get("status") == "ready"
        return Capabilities(review=True, export=True, recognition=recognition,
                            native_extraction=recognition,
                            export_formats=("txt", "md", "json", "xlsx", "pdf"),
                            reason=None if recognition else "review_only" if review_only else "recognition_unavailable")

    def snapshot(self) -> dict:
        project_id = self.context.module_project_id
        value = self._request("GET", f"/api/projects/{project_id}")
        if (value.get("unchanged") is not False or not isinstance(value.get("project"), dict)
                or value["project"].get("id") != project_id
                or not all(isinstance(value.get(key), list) for key in ("images", "versions", "tasks", "documents"))):
            raise OcrUnavailable()
        for key in ("images", "tasks", "documents"):
            if any(not isinstance(row, dict) or row.get("project_id") != project_id
                   or not _is_native_id(row.get("id")) for row in value[key]):
                raise OcrUnavailable()
        images = {row["id"] for row in value["images"]}
        versions = {row["id"]: row["image_id"] for row in value["versions"]
                    if isinstance(row, dict) and _is_native_id(row.get("id"))
                    and _is_native_id(row.get("image_id")) and row["image_id"] in images}
        if len(versions) != len(value["versions"]):
            raise OcrUnavailable()
        if any((row.get("image_id") not in images if row.get("image_id") is not None else False)
               or (row.get("version_id") not in versions if row.get("version_id") is not None else False)
               or (versions[row["version_id"]] != row["image_id"]
                   if row.get("version_id") is not None and row.get("image_id") is not None else False)
               for row in value["tasks"]):
            raise OcrUnavailable()
        return value

    def submit(self, version_ids: list[str], engines: list[str], *, request_id: str) -> tuple[str, ...]:
        if self.context.role == "viewer" or not self.capabilities().recognition:
            raise OcrUnavailable()
        if (not isinstance(version_ids, list) or not 1 <= len(version_ids) <= 1000
                or any(not _is_native_id(x) for x in version_ids) or len(set(version_ids)) != len(version_ids)
                or not isinstance(engines, list) or not 1 <= len(engines) <= 16
                or any(not _is_native_id(x) for x in engines) or len(set(engines)) != len(engines)
                or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
            raise OcrUnavailable()
        snapshot = self.snapshot()
        if not set(version_ids) <= {row["id"] for row in snapshot["versions"]}:
            raise OcrUnavailable()
        state = self._request("GET", "/api/state")
        if not isinstance(state.get("engines"), dict) or not set(engines) <= set(state["engines"]):
            raise OcrUnavailable()
        result = self._request("POST", f"/api/projects/{self.context.module_project_id}/tasks",
                               {"version_ids": version_ids, "engines": engines,
                                "preprocess": [], "request_id": request_id})
        ids = result.get("task_ids")
        if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                or any(not _is_native_id(value) for value in ids)):
            raise OcrUnavailable()
        # Native IDs are the only authoritative job identity. An enqueue
        # response says nothing about eventual success.
        return tuple(ids)

    def batch(self, task_ids: list[str]) -> BatchStatus:
        if (not isinstance(task_ids, list) or not task_ids or any(not _is_native_id(x) for x in task_ids)
                or len(set(task_ids)) != len(task_ids)):
            raise OcrUnavailable()
        return self._batch_from_snapshot(task_ids, self.snapshot())

    @staticmethod
    def _batch_from_snapshot(task_ids: list[str], snapshot: dict) -> BatchStatus:
        if (not isinstance(task_ids, list) or not task_ids or any(not _is_native_id(x) for x in task_ids)
                or len(set(task_ids)) != len(task_ids)):
            raise OcrUnavailable()
        rows = {row["id"]: row for row in snapshot["tasks"]}
        if not set(task_ids) <= set(rows):
            raise OcrUnavailable()
        tasks = tuple(_task(rows[key]) for key in task_ids)
        succeeded = sum(item.native_status == "succeeded" for item in tasks)
        failed = sum(item.native_status == "failed" for item in tasks)
        cancelled = sum(item.native_status == "cancelled" for item in tasks)
        pending = len(tasks) - succeeded - failed - cancelled
        status = ("running" if pending else "succeeded" if succeeded == len(tasks)
                  else "partial" if succeeded else "failed" if failed else "cancelled")
        return BatchStatus(tasks, succeeded, failed, cancelled, pending, status,
                           tuple(item.native_task_id for item in tasks if item.native_status not in TERMINAL))

    def cancel(self, task_ids: list[str]) -> tuple[str, ...]:
        if self.context.role == "viewer":
            raise OcrUnavailable()
        before = self.batch(task_ids)
        active = [task.native_task_id for task in before.tasks if task.native_status not in TERMINAL]
        if not active:
            return ()
        result = self._request("POST", f"/api/projects/{self.context.module_project_id}/queue/cancel",
                               {"task_ids": active})
        affected = result.get("task_ids")
        if (not isinstance(affected, list) or len(set(affected)) != len(affected)
                or any(not _is_native_id(value) for value in affected)
                or not set(affected) <= set(active)):
            raise OcrUnavailable()
        return tuple(affected)

    def results(self, task_ids: list[str]) -> tuple[ResultArtifact, ...]:
        snapshot = self.snapshot()
        before = self._batch_from_snapshot(task_ids, snapshot)
        images = {row["id"]: row for row in snapshot["images"]}
        artifacts = []
        for task in before.tasks:
            if task.native_status != "succeeded" or not task.result_id:
                continue
            image = images[task.image_id]
            locator = {"kind": "image", "image_id": task.image_id, "version_id": task.version_id}
            if _is_native_id(image.get("document_id")) and type(image.get("page_number")) is int:
                locator.update({"document_id": image["document_id"], "page_number": image["page_number"]})
            # OCR geometry is not presumed. Cell polygons require separate
            # evidence and must never be generated by splitting a table box.
            artifacts.append(ResultArtifact(task.result_id, task.native_task_id, task.image_id,
                                            task.version_id, image.get("selected_result") == task.result_id,
                                            locator))
        return tuple(artifacts)


def _is_native_id(value: object) -> bool:
    return isinstance(value, str) and NATIVE_ID.fullmatch(value) is not None


def _task(row: dict) -> TaskStatus:
    status = row.get("status")
    result_id = row.get("result_id")
    if (status not in KNOWN_STATES or (status == "succeeded") != (result_id is not None)
            or (result_id is not None and not _is_native_id(result_id))
            or not isinstance(row.get("phase"), str)
            or not _is_native_id(row.get("image_id")) or not _is_native_id(row.get("version_id"))):
        raise OcrUnavailable()
    error = row.get("error")
    return TaskStatus(row["id"], status, row["phase"], result_id, row["image_id"],
                      row["version_id"], error if isinstance(error, str) else None)
