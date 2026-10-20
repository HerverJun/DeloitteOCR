"""Bounded interpretation preserves one goal, all conditions and exact pages."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from workbench_platform.agent.store import AgentStore
from workbench_platform.agent.goal_interpreter import maybe_route, explicit_pages


def host(tmp_path, goal, media='image/png'):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspace', '目标', ['source'], 'session')
    saved = {'local_goal_to_parse': goal, 'goal_scope_revision': 1, 'goal_selected_assets': ['source']}
    run, _ = store.start_run(session['id'], goal, 'goal', 1, driver_state=saved)
    asset = SimpleNamespace(media_type=media, revision=1, blob_hash='a' * 64)
    config = {'protocol': 'openai_chat_completions', 'model': 'test', 'max_output_tokens': 512}
    runtime = SimpleNamespace(store=store, tasks=SimpleNamespace(get=lambda sid: {'scope_revision': 1, 'manual': False}, record_goal=Mock()),
        services=SimpleNamespace(asset_library=(SimpleNamespace(get=lambda *args: asset), None, None),
                                 workspace_store=SimpleNamespace(list_links=lambda workspace: [])),
        connection=SimpleNamespace(resolve=lambda revision: (config, '')), schedule=Mock(), activity=Mock())
    return runtime, run


def response(**overrides):
    args = {'action': 'image_text', 'page_numbers': [], 'engines': [], 'mode': 'auto',
            'analysis_after_extraction': False, 'reason': '明确文字提取', **overrides}
    return {'content': '', 'calls': [{'tool': 'interpret_goal', 'call_id': 'parse', 'arguments': args}]}


def test_single_interpretation_adopts_same_goal_without_native_or_extra_run(tmp_path):
    runtime, run = host(tmp_path, '把图片上的字弄成文本')
    provider = AsyncMock(return_value=response())
    with patch('workbench_platform.agent.goal_interpreter.request', provider):
        assert asyncio.run(maybe_route(runtime, run['id'])) is True
    adopted = runtime.store.run(run['id'])
    assert adopted['driver'] == 'image-text-v1'
    assert json.loads(adopted['driver_state'])['request']['source_text'] == '把图片上的字弄成文本'
    assert provider.await_count == 1
    assert len(runtime.store.rows('SELECT * FROM runs')) == 1
    assert len(runtime.store.rows('SELECT * FROM model_requests')) == 1
    assert runtime.store.rows('SELECT * FROM operations') == []


def test_parser_cannot_add_unselected_pdf_pages(tmp_path):
    runtime, run = host(tmp_path, '请弄出PDF第2、5页的原文', 'application/pdf')
    with patch('workbench_platform.agent.goal_interpreter.request', AsyncMock(return_value=response(
            action='pdf_text', page_numbers=[2, 3, 5]))):
        assert asyncio.run(maybe_route(runtime, run['id'])) is False
    assert runtime.store.run(run['id'])['driver'] == 'harness'
    assert runtime.store.rows('SELECT * FROM operations') == []


def test_comparison_survives_a_parser_omission(tmp_path):
    goal = '把图片上的字弄出来并比较金额，保留原文和精度'
    runtime, run = host(tmp_path, goal)
    with patch('workbench_platform.agent.goal_interpreter.request', AsyncMock(return_value=response())):
        assert asyncio.run(maybe_route(runtime, run['id'])) is True
    assert json.loads(runtime.store.run(run['id'])['driver_state'])['analysis_goal'] == goal
    assert explicit_pages('第2页到第5页') == {2, 3, 4, 5}


def test_replacing_an_original_while_interpreting_cannot_change_accepted_input(tmp_path):
    import pytest
    from workbench_platform.agent.store import Conflict
    runtime, run = host(tmp_path, '把图片上的字弄成文本')
    saved = json.loads(run['driver_state'])
    saved['goal_input_versions'] = [{'asset_id': 'source', 'revision': 1, 'blob_hash': 'a' * 64}]
    runtime.store.save_driver_state(run['id'], 1, saved)
    asset = runtime.services.asset_library[0].get('workspace', 'source')
    async def changed(*args, **kwargs):
        asset.revision = 2
        return response()
    with patch('workbench_platform.agent.goal_interpreter.request', changed), pytest.raises(Conflict, match='原件版本'):
        asyncio.run(maybe_route(runtime, run['id']))
    assert runtime.store.rows('SELECT * FROM operations') == []
    assert runtime.store.run(run['id'])['driver'] == 'harness'


def test_adoption_crash_preserves_the_full_composite_goal_atomically(tmp_path):
    import pytest
    goal = '把图片上的字弄出来并比较金额'
    runtime, run = host(tmp_path, goal)
    runtime.schedule.side_effect = RuntimeError('cold stop immediately after adoption')
    with patch('workbench_platform.agent.goal_interpreter.request', AsyncMock(return_value=response())), pytest.raises(RuntimeError):
        asyncio.run(maybe_route(runtime, run['id']))
    adopted = runtime.store.run(run['id'])
    assert adopted['driver'] == 'image-text-v1'
    assert json.loads(adopted['driver_state'])['analysis_goal'] == goal
    assert json.loads(adopted['driver_state'])['interpretation_request_id']
    assert adopted['rounds'] == 1


def test_cached_valid_interpretation_survives_a_busy_object_without_reparsing(tmp_path):
    import pytest
    from workbench_platform.application_services.errors import ResourceBusy
    from workbench_platform.application_services import action_admission
    runtime, run = host(tmp_path, '把图片上的字弄成文本')
    provider = AsyncMock(return_value=response())
    with patch('workbench_platform.agent.goal_interpreter.request', provider):
        with patch.object(action_admission, 'start', side_effect=ResourceBusy('busy')), pytest.raises(ResourceBusy):
            asyncio.run(maybe_route(runtime, run['id']))
        assert asyncio.run(maybe_route(runtime, run['id'])) is True
    assert provider.await_count == 1
    assert len(runtime.store.rows('SELECT * FROM runs')) == 1


def test_explicit_native_pdf_requirement_cannot_be_downgraded(tmp_path):
    runtime, run = host(tmp_path, '只读取PDF第2页的原生文字层，不做OCR', 'application/pdf')
    with patch('workbench_platform.agent.goal_interpreter.request', AsyncMock(return_value=response(
            action='pdf_text', page_numbers=[2], mode='auto'))):
        assert asyncio.run(maybe_route(runtime, run['id'])) is False
    assert runtime.store.run(run['id'])['driver'] == 'harness'


def test_orphan_sent_model_intent_never_causes_implicit_resend(tmp_path):
    import pytest
    from workbench_platform.agent.providers import ProviderFault
    runtime, run = host(tmp_path, '把图片上的字弄成文本')
    runtime.store.begin_model(run['id'], 1, 0, 'saved-hash')
    provider = AsyncMock(return_value=response())
    with patch('workbench_platform.agent.goal_interpreter.request', provider), pytest.raises(ProviderFault, match='未知'):
        asyncio.run(maybe_route(runtime, run['id']))
    provider.assert_not_awaited()
    assert len(runtime.store.rows('SELECT * FROM model_requests')) == 1


def test_unexpressible_region_or_format_cannot_be_dropped_by_a_parser(tmp_path):
    runtime, run = host(tmp_path, '只提取图片右半部分的文字，输出Excel')
    provider = AsyncMock(return_value=response())
    with patch('workbench_platform.agent.goal_interpreter.request', provider):
        assert asyncio.run(maybe_route(runtime, run['id'])) is False
    provider.assert_not_awaited()
    assert runtime.store.run(run['id'])['driver'] == 'harness'
