"""Regenerate JSON Schema, OpenAPI and TypeScript from models.py.

Run from repository root: python -m packages.contracts.generate [--check]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import MODELS, PageQuery, VERSION
from .examples import POSITIVE, NEGATIVE

ROOT = Path(__file__).resolve().parent
NAMES = {
    "AppManifest": "app-manifest", "OpenContext": "context", "AssetSet": "asset-set",
    "SourceLocator": "source-locator", "JobRef": "job", "Artifact": "artifact",
    "HandoffRequest": "handoff", "ResourceGrant": "resource-grant", "Error": "error",
}


def dump(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


def components() -> dict[str, dict]:
    result = {}
    for model in MODELS:
        schema = model.model_json_schema()
        definitions = schema.pop("$defs", {})
        for name, definition in definitions.items():
            definition = rewrite_refs(definition)
            if name in result and result[name] != definition:
                raise ValueError(f"conflicting definition: {name}")
            result[name] = definition
        result[model.__name__] = rewrite_refs(schema)
    return result


def rewrite_refs(value):
    if isinstance(value, dict):
        return {key: (item.replace("#/$defs/", "#/components/schemas/") if key == "$ref" else rewrite_refs(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_refs(item) for item in value]
    return value


def ts_type(schema: dict) -> str:
    if "$ref" in schema:
        prefix = "#/components/schemas/"
        if not schema["$ref"].startswith(prefix):
            raise ValueError(f"unsupported TypeScript reference: {schema['$ref']}")
        return schema["$ref"][len(prefix):]
    if "anyOf" in schema:
        return " | ".join(ts_type(part) for part in schema["anyOf"])
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
    lines = ["// Generated from packages/contracts/models.py; do not edit.", "// Runtime validation still belongs to the strict wire models.", ""]
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
                "schema": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$", "maxLength": 64}}

    def get(operation, parameter_name, result):
        return {"get": {"operationId": operation, "parameters": [parameter(parameter_name)],
                        "responses": {"200": response(result), "404": response("Error", "Not found")}}}

    def listing(operation, result):
        properties = PageQuery.model_json_schema()["properties"]

        def query_parameter(name):
            schema = properties[name].copy()
            schema.pop("default", None)
            schema.pop("title", None)
            if "anyOf" in schema:
                schema = next(part for part in schema["anyOf"] if part.get("type") != "null")
            return {"name": name, "in": "query", "required": name == "limit", "schema": schema}

        return {"get": {"operationId": operation,
                        "parameters": [query_parameter(name) for name in ("limit", "cursor", "snapshot_revision")],
                        "responses": {"200": response(result), "409": response("Error", "Snapshot expired")}}}

    paths = {
        "/api/platform/v1/apps": listing("listAppManifests", "AppManifestPage"),
        "/api/platform/v1/apps/{app_id}": get("getAppManifest", "app_id", "AppManifest"),
        "/api/platform/v1/apps/{app_id}/health": get("getAppHealth", "app_id", "AppHealth"),
        "/api/platform/v1/open-contexts": {"post": {"operationId": "issueOpenContext", "requestBody": body("ContextIssueRequest"),
                                                  "responses": {"201": response("OpenContext"), "400": response("Error", "Invalid or inaccessible context")}}},
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
    return {"openapi": "3.1.0", "info": {"title": "Workbench adapter protocol", "version": VERSION},
            "paths": paths, "components": {"schemas": definitions}}


def artifacts() -> dict[Path, str]:
    result = {}
    for model in MODELS:
        if model.__name__ in NAMES:
            result[ROOT / (NAMES[model.__name__] + ".schema.json")] = dump(model.model_json_schema())
    definitions = components()
    result[ROOT / "adapter.openapi.json"] = dump(openapi(definitions))
    result[ROOT / "types.generated.ts"] = render_types(definitions)
    result[ROOT / "examples" / "positive.json"] = dump(POSITIVE)
    result[ROOT / "examples" / "negative.json"] = dump({name: {"model": model, "payload": payload}
                                                          for name, (model, payload) in NEGATIVE.items()})
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
