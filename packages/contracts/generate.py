"""Regenerate JSON Schema, OpenAPI and TypeScript from models.py.

Run from repository root: python -m packages.contracts.generate [--check]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import MODELS, PageQuery, VERSION
from .examples import POSITIVE, NEGATIVE
from .automation import AUTOMATION_MODELS, AUTOMATION_EXAMPLES
from .audit import AUDIT_MODELS, TodoQuery
from .audit_openapi import audit_paths
from .uploads import UPLOAD_MODELS, UploadBinding
from .uploads_openapi import upload_paths
from .forensics import FORENSICS_MODELS, forensics_paths
from .agent import AGENT_MODELS
from .agent_openapi import agent_paths
from .multimodal import MULTIMODAL_MODELS, multimodal_paths
from .tasks import TASK_MODELS, task_paths
from .direct import ImageTextRequest, PdfTextRequest
from .direct_data import DataActionRequest
from .direct_classification import PredictRequest
from .agent_integrations import Integrations

DIRECT_MODELS = [ImageTextRequest, PdfTextRequest, DataActionRequest, PredictRequest, Integrations]

ROOT = Path(__file__).resolve().parent
NAMES = {
    "AppManifest": "app-manifest", "OpenContext": "context", "AssetSet": "asset-set",
    "SourceLocator": "source-locator", "JobRef": "job", "Artifact": "artifact",
    "HandoffRequest": "handoff", "ResourceGrant": "resource-grant", "Error": "error",
    "WorkspacePage": "workspace-page", "WorkspaceCreate": "workspace-create",
    "RecentWork": "recent-work", "RecentWorkPage": "recent-work-page",
}


def dump(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


def components() -> dict[str, dict]:
    result = {}
    for model in [*MODELS, *AUTOMATION_MODELS, *AUDIT_MODELS, *UPLOAD_MODELS, *FORENSICS_MODELS, *AGENT_MODELS, *MULTIMODAL_MODELS, *TASK_MODELS, *DIRECT_MODELS]:
        schema = model.model_json_schema()
        definitions = schema.pop("$defs", {})
        for name, definition in definitions.items():
            definition = rewrite_refs(definition)
            if name in result and result[name] != definition:
                raise ValueError(f"conflicting definition: {name}")
            result[name] = definition
        if schema.get('$ref') == '#/$defs/' + model.__name__:
            schema = definitions[model.__name__]
        result[model.__name__] = rewrite_refs(schema)
    return result


def rewrite_refs(value):
    if isinstance(value, dict):
        result = {key: (item.replace("#/$defs/", "#/components/schemas/") if key == "$ref" else rewrite_refs(item))
                  for key, item in value.items()}
        if isinstance(result.get('discriminator'), dict) and isinstance(result['discriminator'].get('mapping'), dict):
            result['discriminator']['mapping'] = {key: item.replace('#/$defs/', '#/components/schemas/')
                for key, item in result['discriminator']['mapping'].items()}
        return result
    if isinstance(value, list):
        return [rewrite_refs(item) for item in value]
    return value


def ts_type(schema: dict) -> str:
    if not schema:
        return 'unknown'
    if "$ref" in schema:
        prefix = "#/components/schemas/"
        if not schema["$ref"].startswith(prefix):
            raise ValueError(f"unsupported TypeScript reference: {schema['$ref']}")
        return schema["$ref"][len(prefix):]
    if "anyOf" in schema:
        return " | ".join(ts_type(part) for part in schema["anyOf"])
    if "oneOf" in schema:
        return " | ".join(ts_type(part) for part in schema["oneOf"])
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(item) for item in schema["enum"])
    kind = schema.get("type")
    if kind == "null":
        return "null"
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        if "prefixItems" in schema:
            if schema.get("minItems") != schema.get("maxItems") or schema["minItems"] != len(schema["prefixItems"]):
                raise ValueError("variable tuple is unsupported")
            return "[" + ", ".join(ts_type(item) for item in schema["prefixItems"]) + "]"
        if "items" not in schema:
            raise ValueError("untyped array is unsupported")
        return f"Array<{ts_type(schema['items'])}>"
    if kind == "object":
        if schema.get('additionalProperties') is True and 'properties' not in schema:
            return 'Record<string, unknown>'
        props = schema.get("properties")
        if props is not None:
            required = set(schema.get("required", []))
            fields = [f"{json.dumps(name)}{'?' if name not in required else ''}: {ts_type(value)}"
                      for name, value in props.items()]
            if schema.get("additionalProperties", False) is not False:
                raise ValueError("mixed open/closed object is unsupported")
            return "{ " + "; ".join(fields) + " }"
        if isinstance(schema.get("additionalProperties"), dict):
            return "Record<string, " + ts_type(schema["additionalProperties"]) + ">"
        if "patternProperties" in schema and len(schema["patternProperties"]) == 1 and schema.get("additionalProperties", False) is False:
            return "Record<string, " + ts_type(next(iter(schema["patternProperties"].values()))) + ">"
    raise ValueError(f"unsupported TypeScript schema construct: {schema}")


def render_types(definitions: dict[str, dict]) -> str:
    lines = ["// Generated from packages/contracts wire models; do not edit.", "// Runtime validation still belongs to the strict wire models.", ""]
    for name, schema in sorted(definitions.items()):
        lines.append(f"export type {name} = {ts_type(schema)};")
    return "\n".join(lines) + "\n"


def openapi(definitions: dict[str, dict]) -> dict:
    def ref(name):
        return {"$ref": f"#/components/schemas/{name}"}

    def response(name, description="OK"):
        return {"description": description, "content": {"application/json": {"schema": ref(name)}}}

    def body(name):
        return {"required": True, "content": {"application/json": {"schema": ref(name)}}}

    def parameter(name):
        return {"name": name, "in": "path", "required": True,
                "schema": {"type": "string", "pattern": ("^(?!current$)[A-Za-z][A-Za-z0-9_-]{0,63}$"
                                                          if name == "launch_id" else "^[A-Za-z][A-Za-z0-9_-]{0,63}$"),
                           "maxLength": 64}}

    def get(operation, parameter_name, result):
        return {"get": {"operationId": operation, "parameters": [parameter(parameter_name)],
                        "responses": {"200": response(result), "404": response("Error", "Not found")}}}

    def listing(operation, result, *, safe_revision=False):
        properties = PageQuery.model_json_schema()["properties"]

        def query_parameter(name):
            schema = properties[name].copy()
            schema.pop("default", None)
            schema.pop("title", None)
            if "anyOf" in schema:
                schema = next(part for part in schema["anyOf"] if part.get("type") != "null")
            if safe_revision and name == "snapshot_revision":
                schema["maximum"] = 9_007_199_254_740_991
            return {"name": name, "in": "query", "required": name == "limit", "schema": schema}

        return {"get": {"operationId": operation,
                        "parameters": [query_parameter(name) for name in ("limit", "cursor", "snapshot_revision")],
                        "responses": {"200": response(result), "409": response("Error", "Snapshot expired")}}}

    paths = {
        "/api/platform/v1/workspaces": {
            **listing("listWorkspaces", "WorkspacePage", safe_revision=True),
            "post": {"operationId": "createWorkspace", "requestBody": body("WorkspaceCreate"),
                     "responses": {"201": response("Workspace"), "400": response("Error", "Invalid request"),
                                   "409": response("Error", "Creation request already used for different content"),
                                   "403": response("Error", "Origin not permitted"),
                                   "503": response("Error", "Workspace store unavailable")}}},
        "/api/platform/v1/workspaces/{workspace_id}": get("getWorkspace", "workspace_id", "Workspace"),
        "/api/platform/v1/apps": listing("listAppManifests", "AppManifestPage"),
        "/api/platform/v1/apps/{app_id}": get("getAppManifest", "app_id", "AppManifest"),
        "/api/platform/v1/apps/{app_id}/health": get("getAppHealth", "app_id", "AppHealth"),
        "/api/platform/v1/apps/{app_id}/installation": get("getAppInstallation", "app_id", "AppInstallation"),
        "/api/platform/v1/apps/{app_id}/launches/{launch_id}": {
            "post": {"operationId": "startApplication", "parameters": [parameter("app_id"), parameter("launch_id")],
                     "responses": {"201": response("LaunchReceipt"), "200": response("LaunchReceipt"),
                                   "409": response("Error", "Live instance or launch ID conflict"),
                                   "503": response("Error", "Bundle missing or launch unavailable")}},
            "get": {"operationId": "getApplicationLaunch", "parameters": [parameter("app_id"), parameter("launch_id")],
                    "responses": {"200": response("LaunchReceipt"), "404": response("Error", "Unknown launch"),
                                  "410": response("Error", "Historical instance has exited")}}},
        "/api/platform/v1/apps/{app_id}/launches/current": {
            "get": {"operationId": "getCurrentApplicationLaunch", "parameters": [parameter("app_id")],
                    "responses": {"200": response("LaunchReceipt"), "404": response("Error", "No live instance")}}},
        "/api/platform/v1/recent-work": {
            "get": {**listing("listRecentWork", "RecentWorkPage", safe_revision=True)["get"],
                    "parameters": listing("listRecentWork", "RecentWorkPage", safe_revision=True)["get"]["parameters"] +
                                  [{"name": "workspace_id", "in": "query", "required": False,
                                    "schema": parameter("workspace_id")["schema"]}],
                    "responses": {"200": response("RecentWorkPage"), "409": response("Error", "Snapshot expired"),
                                  "503": response("Error", "Recent store unavailable")}}},
        "/api/platform/v1/workspaces/{workspace_id}/open-contexts": {
            "post": {"operationId": "issueOpenContext", "parameters": [parameter("workspace_id")],
                     "requestBody": body("ContextIssueRequest"),
                     "responses": {"201": response("OpenContext"), "400": response("Error", "Invalid or inaccessible context"),
                                   "403": response("Error", "Origin not permitted"),
                                   "503": response("Error", "Instance or store unavailable")}}},
        "/api/platform/v1/asset-sets/{asset_set_id}": get("getAssetSet", "asset_set_id", "AssetSet"),
        "/api/platform/v1/assets": listing("listAssets", "AssetPage"),
        "/api/platform/v1/jobs": listing("listJobs", "JobPage"),
        "/api/platform/v1/jobs/{platform_job_id}": get("getJob", "platform_job_id", "JobRef"),
        "/api/platform/v1/jobs/{platform_job_id}/control": {
            "post": {"operationId": "controlJob", "parameters": [parameter("platform_job_id")], "requestBody": body("JobControlRequest"),
                     "responses": {"200": response("JobRef"), "409": response("Error", "Revision conflict or unsupported capability")}}},
        "/api/platform/v1/artifacts/{artifact_id}": get("getArtifact", "artifact_id", "Artifact"),
        "/api/platform/v1/handoffs": {"post": {"operationId": "submitHandoff", "requestBody": body("HandoffRequest"),
                                            "responses": {"202": response("HandoffStatus"), "409": response("Error", "Idempotency or input conflict")}}},
        "/api/platform/v1/handoffs/{request_id}": get("getHandoffStatus", "request_id", "HandoffStatus"),
        "/api/platform/v1/resource-grants/{resource_id}": get("getResourceGrant", "resource_id", "ResourceGrant"),
    }
    paths.update({
        '/api/platform/v1/batch-templates': {'post': {'operationId': 'createBatchTemplate',
            'requestBody': body('BatchTemplateCreate'), 'responses': {'201': response('BatchTemplate')}}},
        '/api/platform/v1/batches': {'post': {'operationId': 'startProcessingBatch',
            'requestBody': body('BatchRequest'), 'responses': {'202': response('ProcessingBatch')}}},
        '/api/platform/v1/batches/{batch_id}': get('getProcessingBatch', 'batch_id', 'ProcessingBatch'),
        '/api/platform/v1/batches/{batch_id}/control': {'post': {'operationId': 'controlProcessingBatch',
            'parameters': [parameter('batch_id')], 'requestBody': body('BatchControl'),
            'responses': {'200': response('ProcessingBatch')}}},
        '/api/platform/v1/batches/{batch_id}/dispositions': {'post': {'operationId': 'disposeBatchResults',
            'parameters': [parameter('batch_id')], 'requestBody': body('BatchDisposition'),
            'responses': {'200': response('ProcessingBatch'), '409': response('Error', 'Revision or request conflict')}}},
        '/api/platform/v1/batches/{batch_id}/issues': {'get': {'operationId': 'listBatchIssues',
            'parameters': [parameter('batch_id'),
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 200}},
                {'name': 'disposition', 'in': 'query', 'schema': {'type': 'string', 'enum': ['pending', 'confirmed', 'deferred']}}],
            'responses': {'200': response('BatchIssuePage')}}},
        '/api/platform/v1/batches/{batch_id}/items/{item_id}': {'get': {
            'operationId': 'getBatchIndexedItem', 'parameters': [parameter('batch_id'), parameter('item_id'),
                {'name': 'batch_revision', 'in': 'query', 'required': False,
                 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 9_007_199_254_740_991}}],
            'responses': {'200': response('BatchItemRead'), '404': response('Error', 'Unknown batch or item'),
                          '409': response('Error', 'Batch revision changed')}}},
        '/api/platform/v1/batches/{batch_id}/operations/{operation_id}': {'get': {
            'operationId': 'getBatchOperation', 'parameters': [parameter('batch_id'), parameter('operation_id')],
            'responses': {'200': response('BatchOperation'), '404': response('Error', 'Unknown operation')}}},
        '/api/platform/v1/batches/{batch_id}/operations/{operation_id}/recover': {'post': {
            'operationId': 'recoverBatchOperation', 'parameters': [parameter('batch_id'), parameter('operation_id')],
            'description': 'Recover the persisted operation without replacing its request, actor, targets or native IDs.',
            'requestBody': {'required': True, 'content': {'application/json': {
                'schema': {'type': 'object', 'additionalProperties': False, 'maxProperties': 0}}}},
            'responses': {'200': response('ProcessingBatch'), '409': response('Error', 'Native outcome requires verification')}}},
        '/api/platform/v1/batches/{batch_id}/history': {'get': {
            'operationId': 'listBatchDispositionHistory', 'parameters': [parameter('batch_id'),
                {'name': 'item_id', 'in': 'query', 'schema': {'type': 'string'}},
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 200}}],
            'responses': {'200': response('BatchHistoryPage'), '404': response('Error', 'Unknown batch')}}},
        '/api/platform/v1/batches/{batch_id}/items/{item_id}/native-revisions': {'get': {
            'operationId': 'getBatchNativeRevisions', 'parameters': [parameter('batch_id'), parameter('item_id'),
                {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0}},
                {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100}}],
            'responses': {'200': response('BatchNativeRevisionPage'),
                '409': response('Error', 'Native identity or outcome requires verification'),
                '404': response('Error', 'Unknown batch item')}}},
        '/api/platform/v1/batches/{batch_id}/review-artifacts': {'post': {
            'operationId': 'publishReviewArtifacts', 'parameters': [parameter('batch_id')],
            'requestBody': body('ReviewArtifactRequest'),
            'responses': {'201': response('ProcessingBatch'), '409': response('Error', 'Review or revision conflict')}}},
        '/api/platform/v1/workspaces/{workspace_id}/artifacts/{bundle_id}': {'get': {
            'operationId': 'getWorkspaceArtifactBundle',
            'parameters': [parameter('workspace_id'), parameter('bundle_id')],
            'description': 'Ready review bundles return bounded metadata; complete history and unresolved items are immutable bundle files. Current status is evaluated against the live batch snapshot.',
            'responses': {'200': {'description': 'Ready bundle', 'content': {'application/json': {
                'schema': {'oneOf': [ref('ReviewBundle'), ref('WorkpaperBundle'), ref('ForensicBundle'), {'type': 'object',
                    'description': 'Existing data, classification or OCR bundle metadata',
                    'not': {'required': ['kind'], 'properties': {'kind': {'enum': ['batch_review', 'audit_workpaper', 'image_forensics']}}}}]}}}},
                '404': response('Error', 'Unknown or unpublished bundle')}}},
    })
    paths.update(audit_paths(ref, response, body, parameter, TodoQuery))
    paths.update(upload_paths(ref, response, parameter, UploadBinding))
    paths.update(forensics_paths(ref, response, body, parameter))
    paths.update(multimodal_paths(ref, response, body, parameter))
    paths.update(agent_paths(ref, body, parameter))
    paths.update(task_paths())
    return {"openapi": "3.1.0", "info": {"title": "Workbench adapter protocol", "version": VERSION},
            "paths": paths, "components": {"schemas": definitions}}


def artifacts() -> dict[Path, str]:
    result = {}
    for model in MODELS:
        if model.__name__ in NAMES:
            result[ROOT / (NAMES[model.__name__] + ".schema.json")] = dump(model.model_json_schema())
    for model in AUTOMATION_MODELS:
        result[ROOT / ('automation-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    for model in AUDIT_MODELS:
        result[ROOT / ('audit-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    for model in UPLOAD_MODELS:
        result[ROOT / ('upload-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    for model in FORENSICS_MODELS:
        result[ROOT / ("forensics-" + model.__name__.lower() + ".schema.json")] = dump(model.model_json_schema())
    for model in AGENT_MODELS:
        result[ROOT / ('agent-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    for model in MULTIMODAL_MODELS:
        result[ROOT / ('manual-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    for model in TASK_MODELS:
        result[ROOT / ('task-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    for model in DIRECT_MODELS:
        result[ROOT / ('direct-' + model.__name__.lower() + '.schema.json')] = dump(model.model_json_schema())
    definitions = components()
    result[ROOT / "adapter.openapi.json"] = dump(openapi(definitions))
    result[ROOT / "types.generated.ts"] = render_types(definitions)
    result[ROOT / "examples" / "positive.json"] = dump(POSITIVE)
    result[ROOT / "examples" / "negative.json"] = dump({name: {"model": model, "payload": payload}
                                                          for name, (model, payload) in NEGATIVE.items()})
    automation_models = {model.__name__: model for model in AUTOMATION_MODELS}
    result[ROOT / 'examples' / 'automation.json'] = dump({
        name: automation_models[name].model_validate(value).model_dump()
        for name, value in AUTOMATION_EXAMPLES.items()})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    out = artifacts()
    if args.check:
        stale = [str(path) for path, contents in out.items() if not path.exists() or path.read_text(encoding="utf-8") != contents]
        if stale:
            raise SystemExit("Stale contract artifacts: " + ", ".join(stale))
    else:
        for path, contents in out.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
