"""Durable batch composition of the existing native import/job/export contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time

class NativeBatchError(ValueError):
    def __init__(self, message, status="needs_verification"):
        super().__init__(message)
        self.status = status


def _issue(item_id, category, title, detail, *, source=None, raw_output=None):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(item_id)):
        item_id = "n" + hashlib.sha256(str(item_id).encode()).hexdigest()[:32]
    return {"item_id": item_id, "category": category, "title": title, "detail": detail,
            "source": source or {}, "raw_output": raw_output if isinstance(raw_output, dict)
            else {"errors": raw_output} if raw_output else {}}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


class NativeBatchRunner:
    def __init__(self, app):
        self.app = app

    @property
    def jobs(self):
        return self.app.state.job_service

    def _handoff(self, scenario):
        return getattr(self.app.state, scenario + "_handoff")

    def options(self, workspace_id, scenario, link_id):
        if scenario not in {"classification", "ocr"}:
            raise ValueError("未知原生处理场景")
        context = self.jobs._context(workspace_id, scenario, link_id)
        if context.role == "viewer":
            raise PermissionError("当前关联仅允许查看")
        adapter = self.jobs._adapter(context)
        if scenario == "classification":
            project = adapter.project()
            return {"models": [{"id": row["id"], "name": row.get("name") or row["id"]}
                               for row in adapter.models()], "labels": project["labels"],
                    "automatic_accept": False}
        available = adapter.capabilities()
        catalog = adapter._request("GET", "/api/state").get("engines", {})
        return {"recognition": available.recognition, "reason": available.reason,
                "engines": [{"id": key, "name": row.get("name") or key}
                            for key, row in catalog.items() if isinstance(row, dict)]
                           if available.recognition else []}

    def preflight(self, template, inputs):
        scenario, config = template["scenario"], template["config"]
        issues = []
        if not inputs:
            return [_issue("inputs", "input", "请先选择资料", "此批次需要至少一份图像资料。")]
        if len(inputs) > 1000:
            issues.append(_issue("inputs", "input", "资料数量超出单批限制", "每批最多 1000 份资料。"))
        registry = self.app.state.asset_library[0]
        for item in inputs:
            asset = registry.get(template["workspace_id"], item["asset_id"])
            source = {"asset_id": asset.asset_id, "asset_revision": item["asset_revision"],
                      "blob_hash": item["blob_hash"], "name": asset.original_name}
            suffix = Path(asset.original_name).suffix.lower()
            if item.get("role") not in ({"images"} if scenario == "classification" else {"images", "documents"}):
                issues.append(_issue(asset.asset_id, "input", "资料角色与模板不匹配",
                                      "请按模板选择图像资料。", source=source))
            if suffix == ".pdf":
                issues.append(_issue(asset.asset_id, "capability", "PDF 需使用文档工作台",
                    "当前平台批次尚未接入 PDF 原生文档识别任务；已有结果可在文档工作台校对与归集。",
                    source=source))
            elif suffix not in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
                issues.append(_issue(asset.asset_id, "input", "此资料不是受支持图像",
                                      asset.original_name, source=source))
            if (asset.revision, asset.blob_hash) != (item["asset_revision"], item["blob_hash"]):
                issues.append(_issue(asset.asset_id, "input", "资料版本已变化",
                                      "请重新选择当前版本，再启动批次。", source=source))
        try:
            options = self.options(template["workspace_id"], scenario, config.get("link_id"))
            if scenario == "classification":
                if config.get("model_id") not in {row["id"] for row in options["models"]}:
                    issues.append(_issue("model", "configuration", "绑定模型已不可用",
                                          "请选择当前项目中可用的模型，并保存新模板版本。"))
                if config.get("device", "cpu") not in {"cpu", "cuda"}:
                    issues.append(_issue("device", "configuration", "运行设备无效", "请选择 CPU 或 GPU。"))
                if config.get("auto_accept"):
                    issues.append(_issue("policy", "configuration", "自动接受政策尚未验证",
                                          "当前模板仅自动执行分类，所有建议继续交回人工复核。"))
            else:
                if not options["recognition"]:
                    issues.append(_issue("recognition", "capability", "当前只能校对已有结果",
                        "仅校对模式没有识别能力，请启用完整模式后继续；本批次未宣称自动识别。"
                        if options["reason"] == "review_only" else "识别队列不可用，请恢复原生服务后继续。"))
                engines = config.get("engines", [])
                if not engines or not set(engines) <= {row["id"] for row in options["engines"]}:
                    issues.append(_issue("engines", "configuration", "识别引擎需重新选择",
                                          "请选择当前服务可用的引擎，并保存新模板版本。"))
        except Exception as error:
            issues.append(_issue("native", "capability", "原生项目或服务暂不可用", str(error)))
        return issues

    def _pin_snapshot(self, batch, handoff, checkpoint):
        """Keep exact output bytes for collection retry, including edited revision."""
        receipt = batch["receipts"].get("native_snapshot")
        batch_id = batch["batch_id"]
        if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", batch_id):
            raise ValueError("批次身份无效")
        root = self.app.state.artifact_center.store.blob_root.parent / "batch-snapshots"
        root.mkdir(exist_ok=True)
        path = root / (batch_id + ".json")
        if path.is_file():
            raw = path.read_bytes()
            if receipt and hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                raise NativeBatchError("固定结果文件已变化，不能重新计算替代该成果")
            envelope = json.loads(raw)
            if envelope["batch_id"] != batch_id:
                raise NativeBatchError("固定结果身份不匹配")
            snapshot = envelope["snapshot"]
        else:
            if receipt:
                raise NativeBatchError("固定结果文件已缺失，不能重新识别替代原成果")
            snapshot = handoff.result_snapshot(batch["workspace_id"], batch["receipts"]["native_import"]["handoff_id"])
            if batch["template"]["scenario"] == "ocr":
                context = self.jobs._context(batch["workspace_id"], "ocr", batch["template"]["config"]["link_id"])
                adapter = self.jobs._adapter(context)
                for item in snapshot["items"]:
                    detail = adapter._request("GET", "/api/results/" + item["native_result_id"])
                    if detail.get("revision") != item["revision"] or detail.get("task_id") != item["native_task_id"]:
                        raise NativeBatchError("识别结果版本在归集前变化，请重新核对固定结果")
                    item["original_output"] = detail.get("original", {})
                    item["edited_output"] = detail.get("edited", {})
            if "export" in snapshot:
                snapshot = {**snapshot, "export_base64": base64.b64encode(snapshot["export"]).decode("ascii")}
                del snapshot["export"]
            raw = _json({"batch_id": batch_id, "snapshot": snapshot}).encode("utf-8")
            temporary = path.with_suffix(".incoming")
            with temporary.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        checkpoint("native_snapshot", {"sha256": hashlib.sha256(raw).hexdigest(),
                                        "result_count": len(snapshot["items"])})
        if "export_base64" in snapshot:
            snapshot = {**snapshot, "export": base64.b64decode(snapshot["export_base64"], validate=True)}
            del snapshot["export_base64"]
        return snapshot

    def run(self, batch, checkpoint, cancelled):
        stopping = getattr(cancelled, "stopping", lambda: False)

        def check_stopping():
            if stopping():
                raise NativeBatchError("服务关闭，已提交操作将在恢复后查询", "queued")

        def cancellation_requested():
            requested = cancelled()
            # The callback also signals service shutdown. Recheck after it
            # returns so shutdown during a native read never becomes a cancel.
            check_stopping()
            return requested

        check_stopping()
        scenario = batch["template"]["scenario"]
        config = batch["template"]["config"]
        workspace_id, receipts = batch["workspace_id"], batch["receipts"]
        handoff = self._handoff(scenario)
        if cancellation_requested() and "native_submission" not in receipts and "native_dispatch" not in receipts:
            return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [],
                    "artifact_state": "not_generated"}
        # A completed native task can be collected even if the service later
        # switches to review-only; never require recognition to export a pin.
        if "native_submission" not in receipts and "native_dispatch" not in receipts:
            issues = self.preflight({**batch["template"], "workspace_id": workspace_id}, batch["inputs"])
            if issues:
                return {"status": "waiting_input", "summary": {}, "issues": issues,
                        "artifacts": [], "artifact_state": "not_generated"}
        if "native_inputs" not in receipts:
            members = []
            for index, item in enumerate(batch["inputs"]):
                identity = {key: item[key] for key in ("asset_id", "asset_revision", "blob_hash")}
                members.append({"schema_version": "1.0", "item_id": "i" + str(index + 1), **identity,
                    "source_locator": {"schema_version": "1.0", "kind": "file", **identity, "precision": "unknown"}})
            sets = self.app.state.asset_library[1]
            fixed_id = "s" + hashlib.sha256(batch["batch_id"].encode()).hexdigest()[:32]
            asset_set = sets.create(workspace_id, members, asset_set_id=fixed_id)
            checkpoint("native_inputs", {"asset_set_id": asset_set.asset_set_id,
                                          "revision": asset_set.revision, "manifest_hash": asset_set.manifest_hash})
        fixed = receipts["native_inputs"]
        if "native_import" not in receipts or receipts["native_import"].get("state") != "completed":
            if cancellation_requested():
                return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [], "artifact_state": "not_generated"}
            imported = (handoff.begin(workspace_id, config["link_id"], fixed["asset_set_id"], fixed["revision"], {})
                        if scenario == "classification" else
                        handoff.begin(workspace_id, config["link_id"], fixed["asset_set_id"], fixed["revision"]))
            checkpoint("native_import", imported)
            if imported["state"] != "completed":
                return {"status": "needs_verification", "summary": {}, "issues": [_issue("import", "execution",
                    "资料导入需核实", "原生导入回执未到达完成状态；继续时先查询原请求，避免重复导入。")],
                    "artifacts": [], "artifact_state": "not_generated"}
        if "native_submission" not in receipts:
            if cancellation_requested() and "native_dispatch" not in receipts:
                return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [], "artifact_state": "not_generated"}
            checkpoint("native_dispatch", {"status": "queued", "handoff_id": receipts["native_import"]["handoff_id"]})
            allow_new = not cancellation_requested()
            try:
                submitted = (handoff.submit(workspace_id, receipts["native_import"]["handoff_id"], config["model_id"], config.get("device", "cpu"), allow_new=allow_new)
                             if scenario == "classification" else
                             handoff.submit(workspace_id, receipts["native_import"]["handoff_id"], config["engines"], allow_new=allow_new))
            except Exception as error:
                raise NativeBatchError("原生提交响应需核实；继续时先查询原请求，避免重复派发") from error
            if submitted.get("not_submitted"):
                return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [], "artifact_state": "not_generated"}
            checkpoint("native_submission", submitted["job"])
        job = receipts["native_submission"]
        terminal = {"succeeded", "partial", "failed", "cancelled"}
        while True:
            check_stopping()
            job = self.jobs.refresh(workspace_id, self.jobs.ledger.get(workspace_id, job["platform_job_id"]))
            checkpoint("native_progress", job)
            check_stopping()
            if job["status"] in terminal:
                break
            if job.get("availability") != "ready" or job["status"] == "unknown":
                return {"status": "needs_verification", "summary": job.get("counts", {}),
                    "issues": [_issue("job", "execution", "任务状态需核实", "服务暂不可达；继续时查询已提交任务。")],
                    "artifacts": [], "artifact_state": "not_generated"}
            if cancellation_requested() and "cancel" in job.get("capabilities", []) and "native_cancel" not in receipts:
                try:
                    checkpoint("native_cancel", self.jobs.action(workspace_id, job["platform_job_id"], "cancel"))
                except Exception as error:
                    raise NativeBatchError("取消回执需核实；已保留原生任务身份，尚不能认定任务已取消") from error
            time.sleep(0.5)
        if cancellation_requested():
            return {"status": "cancelled", "summary": job.get("counts", {}),
                    "issues": [], "artifacts": [], "artifact_state": "not_generated"}
        summary = {**job.get("counts", {}), "execution_status": job["status"],
                   "execution_complete": True, "automatic_accept": False}
        if scenario == "ocr" and not summary.get("succeeded", 0):
            return {"status": "failed", "summary": summary, "issues": [_issue("job", "execution",
                    "识别任务没有成功结果", "原件与任务回执已保留；在原生工作台检查失败原因。",
                    raw_output=job.get("native_errors"))], "artifacts": [], "artifact_state": "not_generated"}
        snapshot = self._pin_snapshot(batch, handoff, checkpoint)
        issues = []
        for item in snapshot["items"]:
            item_id = item.get("native_item_id") or item.get("native_result_id")
            source = next((value for value in snapshot["inputs"] if value["asset_id"] == item["asset_id"]), None)
            state = item.get("disposition_state", "unreviewed")
            if item.get("state") == "failed":
                issues.append(_issue(item_id, "execution", "分类执行失败", item.get("error") or "需检查原件与任务日志。", source=source, raw_output=item))
            elif item.get("state") == "cancelled":
                issues.append(_issue(item_id, "execution", "分类执行已取消",
                    "本项未完成分类，原件与取消记录已保留。", source=source, raw_output=item))
            elif scenario == "classification" and state == "confirmed":
                continue
            else:
                issues.append(_issue(item_id, "review", "模型建议待复核" if scenario == "classification" else "识别结果待校对",
                    "请核对原图后确认、暂缓或重开；执行成功不代表业务结论已认可。", source=source, raw_output=item))
        for error in job.get("native_errors", []):
            if scenario == "ocr":
                issues.append(_issue(error["native_item_id"], "execution", "识别执行失败", error["message"], raw_output=error))
        summary["needs_review"] = sum(item["category"] == "review" for item in issues)
        summary["review_complete"] = False
        checkpoint("results", {"summary": summary, "issues": issues})
        checkpoint("generated", {"snapshot": receipts["native_snapshot"]})
        try:
            collected = receipts.get("native_collection")
            service = getattr(self.app.state.artifact_center, scenario)
            bundle = (service.get(workspace_id, collected["bundle_id"]) if collected else
                      service.publish(workspace_id, receipts["native_import"]["handoff_id"], snapshot=snapshot))
            checkpoint("native_collection", bundle)
        except Exception as error:
            issues.append(_issue("collection", "artifact", "成果归集失败", str(error)))
            return {"status": "partial", "summary": summary, "issues": issues,
                    "artifacts": [], "artifact_state": "collection_failed"}
        return {"status": job["status"], "summary": summary, "issues": issues,
                "artifacts": [bundle], "artifact_state": "collected"}

    def dispose(self, batch, item_ids, action, note):
        """A portal decision refers to the exact native output shown to its user."""
        targets = [item for item in batch["issues"] if item["item_id"] in item_ids and item["category"] == "review"]
        if not targets:
            return
        scenario = batch["template"]["scenario"]
        context = self.jobs._context(batch["workspace_id"], scenario, batch["template"]["config"]["link_id"])
        adapter = self.jobs._adapter(context)
        if scenario == "classification":
            job_id = batch["receipts"]["native_submission"]["native_job_ids"][0]
            native = adapter.job(job_id)
            rows = self.jobs._items(adapter, job_id, native.total)
            by_id = {row.native_item_id: row for row in rows}
            state = {"confirm": "confirmed", "defer": "deferred", "reopen": "reopened"}[action]
            groups = {}
            for target in targets:
                raw = target["raw_output"]
                row = by_id.get(raw["native_item_id"])
                if row is None or row.prediction_label_id != raw.get("prediction_label_id"):
                    raise ValueError("分类原始结果已变化，请重新打开本次结果")
                label = raw.get("prediction_label_id") if state == "confirmed" else None
                if row.disposition_state == state and row.human_label_id == label and (row.disposition_note or "") == note:
                    continue
                groups.setdefault(label, []).append(row)
            for label, selected in groups.items():
                for offset in range(0, len(selected), 500):
                    adapter.dispose(job_id, selected[offset:offset + 500], state, label, note)
        else:
            snapshot = adapter.snapshot()
            images = {row["id"]: row for row in snapshot["images"]}
            # Validate the entire selected range before writing any review.
            for target in targets:
                raw = target["raw_output"]
                image = images.get(raw["native_asset_id"])
                result = adapter._request("GET", "/api/results/" + raw["native_result_id"])
                if (not image or image.get("selected_result") != raw["native_result_id"]
                        or image.get("active_version") != raw["native_version_id"]
                        or result.get("revision") != raw["revision"]):
                    raise ValueError("请在原生工作台先采用并核对该固定版本结果；采用结果或校对内容已变化")
            for target in targets:
                raw = target["raw_output"]
                adapter._request("PUT", "/api/images/" + raw["native_asset_id"] + "/review", {
                    "status": {"confirm": "confirmed", "defer": "question", "reopen": "pending"}[action],
                    "result_id": raw["native_result_id"], "revision": raw["revision"],
                    "version_id": raw["native_version_id"]})
