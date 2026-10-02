"""Versioned batch automation contracts; execution never implies human approval."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .models import Id, Hash, Revision, Wire

Scenario = Literal['data_compare', 'classification', 'ocr']
ExecutionState = Literal['queued', 'running', 'waiting_input', 'waiting_resource', 'partial',
                         'succeeded', 'failed', 'cancelling', 'cancelled', 'needs_verification']


class BatchTemplateCreate(Wire):
    workspace_id: Id
    name: str = Field(min_length=1, max_length=100)
    scenario: Scenario
    config: dict[str, Any]
    template_id: Id | None = None


class BatchTemplate(Wire):
    workspace_id: Id
    template_id: Id
    version: Revision
    name: str
    scenario: Scenario
    config: dict[str, Any]
    config_hash: Hash
    created_at: str


class BatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    role: Literal['left', 'right', 'images', 'documents']
    asset_id: Id
    asset_revision: Revision
    blob_hash: Hash


class BatchRequest(Wire):
    request_key: Id
    workspace_id: Id
    template_id: Id
    template_version: Revision
    inputs: list[BatchInput] = Field(min_length=1, max_length=1000)


class BatchControl(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['cancel', 'retry']


class BatchDisposition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    item_ids: list[Id] = Field(min_length=1, max_length=1000)
    action: Literal['confirm', 'defer', 'reopen']
    note: str = Field(default='', max_length=2000)


class ProcessingBatch(Wire):
    batch_id: Id
    request_key: Id
    workspace_id: Id
    template_id: Id
    template_version: Revision
    template: BatchTemplate
    inputs: list[BatchInput]
    status: ExecutionState
    stage: str
    summary: dict[str, Any]
    issues: list[dict[str, Any]]
    receipts: dict[str, Any]
    artifact_state: Literal['not_generated', 'generated', 'collected', 'collection_failed']
    artifacts: list[dict[str, Any]]
    capabilities: list[Literal['cancel', 'retry']]
    cancel_requested: bool
    created_at: str
    updated_at: str


AUTOMATION_MODELS = [BatchTemplateCreate, BatchTemplate, BatchInput, BatchRequest,
                     BatchControl, BatchDisposition, ProcessingBatch]
