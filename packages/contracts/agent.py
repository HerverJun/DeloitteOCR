"""Platform Agent contracts. Hidden execution context never appears in schemas."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Identity = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$')]
Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class AgentModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class AgentRef(AgentModel):
    kind: Literal['asset', 'asset_set', 'table', 'result', 'artifact', 'job', 'evidence']
    workspace_id: Identity
    object_id: Identity
    revision: int | str
    sha256: Hash | None = None
    application: Literal['platform', 'data', 'ocr', 'classification', 'forensics'] = 'platform'
    native_id: Identity | None = None
    parent_id: Identity | None = None
    sources: list['AgentRef'] = Field(default_factory=list, max_length=100)
    coverage: dict[str, int | str | bool] = Field(default_factory=dict)


class AgentError(AgentModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class AgentToolResult(AgentModel):
    status: Literal['success', 'pending', 'error', 'needs_user', 'needs_verification', 'unsupported']
    summary: str = Field(max_length=2000)
    data: dict = Field(default_factory=dict)
    result_refs: list[AgentRef] = Field(default_factory=list, max_length=100)
    job_refs: list[AgentRef] = Field(default_factory=list, max_length=100)
    artifact_refs: list[AgentRef] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=50)
    error: AgentError | None = None
    coverage: dict[str, int | str | bool] = Field(default_factory=dict)
    next_offset: int | None = Field(default=None, ge=0)


class AgentCall(AgentModel):
    call_id: str = Field(min_length=1, max_length=256, pattern=r'^[A-Za-z0-9_-]+$')
    tool: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    arguments: dict


class ControllerDraft(AgentModel):
    protocol: Literal['openai_chat_completions', 'anthropic_messages']
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default='', max_length=8192, repr=False)
    token_parameter: Literal['max_tokens', 'max_completion_tokens'] = 'max_completion_tokens'
    context_cap: int = Field(default=16384, ge=4096, le=200000)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    total_seconds: int = Field(default=180, ge=10, le=600)
    streaming: bool = True

    @model_validator(mode='after')
    def capacity(self):
        if self.max_output_tokens > self.context_cap // 2:
            raise ValueError('Output exceeds context budget')
        return self


class ControllerModelsDraft(ControllerDraft):
    model: str = Field(default='', max_length=256)


class AgentSessionCreate(AgentModel):
    name: str = Field(default='新任务', min_length=1, max_length=100)
    selected_assets: list[Identity] = Field(default_factory=list, max_length=100)
    initial_refs: list[AgentRef] = Field(default_factory=list, max_length=20)
    request_id: Identity


class AgentReferenceRequest(AgentModel):
    kind: Literal['artifact', 'table', 'result']
    object_id: Identity
    parent_id: Identity | None = None


class AgentMessage(AgentModel):
    content: str = Field(min_length=1, max_length=16000)
    request_id: Identity


class AgentReply(AgentModel):
    content: str = Field(min_length=1, max_length=4000)
    revision: int = Field(ge=1)
    request_id: Identity


class AgentCancel(AgentModel):
    cancel_owned_jobs: bool = False


class AgentTaskWrite(AgentModel):
    request_id: Identity
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str | None = Field(default=None, max_length=16000)
    selected_assets: list[Identity] | None = Field(default=None, max_length=100)
    initial_refs: list[AgentRef] | None = Field(default=None, max_length=100)


class AgentTaskControl(AgentModel):
    request_id: Identity
    expected_revision: int = Field(ge=1)
    action: Literal['take_over', 'return_to_agent']


class AgentManualReturn(AgentModel):
    request_id: Identity
    expected_revision: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=4000)
    references: list[AgentRef] = Field(default_factory=list, max_length=50)
    include_asset_ids: list[Identity] = Field(default_factory=list, max_length=100)


class AgentNativeAttach(AgentModel):
    request_id: Identity
    application: Literal['ocr', 'classification']
    link_id: Identity
    object_id: Identity


class AuditProjectProfile(AgentModel):
    request_id: Identity
    expected_revision: int = Field(ge=0)
    audit_object: str = Field(default='', max_length=200)
    period_start: str = Field(default='', max_length=10)
    period_end: str = Field(default='', max_length=10)
    objective: str = Field(default='', max_length=4000)
    rules: str = Field(default='', max_length=8000)

    @model_validator(mode='after')
    def period(self):
        from datetime import date
        start = date.fromisoformat(self.period_start) if self.period_start else None
        end = date.fromisoformat(self.period_end) if self.period_end else None
        if start and end and start > end:
            raise ValueError('期间结束不能早于开始')
        return self


class AgentSkillActivate(AgentModel):
    name: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    request_id: str = Field(min_length=1, max_length=128)
    version: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class AgentSkillDeactivate(AgentModel):
    name: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    request_id: str = Field(min_length=1, max_length=128)


class AgentPublicActivity(AgentModel):
    version: Literal[1] = 1
    generation: int = Field(ge=1)
    phase: Literal['preparing_request', 'model_wait', 'receiving_response', 'validating_calls',
                   'tool_dispatch', 'waiting_job', 'waiting_user', 'needs_verification',
                   'finalizing', 'completed', 'stopped', 'failed']
    request_id: str | None = None
    display_id: str | None = None
    tool: str | None = None
    call_id: str | None = None
    status: str | None = None
    reason: str | None = None
    job_refs: list[AgentRef] = Field(default_factory=list)


class AgentResponsePreview(AgentModel):
    version: Literal[1] = 1
    generation: int = Field(ge=1)
    request_id: str
    display_id: str
    revision: int = Field(ge=1)
    mode: Literal['snapshot', 'append', 'replace']
    offset: int = Field(ge=0)
    content: str
    source: Literal['text', 'answer', 'delivery']
    provisional: Literal[True] = True
    truncated: bool = False
    call_id: str | None = None
    tool: str | None = None


class AgentResponseState(AgentModel):
    version: Literal[1] = 1
    generation: int = Field(ge=1)
    request_id: str
    display_id: str
    revision: int = Field(ge=1)
    status: Literal['generated', 'confirmed', 'interrupted']
    source: Literal['text', 'answer', 'delivery']
    content: str
    references: list[AgentRef] = Field(default_factory=list)
    reason: str = ''


class AgentToolActivity(AgentModel):
    version: Literal[1] = 1
    generation: int = Field(ge=1)
    call_id: str
    operation_id: str | None = None
    tool: str
    phase: Literal['started', 'restored', 'pending', 'finished', 'blocked']
    status: str
    summary: str
    job_refs: list[AgentRef] = Field(default_factory=list)


class AgentJobProgress(AgentModel):
    needs_resume: bool = False
    resume_message: str = Field(default='', max_length=256)
    version: Literal[1] = 1
    generation: int = Field(ge=1)
    job_ref: AgentRef
    status: str
    stage: str | None = None
    application: Literal['platform', 'data', 'ocr', 'classification', 'forensics'] | None = None
    job_id: str | None = None
    native_kind: str | None = None
    counts: dict[str, int | None] = Field(default_factory=dict)
    coverage: dict[str, int | None] = Field(default_factory=dict)
    capabilities: list[Literal['pause', 'resume', 'cancel', 'retry_failed']] = Field(default_factory=list)
    observation: Literal['native', 'unavailable'] = 'native'
    availability: str | None = None
    progress_done: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    has_error: bool = False
    hard_cancel: bool | None = None
    execution_complete: bool | None = None
    review_complete: bool | None = None
    index_complete: bool | None = None
    sampled_at: str


AGENT_MODELS = [AgentRef, AgentToolResult, AgentCall, ControllerDraft,
                ControllerModelsDraft, AgentSessionCreate, AgentReferenceRequest, AgentMessage, AgentReply, AgentCancel,
                AgentTaskWrite, AgentTaskControl, AgentManualReturn, AgentNativeAttach, AuditProjectProfile,
                AgentSkillActivate, AgentSkillDeactivate, AgentPublicActivity,
                AgentResponsePreview, AgentResponseState, AgentToolActivity, AgentJobProgress]
