"""Audit version, identity, record and immutable workpaper counterexamples."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from packages.contracts import audit


ACTOR = {"id": "localA", "display_name": "Local auditor", "source": "local"}
UTC = "2026-10-03T00:00:00Z"
PAYLOAD_HASH = "a" * 64


def record(role):
    return {
        "record_id": "recordPrepared" if role == "prepared" else "recordReviewed",
        "entity_kind": "project",
        "entity_id": "projectA",
        "content_revision": 2,
        "project_context_revision": 1,
        "project_revision": 3 if role == "prepared" else 4,
        "role": role,
        "actor": dict(ACTOR),
        "note": "Human declaration",
        "created_at": UTC if role == "prepared" else "2026-10-03T00:01:00Z",
    }


def project():
    return {
        "schema_version": "1.0",
        "audit_version": "1.0",
        "project_id": "projectA",
        "workspace_id": "workspaceA",
        "revision": 4,
        "content_revision": 2,
        "project_context_revision": 1,
        "project": {
            "title": "Audit",
            "audit_object": "Ledger",
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
            "objective": "Check recorded amounts",
        },
        "procedure_count": 1,
        "finding_count": 1,
        "records": [record("prepared"), record("reviewed")],
        "records_current": True,
        "created_at": UTC,
        "updated_at": "2026-10-03T00:01:00Z",
    }


def receipt():
    return {
        "schema_version": "1.0",
        "audit_version": "1.0",
        "request_id": "requestA",
        "project_id": "projectA",
        "project_revision": 4,
        "entity_kind": "project",
        "entity_id": "projectA",
        "payload_hash": PAYLOAD_HASH,
        "actor": dict(ACTOR),
        "created_at": UTC,
    }


def request_status(state="committed"):
    return {
        "schema_version": "1.0",
        "audit_version": "1.0",
        "request_id": "requestA",
        "project_id": "projectA",
        "payload_hash": PAYLOAD_HASH,
        "state": state,
        "receipt": receipt() if state == "committed" else None,
    }


def file(name, artifact_id, media_type):
    return {
        "name": name,
        "artifact_id": artifact_id,
        "revision": 1,
        "media_type": media_type,
        "size_bytes": 7,
        "availability": "available",
        "blob_hash": "b" * 64,
    }


def review_ref():
    return {
        "bundle_id": "reviewA",
        "batch_id": "batchA",
        "batch_revision": 2,
        "snapshot_hash": "c" * 64,
        "results_xlsx": file(
            "results.xlsx", "resultsA",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }


def workpaper():
    return {
        "schema_version": "1.0",
        "audit_version": "1.0",
        "workpaper_id": "paperA",
        "bundle_id": "paperA",
        "workspace_id": "workspaceA",
        "project_id": "projectA",
        "project_revision": 4,
        "kind": "audit_workpaper",
        "delivery_kind": "final",
        "status": "current",
        "request_id": "requestA",
        "payload_hash": PAYLOAD_HASH,
        "snapshot_hash": "d" * 64,
        "actor": dict(ACTOR),
        "title": "Fixed workpaper",
        "created_at": UTC,
        "predecessor_workpaper_id": None,
        "successor_workpaper_ids": [],
        "successor_count": 0,
        "files": [
            file("workpaper.md", "markdownA", "text/markdown"),
            file("workpaper.json", "jsonA", "application/json"),
        ],
        "review_refs": [review_ref()],
        "unresolved_count": 0,
        "unresolved_reasons": [],
    }


def evidence_value(kind, raw, value):
    return {"role": "left", "kind": kind, "raw": raw, "value": value,
            "source": None, "row_id": None, "table_revision": None}


def test_current_record_pair_is_retained():
    value = project()
    parsed = audit.AuditProject.model_validate(value)
    assert [item.role for item in parsed.records] == ["prepared", "reviewed"]
    assert parsed.records_current is True


@pytest.mark.parametrize("case", [
    "missing_role", "duplicate_role", "foreign_entity", "stale_content",
    "stale_context", "future_project_revision", "review_before_preparation",
    "invalid_nested_utc",
])
def test_current_records_reject_inconsistent_declarations(case):
    value = project()
    if case == "missing_role":
        value["records"] = value["records"][:1]
    elif case == "duplicate_role":
        value["records"][1]["role"] = "prepared"
    elif case == "foreign_entity":
        value["records"][1]["entity_id"] = "projectB"
    elif case == "stale_content":
        value["records"][1]["content_revision"] = 1
    elif case == "stale_context":
        value["records"][1]["project_context_revision"] = 2
    elif case == "future_project_revision":
        value["records"][1]["project_revision"] = 5
    elif case == "review_before_preparation":
        value["records"][1]["created_at"] = "2026-10-02T23:59:59Z"
    elif case == "invalid_nested_utc":
        value["records"][1]["created_at"] = "2026-02-30T00:00:00Z"
    with pytest.raises(ValidationError):
        audit.AuditProject.model_validate(value)


def test_noncurrent_records_preserve_old_content_identity():
    value = project()
    value["records_current"] = False
    for item in value["records"]:
        item["content_revision"] = 1
    parsed = audit.AuditProject.model_validate(value)
    assert parsed.records_current is False
    assert all(item.content_revision == 1 for item in parsed.records)


def test_top_level_utc_rejects_nonexistent_date():
    value = project()
    value["updated_at"] = "2026-13-01T00:00:00Z"
    with pytest.raises(ValidationError):
        audit.AuditProject.model_validate(value)


def test_committed_request_returns_original_receipt_identity():
    value = request_status()
    parsed = audit.AuditRequestStatus.model_validate(value)
    assert parsed.receipt.model_dump() == value["receipt"]


@pytest.mark.parametrize("case", ["missing", "request_id", "project_id", "payload_hash"])
def test_committed_request_rejects_missing_or_other_receipt(case):
    value = request_status()
    if case == "missing":
        value["receipt"] = None
    else:
        value["receipt"][case] = "f" * 64 if case == "payload_hash" else "otherA"
    with pytest.raises(ValidationError):
        audit.AuditRequestStatus.model_validate(value)


@pytest.mark.parametrize("state", ["publishing", "needs_verification"])
def test_pending_request_has_no_ready_receipt(state):
    assert audit.AuditRequestStatus.model_validate(request_status(state)).receipt is None


@pytest.mark.parametrize("state", ["publishing", "needs_verification"])
def test_pending_request_rejects_ready_receipt(state):
    value = request_status(state)
    value["receipt"] = receipt()
    with pytest.raises(ValidationError):
        audit.AuditRequestStatus.model_validate(value)


def test_review_reference_preserves_canonical_results_identity():
    value = review_ref()
    assert audit.WorkpaperReviewRef.model_validate(value).model_dump() == value


def test_review_reference_rejects_other_filename():
    value = review_ref()
    value["results_xlsx"]["name"] = "other.xlsx"
    with pytest.raises(ValidationError):
        audit.WorkpaperReviewRef.model_validate(value)


def test_ready_workpaper_preserves_both_fixed_formats_and_single_id():
    value = workpaper()
    parsed = audit.WorkpaperBundle.model_validate(value)
    assert parsed.model_dump() == value
    assert parsed.bundle_id == parsed.workpaper_id


@pytest.mark.parametrize("case", ["predecessor_self", "successor_self", "duplicate_successor", "preview_exceeds_count"])
def test_workpaper_rejects_inconsistent_version_links(case):
    value = workpaper()
    if case == "predecessor_self":
        value["predecessor_workpaper_id"] = value["workpaper_id"]
    elif case == "successor_self":
        value["successor_workpaper_ids"] = [value["workpaper_id"]]
        value["successor_count"] = 1
    elif case == "duplicate_successor":
        value["successor_workpaper_ids"] = ["paperB", "paperB"]
        value["successor_count"] = 2
    elif case == "preview_exceeds_count":
        value["successor_workpaper_ids"] = ["paperB"]
        value["successor_count"] = 0
    with pytest.raises(ValidationError):
        audit.WorkpaperBundle.model_validate(value)


@pytest.mark.parametrize("kind,raw,value", [
    ("integer", "00042", "00042"),
    ("integer", "90071992547409931234567890", "90071992547409931234567890"),
    ("decimal", "=A1+B1", "1.2300"),
])
def test_numeric_evidence_preserves_literal_text(kind, raw, value):
    source = evidence_value(kind, raw, value)
    assert audit.EvidenceValue.model_validate(source).model_dump() == source


@pytest.mark.parametrize("kind,raw,value", [
    ("integer", True, True),
    ("decimal", "1.25", 1.25),
    ("decimal", "NaN", "NaN"),
    ("decimal", "Infinity", "Infinity"),
    ("integer", "1.2", "1.2"),
    ("unknown_formula", "=A1+B1", "1.23"),
])
def test_evidence_rejects_lossy_nonfinite_or_inconsistent_literals(kind, raw, value):
    with pytest.raises(ValidationError):
        audit.EvidenceValue.model_validate(evidence_value(kind, raw, value))


@pytest.mark.parametrize("case", ["missing", "different"])
def test_artifact_center_identity_is_required_and_identical(case):
    value = deepcopy(workpaper())
    if case == "missing":
        del value["bundle_id"]
    else:
        value["bundle_id"] = "paperB"
    with pytest.raises(ValidationError):
        audit.WorkpaperBundle.model_validate(value)
