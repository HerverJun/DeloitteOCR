"""Portal access to registered Skills and their fixed, session-scoped resources."""
from fastapi import APIRouter, HTTPException, Query, Request
from packages.contracts.agent import AgentSkillActivate as Activate, AgentSkillDeactivate as Deactivate
from workbench_platform.assets.routes import library
from .routes import runtime, session_scope, parsed
from .store import Conflict
from .preferences import Business, PreferenceWrite

router = APIRouter(prefix='/api/platform/v1', tags=['platform-agent-skills'])


@router.get('/workspaces/{workspace_id}/agent/preferences')
def preferences(workspace_id: str, request: Request, business: Business = 'project'):
    agent = runtime(request)
    library(request, workspace_id)
    return agent.preferences.preview(workspace_id, business)


@router.post('/workspaces/{workspace_id}/agent/preferences')
async def preferences_update(workspace_id: str, request: Request):
    agent = runtime(request, True)
    library(request, workspace_id, write=True)
    payload = await parsed(request, PreferenceWrite)
    try:
        return agent.preferences.update(workspace_id, payload.business, payload.expected_revision,
            payload.values, payload.revoke_keys, payload.source, payload.request_id)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.get('/agent/sessions/{session_id}/preferences/snapshots')
def preference_snapshots(session_id: str, request: Request):
    agent = runtime(request)
    session = session_scope(agent, request, session_id)
    try:
        return agent.preferences.snapshots(session['workspace_id'], session_id)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None


@router.get('/agent/sessions/{session_id}/skills/match')
def match(session_id: str, request: Request, goal: str = Query(..., min_length=1, max_length=12000)):
    agent = runtime(request)
    session = session_scope(agent, request, session_id)
    return agent.skills.match(session['workspace_id'], session_id, goal)


@router.get('/agent/skills')
def catalog(request: Request, workspace_id: str | None = None, session_id: str | None = None,
            keyword: str = Query('', max_length=100), offset: int = Query(0, ge=0),
            limit: int = Query(20, ge=1, le=50)):
    agent = runtime(request)
    if session_id:
        session = session_scope(agent, request, session_id)
        if workspace_id is not None and session['workspace_id'] != workspace_id:
            raise HTTPException(403, '会话不属于当前项目')
        workspace_id = session['workspace_id']
    if workspace_id is not None:
        library(request, workspace_id)
    try:
        # None explicitly limits this read-only view to registered platform
        # sources; workspace roots and activation still need a real task.
        return agent.skills.catalog(workspace_id, session_id, keyword, offset, limit)
    except (ValueError, Conflict):
        raise HTTPException(400, '技能登记目录或固定版本不可用') from None


@router.post('/agent/sessions/{session_id}/skills/activate')
async def activate(session_id: str, request: Request):
    agent = runtime(request, True)
    session = session_scope(agent, request, session_id, True)
    payload = await parsed(request, Activate)
    try:
        return agent.skills.activate(session['workspace_id'], session_id, payload.name, payload.request_id, payload.version)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post('/agent/sessions/{session_id}/skills/deactivate')
async def deactivate(session_id: str, request: Request):
    agent = runtime(request, True)
    session = session_scope(agent, request, session_id, True)
    payload = await parsed(request, Deactivate)
    try:
        return agent.skills.deactivate(session['workspace_id'], session_id, payload.name, payload.request_id)
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.get('/agent/sessions/{session_id}/skills/{name}/resources')
def resource(session_id: str, name: str, request: Request, path: str = Query('SKILL.md', max_length=200),
             offset: int = Query(0, ge=0), limit: int = Query(4000, ge=1, le=16000)):
    agent = runtime(request)
    session = session_scope(agent, request, session_id)
    try:
        return agent.skills.read_resource(session['workspace_id'], session_id, name, path, offset, limit)
    except Conflict:
        raise HTTPException(409, '技能固定版本校验失败，不能替换为当前源文件') from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
