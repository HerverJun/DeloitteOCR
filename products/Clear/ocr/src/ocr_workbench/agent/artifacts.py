"""Durable exports: staged file -> atomic directory publish -> ready receipt."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import time

from ocr_workbench.atomic_files import write_json
from ocr_workbench.store import Conflict, now, uid
from .contracts import ToolResult
from .policy import AgentPolicy, PolicyDenied
from .store import canonical, digest


class PartialExportRequired(PolicyDenied):
    def __init__(self, manifest, scope):
        super().__init__('partial_results', '部分页面未覆盖，是否仅导出已有结果？')
        self.manifest, self.scope = manifest, scope


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Artifacts:
    def __init__(self, agent, services, operations):
        self.agent, self.services, self.operations = agent, services, operations
        self.store = agent.business

    def directory(self, project_id, artifact_id):
        from re import fullmatch
        if not all(isinstance(key, str) and fullmatch("[a-f0-9]{32}", key) for key in (project_id, artifact_id)):
            raise ValueError("产物路径标识无效")
        relative = Path("agent-artifacts") / project_id / artifact_id
        path = self.store.root / relative
        for ancestor in (self.store.root / "agent-artifacts", path.parent, path):
            if ancestor.is_symlink() or ancestor.is_junction():
                raise ValueError("产物目录不能是符号链接或重解析点")
        if not path.resolve().is_relative_to(self.store.root.resolve()):
            raise ValueError("产物超出工作区")
        return path

    def _manifest(self, db, context, args):
        run = self.agent._run(db, context['run']['project_id'], context['run']['id'])
        selection = run['context'].get('selection')
        selected_pages = {p['page_id'] for p in selection['pages']} if selection is not None else None
        if args.source == "explicit_results":
            refs = [item.model_dump() for item in args.results]
            coverage = {"source": "explicit_results", "requested_results": len(refs), "failed_pages": []}
        else:
            refs, failed, pending = [], [], []
            for row in db.execute("SELECT result FROM agent_operations WHERE run_id=? AND result IS NOT NULL", (args.source_run_id,)):
                result = json.loads(row[0])
                refs.extend(result.get("results", []))
                failed.extend(result.get("coverage", {}).get("failed", []) + result.get("coverage", {}).get("uncovered", []))
                pending.extend(result.get("coverage", {}).get("pending", []))
            if selected_pages is not None:
                refs = [r for r in refs if AgentPolicy._owned(db, run['project_id'], 'result', r['result_id'])['page_id'] in selected_pages]
                failed = [p for p in failed if p['page_id'] in selected_pages]
                pending = [p for p in pending if p['page_id'] in selected_pages]
            coverage = {"source": "run_results", "source_run_id": args.source_run_id, "failed_pages": failed, "pending_pages": pending}
            if pending:
                raise PolicyDenied("partial_results", "本轮仍有未完成任务，不能提前导出")
        refs = list({ref["result_id"]: ref for ref in refs}.values())
        if not refs:
            raise PolicyDenied("not_found", "导出范围没有可用结果")
        sources = []
        for ref in refs:
            current = AgentPolicy._owned(db, run["project_id"], "result", ref["result_id"])
            if (current["revision"], current["version_id"]) != (ref["revision"], ref["version_id"]):
                raise PolicyDenied("stale_revision", "导出范围包含已变化的结果")
            detail = db.execute("""SELECT t.engine,t.image_id,p.page_number,r.original,v.sha256 version_sha256,d.sha256 document_sha256 FROM results r
                JOIN tasks t ON t.id=r.task_id JOIN pages p ON p.image_id=t.image_id
                JOIN versions v ON v.id=t.version_id JOIN documents d ON d.id=p.document_id WHERE r.id=?""", (ref['result_id'],)).fetchone()
            review = db.execute('SELECT * FROM reviews WHERE image_id=?', (detail['image_id'],)).fetchone()
            original = json.loads(detail['original'])
            sources.append({**ref, 'document_id': current['document_id'], 'page_id': current['page_id'],
                'page_number': detail['page_number'], 'engine': detail['engine'],
                'ocr_engines': original.get('document', {}).get('ocr_sources', []),
                'model_revisions': original.get('model_revisions', {}),
                'document_sha256': detail['document_sha256'], 'version_sha256': detail['version_sha256'],
                'pipeline_version': original.get('document', {}).get('pipeline_version'),
                'is_adopted': current['adopted_result'] == ref['result_id'],
                'human_confirmed': bool(review and review['status'] == 'confirmed' and
                    (review['result_id'], review['revision'], review['version_id']) == (ref['result_id'], ref['revision'], ref['version_id'])),
                'edited_sha256': digest(json.loads(current['edited']))})
        if args.source == 'run_results':
            # Worker completion order and random task IDs must not reorder a
            # selected PDF range in the exported workbook/text collection.
            order = {page['page_id']: index for index, page in enumerate(selection['pages'])} if selection else {}
            pairs = sorted(zip(refs, sources), key=lambda pair: (order.get(pair[1]['page_id'], len(order)),
                pair[1]['document_id'], pair[1]['page_number'], pair[1]['result_id']))
            refs, sources = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
        manifest = {"results": refs, "coverage": coverage, "format": args.format,
                "aggregate": args.aggregate, "confirmed_only": args.confirmed_only, "partial_policy": args.partial_policy,
                'project_id': run['project_id'], 'run_id': run['id'], 'sources': sources}
        if coverage['failed_pages'] and args.partial_policy != 'allow':
            if args.partial_policy != 'ask':
                raise PolicyDenied('partial_results', '部分页面未覆盖，当前导出策略禁止部分导出')
            scope = {'manifest_hash': digest(manifest), 'results': refs, 'scope_revision': run['context'].get('scope_revision'),
                     'step': context['step'], 'call_id': context['call_id']}
            decisions = db.execute("SELECT * FROM agent_decisions WHERE run_id=? AND generation=? AND kind='partial_export' AND status='resolved' AND reply='allow' AND scope=? AND expires>?",
                (run['id'], context['generation'], canonical(scope), time.time())).fetchall()
            if not decisions:
                raise PartialExportRequired(manifest, scope)
            manifest['partial_authorization'] = decisions[0]['id']
        return manifest

    def export(self, args, context, *, fault=None):
        run = context["run"]
        if args.source == 'run_results':
            from .jobs import JobBridge
            bridge = JobBridge(self.agent, self.services)
            for row in self.store.rows("""SELECT DISTINCT o.id FROM agent_operations o JOIN agent_job_links j ON j.operation_id=o.id
                WHERE o.run_id=?""", (args.source_run_id,)):
                bridge.operation_result(run['project_id'], args.source_run_id, row['id'])
        def fingerprint(db):
            # Replay a particular provider call against its published snapshot.
            # A new call after retrying pages gets a new result-manifest identity.
            linked = db.execute("""SELECT a.manifest FROM agent_calls c JOIN agent_artifacts a ON a.operation_id=c.operation_id
                WHERE c.run_id=? AND c.step=? AND c.provider_call_id=?""", (run["id"], context["step"], context["call_id"])).fetchone()
            return json.loads(linked[0]) if linked else self._manifest(db, context, args)
        def effect(db, operation_id):
            manifest = self._manifest(db, context, args)
            key = uid()
            folder = self.directory(run["project_id"], key)
            db.execute("""INSERT INTO agent_artifacts(id,project_id,run_id,operation_id,relative_path,mime,manifest,state,expires,created)
                VALUES(?,?,?,?,?,?,?,'staging',?,?)""", (key, run["project_id"], run["id"], operation_id,
                str(folder.relative_to(self.store.root)).replace("\\", "/"), "application/octet-stream", canonical(manifest),
                time.time() + run["limits"].get("artifact_retention_days", 30) * 86400, now()))
            return {"artifact_id": key}, []
        try:
            operation = self.operations.submit(context, "export_results", args.model_dump(exclude_none=True), effect,
                                               fingerprint=fingerprint, identity_by_input=True, deferred_finish=True)
        except PartialExportRequired as error:
            count = len({p['page_id'] for p in error.manifest['coverage']['failed_pages']})
            decision = self.agent.create_decision(run['project_id'], run['id'], context['generation'], {
                'question': f"{count} 页未完整覆盖，目前有 {len(error.manifest['results'])} 份结果。是否仅导出这些结果，并在清单中保留缺失页？",
                'options': [{'id': 'allow', 'label': '仅导出已有结果'}, {'id': 'return', 'label': '暂不导出，返回助手'}]}, kind='partial_export', scope=error.scope)
            return ToolResult(status='needs_user', summary='部分导出等待你的选择', data={'decision_id': decision['id'], 'decision_kind': 'partial_export'}).model_dump(mode='json')
        artifact_id = operation["artifact_id"]
        with self.store.file_lock:
            artifact = self.get(run["project_id"], artifact_id)
            if artifact["state"] == "ready":
                self.verify(run["project_id"], artifact_id)
                self._finish_ready_operation(artifact)
                return self.result(artifact_id, artifact)
            if artifact["state"] != "staging":
                raise PolicyDenied("artifact_unavailable", "导出产物不可用，请明确创建新的导出")
            folder = self.directory(run["project_id"], artifact_id)
            if folder.exists():
                # Crash after file publication and before DB ready commit.
                artifact = self.reconcile(run["project_id"], artifact_id)
                return self.result(artifact_id, artifact)
            manifest = json.loads(artifact["manifest"])
            staging = folder.parent / (artifact_id + ".staging-" + uid())
            folder.parent.mkdir(parents=True, exist_ok=True)
            staging.mkdir()
            generated = None
            try:
                body = {"result_ids": [r["result_id"] for r in manifest["results"]], "format": args.format,
                        "aggregate": args.aggregate, "confirmed_only": args.confirmed_only,
                        "expected_results": {r["result_id"]: r for r in manifest["results"]}}
                try:
                    generated = self.services.export(body)
                except Conflict:
                    raise
                except ValueError as error:
                    with self.store.transaction() as db:
                        db.execute("UPDATE agent_artifacts SET state='failed' WHERE id=? AND state='staging'", (artifact_id,))
                    raise PolicyDenied('business_failed', str(error)[:1000]) from None
                partial = bool(manifest["coverage"].get("failed_pages"))
                target = staging / ("export-partial" if partial else "export")
                target = target.with_suffix(generated.suffix.lower())
                with generated.open("rb") as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                published = {"artifact_id": artifact_id, "project_id": run["project_id"], "file": target.name,
                             "sha256": file_hash(target), "bytes": target.stat().st_size,
                             "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream", "manifest": manifest}
                write_json(staging / "manifest.json", published, durable=True)
                if fault:
                    fault("before_publish")
                current = self.agent.run(run["project_id"], run["id"])
                self.agent.require_generation(current, context["generation"])
                os.replace(staging, folder)
                if fault:
                    fault("after_publish")
                artifact = self.reconcile(run["project_id"], artifact_id)
                return self.result(artifact_id, artifact)
            finally:
                if generated is not None:
                    shutil.rmtree(generated.parent, ignore_errors=True)
                if staging.exists():
                    shutil.rmtree(staging)

    def get(self, project_id, artifact_id):
        rows = self.store.rows("SELECT * FROM agent_artifacts WHERE id=? AND project_id=?", (artifact_id, project_id))
        if not rows:
            raise KeyError("产物不属于当前项目")
        return rows[0]

    def _finish_ready_operation(self, artifact, db=None):
        # Replay may arrive after the ready commit but before the response.
        # Only the verified, explicitly identified export is safe to finish.
        if db is None:
            with self.store.transaction() as db:
                self._finish_ready_operation(artifact, db)
            return
        db.execute("""UPDATE agent_operations SET state='finished',updated=?
            WHERE id=? AND project_id=? AND run_id=? AND state='submitted'
            AND NOT EXISTS (SELECT 1 FROM agent_job_links WHERE operation_id=?)""",
            (now(), artifact['operation_id'], artifact['project_id'], artifact['run_id'], artifact['operation_id']))

    def _published(self, artifact):
        folder = self.directory(artifact["project_id"], artifact["id"])
        receipt_path = folder / "manifest.json"
        if receipt_path.is_symlink() or receipt_path.is_junction():
            raise ValueError("产物清单不能是重解析点")
        receipt = json.loads(receipt_path.read_text("utf-8"))
        if receipt.get("artifact_id") != artifact["id"] or receipt.get("project_id") != artifact["project_id"] or receipt.get("manifest") != json.loads(artifact["manifest"]):
            raise ValueError("产物清单与业务记录不一致")
        name = receipt.get("file")
        if not isinstance(name, str) or not re.fullmatch(r"export(?:-partial)?\.[a-z0-9]+", name):
            raise ValueError("产物文件名无效")
        target = folder / name
        if target.is_symlink() or target.is_junction():
            raise ValueError("产物不能是重解析点")
        if target.stat().st_size != receipt["bytes"] or file_hash(target) != receipt["sha256"]:
            raise ValueError("产物哈希校验失败")
        return target, receipt

    def reconcile(self, project_id, artifact_id):
        artifact = self.get(project_id, artifact_id)
        try:
            target, receipt = self._published(artifact)
        except (OSError, ValueError, KeyError, TypeError):
            with self.store.transaction() as db:
                db.execute("UPDATE agent_artifacts SET state='corrupt' WHERE id=?", (artifact_id,))
            raise PolicyDenied("artifact_unavailable", "导出文件缺失或损坏，未标记为可下载") from None
        with self.store.transaction() as db:
            db.execute("UPDATE agent_artifacts SET state='ready',relative_path=?,sha256=?,bytes=?,mime=? WHERE id=?",
                       (str(target.relative_to(self.store.root)).replace("\\", "/"), receipt["sha256"], receipt["bytes"], receipt["mime"], artifact_id))
            self._finish_ready_operation(artifact, db)
            run = self.agent._run(db, project_id, artifact["run_id"])
            self.agent._event(db, run["session_id"], run["id"], run["generation"], artifact_id + ":ready", "artifact_ready", {"artifact_id": artifact_id, "state": "ready", "bytes": receipt["bytes"]})
        return self.get(project_id, artifact_id)

    def verify(self, project_id, artifact_id):
        artifact = self.get(project_id, artifact_id)
        if artifact["state"] != "ready":
            raise PolicyDenied("artifact_unavailable", "产物当前不可下载")
        if not artifact['pinned'] and artifact['expires'] <= time.time():
            raise PolicyDenied('artifact_unavailable', '产物保留期已到，请重新导出')
        try:
            target, receipt = self._published(artifact)
            if (receipt["sha256"] != artifact["sha256"] or receipt["bytes"] != artifact["bytes"]
                    or receipt["mime"] != artifact["mime"]
                    or target.relative_to(self.store.root).as_posix() != artifact["relative_path"]):
                raise ValueError("Published manifest changed")
            return target
        except FileNotFoundError:
            with self.store.transaction() as db:
                db.execute("UPDATE agent_artifacts SET state='missing' WHERE id=?", (artifact_id,))
            raise PolicyDenied("artifact_unavailable", "产物文件缺失，请重新导出") from None
        except (ValueError, TypeError, KeyError):
            with self.store.transaction() as db:
                db.execute("UPDATE agent_artifacts SET state='corrupt' WHERE id=?", (artifact_id,))
            raise PolicyDenied("artifact_unavailable", "产物文件或清单校验失败，请重新导出") from None
        except OSError:
            # A sharing violation or temporary I/O error is not proof that the
            # retained file has disappeared or its contents have been damaged.
            raise PolicyDenied("artifact_unavailable", "产物文件暂时无法读取，请稍后重试") from None

    @staticmethod
    def result(artifact_id, artifact):
        manifest = json.loads(artifact["manifest"])
        partial = bool(manifest["coverage"].get("failed_pages"))
        return ToolResult(status="partial" if partial else "success", summary="导出已保存，可在会话历史再次下载" + ("；仅包含成功页" if partial else ""),
                          data={"result_count": len(manifest["results"]), "partial": partial}, artifact_refs=[{"artifact_id": artifact_id, "state": "ready"}]).model_dump(mode="json")

    def lease(self, project_id, artifact_id):
        with self.store.file_lock:
            target = self.verify(project_id, artifact_id)
            key = uid()
            with self.store.transaction() as db:
                db.execute("INSERT INTO agent_artifact_leases VALUES(?,?,?)", (key, artifact_id, time.time() + 3600))
            return key, target

    def coverage_manifest(self, project_id, artifact_id):
        artifact = self.get(project_id, artifact_id)
        if not json.loads(artifact['manifest'])['coverage'].get('failed_pages'):
            raise PolicyDenied('artifact_unavailable', '完整导出没有部分覆盖清单')
        lease, target = self.lease(project_id, artifact_id)
        return lease, target.parent / 'manifest.json'

    def release(self, lease_id):
        with self.store.transaction() as db:
            db.execute("DELETE FROM agent_artifact_leases WHERE id=?", (lease_id,))

    def renew(self, lease_id):
        with self.store.transaction() as db:
            changed = db.execute("UPDATE agent_artifact_leases SET expires=? WHERE id=?", (time.time() + 3600, lease_id)).rowcount
            if not changed:
                raise Conflict('下载租约已失效')

    def pin(self, project_id, artifact_id, pinned):
        with self.store.file_lock, self.store.transaction() as db:
            artifact = self.get(project_id, artifact_id)
            if artifact['state'] not in {'ready', 'staging'}:
                raise Conflict('产物已不可用，不能修改保留状态')
            db.execute('UPDATE agent_artifacts SET pinned=? WHERE id=?', (int(pinned), artifact_id))

    def delete(self, project_id, artifact_id, *, expired_only=False):
        with self.store.file_lock:
            with self.store.transaction() as db:
                db.execute("BEGIN IMMEDIATE")
                artifact = self.get(project_id, artifact_id)
                if artifact['state'] in {'deleted', 'expired'}:
                    return False
                if artifact["pinned"]:
                    raise Conflict("已保留的产物不能自动或直接清理，请先取消保留")
                if expired_only and artifact["expires"] > time.time() and artifact['state'] not in {'deleting', 'expiring'}:
                    return False
                if db.execute("SELECT 1 FROM agent_runs WHERE id=? AND status NOT IN ('completed','failed','cancelled')", (artifact['run_id'],)).fetchone():
                    raise Conflict('运行仍活跃，产物暂不能清理')
                if db.execute("SELECT 1 FROM agent_artifact_leases WHERE artifact_id=? AND expires>?", (artifact_id, time.time())).fetchone():
                    raise Conflict("产物正在下载，请稍后清理")
                expiring = artifact['state'] == 'expiring' or (expired_only and artifact['state'] != 'deleting')
                db.execute('UPDATE agent_artifacts SET state=? WHERE id=?', ('expiring' if expiring else 'deleting', artifact_id))
            directory = self.directory(project_id, artifact_id)
            if directory.exists():
                shutil.rmtree(directory)
            with self.store.transaction() as db:
                db.execute("UPDATE agent_artifacts SET state=? WHERE id=?", ("expired" if expiring else "deleted", artifact_id))
            return True

    def cleanup(self, limit=20):
        rows = self.store.rows("""SELECT a.id,a.project_id FROM agent_artifacts a JOIN agent_runs r ON r.id=a.run_id
            WHERE a.pinned=0 AND a.state NOT IN ('expired','deleted') AND (a.expires<=? OR a.state IN ('deleting','expiring'))
            AND r.status IN ('completed','failed','cancelled') ORDER BY a.expires,a.id LIMIT ?""", (time.time(), limit))
        removed = []
        for row in rows:
            try:
                if self.delete(row['project_id'], row['id'], expired_only=True):
                    removed.append(row['id'])
            except Conflict:
                continue
        with self.store.transaction() as db:
            db.execute('DELETE FROM agent_artifact_leases WHERE expires<=?', (time.time(),))
        return removed
