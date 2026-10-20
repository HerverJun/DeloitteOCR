"""One bounded interpretation, then the existing fixed business driver.

No tools, internal IDs or writes are delegated to this model request. The
complete user goal, typed parameters and original model receipt stay on one run.
"""
import json
import re
from typing import Literal

from pydantic import Field
from packages.contracts.agent import AgentModel
from .providers import payload_for, request, ProviderFault
from .store import canonical, digest, Conflict


class Interpretation(AgentModel):
    action: Literal['image_text', 'pdf_text', 'unsupported']
    page_numbers: list[int] = Field(default_factory=list, max_length=100)
    engines: list[Literal['ppocr', 'paddlevl', 'glm', 'hunyuan']] = Field(default_factory=list, max_length=4)
    mode: Literal['auto', 'native', 'ocr'] = 'auto'
    analysis_after_extraction: bool
    reason: str = Field(max_length=500)


def explicit_pages(goal):
    text = re.sub(r'\s+', '', goal)
    text = re.sub(r'第?(\d+)页(?:至|到|-)第?(\d+)页', r'第\1至\2页', text)
    pages = set()
    for group in re.findall(r'第?(\d+(?:(?:、|,|，|和|及|与|至|到|-)\d+)*)页', text):
        if re.fullmatch(r'\d+(?:至|到|-)\d+', group):
            start, end = map(int, re.split(r'至|到|-', group))
            if not 1 <= start <= end <= 1000 or end - start >= 100:
                return None
            pages.update(range(start, end + 1))
        else:
            pages.update(map(int, re.split(r'、|,|，|和|及|与', group)))
    return pages or None


SYSTEM = ('你只进行一次目标理解，不执行任务。根据完整原目标和资料类型，判断能否交给已有固定文字提取动作。'
          '只能调用interpret_goal一次。image_text提取所选图片全部文字并提供全文/TXT；pdf_text只处理明确的页码。'
          '有比较、分析、翻译、制表等后续要求时analysis_after_extraction必须为true，保留全部原目标。'
          '用户要求区域/裁剪、排除部分图片、特殊格式/精度或参数无法表达、PDF页码不明确、非文字提取目标，'
          'action必须unsupported。不要猜页码、不要更换用户指定引擎，不把否定要求理解为提取。'
          '只读原文/保留原文和提供TXT是固定动作已有能力；不要询问内部服务、ID或执行模式。')


def unsupported_conditions(goal):
    return bool(re.search(r'区域|裁剪|框选|左半|右半|上半|下半|左侧|右侧|左边|右边|局部|坐标|排除|忽略|除了|除.+以?外|'
                          r'第\s*[一二三四五六七八九十\d]+\s*张|前\s*[一二三四五六七八九十\d]+\s*张|'
                          r'excel|xlsx|csv|json|word|docx|排版|字体|不要\s*txt|不需要\s*txt', goal, re.I))


def eligible(runtime, session, task, content):
    if unsupported_conditions(content):
        return False
    if runtime.model_provider or session.get('initial_refs') or task.get('delivery_requirements'):
        return False
    if not session.get('selected_assets') or not re.search(r'字|文本|原文|识别|抄|ocr', content, re.I):
        return False
    registry = runtime.services.asset_library[0]
    media = [registry.get(session['workspace_id'], item).media_type for item in session['selected_assets']]
    return all(value.startswith('image/') for value in media) or media == ['application/pdf']


def assert_input_versions(runtime, run, saved):
    registry = runtime.services.asset_library[0]
    for expected in saved.get('goal_input_versions', []):
        asset = registry.get(run['workspace_id'], expected['asset_id'])
        if (asset.revision, asset.blob_hash) != (expected['revision'], expected['blob_hash']):
            raise Conflict('目标理解期间原件版本已改变；原目标与解析回执保留，未提交业务写入')


async def maybe_route(runtime, run_id):
    run = runtime.store.run(run_id)
    saved = json.loads(run.get('driver_state') or '{}')
    goal = saved.get('local_goal_to_parse')
    if not goal or saved.get('interpretation_complete'):
        return False
    if unsupported_conditions(goal):
        saved['interpretation_complete'] = True
        runtime.store.save_driver_state(run_id, run['generation'], saved)
        return False
    if runtime.store.rows("SELECT id FROM model_requests WHERE run_id=? AND state='sent'", (run_id,)):
        raise ProviderFault('response_unknown', '原模型尝试回包未知；请点击继续核对原回执，未自动发送新的理解请求')
    session = runtime.store.session(run['session_id'])
    task = runtime.tasks.get(run['session_id'])
    assert_input_versions(runtime, run, saved)
    if (task.get('pending_scope') or task['scope_revision'] != saved['goal_scope_revision'] or
            session['selected_assets'] != saved['goal_selected_assets']):
        raise Conflict('理解目标期间资料范围已变更；原目标保留，未提交任何本地业务操作')
    registry = runtime.services.asset_library[0]
    inputs = [{'asset_id': item, 'media_type': registry.get(session['workspace_id'], item).media_type}
              for item in session['selected_assets']]
    config, secret = runtime.connection.resolve(run['connection_revision'])
    config = {**config, 'max_output_tokens': min(config.get('max_output_tokens', 1024), 1024)}
    payload = payload_for(config, [{'role': 'user', 'content': canonical({'goal': goal, 'selected_sources': inputs})}],
                          schemas={'interpret_goal': Interpretation.model_json_schema()}, system=SYSTEM)
    records = runtime.store.rows("SELECT m.* FROM model_requests m JOIN events e ON e.run_id=m.run_id "
        "AND e.identity=m.id||':goal-interpretation' WHERE m.run_id=? ORDER BY m.step DESC", (run_id,))
    record = records[0] if records and records[0]['state'] not in {'rejected', 'abandoned', 'cancelled'} else None
    state = {'run_id': run_id, 'generation': run['generation'], 'session_id': run['session_id'],
             'workspace_id': run['workspace_id']}
    if record and record['state'] == 'sent':
        raise ProviderFault('response_unknown', '目标理解回包未知；原目标与资料已保存，点击继续可重新理解，未执行业务写入')
    if record:
        response = json.loads(record['response'])
    else:
        ordinal = runtime.store.rows('SELECT COALESCE(MAX(step),-1)+1 AS n FROM model_requests WHERE run_id=?', (run_id,))[0]['n']
        record, _ = runtime.store.begin_model(run_id, run['generation'], ordinal, digest(payload))
        runtime.store.event(run['session_id'], run_id, record['id'] + ':goal-interpretation', 'model_request',
            {'purpose': 'goal_interpretation', 'model': config['model'], 'generation': run['generation'],
             'request_snapshot': {'format': 'clear-goal-interpretation-v1', 'payload_sha256': digest(payload)}})
        runtime.activity(state, 'model_wait', reason='理解完整目标后交给本地动作')
        try:
            response = await request(config, secret, payload, record['id'])
        except ProviderFault as error:
            if error.code != 'response_unknown':
                with runtime.store.transaction() as db:
                    db.execute("UPDATE model_requests SET state='rejected',response=? WHERE id=?",
                               (canonical({'error': error.code}), record['id']))
            raise
        runtime.store.assert_current(run_id, run['generation'])
        runtime.store.finish_model(record['id'], response)
    with runtime.store.transaction() as db:
        db.execute("UPDATE runs SET rounds=MAX(rounds,(SELECT COUNT(*) FROM model_requests WHERE run_id=? AND state='received')) "
                   'WHERE id=? AND generation=?', (run_id, run_id, run['generation']))
    calls = response.get('calls', [])
    value = None
    if len(calls) == 1 and calls[0]['tool'] == 'interpret_goal':
        try:
            value = Interpretation.model_validate(calls[0]['arguments'])
        except ValueError:
            pass
    def fallback():
        saved['interpretation_complete'] = True
        saved['interpretation_request_id'] = record['id']
        runtime.store.save_driver_state(run_id, run['generation'], saved)
        return False
    if value is None or value.action == 'unsupported':
        return fallback()
    media = [item['media_type'] for item in inputs]
    requested_engines = {name for name in ('ppocr', 'paddlevl', 'glm', 'hunyuan') if name in goal.lower()}
    if set(value.engines) != requested_engines:
        return fallback()
    if len(value.engines) != len(set(value.engines)):
        return fallback()
    # The model cannot enlarge source types or manufacture PDF page scope.
    if value.action == 'image_text' and not all(item.startswith('image/') for item in media):
        return fallback()
    if value.action == 'image_text' and value.page_numbers:
        return fallback()
    if value.action == 'pdf_text' and (media != ['application/pdf'] or not value.page_numbers):
        return fallback()
    if value.action == 'pdf_text':
        if re.search(r'原生|native|文字层|(?:不做|不要|无需|禁用|不用)\s*ocr', goal, re.I) and value.mode != 'native':
            return fallback()
        if re.search(r'(?:强制|使用|用)\s*ocr', goal, re.I) and value.mode != 'ocr':
            return fallback()
        if value.mode == 'native' and not re.search(r'原生|native|文字层', goal, re.I):
            return fallback()
        if value.mode == 'ocr' and not re.search(r'ocr|扫描', goal, re.I):
            return fallback()
        explicit = explicit_pages(goal)
        if explicit is None or set(value.page_numbers) != explicit:
            return fallback()
        from workbench_platform.application_services.pdf_text import PdfTextRequest, submit
        if len(value.engines) > 1:
            return fallback()
        try:
            typed = PdfTextRequest(request_id=run['request_id'], page_numbers=value.page_numbers, mode=value.mode,
                                   engine=value.engines[0] if value.engines else 'ppocr')
        except ValueError:
            return fallback()
    else:
        from workbench_platform.application_services.image_text import ImageTextRequest, submit
        typed = ImageTextRequest(request_id=run['request_id'], engines=value.engines)
    current = runtime.tasks.get(run['session_id'])
    assert_input_versions(runtime, run, saved)
    if current.get('pending_scope') or current['scope_revision'] != saved['goal_scope_revision']:
        raise Conflict('目标理解后的资料范围已改变，未提交业务操作')
    typed.expected_scope_revision = saved['goal_scope_revision']
    analysis_goal = goal if value.analysis_after_extraction or re.search(r'比较|对比|分析|解释|翻译|制表|汇总|核对', goal) else None
    await submit(runtime, run['session_id'], typed, source_text=goal, adopt_run_id=run_id,
                 analysis_goal=analysis_goal, interpretation_request_id=record['id'],
                 expected_inputs=saved.get('goal_input_versions'))
    return True
