"""Versioned local audit projects, investigations, evidence and workpapers.

D13/D14 use the business models; todo models reserve the D17/D18 contract.
Generate JSON Schema, OpenAPI and frontend types through contracts.generate.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator
from packages.contracts.models import (
    Count, Cursor, Hash, Id, NativeId, Revision, SourceLocator, Timestamped, Utc,
)
from packages.contracts.automation import (
    ExecutionState, LocalActor, ReviewBundleFile, Scenario,
)

from .forensics import ForensicEvidenceSelection, ForensicEvidenceRef, ForensicEvidenceSnapshot
from .agent import AgentModel, AgentRef, Identity

AuditScenario = Scenario | Literal['image_forensics']

AUDIT_VERSION = '1.0'
Priority = Literal['high', 'normal', 'low']
EntityKind = Literal['project', 'procedure', 'finding']
AuditDate = Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
Text = Annotated[str, Field(max_length=4000)]
Title = Annotated[str, Field(min_length=1, max_length=200)]
ValueText = Annotated[str, Field(max_length=40000)]


class AuditStrict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('created_at', 'updated_at', 'captured_at', 'expires_at',
                     'opened_at', 'creation_time', check_fields=False)
    @classmethod
    def actual_utc_timestamp(cls, value):
        return Timestamped.utc_timestamp(value)


class AuditWire(Timestamped):
    audit_version: Literal['1.0'] = AUDIT_VERSION


class AuditCommand(AuditWire):
    request_id: Id
    actor: LocalActor


class AuditConditionalCommand(AuditCommand):
    expected_revision: Revision


class AuditProjectFields(AuditStrict):
    title: Title
    audit_object: Title
    period_start: AuditDate
    period_end: AuditDate
    objective: Text

    @model_validator(mode='after')
    def valid_period(self):
        if date.fromisoformat(self.period_start) > date.fromisoformat(self.period_end):
            raise ValueError('period start must not follow period end')
        return self


class AuditBatchSelection(AuditStrict):
    batch_id: Id
    batch_revision: Revision


class AuditBatchRef(AuditBatchSelection):
    template_id: Id
    template_version: Revision
    config_hash: Hash


class AuditProcedureFields(AuditStrict):
    title: Title
    basis: Text
    scope: Text
    note: Text = ''
    conclusion: Text = ''


class AuditFindingFields(AuditStrict):
    title: Title
    priority: Priority = 'normal'
    explanation: Text = ''
    conclusion: Text = ''
    status: Literal['draft', 'resolved'] = 'draft'


class AuditEvidenceSelection(AuditStrict):
    batch_id: Id
    item_id: Id
    batch_revision: Revision


class AuditEvidenceKeep(AuditStrict):
    evidence_id: Id


class AuditEvidenceRef(AuditEvidenceSelection):
    """Server-created immutable snapshot identity, retained after unlinking."""
    evidence_id: Id
    project_id: Id
    workspace_id: Id
    scenario: Scenario
    issue_type: Literal['result', 'technical']
    snapshot_hash: Hash
    captured_at: Utc
    sources: list[SourceLocator] = Field(max_length=10)
    source_count: Count


class DraftTransferSource(AuditStrict):
    task_id: Identity
    draft_id: Identity
    revision: Revision
    snapshot_hash: Hash
    scope_revision: Revision
    project_context_revision: int = Field(ge=0)


class AuditDirectEvidenceSelection(AuditStrict):
    task_id: Identity
    source_evidence_id: Identity
    snapshot_hash: Hash


class ManualEvidenceSelection(AuditStrict):
    manual_bundle_id: Id


class AuditDirectEvidenceRef(AuditDirectEvidenceSelection):
    evidence_id: Id
    project_id: Id
    workspace_id: Id
    scenario: Literal['direct_tool'] = 'direct_tool'
    captured_at: Utc
    sources: list[SourceLocator] = Field(max_length=200)
    source_assets: list[SourceLocator] = Field(max_length=10000)
    source_count: Count


class AuditFindingOrigin(AuditStrict):
    source: DraftTransferSource
    content: dict
    author: dict
    transfer_actor: LocalActor
    transfer_kind: Literal['human', 'agent']
    transferred_at: Utc


class AuditRecord(AuditStrict):
    record_id: Id
    entity_kind: EntityKind
    entity_id: Id
    content_revision: Revision
    project_context_revision: Revision
    project_revision: Revision
    role: Literal['prepared', 'reviewed']
    actor: LocalActor
    note: Text
    created_at: Utc


def _check_records(owner, entity_kind, entity_id, project_revision):
    roles = [record.role for record in owner.records]
    if len(set(roles)) != len(roles):
        raise ValueError('duplicate record role')
    for record in owner.records:
        if (record.entity_kind, record.entity_id) != (entity_kind, entity_id):
            raise ValueError('record belongs to another entity')
    if owner.records_current:
        if set(roles) != {'prepared', 'reviewed'}:
            raise ValueError('current records require both roles')
        for record in owner.records:
            if (record.content_revision != owner.content_revision or
                    record.project_context_revision != owner.project_context_revision or
                    record.project_revision > project_revision):
                raise ValueError('current record version differs')
        by_role = {record.role: record for record in owner.records}
        prepared = datetime.fromisoformat(by_role['prepared'].created_at.replace('Z', '+00:00'))
        reviewed = datetime.fromisoformat(by_role['reviewed'].created_at.replace('Z', '+00:00'))
        if reviewed < prepared:
            raise ValueError('review cannot precede preparation')
    return owner


class AuditProjectCreate(AuditCommand):
    project: AuditProjectFields


class AuditProjectWrite(AuditConditionalCommand):
    project: AuditProjectFields


class AuditProcedureCreate(AuditConditionalCommand):
    procedure: AuditProcedureFields
    batches: list[AuditBatchSelection] = Field(max_length=100)


class AuditProcedureWrite(AuditProcedureCreate):
    pass


class AuditFindingCreate(AuditConditionalCommand):
    procedure_id: Id
    finding: AuditFindingFields | None = None
    evidence: list[AuditEvidenceSelection | ForensicEvidenceSelection | AuditEvidenceKeep | AuditDirectEvidenceSelection | ManualEvidenceSelection] = Field(default_factory=list, max_length=100)
    source_draft: DraftTransferSource | None = None

    @model_validator(mode='after')
    def one_source(self):
        if self.source_draft is None and self.finding is None:
            raise ValueError('finding or fixed source draft is required')
        if self.source_draft is not None and (self.finding is not None or self.evidence):
            raise ValueError('source draft supplies server-owned content and evidence')
        return self


class AuditFindingWrite(AuditConditionalCommand):
    finding: AuditFindingFields
    evidence: list[AuditEvidenceSelection | ForensicEvidenceSelection | AuditEvidenceKeep | AuditDirectEvidenceSelection | ManualEvidenceSelection] = Field(max_length=100)


class AuditRecordCreate(AuditConditionalCommand):
    entity_kind: EntityKind
    entity_id: Id
    content_revision: Revision
    project_context_revision: Revision
    role: Literal['prepared', 'reviewed']
    note: Text = ''


class AuditProject(AuditWire):
    project_id: Id
    workspace_id: Id
    revision: Revision
    content_revision: Revision
    project_context_revision: Revision
    project: AuditProjectFields
    procedure_count: Count
    finding_count: Count
    records: list[AuditRecord] = Field(max_length=2)
    records_current: bool
    created_at: Utc
    updated_at: Utc

    @model_validator(mode='after')
    def consistent_records(self):
        return _check_records(self, 'project', self.project_id, self.revision)


class AuditProcedure(AuditWire):
    procedure_id: Id
    project_id: Id
    project_revision: Revision
    content_revision: Revision
    project_context_revision: Revision
    procedure: AuditProcedureFields
    batches: list[AuditBatchRef] = Field(max_length=100)
    records: list[AuditRecord] = Field(max_length=2)
    records_current: bool
    created_at: Utc
    updated_at: Utc

    @model_validator(mode='after')
    def consistent_records(self):
        return _check_records(self, 'procedure', self.procedure_id, self.project_revision)


class AuditFinding(AuditWire):
    finding_id: Id
    procedure_id: Id
    project_id: Id
    project_revision: Revision
    content_revision: Revision
    project_context_revision: Revision
    finding: AuditFindingFields
    evidence_refs: list[AuditEvidenceRef | ForensicEvidenceRef | AuditDirectEvidenceRef] = Field(max_length=100)
    origin: AuditFindingOrigin | None = None
    outdated: bool = False
    outdated_reasons: list[str] = Field(default_factory=list, max_length=100)
    records: list[AuditRecord] = Field(max_length=2)
    records_current: bool
    created_at: Utc
    updated_at: Utc

    @model_validator(mode='after')
    def consistent_records(self):
        if any(ref.project_id != self.project_id for ref in self.evidence_refs):
            raise ValueError('finding evidence belongs to another project')
        return _check_records(self, 'finding', self.finding_id, self.project_revision)


class AuditWriteReceipt(AuditWire):
    """Fixed transaction receipt; GET entities supplies current content."""
    request_id: Id
    project_id: Id
    project_revision: Revision
    entity_kind: Literal['project', 'procedure', 'finding', 'record']
    entity_id: Id
    payload_hash: Hash
    actor: LocalActor
    created_at: Utc


class AuditPageBase(AuditWire):
    total: Count
    snapshot_token: Cursor
    next_cursor: Cursor | None


class AuditProjectPage(AuditPageBase):
    workspace_id: Id
    items: list[AuditProject] = Field(max_length=200)


class AuditProcedurePage(AuditPageBase):
    project_id: Id
    project_revision: Revision
    items: list[AuditProcedure] = Field(max_length=200)


class AuditFindingPage(AuditPageBase):
    project_id: Id
    project_revision: Revision
    items: list[AuditFinding] = Field(max_length=200)


class AuditHistoryEvent(AuditStrict):
    event_id: Id
    project_id: Id
    project_revision: Revision
    entity_kind: Literal['project', 'procedure', 'finding', 'record', 'workpaper']
    entity_id: Id
    request_id: Id
    actor: LocalActor
    action: str = Field(min_length=1, max_length=64)
    previous: dict[str, Any] | None
    current: dict[str, Any]
    created_at: Utc

    @field_validator('previous', 'current')
    @classmethod
    def bounded_history_object(cls, value):
        if value is not None and len(json.dumps(
                value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 262144:
            raise ValueError('history object exceeds limit; do not truncate')
        return value


class AuditHistoryPage(AuditPageBase):
    project_id: Id
    project_revision: Revision
    items: list[AuditHistoryEvent] = Field(max_length=200)


class WorkpaperReviewRef(AuditStrict):
    bundle_id: Id
    batch_id: Id
    batch_revision: Revision
    snapshot_hash: Hash
    results_xlsx: ReviewBundleFile

    @model_validator(mode='after')
    def exact_result_file(self):
        if self.results_xlsx.name != 'results.xlsx':
            raise ValueError('review reference must name results.xlsx')
        return self


class WorkpaperRequest(AuditConditionalCommand):
    kind: Literal['intermediate', 'final']
    review_bundle_ids: list[Id] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def distinct_choices(self):
        if len(set(self.review_bundle_ids)) != len(self.review_bundle_ids):
            raise ValueError('duplicate review bundle')
        return self


class WorkpaperBundle(AuditWire):
    """Ready only; fixed receipt bytes and dynamic GET current/outdated status."""
    workpaper_id: Id
    bundle_id: Id
    workspace_id: Id
    project_id: Id
    project_revision: Revision
    kind: Literal['audit_workpaper']
    delivery_kind: Literal['intermediate', 'final']
    status: Literal['current', 'outdated']
    request_id: Id
    payload_hash: Hash
    snapshot_hash: Hash
    actor: LocalActor
    title: Title
    created_at: Utc
    predecessor_workpaper_id: Id | None
    successor_workpaper_ids: list[Id] = Field(max_length=100)
    successor_count: Count
    files: list[ReviewBundleFile] = Field(min_length=2, max_length=2)
    review_refs: list[WorkpaperReviewRef] = Field(max_length=100)
    forensic_refs: list[ForensicEvidenceRef] = Field(default_factory=list, max_length=100)
    unresolved_count: Count
    unresolved_reasons: list[Title] = Field(max_length=30)

    @model_validator(mode='after')
    def ready_files(self):
        if self.bundle_id != self.workpaper_id:
            raise ValueError('workpaper and artifact-center bundle identity must match')
        if (self.predecessor_workpaper_id == self.workpaper_id or
                self.workpaper_id in self.successor_workpaper_ids or
                len(set(self.successor_workpaper_ids)) != len(self.successor_workpaper_ids) or
                len(self.successor_workpaper_ids) > self.successor_count):
            raise ValueError('invalid workpaper predecessor or successor identity')
        if {item.name for item in self.files} != {'workpaper.md', 'workpaper.json'}:
            raise ValueError('workpaper must contain both fixed formats')
        if self.delivery_kind == 'final' and (
                self.unresolved_count or self.unresolved_reasons or not (self.review_refs or self.forensic_refs)):
            raise ValueError('final workpaper cannot contain unresolved work')
        return self


class AuditRequestStatus(AuditWire):
    request_id: Id
    project_id: Id
    payload_hash: Hash
    state: Literal['committed', 'publishing', 'needs_verification']
    receipt: AuditWriteReceipt | WorkpaperBundle | None

    @model_validator(mode='after')
    def fixed_receipt_identity(self):
        if self.state == 'committed':
            if self.receipt is None:
                raise ValueError('committed request requires a receipt')
            if (self.receipt.request_id, self.receipt.project_id, self.receipt.payload_hash) != (
                    self.request_id, self.project_id, self.payload_hash):
                raise ValueError('request receipt identity differs')
        elif self.receipt is not None:
            raise ValueError('pending request cannot contain a ready receipt')
        return self


class TodoQuery(AuditStrict):
    kind: Literal['issue', 'finding'] | None = None
    issue_type: Literal['result', 'technical', 'investigation'] | None = None
    scenario: AuditScenario | None = None
    disposition: Literal['pending', 'confirmed', 'deferred'] | None = None
    finding_status: Literal['draft', 'resolved'] | None = None
    execution_state: ExecutionState | None = None
    project_id: Id | None = None
    procedure_id: Id | None = None
    priority: Priority | None = None
    sort: Literal['priority_then_updated', 'updated'] = 'priority_then_updated'
    limit: int = Field(default=50, strict=True, ge=1, le=200)
    cursor: Cursor | None = None
    snapshot_token: Cursor | None = None


class TodoContext(AuditStrict):
    workspace_id: Id
    project_id: Id | None = None
    procedure_id: Id | None = None
    batch_id: Id | None = None
    item_id: Id | None = None
    finding_id: Id | None = None


class TodoItem(AuditStrict):
    todo_id: Id
    kind: Literal['issue', 'finding']
    issue_type: Literal['result', 'technical', 'investigation']
    context: TodoContext
    scenario: AuditScenario | None
    batch_revision: Revision | None
    project_revision: Revision | None
    title: Title
    state: str = Field(min_length=1, max_length=64)
    priority: Priority
    priority_basis: Literal['manual_investigation', 'default_review_order']
    updated_at: Utc
    source_summary: str = Field(max_length=1000)
    actions: list[Literal['open_evidence', 'open_native', 'open_audit',
                          'confirm', 'defer', 'reopen', 'adopt']] = Field(max_length=7)


class TodoPage(AuditPageBase):
    workspace_id: Id
    query_hash: Hash
    sort: Literal['priority_then_updated', 'updated']
    items: list[TodoItem] = Field(max_length=200)


class TodoLocate(AuditWire):
    item: TodoItem
    matches_filters: bool
    snapshot_token: Cursor
    page_cursor: Cursor | None


class EvidenceValue(AuditStrict):
    """Literal source values; integer/decimal/formula strings stay strings."""
    role: Literal['left', 'right', 'original', 'adopted', 'manual']
    kind: Literal['null', 'text', 'decimal', 'integer', 'boolean', 'date',
                  'error', 'unknown_formula', 'unknown']
    raw: ValueText | bool | None
    value: ValueText | bool | None
    source: SourceLocator | None
    row_id: str | None = Field(default=None, pattern=r'^r[a-f0-9]{32}$')
    table_revision: str | None = Field(default=None, pattern=r'^v[a-f0-9]{64}$')

    @model_validator(mode='after')
    def literal_value_type(self):
        if self.kind in {'integer', 'decimal'}:
            if not isinstance(self.value, str) or isinstance(self.raw, bool):
                raise ValueError('numeric evidence must preserve literal strings')
            pattern = (r'[+-]?[0-9]+' if self.kind == 'integer' else
                       r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
            if not re.fullmatch(pattern, self.value):
                raise ValueError('invalid numeric evidence string')
            try:
                if not Decimal(self.value).is_finite():
                    raise ValueError('nonfinite numeric evidence')
            except InvalidOperation:
                raise ValueError('invalid numeric evidence string') from None
        elif self.kind == 'boolean':
            if type(self.value) is not bool:
                raise ValueError('boolean evidence requires a boolean value')
        elif self.kind in {'text', 'date'}:
            if not isinstance(self.value, str) or isinstance(self.raw, bool):
                raise ValueError('text/date evidence requires literal strings')
        elif self.kind in {'null', 'unknown_formula'}:
            if self.value is not None or isinstance(self.raw, bool):
                raise ValueError('null/unknown formula evidence cannot contain an adopted value')
        elif self.kind == 'error':
            if isinstance(self.value, bool) or isinstance(self.raw, bool):
                raise ValueError('error evidence requires text or null')
        return self


class EvidenceField(AuditStrict):
    name: str = Field(min_length=1, max_length=256)
    values: list[EvidenceValue] = Field(max_length=5)
    rule_kind: Literal['exact', 'text', 'amount', 'native_revision',
                       'unmatched', 'ambiguous', 'technical', 'unknown']
    outcome: Literal['equal', 'different', 'within_tolerance', 'outside_tolerance',
                     'unmatched', 'ambiguous', 'adopted', 'unadopted', 'unknown']
    delta: str | None = Field(default=None, max_length=128)
    tolerance: str | None = Field(default=None, max_length=128)
    within_tolerance: bool | None = None

    @field_validator('delta', 'tolerance')
    @classmethod
    def finite_decimal_text(cls, value):
        if value is not None:
            try:
                if not Decimal(value).is_finite():
                    raise ValueError('nonfinite decimal explanation')
            except InvalidOperation:
                raise ValueError('invalid decimal explanation') from None
        return value


class ItemEvidence(AuditWire):
    workspace_id: Id
    batch_id: Id
    item_id: Id
    batch_revision: Revision
    scenario: Scenario
    issue_type: Literal['result', 'technical']
    snapshot_hash: Hash
    template_id: Id
    template_version: Revision
    config_hash: Hash
    result_id: NativeId | None
    native_version_id: NativeId | None
    native_revision: Count | None
    adoption_operation_id: Id | None
    rule_summary: str = Field(max_length=4000)
    rule_hash: Hash
    fields: list[EvidenceField] = Field(max_length=256)
    sources: list[SourceLocator] = Field(max_length=10)
    source_count: Count
    native_context_available: bool


class DirectEvidenceRequest(AgentModel):
    reference: AgentRef
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class DirectEvidenceSnapshot(AgentModel):
    snapshot_version: Literal[1, 2] = 1
    scenario: Literal['direct_tool'] = 'direct_tool'
    evidence_id: Identity
    workspace_id: Identity
    task_id: Identity
    reference: AgentRef
    snapshot_hash: Hash
    summary: str = Field(max_length=4000)
    sources: list[SourceLocator] = Field(default_factory=list, max_length=200)
    source_count: int = Field(ge=0)
    source_assets: list[SourceLocator] = Field(default_factory=list, max_length=10000)
    observation: dict
    created_at: str
    location_url: str


class AuditDraftWrite(AgentModel):
    request_id: Identity
    expected_revision: int = Field(ge=0)
    draft_id: Identity | None = None
    title: str = Field(min_length=1, max_length=200)
    facts: str = Field(default='', max_length=12000)
    interpretation: str = Field(default='', max_length=12000)
    missing_information: str = Field(default='', max_length=8000)
    recommendations: str = Field(default='', max_length=8000)
    rule: str = Field(default='', max_length=8000)
    evidence_ids: list[Identity] = Field(default_factory=list, max_length=100)
    status: Literal['draft', 'confirmed', 'dismissed'] = 'draft'


class AuditDraftExport(AgentModel):
    request_id: Identity
    draft_ids: list[Identity] = Field(min_length=1, max_length=100)
    title: str = Field(default='审计工作底稿草稿', min_length=1, max_length=200)


class AuditEvidenceSnapshot(RootModel[ItemEvidence | ForensicEvidenceSnapshot | DirectEvidenceSnapshot]):
    pass


AUDIT_MODELS = [AuditEvidenceSnapshot, DirectEvidenceRequest, DirectEvidenceSnapshot, AuditDraftWrite, AuditDraftExport,
    DraftTransferSource, AuditDirectEvidenceSelection, AuditDirectEvidenceRef, AuditFindingOrigin,
    AuditProjectCreate, AuditProjectWrite, AuditProcedureCreate,
    AuditProcedureWrite, AuditFindingCreate, AuditFindingWrite, AuditRecordCreate,
    AuditProject, AuditProcedure, AuditFinding, AuditEvidenceRef, AuditWriteReceipt,
    AuditRequestStatus, AuditProjectPage, AuditProcedurePage, AuditFindingPage,
    AuditHistoryPage, WorkpaperRequest, WorkpaperBundle, TodoQuery, TodoPage,
    TodoLocate, ItemEvidence,
]
