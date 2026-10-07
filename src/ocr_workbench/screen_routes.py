"""Authenticated native API shared by the ordinary workbench and platform Agent."""
from fastapi.responses import FileResponse


def register_screen_routes(app, manager, require_recognition):
    @app.get('/api/projects/{project_id}/screen-batches')
    def collection(project_id: str):
        return manager.list(project_id)

    @app.post('/api/projects/{project_id}/screen-batches')
    def submit(project_id: str, body: dict):
        require_recognition()
        return manager.submit(project_id, body)

    @app.get('/api/projects/{project_id}/screen-batch-requests/{request_id}')
    def lookup(project_id: str, request_id: str):
        return manager.lookup(project_id, request_id)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}')
    def status(project_id: str, batch_id: str, offset: int = 0, limit: int = 30, status: str | None = None):
        return manager.get(project_id, batch_id, offset=offset, limit=limit, status=status)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/items/{version_id}')
    def detail(project_id: str, batch_id: str, version_id: str):
        return manager.detail(project_id, batch_id, version_id)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/items/{version_id}/files/{file_id}')
    def image(project_id: str, batch_id: str, version_id: str, file_id: str):
        return FileResponse(manager.file(project_id, batch_id, version_id, file_id), media_type='image/png')

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/action')
    def action(project_id: str, batch_id: str, body: dict):
        if body.get('action') in {'resume', 'retry_failed', 'upgrade'}:
            require_recognition()
        return manager.action(project_id, batch_id, body)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/export')
    def export(project_id: str, batch_id: str, body: dict):
        path, _ = manager.export(project_id, batch_id, body)
        return FileResponse(path, media_type='application/zip', filename='screen-tables.zip')
