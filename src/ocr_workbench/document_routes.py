"""Authenticated document endpoints registered under the existing service middleware."""
import json
from pathlib import Path
import shutil

from fastapi import UploadFile, File, Form
from starlette.concurrency import run_in_threadpool
from ocr_workbench.coordinates import pdf_transform
from ocr_workbench.store import uid, now


from ocr_workbench.application_services import ApplicationServices, _search_fields, _search_snippet


def register_document_routes(app, documents, maintenance, services=None):
    store = documents.store
    services = services or ApplicationServices(store, documents, maintenance)

    @app.post('/api/projects/{key}/documents')
    async def import_documents(key: str, files: list[UploadFile] = File(default=[]),
                               dpi: float = Form(300), password: str | None = Form(None),
                               image_ids: str | None = Form(None)):
        store.one('projects', key)
        if len(files) > 1000:
            raise ValueError('每次最多导入 1000 个文档')
        imported, errors = [], []
        if image_ids:
            keys = json.loads(image_ids)
            if not isinstance(keys, list) or len(keys) > 1000 or any(not isinstance(i, str) for i in keys):
                raise ValueError('现有图片列表无效')
            for image_id in keys:
                image = store.one('images', image_id)
                if image['project_id'] != key:
                    raise ValueError('图片不属于当前项目')
                page = store.page_for_version(image['active_version'])
                imported.append(store.one('documents', page['document_id']))
        inbox = store.root / 'inbox'
        inbox.mkdir(exist_ok=True)
        for upload in files:
            path = inbox / uid()
            name = (upload.filename or '文档').replace('\\', '/').split('/')[-1]
            try:
                count = 0
                with path.open('wb') as stream:
                    while chunk := await upload.read(1024*1024):
                        count += len(chunk)
                        if count > 2*1024**3 or shutil.disk_usage(inbox).free < 512*1024**2:
                            raise ValueError('文档超过 2 GB 或项目磁盘空间不足')
                        stream.write(chunk)
                imported.append(await run_in_threadpool(documents.import_document, key, name, path, dpi, password))
            except Exception as error:
                message = str(error)
                if password:
                    message = message.replace(password, '[redacted]')
                errors.append({'name': name, 'message': message})
            finally:
                path.unlink(missing_ok=True)
                await upload.close()
        if not files and not image_ids:
            raise ValueError('请选择 PDF、TIFF 或图片')
        return {'documents': imported, 'errors': errors}

    @app.get('/api/documents/{key}/pages')
    def pages(key: str, offset: int = 0, limit: int = 50):
        doc = store.one('documents', key)
        return {'document': doc, 'pages': store.document_pages(key, offset, limit),
                'offset': offset, 'limit': limit, 'total': doc['page_count']}

    @app.post('/api/documents/{key}/unlock')
    def unlock(key: str, body: dict):
        return documents.unlock(key, body.get('password'))

    @app.get('/api/documents/{key}/stages')
    def stages(key: str, offset: int = 0, limit: int = 100):
        store.one('documents', key)
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError('处理阶段分页范围无效')
        return {'stages': store.rows('SELECT s.*,p.page_number FROM document_stages s JOIN pages p ON p.id=s.page_id WHERE p.document_id=? ORDER BY s.created DESC LIMIT ? OFFSET ?', (key, limit, offset))}

    @app.post('/api/documents/{key}/process')
    def process_document(key: str, body: dict):
        return services.process_document(key, body)

    @app.post('/api/documents/{key}/queue/{action}')
    def action(key: str, action: str):
        return {'stage_ids': documents.action(key, action)}

    @app.post('/api/pages/{key}/render')
    def render(key: str, body: dict):
        page = store.one('pages', key)
        if 'dpi' in body:
            dpi = body['dpi']
            pdf_transform([0, 0, 100, 100], dpi=dpi)
            if page['image_id'] and dpi != page['render_dpi']:
                raise ValueError('此页已展开；如需不同 DPI，请重新导入文档以保留版本追溯')
        stage_id = store.enqueue_document_stage(key, 'render', {'dpi': body.get('dpi', page['render_dpi'])})
        documents.wake.set()
        return {'stage_id': stage_id}

    @app.post('/api/pages/{key}/process')
    def process(key: str, body: dict):
        if type(body.get('force', False)) is not bool:
            raise ValueError('重新处理选项必须为布尔值')
        return {'stage_id': documents.process(key, body.get('mode', 'auto'), force=body.get('force', False), engine=body.get('engine', 'ppocr'))}

    @app.get('/api/pages/{key}/regions')
    def regions(key: str, version_id: str | None = None):
        page = store.one('pages', key)
        rows = store.page_regions(key, version_id)
        return {'page_id': page['id'], 'regions': rows}

    @app.post('/api/pages/{key}/regions')
    def add_region(key: str, body: dict):
        if body.get('kind') not in ('table', 'ocr-needed', 'manual'):
            raise ValueError('无效的区域类型')
        region_id = store.add_region(key, body.get('version_id'), body['kind'], body.get('polygon'), 'manual')
        return {'region_id': region_id}

    @app.get('/api/documents/{key}/search')
    def search(key: str, q: str, offset: int = 0, limit: int = 50):
        return services.search_document(key, q, offset, limit)
