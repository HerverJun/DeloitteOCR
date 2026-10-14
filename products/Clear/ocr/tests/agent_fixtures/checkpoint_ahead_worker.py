"""Exit after the official saver advances generation, before resume UI events."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.documents import Documents
from ocr_workbench.store import Store


async def main(root, project_id, run_id):
    business = Store(root)
    services = ApplicationServices(business, Documents(business, Path(root) / 'bundle', None),
                                   SimpleNamespace(guard=business.file_lock))

    async def no_model(state, run):
        raise AssertionError('No model request belongs in this fault window')

    runtime = await AgentRuntime(services, policy={'limits': {}},
        connection=SimpleNamespace(assert_current=lambda revision: None), model_override=no_model).open()
    original = runtime.agent.transition

    def crash_before_projection(project, key, generation, status, **kwargs):
        if key == run_id and status == 'queued' and kwargs.get('event_key') == f'{run_id}:{generation}:resume':
            # _resume_interrupted already awaited graph.aupdate_state against
            # the real SQLite saver. No business resume transition was made.
            os._exit(93)
        return original(project, key, generation, status, **kwargs)

    runtime.agent.transition = crash_before_projection
    run = runtime.agent.run(project_id, run_id)
    await runtime.resume_interrupted(project_id, run_id, run['generation'])
    raise AssertionError('Checkpoint-ahead crash point was not reached')


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))
