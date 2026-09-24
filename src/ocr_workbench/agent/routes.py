"""Authenticated application API. No client thread/checkpoint/Command surface."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from .contracts import CreateSession, SendMessage, UpdateSession, CancelRun, ResumeRun, ReplyDecision
from .connection import ControllerConnection, probe
from .events import stream_events
from .store import AgentStore, digest
from .selection import SelectionRequest, create_selection, read_selection, grant_selection
from .budgets import IncreaseBudget, increase
from ocr_workbench.store import Conflict, now


def public_session(session):
    return {k: session[k] for k in ("id", "project_id", "title", "status", "created", "updated")}


def public_run(run):
    return {k: run[k] for k in ("id", "session_id", "project_id", "goal", "status", "outcome", "generation", "limits", "usage", "coverage", "last_event_seq", "created", "updated")}


def register_agent_routes(app, services, *, enabled, policy):
    business = services.store
    agent = AgentStore(business)
    connection = ControllerConnection(business)
    app.state.agent_connection = connection
    app.state.agent_runtime = None
    app.state.agent_startup_error = None

    def runtime():
        manager = app.state.agent_runtime
        if not enabled or manager is None or manager.closed:
            raise HTTPException(503, "助手未启用或运行依赖不可用，普通 OCR 可继续使用")
        return manager

    def session(key):
        rows = business.rows("SELECT * FROM agent_sessions WHERE id=?", (key,))
        if not rows:
            raise KeyError("助手会话不存在")
        return rows[0]

    def run(key):
        rows = business.rows("SELECT s.project_id FROM agent_runs r JOIN agent_sessions s ON s.id=r.session_id WHERE r.id=?", (key,))
        if not rows:
            raise KeyError("助手运行不存在")
        return agent.run(rows[0]["project_id"], key)

    @app.get("/api/agent/status")
    def status():
        manager = app.state.agent_runtime
        return {"enabled": enabled, "available": manager is not None and not manager.closed,
                "reason": app.state.agent_startup_error, 'observer_error': manager.observer_error if manager else None,
                "capabilities": manager.capabilities() if manager else {}}

    @app.get("/api/agent/connection")
    def connection_view():
        return connection.view()

    @app.put("/api/agent/connection")
    async def connection_save(body: dict):
        if not enabled:
            raise HTTPException(503, "助手未启用")
        return await connection.save(body)

    @app.delete("/api/agent/connection")
    def connection_clear():
        return connection.clear()

    @app.post("/api/agent/connection/probe")
    async def connection_probe(body: dict):
        if not enabled:
            raise HTTPException(503, "助手未启用")
        config, key, _ = connection.draft(body)
        return await probe(config, key)

    @app.post("/api/agent/connection/models")
    async def connection_models(body: dict):
        if not enabled:
            raise HTTPException(503, "助手未启用")
        async with asyncio.timeout(20):
            return await connection.models(body)

    @app.get("/api/projects/{project_id}/agent/sessions")
    def sessions(project_id: str, offset: int = 0, limit: int = 30):
        business.one("projects", project_id)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("会话分页参数无效")
        return {"sessions": [public_session(s) for s in business.rows("SELECT * FROM agent_sessions WHERE project_id=? ORDER BY updated DESC,id LIMIT ? OFFSET ?", (project_id, limit, offset))]}

    @app.post("/api/projects/{project_id}/agent/sessions")
    def create_session(project_id: str, body: CreateSession):
        runtime()
        return public_session(agent.create_session(project_id, body.model_dump()))

    @app.get("/api/agent/sessions/{session_id}")
    def session_snapshot(session_id: str):
        current = session(session_id)
        runs = business.rows("SELECT id FROM agent_runs WHERE session_id=? ORDER BY created DESC LIMIT 30", (session_id,))
        return {"session": public_session(current), "runs": [public_run(agent.run(current["project_id"], row["id"])) for row in runs],
                'inbox': business.rows("""SELECT i.id,i.run_id,i.state,i.created,json_extract(i.context,'$.scope_error') scope_error FROM agent_inbox i
                    JOIN agent_runs r ON r.id=i.run_id WHERE r.session_id=? ORDER BY i.id DESC LIMIT 50""", (session_id,)),
                "last_seq": current["next_seq"] - 1}

    @app.patch("/api/agent/sessions/{session_id}")
    def update_session(session_id: str, body: UpdateSession):
        current = session(session_id)
        return public_session(agent.update_session(current['project_id'], session_id, body))

    @app.put("/api/projects/{project_id}/agent/controller-authorization")
    def authorize_controller(project_id: str, body: dict):
        manager = runtime()
        if set(body) != {"revision", "allow"} or body["allow"] is not True or type(body["revision"]) is not int:
            raise ValueError("请明确确认当前主控接收地址与项目文字范围")
        config, _ = connection.resolve(body["revision"])
        business.one("projects", project_id)
        documents = business.rows("SELECT id FROM documents WHERE project_id=?", (project_id,))
        pages = business.rows("SELECT p.id FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.project_id=?", (project_id,))
        scope = {"role": "controller", "endpoint": config["base_url"], "config_revision": config["revision"],
                 "document_ids": sorted(d["id"] for d in documents), "page_ids": sorted(p["id"] for p in pages), "data_kinds": ["metadata", "text"]}
        grant_id = manager.policy.grant(project_id, "controller_content", scope, source="bound_ui_scope", expires=time.time() + 30 * 86400)
        return {"grant_id": grant_id, "scope": scope}

    @app.get('/api/projects/{project_id}/agent/controller-authorization')
    def controller_authorization(project_id: str):
        from .policy import AgentPolicy, PolicyDenied
        business.one('projects', project_id)
        config = connection.view()
        if not config.get('available'):
            return {'authorized': False, 'revision': config['revision']}
        documents = business.rows('SELECT id FROM documents WHERE project_id=?', (project_id,))
        pages = business.rows('SELECT p.id FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.project_id=?', (project_id,))
        try:
            AgentPolicy(agent).authorize_outbound({'project_id': project_id, 'id': 'new'}, role='controller', endpoint=config['base_url'],
                config_revision=config['revision'], document_ids=[d['id'] for d in documents], page_ids=[p['id'] for p in pages], data_kinds=['metadata', 'text'])
        except PolicyDenied:
            return {'authorized': False, 'revision': config['revision']}
        return {'authorized': True, 'revision': config['revision']}

    @app.post("/api/agent/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: SendMessage):
        manager = runtime()
        current = session(session_id)
        old = business.rows("SELECT id,request_hash FROM agent_runs WHERE session_id=? AND client_request_id=?", (session_id, body.client_request_id))
        if old:
            if old[0]["request_hash"] != digest(body.model_dump(exclude={"client_request_id"})):
                raise Conflict("相同请求编号对应不同内容")
            accepted = agent.run(current["project_id"], old[0]["id"])
            if accepted['status'] == 'queued' and accepted['owner'] is None:
                connection.assert_current(accepted['config']['revision'])
                if accepted['context'].get('selection'):
                    grant_selection(manager.policy, accepted, accepted['context']['selection'], services=services)
                manager.schedule(accepted)
            return {"run": public_run(agent.run(current["project_id"], old[0]["id"]))}
        previous_inbox = manager.inbox.existing(session_id, body)
        if previous_inbox:
            return {'run': public_run(agent.run(current['project_id'], previous_inbox['run_id'])), 'inbox_id': previous_inbox['id']}
        selection = read_selection(agent, current["project_id"], body.selection_token) if body.selection_token else None
        active = business.rows("SELECT id FROM agent_runs WHERE session_id=? AND status NOT IN ('completed','failed','cancelled')", (session_id,))
        if active:
            item = manager.inbox.enqueue(current['project_id'], session_id, body, selection)
            return {'run': public_run(agent.run(current['project_id'], item['run_id'])), 'inbox_id': item['id']}
        view = connection.view()
        if not view.get("available"):
            raise ValueError("请先配置并测试独立主控连接")
        config, _ = connection.resolve(view["revision"])
        documents = business.rows("SELECT id FROM documents WHERE project_id=?", (current["project_id"],))
        pages = business.rows("SELECT p.id FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.project_id=?", (current["project_id"],))
        context = {"document_ids": sorted(d["id"] for d in documents), "page_ids": sorted(p["id"] for p in pages)}
        if selection:
            context.update(selection_token=body.selection_token, selection=selection)
            if selection['permissions'].get('visual_model_id'):
                from .visual import targets_for_result
                services.visual_parameters(selection['permissions']['visual_model_id'])
                with business.transaction() as db:
                    for selected_page in selection['pages']:
                        if selected_page['result_id']:
                            targets_for_result(db, selected_page['result_id'])
        manager.policy.authorize_outbound({"project_id": current["project_id"], "id": "new"}, role="controller", endpoint=config["base_url"], config_revision=config["revision"],
                                           document_ids=context["document_ids"], page_ids=context["page_ids"], data_kinds=["metadata", "text"])
        created = agent.create_run(current["project_id"], session_id, body.model_dump(), context=context, config=config, limits=policy["limits"])
        try:
            if selection:
                grant_selection(manager.policy, created, selection, services=services)
            if created["status"] == "queued":
                manager.schedule(created)
        except Exception:
            if agent.run(current['project_id'], created['id'])['status'] == 'queued':
                agent.transition(current['project_id'], created['id'], created['generation'], 'cancelled', event_key=created['id'] + ':submission-failed')
            raise
        return {"run": public_run(created)}

    @app.post("/api/projects/{project_id}/agent/selection")
    def selection_snapshot(project_id: str, body: SelectionRequest):
        runtime()
        return create_selection(agent, project_id, body.model_dump(), available_engines=services.registry.engines())

    @app.get("/api/agent/sessions/{session_id}/events")
    def events(session_id: str, after_seq: int = 0, limit: int = 100):
        current = session(session_id)
        rows = agent.events(current["project_id"], session_id, after_seq, limit)
        return {"events": rows, "next_seq": rows[-1]["seq"] if rows else after_seq}

    @app.get("/api/agent/sessions/{session_id}/stream")
    async def stream(session_id: str, request: Request, after_seq: int = 0):
        current = session(session_id)
        if after_seq < 0:
            raise ValueError("事件游标无效")
        return StreamingResponse(stream_events(request, agent, current["project_id"], session_id, after_seq), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/agent/runs/{run_id}")
    def run_snapshot(run_id: str):
        return public_run(run(run_id))

    @app.post("/api/agent/runs/{run_id}/budget")
    def increase_budget(run_id: str, body: IncreaseBudget):
        current = run(run_id)
        return public_run(increase(agent, current["project_id"], run_id, body))

    @app.get("/api/agent/evidence/{reference_id}")
    def resolve_evidence(reference_id: str, project_id: str):
        from .policy import AgentPolicy
        with business.transaction() as db:
            row = db.execute("""SELECT e.reference FROM agent_evidence e JOIN agent_runs r ON r.id=e.run_id
                JOIN agent_sessions s ON s.id=r.session_id WHERE e.id=? AND s.project_id=?""", (reference_id, project_id)).fetchone()
            if not row:
                raise KeyError("证据不属于当前项目")
            ref = json.loads(row[0])
            resource = AgentPolicy._owned(db, project_id, "result", ref["result_id"])
            if (resource["revision"], resource["version_id"]) != (ref["revision"], ref["version_id"]):
                raise Conflict("证据结果已变化，请重新查询")
            page = db.execute("SELECT image_id FROM pages WHERE id=?", (ref["page_id"],)).fetchone()
            return {"reference": ref, "image_id": page[0]}

    @app.post("/api/agent/runs/{run_id}/cancel")
    async def cancel(run_id: str, body: CancelRun):
        current = run(run_id)
        return public_run(await runtime().cancel(current["project_id"], run_id, body.generation, body.mode, request_id=body.client_request_id))

    @app.post("/api/agent/runs/{run_id}/resume")
    async def resume(run_id: str, body: ResumeRun):
        current = run(run_id)
        await runtime().resume_interrupted(current["project_id"], run_id, body.generation, request_id=body.client_request_id)
        return public_run(run(run_id))

    @app.post("/api/agent/decisions/{decision_id}/reply")
    async def decision_reply(decision_id: str, body: ReplyDecision):
        rows = business.rows("SELECT run_id FROM agent_decisions WHERE id=?", (decision_id,))
        if not rows:
            raise KeyError("问题不存在")
        current = run(rows[0]["run_id"])
        reply = agent.reply_decision(current["project_id"], current["id"], decision_id, body.model_dump())
        if current["status"] == "waiting_user":
            runtime().schedule(current, resume=True)
        return {"decision_id": decision_id, "status": reply["status"]}

    @app.get("/api/projects/{project_id}/agent/artifacts/{artifact_id}")
    def artifact_snapshot(project_id: str, artifact_id: str):
        record = runtime().artifacts.get(project_id, artifact_id)
        return {k: json.loads(v) if k == "manifest" else v for k, v in record.items() if k != "relative_path"}

    @app.get("/api/projects/{project_id}/agent/artifacts/{artifact_id}/download")
    def artifact_download(project_id: str, artifact_id: str):
        from .downloads import LeasedFileResponse
        artifacts = runtime().artifacts
        lease, target = artifacts.lease(project_id, artifact_id)
        return LeasedFileResponse(artifacts, lease, target)

    @app.get("/api/projects/{project_id}/agent/artifacts/{artifact_id}/coverage-manifest")
    def artifact_coverage_manifest(project_id: str, artifact_id: str):
        from .downloads import LeasedFileResponse

        artifacts = runtime().artifacts
        lease, target = artifacts.coverage_manifest(project_id, artifact_id)
        return LeasedFileResponse(artifacts, lease, target, filename="export-partial-manifest.json")

    @app.patch("/api/projects/{project_id}/agent/artifacts/{artifact_id}")
    def artifact_pin(project_id: str, artifact_id: str, body: dict):
        if set(body) != {"pinned"} or type(body["pinned"]) is not bool:
            raise ValueError("产物保留参数无效")
        runtime().artifacts.pin(project_id, artifact_id, body['pinned'])
        return artifact_snapshot(project_id, artifact_id)

    @app.delete("/api/projects/{project_id}/agent/artifacts/{artifact_id}")
    def artifact_delete(project_id: str, artifact_id: str):
        return {"deleted": runtime().artifacts.delete(project_id, artifact_id)}

    return connection
