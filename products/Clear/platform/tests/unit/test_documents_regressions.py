"""A native heading must not hide scanned content on the same PDF page."""
from copy import deepcopy
from contextlib import closing
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch
import pytest

from workbench_platform.documents.normalize import normalize
from workbench_platform.documents.service import Cancelled, DocumentService, reconcile_native_tables, duplicate_table_block, without_structured_html, readable_table_text
from workbench_platform.documents.tables import parse_tables
from workbench_platform.batches.store import encoded


def test_pdf_coverage_reads_native_and_mixed_pages_and_keeps_actual_model_source():
    source = {'asset_id': 'asset', 'asset_revision': 1, 'blob_hash': 'a' * 64,
              'original_name': 'mixed.pdf', 'media_type': 'application/pdf'}
    document = normalize({'pages': [
        {'page_idx': 0, 'blocks': [{'type': 'text', 'content': 'Native page'}]},
        {'page_idx': 1, 'blocks': [{'type': 'text', 'content': 'Native heading'}]},
    ]}, source, 'document')
    # Both pages have native text. The previous implementation submitted none.
    assert not any(page['needs_ocr'] for page in document['pages'])
    adapter = Mock()
    adapter.import_status.return_value = {'items': [{'target_document_id': 'native-doc', 'page_count': 2}]}
    stages = [{'status': 'succeeded', 'page_number': number,
               'output': {'result_id': f'result-{number}'}} for number in (1, 2)]
    adapter.page_request.side_effect = [
        {'submitted': False}, {'submitted': True, 'stages': stages}, {'submitted': True, 'stages': stages}]
    adapter.snapshot.return_value = {'tasks': [{'id': f'task-{number}', 'result_id': f'result-{number}',
                                               'status': 'succeeded'} for number in (1, 2)]}
    adapter.results.return_value = [SimpleNamespace(native_result_id=f'result-{number}',
        image_id=f'image-{number}', version_id=f'version-{number}') for number in (1, 2)]
    adapter.result_revision.return_value = 0
    native = {'engine': 'pdf-native', 'text': 'Native page',
              'document': {'page_class': 'native', 'ocr_sources': []}}
    mixed = {'engine': 'document', 'text': 'Native heading\nTotal 1,214.50',
             'model_revisions': {'MinerU2.5-Pro-2605-1.2B': 'pinned-model'},
             'document': {'page_class': 'mixed', 'ocr_sources': ['mineru_pro'],
                          'engine_routes': [{'requested': 'auto', 'actual': 'mineru_pro'}]},
             'blocks': [{'text': 'Native heading', 'source': 'pdf-native'},
                        {'text': 'Total 1,214.50', 'source_kind': 'ocr', 'source': 'mineru_pro'}]}
    adapter.result_snapshot.side_effect = [{'original': value, 'edited': {'text': value['text']}}
                                          for value in (native, mixed)]
    table = {'rows': 1, 'columns': 1, 'cells': [{'row': 0, 'column': 0, 'row_span': 1,
             'column_span': 1, 'text': '1,214.50', 'polygon': None, 'confidence': None}]}
    adapter.result_tables.side_effect = [{'tables': [], 'content_sha256': 'b' * 64},
                                         {'tables': [table], 'content_sha256': 'c' * 64}]
    handoff, sets = Mock(), Mock()
    handoff.begin.return_value = {'handoff_id': 'handoff'}
    handoff._row.return_value = {'request_id': 'import'}
    handoff._adapter.return_value = adapter
    sets.create.return_value = SimpleNamespace(asset_set_id='set', revision=1)
    service = object.__new__(DocumentService)
    service.app = SimpleNamespace(state=SimpleNamespace(asset_library=(None, sets), ocr_handoff=handoff))
    service.context = lambda _: SimpleNamespace(workspace_id='workspace')
    service.update, service.current = Mock(), Mock()
    service.stop = threading.Event()
    value = {**source, 'workspace_id': 'workspace', 'document_id': 'document', 'generation': 1, 'ocr_link_id': None,
             'source': source}
    with patch('workbench_platform.application_services.native.prepare',
               return_value={'requires_project_selection': False, 'link_id': 'link'}):
        service.ocr_pages(value, document)
    submitted = adapter.page_request.call_args_list[1].kwargs['body']
    assert submitted == {'page_numbers': [1, 2], 'mode': 'auto', 'engine': 'auto', 'force': False}
    assert [block['text'] for block in document['pages'][0]['blocks']] == ['Native page']
    page = document['pages'][1]
    assert [block['text'] for block in page['blocks'] if block['type'] == 'text'] == ['Native heading', 'Total 1,214.50']
    assert page['engine'] == 'mineru_pro' and not page['needs_ocr']
    saved = document['tables'][0]
    assert saved['model_revisions'] == mixed['model_revisions']
    assert saved['cells'][0]['polygon'] is None
    assert saved['cells'][0]['source']['page_number'] == 2
    assert saved['cells'][0]['source']['native_result_id'] == 'result-2'
    # XLSX provenance uses the actual model, not the old generic clear-ocr name.
    service.get = Mock(return_value={**value})
    service.table = Mock(return_value=deepcopy(saved))
    with patch('workbench_platform.documents.service.export_xlsx') as export:
        service.xlsx('workspace', 'document', saved['table_id'])
    record = export.call_args.kwargs['source_rows'][0]
    assert record['engine'] == 'mineru_pro'
    assert record['page_number'] == 2 and record['input_sha256'] == source['blob_hash']
    assert 'pinned-model' in record['model_revisions']
    # A native stage can fail without raising through the polling API. Keep
    # the missing-page flag and publish its safe phase/error instead of hiding
    # the cause behind a generic pending-coverage warning.
    failed_document = normalize({'pages': [{'page_idx': 0, 'blocks': [{'type': 'text', 'content': 'Native page'}]}]}, source, 'document')
    failed_document['pages'][0]['needs_ocr'] = True
    adapter.import_status.return_value = {'items': [{'target_document_id': 'native-doc', 'page_count': 1}]}
    failed = [{'status': 'failed', 'page_number': 1, 'phase': '区域识别未完成',
               'error': '引擎加载失败。 api_key=private-value model=F:\\private\\weights'}]
    adapter.page_request.side_effect = [
        {'submitted': False}, {'submitted': True, 'stages': failed}, {'submitted': True, 'stages': failed}]
    adapter.snapshot.return_value = {'tasks': []}
    with patch('workbench_platform.application_services.native.prepare',
               return_value={'requires_project_selection': False, 'link_id': 'link'}):
        service.ocr_pages(value, failed_document)
    page = failed_document['pages'][0]
    assert page['needs_ocr'] and page['coverage_check'] == 'failed'
    assert page['ocr_error']['phase'] == '区域识别未完成'
    warning = '\n'.join(failed_document['warnings'])
    assert '第 1 页' in warning and '引擎加载失败' in warning and '区域识别未完成' in warning
    assert 'private-value' not in warning and 'F:\\private' not in warning


def test_document_resume_prepares_a_fresh_launch_and_reuses_the_linked_project():
    from workbench_platform.application_services.native import prepare
    from workbench_platform.catalog.launch import LaunchStatus
    launch, workspace, jobs, projects = Mock(), Mock(), Mock(), Mock()
    launch.current.side_effect = LaunchStatus(404, 'NOT_FOUND')
    launch.start.side_effect = lambda app, key: (SimpleNamespace(instance_id=key), False)
    workspace.get_workspace.return_value = SimpleNamespace(name='Original workspace')
    workspace.list_links.return_value = [SimpleNamespace(app_id='ocr', link_id='original-link',
        module_project_id='original-project', role='owner')]
    projects.projects.return_value = {'items': [{'project_id': 'original-project'}]}
    jobs._adapter.return_value._request.return_value = {'engines': {'auto': {'ready': True}}}
    jobs._adapter.return_value.capabilities.return_value = SimpleNamespace()
    service = object.__new__(DocumentService)
    service.app = SimpleNamespace(state=SimpleNamespace(launch_service=launch, workspace_store=workspace, job_service=jobs))
    original = {'workspace_id': 'workspace', 'document_id': 'document', 'asset_id': 'asset', 'generation': 1}
    resumed = {**original, 'generation': 2}
    first, second = service.context(original), service.context(resumed)
    assert first.operation_id != second.operation_id
    assert service.context(resumed).operation_id == second.operation_id
    with patch('workbench_platform.application_services.native.availability', return_value={'state': 'preparable'}), \
         patch('workbench_platform.application_services.native.OcrProjects', return_value=projects), \
         patch('workbench_platform.application_services.native.asdict', return_value={}):
        before, after = prepare(first, 'ocr'), prepare(second, 'ocr')
    assert before['instance_id'] != after['instance_id']
    assert launch.start.call_count == 2
    assert launch.start.call_args_list[0].args[1] != launch.start.call_args_list[1].args[1]
    assert before['project_id'] == after['project_id'] == 'original-project'
    assert before['link_id'] == after['link_id'] == 'original-link'
    projects.create.assert_not_called()


def test_equal_table_coalescing_requires_the_same_source_structure_and_region():
    source = {'asset_id': 'asset', 'asset_revision': 1, 'blob_hash': 'a' * 64,
              'original_name': 'native.pdf'}
    html = '<table><tr><td>Account</td><td>Amount (CNY)</td></tr><tr><td>001234567890</td><td>1,234.50</td></tr></table>'
    native = normalize({'pages': [{'page_idx': 0, 'blocks': [{'type': 'table', 'bbox': [.084, .121, .874, .264],
        'content': [{'type': 'table_body', 'content': html}]}]}]}, source, 'document')
    candidate = parse_tables(html)[0]
    candidate['polygon'] = [[200.088, 420.014], [2176.901, 420.014], [2176.901, 927.668], [200.088, 927.668]]
    image, identity = {'width': 2480, 'height': 3508}, {'id': 'mineru_pro'}
    frozen = deepcopy(candidate)
    kept, duplicates = reconcile_native_tables(native, 1, [candidate], image, source, identity)
    assert not kept and len(duplicates) == 1 and candidate == frozen
    assert len(native['tables']) == 1 and native['tables'][0]['ocr_confirmations'][0]['engine_identity'] == identity
    assert all(cell['polygon'] is None for cell in native['tables'][0]['cells'])
    block = {'kind': 'table', 'text': html, 'polygon': candidate['polygon']}
    assert duplicate_table_block(block, duplicates, image)
    assert not duplicate_table_block({**block, 'text': 'Supplementary text ' + html}, duplicates, image)
    shifted = deepcopy(candidate)
    shifted['polygon'] = [[x, y + 1400] for x, y in shifted['polygon']]
    modified = deepcopy(candidate)
    modified['cells'][2]['text'] = '009999999999'
    for item, dimensions, page, original in [(shifted, image, 1, source), (modified, image, 1, source),
            (candidate, {}, 1, source), (candidate, image, 2, source),
            (candidate, image, 1, {**source, 'blob_hash': 'b' * 64})]:
        retained, repeated = reconcile_native_tables(deepcopy(native), page, [item], dimensions, original, identity)
        assert len(retained) == 1 and not repeated
    # A genuine second copy elsewhere on the page survives the same batch.
    retained, repeated = reconcile_native_tables(deepcopy(native), 1, [candidate, shifted], image, source, identity)
    assert len(retained) == 1 and retained[0][1] == shifted and len(repeated) == 1
    assert not duplicate_table_block({**block, 'polygon': shifted['polygon']}, duplicates, image)


@pytest.mark.parametrize('suffix', ['png', 'pdf'])
def test_cancel_then_immediate_resume_cancels_the_old_accepted_native_child(tmp_path, suffix):
    sets, handoff, adapter, jobs, workspace = Mock(), Mock(), Mock(), Mock(), Mock()
    sets.create.return_value = SimpleNamespace(asset_set_id='set', revision=1)
    handoff.begin.return_value = {'handoff_id': 'handoff'}
    handoff._row.return_value = {'request_id': 'import'}
    handoff._adapter.return_value = adapter
    adapter.import_status.return_value = {'items': [{'target_document_id': 'native-document', 'page_count': 1}]}
    workspace.get_workspace.return_value = SimpleNamespace(name='Original workspace')
    app = SimpleNamespace(state=SimpleNamespace(asset_library=(Mock(), sets), ocr_handoff=handoff,
        job_service=jobs, workspace_store=workspace))
    service = DocumentService(app, tmp_path / 'documents')
    service.executor.shutdown(wait=True)
    service.executor = Mock()
    service.execution.generation = 1
    source = {'asset_id': 'asset', 'asset_revision': 1, 'blob_hash': 'a' * 64, 'original_name': 'debug.' + suffix}
    value = {**source, 'source': source, 'workspace_id': 'workspace', 'document_id': 'document', 'generation': 1,
             'status': 'running', 'stage': 'ocr', 'ocr_link_id': None, 'request_id': 'request'}
    with closing(service.db()) as db:
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?)', ('document', 'workspace', 'request', 'b' * 64, encoded(value)))
    accepted = {'running': False, 'cancelled': False}
    def accept():
        accepted['running'] = True
        assert service.control('workspace', 'document', 'cancel')['status'] == 'cancelled'
        assert service.control('workspace', 'document', 'resume')['generation'] == 2
    def cancel(*_, **__):
        accepted.update(running=False, cancelled=True)
    def submit(*_, **__):
        accept()
        return {'job': {'platform_job_id': 'old-native-job'}}
    def page_request(*_, body=None, action=None):
        if action == 'cancel':
            cancel()
            return {'submitted': True, 'stages': []}
        if body is None:
            return {'submitted': False}
        accept()
        return {'submitted': True, 'stages': [{'status': 'running', 'page_number': 1}]}
    handoff.submit.side_effect, adapter.page_request.side_effect, jobs.action.side_effect = submit, page_request, cancel
    document = normalize({'pages': [{'page_idx': 0, 'blocks': []}]}, source, 'document')
    try:
        with patch('workbench_platform.application_services.native.prepare',
                   return_value={'requires_project_selection': False, 'link_id': 'link'}), pytest.raises(Cancelled):
            service.ocr_pages(value, document)
        current = service.get('workspace', 'document')
        assert current['status'] == 'queued' and current['generation'] == 2
        assert accepted == {'running': False, 'cancelled': True}
        service.executor.submit.assert_called_once()
        if suffix == 'png':
            jobs.action.assert_called_once_with('workspace', 'old-native-job', 'cancel')
        else:
            assert adapter.page_request.call_args.kwargs['action'] == 'cancel'
    finally:
        service.close()


def test_structured_table_projection_keeps_caption_every_cell_and_surrounding_text():
    rows = ''.join(f'<tr><td>编号-{number:03}</td><td>{number}.25</td></tr>' for number in range(40))
    html = '<table><caption>币种 CNY · 完整明细</caption>' + rows + '</table>'
    original = '页眉单位 CNY\n' + html + '\n脚注：括号为负数。'
    table = parse_tables(original)[0]
    before = deepcopy(table)
    body = without_structured_html(original, [table])
    readable = readable_table_text(table)
    assert '<table' not in body and '<td' not in body
    assert '页眉单位 CNY' in body and '脚注：括号为负数。' in body
    assert table['caption'] in readable
    assert '编号-039' in readable and '39.25' in readable
    assert all(cell['text'] in readable for cell in table['cells'])
    assert before == table
    # Unrepresented HTML and literal code remain available rather than being
    # stripped under the assumption that a structural table exists.
    assert without_structured_html(original, []) == original
    literal = '```html\n' + html + '\n```'
    assert without_structured_html(literal, [table]) == literal
