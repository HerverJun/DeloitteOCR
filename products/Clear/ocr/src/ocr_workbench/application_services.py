"""Shared UI/agent business entrypoints; independent of HTTP and graph state."""
from __future__ import annotations

import json


class ApplicationServices:
    def __init__(self, store, documents, maintenance, *, queue=None, fusion_queue=None,
                 registry=None, bundle=None, require_recognition=None, external_connection=None, external_queue=None, queues_started=False):
        self.store, self.documents, self.maintenance = store, documents, maintenance
        self.queue, self.fusion_queue, self.registry, self.bundle = queue, fusion_queue, registry, bundle
        self.require_recognition = require_recognition
        self.external_connection, self.external_queue, self.queues_started = external_connection, external_queue, queues_started
        from ocr_workbench.multimodal_runtime import load_config, review_readiness
        self._visual_load_config, self._visual_readiness = load_config, review_readiness

    def visual_parameters(self, model_id):
        if isinstance(model_id, str) and model_id.startswith('external:'):
            if self.external_connection is None:
                raise ValueError('外部视觉连接不可用')
            config, _ = self.external_connection.resolve(model_id)
            queue = self.external_queue
        else:
            if self.require_recognition is None:
                raise ValueError('本工作台未启用本地视觉模型')
            self.require_recognition()
            config = self._visual_load_config(self.bundle, model_id)
            ready = self._visual_readiness(self.bundle, config)
            if not ready['ready']:
                raise ValueError(ready.get('reason') or '视觉模型尚未就绪')
            queue = self.queue
        if queue is None or self.queues_started and not queue.status()['healthy']:
            raise ValueError('视觉审校队列不可用或正在恢复')
        return config, queue

    def submit_visual_review(self, result_id, body):
        from ocr_workbench.multimodal_store import lookup_existing_review, enqueue_review
        existing = lookup_existing_review(self.store, result_id, body)
        if existing is not None:
            return {'task_id': existing}
        config, queue = self.visual_parameters(body.get('model_id'))
        with self.maintenance.guard:
            task_id = enqueue_review(self.store, result_id, body, config)
        queue.wake.set()
        return {'task_id': task_id}

    def submit_ocr(self, project_id, body):
        if self.require_recognition is None:
            raise ValueError("此服务未启用识别入口")
        self.require_recognition()
        from ocr_workbench.fusion import load_policy
        fusion = body.get("fusion")
        policy = load_policy(self.bundle, fusion.get("content_type", "table"), fusion.get("mode", "conservative")) if isinstance(fusion, dict) else None
        with self.maintenance.guard:
            ids = self.store.enqueue(project_id, body.get("version_ids", []), body.get("engines", []),
                                     body.get("preprocess", []), {name: spec["package_id"] for name, spec in self.registry.engines().items()},
                                     fusion_policy=policy, request_id=body.get("request_id"))
        self.queue.wake.set()
        self.fusion_queue.wake.set()
        return {"task_ids": ids}

    def process_document(self, document_id, body):
        with self.maintenance.guard:
            doc = self.store.one("documents", document_id)
            if not doc["page_count"]:
                raise ValueError("请先解锁文档")
            numbers = body.get("page_numbers")
            if numbers is not None and (not isinstance(numbers, list) or not numbers or len(numbers) > 1000 or
                                       any(type(n) is not int or not 1 <= n <= doc["page_count"] for n in numbers)):
                raise ValueError("页面范围无效；每批可显式选择最多 1000 页")
            pages = self.store.rows("SELECT id,page_number FROM pages WHERE document_id=? ORDER BY page_number", (document_id,))
            page_ids = [p["id"] for p in pages if numbers is None or p["page_number"] in numbers]
            return {"stage_ids": self.documents.process_pages(page_ids, body.get("mode", "auto"), force=body.get("force", False), engine=body.get("engine", "ppocr"))}

    def export(self, body):
        with self.maintenance.guard:
            if body.get("format") == "pdf":
                from ocr_workbench.pdf_export import build_pdf_export
                return build_pdf_export(self.store, self.documents, body)
            from ocr_workbench.exporting import build_export
            return build_export(self.store, body.get("result_ids", []), body.get("format"), body.get("aggregate", False),
                                confirmed_only=body.get("confirmed_only", False), expected_results=body.get("expected_results"))

    def search_document(self, key, q, offset=0, limit=50):
        self.store.one("documents", key)
        if not 1 <= len(q) <= 200 or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("请输入 1–200 字的搜索词；每次最多返回 100 个结果")
        matches, total, needle = [], 0, q.casefold()
        with self.store.transaction() as db:
            db.execute("BEGIN")
            rows = db.execute("""SELECT p.id page_id,p.page_number,p.image_id,r.id result_id,r.revision,r.edited
                FROM pages p JOIN selections s ON s.image_id=p.image_id JOIN results r ON r.id=s.result_id
                WHERE p.document_id=? ORDER BY p.page_number""", (key,))
            for record in rows:
                row = dict(record)
                edit = json.loads(row.pop("edited"))
                for target, text in _search_fields(edit):
                    snippet = _search_snippet(text, needle)
                    if snippet is not None:
                        if offset <= total < offset + limit:
                            matches.append({**row, "target": target, "snippet": snippet})
                        total += 1
        return {"matches": matches, "total": total, "offset": offset}


def _search_fields(edit):
    """Search the same saved text/table fields displayed by the text editor."""
    from ocr_workbench.editing import table_bindings, validate_edit

    parsed, replacements, _ = table_bindings(edit)
    body, retained, offset = [], [], 0
    for index, table in enumerate(parsed):
        try:
            validate_edit({'text': '', 'tables': [table]})
        except ValueError:
            continue  # Unusable model structure stays literal in the editor.
        source = table['source']
        body.extend((edit['text'][offset:source['start']], '\n'))
        offset = source['end']
        if index not in replacements:
            retained.append(table)
    body.append(edit['text'][offset:])
    yield 'text', ''.join(body)
    for index, table in enumerate(edit['tables']):
        if table.get('caption'):
            yield f'caption:{index}', table['caption']
        for cell in table['cells']:
            yield f"table:{index}:{cell['row']}:{cell['column']}", cell['text']
    for table in retained:
        # Removing an editable structure still retains its source text on screen.
        if table.get('caption'):
            yield 'text', table['caption']
        for cell in table['cells']:
            yield 'text', cell['text']


def _search_snippet(text, needle):
    position = text.casefold().find(needle)
    if position < 0:
        return None
    # Case folding can expand a character (e.g. ß -> ss). Map the match back
    # before slicing the original string so snippets still contain the match.
    folded_offset, start, end = 0, 0, len(text)
    for index, character in enumerate(text):
        following = folded_offset + len(character.casefold())
        if folded_offset <= position < following:
            start = index
        if following >= position + len(needle):
            end = index + 1
            break
        folded_offset = following
    return text[max(0, start-30):end+80]
