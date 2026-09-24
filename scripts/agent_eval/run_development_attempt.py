"""Run one F05 development task in a new workspace with a synthetic controller.

This is an execution/identity probe, not a task solution or a qualification run.
It never reads sealed fixtures, invokes a remote controller, or scores assertions.
The receipt deliberately contains no observation, explanation score or gate verdict.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


TASK_ID = re.compile(r"S0[1-6]-\d{2}\Z")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _development_input(tasks_path: Path, task_id: str):
    tasks_path = tasks_path.resolve(strict=True)
    if tasks_path.name != "tasks.json" or tasks_path.parent.name != "development":
        raise ValueError("only development/tasks.json is accepted")
    if not TASK_ID.fullmatch(task_id):
        raise ValueError("invalid task id")
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    matched = [task for task in tasks if task.get("id") == task_id and task.get("split") == "development"]
    if len(matched) != 1:
        raise ValueError("development task missing or ambiguous")
    task = matched[0]
    fixture_id = task["initial_state"]["fixture_state"]
    if not isinstance(fixture_id, str) or not re.fullmatch(r"development/FX\d{2}", fixture_id):
        raise ValueError("invalid development fixture reference")
    fixture_path = tasks_path.parent / (fixture_id.split("/")[1] + ".json")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture["id"] != fixture_id:
        raise ValueError("fixture identity mismatch")
    relative = Path(fixture["path"])
    if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "documents":
        raise ValueError("invalid development document path")
    source = (tasks_path.parent / relative).resolve(strict=True)
    if not source.is_relative_to((tasks_path.parent / "documents").resolve(strict=True)) or not source.is_file():
        raise ValueError("development document escapes fixture root")
    return task, fixture_id, source, {"tasks_sha256": _sha(tasks_path), "fixture_sha256": _sha(fixture_path),
                                       "document_sha256": _sha(source)}


async def execute(tasks_path: Path, task_id: str, repeat: int, output_root: Path, bundle: Path, *, timeout: float = 30) -> dict:
    """Persist actual Agent IDs and runtime effects; synthetic response is never task-derived."""
    if type(repeat) is not int or repeat < 1 or timeout <= 0:
        raise ValueError("repeat must be positive and timeout must exceed zero")
    task, fixture_id, source, hashes = _development_input(tasks_path, task_id)
    bundle = bundle.resolve(strict=True)
    output_root = output_root.resolve()
    fixture_root = tasks_path.resolve(strict=True).parent
    if output_root.is_relative_to(fixture_root) or output_root.is_relative_to(bundle):
        raise ValueError("output root must be outside frozen fixtures and bundle")
    output_root.mkdir(parents=True, exist_ok=True)
    attempt_dir = output_root / f"{task_id}-r{repeat}-{secrets.token_hex(8)}"
    attempt_dir.mkdir(exist_ok=False)
    workspace = attempt_dir / "workspace"
    started = datetime.now(timezone.utc).isoformat()
    receipt = {"schema": "f05-execution-probe-v1", "task_id": task_id, "repeat": repeat,
               "split": "development", "provider_mode": "synthetic", "real_model_qualification": "not_tested",
               "workspace_id": attempt_dir.name, "workspace_path": str(workspace.resolve()),
               "started_utc": started, "fixture_id": fixture_id, "source_hashes": hashes,
               "run_identity": None, "execution_status": "setup_failed"}

    def save():
        path = attempt_dir / "execution.json"
        temporary = attempt_dir / "execution.json.tmp"
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    save()  # Preserve the allocated identity even if PDF setup fails.
    runtime = None
    documents = None
    try:
        from ocr_workbench.agent.runtime import AgentRuntime
        from ocr_workbench.agent.providers import normalize_response
        from ocr_workbench.agent.store import digest
        from ocr_workbench.agent.selection import create_selection, grant_selection
        from ocr_workbench.application_services import ApplicationServices
        from ocr_workbench.documents import Documents
        from ocr_workbench.store import Store

        store = Store(workspace)
        project = store.project(task["initial_state"]["project_label"])["id"]
        documents = Documents(store, bundle)
        document = await asyncio.to_thread(documents.import_document, project, source.name, source, 72)
        requested = task["initial_state"].get("requested_pages") or [1]
        if (not isinstance(requested, list) or not requested or
                any(type(n) is not int or not 1 <= n <= document["page_count"] for n in requested)):
            raise ValueError("development requested pages exceed imported document")

        async def synthetic_model(state, run):
            step = state["step"]
            if step == 0:
                message = {"role": "assistant", "tool_calls": [{"id": "synthetic-context",
                    "type": "function", "function": {"name": "get_workspace_context", "arguments": "{}"}}]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "模拟主控仅核对工作区上下文；任务未执行。"}
                finish = "stop"
            record, fresh = runtime.agent.begin_model_request(project, run["id"], state["generation"], step, digest(message))
            if not fresh:
                return json.loads(record["normalized_response"])
            response = normalize_response("openai_chat_completions", {"choices": [{"message": message,
                "finish_reason": finish}]}, record["request_id"])
            runtime.agent.finish_model_request(project, run["id"], state["generation"], record["request_id"], response)
            return response

        services = ApplicationServices(store, documents, SimpleNamespace(guard=store.file_lock))
        runtime = await AgentRuntime(services, policy={}, model_override=synthetic_model).open()
        session = runtime.agent.create_session(project, {"client_request_id": "f05-session", "title": task_id})
        selection = create_selection(runtime.agent, project, {"document_ranges": [{"document_id": document["id"],
            "first_page": min(requested), "last_page": max(requested)}], "allow_processing": True,
            "allow_export": True}, available_engines=[])
        run = runtime.agent.create_run(project, session["id"], {"client_request_id": "f05-run",
            "content": task["intent"]}, context={"selection": selection["selection"],
            "selection_token": selection["selection_token"]}, config={}, limits={})
        grant_selection(runtime.policy, run, selection["selection"])
        receipt["run_identity"] = {"project_id": project, "document_id": document["id"],
            "session_id": session["id"], "run_id": run["id"], "graph_thread_id": run["graph_thread_id"],
            "executor_owner": runtime.owner, "selection_token": selection["selection_token"]}
        receipt["execution_status"] = "running"
        save()
        runtime.schedule(run)
        try:
            await asyncio.wait_for(runtime.tasks[run["id"]], timeout)
        except asyncio.TimeoutError:
            receipt["execution_status"] = "timeout_interrupted"
        current = runtime.agent.run(project, run["id"])
        receipt["terminal_state"] = current["status"]
        receipt["outcome"] = current["outcome"]
        receipt["last_event_seq"] = current["last_event_seq"]
        receipt["model_request_count"] = store.rows("SELECT COUNT(*) AS n FROM agent_model_requests WHERE run_id=?", (run["id"],))[0]["n"]
        receipt["execution_status"] = "synthetic_executed" if current["status"] == "completed" else receipt["execution_status"]
    except Exception as error:
        receipt["execution_status"] = "execution_error"
        receipt["error"] = f"{type(error).__name__}: {error}"
    finally:
        if runtime is not None:
            await runtime.close()
        if documents is not None:
            await asyncio.to_thread(documents.stop)
        receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True, help="development/tasks.json only")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True, help="local PDF runtime bundle")
    args = parser.parse_args()
    result = asyncio.run(execute(args.tasks, args.task_id, args.repeat, args.output_root, args.bundle))
    print(json.dumps({"execution_status": result["execution_status"], "workspace_id": result["workspace_id"],
                      "run_identity": result["run_identity"], "real_model_qualification": "not_tested"}, ensure_ascii=False))
    if result["execution_status"] != "synthetic_executed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
