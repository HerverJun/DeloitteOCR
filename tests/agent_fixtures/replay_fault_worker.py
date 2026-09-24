"""Separate-process crash at the real operation commit / tool-return boundary."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.store import AgentStore, digest
from ocr_workbench.documents import Documents
from ocr_workbench.store import Store


async def main(root, project_id, session_id, page_id):
    business = Store(root)
    agent = AgentStore(business)
    documents = Documents(business, Path(root) / 'bundle', None)
    services = ApplicationServices(business, documents, SimpleNamespace(guard=business.file_lock))

    async def model(state, run):
        message = ({'role': 'assistant', 'tool_calls': [{'id': 'pages', 'type': 'function', 'function': {
            'name': 'process_pages', 'arguments': json.dumps({'document_id': page_id, 'page_numbers': [1],
                                                               'mode': 'native', 'force': True})}}]}
                   if state['step'] == 0 else {'role': 'assistant', 'content': '已核对后台任务状态。'})
        record, fresh = agent.begin_model_request(project_id, run['id'], state['generation'], state['step'], digest(message))
        if not fresh:
            return json.loads(record['normalized_response'])
        response = normalize_response('openai_chat_completions', {'choices': [{'message': message,
            'finish_reason': 'tool_calls' if state['step'] == 0 else 'stop'}]}, record['request_id'])
        agent.finish_model_request(project_id, run['id'], state['generation'], record['request_id'], response)
        return response

    runtime = await AgentRuntime(services, policy={'limits': {}}, model_override=model).open()
    run = agent.create_run(project_id, session_id, {'client_request_id': 'fault-window', 'content': '处理第一页'},
                           context={}, config={'revision': 1}, limits={})
    runtime.policy.grant(project_id, 'scoped_processing', {'document_ids': [page_id], 'page_ids': [page_id],
        'mode': 'native', 'force': True}, source='user_request', expires=time.time() + 60, run_id=run['id'])
    original = Operations.submit

    def crash_after_commit(self, *args, **kwargs):
        def fault(window):
            if window == 'after_commit':
                # This is after business operation, stage and link commit but
                # before the tool can return or LangGraph can save its result.
                os._exit(91)
        return original(self, *args, **{**kwargs, 'fault': fault})

    Operations.submit = crash_after_commit
    runtime.schedule(run)
    await runtime.tasks[run['id']]
    raise AssertionError('The after_commit injection was not reached')


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))
