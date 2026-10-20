"""Additive contracts for manual multimodal workers; source assets remain authoritative."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .models import Id, Hash, Revision, SourceLocator

class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')

class ManualInput(Payload):
    asset_id: Id
    asset_revision: Revision
    blob_hash: Hash

class ManualOptions(Payload):
    device: Literal['cpu', 'cuda'] = 'cpu'
    action: Literal['detect', 'compare', 'search', 'analyze'] = 'analyze'
    face_indices: list[int] = Field(default_factory=list, max_length=2)
    top_k: int = Field(default=5, ge=1, le=20)
    threshold: float = Field(default=.363, ge=-1, le=1, allow_inf_nan=False)
    expected_subject: str = Field(default='', max_length=256)
    channel: int | None = Field(default=None, ge=0, le=63)
    # None is the legacy manual-library search. Agent submissions always supply
    # the explicitly authorized IDs (including an empty list).
    reference_ids: list[Id] | None = Field(default=None, max_length=1000)

class ManualControl(Payload):
    request_id: Id | None = None
    expected_revision: Revision | None = None
    expected_generation: Revision | None = None

class ManualReferenceDelete(Payload):
    request_id: Id | None = None
    expected_revision: Revision | None = None

class ManualDraftTransfer(Payload):
    request_id: Id | None = None
    snapshot_id: Id | None = None
    expected_revision: Revision | None = None

class ManualAnalysisCreate(Payload):
    request_id: Id
    kind: Literal['face', 'audio', 'seal', 'catnet']
    inputs: list[ManualInput] = Field(min_length=1, max_length=2)
    options: ManualOptions = Field(default_factory=ManualOptions)

class ManualRegion(Payload):
    box: tuple[float, float, float, float]
    note: str = Field(min_length=1, max_length=2000)
    source: SourceLocator | None = None

class ManualReview(Payload):
    request_id: Id
    expected_revision: Revision
    actor: str = Field(min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=10000)
    assessment: Literal['confirmed', 'needs_investigation', 'inconclusive']
    # Edits are versioned separately from the immutable model result.
    edits: list[dict] = Field(default_factory=list, max_length=50000)
    regions: list[ManualRegion] = Field(default_factory=list, max_length=200)

class ManualFaceReferenceCreate(Payload):
    request_id: Id
    job_id: Id
    input_index: int = Field(default=0, ge=0, le=1)
    face_index: int = Field(ge=0, le=1000)
    label: str = Field(min_length=1, max_length=128)

class ManualSegment(Payload):
    row_id: Id | None = None
    text: str
    speaker: str | None = None
    source: SourceLocator

class ManualAnalysis(Payload):
    schema_version: Literal['1.0']
    job_id: Id
    workspace_id: Id
    request_id: Id
    kind: Literal['face','audio','seal','catnet']
    inputs: list[dict]
    options: ManualOptions
    status: Literal['queued','running','cancelling','cancelled','failed','succeeded','needs_verification']
    stage: str
    revision: Revision
    generation: Revision
    created_at: str
    updated_at: str
    result: dict | None
    result_hash: Hash | None = None
    review: dict | None
    error: str | None
    progress: dict = Field(default_factory=dict)
    actual_device: str | None = None
    purpose: Literal['analysis','preparation'] = 'analysis'

class ManualAnalysisPage(Payload):
    items: list[ManualAnalysis]
    total: int = Field(ge=0)

class ManualResultSnapshot(Payload):
    schema_version: Literal['1.0']
    snapshot_id: Id
    workspace_id: Id
    job_id: Id
    kind: Literal['face','audio','seal','catnet']
    generation: Revision
    revision: Revision
    review_revision: int = Field(ge=0)
    result_hash: Hash
    model_identity: dict
    sources: list[dict]
    reference_scope: list[dict]
    review_state: Literal['reviewed','unreviewed']
    analysis: ManualAnalysis
    snapshot_hash: Hash

class ManualOperationReceipt(Payload):
    found: bool
    operation_type: str
    request_id: str
    resource_id: str | None
    receipt: dict | None

class ManualDraftTransferReceipt(Payload):
    session_id: str
    job: dict
    asset: dict
    snapshot_id: Id
    draft: Literal[True]
    review_state: Literal['unreviewed']
    location: str

Create,Review,Reference=ManualAnalysisCreate,ManualReview,ManualFaceReferenceCreate
MULTIMODAL_MODELS=[ManualAnalysisCreate,ManualReview,ManualFaceReferenceCreate,ManualControl,ManualReferenceDelete,
                  ManualDraftTransfer,ManualSegment,ManualAnalysis,ManualAnalysisPage,ManualResultSnapshot,
                  ManualOperationReceipt,ManualDraftTransferReceipt]

def multimodal_paths(ref,response,body,parameter):
    base='/api/platform/v1/workspaces/{workspace_id}/multimodal'
    def op(name,result,extra=(),payload=None,code='200',optional=False):
        value={'operationId':name,'parameters':[parameter('workspace_id'),*extra],
            'responses':{code:response(result),'400':response('Error'),'404':response('Error'),'409':response('Error'),'503':response('Error')}}
        if payload:
            value['requestBody']=body(payload)
            if optional:value['requestBody']['required']=False
        return value
    job=base+'/jobs/{job_id}';identifier=[parameter('job_id')]
    return {base+'/jobs':{'get':op('listManualAnalyses','ManualAnalysisPage'),
        'post':op('startManualAnalysis','ManualAnalysis',payload='ManualAnalysisCreate',code='202')},
        job:{'get':op('getManualAnalysis','ManualAnalysis',identifier)},
        job+'/review':{'post':op('reviewManualAnalysis','ManualAnalysis',identifier,'ManualReview')},
        job+'/control/{action}':{'post':op('controlManualAnalysis','ManualAnalysis',[*identifier,parameter('action')],'ManualControl',optional=True)},
        base+'/operations/{operation_type}/{request_id}':{'get':op('lookupManualOperation','ManualOperationReceipt',[parameter('operation_type'),parameter('request_id')])},
        job+'/snapshot':{'get':op('currentManualResultSnapshot','ManualResultSnapshot',identifier)},
        job+'/snapshots/{snapshot_id}':{'get':op('getManualResultSnapshot','ManualResultSnapshot',[*identifier,parameter('snapshot_id')])},
        job+'/snapshots/{snapshot_id}/download':{'get':{'operationId':'exportManualResultSnapshot',
            'parameters':[parameter('workspace_id'),*identifier,parameter('snapshot_id')],
            'responses':{'200':{'description':'Immutable snapshot ZIP with draft/reviewed state'},'404':response('Error'),'409':response('Error')}}},
        job+'/snapshots/{snapshot_id}/files/{name}':{'get':{'operationId':'getManualSnapshotOutput',
            'parameters':[parameter('workspace_id'),*identifier,parameter('snapshot_id'),parameter('name')],
            'responses':{'200':{'description':'Hash-verified immutable output'},'404':response('Error'),'409':response('Error')}}},
        job+'/draft-to-data':{'post':op('manualDraftToData','ManualDraftTransferReceipt',identifier,'ManualDraftTransfer')},
        job+'/download':{'get':{'operationId':'exportManualAnalysis','parameters':[parameter('workspace_id'),*identifier],
            'responses':{'200':{'description':'ZIP with reviewed JSON/CSV/subtitles and hash-verified outputs'},'409':response('Error')}}}}
