"""Version 2 task-history contracts. Credentials and actor IDs are never accepted."""
from typing import Any, Literal
import json
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskState = Literal['queued','waiting_resource','running','waiting_user','pausing','paused','cancelling','cancelled','succeeded','partial','failed','needs_verification']
TaskTool = Literal['ocr','visual_review','classification','data','forensics','face','audio','seal','catnet','document_forensics','batch','agent','assets','artifact']

class TaskWire(BaseModel):
    model_config = ConfigDict(extra='forbid')

class TaskCreate(TaskWire):
    workspace_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
    tool_id: TaskTool
    action: str = Field(min_length=1,max_length=100)
    name: str | None = Field(default=None,min_length=1,max_length=200)
    description: str = Field(default='',max_length=10000)
    idempotency_key: str = Field(min_length=1,max_length=200)
    inputs: list[dict[str,Any]] = Field(default_factory=list,max_length=10000)
    options: dict[str,Any] = Field(default_factory=dict)
    ai_service_profile_id: str | None = None

    @model_validator(mode='after')
    def valid_inputs(self):
        json.dumps(self.options,allow_nan=False)
        for item in self.inputs:
            if not isinstance(item.get('asset_id'),str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,99}',item['asset_id']):raise ValueError('invalid input asset identity')
            if type(item.get('asset_revision')) is not int or item['asset_revision']<1:raise ValueError('invalid input revision')
            if not isinstance(item.get('blob_hash'),str) or not re.fullmatch(r'[0-9a-f]{64}',item['blob_hash']):raise ValueError('invalid input hash')
            if item.get('source_locator'):
                from .models import SourceLocator
                locator=SourceLocator.model_validate(item['source_locator'])
                if (locator.asset_id,locator.asset_revision,locator.blob_hash)!=(item['asset_id'],item['asset_revision'],item['blob_hash']):raise ValueError('input locator identity mismatch')
        return self

class TaskSummary(TaskWire):
    id: str
    task_code: str
    workspace_id: str | None
    project_name_snapshot: str | None
    created_by: str | None
    creator_name_snapshot: str | None
    name: str
    description: str
    tool_id: str
    tool_version: str | None
    action: str
    source: str
    parent_task_id: str | None
    parent_run_id: str | None
    status: TaskState
    stage: str
    availability: str
    native_status: str | None
    current_run_id: str | None
    run_count: int
    revision: int
    input_total_bytes: int | None
    file_count: int | None
    page_count: int | None
    progress_done: int | None
    progress_total: int | None
    created_at: str | None
    updated_at: str | None
    last_verified_at: str | None
    elapsed_ms: int | None
    archived: bool
    summary: dict[str,Any]
    capabilities: list[str]

class TaskRun(TaskWire):
    id: str
    task_id: str
    attempt: int
    generation: int
    status: TaskState
    queued_at: str | None
    started_at: str | None
    finished_at: str | None
    elapsed_ms: int | None
    queue_ms: int | None
    error: str | None
    requested_by: str | None
    request_snapshot: dict[str,Any]
    scope: dict[str,Any]

class TaskStepRun(TaskWire):
    id: str
    task_id: str
    run_id: str
    input_id: str | None
    step_key: str
    attempt: int
    tool_id: str
    action: str
    status: str
    native_id: str | None
    native_generation: int | None
    worker_id: str | None
    reused_from_step_run_id: str | None
    started_at: str | None
    finished_at: str | None
    elapsed_ms: int | None
    error: str | None
    details: dict[str,Any]

class TaskEvent(TaskWire):
    seq: int
    task_id: str
    run_id: str | None
    identity: str
    kind: str
    actor_id: str | None
    payload: dict[str,Any]
    created_at: str

class TaskDetail(TaskWire):
    task: TaskSummary
    inputs: list[dict[str,Any]]
    runs: list[TaskRun]
    steps: list[TaskStepRun]
    ai_calls: list[dict[str,Any]]
    artifacts: list[dict[str,Any]]
    children: list[dict[str,Any]]
    collection_cursors: dict[str,str|None] = Field(default_factory=dict)

class TaskPage(TaskWire):
    items: list[TaskSummary]
    next_cursor: str | None

class TaskControl(TaskWire):
    operation: Literal['cancel','pause','resume','retry','verify','archive','unarchive']
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1,max_length=200)
    scope: dict[str,Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_scope(self):
        json.dumps(self.scope,allow_nan=False)
        return self

class TaskEdit(TaskWire):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None,min_length=1,max_length=200)
    description: str | None = Field(default=None,max_length=10000)

TASK_MODELS=[TaskCreate,TaskSummary,TaskRun,TaskStepRun,TaskEvent,TaskDetail,TaskPage,TaskControl,TaskEdit]

def task_paths():
    result={}
    operations={
        '/api/platform/v2/tasks': [('get','TaskPage'),('post','TaskSummary')],
        '/api/platform/v2/tasks/{task_id}':[('get','TaskDetail'),('patch','TaskSummary')],
        '/api/platform/v2/tasks/{task_id}/controls':[('post',None)],
        '/api/platform/v2/tasks/{task_id}/events':[('get',None)],
        '/api/platform/v2/tasks/{task_id}/collections/{name}':[('get',None)],
    }
    bodies={'postTaskSummary':'TaskCreate','patchTaskSummary':'TaskEdit','postNone':'TaskControl'}
    for path,methods in operations.items():
        result[path]={}
        for method,model in methods:
            response={'description':'Successful task ledger response'}
            if model: response['content']={'application/json':{'schema':{'$ref':'#/components/schemas/'+model}}}
            code='202' if method=='post' else '200'
            op={'tags':['task-history'],'responses':{code:response}}
            if '{task_id}' in path: op['parameters']=[{'name':'task_id','in':'path','required':True,'schema':{'type':'string'}}]
            if '{name}' in path:op.setdefault('parameters',[]).append({'name':'name','in':'path','required':True,'schema':{'type':'string','enum':['inputs','runs','steps','ai_calls','artifacts','children']}})
            body=bodies.get(method+str(model))
            if body: op['requestBody']={'required':True,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/'+body}}}}
            result[path][method]=op
    return result
