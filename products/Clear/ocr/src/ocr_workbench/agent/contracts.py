"""P0 wire contracts; database ownership/authorization checks belong to P1.

Models accept JSON-compatible values without coercing strings, floats or bools
to integers. Provider arguments must be validated as a complete object before
any tool in that response executes. This module never dispatches business work.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

CONTRACT_VERSION = "ocr-agent-v1"
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Revision = Annotated[int, Field(ge=0)]  # Existing results start at revision zero.
PageNumber = Annotated[int, Field(ge=1)]
Cursor = Annotated[str, Field(min_length=1, max_length=1024)]
Ids = Annotated[list[Id], Field(min_length=1, max_length=100)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResultRef(StrictModel):
    result_id: Id
    revision: Revision
    version_id: Id


class EvidenceRef(ResultRef):
    ref_id: Id
    project_id: Id
    document_id: Id
    page_id: Id
    page_number: PageNumber
    table_id: Id | None = None
    target_id: Id | None = None
    is_adopted: bool
    source_run_id: Id | None = None


class JobRef(StrictModel):
    kind: Literal["ocr", "pdf_stage", "fusion", "visual_review", "structure"]
    job_id: Id


class JobLink(JobRef):
    operation_id: Id
    ownership: Literal["created", "reused"]
    input_revision: Revision | None = None
    state: Literal["queued", "running", "waiting_gpu", "paused", "interrupted", "succeeded", "failed", "cancelled"]


class ArtifactRef(StrictModel):
    artifact_id: Id
    state: Literal["staging", "ready", "expired", "missing", "corrupt", "deleted", "failed"]


class GetWorkspaceContext(StrictModel):
    selection_token: Id | None = None
    cursor: Cursor | None = None


class SearchDocument(StrictModel):
    document_id: Id
    query: Annotated[str, Field(min_length=1, max_length=200)]
    limit: Annotated[int, Field(ge=1, le=50)] = 20
    cursor: Cursor | None = None


class ReadPageResult(StrictModel):
    page_id: Id
    source: Literal["adopted", "run_result"] = "adopted"
    result_id: Id | None = None
    table_id: Id | None = None
    target_ids: Ids | None = None
    cursor: Cursor | None = None

    @model_validator(mode="after")
    def source_selection(self):
        if (self.source == "run_result") != (self.result_id is not None):
            raise ValueError("Only run_result requires an explicit result_id")
        return self


class ProcessPages(StrictModel):
    document_id: Id
    page_numbers: Annotated[list[PageNumber], Field(min_length=1, max_length=100)]
    mode: Literal["auto", "native", "ocr"] = "auto"
    engine: Id | None = None
    force: bool = False

    @model_validator(mode="after")
    def explicit_pages(self):
        if len(set(self.page_numbers)) != len(self.page_numbers):
            raise ValueError("Duplicate pages are not an explicit set")
        if self.mode == "ocr" and self.engine is None:
            raise ValueError("OCR requires an engine from the workspace capabilities")
        if self.mode == "native" and self.engine is not None:
            raise ValueError("Native extraction does not select an OCR engine")
        return self


class RunOCR(StrictModel):
    version_ids: Ids
    engines: Annotated[list[Id], Field(min_length=1, max_length=4)]
    fusion_config_id: Id | None = None

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.version_ids)) != len(self.version_ids) or len(set(self.engines)) != len(self.engines):
            raise ValueError("Duplicate OCR inputs")
        return self


class InspectTable(StrictModel):
    result_id: Id
    revision: Revision
    table_id: Id
    action: Literal["read_checks", "generate_candidates"] = "read_checks"
    checks: Annotated[list[Literal["structure", "totals", "units", "rounding", "duplicates"]], Field(min_length=1, max_length=5)]


class RequestVisualReview(StrictModel):
    result_id: Id
    revision: Revision
    version_id: Id
    target_ids: Ids
    visual_model_id: Id


class GetJobStatus(StrictModel):
    jobs: Annotated[list[JobRef], Field(min_length=1, max_length=100)]


class RetryFailedJobs(GetJobStatus):
    previous_run_id: Id
    operation_id: Id
    action: Literal["retry", "resume"]


class ExportResults(StrictModel):
    source: Literal["explicit_results", "run_results"]
    results: Annotated[list[ResultRef], Field(min_length=1, max_length=1000)] | None = None
    source_run_id: Id | None = None
    format: Literal["xlsx", "txt", "md", "json", "pdf"]
    aggregate: bool = False
    confirmed_only: bool = False
    partial_policy: Literal["ask", "allow"] = "ask"

    @model_validator(mode="after")
    def frozen_source(self):
        if self.source == "explicit_results":
            if self.results is None or self.source_run_id is not None:
                raise ValueError("Explicit export requires only versioned results")
        elif self.source_run_id is None or self.results is not None:
            raise ValueError("Run export requires only a source run")
        return self


class NavigateToEvidence(StrictModel):
    evidence_ref_id: Id


class AskUser(StrictModel):
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    options: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=2, max_length=6)]
    evidence_ref_ids: Annotated[list[Id], Field(max_length=20)] = Field(default_factory=list)


TOOL_INPUTS: dict[str, type[StrictModel]] = {
    "get_workspace_context": GetWorkspaceContext,
    "search_document": SearchDocument,
    "read_page_result": ReadPageResult,
    "process_pages": ProcessPages,
    "run_ocr": RunOCR,
    "inspect_table": InspectTable,
    "request_visual_review": RequestVisualReview,
    "get_job_status": GetJobStatus,
    "retry_failed_jobs": RetryFailedJobs,
    "export_results": ExportResults,
    "navigate_to_evidence": NavigateToEvidence,
    "ask_user": AskUser,
}

ErrorCode = Literal[
    "invalid_arguments", "unknown_tool", "invalid_response", "truncated_response",
    "refusal", "authentication", "rate_limited", "provider_unavailable", "timeout",
    "response_unknown", "unsupported_capability", "scope_denied", "authorization_required",
    "stale_revision", "config_changed", "idempotency_conflict", "not_found",
    "budget_exceeded", "partial_results", "cancelled", "disk_full", "artifact_unavailable",
    "business_failed", "internal_error",
]


class ToolError(StrictModel):
    code: ErrorCode
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    retryable: bool
    required_action: Literal["none", "correct_arguments", "refresh", "configure", "decide", "retry_explicitly", "free_space"]


class ToolResult(StrictModel):
    status: Literal["success", "partial", "error", "needs_user"]
    summary: Annotated[str, Field(max_length=2000)]
    data: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    job_refs: list[JobLink] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error: ToolError | None = None
    truncated: bool = False
    next_cursor: Cursor | None = None

    @model_validator(mode="after")
    def bounded_final_result(self):
        if (self.status == "error") != (self.error is not None):
            raise ValueError("Only an error result carries a ToolError")
        if self.next_cursor is not None and not self.truncated:
            raise ValueError("A continuation cursor requires truncated=true")
        if self.status == "success" and any(j.state != "succeeded" for j in self.job_refs):
            raise ValueError("Queued, failed or unfinished jobs cannot report tool success")
        if len(self.model_dump_json().encode("utf-8")) > 16384:
            raise ValueError("Tool result exceeds the UTF-8 16 KiB wire budget")
        return self


RunStatus = Literal["queued", "running", "waiting_jobs", "waiting_user", "completed", "failed", "cancelled", "interrupted"]
RUN_TRANSITIONS = {
    "queued": ["running", "cancelled", "interrupted"],
    "running": ["waiting_jobs", "waiting_user", "completed", "failed", "cancelled", "interrupted"],
    "waiting_jobs": ["running", "waiting_user", "failed", "cancelled", "interrupted"],
    "waiting_user": ["queued", "cancelled", "interrupted"],
    "interrupted": ["queued", "cancelled"],
    "completed": [], "failed": [], "cancelled": [],
}


class RunState(StrictModel):
    run_id: Id
    session_id: Id
    status: RunStatus
    generation: Annotated[int, Field(ge=1)]
    outcome: Literal["success", "partial", "answered"] | None = None

    @model_validator(mode="after")
    def terminal_outcome(self):
        if (self.status == "completed") != (self.outcome is not None):
            raise ValueError("Only completed runs have an outcome")
        return self


class Event(StrictModel):
    session_id: Id
    run_id: Id | None
    seq: Annotated[int, Field(ge=1)]
    generation: Annotated[int, Field(ge=1)] | None
    type: Literal["message", "run_state", "tool_started", "tool_finished", "job_progress", "decision_required", "decision_resolved", "artifact_ready", "budget_updated", "error"]
    created: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$")]
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def run_generation(self):
        if (self.run_id is None) != (self.generation is None):
            raise ValueError("Run events must carry a generation")
        return self


class WriteRequest(StrictModel):
    client_request_id: Id


class CreateSession(WriteRequest):
    title: Annotated[str, Field(min_length=1, max_length=200)]


class UpdateSession(WriteRequest):
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    status: Literal["active", "archived"] | None = None


class SendMessage(WriteRequest):
    content: Annotated[str, Field(min_length=1, max_length=16000)]
    selection_token: Id | None = None


class CancelRun(WriteRequest):
    mode: Literal["stop_agent", "cancel_owned_jobs"]
    generation: Annotated[int, Field(ge=1)]


class ResumeRun(WriteRequest):
    generation: Annotated[int, Field(ge=1)]


class ReplyDecision(WriteRequest):
    payload_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    option_id: Id


class ProviderCall(StrictModel):
    call_id: Id
    tool: Id
    arguments: dict[str, JsonValue]


def validate_tool_arguments(tool: str, arguments: object) -> StrictModel:
    if tool not in TOOL_INPUTS:
        raise ValueError("unknown_tool")
    return TOOL_INPUTS[tool].model_validate(arguments)


def validate_call_batch(calls: list[dict], *, complete: bool) -> list[ProviderCall]:
    """Validation is all-or-nothing, before later P1/P2 dispatch/side effects."""
    if not complete:
        raise ValueError("truncated_response")
    if not 1 <= len(calls) <= 40:
        raise ValueError("invalid_response")
    parsed = [ProviderCall.model_validate(call) for call in calls]
    if len({call.call_id for call in parsed}) != len(parsed):
        raise ValueError("duplicate_call_id")
    for call in parsed:
        validate_tool_arguments(call.tool, call.arguments)
    return parsed


def validate_result_pairing(calls: list[ProviderCall], results: list[dict]) -> None:
    if [c.call_id for c in calls] != [r.get("call_id") for r in results]:
        raise ValueError("unpaired_tool_results")
    for result in results:
        if set(result) != {"call_id", "result"}:
            raise ValueError("invalid_tool_result_fields")
        ToolResult.model_validate(result["result"])


def export_contracts() -> dict:
    models = [ResultRef, EvidenceRef, JobLink, ArtifactRef, ToolError, ToolResult,
              RunState, Event, CreateSession, UpdateSession, SendMessage, CancelRun,
              ResumeRun, ReplyDecision, ProviderCall]
    return {
        "version": CONTRACT_VERSION, "implementation_scope": "P0 validation only; no dispatch or authorization engine",
        "tools": {name: {"version": 1, "input_schema": model.model_json_schema(),
                           "output_schema_ref": "ToolResult"} for name, model in TOOL_INPUTS.items()},
        "models": {model.__name__: model.model_json_schema() for model in models},
        "run_transitions": RUN_TRANSITIONS,
        "semantic_constraints": {
            "read_page_result": "run_result requires result_id linked to this run; adopted forbids result_id",
            "process_pages": "unique 1-based pages; OCR requires an available engine; native forbids engine",
            "export_results": "exactly one versioned results list or source_run_id; allow requires user scope grant",
            "inspect_table": "action=read_checks is read-only; generate_candidates is a separately authorized operation",
            "result": "error iff error payload; successful jobs are terminal succeeded; UTF-8 <=16384 bytes",
            "provider": "complete response; unique call IDs; ordered one result per call; private opaque blocks preserved by adapter",
        },
    }
