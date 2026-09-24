"""JSON-only graph state. Services and credentials are runtime dependencies."""
from typing import Annotated, TypedDict


def merge_messages(previous, updates):
    values = {message["id"]: message for message in previous}
    for message in updates:
        # Only application context preparation emits these internal directives;
        # provider normalization never copies arbitrary response fields.
        if message.get('_delete') is True:
            values.pop(message['id'], None)
        else:
            values[message['id']] = message
    return list(values.values())


class AgentState(TypedDict, total=False):
    messages: Annotated[list[dict], merge_messages]
    run_id: str
    project_id: str
    generation: int
    graph_version: str
    state_version: int
    step: int
    calls: list[dict]
    results: list[dict]
    cursor: int
    decision_id: str | None
    wait_reason: str | None
    batch_error: dict | None
    error_stop: str | None
    final_text: str
    outcome: str
    context_summary: dict | None
    context_prepared: dict | None
    context_summary_attempt: int
    context_summary_done: bool


def initial_state(run, content):
    return {"messages": [{"id": run["id"] + ":user", "role": "user", "content": content}],
            "run_id": run["id"], "project_id": run["project_id"], "generation": run["generation"],
            "graph_version": "ocr-agent-graph-v1", "state_version": 1, "step": 0, "calls": [],
            "results": [], "cursor": 0, "decision_id": None, "wait_reason": None, "batch_error": None, "final_text": "",
            "context_prepared": None, "context_summary_attempt": 0, "context_summary_done": False}
