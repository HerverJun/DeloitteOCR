from typing import Annotated, TypedDict


def messages(previous, incoming):
    result = list(previous or [])
    by_id = {message.get('id'): index for index, message in enumerate(result) if message.get('id')}
    for message in incoming or []:
        identity = message.get('id')
        if identity and identity in by_id:
            result[by_id[identity]] = message
        else:
            if identity:
                by_id[identity] = len(result)
            result.append(message)
    return result


class AgentState(TypedDict, total=False):
    run_id: str
    session_id: str
    workspace_id: str
    generation: int
    messages: Annotated[list[dict], messages]
    visible: list[str]
    protected_tools: list[str]
    step: int
    tool_count: int
    calls: list[dict]
    cursor: int
    results: list[dict]
    inbox_received: list[str]
    wait: str | None
    decision_id: str | None
    final_text: str
    correction_count: int
    delivery: dict | None
    error_counts: dict[str, int]
    answer: dict | None
    answer_display_id: str | None
    delivery_display_id: str | None
    # Screen batches remain durable native operations while a new user
    # directive is handled. Other job families keep the normal blocking wait.
    background_jobs: list[dict]
