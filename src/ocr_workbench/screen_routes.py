"""Authenticated native API shared by the ordinary workbench and platform Agent."""
from fastapi.responses import FileResponse, Response


def register_screen_routes(app, manager, require_recognition):
    from ocr_workbench.screen_local_candidates import submit_local_candidates, read_local_candidates, control_local_candidates, local_candidate_file
    from ocr_workbench.screen_cell_review import list_groups, preview, confirm

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/cell-groups')
    def cell_groups(project_id: str, batch_id: str, offset: int = 0, limit: int = 8):
        return list_groups(manager, project_id, batch_id, offset=offset, limit=limit)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/cell-groups/{group_id}/image')
    def cell_preview(project_id: str, batch_id: str, group_id: str):
        return Response(preview(manager, project_id, batch_id, group_id), media_type='image/png',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/cell-confirmations')
    def cell_confirm(project_id: str, batch_id: str, body: dict):
        return confirm(manager, project_id, batch_id, body)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/cell-confirmations/lookup')
    def cell_lookup(project_id: str, batch_id: str, body: dict):
        return confirm(manager, project_id, batch_id, body, allow_new=False)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates')
    def local_submit(project_id: str, batch_id: str, body: dict):
        require_recognition()
        return submit_local_candidates(manager, project_id, batch_id, body)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates/lookup')
    def local_lookup(project_id: str, batch_id: str, body: dict):
        return submit_local_candidates(manager, project_id, batch_id, body, allow_new=False)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates/{request_id}')
    def local_read(project_id: str, batch_id: str, request_id: str, offset: int = 0, limit: int = 12):
        return read_local_candidates(manager, project_id, batch_id, request_id, offset=offset, limit=limit)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates/{request_id}/control')
    def local_control(project_id: str, batch_id: str, request_id: str, body: dict):
        if body.get('action') in {'resume', 'retry_failed'}:
            require_recognition()
        return control_local_candidates(manager, project_id, batch_id, request_id, body)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates/{request_id}/control-lookup')
    def local_control_lookup(project_id: str, batch_id: str, request_id: str, body: dict):
        return control_local_candidates(manager, project_id, batch_id, request_id, body, allow_new=False)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates/{request_id}/tasks/{task_id}/files/{file_id}')
    def local_file(project_id: str, batch_id: str, request_id: str, task_id: str, file_id: str):
        path = local_candidate_file(manager, project_id, batch_id, request_id, task_id, file_id)
        media_type = 'image/png' if file_id.endswith('.png') else 'application/json' if file_id.endswith('.json') else 'text/plain; charset=utf-8'
        return FileResponse(path, media_type=media_type, filename=file_id)

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

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/items/{version_id}/compare')
    def compare(project_id: str, batch_id: str, version_id: str, body: dict):
        return manager.compare(project_id, batch_id, version_id, body)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/delivery-snapshot')
    def delivery_snapshot(project_id: str, batch_id: str):
        return manager.delivery_snapshot(project_id, batch_id)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/resolve')
    def resolve(project_id: str, batch_id: str, body: dict):
        require_recognition()
        return manager.resolve(project_id, batch_id, body)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/resolve-lookup')
    def resolve_lookup(project_id: str, batch_id: str, body: dict):
        return manager.resolve(project_id, batch_id, body, allow_new=False)

    @app.get('/api/projects/{project_id}/screen-batches/{batch_id}/items/{version_id}/files/{file_id}')
    def image(project_id: str, batch_id: str, version_id: str, file_id: str):
        return FileResponse(manager.file(project_id, batch_id, version_id, file_id), media_type='image/png')

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/action')
    def action(project_id: str, batch_id: str, body: dict):
        if body.get('action') in {'resume', 'retry_failed', 'upgrade'}:
            require_recognition()
        return manager.action(project_id, batch_id, body)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/action-lookup')
    def action_lookup(project_id: str, batch_id: str, body: dict):
        return manager.action(project_id, batch_id, body, allow_new=False)

    @app.post('/api/projects/{project_id}/screen-batches/{batch_id}/export')
    def export(project_id: str, batch_id: str, body: dict):
        path, _ = manager.export(project_id, batch_id, body)
        return FileResponse(path, media_type='application/zip', filename='screen-tables.zip')
