"""Server-side classification adapter; predictions never become human labels."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit

from workbench_platform.bridge.context import ResolvedContext

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{8,120}\Z")
IMPORT_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,119}\Z")
MAX_JSON = 8 * 1024 * 1024
MAX_IMPORT_MANIFEST = 1_048_576
MAX_IMPORT_ITEM = 100 * 1024 * 1024
STATES = frozenset({"queued", "running", "pausing", "paused", "cancelling", "cancelled",
                    "succeeded", "completed_with_errors", "failed"})
KINDS = frozenset({"import", "train", "inference"})


class ClassificationUnavailable(ValueError):
    def __init__(self):
        super().__init__("Classification adapter is unavailable")


class ClassificationImportConflict(ClassificationUnavailable):
    """The native request ID belongs to a different import manifest."""


class ClassificationConflict(ClassificationUnavailable):
    """Conditional disposition or operation identity no longer matches."""


@dataclass(frozen=True)
class JobStatus:
    native_job_id: str
    kind: str
    state: str
    stage: str
    total: int
    done: int
    failed: int
    actions: tuple[str, ...]
    interrupted_training_requires_new_job: bool


@dataclass(frozen=True)
class ReviewItem:
    native_item_id: str
    asset_id: str | None
    state: str
    prediction_label_id: str | None
    prediction_status: str | None
    prediction_score: float | None
    human_label_id: str | None
    error: str | None
    disposition_state: str = "unreviewed"
    disposition_revision: int = 0
    disposition_note: str | None = None
    current_annotation_label_id: str | None = None

    @property
    def effective_label_id(self):
        return self.human_label_id if self.disposition_state == "confirmed" else self.prediction_label_id


def _id(value: object) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


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
        raise ClassificationUnavailable() from None


def _actions(kind: str, state: str) -> tuple[str, ...]:
    allowed = []
    if kind == "inference" and state in {"queued", "running", "pausing"}:
        allowed.append("pause")
    if kind == "inference" and state == "paused":
        allowed.append("resume")
    if state in {"queued", "running", "pausing", "paused", "cancelling"}:
        allowed.append("cancel")
    if kind == "inference" and state in {"failed", "completed_with_errors", "cancelled"}:
        allowed.append("retry_failed")
    return tuple(allowed)


class ClassificationAdapter:
    def __init__(self, context: ResolvedContext, *, live: Callable[[ResolvedContext], bool],
                 timeout: float = 3.0):
        if (not isinstance(context, ResolvedContext) or context.app_id != "classification"
                or not all(_id(value) for value in (context.workspace_id, context.link_id,
                                                     context.instance_id, context.module_project_id))
                or context.role not in {"owner", "editor", "viewer"} or not callable(live)
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10):
            raise ClassificationUnavailable()
        self.context, self.live, self.timeout = context, live, timeout
        self.port = _port(context.base_url)

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, key: str | None = None, max_body: int = 8192,
                 import_conflict: bool = False, conditional: bool = False) -> dict | list:
        try:
            if not self.live(self.context):
                raise ClassificationUnavailable()
        except Exception:
            raise ClassificationUnavailable() from None
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        if payload is not None and len(payload) > max_body:
            raise ClassificationUnavailable()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=payload,
                               headers={"Accept": "application/json", "X-Platform-Request": "1",
                                        **({"Content-Type": "application/json"} if payload is not None else {}),
                                        **({"Idempotency-Key": key} if key else {})})
            response = connection.getresponse()
            if conditional and response.status == 409:
                raise ClassificationConflict()
            if import_conflict and response.status == 409:
                raise ClassificationImportConflict()
            if response.status not in {200, 201, 202} or int(response.getheader("Content-Length", "0")) > MAX_JSON:
                raise ClassificationUnavailable()
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise ClassificationUnavailable()
            result = json.loads(raw)
            if not isinstance(result, (dict, list)):
                raise ClassificationUnavailable()
            return result
        except (ClassificationImportConflict, ClassificationConflict):
            raise
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                http.client.HTTPException):
            raise ClassificationUnavailable() from None
        finally:
            connection.close()

    def _import_path(self, request_id: str) -> str:
        if not isinstance(request_id, str) or not IMPORT_REQUEST_ID.fullmatch(request_id):
            raise ClassificationUnavailable()
        return f"/api/v1/platform/imports/{self.context.module_project_id}/{request_id}"

    @staticmethod
    def _import_status(result, request_id: str) -> dict:
        if (not isinstance(result, dict) or result.get("request_id") != request_id
                or result.get("state") not in {"prepared", "accepted", "completed", "unknown"}
                or not isinstance(result.get("payload_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["payload_hash"])
                or not isinstance(result.get("items"), list)
                or not 1 <= len(result["items"]) <= 1000):
            raise ClassificationUnavailable()
        seen = set()
        for item in result["items"]:
            if (not isinstance(item, dict) or not _id(item.get("item_id"))
                    or item["item_id"] in seen
                    or item.get("status") not in {"pending", "imported", "missing"}):
                raise ClassificationUnavailable()
            seen.add(item["item_id"])
            if item["status"] == "imported":
                if not _id(item.get("target_asset_id")):
                    raise ClassificationUnavailable()
            elif item.get("target_asset_id") is not None:
                raise ClassificationUnavailable()
        states = {item["status"] for item in result["items"]}
        expected = ("unknown" if "missing" in states else "completed" if states == {"imported"}
                    else "accepted" if "imported" in states else "prepared")
        if result["state"] != expected:
            raise ClassificationUnavailable()
        return result

    def begin_import(self, request_id: str, manifest: dict) -> dict:
        if (self.context.role == "viewer" or not isinstance(request_id, str)
                or not IMPORT_REQUEST_ID.fullmatch(request_id) or not isinstance(manifest, dict)):
            raise ClassificationUnavailable()
        result = self._request("POST", self._import_path(request_id), manifest,
                               max_body=MAX_IMPORT_MANIFEST, import_conflict=True)
        status = self._import_status(result, request_id)
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        if (status["payload_hash"] != hashlib.sha256(canonical).hexdigest()
                or [item["item_id"] for item in status["items"]]
                != [item.get("item_id") for item in manifest.get("items", [])]):
            raise ClassificationUnavailable()
        return status

    def import_status(self, request_id: str) -> dict:
        return self._import_status(self._request("GET", self._import_path(request_id)), request_id)

    def import_item(self, request_id: str, item_id: str, source: Path,
                    *, blob_hash: str, size_bytes: int) -> dict:
        if (self.context.role == "viewer" or not _id(item_id) or not isinstance(source, Path)
                or not isinstance(blob_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", blob_hash)
                or type(size_bytes) is not int or not 1 <= size_bytes <= MAX_IMPORT_ITEM):
            raise ClassificationUnavailable()
        try:
            path = source.resolve(strict=True)
            if not self.live(self.context) or not path.is_file() or path.stat().st_size != size_bytes:
                raise ClassificationUnavailable()
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != blob_hash:
                    raise ClassificationUnavailable()
        except (OSError, ValueError):
            raise ClassificationUnavailable() from None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.putrequest("PUT", self._import_path(request_id) + "/items/" + item_id)
            connection.putheader("Accept", "application/json")
            connection.putheader("X-Platform-Request", "1")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size_bytes))
            connection.endheaders()
            with path.open("rb") as stream:
                remaining = size_bytes
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ClassificationUnavailable()
                    connection.send(chunk)
                    remaining -= len(chunk)
            response = connection.getresponse()
            if response.status != 200 or int(response.getheader("Content-Length", "0")) > MAX_JSON:
                raise ClassificationUnavailable()
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise ClassificationUnavailable()
            return self._import_status(json.loads(raw), request_id)
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                http.client.HTTPException):
            raise ClassificationUnavailable() from None
        finally:
            connection.close()

    @property
    def project_id(self) -> str:
        return self.context.module_project_id

    def project(self) -> dict:
        result = self._request("GET", f"/api/v1/projects/{self.project_id}")
        if not isinstance(result, dict) or result.get("id") != self.project_id or not isinstance(result.get("labels"), list):
            raise ClassificationUnavailable()
        return result

    def assets(self, *, limit: int = 48, offset: int = 0) -> tuple[dict, ...]:
        if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
            raise ClassificationUnavailable()
        result = self._request("GET", f"/api/v1/projects/{self.project_id}/assets?limit={limit}&offset={offset}")
        if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
                or any(not isinstance(row, dict) or row.get("project_id") != self.project_id
                       or not _id(row.get("id")) for row in result["items"])):
            raise ClassificationUnavailable()
        return tuple(result["items"])

    def annotate(self, asset_ids: list[str], label_id: str | None, *, group_id: str | None = None) -> int:
        if (self.context.role == "viewer" or not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 500
                or any(not _id(item) for item in asset_ids) or len(set(asset_ids)) != len(asset_ids)
                or (group_id is not None and (not isinstance(group_id, str) or not 1 <= len(group_id) <= 200))):
            raise ClassificationUnavailable()
        project = self.project()
        labels = {item.get("label_id") for item in project["labels"] if isinstance(item, dict)}
        if label_id is not None and (not _id(label_id) or label_id not in labels):
            raise ClassificationUnavailable()
        for asset_id in asset_ids:
            asset = self._request("GET", f"/api/v1/projects/{self.project_id}/assets/{asset_id}")
            if not isinstance(asset, dict) or asset.get("id") != asset_id or asset.get("project_id") != self.project_id:
                raise ClassificationUnavailable()
        result = self._request("POST", f"/api/v1/projects/{self.project_id}/annotations",
                               {"asset_ids": asset_ids, "label_id": label_id, "group_id": group_id})
        if not isinstance(result, dict) or result.get("updated") != len(asset_ids):
            raise ClassificationUnavailable()
        return len(asset_ids)

    def dataset_versions(self) -> tuple[dict, ...]:
        result = self._request("GET", f"/api/v1/projects/{self.project_id}/dataset-versions")
        if not isinstance(result, list) or any(not isinstance(item, dict) or not _id(item.get("id")) for item in result):
            raise ClassificationUnavailable()
        return tuple(result)

    def models(self) -> tuple[dict, ...]:
        result = self._request("GET", f"/api/v1/projects/{self.project_id}/models")
        if (not isinstance(result, list) or any(not isinstance(item, dict) or not _id(item.get("id"))
                                               or item.get("project_id") != self.project_id for item in result)):
            raise ClassificationUnavailable()
        return tuple(result)

    def train(self, dataset_id: str, *, key: str, epochs: int = 1, device: str = "cpu") -> str:
        if (self.context.role == "viewer" or not _id(dataset_id) or not isinstance(key, str)
                or not KEY.fullmatch(key) or type(epochs) is not int or not 1 <= epochs <= 500
                or device not in {"cpu", "cuda"}
                or dataset_id not in {row["id"] for row in self.dataset_versions()}):
            raise ClassificationUnavailable()
        result = self._request("POST", f"/api/v1/projects/{self.project_id}/experiments",
                               {"dataset_id": dataset_id, "epochs": epochs, "device": device}, key=key)
        if not isinstance(result, dict) or not _id(result.get("job_id")):
            raise ClassificationUnavailable()
        return result["job_id"]

    def infer(self, model_id: str, asset_ids: list[str], *, key: str, device: str = "cpu") -> str:
        if (self.context.role == "viewer" or not _id(model_id) or not isinstance(key, str)
                or not KEY.fullmatch(key) or device not in {"cpu", "cuda"}
                or not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 5000
                or any(not _id(item) for item in asset_ids) or len(set(asset_ids)) != len(asset_ids)
                or model_id not in {row["id"] for row in self.models()}):
            raise ClassificationUnavailable()
        result = self._request("POST", f"/api/v1/projects/{self.project_id}/batch-jobs",
                               {"model_id": model_id, "asset_ids": asset_ids, "device": device,
                                "auto_accept": False}, key=key)
        if not isinstance(result, dict) or not _id(result.get("job_id")):
            raise ClassificationUnavailable()
        return result["job_id"]

    def lookup_inference(self, request_id: str) -> str | None:
        if not isinstance(request_id, str) or not KEY.fullmatch(request_id):
            raise ClassificationUnavailable()
        result = self._request("GET", f"/api/v1/projects/{self.project_id}/inference-requests/{request_id}")
        if (not isinstance(result, dict) or type(result.get("submitted")) is not bool
                or (result["submitted"] and not _id(result.get("job_id")))
                or (not result["submitted"] and result.get("job_id") is not None)):
            raise ClassificationUnavailable()
        return result["job_id"]

    def _receipt(self, result, operation_id, *, expected_instance_id=None):
        if (not isinstance(result, dict) or result.get("operation_id") != operation_id
                or result.get("state") != "committed" or result.get("project_id") != self.project_id
                or result.get("instance_id") != (expected_instance_id or self.context.instance_id)
                or not _id(result.get("job_id"))
                or not isinstance(result.get("payload_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["payload_hash"])
                or not isinstance(result.get("items"), list) or not 1 <= len(result["items"]) <= 500
                or not isinstance(result.get("revisions"), dict)):
            raise ClassificationUnavailable()
        seen = set()
        for item in result["items"]:
            if (not isinstance(item, dict) or not _id(item.get("item_id")) or item["item_id"] in seen
                    or type(item.get("revision")) is not int or item["revision"] < 1
                    or result["revisions"].get(item["item_id"]) != item["revision"]
                    or not isinstance(item.get("original_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["original_sha256"])
                    or (item.get("asset_id") is not None and not _id(item["asset_id"]))):
                raise ClassificationUnavailable()
            seen.add(item["item_id"])
        if set(result["revisions"]) != seen:
            raise ClassificationUnavailable()
        return result

    def operation(self, operation_id: str, *, expected_instance_id: str | None = None):
        if not _id(operation_id) or (expected_instance_id is not None and not _id(expected_instance_id)):
            raise ClassificationUnavailable()
        original_instance = expected_instance_id or self.context.instance_id
        result = self._request("GET", f"/api/v1/projects/{self.project_id}/disposition-operations/{operation_id}"
                               f"?instance_id={self.context.instance_id}&operation_instance_id={original_instance}",
                               conditional=True)
        if result == {"operation_id": operation_id, "state": "not_found"}:
            return None
        return self._receipt(result, operation_id, expected_instance_id=expected_instance_id)

    def disposition_history(self, job_id: str, item_id: str):
        self.job(job_id)
        if not _id(item_id):
            raise ClassificationUnavailable()
        result = self._request("GET", f"/api/v1/jobs/{job_id}/results/{item_id}/dispositions")
        if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
                or any(not isinstance(item, dict) or item.get("job_id") != job_id
                       or item.get("item_id") != item_id or type(item.get("revision")) is not int
                       or item["revision"] < 1 for item in result["items"])):
            raise ClassificationUnavailable()
        return result["items"]

    def dispose(self, job_id: str, items: list[ReviewItem], state: str, label_id=None, note="",
                *, operation_id: str, actor: dict, request_id: str | None = None, intent="accept_prediction"):
        if (self.context.role == "viewer" or state not in {"confirmed", "deferred", "reopened"}
                or not _id(operation_id) or not isinstance(items, list) or not 1 <= len(items) <= 500
                or any(not isinstance(item, ReviewItem) for item in items)
                or len({item.native_item_id for item in items}) != len(items)):
            raise ClassificationUnavailable()
        self.job(job_id)  # Prove project ownership before sending a conditional write.
        request_id = request_id or operation_id
        if not _id(request_id):
            raise ClassificationUnavailable()
        body = {
            "item_ids": [item.native_item_id for item in items], "state": state,
            "label_id": label_id, "note": note,
            "expected_revisions": {item.native_item_id: item.disposition_revision for item in items},
            "operation_id": operation_id, "request_id": request_id, "actor": actor,
            "instance_id": self.context.instance_id, "intent": intent}
        result = self._request("POST", f"/api/v1/jobs/{job_id}/dispositions", body,
                               max_body=65536, conditional=True)
        receipt = self._receipt(result, operation_id)
        identity = {"job_id": job_id, **{key: value for key, value in body.items()
                                       if key not in {"operation_id", "request_id"}}}
        identity["item_ids"] = sorted(identity["item_ids"])
        expected_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if (receipt["job_id"] != job_id or set(receipt["revisions"]) != {item.native_item_id for item in items}
                or receipt.get("human_label_id") != label_id or receipt.get("disposition_state") != state
                or receipt.get("request_id") != request_id or receipt.get("payload_hash") != expected_hash
                or receipt.get("actor") != actor or receipt.get("note") != note
                or {item["item_id"]: item.get("asset_id") for item in receipt["items"]}
                   != {item.native_item_id: item.asset_id for item in items}):
            raise ClassificationUnavailable()
        return receipt

    def adopt(self, job_id: str, item: ReviewItem, native_revision: int, human_label_id: str,
              *, operation_id: str, actor: dict, note="", request_id: str | None = None):
        if (type(native_revision) is not int or not isinstance(item, ReviewItem)
                or item.disposition_revision != native_revision or item.disposition_state != "confirmed"
                or not _id(human_label_id) or item.human_label_id != human_label_id):
            raise ClassificationConflict()
        return self.dispose(job_id, [item], "confirmed", human_label_id, note,
                            operation_id=operation_id, request_id=request_id, actor=actor, intent="adopt")

    def job(self, job_id: str) -> JobStatus:
        if not _id(job_id):
            raise ClassificationUnavailable()
        result = self._request("GET", f"/api/v1/jobs/{job_id}")
        if (not isinstance(result, dict) or result.get("id") != job_id
                or result.get("project_id") != self.project_id or result.get("kind") not in KINDS
                or result.get("state") not in STATES or not isinstance(result.get("stage"), str)
                or any(type(result.get(name)) is not int or result[name] < 0
                       for name in ("total", "done", "failed"))):
            raise ClassificationUnavailable()
        kind, state = result["kind"], result["state"]
        if result["done"] + result["failed"] > result["total"] and kind == "inference":
            raise ClassificationUnavailable()
        return JobStatus(job_id, kind, state, result["stage"], result["total"], result["done"],
                         result["failed"], _actions(kind, state),
                         kind == "train" and state in {"paused", "failed", "cancelled"})

    def action(self, job_id: str, action: str) -> JobStatus | str:
        if self.context.role == "viewer":
            raise ClassificationUnavailable()
        before = self.job(job_id)
        if action not in before.actions:
            raise ClassificationUnavailable()
        result = self._request("POST", f"/api/v1/jobs/{job_id}/actions", {"action": action})
        if action == "retry_failed":
            if not isinstance(result, dict) or not _id(result.get("job_id")) or result["job_id"] == job_id:
                raise ClassificationUnavailable()
            return result["job_id"]  # A new inference job, never a resumed training run.
        return self.job(job_id)

    def results(self, job_id: str, *, limit: int = 48, offset: int = 0) -> tuple[ReviewItem, ...]:
        before = self.job(job_id)
        if before.kind != "inference" or not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
            raise ClassificationUnavailable()
        result = self._request("GET", f"/api/v1/jobs/{job_id}/results?limit={limit}&offset={offset}")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ClassificationUnavailable()
        items = []
        for row in result["items"]:
            if (not isinstance(row, dict) or row.get("job_id") != job_id or not _id(row.get("id"))
                    or (row.get("asset_id") is not None and not _id(row["asset_id"]))):
                raise ClassificationUnavailable()
            prediction = row.get("prediction")
            if prediction is not None and not isinstance(prediction, dict):
                raise ClassificationUnavailable()
            label = prediction.get("label_id") if prediction else None
            status = prediction.get("status") if prediction else None
            score = prediction.get("score") if prediction else None
            human = row.get("human_label_id")
            disposition = row.get("disposition_state", "unreviewed")
            disposition_revision = row.get("disposition_revision", 0)
            current_annotation = row.get("confirmed_label")
            if ((label is not None and not _id(label)) or (human is not None and not _id(human))
                    or disposition not in {"unreviewed", "confirmed", "deferred", "reopened"}
                    or type(disposition_revision) is not int or disposition_revision < 0
                    or (current_annotation is not None and not _id(current_annotation))
                    or (status is not None and status not in {"accepted", "needs_review"})
                    or (score is not None and (not isinstance(score, (int, float)) or type(score) is bool
                                               or not 0 <= score <= 1))):
                raise ClassificationUnavailable()
            items.append(ReviewItem(row["id"], row.get("asset_id"), str(row.get("state", "")),
                                    label, status, float(score) if score is not None else None,
                                    human, row.get("error") if isinstance(row.get("error"), str) else None,
                                    disposition, disposition_revision,
                                    row.get("disposition_note") if isinstance(row.get("disposition_note"), str) else None,
                                    current_annotation))
        return tuple(items)
