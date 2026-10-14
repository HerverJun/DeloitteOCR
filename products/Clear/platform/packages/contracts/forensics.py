"""Local image inspections and explicit human review, never an automatic verdict."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import Id, Hash, Revision, SourceLocator, Utc, Wire
from .automation import LocalActor

Tool = Literal['metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move']


class InspectionCreate(Wire):
    request_id: Id
    asset_id: Id
    asset_revision: Revision
    blob_hash: Hash
    tools: list[Tool] = Field(min_length=1, max_length=6)

    @model_validator(mode='after')
    def unique_tools(self):
        if len(set(self.tools)) != len(self.tools):
            raise ValueError('duplicate tool')
        return self


class InspectionReview(Wire):
    request_id: Id
    expected_revision: Revision
    actor: LocalActor
    assessment: Literal['needs_investigation', 'inconclusive', 'no_supported_clue']
    note: str = Field(min_length=1, max_length=4000)

    @model_validator(mode='after')
    def meaningful_note(self):
        if not self.note.strip():
            raise ValueError('review requires explanation')
        return self


class InspectionFile(Wire):
    name: str
    sha256: Hash
    size_bytes: int = Field(ge=0)
    media_type: str


class Inspection(Wire):
    inspection_id: Id
    workspace_id: Id
    request_id: Id
    revision: Revision
    asset_id: Id
    asset_revision: Revision
    blob_hash: Hash
    original_name: str
    tools: list[Tool]
    status: Literal['queued', 'running', 'succeeded', 'partial', 'failed', 'needs_verification']
    error: str | None
    report_hash: Hash | None
    report: dict[str, Any] | None
    files: list[InspectionFile]
    review: dict[str, Any] | None
    review_history: list[dict[str, Any]]
    created_at: Utc
    updated_at: Utc


class InspectionPage(Wire):
    items: list[Inspection]
    total: int
    next_offset: int | None


class ForensicEvidenceSelection(Wire):
    inspection_id: Id
    inspection_revision: Revision
    report_hash: Hash


class ForensicEvidenceRef(ForensicEvidenceSelection):
    evidence_id: Id
    project_id: Id
    workspace_id: Id
    scenario: Literal['image_forensics']
    issue_type: Literal['result']
    snapshot_hash: Hash
    captured_at: Utc
    sources: list[SourceLocator]
    source_count: int = Field(ge=1)


class ForensicEvidenceSnapshot(Wire):
    scenario: Literal['image_forensics']
    workspace_id: Id
    inspection: Inspection
    sources: list[SourceLocator]
    source_count: int
    snapshot_hash: Hash


class ForensicBundleInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    role: Literal['image']
    name: str
    asset_id: Id
    revision: Revision


class ForensicBundleFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str
    artifact_id: Id
    revision: Revision
    blob_hash: Hash
    size_bytes: int
    media_type: str
    availability: Literal['available']


class ForensicBundle(Wire):
    bundle_id: Id
    workspace_id: Id
    kind: Literal['image_forensics']
    title: str
    inspection_id: Id
    report_hash: Hash
    created_at: Utc
    inputs: list[ForensicBundleInput] = Field(min_length=1, max_length=1)
    files: list[ForensicBundleFile] = Field(min_length=3)


FORENSICS_MODELS = [InspectionCreate, InspectionReview, InspectionFile, Inspection,
                    InspectionPage, ForensicEvidenceSelection, ForensicEvidenceRef, ForensicEvidenceSnapshot, ForensicBundle]


def forensics_paths(ref, response, body, parameter):
    base = '/api/platform/v1/workspaces/{workspace_id}/forensics'
    def op(name, result, extra=(), payload=None):
        value = {'operationId': name, 'parameters': [parameter('workspace_id'), *extra],
                 'responses': {'200': response(result), '400': response('Error'),
                               '409': response('Error'), '503': response('Error')}}
        if payload:
            value['requestBody'] = body(payload)
        return value
    item = base + '/{inspection_id}'
    return {base: {'get': op('listImageInspections', 'InspectionPage', [
        {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0}},
        {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 200}}]),
        'post': {**op('startImageInspection', 'Inspection', payload='InspectionCreate'),
                 'responses': {'202': response('Inspection'), '409': response('Error')}}},
        item: {'get': op('getImageInspection', 'Inspection', [parameter('inspection_id')])},
        item + '/review': {'post': op('reviewImageInspection', 'Inspection', [parameter('inspection_id')], 'InspectionReview')},
        item + '/resume': {'post': op('resumeInterruptedImageInspection', 'Inspection', [parameter('inspection_id')])},
        item + '/collect': {'post': {'operationId': 'collectImageEvidenceBundle',
            'parameters': [parameter('workspace_id'), parameter('inspection_id')],
            'responses': {'200': response('ForensicBundle'), '409': response('Error')}}},
        item + '/files/{name}': {'get': {'operationId': 'downloadImageEvidence',
            'parameters': [parameter('workspace_id'), parameter('inspection_id'),
                           {'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
            'responses': {'200': {'description': 'SHA256-verified evidence bytes'}, '409': response('Error')}}}}
