"""Version 1 wire models. This module is the source of generated contract artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VERSION = "1.0"
Id = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$", max_length=64)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=256)]
Utc = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Bytes = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]
Cursor = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,256}$", max_length=256)]


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]

    @field_validator("schema_version")
    @classmethod
    def known_version(cls, value: str) -> str:
        if value != VERSION:
            raise ValueError("unsupported schema version")
        return value


class Timestamped(Wire):
    @field_validator("created_at", "updated_at", "expires_at", "granted_at", "heartbeat_at", "lease_until", "creation_time", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid UTC timestamp") from exc
        if parsed.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return value


class AppManifest(Wire):
    app_id: Id
    display_name: ShortText
    app_version: ShortText
    adapter_version: ShortText
    min_platform_version: ShortText
    data_schema_version: ShortText
    entry_mode: Literal["standalone_page"]
    capabilities: list[Literal["cancel", "pause", "resume", "retry_failed", "import", "export", "gpu"]] = Field(max_length=16)
    input_types: list[ShortText] = Field(max_length=64)
    output_types: list[ShortText] = Field(max_length=64)
    resources: list[Literal["cpu", "gpu", "network"]] = Field(max_length=8)
    license_refs: list[ShortText] = Field(max_length=64)


class Health(Wire):
    state: Literal["ready", "unavailable", "disabled", "unknown"]
    reason: str = Field(max_length=512)


class AppHealth(Wire):
    service: Health
    ui: Health
    cpu: Health
    gpu: Health
    models: Health


class LaunchReceipt(Timestamped):
    launch_id: Id
    app_id: Id
    instance_id: Id
    pid: int = Field(strict=True, gt=0)
    creation_time: Utc
    base_url: Annotated[str, Field(pattern=r"^http://127\.0\.0\.1:[0-9]{1,5}/?$", max_length=40)]
    handshake_nonce: Id
    ownership: Literal["owned", "discovered"]
    version: ShortText


class Workspace(Timestamped):
    workspace_id: Id
    name: ShortText
    revision: Revision
    created_at: Utc
    updated_at: Utc


class ModuleProjectLink(Wire):
    workspace_id: Id
    app_id: Id
    module_project_id: Id
    link_id: Id
    role: Literal["owner", "editor", "viewer"]
    revision: Revision


class OpenContext(Timestamped):
    app_id: Id
    instance_id: Id
    link_id: Id
    asset_set_id: Id | None = None
    artifact_id: Id | None = None
    nonce: Id
    expires_at: Utc
    return_route: Annotated[str, Field(pattern=r"^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$", max_length=257)]


class ContextIssueRequest(Wire):
    app_id: Id
    instance_id: Id
    link_id: Id
    asset_set_id: Id | None = None
    artifact_id: Id | None = None
    return_route: Annotated[str, Field(pattern=r"^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$", max_length=257)]


class Asset(Timestamped):
    asset_id: Id
    workspace_id: Id
    source_id: Id
    original_name: ShortText
    media_type: ShortText
    storage_mode: Literal["managed", "referenced"]
    blob_hash: Hash
    size_bytes: Bytes
    revision: Revision
    created_at: Utc


class SourceLocator(Wire):
    kind: Literal["file", "page", "table", "cell", "row", "region"]
    asset_id: Id
    asset_revision: Revision
    blob_hash: Hash
    page_number: int | None = Field(default=None, strict=True, ge=1)
    sheet_name: ShortText | None = None
    table_number: int | None = Field(default=None, strict=True, ge=1)
    row_number: int | None = Field(default=None, strict=True, ge=1)
    column_number: int | None = Field(default=None, strict=True, ge=1)
    image_version_id: Id | None = None
    coordinate_space: Literal["image_pixels", "normalized"] | None = None
    transform_revision: Revision | None = None
    precision: Literal["exact", "approximate", "page_only", "unknown"]
    box: tuple[Annotated[float, Field(allow_inf_nan=False)], Annotated[float, Field(allow_inf_nan=False)],
               Annotated[float, Field(allow_inf_nan=False)], Annotated[float, Field(allow_inf_nan=False)]] | None = None

    @model_validator(mode="after")
    def locate(self) -> SourceLocator:
        if self.kind in {"page", "region"} and self.page_number is None:
            raise ValueError("page number required")
        if self.kind in {"table", "cell", "row"} and (self.sheet_name is None) == (self.page_number is None):
            raise ValueError("table source requires exactly one sheet or PDF page")
        if self.kind in {"table", "cell", "row"} and self.table_number is None:
            raise ValueError("1-based table index required")
        if self.kind in {"row", "cell"} and self.row_number is None:
            raise ValueError("source row number required")
        if self.kind == "cell" and self.column_number is None:
            raise ValueError("source column number required")
        if self.box is not None:
            if self.kind != "region" or self.precision not in {"exact", "approximate"} or not all((self.image_version_id, self.coordinate_space, self.transform_revision)):
                raise ValueError("box requires region and image transform identity")
            x1, y1, x2, y2 = self.box
            if not 0 <= x1 < x2 or not 0 <= y1 < y2:
                raise ValueError("invalid box")
            if self.coordinate_space == "normalized" and (x2 > 1 or y2 > 1):
                raise ValueError("normalized box exceeds unit space")
        if self.precision in {"exact", "approximate"} and self.kind == "region" and self.box is None:
            raise ValueError("region precision requires transform and box")
        if self.box is None and any((self.image_version_id, self.coordinate_space, self.transform_revision)):
            raise ValueError("coordinate identity without reliable box")
        if self.precision == "page_only" and self.page_number is None:
            raise ValueError("page-only precision requires page")
        return self


class AssetSetItem(Wire):
    item_id: Id
    asset_id: Id
    asset_revision: Revision
    blob_hash: Hash
    source_locator: SourceLocator

    @model_validator(mode="after")
    def identity(self) -> AssetSetItem:
        locator = self.source_locator
        if (self.asset_id, self.asset_revision, self.blob_hash) != (locator.asset_id, locator.asset_revision, locator.blob_hash):
            raise ValueError("locator identity differs from item")
        return self


class AssetSet(Wire):
    asset_set_id: Id
    revision: Revision
    manifest_hash: Hash
    items: list[AssetSetItem] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique_items(self) -> AssetSet:
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate item_id")
        return self


class PageQuery(Wire):
    limit: int = Field(strict=True, ge=1, le=200)
    cursor: Cursor | None = None
    snapshot_revision: Revision | None = None


class PageBase(Wire):
    next_cursor: Cursor | None
    snapshot_revision: Revision


class AppManifestPage(PageBase):
    items: list[AppManifest] = Field(max_length=200)


class AssetPage(PageBase):
    items: list[Asset] = Field(max_length=200)


class Counts(Wire):
    total: Count | None
    succeeded: Count
    failed: Count
    cancelled: Count
    pending: Count

    @model_validator(mode="after")
    def coverage(self) -> Counts:
        if self.total is not None and sum((self.succeeded, self.failed, self.cancelled, self.pending)) != self.total:
            raise ValueError("counts do not cover immutable total")
        return self


class JobRef(Timestamped):
    platform_job_id: Id
    app_id: Id
    instance_id: Id
    native_job_id: Id
    operation_id: Id
    input_manifest_hash: Hash
    native_status: ShortText
    status: Literal["queued", "waiting_resource", "running", "pausing", "paused", "cancelling", "cancelled", "succeeded", "partial", "failed", "unknown"]
    stage: ShortText
    counts: Counts
    capabilities: list[Literal["cancel", "pause", "resume", "retry_failed"]] = Field(max_length=4)
    last_event_seq: Count
    updated_at: Utc

    @model_validator(mode="after")
    def terminal_counts(self) -> JobRef:
        if self.status in {"succeeded", "partial", "failed", "cancelled"} and self.counts.pending:
            raise ValueError("terminal job has pending items")
        if self.status == "succeeded" and (self.counts.failed or self.counts.cancelled):
            raise ValueError("succeeded job has unsuccessful items")
        if self.status == "partial" and not (self.counts.succeeded and (self.counts.failed or self.counts.cancelled)):
            raise ValueError("partial requires successful and unsuccessful items")
        return self


class JobPage(PageBase):
    items: list[JobRef] = Field(max_length=200)


class JobControlRequest(Wire):
    operation: Literal["cancel", "pause", "resume", "retry_failed"]
    expected_event_seq: Count


class Artifact(Timestamped):
    artifact_id: Id
    workspace_id: Id
    job_ref: JobRef
    revision: Revision
    media_type: ShortText
    blob_hash: Hash
    size_bytes: Bytes
    input_manifest_hash: Hash
    application_version: ShortText
    rule_or_model_version: ShortText
    created_at: Utc
    availability: Literal["available", "expired", "missing", "corrupt"]
    source_refs: list[SourceLocator] = Field(max_length=10000)

    @model_validator(mode="after")
    def input_identity(self) -> Artifact:
        if self.input_manifest_hash != self.job_ref.input_manifest_hash:
            raise ValueError("artifact and job input differ")
        return self


class ImportOptions(Wire):
    mode: Literal["copy", "reference"]
    duplicate_policy: Literal["skip", "keep_separate"]


class HandoffRequest(Wire):
    request_id: Id
    target_link_id: Id
    asset_set_id: Id
    asset_set_revision: Revision
    manifest_hash: Hash
    import_options: ImportOptions


class HandoffItem(Wire):
    item_id: Id
    status: Literal["pending", "imported", "failed"]
    target_asset_id: Id | None = None
    error_code: Literal["INPUT_CHANGED", "INPUT_MISSING", "VALIDATION_FAILED"] | None = None

    @model_validator(mode="after")
    def status_identity(self) -> HandoffItem:
        if self.status == "imported" and (self.target_asset_id is None or self.error_code is not None):
            raise ValueError("imported item requires target identity and no error")
        if self.status == "failed" and (self.error_code is None or self.target_asset_id is not None):
            raise ValueError("failed item requires error and no target identity")
        if self.status == "pending" and (self.error_code is not None or self.target_asset_id is not None):
            raise ValueError("pending item cannot claim result")
        return self


class HandoffStatus(Wire):
    request_id: Id
    operation_id: Id
    state: Literal["prepared", "submitting", "accepted", "reconciling", "completed", "partial", "failed", "unknown"]
    items: list[HandoffItem] = Field(max_length=10000)

    @model_validator(mode="after")
    def completeness(self) -> HandoffStatus:
        statuses = [item.status for item in self.items]
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate handoff item")
        if self.state == "completed" and any(s != "imported" for s in statuses):
            raise ValueError("completed handoff has incomplete items")
        if self.state == "partial" and not ("imported" in statuses and "failed" in statuses):
            raise ValueError("partial handoff requires imported and failed items")
        if self.state in {"completed", "partial", "failed"} and "pending" in statuses:
            raise ValueError("terminal handoff has pending items")
        if self.state == "failed" and "imported" in statuses:
            raise ValueError("failed handoff with imports is partial")
        return self


class ResourceGrant(Timestamped):
    resource_id: Id
    owner_instance_id: Id
    job_id: Id
    generation: Revision
    granted_at: Utc
    heartbeat_at: Utc
    lease_until: Utc
    managed_process_identity: Id

    @model_validator(mode="after")
    def time_order(self) -> ResourceGrant:
        times = [datetime.fromisoformat(getattr(self, key).replace("Z", "+00:00")) for key in ("granted_at", "heartbeat_at", "lease_until")]
        if times != sorted(times):
            raise ValueError("invalid grant time order")
        return self


class Error(Wire):
    code: Literal["APP_UNAVAILABLE", "AUTH_EXPIRED", "CONTEXT_EXPIRED", "REVISION_CONFLICT", "INPUT_CHANGED", "INPUT_MISSING", "ARTIFACT_MISSING", "IDEMPOTENCY_CONFLICT", "RESOURCE_BUSY", "SUBMISSION_UNKNOWN", "CAPABILITY_UNSUPPORTED", "CANCEL_REQUESTED", "OUTPUT_INCOMPLETE", "INTEGRITY_MISMATCH", "VALIDATION_FAILED", "NOT_FOUND", "SNAPSHOT_EXPIRED"]
    message: ShortText
    retryable: bool
    request_id: Id
    details: dict[Id, ShortText] = Field(max_length=16)


class DecimalValue(Wire):
    value: Annotated[str, Field(pattern=r"^-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,6})?$", max_length=26)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

    @field_validator("value")
    @classmethod
    def decimal_not_float(cls, value: str) -> str:
        try:
            Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal") from exc
        return value


MODELS = (AppManifest, Health, AppHealth, LaunchReceipt, Workspace, ModuleProjectLink, OpenContext, ContextIssueRequest,
          Asset, SourceLocator, AssetSetItem, AssetSet, PageQuery, PageBase, AppManifestPage, AssetPage,
          Counts, JobRef, JobPage, Artifact, ImportOptions,
          JobControlRequest, HandoffRequest, HandoffItem, HandoffStatus, ResourceGrant, Error, DecimalValue)
