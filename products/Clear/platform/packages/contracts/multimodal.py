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

class ManualAnalysisPage(Payload):
    items: list[ManualAnalysis]
    total: int = Field(ge=0)

Create,Review,Reference=ManualAnalysisCreate,ManualReview,ManualFaceReferenceCreate
MULTIMODAL_MODELS=[ManualAnalysisCreate,ManualReview,ManualFaceReferenceCreate,ManualSegment,ManualAnalysis,ManualAnalysisPage]

def multimodal_paths(ref,response,body,parameter):
    base='/api/platform/v1/workspaces/{workspace_id}/multimodal'
    def op(name,result,extra=(),payload=None,code='200'):
        value={'operationId':name,'parameters':[parameter('workspace_id'),*extra],
            'responses':{code:response(result),'400':response('Error'),'404':response('Error'),'409':response('Error'),'503':response('Error')}}
        if payload:value['requestBody']=body(payload)
        return value
    job=base+'/jobs/{job_id}';identifier=[parameter('job_id')]
    return {base+'/jobs':{'get':op('listManualAnalyses','ManualAnalysisPage'),
        'post':op('startManualAnalysis','ManualAnalysis',payload='ManualAnalysisCreate',code='202')},
        job:{'get':op('getManualAnalysis','ManualAnalysis',identifier)},
        job+'/review':{'post':op('reviewManualAnalysis','ManualAnalysis',identifier,'ManualReview')},
        job+'/control/{action}':{'post':op('controlManualAnalysis','ManualAnalysis',[*identifier,parameter('action')])},
        job+'/download':{'get':{'operationId':'exportManualAnalysis','parameters':[parameter('workspace_id'),*identifier],
            'responses':{'200':{'description':'ZIP with reviewed JSON/CSV/subtitles and hash-verified outputs'},'409':response('Error')}}}}
