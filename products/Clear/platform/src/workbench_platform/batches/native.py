"""Durable batch composition of the existing native import/job/export contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
from .store import fingerprint
from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.data_api.service import DataError
from workbench_platform.jobs.native_classification import ClassificationConflict
from workbench_platform.jobs.native_ocr import OcrResultConflict, ResultArtifact

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

    def preflight(self, template, inputs, *, check_runtime=True):
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
            elif suffix not in ({".png", ".jpg", ".jpeg", ".bmp", ".webp"}
                                | ({".tif", ".tiff"} if scenario == "ocr" else set())):
                issues.append(_issue(asset.asset_id, "input", "此资料不是受支持图像",
                                      asset.original_name, source=source))
            if (asset.revision, asset.blob_hash) != (item["asset_revision"], item["blob_hash"]):
                issues.append(_issue(asset.asset_id, "input", "资料版本已变化",
                                      "请重新选择当前版本，再启动批次。", source=source))
        if not check_runtime:
            return issues
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

    def prepare_runtime(self, batch, checkpoint, cancelled):
        """Prepare only new work; recovery never starts a replacement instance."""
        stopping = getattr(cancelled, "stopping", lambda: False)

        def interrupted():
            requested = cancelled()
            if stopping():
                raise NativeBatchError("服务关闭，原准备请求将在恢复后核实", "queued")
            return requested

        receipts = batch["receipts"]
        scenario, config = batch["template"]["scenario"], batch["template"]["config"]
        previous = receipts.get("native_prepare", {})
        stable_id = "l" + hashlib.sha256(_json([batch["batch_id"], scenario, 1]).encode()).hexdigest()[:32]
        preparation = {"app_id": scenario, "attempt": 1, "launch_id": stable_id, **previous}

        def publish(phase, code, message, **values):
            preparation.update(phase=phase, next_action={"code": code, "message": message}, **values)
            checkpoint("native_prepare", dict(preparation))

        def blocked(message, *, status="waiting_input", phase="unavailable", code="retry_preparation"):
            publish(phase, code, message)
            return {"status": status, "summary": {}, "issues": [_issue("native_prepare", "capability",
                "原生准备尚未完成", message, raw_output={"phase": phase,
                    "next_action": preparation["next_action"], "launch_id": preparation["launch_id"]})],
                "artifacts": [], "artifact_state": "not_generated"}

        if interrupted() and not any(key in receipts for key in ("native_dispatch", "native_submission")):
            return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [],
                    "artifact_state": "not_generated"}
        service = getattr(self.app.state, "launch_service", None)
        # Imported/possibly dispatched work belongs to its original generation.
        # In particular, a missing current is not permission to launch then POST.
        if any(key in receipts for key in ("native_import", "native_dispatch", "native_submission")):
            original = next((receipts[key].get("instance_id") for key in
                ("native_submission", "native_dispatch", "native_import", "native_prepare")
                if receipts.get(key, {}).get("instance_id")), None)
            if not original:
                return blocked("原导入或派发缺少实例身份；请在原生工作台核实原请求，不能替换实例重新执行。",
                    status="needs_verification", code="verify_original_request")
            try:
                submitted = receipts.get("native_submission")
                try:
                    context = self.jobs._context(batch["workspace_id"], scenario, config["link_id"], original)
                except (LaunchStatus, ValueError):
                    if not submitted:
                        raise
                    # Existing D11 recovery verifies the exact native IDs before
                    # rebinding a read-only projection to a restored instance.
                    recovered = self.jobs.refresh(batch["workspace_id"],
                        self.jobs.ledger.get(batch["workspace_id"], submitted["platform_job_id"]))
                    if (recovered.get("availability") != "ready" or recovered.get("status") == "unknown"
                            or recovered.get("native_job_ids") != submitted.get("native_job_ids")
                            or recovered.get("module_project_id") != submitted.get("module_project_id")):
                        raise ValueError("原任务来源不可核实")
                    context = self.jobs._context(batch["workspace_id"], scenario, config["link_id"],
                                                 recovered["instance_id"])
                    checkpoint("native_progress", recovered)
                project_id = (submitted or {}).get("module_project_id")
                if project_id and context.module_project_id != project_id:
                    raise ValueError("原生项目关联已变化")
            except Exception:
                return blocked("原任务实例暂不可核实；请恢复或核对原实例，再继续本批次。资料和原任务身份已保留。",
                    status="needs_verification", code="verify_original_instance")
            publish("ready", "continue_original_task", "查询原导入和任务身份，不重新导入或识别。",
                instance_id=context.instance_id, base_url=context.base_url, recovery_only=True,
                original_instance_id=original)
            return None
        problems = self.preflight({**batch["template"], "workspace_id": batch["workspace_id"]},
                                  batch["inputs"], check_runtime=False)
        if problems:
            publish("unavailable", "correct_inputs", "请处理资料类型或版本问题后继续，原选择已保留。")
            return {"status": "waiting_input", "summary": {}, "issues": problems,
                    "artifacts": [], "artifact_state": "not_generated"}
        workspaces = self.app.state.workspace_store
        links = [link for link in workspaces.list_links(batch["workspace_id"])
                 if link.app_id == scenario and link.link_id == config.get("link_id")]
        if len(links) != 1 or links[0].role == "viewer":
            return blocked("请绑定当前工作空间中允许编辑的原生项目，并保存新模板版本。", code="select_project")
        if service is None:
            return blocked("平台受控启动服务不可用；请恢复平台后重试本批次。", code="restore_platform")
        registry = service.supervisor.registry
        last_verification = None
        while scenario in registry._snapshot.verifying:
            if interrupted():
                return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [],
                        "artifact_state": "not_generated"}
            verification = registry.verification(scenario)
            progress = verification.model_dump(mode="json") if verification else None
            if preparation.get("phase") != "checking" or progress != last_verification:
                publish("checking", "wait_verification", "正在校验受信应用文件，完成后继续原批次。",
                    verification=progress)
                last_verification = progress
            time.sleep(0.5)
        snapshot = registry._snapshot
        install = snapshot.installations.get(scenario)
        if install is None or not install.installed:
            return blocked("受信应用未安装或校验失败；请修复已登记的应用目录后重试，资料与模板已保留。",
                code="repair_installation")
        if service.recent is None:
            return blocked("受控启动身份存储不可用；请修复平台数据目录后重试。", code="repair_platform_storage")
        installation = {"catalog_revision": snapshot.revision,
            "working_directory": str(install.working_directory),
            "bundle_manifest_sha256": install.bundle_manifest_sha256,
            "executable_sha256": install.executable_sha256}
        if preparation.get("start_attempted") and preparation.get("installation") != installation:
            return blocked("原启动尝试绑定的应用目录已变化；请核实原启动树，不能按新目录自动重启。",
                status="needs_verification", code="verify_original_launch")
        publish("checking", "check_current", "核实当前受控实例。", installation=installation)
        try:
            if preparation.get("start_attempted"):
                try:
                    live = service.get(scenario, preparation["launch_id"])
                except LaunchStatus as error:
                    if error.status != 404:
                        raise
                    # Replaying the same durable ID can reconcile a lost start;
                    # the launch service rejects reserved/retired IDs itself.
                    if interrupted():
                        return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [],
                                "artifact_state": "not_generated"}
                    live, _ = service.start(scenario, preparation["launch_id"])
            else:
                try:
                    live = service.current(scenario)
                except LaunchStatus as error:
                    if error.status != 404:
                        raise
                    if interrupted():
                        return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [],
                                "artifact_state": "not_generated"}
                    publish("launching", "wait_launch", "正在启动已校验的原生服务。", start_attempted=True)
                    live, _ = service.start(scenario, preparation["launch_id"])
        except (LaunchStatus, OSError, TimeoutError, ValueError) as error:
            # A lost response is recovered by its original ID, never a new ID.
            try:
                live = (service.current(scenario) if isinstance(error, LaunchStatus)
                        and error.code == "RESOURCE_BUSY" else service.get(scenario, preparation["launch_id"]))
            except (LaunchStatus, OSError, TimeoutError, ValueError):
                return blocked("原启动响应或实例需核实；请核对原启动尝试后重试同一批次，不能改用新身份强行启动。",
                    status="needs_verification", code="verify_original_launch")
        launch = live.model_dump(mode="json")
        publish("checking", "check_capabilities", "正在检查原生项目与实际处理能力。",
            launch_id=live.launch_id, instance_id=live.instance_id, base_url=live.base_url, launch_receipt=launch)
        if interrupted():
            return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [],
                    "artifact_state": "not_generated"}
        try:
            context = self.jobs._context(batch["workspace_id"], scenario, config["link_id"], live.instance_id)
            if scenario == "ocr":
                available = self.jobs._adapter(context).capabilities()
                if not available.recognition:
                    review_only = available.reason == "review_only"
                    return blocked("当前实例仅可校对和导出已有结果；请通过受控配置启用完整识别模式后继续。"
                        if review_only else "原生识别队列不可用；请修复原实例后重试本批次。",
                        phase="review_only" if review_only else "unavailable",
                        code="configure_full_mode" if review_only else "restore_recognition")
            elif config.get("device", "cpu") == "cuda":
                runtime = self.jobs._adapter(context)._request("GET", "/api/v1/runtime")
                if not isinstance(runtime.get("gpu"), dict):
                    return blocked("原生服务未检测到可用 GPU；请检查 GPU 环境，或显式保存 CPU 模板后重新处理。",
                        code="check_gpu_or_select_cpu")
        except Exception:
            return blocked("原生项目或处理能力暂不可读取；请核对原实例和项目关联后重试。", code="check_native_project")
        publish("ready", "continue_batch", "原生服务已准备好，将按原资料和模板执行。", recovery_only=False)
        return None

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

        def bound_context():
            original = next((receipts[key].get("instance_id") for key in
                ("native_submission", "native_dispatch", "native_import", "native_prepare")
                if receipts.get(key, {}).get("instance_id")), None)
            if original:
                try:
                    return self.jobs._context(workspace_id, scenario, config["link_id"], original)
                except Exception as error:
                    raise NativeBatchError("原生实例需核实，不能替换原导入或任务身份") from error
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
            context = bound_context()
            binding = ({"instance_id": context.instance_id, "module_project_id": context.module_project_id}
                       if context else {})
            # Persist original generation before the first possibly lost import response.
            checkpoint("native_import", {**receipts.get("native_import", {}), **binding, "state": "pending"})
            imported = (handoff.begin(workspace_id, config["link_id"], fixed["asset_set_id"], fixed["revision"], {})
                        if scenario == "classification" else
                        handoff.begin(workspace_id, config["link_id"], fixed["asset_set_id"], fixed["revision"]))
            checkpoint("native_import", {**imported, **binding})
            if imported["state"] != "completed":
                return {"status": "needs_verification", "summary": {}, "issues": [_issue("import", "execution",
                    "资料导入需核实", "原生导入回执未到达完成状态；继续时先查询原请求，避免重复导入。")],
                    "artifacts": [], "artifact_state": "not_generated"}
        if "native_submission" not in receipts:
            if cancellation_requested() and "native_dispatch" not in receipts:
                return {"status": "cancelled", "summary": {}, "issues": [], "artifacts": [], "artifact_state": "not_generated"}
            context = bound_context()
            binding = ({"instance_id": context.instance_id, "module_project_id": context.module_project_id}
                       if context else {})
            checkpoint("native_dispatch", {**receipts.get("native_dispatch", {}), **binding,
                "status": "queued", "handoff_id": receipts["native_import"]["handoff_id"]})
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

    def _review_context(self, batch, origin=None):
        scenario = batch['template']['scenario']
        context = self.jobs._context(batch['workspace_id'], scenario, batch['template']['config']['link_id'])
        if context.role == 'viewer':
            raise DataError('当前原生项目关联仅允许查看', 403)
        original_project = batch['receipts'].get('native_submission', {}).get('module_project_id')
        if original_project and context.module_project_id != original_project:
            raise DataError('原批次原生项目关联已变化，不能替代所见结果', 409)
        if origin and (origin.get('project_id') != context.module_project_id or origin.get('link_id') != context.link_id):
            raise DataError('原操作项目或关联已变化，保持待核实', 409)
        return context, self.jobs._adapter(context)

    @staticmethod
    def _ocr_result(target):
        raw = target['raw_output']
        return ResultArtifact(raw['native_result_id'], raw['native_task_id'], raw['native_asset_id'],
            raw['native_version_id'], raw.get('selected', False), {'kind': 'image',
            'image_id': raw['native_asset_id'], 'version_id': raw['native_version_id']})

    def prepare(self, batch, targets, payload, store):
        scenario, action = batch['template']['scenario'], payload['action']
        if scenario == 'ocr' and action in {'defer', 'reopen'}:
            return {'effect': 'platform_only', 'link_id': batch['template']['config']['link_id'],
                'instance_id': batch['receipts'].get('native_submission', {}).get('instance_id')}, []
        if any(target.get('category') != 'review' for target in targets) and action in {'confirm', 'adopt'}:
            raise DataError('执行异常没有可接受的原结果，请先核实原生任务', 409)
        context, adapter = self._review_context(batch)
        origin = {'effect': 'conditional_native', 'instance_id': context.instance_id,
            'project_id': context.module_project_id, 'link_id': context.link_id,
            'input_manifest_hash': fingerprint(batch['inputs'])}
        plans = []
        if scenario == 'classification':
            job_id = batch['receipts']['native_submission']['native_job_ids'][0]
            by_id = {row.native_item_id: row for row in self.jobs._items(adapter, job_id, adapter.job(job_id).total)}
            recorded = store.native_revisions(batch['batch_id'])
            groups = {}
            for target in targets:
                raw = target['raw_output']
                row = by_id.get(raw.get('native_item_id'))
                if (row is None or row.asset_id != raw.get('native_asset_id')
                        or row.prediction_label_id != raw.get('prediction_label_id')):
                    raise DataError('分类固定结果或原图身份已变化', 409)
                expected = recorded.get(row.native_item_id, raw.get('disposition_revision', 0))
                label = raw.get('prediction_label_id') if action == 'confirm' else None
                if action == 'adopt':
                    adoption = payload['adoption']
                    if (adoption.get('native_result_id') or adoption.get('native_version_id')
                            or not adoption.get('human_label_id') or row.disposition_state != 'confirmed'
                            or row.human_label_id != adoption['human_label_id']):
                        raise DataError('采用必须明确对应原生所见人工类别', 409)
                    expected, label = adoption['native_revision'], adoption['human_label_id']
                elif action == 'confirm' and (target.get('adoption')
                        or row.human_label_id not in {None, label} or row.current_annotation_label_id not in {None, label}):
                    raise DataError('原建议不能覆盖已有人工类别，请明确采用所见原生修订', 409)
                if row.disposition_revision != expected:
                    raise DataError('原生处置版本冲突；请核对所见版本，不会自动补最新版本', 409)
                binding = {'platform_item_id': target['item_id'], 'native_item_id': row.native_item_id,
                    'native_asset_id': row.asset_id, 'original_sha256': target['source']['blob_hash'],
                    'expected_revision': expected, 'prediction_label_id': raw.get('prediction_label_id')}
                groups.setdefault(label, []).append(binding)
            for label, bindings in groups.items():
                for offset in range(0, len(bindings), 500):
                    selected = bindings[offset:offset + 500]
                    body = {'job_id': job_id, 'item_ids': sorted(item['native_item_id'] for item in selected),
                        'state': {'confirm': 'confirmed', 'adopt': 'confirmed', 'defer': 'deferred', 'reopen': 'reopened'}[action],
                        'label_id': label, 'note': payload['note'],
                        'expected_revisions': {item['native_item_id']: item['expected_revision'] for item in selected},
                        'actor': payload['actor'], 'instance_id': context.instance_id,
                        'intent': 'adopt' if action == 'adopt' else 'accept_prediction'}
                    plans.append({'scenario': scenario, 'payload': body, 'payload_hash': fingerprint(body), 'bindings': selected})
        else:
            images = {item['id']: item for item in adapter.snapshot()['images']}
            for target in targets:
                raw, requested = target['raw_output'], payload.get('adoption')
                if requested and (requested.get('human_label_id') or requested.get('native_result_id') not in {None, raw['native_result_id']}
                        or requested.get('native_version_id') not in {None, raw['native_version_id']}):
                    raise DataError('采用的结果或图像版本不属于原批次事项', 409)
                if action == 'confirm' and target.get('adoption'):
                    raise DataError('已有采用关系，请明确采用当前所见修订，不能确认旧识别结果', 409)
                revision = requested['native_revision'] if requested else raw['revision']
                snapshot = adapter.result_snapshot(self._ocr_result(target), revision)
                image = images.get(raw['native_asset_id'])
                if (not snapshot['available'] or snapshot['current_revision'] != revision
                        or snapshot['original_sha256'] != target['source']['blob_hash']
                        or not image or image.get('active_version') != raw['native_version_id']
                        or (action == 'confirm' and image.get('selected_result') != raw['native_result_id'])):
                    raise DataError('OCR 来源、所见修订或所选结果已变化，请先核对原生结果', 409)
                body = {'project_id': context.module_project_id, 'instance_id': context.instance_id,
                    'result_id': raw['native_result_id'], 'task_id': raw['native_task_id'],
                    'image_id': raw['native_asset_id'], 'version_id': raw['native_version_id'],
                    'revision': revision, 'original_sha256': snapshot['original_sha256'],
                    'actor': payload['actor'], 'note': payload['note']}
                plans.append({'scenario': scenario, 'payload': body, 'payload_hash': fingerprint(body),
                    'bindings': [{'platform_item_id': target['item_id']}], 'snapshot': snapshot})
        return origin, plans

    @staticmethod
    def _verify_segment(segment, receipt, origin):
        body = segment['payload']
        if (not isinstance(receipt, dict) or receipt.get('operation_id') != segment['operation_id']
                or receipt.get('state') != 'committed' or receipt.get('instance_id') != origin['instance_id']
                or receipt.get('project_id') != origin['project_id'] or receipt.get('payload_hash') != segment['payload_hash']
                or receipt.get('actor') != body['actor'] or receipt.get('note') != body['note']):
            raise DataError('原生回执身份或请求内容不符，保持待核实', 409)
        if segment['scenario'] == 'ocr':
            if any(receipt.get(key) != value for key, value in body.items()):
                raise DataError('OCR 回执来源或所见版本不符，保持待核实', 409)
        else:
            if (receipt.get('job_id') != body['job_id'] or receipt.get('request_id') != segment['operation_id']
                    or receipt.get('human_label_id') != body['label_id'] or receipt.get('disposition_state') != body['state']
                    or receipt.get('revisions') != {key: value + 1 for key, value in body['expected_revisions'].items()}):
                raise DataError('分类回执目标或处置版本不符，保持待核实', 409)
            items = receipt.get('items', [])
            observed = {item.get('item_id'): item for item in items if isinstance(item, dict)}
            if len(items) != len(segment['bindings']) or set(observed) != set(body['item_ids']):
                raise DataError('分类回执目标不完整，保持待核实', 409)
            for binding in segment['bindings']:
                item = observed[binding['native_item_id']]
                if (item.get('asset_id') != binding['native_asset_id'] or item.get('original_sha256') != binding['original_sha256']
                        or item.get('revision') != binding['expected_revision'] + 1):
                    raise DataError('分类回执原件身份或版本不符，保持待核实', 409)
        return receipt

    @staticmethod
    def _dispatch(adapter, segment):
        # These typed exceptions come only from this specific conditional POST
        # returning HTTP 409. Reads, local validation and lost replies cannot
        # authorize releasing the operation reservation.
        body = segment['payload']
        try:
            if segment['scenario'] == 'classification':
                return adapter._request('POST', '/api/v1/jobs/' + body['job_id'] + '/dispositions',
                    {key: value for key, value in body.items() if key != 'job_id'} |
                    {'operation_id': segment['operation_id'], 'request_id': segment['operation_id']},
                    conditional=True, max_body=65536)
            return adapter._request('POST', '/api/platform/adoptions/' + segment['operation_id'],
                                    body, result_conflict=True, max_body=16384)
        except (ClassificationConflict, OcrResultConflict) as error:
            conflict = DataError('原生明确拒绝了所见版本，未执行此分段', 409)
            conflict.definite_native_conflict = {'source': 'conditional_post_409', 'status_code': 409,
                'operation_id': segment['operation_id'], 'payload_hash': segment['payload_hash'], 'error_type': type(error).__name__}
            raise conflict from error

    def finish(self, batch, operation, store):
        operation_id = operation['operation_id']
        reconciling = operation['intent'].get('native_started', False)
        if operation['intent']['origin'].get('effect') == 'platform_only':
            return store.commit_operation(operation_id)
        try:
            context, adapter = self._review_context(batch, operation['intent']['origin'])
            origin = operation['intent']['origin']
            if not operation['intent'].get('segments'):
                raise DataError('旧操作缺少固定派发绑定，请核实原请求后恢复', 409)
            for original_segment in operation['intent']['segments']:
                operation = store.operation(operation_id, private=True)
                if operation['state'] == 'committed':
                    if operation['receipt'].get('outcome') == 'conflict':
                        raise DataError(operation['receipt']['reason'], 409)
                    return operation['receipt']
                segment = next(item for item in operation['intent']['segments'] if item['operation_id'] == original_segment['operation_id'])
                receipt = segment.get('native_receipt')
                if receipt is not None and reconciling:
                    observed = adapter.operation(segment['operation_id'], expected_instance_id=origin['instance_id'])
                    if observed != receipt:
                        raise DataError('原操作回执与持久核对回执不一致，保持待核实', 409)
                if receipt is None:
                    receipt = adapter.operation(segment['operation_id'], expected_instance_id=origin['instance_id'])
                if receipt is None:
                    if segment['native_started']:
                        raise DataError('原派发回执尚不可核实；仅查询原操作，不能重新派发', 409)
                    if context.instance_id != origin['instance_id']:
                        raise DataError('原实例已变化；未派发分段仍保留原绑定，不能替换实例补写', 409)
                    operation, claimed = store.start_segment(operation_id, segment['operation_id'])
                    if not claimed:
                        receipt = adapter.operation(segment['operation_id'], expected_instance_id=origin['instance_id'])
                        if receipt is None:
                            raise DataError('另一窗口已派发原分段，请稍后查询原回执', 409)
                    else:
                        segment = next(item for item in operation['intent']['segments'] if item['operation_id'] == segment['operation_id'])
                        try:
                            receipt = self._dispatch(adapter, segment)
                        except DataError as error:
                            evidence = getattr(error, 'definite_native_conflict', None)
                            if evidence:
                                try:
                                    store.reject_native_conflict(operation_id, evidence)
                                except DataError:
                                    pass  # A previous segment succeeded: reservation remains.
                            raise
                self._verify_segment(segment, receipt, origin)
                segment['native_receipt'], segment['state'] = receipt, 'native_committed'
                store.checkpoint_operation(operation_id, 'intent' if operation['state'] == 'intent' else 'needs_verification',
                    intent=operation['intent'])
            operation = store.operation(operation_id, private=True)
            receipts = [segment['native_receipt'] for segment in operation['intent']['segments']]
            native_receipt = {'scenario': operation['intent']['scenario'], 'segments': receipts}
            operation = store.checkpoint_operation(operation_id, 'native_committed', native_receipt=native_receipt)
            adoption = None
            if operation['payload']['action'] == 'adopt':
                segment, receipt = operation['intent']['segments'][0], receipts[0]
                adoption = {'scenario': operation['intent']['scenario'], **operation['payload']['adoption'],
                    'origin': origin, 'operation_id': operation_id, 'native_receipt': receipt,
                    'native_review_qualification': 'independent' if segment['scenario'] == 'ocr' else 'confirmed'}
                if segment['scenario'] == 'ocr':
                    adoption.update(native_result_id=receipt['result_id'], native_version_id=receipt['version_id'],
                                    snapshot=segment['snapshot'])
                else:
                    adoption['committed_native_revision'] = receipt['revisions'][segment['bindings'][0]['native_item_id']]
            return store.commit_operation(operation_id, adoption=adoption)
        except Exception as error:
            current = store.operation(operation_id, private=True)
            if current['state'] != 'committed':
                store.checkpoint_operation(operation_id, 'needs_verification', reason=str(error)[:300])
            if isinstance(error, DataError):
                raise
            raise DataError('原生响应或平台记账需核实；已保留原操作身份，请恢复原请求', 409) from error

    def revisions(self, batch, item, offset=0, limit=30):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DataError('原生历史分页参数无效')
        if batch['template']['scenario'] == 'data_compare' or item.get('category') != 'review':
            raise DataError('此事项没有可采用的原生修订', 409)
        try:
            context, adapter = self._review_context(batch)
            raw = item['raw_output']
            response = {'schema_version': '1.0', 'batch_id': batch['batch_id'], 'revision': batch['revision'],
                'item_id': item['item_id'], 'scenario': batch['template']['scenario'], 'original': raw,
                'source': item['source'], 'adoption': item.get('adoption'), 'instance_id': context.instance_id,
                'project_id': context.module_project_id, 'offset': offset, 'limit': limit}
            if batch['template']['scenario'] == 'ocr':
                result = self._ocr_result(item)
                snapshot = adapter.result_snapshot(result)
                response.update(current=snapshot, current_revision=snapshot['revision'],
                    adoptable=snapshot['available'] and snapshot['original_sha256'] == item['source']['blob_hash'],
                    history=adapter.result_history(result, offset, limit), native_review_qualification='independent')
            else:
                job_id = batch['receipts']['native_submission']['native_job_ids'][0]
                rows = self.jobs._items(adapter, job_id, adapter.job(job_id).total)
                row = next((value for value in rows if value.native_item_id == raw['native_item_id']), None)
                if row is None or row.asset_id != raw.get('native_asset_id'):
                    raise DataError('原生结果或原图不属于此事项', 409)
                from dataclasses import asdict
                history = adapter.disposition_history(job_id, raw['native_item_id'])
                response.update(current=asdict(row), history={'items': history[offset:offset + limit], 'total': len(history)},
                    current_revision=row.disposition_revision,
                    adoptable=row.disposition_state == 'confirmed' and row.human_label_id is not None,
                    native_review_qualification='native_disposition')
            return response
        except DataError:
            raise
        except Exception as error:
            raise DataError('原生修订暂不可读取；平台原结果与判断已保留', 409) from error
