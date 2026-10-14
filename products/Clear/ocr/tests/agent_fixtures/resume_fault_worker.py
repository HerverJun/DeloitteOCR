"""Crash after a resumed await_jobs commits its call result, before graph saver commit."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.store import AgentStore, digest
from ocr_workbench.documents import Documents
from ocr_workbench.store import Store


async def main(root, project_id, session_id, page_id):
    business = Store(root)
    agent = AgentStore(business)
    services = ApplicationServices(business, Documents(business, Path(root) / 'bundle', None),
                                   SimpleNamespace(guard=business.file_lock))

    async def model(state, run):
        if state['step'] != 0:
            raise AssertionError('The resume fault must occur before a second model request')
        message = {'role': 'assistant', 'tool_calls': [{'id': 'pages', 'type': 'function', 'function': {
            'name': 'process_pages', 'arguments': json.dumps({'document_id': page_id, 'page_numbers': [1],
                                                               'mode': 'native', 'force': True})}}]}
        record, fresh = agent.begin_model_request(project_id, run['id'], state['generation'], 0, digest(message))
        if not fresh:
            return json.loads(record['normalized_response'])
        response = normalize_response('openai_chat_completions', {'choices': [{'message': message,
            'finish_reason': 'tool_calls'}]}, record['request_id'])
        agent.finish_model_request(project_id, run['id'], state['generation'], record['request_id'], response)
        return response

    runtime = await AgentRuntime(services, policy={'limits': {}}, model_override=model).open()
    run = agent.create_run(project_id, session_id, {'client_request_id': 'resume-fault-window',
        'content': '处理第一页'}, context={}, config={'revision': 1}, limits={})
    runtime.policy.grant(project_id, 'scoped_processing', {'document_ids': [page_id], 'page_ids': [page_id],
        'mode': 'native', 'force': True}, source='user_request', expires=time.time() + 60, run_id=run['id'])
    runtime.schedule(run)
    for _ in range(100):
        if agent.run(project_id, run['id'])['status'] == 'waiting_jobs':
            break
        await asyncio.sleep(.02)
    else:
        raise AssertionError('No waiting checkpoint was saved')
    await runtime.tasks[run['id']]
    stage = business.claim_document_stage()
    business.finish_document_stage(stage['id'], {'synthetic': True})
    original = runtime.agent.finish_call

    def crash_after_call_commit(*args, **kwargs):
        original(*args, **kwargs)
        # The business result and tool_finished projection are durable. The
        # Command resume node result has not reached the official SQLite saver.
        os._exit(92)

    runtime.agent.finish_call = crash_after_call_commit
    await runtime._observe_once()
    await runtime.tasks[run['id']]
    raise AssertionError('The resumed call-result fault was not reached')


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))
