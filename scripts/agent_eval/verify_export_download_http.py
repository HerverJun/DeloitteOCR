"""Small real HTTP + browser download check, using synthetic in-process artifacts only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import httpx
import uvicorn

from ocr_workbench.agent.contracts import ExportResults
from ocr_workbench.service import create_app
from test_agent_artifacts import ArtifactTests


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a new output directory")
    output.mkdir(parents=True)
    receipt: dict = {"status": "running", "scope": "synthetic product artifact, real loopback HTTP and browser UI; no controller/OCR/GPU", "checks": []}
    fixture = ArtifactTests("test_complete_file_remains_original_format_without_partial_sidecar")
    fixture.setUp()
    server = None
    thread = None
    sock = None
    try:
        artifacts, partial_args, context = fixture.partial()
        fixture.agent.record_calls(fixture.project, fixture.run["id"], 1, 1,
                                   [{"call_id": "partial", "tool": "export_results", "arguments": partial_args.model_dump(exclude_none=True)}])
        if artifacts.export(partial_args, context)["status"] != "needs_user":
            raise AssertionError("Partial export did not require authorization")
        decision = fixture.store.rows("SELECT * FROM agent_decisions")[0]
        fixture.agent.reply_decision(fixture.project, fixture.run["id"], decision["id"],
                                     {"client_request_id": "allow-http", "payload_hash": decision["payload_hash"], "option_id": "allow"})
        partial = artifacts.export(partial_args, context)
        partial_id = partial["artifact_refs"][0]["artifact_id"]
        if partial["status"] != "partial":
            raise AssertionError("Partial artifact was not published")

        result_id = fixture.store.rows("SELECT id FROM results")[0]["id"]
        complete_args = ExportResults(source="explicit_results", format="txt", results=[{
            "result_id": result_id, "revision": 0, "version_id": fixture.photo["active_version"]}])
        fixture.agent.record_calls(fixture.project, fixture.run["id"], 1, 2,
                                   [{"call_id": "complete-http", "tool": "export_results", "arguments": complete_args.model_dump(exclude_none=True)}])
        complete = artifacts.export(complete_args, {**context, "step": 2, "call_id": "complete-http"})
        complete_id = complete["artifact_refs"][0]["artifact_id"]
        if complete["status"] != "success":
            raise AssertionError("Complete artifact was not published")
        receipt["checks"].append("product artifacts created with explicit scoped export and partial consent")

        bundle = Path(fixture.temp.name) / "bundle"
        (bundle / "config").mkdir(parents=True)
        (bundle / "config/engines.json").write_text("{}", encoding="utf-8")
        app = create_app(bundle, fixture.store.root, "synthetic-http-token", start_queue=False, agent_enabled=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
        server = uvicorn.Server(config)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base = "http://127.0.0.1:" + str(sock.getsockname()[1])
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        if not server.started:
            raise RuntimeError("Loopback service did not start")
        prefix = f"/api/projects/{fixture.project}/agent/artifacts"
        with httpx.Client(base_url=base, timeout=15) as client:
            no_auth = client.get(f"{prefix}/{partial_id}/download")
            if no_auth.status_code != 401:
                raise AssertionError(f"Unauthenticated download returned {no_auth.status_code}")
            client.headers["Authorization"] = "Bearer synthetic-http-token"
            wrong_project = client.get(f"/api/projects/{fixture.other}/agent/artifacts/{partial_id}/download")
            missing = client.get(f"{prefix}/missing/coverage-manifest")
            if not 400 <= wrong_project.status_code < 500 or not 400 <= missing.status_code < 500:
                raise AssertionError("Cross-project/missing artifact did not produce 4xx")
            full_file = client.get(f"{prefix}/{complete_id}/download")
            full_sidecar = client.get(f"{prefix}/{complete_id}/coverage-manifest")
            partial_file = client.get(f"{prefix}/{partial_id}/download")
            partial_sidecar = client.get(f"{prefix}/{partial_id}/coverage-manifest")
            if any(response.status_code != 200 for response in (full_file, partial_file, partial_sidecar)):
                raise AssertionError("Published file or sidecar failed HTTP download")
            if not 400 <= full_sidecar.status_code < 500:
                raise AssertionError("Complete artifact unexpectedly exposed partial sidecar")
            if 'filename="export.txt"' not in full_file.headers.get("content-disposition", "") or \
                    'filename="export-partial.txt"' not in partial_file.headers.get("content-disposition", "") or \
                    'filename="export-partial-manifest.json"' not in partial_sidecar.headers.get("content-disposition", ""):
                raise AssertionError("Download filenames are incorrect")
            sidecar = partial_sidecar.json()
            if sidecar["sha256"] != sha(partial_file.content) or sidecar["bytes"] != len(partial_file.content) or \
                    sidecar["manifest"]["partial_authorization"] != decision["id"] or \
                    not sidecar["manifest"]["coverage"]["failed_pages"]:
                raise AssertionError("Partial sidecar does not match downloaded bytes/authorization/coverage")
            receipt["http"] = {"unauthorized": no_auth.status_code, "cross_project": wrong_project.status_code,
                               "missing": missing.status_code, "complete_sidecar": full_sidecar.status_code,
                               "complete_sha256": sha(full_file.content), "partial_sha256": sha(partial_file.content),
                               "partial_manifest_sha256": sha(partial_sidecar.content), "partial_authorization_matches": True}
        receipt["checks"].append("real local HTTP 401/4xx, complete/partial files and verified coverage manifest")

        node = ROOT / "frontend/node_modules/node/bin/node.exe"
        if not node.is_file():
            node = Path("node")
        browser_receipt = output / "browser-download.json"
        command = [str(node), str(ROOT / "frontend/tests/agent-artifact-download-browser.mjs"),
                   base, fixture.project, fixture.other, complete_id, partial_id, str(browser_receipt)]
        with (output / "browser.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(command, cwd=ROOT / "frontend", stdout=log, stderr=subprocess.STDOUT, timeout=90)
        if process.returncode:
            raise RuntimeError("Browser download failed; inspect browser.log")
        browser_result = json.loads(browser_receipt.read_text("utf-8"))
        if browser_result.get("status") != "pass" or browser_result.get("partial_sha256") != receipt["http"]["partial_sha256"]:
            raise AssertionError("Browser result does not match HTTP artifact")
        receipt["browser"] = browser_result
        receipt["checks"].append("React UI browser clicks downloaded complete and partial file/manifest")
        receipt["status"] = "pass_synthetic_functional_only"
    except BaseException as error:
        receipt.update(status="fail", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        if sock:
            sock.close()
        fixture.doCleanups()
        receipt["service_stopped"] = not thread or not thread.is_alive()
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
