"""Structure review routes share the service authentication and revision contract."""
from ocr_workbench.editing import present_result


def register_structure_routes(app, store):
    from ocr_workbench.structure_store import structure_view, refresh_proposals, decide_structure
    from ocr_workbench.document_review import document_review_queue, refresh_document_review

    @app.get('/api/results/{key}/structure')
    def view(key: str):
        return structure_view(store, key)

    @app.post('/api/results/{key}/structure/check')
    def check(key: str, body: dict):
        return refresh_proposals(store, key, body.get('revision'))

    @app.post('/api/results/{key}/structure/tool-retry')
    def retry_tool(key: str, body: dict):
        from ocr_workbench.table_tool import enqueue
        return enqueue(app.state.documents, key, body.get('revision'))

    @app.post('/api/results/{key}/structure/{proposal_id}/decision')
    def decide(key: str, proposal_id: str, body: dict):
        return present_result(decide_structure(store, key, proposal_id, body))

    @app.get('/api/documents/{key}/review-queue')
    def queue(key: str, offset: int = 0, limit: int = 50, state: str = 'open'):
        return document_review_queue(store, key, offset=offset, limit=limit, state=state)

    @app.post('/api/documents/{key}/review-queue/check')
    def check_document(key: str):
        return refresh_document_review(store, key)
