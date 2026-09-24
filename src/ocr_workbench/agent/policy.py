"""Resolve real resource ownership and authoritative, scoped user grants."""
from __future__ import annotations

import json
import time

from ocr_workbench.store import Conflict, uid
from .contracts import validate_tool_arguments
from .store import canonical, digest

PERMISSIONS = {
    "get_workspace_context": "project_read", "search_document": "project_read", "read_page_result": "project_read",
    "process_pages": "scoped_processing", "run_ocr": "scoped_processing", "inspect_table": "project_read",
    "request_visual_review": "scoped_visual_review", "get_job_status": "linked_jobs_read",
    "retry_failed_jobs": "scoped_failed_subset", "export_results": "scoped_new_artifact",
    "navigate_to_evidence": "verified_navigation_suggestion", "ask_user": "clarification_only",
}
READ_PERMISSIONS = {"project_read", "linked_jobs_read", "verified_navigation_suggestion", "clarification_only"}


class PolicyDenied(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AgentPolicy:
    def __init__(self, agent_store, *, review_only=False):
        self.agent = agent_store
        self.store = agent_store.business
        self.review_only = review_only

    def grant(self, project_id, permission, scope, *, source, expires, run_id=None):
        """Trusted UI/user-request boundary only; never registered as a model tool."""
        if permission not in set(PERMISSIONS.values()) | {"scoped_check_job", "controller_content", "visual_images"}:
            raise ValueError("未知授权类型")
        if source not in {"user_request", "bound_ui_scope", "decision"} or expires <= time.time():
            raise ValueError("授权来源或有效期无效")
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError("项目不存在")
            if run_id:
                self.agent._run(db, project_id, run_id)
            previous = db.execute("""SELECT id FROM agent_grants WHERE project_id=? AND run_id IS ? AND permission=?
                AND scope_hash=? AND source=? AND revoked=0 AND expires>?""", (project_id, run_id, permission, digest(scope), source, time.time())).fetchone()
            if previous:
                return previous[0]
            key = uid()
            db.execute("INSERT INTO agent_grants VALUES(?,?,?,?,?,?,?,?,0)",
                       (key, project_id, run_id, permission, digest(scope), canonical(scope), source, expires))
        return key

    def check_grant(self, project_id, run_id, permission, scope):
        grants = self.store.rows("""SELECT scope FROM agent_grants WHERE project_id=? AND permission=?
            AND (run_id IS NULL OR run_id=?) AND revoked=0 AND expires>?""", (project_id, permission, run_id, time.time()))
        for row in grants:
            allowed = json.loads(row["scope"])
            if self._covers(allowed, scope):
                return
        raise PolicyDenied("authorization_required", "该动作超出已授权范围，请在工作台确认具体范围")

    @staticmethod
    def _covers(allowed, requested):
        # Every requested key must be bound. No omitted endpoint/role wildcard.
        for key, value in requested.items():
            if key not in allowed:
                return False
            if isinstance(value, list):
                if not isinstance(allowed[key], list) or not set(value) <= set(allowed[key]):
                    return False
            elif allowed[key] != value:
                return False
        return True

    @staticmethod
    def _owned(db, project_id, kind, key):
        queries = {
            "document": "SELECT id document_id,project_id FROM documents WHERE id=?",
            "page": "SELECT p.id page_id,p.document_id,d.project_id FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?",
            "version": """SELECT v.id version_id,i.project_id,p.id page_id,p.document_id,i.active_version
                FROM versions v JOIN images i ON i.id=v.image_id JOIN pages p ON p.image_id=i.id WHERE v.id=?""",
            "result": """SELECT r.id result_id,r.revision,t.version_id,t.project_id,p.id page_id,p.document_id,r.edited,
                s.result_id adopted_result,i.active_version FROM results r JOIN tasks t ON t.id=r.task_id
                JOIN images i ON i.id=t.image_id JOIN pages p ON p.image_id=i.id
                LEFT JOIN selections s ON s.image_id=i.id WHERE r.id=?""",
        }
        row = db.execute(queries[kind], (key,)).fetchone()
        if not row or row["project_id"] != project_id:
            raise PolicyDenied("scope_denied", "资源不属于当前会话项目或已不存在")
        return dict(row)

    def authorize(self, run, generation, tool, arguments):
        parsed = validate_tool_arguments(tool, arguments)
        args = parsed.model_dump(exclude_none=True)
        permission = "scoped_check_job" if tool == "inspect_table" and args["action"] == "generate_candidates" else PERMISSIONS[tool]
        project_id = run["project_id"]
        scope = {}
        with self.store.transaction() as db:
            current = self.agent._run(db, project_id, run["id"])
            self.agent.require_generation(current, generation)
            if tool == "get_workspace_context" and args.get("selection_token") and args["selection_token"] != current["context"].get("selection_token"):
                raise PolicyDenied("scope_denied", "选择快照不属于本轮用户消息")
            resources = []
            for field, kind in (("document_id", "document"), ("page_id", "page"), ("result_id", "result")):
                if field in args:
                    resources.append(self._owned(db, project_id, kind, args[field]))
            for version_id in args.get("version_ids", []):
                resource = self._owned(db, project_id, "version", version_id)
                if resource["active_version"] != version_id:
                    raise PolicyDenied("stale_revision", "页面版本已改变")
                resources.append(resource)
            if "version_id" in args:
                version = self._owned(db, project_id, "version", args["version_id"])
                if not resources or version["version_id"] != resources[0].get("version_id"):
                    raise PolicyDenied("stale_revision", "结果与页面版本不一致")
            if "revision" in args and (not resources or resources[0].get("revision") != args["revision"]):
                raise PolicyDenied("stale_revision", "结果已被修改，请刷新后重试")
            if tool in {"inspect_table", "request_visual_review"}:
                resource = resources[0]
                if permission != "project_read" and (resource["adopted_result"] != resource["result_id"] or resource["active_version"] != resource["version_id"]):
                    raise PolicyDenied("stale_revision", "请先在现有入口采用对应版本")
                edit = json.loads(resource["edited"])
                tables = {f"table:{index}": table for index, table in enumerate(edit.get("tables", []))}
                if "table_id" in args and args["table_id"] not in tables:
                    raise PolicyDenied("not_found", "指定表格不存在")
                if "target_ids" in args:
                    from .visual import targets_for_result
                    targets = {target['id'] for target in targets_for_result(db, resource['result_id'], edit=edit)}
                    if not set(args["target_ids"]) <= targets:
                        raise PolicyDenied("not_found", "指定审校目标不存在")
            if tool == "process_pages":
                rows = db.execute("SELECT id,page_number,document_id FROM pages WHERE document_id=?", (args["document_id"],)).fetchall()
                by_number = {row["page_number"]: dict(row) for row in rows}
                if not set(args["page_numbers"]) <= by_number.keys():
                    raise PolicyDenied("not_found", "请求包含不存在的页面")
                resources += [{"page_id": by_number[n]["id"], "document_id": args["document_id"]} for n in args["page_numbers"]]
                if current["context"].get("selection"):
                    from .selection import validate_snapshot
                    validate_snapshot(db, project_id, current["context"]["selection"],
                                      page_ids={by_number[n]["id"] for n in args["page_numbers"]}, allow_initial_render=True)
            if tool == "read_page_result" and args["source"] == "run_result":
                result = next(r for r in resources if "result_id" in r)
                page = next(r for r in resources if "result_id" not in r)
                linked = db.execute("""SELECT 1 FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
                    LEFT JOIN tasks t ON t.id=j.job_id AND j.job_kind IN ('ocr','fusion')
                    LEFT JOIN document_stages s ON s.id=j.job_id AND j.job_kind='pdf_stage'
                    WHERE o.run_id=? AND (t.result_id=? OR json_extract(s.output,'$.result_id')=?)""",
                    (run["id"], args["result_id"], args["result_id"])).fetchone()
                if not linked or result["page_id"] != page["page_id"]:
                    raise PolicyDenied("scope_denied", "结果不属于本轮或指定页面")
            for reference in args.get("results", []):
                resource = self._owned(db, project_id, "result", reference["result_id"])
                if (resource["revision"], resource["version_id"]) != (reference["revision"], reference["version_id"]):
                    raise PolicyDenied("stale_revision", "导出结果版本已变化")
                resources.append(resource)
            if "source_run_id" in args:
                source = self.agent._run(db, project_id, args["source_run_id"])
                if source["session_id"] != run["session_id"]:
                    raise PolicyDenied("scope_denied", "不能导出其他会话的任务范围")
                scope["source_run_id"] = source["id"]
            for job in args.get("jobs", []):
                link = db.execute("""SELECT o.run_id,o.id operation_id,r.session_id FROM agent_job_links j
                    JOIN agent_operations o ON o.id=j.operation_id JOIN agent_runs r ON r.id=o.run_id
                    WHERE j.job_kind=? AND j.job_id=? AND o.project_id=? AND r.session_id=?""",
                    (job["kind"], job["job_id"], project_id, run["session_id"])).fetchall()
                if tool == "retry_failed_jobs":
                    link = [row for row in link if row["run_id"] == args["previous_run_id"] and row["operation_id"] == args["operation_id"]]
                if not link:
                    raise PolicyDenied("scope_denied", "任务未关联当前会话和操作")
                scope.setdefault("jobs", []).append(job["kind"] + ":" + job["job_id"])
            if tool == "navigate_to_evidence" or args.get("evidence_ref_ids"):
                for ref in [args["evidence_ref_id"]] if tool == "navigate_to_evidence" else args["evidence_ref_ids"]:
                    row = db.execute("""SELECT e.reference FROM agent_evidence e JOIN agent_runs r ON r.id=e.run_id
                        WHERE e.id=? AND r.session_id=?""", (ref, run["session_id"])).fetchone()
                    if not row:
                        raise PolicyDenied("scope_denied", "证据引用不属于当前会话")
                    reference = json.loads(row[0])
                    resource = self._owned(db, project_id, "result", reference["result_id"])
                    if (resource["revision"], resource["version_id"]) != (reference["revision"], reference["version_id"]):
                        raise PolicyDenied("stale_revision", "证据版本已变化，请重新读取")
            for singular in ("document_id", "page_id", "version_id", "result_id"):
                values = sorted({r[singular] for r in resources if r.get(singular)})
                if values:
                    scope[singular + "s"] = values
            if self.review_only and tool == 'retry_failed_jobs':
                for job in args['jobs']:
                    if job['kind'] == 'visual_review':
                        allowed = db.execute("SELECT 1 FROM multimodal_requests WHERE task_id=? AND backend='external'", (job['job_id'],)).fetchone()
                    elif job['kind'] == 'pdf_stage':
                        allowed = db.execute("SELECT 1 FROM document_stages WHERE id=? AND json_extract(parameters,'$.mode')='native'", (job['job_id'],)).fetchone()
                    else:
                        allowed = False
                    if not allowed:
                        raise PolicyDenied('unsupported_capability', '仅校对模式不能重试本地模型任务')
        if self.review_only and (tool == "run_ocr" or tool == "process_pages" and args["mode"] != "native"):
            raise PolicyDenied("unsupported_capability", "仅校对模式不允许 OCR；可以进行原生提取")
        if permission not in READ_PERMISSIONS:
            if current['context'].get('scope_blocked'):
                raise PolicyDenied('authorization_required', '追加范围未通过版本检查，请刷新选择后发送；当前只能查询')
            if 'scope_revision' in current['context']:
                scope['scope_revision'] = current['context']['scope_revision']
            if tool == "export_results":
                scope.update(format=args["format"], partial_policy=args["partial_policy"])
            if tool == "run_ocr":
                scope["engines"] = args["engines"]
            if tool == "request_visual_review":
                scope.update(target_ids=args["target_ids"], visual_model_id=args["visual_model_id"], revision=args["revision"])
            if tool == "inspect_table":
                scope.update(table_id=args["table_id"], revision=args["revision"], checks=args["checks"])
            if tool == 'retry_failed_jobs':
                scope.update(previous_run_id=args['previous_run_id'], operation_id=args['operation_id'], action=args['action'])
            if tool == "process_pages":
                scope.update(mode=args["mode"], force=args["force"])
                if args.get("engine"):
                    scope["engine"] = args["engine"]
            self.check_grant(project_id, run["id"], permission, scope)
        return parsed

    def authorize_outbound(self, run, *, role, endpoint, config_revision, document_ids, page_ids, data_kinds):
        if role not in {"controller", "visual"}:
            raise ValueError("未知外发角色")
        permission = "controller_content" if role == "controller" else "visual_images"
        self.check_grant(run["project_id"], run["id"], permission, {
            "role": role, "endpoint": endpoint, "config_revision": config_revision,
            "document_ids": sorted(document_ids), "page_ids": sorted(page_ids), "data_kinds": sorted(data_kinds),
        })
