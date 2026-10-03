"""Checks requiring trusted, persisted state, after structural model validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import AppManifest, AssetSet, Error, HandoffRequest, JobRef, OpenContext, ResourceGrant, VERSION


class ContractError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def public_error(self, request_id: str) -> Error:
        # Never include Pydantic input_value, source text, credentials or paths.
        return Error(schema_version=VERSION, code=self.code, message=str(self), retryable=False,
                     request_id=request_id, details={})


def public_validation_error(request_id: str) -> Error:
    """Return a fixed message: ValidationError strings contain untrusted input."""
    return Error(schema_version=VERSION, code="VALIDATION_FAILED", message="Invalid contract payload",
                 retryable=False, request_id=request_id, details={})


def check_context(context: OpenContext, *, expected_app_id: str, expected_instance_id: str,
                  accessible_link_ids: set[str], accessible_asset_set_ids: set[str],
                  accessible_artifact_ids: set[str], now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if datetime.fromisoformat(context.expires_at.replace("Z", "+00:00")) <= now:
        raise ContractError("CONTEXT_EXPIRED", "Open context has expired")
    if context.app_id != expected_app_id or context.instance_id != expected_instance_id:
        raise ContractError("VALIDATION_FAILED", "Application identity does not match")
    if context.link_id not in accessible_link_ids or (context.asset_set_id is not None and context.asset_set_id not in accessible_asset_set_ids) or (context.artifact_id is not None and context.artifact_id not in accessible_artifact_ids):
        raise ContractError("VALIDATION_FAILED", "Context object is not accessible")


def check_control(job: JobRef, operation: str) -> None:
    if operation not in {"cancel", "pause", "resume", "retry_failed"} or operation not in job.capabilities:
        raise ContractError("CAPABILITY_UNSUPPORTED", "Operation is not supported for this job")
    allowed = {"cancel": {"queued", "waiting_resource", "running", "pausing", "paused"},
               "pause": {"running"}, "resume": {"paused"}, "retry_failed": {"partial", "failed"}}
    if job.status not in allowed[operation]:
        raise ContractError("CAPABILITY_UNSUPPORTED", "Operation is not available in this state")


def check_control_sequence(job: JobRef, expected_event_seq: int) -> None:
    if expected_event_seq != job.last_event_seq:
        raise ContractError("REVISION_CONFLICT", "Job event sequence has changed")


def check_revision(*, supplied: int, persisted: int) -> None:
    if supplied != persisted:
        raise ContractError("REVISION_CONFLICT", "Object revision has changed")


def check_review_completion(coverage, *, execution_state: str,
                            pending_operations: int = 0) -> None:
    """Final review requires durable complete coverage and resolved human work."""
    from .automation import BatchCoverage
    value = BatchCoverage.model_validate(coverage)
    if (execution_state != 'succeeded' or not value.index_complete or
            value.pending or value.deferred or pending_operations):
        raise ContractError('REVIEW_INCOMPLETE', 'Review has unresolved coverage, items or operations')


def check_snapshot(*, supplied: int | None, persisted: int) -> None:
    if supplied is not None and supplied != persisted:
        raise ContractError("SNAPSHOT_EXPIRED", "List snapshot is no longer available")


def check_asset_set(request: HandoffRequest, asset_set: AssetSet) -> None:
    if request.asset_set_id != asset_set.asset_set_id or request.asset_set_revision != asset_set.revision:
        raise ContractError("REVISION_CONFLICT", "Asset set revision has changed")
    if request.manifest_hash != asset_set.manifest_hash:
        raise ContractError("INPUT_CHANGED", "Asset set manifest has changed")


def semantic_payload_hash(request: HandoffRequest) -> str:
    """Exclude request_id and transport metadata; include all business parameters."""
    value = request.model_dump(exclude={"request_id"})
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class HandoffLedger:
    """Small reference ledger for adapter conformance; production persistence is T026."""

    entries: dict[str, tuple[str, str]] = field(default_factory=dict)

    def accept(self, request: HandoffRequest, operation_id: str) -> str:
        digest = semantic_payload_hash(request)
        prior = self.entries.get(request.request_id)
        if prior:
            if prior[0] != digest:
                raise ContractError("IDEMPOTENCY_CONFLICT", "Request ID belongs to another payload")
            return prior[1]
        self.entries[request.request_id] = (digest, operation_id)
        return operation_id

    def lookup(self, request_id: str) -> str:
        if request_id not in self.entries:
            raise ContractError("NOT_FOUND", "Request ID was not accepted")
        return self.entries[request_id][1]


def check_grant_action(grant: ResourceGrant, *, persisted_generation: int,
                       persisted_owner_instance_id: str, owner_process_alive: bool) -> None:
    if grant.generation != persisted_generation or grant.owner_instance_id != persisted_owner_instance_id:
        raise ContractError("REVISION_CONFLICT", "Stale resource generation")
    if not owner_process_alive:
        raise ContractError("RESOURCE_BUSY", "Resource owner requires reconciliation")


def check_manifest_capability(manifest: AppManifest, required: str) -> None:
    if required not in manifest.capabilities:
        raise ContractError("CAPABILITY_UNSUPPORTED", "Application does not declare this capability")
