"""Packaged OCR native adapter over an already-authorized bridge context.

The caller must consume ContextBridge authorization first, then supply a live
Supervisor verifier and a trusted native session-token provider. Browser input
never chooses a host, credentials, native project, or arbitrary route.
Mirrors adapters/ocr/adapter.py until the shared adapter package is distributed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit
import zipfile

from workbench_platform.bridge.context import ResolvedContext

NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,120}\Z")
IMPORT_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,119}\Z")
MAX_JSON = 8 * 1024 * 1024
MAX_IMPORT_MANIFEST = 1_048_576
MAX_IMPORT_ITEM = 128 * 1024 * 1024
MAX_EXPORT = 128 * 1024 * 1024
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
KNOWN_STATES = frozenset({"queued", "running", "paused", "interrupted", *TERMINAL})


class OcrUnavailable(ValueError):
    def __init__(self):
        super().__init__("OCR adapter is unavailable")


class OcrImportConflict(OcrUnavailable):
    """The native request ID belongs to a different immutable manifest."""


class OcrResultConflict(OcrUnavailable):
    """The requested revision or identified adoption conflicts with native state."""


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
    error_details: dict | None = None


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

    def _credential(self) -> str:
        try:
            if not self.live(self.context):
                raise OcrUnavailable()
            credential = self.token()
        except Exception:
            raise OcrUnavailable() from None
        if (not isinstance(credential, str) or len(credential) < 32 or len(credential) > 512
                or not re.fullmatch(r"[A-Za-z0-9_-]+", credential)):
            raise OcrUnavailable()
        return credential

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, max_body: int = 8192, import_conflict: bool = False,
                 result_conflict: bool = False, allow_null: bool = False) -> dict | None:
        credential = self._credential()
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        if payload is not None and len(payload) > max_body:
            raise OcrUnavailable()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=payload,
                               headers={"Authorization": "Bearer " + credential,
                                        "Accept": "application/json",
                                        **({"Content-Type": "application/json"} if payload is not None else {})})
            response = connection.getresponse()
            if import_conflict and response.status == 409:
                raise OcrImportConflict()
            if result_conflict and response.status == 409:
                raise OcrResultConflict()
            if response.status != 200 or int(response.getheader("Content-Length", "0")) > MAX_JSON:
                raise OcrUnavailable()
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise OcrUnavailable()
            value = json.loads(raw)
            if value is None and allow_null:
                return None
            if not isinstance(value, dict):
                raise OcrUnavailable()
            return value
        except (OcrImportConflict, OcrResultConflict):
            raise
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()

    def _import_path(self, request_id: str) -> str:
        if not isinstance(request_id, str) or not IMPORT_REQUEST_ID.fullmatch(request_id):
            raise OcrUnavailable()
        return f"/api/platform/imports/{self.context.module_project_id}/{request_id}"

    @staticmethod
    def _import_status(result: dict, request_id: str) -> dict:
        if (not isinstance(result, dict) or result.get("request_id") != request_id
                or result.get("state") not in {"prepared", "accepted", "completed", "unknown"}
                or not isinstance(result.get("payload_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["payload_hash"])
                or not isinstance(result.get("items"), list)
                or not 1 <= len(result["items"]) <= 1000):
            raise OcrUnavailable()
        ids = set()
        for item in result["items"]:
            if (not isinstance(item, dict) or not _is_native_id(item.get("item_id"))
                    or item["item_id"] in ids
                    or item.get("status") not in {"pending", "imported", "missing"}):
                raise OcrUnavailable()
            ids.add(item["item_id"])
            image = (_is_native_id(item.get("target_asset_id"))
                     and _is_native_id(item.get("target_version_id")))
            document = _is_native_id(item.get("target_document_id"))
            if item["status"] == "imported":
                if image == document:
                    raise OcrUnavailable()
                if document:
                    count, pages = item.get("page_count"), item.get("page_numbers")
                    if (type(count) is not int or count < 0 or not isinstance(pages, list)
                            or pages != list(range(1, count + 1))):
                        raise OcrUnavailable()
                elif item.get("page_count") is not None or item.get("page_numbers") is not None:
                    raise OcrUnavailable()
            elif any(item.get(key) is not None for key in (
                    "target_asset_id", "target_version_id", "target_document_id",
                    "page_count", "page_numbers")):
                raise OcrUnavailable()
        states = {item["status"] for item in result["items"]}
        expected = ("unknown" if "missing" in states else "completed" if states == {"imported"}
                    else "accepted" if "imported" in states else "prepared")
        if result["state"] != expected:
            raise OcrUnavailable()
        return result

    def begin_import(self, request_id: str, manifest: dict) -> dict:
        if (self.context.role == "viewer" or not isinstance(request_id, str)
                or not IMPORT_REQUEST_ID.fullmatch(request_id) or not isinstance(manifest, dict)):
            raise OcrUnavailable()
        result = self._request("POST", self._import_path(request_id), manifest,
                               max_body=MAX_IMPORT_MANIFEST, import_conflict=True)
        status = self._import_status(result, request_id)
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        if (status["payload_hash"] != hashlib.sha256(canonical).hexdigest()
                or [item["item_id"] for item in status["items"]]
                != [item.get("item_id") for item in manifest.get("items", [])]):
            raise OcrUnavailable()
        return status

    def import_status(self, request_id: str) -> dict:
        return self._import_status(self._request("GET", self._import_path(request_id)), request_id)

    def import_item(self, request_id: str, item_id: str, source: Path,
                    *, blob_hash: str, size_bytes: int) -> dict:
        if (self.context.role == "viewer" or not _is_native_id(item_id)
                or not isinstance(source, Path) or not re.fullmatch(r"[0-9a-f]{64}", blob_hash)
                or type(size_bytes) is not int or not 1 <= size_bytes <= MAX_IMPORT_ITEM):
            raise OcrUnavailable()
        try:
            path = source.resolve(strict=True)
        except OSError:
            raise OcrUnavailable() from None
        if not path.is_file() or path.stat().st_size != size_bytes:
            raise OcrUnavailable()
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != blob_hash:
                raise OcrUnavailable()
        credential = self._credential()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.putrequest("PUT", self._import_path(request_id) + "/items/" + item_id)
            connection.putheader("Authorization", "Bearer " + credential)
            connection.putheader("Accept", "application/json")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size_bytes))
            connection.endheaders()
            with path.open("rb") as stream:
                remaining = size_bytes
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OcrUnavailable()
                    connection.send(chunk)
                    remaining -= len(chunk)
            response = connection.getresponse()
            if response.status != 200 or int(response.getheader("Content-Length", "0")) > MAX_JSON:
                raise OcrUnavailable()
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise OcrUnavailable()
            return self._import_status(json.loads(raw), request_id)
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

    def document_pages(self, document_id: str, *, offset=0, limit=50):
        if (not _is_native_id(document_id) or not 0 <= offset <= 1000 or not 1 <= limit <= 100
                or document_id not in {doc['id'] for doc in self.snapshot()['documents']}):
            raise OcrUnavailable()
        value = self._request('GET', f'/api/documents/{document_id}/pages?offset={offset}&limit={limit}')
        if (value.get('document', {}).get('id') != document_id or value['document'].get('project_id') != self.context.module_project_id
                or not isinstance(value.get('pages'), list) or len(value['pages']) > limit
                or any(page.get('document_id') != document_id for page in value['pages'])):
            raise OcrUnavailable()
        return value

    def page_request(self, document_id: str, request_id: str, *, body=None, action=None):
        if (self.context.role == 'viewer' and (body is not None or action)
                or not _is_native_id(document_id) or not REQUEST_ID.fullmatch(request_id)
                or action not in {None, 'cancel', 'resume'}
                or document_id not in {doc['id'] for doc in self.snapshot()['documents']}):
            raise OcrUnavailable()
        route = f'/api/platform/documents/{document_id}/requests/{request_id}'
        value = self._request('POST' if body is not None or action else 'GET',
                              route + ('/' + action if action else ''), body, max_body=32768)
        if (value.get('document_id') != document_id or value.get('project_id') != self.context.module_project_id
                or value.get('request_id') != request_id or type(value.get('submitted')) is not bool):
            raise OcrUnavailable()
        if value['submitted']:
            stages = value.get('stages')
            parameters = value.get('parameters')
            if (not isinstance(stages, list) or not isinstance(parameters, dict)
                    or len(stages) != len(parameters.get('page_numbers', []))
                    or sorted(stage.get('page_number', 0) for stage in stages) != parameters['page_numbers']
                    or any(not _is_native_id(stage.get('stage_id')) or not _is_native_id(stage.get('page_id'))
                           or type(stage.get('owned')) is not bool for stage in stages)):
                raise OcrUnavailable()
        return value

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

    def submit(self, version_ids: list[str], engines: list[str], *, request_id: str, preprocess=None, fallback_engine=None) -> tuple[str, ...]:
        preprocess = preprocess or []
        validate_preprocess(preprocess)
        if fallback_engine is not None and (not isinstance(fallback_engine, str) or fallback_engine not in {'ppocr', 'paddlevl', 'glm', 'hunyuan'} or not isinstance(engines, list) or 'auto' not in engines):
            raise OcrUnavailable()
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
                                "preprocess": preprocess, "request_id": request_id,
                                **({'fallback_engine': fallback_engine} if fallback_engine is not None else {})}, max_body=65536)
        ids = result.get("task_ids")
        if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                or any(not _is_native_id(value) for value in ids)):
            raise OcrUnavailable()
        # Native IDs are the only authoritative job identity. An enqueue
        # response says nothing about eventual success.
        return tuple(ids)

    def lookup_submission(self, request_id: str) -> tuple[str, ...] | None:
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise OcrUnavailable()
        value = self._request("GET", f"/api/projects/{self.context.module_project_id}/task-requests/{request_id}")
        ids = value.get("task_ids")
        if (type(value.get("submitted")) is not bool or not isinstance(ids, list)
                or len(ids) != len(set(ids)) or any(not _is_native_id(item) for item in ids)
                or (bool(ids) != value["submitted"])):
            raise OcrUnavailable()
        return tuple(ids) if value["submitted"] else None

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

    def result_revision(self, result: ResultArtifact) -> int:
        value = self._request("GET", f"/api/results/{result.native_result_id}")
        revision = value.get("revision")
        if (value.get("id") != result.native_result_id
                or value.get("task_id") != result.native_task_id
                or type(revision) is not int or revision < 0):
            raise OcrUnavailable()
        return revision

    def result_snapshot(self, result: ResultArtifact, revision: int | None = None) -> dict:
        if (not isinstance(result, ResultArtifact)
                or any(not _is_native_id(value) for value in
                       (result.native_result_id, result.native_task_id, result.image_id, result.version_id))
                or (revision is not None and (type(revision) is not int or revision < 0))):
            raise OcrUnavailable()
        suffix = "snapshot" if revision is None else f"revisions/{revision}"
        value = self._request("GET", f"/api/results/{result.native_result_id}/{suffix}")
        if ((value.get("id"), value.get("task_id"), value.get("image_id"), value.get("version_id"),
             value.get("project_id")) != (result.native_result_id, result.native_task_id,
                result.image_id, result.version_id, self.context.module_project_id)
                or type(value.get("revision")) is not int or value["revision"] < 0
                or type(value.get("current_revision")) is not int or value["current_revision"] < value["revision"]
                or (revision is not None and value["revision"] != revision)
                or type(value.get("available")) is not bool
                or not isinstance(value.get("original_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["original_sha256"])
                or (value["available"] and not isinstance(value.get("edited"), dict))
                or (not value["available"] and value.get("reason") != "legacy_revision_unavailable")):
            raise OcrUnavailable()
        return value

    def result_tables(self, result: ResultArtifact, revision: int) -> dict:
        snapshot = self.result_snapshot(result, revision)
        if not snapshot['available']:
            raise OcrUnavailable()
        value = self._request('GET', f'/api/results/{result.native_result_id}/revisions/{revision}/tables')
        if ((value.get('id'), value.get('project_id'), value.get('revision')) !=
                (result.native_result_id, self.context.module_project_id, revision)
                or value.get('source') not in {'revision', 'pdf-native-candidate', 'none'}
                or not isinstance(value.get('tables'), list) or not isinstance(value.get('warnings'), list)
                or any(not isinstance(value.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', value[key])
                       for key in ('content_sha256', 'tables_sha256'))):
            raise OcrUnavailable()
        def fingerprint(body):
            return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        content = {'id': result.native_result_id, 'revision': revision, 'original': snapshot['original'],
                   'edited': snapshot['edited'], 'table_projection_version': 1,
                   'tables': value['tables'], 'source': value['source']}
        if value['content_sha256'] != fingerprint(content) or value['tables_sha256'] != fingerprint(value['tables']):
            raise OcrUnavailable()
        return value

    def result_history(self, result: ResultArtifact, offset: int = 0, limit: int = 50) -> dict:
        if (not isinstance(result, ResultArtifact) or not _is_native_id(result.native_result_id)
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100):
            raise OcrUnavailable()
        value = self._request("GET", f"/api/results/{result.native_result_id}/revisions?offset={offset}&limit={limit}")
        if ((value.get("id"), value.get("task_id"), value.get("project_id"), value.get("image_id"),
             value.get("version_id")) != (result.native_result_id, result.native_task_id,
                self.context.module_project_id, result.image_id, result.version_id)
                or value.get("offset") != offset or value.get("limit") != limit
                or any(type(value.get(key)) is not int or value[key] < 0
                       for key in ("current_revision", "total", "unavailable_revisions"))
                or not isinstance(value.get("items"), list) or len(value["items"]) > limit
                or any(not isinstance(row, dict) or type(row.get("revision")) is not int
                       or not 0 <= row["revision"] <= value["current_revision"] for row in value["items"])):
            raise OcrUnavailable()
        return value

    def _adoption_receipt(self, value: dict, operation_id: str, expected_instance_id: str | None = None) -> dict:
        if (value.get("operation_id") != operation_id or value.get("state") != "committed"
                or value.get("project_id") != self.context.module_project_id
                or value.get("instance_id") != (expected_instance_id or self.context.instance_id)
                or any(not _is_native_id(value.get(key)) for key in ("result_id", "task_id", "image_id", "version_id"))
                or type(value.get("revision")) is not int or value["revision"] < 0
                or not isinstance(value.get("payload_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["payload_hash"])
                or not isinstance(value.get("original_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["original_sha256"])):
            raise OcrUnavailable()
        actor = value.get("actor")
        if (not isinstance(actor, dict) or set(actor) != {"id", "display_name", "source"}
                or not _is_native_id(actor["id"]) or actor["source"] != "local"
                or not isinstance(actor["display_name"], str) or not 1 <= len(actor["display_name"]) <= 100
                or not isinstance(value.get("note"), str) or len(value["note"]) > 2000):
            raise OcrUnavailable()
        body = {key: value[key] for key in ("project_id", "instance_id", "result_id", "task_id", "image_id",
                                           "version_id", "revision", "original_sha256", "actor", "note")}
        digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        if value["payload_hash"] != digest:
            raise OcrUnavailable()
        return value

    def operation(self, operation_id: str, *, expected_instance_id: str | None = None) -> dict | None:
        if (not isinstance(operation_id, str) or not REQUEST_ID.fullmatch(operation_id)
                or (expected_instance_id is not None and not _is_native_id(expected_instance_id))):
            raise OcrUnavailable()
        original_instance = expected_instance_id or self.context.instance_id
        value = self._request("GET", f"/api/platform/adoptions/{operation_id}"
                              f"?project_id={self.context.module_project_id}&instance_id={self.context.instance_id}"
                              f"&operation_instance_id={original_instance}",
                              result_conflict=True)
        if (value.get("operation_id") != operation_id or type(value.get("committed")) is not bool
                or (value["committed"] and not isinstance(value.get("receipt"), dict))
                or (not value["committed"] and value.get("receipt") is not None)):
            raise OcrUnavailable()
        return self._adoption_receipt(value["receipt"], operation_id, original_instance) if value["committed"] else None

    def adopt(self, result: ResultArtifact, native_revision: int, *, operation_id: str,
              actor: dict, note: str = "") -> dict:
        if (self.context.role == "viewer" or not isinstance(result, ResultArtifact)
                or any(not _is_native_id(value) for value in
                       (result.native_result_id, result.native_task_id, result.image_id, result.version_id))
                or type(native_revision) is not int or native_revision < 0
                or not isinstance(actor, dict) or set(actor) != {"id", "display_name", "source"}
                or not _is_native_id(actor["id"]) or actor["source"] != "local"
                or not isinstance(actor["display_name"], str) or not 1 <= len(actor["display_name"]) <= 100
                or not isinstance(note, str) or len(note) > 2000):
            raise OcrUnavailable()
        expected = {"result_id": result.native_result_id, "task_id": result.native_task_id,
                    "image_id": result.image_id, "version_id": result.version_id,
                    "revision": native_revision, "actor": actor, "note": note}
        existing = self.operation(operation_id)
        if existing is not None:
            if any(existing.get(key) != value for key, value in expected.items()):
                raise OcrResultConflict()
            return existing
        snapshot = self.result_snapshot(result, native_revision)
        if not snapshot["available"] or snapshot["current_revision"] != native_revision:
            raise OcrResultConflict()
        body = {**expected, "project_id": self.context.module_project_id,
                "instance_id": self.context.instance_id, "original_sha256": snapshot["original_sha256"]}
        receipt = self._adoption_receipt(self._request("POST", f"/api/platform/adoptions/{operation_id}",
                                       body, max_body=16384, result_conflict=True), operation_id)
        if any(receipt.get(key) != value for key, value in body.items()):
            raise OcrUnavailable()
        expected_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        if receipt["payload_hash"] != expected_hash:
            raise OcrUnavailable()
        return receipt

    def screen_image(self, project_id: str, batch_id: str, version_id: str, file_id: str) -> tuple[bytes,str]:
        if project_id != self.context.module_project_id or not all(_is_native_id(value) for value in (batch_id,version_id,file_id)):
            raise OcrUnavailable()
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=30)
        try:
            connection.request('GET',f'/api/projects/{project_id}/screen-batches/{batch_id}/items/{version_id}/files/{file_id}',headers={'Authorization':'Bearer '+self._credential(),'Accept':'image/*'})
            response=connection.getresponse()
            media=response.getheader('Content-Type','').split(';')[0]
            if response.status!=200 or media not in {'image/png','image/jpeg','image/webp'}:
                raise OcrUnavailable()
            data=response.read(32*1024*1024+1)
            if not data or len(data)>32*1024*1024:
                raise OcrUnavailable()
            return data,media
        except (OSError,ValueError,http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()

    def screen_local_file(self, project_id: str, batch_id: str, request_id: str,
                          task_id: str, file_id: str) -> tuple[bytes, str]:
        if (project_id != self.context.module_project_id
                or not all(_is_native_id(value) for value in (batch_id, request_id, task_id))
                or file_id not in {'raw.json', 'result.json', 'error.json', 'input.png', 'result.txt',
                                   'result.md', 'crop.png', 'crop-binding.json'}):
            raise OcrUnavailable()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        try:
            connection.request('GET', f'/api/projects/{project_id}/screen-batches/{batch_id}'
                f'/local-candidates/{request_id}/tasks/{task_id}/files/{file_id}',
                headers={'Authorization': 'Bearer '+self._credential()})
            response = connection.getresponse()
            media = response.getheader('Content-Type', '').split(';')[0]
            expected = 'image/png' if file_id.endswith('.png') else 'application/json' if file_id.endswith('.json') else 'text/plain'
            if response.status != 200 or media != expected:
                raise OcrUnavailable()
            data = response.read(32*1024*1024+1)
            if not data or len(data) > 32*1024*1024:
                raise OcrUnavailable()
            return data, media
        except (OSError, ValueError, http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()

    def screen_export(self, project_id: str, batch_id: str, options: dict) -> bytes:
        if project_id != self.context.module_project_id or not _is_native_id(batch_id):
            raise OcrUnavailable()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        try:
            connection.request('POST', f'/api/projects/{project_id}/screen-batches/{batch_id}/export',
                body=json.dumps(options, separators=(',', ':')).encode(), headers={
                    'Authorization': 'Bearer '+self._credential(), 'Content-Type':'application/json', 'Accept':'application/zip'})
            response = connection.getresponse()
            if response.status == 409:
                raise OcrResultConflict()
            if response.status != 200:
                raise OcrUnavailable()
            data = response.read(MAX_EXPORT+1)
            if not data or len(data)>MAX_EXPORT or not zipfile.is_zipfile(io.BytesIO(data)):
                raise OcrUnavailable()
            return data
        except OcrResultConflict:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()

    def export_results(self, results: list[tuple[ResultArtifact, int]]) -> tuple[bytes, str]:
        if (not results or len(results) > 1000
                or len({item.native_result_id for item, _ in results}) != len(results)
                or any(type(revision) is not int or revision < 0 for _, revision in results)):
            raise OcrUnavailable()
        body = {"result_ids": [item.native_result_id for item, _ in results],
                "format": "json", "expected_results": {
                    item.native_result_id: {"revision": revision, "version_id": item.version_id}
                    for item, revision in results}}
        credential = self._credential()
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", "/api/export", body=payload,
                               headers={"Authorization": "Bearer " + credential,
                                        "Content-Type": "application/json", "Accept": "application/octet-stream"})
            response = connection.getresponse()
            if response.status == 409:
                raise OcrResultConflict()
            if response.status != 200:
                raise OcrUnavailable()
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_EXPORT):
                raise OcrUnavailable()
            data = response.read(MAX_EXPORT + 1)
            if not data or len(data) > MAX_EXPORT:
                raise OcrUnavailable()
            names = (["OCR-result.json"] if len(results) == 1 else
                     [f"{index:04d}.json" for index in range(1, len(results) + 1)])
            if zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries = archive.infolist()
                    if (len(entries) > 10_000 or len({entry.filename for entry in entries}) != len(entries)
                            or sum(entry.file_size for entry in entries) > MAX_EXPORT):
                        raise OcrUnavailable()
                    for name, (item, revision) in zip(names, results):
                        if name not in archive.namelist() or archive.getinfo(name).file_size > MAX_JSON:
                            raise OcrUnavailable()
                        value = json.loads(archive.read(name))
                        if (value.get("id"), value.get("revision")) != (item.native_result_id, revision):
                            raise OcrUnavailable()
                return data, "application/zip"
            if len(results) != 1 or len(data) > MAX_JSON:
                raise OcrUnavailable()
            value = json.loads(data)
            if (value.get("id"), value.get("revision")) != (
                    results[0][0].native_result_id, results[0][1]):
                raise OcrUnavailable()
            return data, "application/json"
        except OcrResultConflict:
            raise
        except (OSError, TimeoutError, ValueError, TypeError, KeyError,
                json.JSONDecodeError, zipfile.BadZipFile, http.client.HTTPException):
            raise OcrUnavailable() from None
        finally:
            connection.close()


def _is_native_id(value: object) -> bool:
    return isinstance(value, str) and NATIVE_ID.fullmatch(value) is not None


def validate_preprocess(operations):
    if not isinstance(operations, list) or len(operations) > 2:
        raise ValueError('预处理仅支持一次旋转与一次对比度调整')
    seen = set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get('kind') in seen:
            raise ValueError('预处理参数重复或无效')
        kind = operation.get('kind'); seen.add(kind)
        if kind == 'rotate' and set(operation) == {'kind', 'degrees'} and type(operation['degrees']) is int and operation['degrees'] in {90, 180, 270, -90}:
            continue
        if kind == 'contrast' and set(operation) == {'kind', 'factor'} and type(operation['factor']) in {int, float} and .2 <= operation['factor'] <= 3:
            continue
        raise ValueError('预处理参数无效')


def _task(row: dict) -> TaskStatus:
    status = row.get("status")
    result_id = row.get("result_id")
    if (status not in KNOWN_STATES or
            ((status == "succeeded") != (result_id is not None) and not (row.get('kind') == 'multimodal' and result_id is None))
            or (result_id is not None and not _is_native_id(result_id))
            or not isinstance(row.get("phase"), str)
            or not _is_native_id(row.get("image_id")) or not _is_native_id(row.get("version_id"))):
        raise OcrUnavailable()
    error = row.get("error")
    details = row.get('error_details')
    if details is not None and (not isinstance(details, dict) or details.get('version') != 1
                                or details.get('attempt_id') != row['id']):
        raise OcrUnavailable()
    return TaskStatus(row["id"], status, row["phase"], result_id, row["image_id"],
                      row["version_id"], error if isinstance(error, str) else None, details)
