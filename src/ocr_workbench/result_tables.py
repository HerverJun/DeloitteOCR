"""Read-only revision-bound tables, retaining candidate/adoption distinctions."""
from ocr_workbench.document_store import fingerprint


def table_snapshot(store, result_id, revision):
    snapshot = store.result_snapshot(result_id, revision)
    if not snapshot['available']:
        raise ValueError('识别修订不可读')
    raw, edited = snapshot['original'], snapshot['edited']
    tables = edited.get('tables') or []
    source = 'revision'
    warnings = []
    document = raw.get('document') or {}
    if not tables and document.get('table_structure'):
        from ocr_workbench.native_tables import native_table_preview
        image = raw.get('image') or store.one('versions', snapshot['version_id'])
        preview = native_table_preview(raw, document['table_structure'], image['width'], image['height'])
        tables = preview['tables'] if preview else []
        source = 'pdf-native-candidate' if tables else 'none'
        if tables:
            warnings.append('表格来自已提取原生文字与锁定几何候选，尚未人工采用')
    if document.get('table_structure_error'):
        warnings.append('PDF表格组件提取失败，页面文字不代表结构表已完成')
    if document.get('native_only_incomplete') or document.get('unprocessed_regions'):
        warnings.append('本页仍有未处理区域，不能宣称完整内容覆盖')
    content = {'id': result_id, 'revision': revision, 'original': raw, 'edited': edited,
               'table_projection_version': 1, 'tables': tables, 'source': source}
    return {'id': result_id, 'project_id': snapshot['project_id'], 'revision': revision,
            'content_sha256': fingerprint(content), 'tables_sha256': fingerprint(tables),
            'source': source, 'tables': tables, 'warnings': warnings}
