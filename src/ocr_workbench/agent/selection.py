"""UI-owned selection snapshots; a model cannot mint a processing grant."""
from __future__ import annotations

import json
import time
from typing import Annotated

from pydantic import Field
from .contracts import StrictModel, Id, PageNumber
from .store import canonical
from ocr_workbench.store import uid, now, Conflict


class DocumentRange(StrictModel):
    document_id: Id
    first_page: PageNumber
    last_page: PageNumber


class SelectionRequest(StrictModel):
    image_ids: Annotated[list[Id], Field(max_length=1000)] = Field(default_factory=list)
    document_ranges: Annotated[list[DocumentRange], Field(max_length=100)] = Field(default_factory=list)
    engines: Annotated[list[Id], Field(max_length=4)] = Field(default_factory=list)
    allow_processing: bool = False
    allow_export: bool = True
    allow_reprocess: bool = False
    allow_partial: bool = False
    allow_structure_checks: bool = False
    visual_model_id: Id | None = None
    allow_visual_images: bool = False
    allow_retry_failed: bool = False


def create_selection(agent, project_id, request, *, available_engines):
    request = SelectionRequest.model_validate(request)
    if not set(request.engines) <= set(available_engines):
        raise ValueError("所选引擎不在工作台能力清单")
    if request.visual_model_id and request.visual_model_id.startswith('external:') and not request.allow_visual_images:
        raise ValueError('外部审校需要明确允许向所选视觉连接发送原图、文字和元数据')
    images = list(dict.fromkeys(request.image_ids))
    with agent.business.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise KeyError("项目不存在")
        rows = []
        for key in images:
            row = db.execute("""SELECT i.id image_id,i.active_version version_id,p.id page_id,p.document_id,p.page_number,
                s.result_id,r.revision FROM images i JOIN pages p ON p.image_id=i.id
                LEFT JOIN selections s ON s.image_id=i.id LEFT JOIN results r ON r.id=s.result_id
                WHERE i.id=? AND i.project_id=?""", (key, project_id)).fetchone()
            if not row:
                raise ValueError("选择中包含其他项目或已删除页面")
            rows.append(dict(row))
        for selected in request.document_ranges:
            if selected.last_page < selected.first_page or selected.last_page - selected.first_page >= 1000:
                raise ValueError("页面范围必须按顺序且不超过 1000 页")
            found = db.execute("""SELECT p.image_id,i.active_version version_id,p.id page_id,p.document_id,p.page_number,
                s.result_id,r.revision FROM pages p JOIN documents d ON d.id=p.document_id
                LEFT JOIN images i ON i.id=p.image_id LEFT JOIN selections s ON s.image_id=i.id
                LEFT JOIN results r ON r.id=s.result_id WHERE d.project_id=? AND d.id=? AND p.page_number BETWEEN ? AND ? ORDER BY p.page_number""",
                (project_id, selected.document_id, selected.first_page, selected.last_page)).fetchall()
            if len(found) != selected.last_page - selected.first_page + 1:
                raise ValueError("文档范围包含其他项目或不存在的页面")
            rows.extend(dict(row) for row in found)
        rows = list({r["page_id"]: r for r in rows}.values())
        if len(rows) > 1000:
            raise ValueError("本次选择不能超过 1000 页")
        for row in rows:
            source = db.execute("SELECT d.sha256,p.render_parameters FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?", (row["page_id"],)).fetchone()
            row.update(document_sha256=source[0], render_parameters=source[1])
        snapshot = {"pages": rows, "permissions": request.model_dump(exclude={"image_ids", "document_ranges"}), "project_id": project_id}
        key = uid()
        db.execute("INSERT INTO agent_selections VALUES(?,?,?,?,?)", (key, project_id, canonical(snapshot), time.time() + 1800, now()))
    return {"selection_token": key, "selection": snapshot}


def read_selection(agent, project_id, token):
    with agent.business.transaction() as db:
        rows = db.execute("SELECT snapshot,expires FROM agent_selections WHERE id=? AND project_id=?", (token, project_id)).fetchone()
        if not rows or rows[1] <= time.time():
            raise Conflict("选择快照已过期，请重新发送当前选择")
        snapshot = json.loads(rows[0])
        validate_snapshot(db, project_id, snapshot)
        return snapshot


def validate_snapshot(db, project_id, snapshot, *, page_ids=None, allow_initial_render=False):
    for page in snapshot["pages"]:
        if page_ids is not None and page["page_id"] not in page_ids:
            continue
        current = db.execute("""SELECT i.active_version,v.parent_id,d.sha256,p.render_parameters,s.result_id,r.revision FROM pages p
            JOIN documents d ON d.id=p.document_id LEFT JOIN images i ON i.id=p.image_id LEFT JOIN versions v ON v.id=i.active_version
            LEFT JOIN selections s ON s.image_id=i.id LEFT JOIN results r ON r.id=s.result_id
            WHERE p.id=? AND d.project_id=?""", (page["page_id"], project_id)).fetchone()
        if not current or (page.get("document_sha256"), page.get("render_parameters")) != (current[2], current[3]):
            raise Conflict("已选文档或渲染参数发生变化，请刷新选择")
        if current[0] != page["version_id"] and not (allow_initial_render and page["version_id"] is None and current[1] is None):
            raise Conflict("已选页面版本发生变化，请刷新选择")
        if page['result_id'] is not None and (page['result_id'], page['revision']) != (current[4], current[5]):
            raise Conflict('已选采用结果或修订发生变化，请刷新选择')


def grant_selection(policy, run, selection, *, services=None):
    """Transform explicit UI permissions into exact tool scopes before scheduling."""
    expires = time.time() + 86400
    pages = selection["pages"]
    permissions = selection["permissions"]
    with policy.store.transaction() as db:
        validate_snapshot(db, run['project_id'], selection, allow_initial_render=True)
    def grant(permission, scope):
        if 'scope_revision' in run['context'] and permission != 'visual_images':
            scope = {**scope, 'scope_revision': run['context']['scope_revision']}
        policy.grant(run["project_id"], permission, scope, source="bound_ui_scope", expires=expires, run_id=run["id"])
    if permissions["allow_processing"] and pages:
        by_document = {}
        for page in pages:
            by_document.setdefault(page["document_id"], []).append(page)
        for doc, group in by_document.items():
            base = {"document_ids": [doc], "page_ids": sorted(p["page_id"] for p in group)}
            for force in ([False, True] if permissions["allow_reprocess"] else [False]):
                for mode in ("native", "auto", "ocr"):
                    if policy.review_only and mode != "native":
                        continue
                    if mode == "native" or mode == "auto" and "ppocr" in permissions["engines"]:
                        grant("scoped_processing", {**base, "mode": mode, "force": force})
                    if mode != "native":
                        for engine in permissions["engines"]:
                            grant("scoped_processing", {**base, "mode": mode, "force": force, "engine": engine})
        if not policy.review_only:
            materialized = [p for p in pages if p["version_id"]]
            grant("scoped_processing", {"document_ids": sorted(by_document), "page_ids": sorted(p["page_id"] for p in pages),
                  "version_ids": sorted(p["version_id"] for p in materialized), "engines": permissions["engines"]})
    if permissions["allow_export"]:
        references = [p for p in pages if p["result_id"] is not None]
        for format in ("txt", "md", "json", "xlsx", "pdf"):
            for partial in (["ask", "allow"] if permissions["allow_partial"] else ["ask"]):
                grant("scoped_new_artifact", {"source_run_id": run["id"], "format": format, "partial_policy": partial})
                if references:
                    grant("scoped_new_artifact", {"document_ids": sorted({p["document_id"] for p in references}), "page_ids": sorted(p["page_id"] for p in references),
                        "version_ids": sorted(p["version_id"] for p in references), "result_ids": sorted(p["result_id"] for p in references), "format": format, "partial_policy": partial})
    if permissions.get('allow_structure_checks') or permissions.get('visual_model_id'):
        from .visual import targets_for_result
        config = services.visual_parameters(permissions['visual_model_id'])[0] if permissions.get('visual_model_id') and services else None
        if permissions.get('visual_model_id') and config is None:
            raise ValueError('无法核对视觉模型能力')
        for page in pages:
            if not page['result_id']:
                continue
            base = {'document_ids': [page['document_id']], 'page_ids': [page['page_id']], 'result_ids': [page['result_id']],
                    'version_ids': [page['version_id']], 'revision': page['revision']}
            with policy.store.transaction() as db:
                edit = json.loads(db.execute('SELECT edited FROM results WHERE id=?', (page['result_id'],)).fetchone()[0])
                targets = targets_for_result(db, page['result_id'], edit=edit) if config else []
            if permissions.get('allow_structure_checks'):
                for i in range(len(edit['tables'])):
                    grant('scoped_check_job', {**base, 'table_id': f'table:{i}', 'checks': ['structure', 'totals', 'units', 'rounding', 'duplicates']})
            if config:
                grant('scoped_visual_review', {**base, 'visual_model_id': permissions['visual_model_id'], 'target_ids': [t['id'] for t in targets]})
                if config.get('backend') == 'external':
                    if not permissions.get('allow_visual_images'):
                        raise ValueError('缺少外发原图授权')
                    grant('visual_images', {'role': 'visual', 'endpoint': config['base_url'], 'config_revision': config['revision'],
                        'document_ids': [page['document_id']], 'page_ids': [page['page_id']], 'data_kinds': ['image', 'text', 'metadata']})
