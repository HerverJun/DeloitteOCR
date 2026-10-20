"""HTTP stop keeps ownership of the controller's asyncio event loop."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from workbench_platform.agent import routes


def test_stop_route_signals_the_owned_harness_on_its_event_loop():
    observed = []
    def cancel(run_id, native):
        observed.append(asyncio.get_running_loop())
        return {'id': run_id, 'status': 'cancelled'}
    async def parsed(*args):
        return SimpleNamespace(cancel_owned_jobs=False)
    async def scenario():
        loop = asyncio.get_running_loop()
        with patch.object(routes, 'runtime', return_value=SimpleNamespace(cancel=cancel)), \
             patch.object(routes, 'run_scope'), patch.object(routes, 'parsed', parsed):
            assert (await routes.cancel('run', SimpleNamespace()))['status'] == 'cancelled'
        assert observed == [loop]
    asyncio.run(scenario())
