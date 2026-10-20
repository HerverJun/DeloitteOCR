"""Catalog-backed ordinary chat goals; synthetic catalogs, real Data worker."""
import asyncio
import json
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from packages.contracts.direct_classification import PredictRequest
from workbench_platform.application_services.classification_action import select_existing_model
from workbench_platform.application_services.goal_fixed_actions import cleaning_steps, prediction_goal, choose_sheet


COLUMNS = [{'column_id': 'c1', 'header': '供应商编号'}, {'column_id': 'c2', 'header': '供应商名称'}]


def test_column_name_cannot_silently_choose_sheet_or_be_erased():
    columns = COLUMNS + [{'column_id': 'c3', 'header': '名称'}]
    sheets = [{'name': '供应商', 'columns': columns}, {'name': '其它', 'columns': columns}]
    goal = '清理供应商名称列首尾空格'
    with pytest.raises(ValueError, match='多个工作表'):
        choose_sheet(goal, sheets)
    sheet, retained = choose_sheet(goal, sheets[:1])
    assert retained == goal
    assert cleaning_steps(retained, sheet['columns']) == [{'step': {'operator': 'trim', 'column': 'c2'}}]


@pytest.mark.parametrize('qualifier', ['工作表「供应商」', '“供应商”工作表', '名为“供应商”的工作表'])
def test_explicit_sheet_qualifier_keeps_the_same_named_column_intact(qualifier):
    sheets = [{'name': '供应商', 'columns': COLUMNS}, {'name': '其它', 'columns': COLUMNS}]
    selected, goal = choose_sheet('清理' + qualifier + '中供应商名称列首尾空格', sheets)
    assert selected is sheets[0]
    assert cleaning_steps(goal, selected['columns']) == [{'step': {'operator': 'trim', 'column': 'c2'}}]


@pytest.mark.parametrize('goal', ['请清理表格中供应商名称列的前后空格，谢谢',
    '去除供应商名称列首尾空格', '把供应商名称列的两端空白去掉并导出CSV'])
def test_column_names_are_resolved_from_catalog_without_internal_ids(goal):
    assert cleaning_steps(goal, COLUMNS) == [{'step': {'operator': 'trim', 'column': 'c2'}}]


@pytest.mark.parametrize('goal', ['去除供应商名称列的所有空格', '供应商名称列去重',
    '清理供应商名称列首尾空格并解释差异', '清理供应商列首尾空格', '不要清理供应商名称列首尾空格'])
def test_qualifiers_unknown_columns_and_deduplication_are_not_silently_rewritten(goal):
    assert cleaning_steps(goal, COLUMNS) is None


def test_duplicate_queries_are_not_misrepresented_as_completed_and_ambiguous_header():
    assert cleaning_steps('按供应商编号列查找重复行', COLUMNS) is None
    assert cleaning_steps('清理供应商名称列首尾空格并按供应商编号列定位重复行', COLUMNS) is None
    with pytest.raises(ValueError, match='重复'):
        cleaning_steps('清理供应商名称列首尾空格', COLUMNS + [{'column_id': 'c3', 'header': '供应商名称'}])


@pytest.mark.parametrize('goal', ['用已有模型给所选图片分类', '请使用当前分类模型预测图片，谢谢',
                                 '使用模型“发票分类 V2”对所选图片分类'])
def test_existing_model_goals_keep_business_model_name(goal):
    intent = prediction_goal(goal, ['image/png'])
    assert intent['use_existing_model'] is True
    assert intent['model_name'] == ('发票分类 V2' if 'V2' in goal else None)


def test_existing_model_selection_never_guesses_between_models_or_trains():
    request = PredictRequest(request_id='request', use_existing_model=True)
    models = [{'id': 'old', 'name': 'Old'}, {'id': 'current', 'name': 'Current'}]
    assert select_existing_model(request, {'models': models, 'active_model_id': 'current'}) == 'current'
    with pytest.raises(ValueError, match='未自动启动训练'):
        select_existing_model(request, {'models': models})
    with pytest.raises(ValueError, match='未自动启动训练'):
        select_existing_model(request, {'models': []})
    named = PredictRequest(request_id='named', use_existing_model=True, model_name='Old')
    assert select_existing_model(named, {'models': models, 'active_model_id': 'current'}) == 'old'
    for goal in ('训练模型再分类图片', '用已有模型分类图片并解释差异', '用已有模型分类图片，不要训练'):
        assert prediction_goal(goal, ['image/png']) is None


def test_primary_chat_accepts_existing_model_goal_without_connection_or_internal_ids(tmp_path):
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.store import AgentStore
    from workbench_platform.agent.tasks import AuditTasks
    runtime = object.__new__(AgentRuntime)
    runtime.store = AgentStore(tmp_path / 'agent')
    asset = SimpleNamespace(asset_id='selectedImage', media_type='image/png', revision=1,
                            blob_hash='a' * 64, original_name='selected.png')
    link = SimpleNamespace(link_id='serverLink', app_id='classification', role='editor', module_project_id='nativeProject')
    runtime.services = SimpleNamespace(asset_library=(SimpleNamespace(get=lambda *_: asset), None),
        workspace_store=SimpleNamespace(list_links=lambda _: [link]))
    runtime.tasks, runtime.schedule = AuditTasks(runtime), Mock()
    runtime.maintenance, runtime.agent_enabled = {}, False
    runtime.services.agent = runtime
    session = runtime.store.create_session('project', 'Classify', ['selectedImage'], 'classify-session')
    goal = '用已有模型给所选图片分类'
    run = asyncio.run(runtime.submit(session['id'], goal, 'classify-chat'))
    assert run['driver'] == 'classification-predict-v1'
    saved = json.loads(run['driver_state'])
    assert saved['request']['model_id'] is None and saved['request']['link_id'] is None
    assert saved['request']['use_existing_model'] is True
    assert saved['resource_object'] == 'project:nativeProject'
    assert saved['request']['source_text'] == goal
    assert runtime.store.rows('SELECT id FROM model_requests') == []
    assert asyncio.run(runtime.submit(session['id'], goal, 'classify-chat'))['id'] == run['id']


def test_primary_chat_cleans_real_table_and_replays_without_model(tmp_path, monkeypatch):
    import test_data_tools as fixtures
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.tasks import AuditTasks
    from workbench_platform.application_services import data_action

    original = fixtures.RUN / 'fixtures' / 'suppliers-left.csv'
    task_root = tmp_path / 'real-data'
    (task_root / 'fixtures').mkdir(parents=True)
    shutil.copyfile(original, task_root / 'fixtures' / original.name)
    monkeypatch.setattr(fixtures, 'RUN', task_root)
    fixture = fixtures.DataToolTests(methodName='test_real_clean_compare_export_independent_values_and_native_provenance')
    fixture.setUp()
    try:
        fixture.store.status(fixture.ctx.run_id, 1, 'succeeded')
        runtime = object.__new__(AgentRuntime)
        runtime.store, runtime.services = fixture.store, fixture.services
        runtime.tasks, runtime.schedule = AuditTasks(runtime), Mock()
        runtime.maintenance, runtime.agent_enabled = {}, False
        fixture.services.agent = runtime
        asset = fixture.asset('suppliers-left.csv')
        session = fixture.store.create_session('wtest', 'Natural clean', [asset.asset_id], 'natural-clean')
        goal = '请清理表格中供应商名称列的前后空格，谢谢'
        async def execute():
            run = await runtime.submit(session['id'], goal, 'clean-chat')
            assert run['driver'] == 'data-action-v1'
            await data_action.drive(runtime, run['id'])
            return runtime.store.run(run['id'])
        done = asyncio.run(execute())
        assert done['status'] == 'succeeded'
        saved = json.loads(done['driver_state'])
        assert saved['request']['source_text'] == goal
        assert saved['request']['steps'][0]['step']['column'] == 'c2'
        rows = saved['receipts']['preview']['data']['rows']
        assert rows[0]['cells'][1]['value'] == '甲公司'
        assert rows[0]['cells'][0]['value'] == '000001'
        operations = len(runtime.store.rows('SELECT id FROM operations'))
        replay = asyncio.run(runtime.submit(session['id'], goal, 'clean-chat'))
        assert replay['id'] == done['id']
        assert len(runtime.store.rows('SELECT id FROM operations')) == operations
        assert runtime.store.rows('SELECT id FROM model_requests') == []
        assert saved['receipts']['exported']['artifact_refs']
    finally:
        fixture.tearDown()
