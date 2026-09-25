"""Public synthetic wire examples; no user files, credentials or machine paths."""

from __future__ import annotations

from copy import deepcopy

H = "a" * 64
H2 = "b" * 64
T = "2026-09-25T01:00:00Z"
V = {"schema_version": "1.0"}
LOCATOR = {**V, "kind": "page", "asset_id": "assetA", "asset_revision": 1,
           "blob_hash": H, "page_number": 1, "precision": "page_only"}
COUNTS = {**V, "total": 3, "succeeded": 1, "failed": 0, "cancelled": 0, "pending": 2}
OCR_NATIVE_ID = "9" + "a" * 31  # OCR's uuid4().hex may start with a digit.
JOB = {**V, "platform_job_id": "jobA", "app_id": "ocr", "instance_id": "instanceA",
       "native_job_id": OCR_NATIVE_ID, "operation_id": "operationA", "input_manifest_hash": H,
       "native_status": "working", "status": "running", "stage": "extracting", "counts": COUNTS,
       "capabilities": ["cancel"], "last_event_seq": 4, "updated_at": T}

POSITIVE = {
    "WorkspaceCreate": {**V, "name": "客户资料核对"},
    "WorkspacePage": {**V, "items": [{**V, "workspace_id": "workspaceA", "name": "客户资料核对",
                                    "revision": 1, "created_at": T, "updated_at": T}],
                      "next_cursor": "1", "snapshot_revision": 2},
    "RecentWork": {**V, "recent_id": "recentA", "source": "platform_launch",
                   "app_id": "ocr", "launch_id": "launchA", "instance_id": "instanceA",
                   "opened_at": T},
    "RecentWorkPage": {**V, "items": [{**V, "recent_id": "recentA", "source": "platform_launch",
                                             "app_id": "ocr", "launch_id": "launchA", "instance_id": "instanceA",
                                             "opened_at": T}], "next_cursor": "1", "snapshot_revision": 1},
    "ModuleProjectLink": {**V, "workspace_id": "workspaceA", "app_id": "ocr",
                          "module_project_id": OCR_NATIVE_ID, "link_id": "linkA",
                          "role": "owner", "revision": 1},
    "AppManifest": {**V, "app_id": "ocr", "display_name": "OCR", "app_version": "1.0.0",
                    "adapter_version": "1.0.0", "min_platform_version": "1.0.0",
                    "data_schema_version": "1", "entry_mode": "standalone_page",
                    "capabilities": ["cancel", "import"], "input_types": ["image/png"],
                    "output_types": ["application/json"], "resources": ["cpu"], "license_refs": ["LICENSE"]},
    "AppInstallation": {**V, "app_id": "ocr", "installed": False, "launch_ready": False, "state": "not_registered"},
    "OpenContext": {**V, "app_id": "ocr", "instance_id": "instanceA", "link_id": "linkA",
                    "asset_set_id": "setA", "nonce": "nonceA", "expires_at": "2026-09-25T02:00:00Z",
                    "return_route": "/workspaces/demo/assets"},
    "AssetSet": {**V, "asset_set_id": "setA", "revision": 2, "manifest_hash": H,
                 "items": [{**V, "item_id": "itemA", "asset_id": "assetA", "asset_revision": 1,
                            "blob_hash": H, "source_locator": LOCATOR}]},
    "SourceLocator": LOCATOR,
    "JobRef": JOB,
    "Artifact": {**V, "artifact_id": "artifactA", "workspace_id": "workspaceA", "job_ref": JOB,
                 "revision": 1, "media_type": "application/json", "blob_hash": H2,
                 "size_bytes": 100, "input_manifest_hash": H, "application_version": "1.0.0",
                 "rule_or_model_version": "rules-1", "created_at": T, "availability": "available",
                 "source_refs": [LOCATOR]},
    "HandoffRequest": {**V, "request_id": "requestA", "target_link_id": "linkA",
                       "asset_set_id": "setA", "asset_set_revision": 2, "manifest_hash": H,
                       "import_options": {**V, "mode": "copy", "duplicate_policy": "skip"}},
    "ResourceGrant": {**V, "resource_id": "gpuA", "owner_instance_id": "instanceA",
                      "job_id": "jobA", "generation": 2, "granted_at": T,
                      "heartbeat_at": "2026-09-25T01:01:00Z", "lease_until": "2026-09-25T01:02:00Z",
                      "managed_process_identity": "processA"},
    "Error": {**V, "code": "INPUT_CHANGED", "message": "Input has changed",
              "retryable": False, "request_id": "requestA", "details": {}},
}


def invalid(model: str, path: tuple[str | int, ...], value) -> dict:
    payload = deepcopy(POSITIVE[model])
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return payload


NEGATIVE = {
    "workspace_create_blank": ("WorkspaceCreate", {**V, "name": ""}),
    "workspace_create_extra": ("WorkspaceCreate", {**V, "name": "Case", "path": "C:/private"}),
    "workspace_page_zero_snapshot": ("WorkspacePage", {**POSITIVE["WorkspacePage"], "snapshot_revision": 0}),
    "workspace_page_unsafe_snapshot": ("WorkspacePage", {**POSITIVE["WorkspacePage"], "snapshot_revision": 9_007_199_254_740_992}),
    "unknown_control_field": ("AppManifest", invalid("AppManifest", ("executable",), "C:/evil.exe")),
    "unsupported_entry_mode": ("AppManifest", invalid("AppManifest", ("entry_mode",), "embedded")),
    "unsupported_schema_version": ("AppManifest", invalid("AppManifest", ("schema_version",), "2.0")),
    "overlong_capabilities": ("AppManifest", invalid("AppManifest", ("capabilities",), ["cancel"] * 17)),
    "external_return_route": ("OpenContext", invalid("OpenContext", ("return_route",), "https://example.test/")),
    "encoded_return_route": ("OpenContext", invalid("OpenContext", ("return_route",), "/%2f/example")),
    "path_disguised_id": ("OpenContext", invalid("OpenContext", ("asset_set_id",), "C:/private/data")),
    "utc_offset": ("OpenContext", invalid("OpenContext", ("expires_at",), "2026-09-25T10:00:00+08:00")),
    "invalid_calendar_date": ("OpenContext", invalid("OpenContext", ("expires_at",), "2026-02-30T01:00:00Z")),
    "bool_revision": ("AssetSet", invalid("AssetSet", ("revision",), True)),
    "locator_mismatch": ("AssetSet", invalid("AssetSet", ("items", 0, "source_locator", "asset_id"), "otherAsset")),
    "zero_page": ("SourceLocator", invalid("SourceLocator", ("page_number",), 0)),
    "false_exact_region": ("SourceLocator", {**LOCATOR, "kind": "region", "precision": "exact"}),
    "page_only_with_box": ("SourceLocator", {**LOCATOR, "kind": "region", "precision": "page_only",
                                                 "image_version_id": "imageA", "coordinate_space": "normalized",
                                                 "transform_revision": 1, "box": [0.1, 0.1, 0.5, 0.5]}),
    "infinite_coordinate": ("SourceLocator", {**LOCATOR, "kind": "region", "precision": "exact",
                                                   "image_version_id": "imageA", "coordinate_space": "normalized",
                                                   "transform_revision": 1, "box": [0.1, 0.1, "Infinity", 0.5]}),
    "pdf_table_without_index": ("SourceLocator", {**LOCATOR, "kind": "table"}),
    "incorrect_counts": ("JobRef", invalid("JobRef", ("counts", "pending"), 1)),
    "bool_event_sequence": ("JobRef", invalid("JobRef", ("last_event_seq",), True)),
    "partial_without_failures": ("JobRef", invalid("JobRef", ("status",), "partial")),
    "partial_all_failed": ("JobRef", {**JOB, "status": "partial", "counts": {**COUNTS,
                                                  "succeeded": 0, "failed": 3, "pending": 0}}),
    "artifact_input_mismatch": ("Artifact", invalid("Artifact", ("input_manifest_hash",), H2)),
    "handoff_path_id": ("HandoffRequest", invalid("HandoffRequest", ("target_link_id",), "../secret")),
    "grant_old_generation": ("ResourceGrant", invalid("ResourceGrant", ("generation",), 0)),
    "grant_time_order": ("ResourceGrant", invalid("ResourceGrant", ("lease_until",), "2026-09-25T00:00:00Z")),
    "error_unknown_code": ("Error", invalid("Error", ("code",), "SOMETHING_ELSE")),
    "error_details_array": ("Error", invalid("Error", ("details",), ["raw private text"])),
    "handoff_import_without_target": ("HandoffStatus", {**V, "request_id": "requestA", "operation_id": "operationA",
                                                         "state": "completed", "items": [{**V, "item_id": "itemA", "status": "imported"}]}),
    "handoff_partial_pending": ("HandoffStatus", {**V, "request_id": "requestA", "operation_id": "operationA",
                                                  "state": "partial", "items": [
                                                      {**V, "item_id": "itemA", "status": "imported", "target_asset_id": OCR_NATIVE_ID},
                                                      {**V, "item_id": "itemB", "status": "failed", "error_code": "INPUT_MISSING"},
                                                      {**V, "item_id": "itemC", "status": "pending"}]}),
    "page_limit_exceeded": ("PageQuery", {**V, "limit": 201}),
    "page_bool_limit": ("PageQuery", {**V, "limit": True}),
    "decimal_as_float": ("DecimalValue", {**V, "value": 12.5, "currency": "USD"}),
    "decimal_precision_overflow": ("DecimalValue", {**V, "value": "1234567890123456789.00", "currency": "USD"}),
    "decimal_scale_overflow": ("DecimalValue", {**V, "value": "12.1234567", "currency": "USD"}),
}

POSITIVE["HandoffStatus"] = {**V, "request_id": "requestA", "operation_id": "operationA", "state": "partial",
                             "items": [{**V, "item_id": "itemA", "status": "imported", "target_asset_id": OCR_NATIVE_ID},
                                       {**V, "item_id": "itemB", "status": "failed", "error_code": "INPUT_MISSING"}]}
POSITIVE["PageQuery"] = {**V, "limit": 100, "cursor": "cursorA", "snapshot_revision": 2}
POSITIVE["Asset"] = {**V, "asset_id": "assetA", "workspace_id": "workspaceA", "source_id": "sourceA",
                     "original_name": "sample.pdf", "media_type": "application/pdf", "storage_mode": "managed",
                     "blob_hash": H, "size_bytes": 2_000_000_000, "revision": 1, "created_at": T}
POSITIVE["DecimalValue"] = {**V, "value": "123456789012.123456", "currency": "USD"}

NEGATIVE.update({
    "installation_inconsistent": ("AppInstallation", {**POSITIVE["AppInstallation"], "installed": True}),
    "installation_storage_inconsistent": ("AppInstallation", {**POSITIVE["AppInstallation"],
                                                "installed": True, "launch_ready": True,
                                                "state": "storage_unavailable"}),
    "recent_launch_fake_project": ("RecentWork", invalid("RecentWork", ("module_project_id",), "projectA")),
    "recent_native_missing_link": ("RecentWork", {**POSITIVE["RecentWork"], "source": "native_project_open",
                                                 "workspace_id": "workspaceA", "module_project_id": "projectA"}),
    "native_project_path": ("ModuleProjectLink", invalid("ModuleProjectLink", ("module_project_id",), "../secret")),
    "native_project_empty": ("ModuleProjectLink", invalid("ModuleProjectLink", ("module_project_id",), "")),
    "native_project_overlong": ("ModuleProjectLink", invalid("ModuleProjectLink", ("module_project_id",), "a" * 65)),
    "native_job_path": ("JobRef", invalid("JobRef", ("native_job_id",), "C:/jobs/private")),
    "native_job_empty": ("JobRef", invalid("JobRef", ("native_job_id",), "")),
    "native_job_overlong": ("JobRef", invalid("JobRef", ("native_job_id",), "a" * 65)),
    "receiver_asset_path": ("HandoffStatus", invalid("HandoffStatus", ("items", 0, "target_asset_id"), "../private")),
    "receiver_asset_empty": ("HandoffStatus", invalid("HandoffStatus", ("items", 0, "target_asset_id"), "")),
    "receiver_asset_overlong": ("HandoffStatus", invalid("HandoffStatus", ("items", 0, "target_asset_id"), "a" * 65)),
})
