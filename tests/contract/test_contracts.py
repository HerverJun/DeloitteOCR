"""Contract artifacts, wire payloads and state-dependent protocol guarantees."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.contracts.examples import H, H2, POSITIVE
from packages.contracts.generate import NAMES, artifacts, components, openapi, render_types, ts_type
from packages.contracts.models import MODELS, AssetSet, HandoffRequest, JobRef, OpenContext, PageQuery
from packages.contracts.semantics import (
    ContractError, HandoffLedger, check_asset_set, check_context, check_control,
    check_control_sequence, check_grant_action, check_manifest_capability, check_revision,
    check_snapshot, public_validation_error,
)

ROOT = Path(__file__).resolve().parents[2] / "packages" / "contracts"
BY_NAME = {model.__name__: model for model in MODELS}


@pytest.mark.parametrize("name", NAMES)
def test_public_positive_wire_examples(name):
    example = json.loads((ROOT / "examples" / "positive.json").read_text(encoding="utf-8"))[name]
    assert BY_NAME[name].model_validate_json(json.dumps(example)).model_dump(exclude_none=True) == example


@pytest.mark.parametrize("name", sorted(json.loads((ROOT / "examples" / "negative.json").read_text(encoding="utf-8"))))
def test_public_negative_wire_examples(name):
    example = json.loads((ROOT / "examples" / "negative.json").read_text(encoding="utf-8"))[name]
    with pytest.raises(ValidationError):
        BY_NAME[example["model"]].model_validate_json(json.dumps(example["payload"]))


def test_generated_artifacts_are_exact_and_all_refs_resolve():
    for path, expected in artifacts().items():
        assert path.read_text(encoding="utf-8") == expected, f"stale generated contract: {path.name}"
    defs = components()
    spec = json.loads((ROOT / "adapter.openapi.json").read_text(encoding="utf-8"))
    assert spec == openapi(defs)
    assert all(name in defs for name in NAMES)
    assert (ROOT / "types.generated.ts").read_text(encoding="utf-8") == render_types(defs)
    query_schema = PageQuery.model_json_schema()["properties"]
    for name in ("limit", "cursor", "snapshot_revision"):
        parameter = next(p for p in spec["paths"]["/api/platform/v1/apps"]["get"]["parameters"] if p["name"] == name)
        expected = query_schema[name].copy()
        expected.pop("title", None)
        expected.pop("default", None)
        if "anyOf" in expected:
            expected = next(part for part in expected["anyOf"] if part.get("type") != "null")
        assert parameter["schema"] == expected

    def refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/components/schemas/")
                assert value["$ref"].rsplit("/", 1)[1] in defs
            for child in value.values():
                refs(child)
        elif isinstance(value, list):
            for child in value:
                refs(child)

    refs(spec)
    for name in NAMES:
        schema = json.loads((ROOT / f"{NAMES[name]}.schema.json").read_text(encoding="utf-8"))
        assert schema == BY_NAME[name].model_json_schema()
        assert schema["properties"]["schema_version"]["const"] == "1.0"
        assert "schema_version" in schema["required"]
        assert schema["additionalProperties"] is False


def test_typescript_generator_rejects_unrepresented_constructs():
    with pytest.raises(ValueError, match="unsupported"):
        ts_type({"oneOf": [{"type": "string"}]})
    with pytest.raises(ValueError, match="unsupported"):
        ts_type({"type": "array"})


def test_context_access_and_expiry_are_state_checks():
    context = OpenContext.model_validate(POSITIVE["OpenContext"])
    kwargs = dict(expected_app_id="ocr", expected_instance_id="instanceA",
                  accessible_link_ids={"linkA"}, accessible_asset_set_ids={"setA"},
                  accessible_artifact_ids=set(), now=datetime(2026, 9, 25, 1, 30, tzinfo=timezone.utc))
    check_context(context, **kwargs)
    with pytest.raises(ContractError, match="not accessible") as exc:
        check_context(context, **(kwargs | {"accessible_link_ids": set()}))
    assert exc.value.code == "VALIDATION_FAILED"
    with pytest.raises(ContractError) as exc:
        check_context(context, **(kwargs | {"now": datetime(2026, 9, 25, 2, tzinfo=timezone.utc)}))
    assert exc.value.code == "CONTEXT_EXPIRED"


def test_revision_manifest_and_capability_checks():
    handoff = HandoffRequest.model_validate(POSITIVE["HandoffRequest"])
    asset_set = AssetSet.model_validate(POSITIVE["AssetSet"])
    check_asset_set(handoff, asset_set)
    check_revision(supplied=2, persisted=2)
    with pytest.raises(ContractError) as exc:
        check_revision(supplied=1, persisted=2)
    assert exc.value.code == "REVISION_CONFLICT"
    changed = asset_set.model_copy(update={"manifest_hash": H2})
    with pytest.raises(ContractError) as exc:
        check_asset_set(handoff, changed)
    assert exc.value.code == "INPUT_CHANGED"
    job = JobRef.model_validate(POSITIVE["JobRef"])
    check_control(job, "cancel")
    check_control_sequence(job, 4)
    with pytest.raises(ContractError) as exc:
        check_control_sequence(job, 3)
    assert exc.value.code == "REVISION_CONFLICT"
    for operation in ("pause", "resume", "retry_failed", "unknown"):
        with pytest.raises(ContractError) as exc:
            check_control(job, operation)
        assert exc.value.code == "CAPABILITY_UNSUPPORTED"
    from packages.contracts.models import AppManifest
    manifest = AppManifest.model_validate(POSITIVE["AppManifest"])
    with pytest.raises(ContractError) as exc:
        check_manifest_capability(manifest, "resume")
    assert exc.value.code == "CAPABILITY_UNSUPPORTED"


def test_idempotency_same_payload_conflict_and_not_found():
    request = HandoffRequest.model_validate(POSITIVE["HandoffRequest"])
    ledger = HandoffLedger()
    assert ledger.accept(request, "operationA") == "operationA"
    assert ledger.accept(request, "otherOperation") == "operationA"
    assert ledger.lookup("requestA") == "operationA"
    changed = request.model_copy(update={"manifest_hash": H2})
    with pytest.raises(ContractError) as exc:
        ledger.accept(changed, "otherOperation")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(ContractError) as exc:
        ledger.lookup("neverSeen")
    assert exc.value.code == "NOT_FOUND"


def test_stale_grant_requires_reconciliation():
    from packages.contracts.models import ResourceGrant
    grant = ResourceGrant.model_validate(POSITIVE["ResourceGrant"])
    check_grant_action(grant, persisted_generation=2, persisted_owner_instance_id="instanceA", owner_process_alive=True)
    with pytest.raises(ContractError) as exc:
        check_grant_action(grant, persisted_generation=3, persisted_owner_instance_id="instanceA", owner_process_alive=True)
    assert exc.value.code == "REVISION_CONFLICT"
    with pytest.raises(ContractError) as exc:
        check_grant_action(grant, persisted_generation=2, persisted_owner_instance_id="instanceA", owner_process_alive=False)
    assert exc.value.code == "RESOURCE_BUSY"


def test_public_validation_error_does_not_echo_private_input():
    payload = deepcopy(POSITIVE["OpenContext"])
    payload["return_route"] = "C:/private/secret-key-123"
    with pytest.raises(ValidationError) as exc:
        OpenContext.model_validate(payload)
    assert "secret-key-123" in str(exc.value)  # never serialize this exception to the API
    output = public_validation_error("requestA").model_dump_json()
    assert "secret-key-123" not in output
    assert "C:/" not in output
    assert json.loads(output)["details"] == {}


def test_locators_use_one_based_pdf_tables_and_original_sheet_rows():
    from packages.contracts.models import SourceLocator
    pdf = {**POSITIVE["SourceLocator"], "kind": "cell", "page_number": 2,
           "table_number": 1, "row_number": 3, "column_number": 4, "precision": "exact"}
    SourceLocator.model_validate_json(json.dumps(pdf))
    sheet = {**pdf, "page_number": None, "sheet_name": "Data", "row_number": 19}
    SourceLocator.model_validate_json(json.dumps(sheet))
    with pytest.raises(ValidationError):
        SourceLocator.model_validate_json(json.dumps(sheet | {"page_number": 2}))
    with pytest.raises(ValidationError):
        SourceLocator.model_validate(pdf | {"kind": "region", "image_version_id": "imgA",
                                      "coordinate_space": "normalized", "transform_revision": 1,
                                      "box": (0, 0, float("inf"), 1)})


def test_snapshot_pagination_and_partial_handoff_recovery():
    from packages.contracts.models import AppManifestPage, HandoffStatus, PageQuery
    query = PageQuery.model_validate(POSITIVE["PageQuery"])
    check_snapshot(supplied=query.snapshot_revision, persisted=2)
    with pytest.raises(ContractError) as exc:
        check_snapshot(supplied=query.snapshot_revision, persisted=3)
    assert exc.value.code == "SNAPSHOT_EXPIRED"
    page = AppManifestPage.model_validate({"schema_version": "1.0", "items": [POSITIVE["AppManifest"]],
                                           "next_cursor": "cursorB", "snapshot_revision": 2})
    assert page.items[0].app_id == "ocr"
    partial = HandoffStatus.model_validate(POSITIVE["HandoffStatus"])
    assert [item.status for item in partial.items] == ["imported", "failed"]
    with pytest.raises(ValidationError):
        HandoffStatus.model_validate({**POSITIVE["HandoffStatus"], "state": "completed"})


def test_decimal_wire_and_large_safe_file_size():
    from packages.contracts.models import Asset, DecimalValue
    amount = DecimalValue.model_validate_json(json.dumps(POSITIVE["DecimalValue"]))
    assert amount.value == "123456789012.123456"
    asset = Asset.model_validate(POSITIVE["Asset"])
    assert asset.size_bytes == 2_000_000_000
    with pytest.raises(ValidationError):
        Asset.model_validate(POSITIVE["Asset"] | {"size_bytes": 9_007_199_254_740_992})
