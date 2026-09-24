"""A download owns a renewable lease until response completion or disconnect."""
import asyncio
from fastapi.responses import FileResponse


class LeasedFileResponse(FileResponse):
    def __init__(self, artifacts, lease_id, target, *, filename=None):
        super().__init__(target, filename=filename or target.name)
        self.artifacts, self.lease_id = artifacts, lease_id

    async def __call__(self, scope, receive, send):
        async def renew():
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(self.artifacts.renew, self.lease_id)
        task = asyncio.create_task(renew())
        stream = None
        try:
            # Keep streaming under this response's lifetime, including a slow
            # client, instead of delegating an unobservable sendfile extension.
            scope = {**scope, 'extensions': {k: v for k, v in scope.get('extensions', {}).items() if k != 'http.response.pathsend'}}
            stream = asyncio.create_task(super().__call__(scope, receive, send))
            finished, _ = await asyncio.wait({task, stream}, return_when=asyncio.FIRST_COMPLETED)
            if task in finished:
                task.result()
            await stream
        finally:
            task.cancel()
            if stream and not stream.done():
                stream.cancel()
            await asyncio.gather(task, *([stream] if stream else []), return_exceptions=True)
            await asyncio.to_thread(self.artifacts.release, self.lease_id)
