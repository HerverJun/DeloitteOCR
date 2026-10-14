"""Export deterministic P0 contracts, API declarations and policy defaults."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from ocr_workbench.agent.contracts import export_contracts


def api_contract():
    rows = [
        ("GET", "/api/agent/connection", None, "redacted connection"),
        ("PUT", "/api/agent/connection", "probe-bound connection + client_request_id", "redacted connection revision"),
        ("DELETE", "/api/agent/connection", "client_request_id + config_revision", "cleared revision"),
        ("POST", "/api/agent/connection/models", "explicit draft connection", "model list; no save"),
        ("POST", "/api/agent/connection/probe", "explicit draft connection", "synthetic two-turn capability receipt; no project content"),
        ("GET", "/api/projects/{project_id}/agent/sessions", None, "sessions + next_cursor"),
        ("POST", "/api/projects/{project_id}/agent/sessions", "CreateSession", "session"),
        ("GET", "/api/agent/sessions/{session_id}", None, "session snapshot + through_seq"),
        ("PATCH", "/api/agent/sessions/{session_id}", "UpdateSession", "session; active archive conflicts"),
        ("POST", "/api/agent/sessions/{session_id}/messages", "SendMessage", "run_id or inbox_id"),
        ("GET", "/api/agent/sessions/{session_id}/events", "after_seq >= 0; limit 1..500", "events + through_seq + has_more"),
        ("GET", "/api/agent/sessions/{session_id}/stream", "after_seq >= 0", "text/event-stream; id=seq; data=Event; heartbeat comment"),
        ("GET", "/api/agent/runs/{run_id}", None, "RunState + coverage + jobs + usage"),
        ("POST", "/api/agent/runs/{run_id}/cancel", "CancelRun", "run snapshot; idempotent"),
        ("POST", "/api/agent/runs/{run_id}/resume", "ResumeRun", "run snapshot after revalidation"),
        ("POST", "/api/agent/decisions/{decision_id}/reply", "ReplyDecision", "decision + run snapshot"),
        ("GET", "/api/agent/artifacts/{artifact_id}", None, "artifact state + manifest"),
        ("GET", "/api/agent/artifacts/{artifact_id}/download", None, "leased file or explicit unavailable state"),
        ("PATCH", "/api/agent/artifacts/{artifact_id}", "client_request_id + pinned:boolean", "artifact"),
        ("DELETE", "/api/agent/artifacts/{artifact_id}", "client_request_id", "deleted or active lease conflict"),
    ]
    return {
        "version": "ocr-agent-api-v1", "implemented_routes": False,
        "routes": [dict(method=m, path=p, request=r, response=s) for m, p, r, s in rows],
        "transport": {"auth": "existing loopback + Origin + Bearer middleware", "sse": "fetch with Authorization header; never token in URL", "unauthorized": "401 stops reconnect", "replay": "session monotonically increasing seq; snapshot through_seq; reducer dedup and generation check", "atomicity": "state and event in one transaction; database cursor is source of truth", "slow_client": "bounded buffer then disconnect; replay from last processed seq"},
        "http_errors": {"401": "invalid session bearer", "403": "scope/authorization denied", "404": "resource unavailable in bound project", "409": "revision/config/idempotency/state conflict", "410": "expired/deleted artifact", "422": "strict schema validation", "429": "local budget/concurrency limit", "503": "capability unavailable"},
        "writes": "all replayable mutations require client_request_id + canonical request hash; same key same input reuses receipt; different input conflicts",
        "secrets": "draft credentials only in explicit connection write/probe request; never in GET, SSE, errors, export or diagnostics; persist independent DPAPI reference",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Audit directory for deterministic JSON exports")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    contracts = export_contracts()
    contracts["api"] = api_contract()
    contracts["state_boundaries"] = {
        "session": {"states": ["active", "archived"], "binding": "immutable project; archive does not delete OCR or artifacts"},
        "run": {"identity": "one user goal; unique(session_id, client_request_id)", "one_active_per_session": True, "completion": "coverage settled plus validated final response; success/partial/answered"},
        "call": {"identity": "unique(run_id, step, provider_call_id)", "states": ["validated", "running", "waiting_jobs", "finished", "error", "cancelled"], "pairing": "all calls get ordered final result/error; queued UI events are not tool completion"},
        "operation": {"identity": "server logical action + version + canonical input hash, independent of provider_call_id", "states": ["prepared", "submitted", "waiting_jobs", "succeeded", "partial", "failed", "cancelled", "interrupted"], "transaction": "operation and business job links atomically committed before wake"},
        "decision": {"states": ["pending", "resolved", "expired", "cancelled"], "same_reply": "idempotent", "different_reply": "conflict", "authority": "structured scope/revision/config and payload hash; never model summary"},
    }
    for name, value in [("contracts.json", contracts), ("policy-defaults.json", json.loads((ROOT / "config/agent-policy.json").read_text("utf-8")))]:
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print("Exported 12 tool schemas, 20 API declarations and versioned policy; routes are not implemented.")


if __name__ == "__main__":
    main()
