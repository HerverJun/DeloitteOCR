"""Versioned batch automation contracts; execution never implies human approval."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import Id, NativeId, Hash, Revision, Count, Bytes, Wire

# The common transport envelope remains schema_version=1.0. Automation v2
# requires conditional, identified writes; v1 disposition writes are rejected.
AUTOMATION_VERSION = '2.0'

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


class LocalActor(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: Id
    display_name: str = Field(min_length=1, max_length=100)
    source: Literal['local']


class NativeAdoption(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    native_revision: int = Field(ge=0)
    native_result_id: NativeId | None = None
    native_version_id: NativeId | None = None
    human_label_id: NativeId | None = None


class BatchDisposition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    automation_version: Literal['2.0'] = AUTOMATION_VERSION
    request_id: Id
    expected_revision: Revision
    actor: LocalActor
    item_ids: list[Id] = Field(min_length=1, max_length=1000)
    action: Literal['confirm', 'defer', 'reopen', 'adopt']
    note: str = Field(default='', max_length=2000)
    adoption: NativeAdoption | None = None

    @model_validator(mode='after')
    def explicit_adoption(self):
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError('duplicate item identity')
        if self.action == 'adopt':
            if self.adoption is None or len(self.item_ids) != 1:
                raise ValueError('adoption requires one item and an explicit native revision')
        elif self.adoption is not None:
            raise ValueError('adoption is only valid for adopt')
        return self


class BatchCoverage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    input_files: Count = 0
    left_rows: Count = 0
    right_rows: Count = 0
    result_rows: Count = 0
    required_items: Count = 0
    indexed_items: Count = 0
    scanned_results: Count = 0
    next_ordinal: Count = 0
    index_complete: bool = False
    pending: Count = 0
    confirmed: Count = 0
    deferred: Count = 0

    @model_validator(mode='after')
    def conserved_index(self):
        if self.scanned_results > self.result_rows or self.indexed_items > self.required_items:
            raise ValueError('index exceeds fixed result coverage')
        if self.pending + self.confirmed + self.deferred != self.indexed_items:
            raise ValueError('disposition counts must conserve indexed items')
        if self.index_complete and (self.scanned_results != self.result_rows or
                                    self.indexed_items != self.required_items):
            raise ValueError('complete index must cover every result and required item')
        return self


class BatchDelivery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: Revision | None = None
    bundle_id: Id | None = None
    status: Literal['none', 'current', 'outdated'] = 'none'
    kind: Literal['intermediate', 'final'] | None = None


class BatchOperation(Wire):
    operation_id: Id
    batch_id: Id
    request_id: Id
    payload_hash: Hash
    expected_revision: Revision
    actor: LocalActor
    state: Literal['intent', 'native_committed', 'committed', 'needs_verification']
    receipt: dict[str, Any]
    created_at: str
    updated_at: str


class ReviewArtifactRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: Id
    expected_revision: Revision
    actor: LocalActor
    kind: Literal['intermediate', 'final']


class ReviewBundleFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1)
    artifact_id: Id
    revision: Revision
    media_type: str
    size_bytes: Bytes
    availability: Literal['available', 'expired', 'missing', 'corrupt']
    blob_hash: Hash


class ReviewUnresolvedItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    item_id: Id
    category: str
    disposition: Literal['pending', 'deferred']
    title: str


class ReviewBundle(BaseModel):
    """Ready immutable materials plus bounded, current read-time metadata."""
    model_config = ConfigDict(extra='forbid', strict=True)
    bundle_id: Id
    workspace_id: Id
    kind: Literal['batch_review']
    batch_id: Id
    batch_revision: Revision
    delivery_kind: Literal['intermediate', 'final']
    predecessor_bundle_id: Id | None
    successor_bundle_ids: list[Id] = Field(max_length=100)
    successor_count: Count
    status: Literal['current', 'outdated']
    request_id: Id
    payload_hash: Hash
    snapshot_hash: Hash
    title: str
    created_at: str
    files: list[ReviewBundleFile]
    inputs: list[BatchInput] = Field(max_length=1000)
    coverage: BatchCoverage
    unresolved_count: Count
    unresolved_preview: list[ReviewUnresolvedItem] = Field(max_length=30)
    execution_status: ExecutionState
    pending_operations: Count

    @model_validator(mode='after')
    def consistent_ready_materials(self):
        if (len(self.successor_bundle_ids) > self.successor_count or
                len(set(self.successor_bundle_ids)) != len(self.successor_bundle_ids)):
            raise ValueError('successor preview must contain distinct registered bundles')
        if (len(self.unresolved_preview) > self.unresolved_count or
                self.unresolved_count < self.coverage.pending + self.coverage.deferred):
            raise ValueError('unresolved summary must include indexed and technical dispositions')
        if self.delivery_kind == 'final' and (
                self.execution_status != 'succeeded' or not self.coverage.index_complete or
                self.unresolved_count or self.pending_operations):
            raise ValueError('final materials require complete resolved execution')
        return self


class BatchIssuePage(Wire):
    batch_id: Id
    revision: Revision
    items: list[dict[str, Any]]
    total: Count
    offset: Count
    summary: dict[str, Any]
    coverage: BatchCoverage


class BatchItemRead(Wire):
    workspace_id: Id
    batch_id: Id
    revision: Revision
    item: dict[str, Any]


class BatchHistoryEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    event_id: Id
    batch_id: Id
    item_id: Id
    operation_id: Id | None
    revision: Revision | None
    action: Literal['confirm', 'defer', 'reopen', 'adopt']
    note: str
    actor: LocalActor | None
    previous_state: dict[str, Any]
    current_state: dict[str, Any]
    adoption: dict[str, Any] | None
    legacy: bool
    created_at: str

    @model_validator(mode='after')
    def preserve_history_identity(self):
        if not self.legacy and (self.operation_id is None or self.revision is None or self.actor is None):
            raise ValueError('identified history requires its operation, revision and actor')
        return self


class BatchHistoryPage(Wire):
    batch_id: Id
    revision: Revision
    items: list[BatchHistoryEvent] = Field(max_length=200)
    total: Count
    offset: Count


class BatchNativeRevisionPage(Wire):
    batch_id: Id
    revision: Revision
    item_id: Id
    scenario: Literal['classification', 'ocr']
    original: dict[str, Any]
    source: dict[str, Any]
    adoption: dict[str, Any] | None
    instance_id: NativeId
    project_id: NativeId
    offset: Count
    limit: int = Field(ge=1, le=100)
    current_revision: int = Field(ge=0)
    adoptable: bool
    native_review_qualification: Literal['independent', 'native_disposition']
    current: dict[str, Any]
    history: dict[str, Any]

    @model_validator(mode='after')
    def independent_native_review(self):
        expected = 'independent' if self.scenario == 'ocr' else 'native_disposition'
        if self.native_review_qualification != expected:
            raise ValueError('native review qualification must describe its own scenario')
        return self


class ProcessingBatch(Wire):
    automation_version: Literal['2.0'] = AUTOMATION_VERSION
    revision: Revision = 1
    coverage: BatchCoverage = Field(default_factory=BatchCoverage)
    delivery: BatchDelivery = Field(default_factory=BatchDelivery)
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
                     BatchControl, LocalActor, NativeAdoption, BatchDisposition,
                     BatchCoverage, BatchDelivery, BatchOperation, ReviewArtifactRequest,
                     ReviewBundleFile, ReviewUnresolvedItem, ReviewBundle,
                     BatchIssuePage, BatchItemRead, BatchHistoryEvent, BatchHistoryPage,
                     BatchNativeRevisionPage, ProcessingBatch]

AUTOMATION_EXAMPLES = {
    'BatchDisposition': {'request_id': 'requestA', 'expected_revision': 1,
        'actor': {'id': 'localUser', 'display_name': '本机编制人', 'source': 'local'},
        'item_ids': ['itemA'], 'action': 'adopt', 'note': '采用原生校对 revision 2',
        'adoption': {'native_revision': 2, 'native_result_id': '01result'}},
    'BatchCoverage': {'input_files': 2, 'left_rows': 50001, 'right_rows': 50001,
        'result_rows': 100002, 'required_items': 100002, 'indexed_items': 100002,
        'scanned_results': 100002, 'next_ordinal': 100002, 'index_complete': True,
        'pending': 100002, 'confirmed': 0, 'deferred': 0},
}
