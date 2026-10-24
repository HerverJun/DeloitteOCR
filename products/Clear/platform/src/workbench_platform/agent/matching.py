"""Bounded business matching over registered tools; suggestions grant no access."""
from __future__ import annotations

import re


WORKFLOWS = {
    'existing_ocr_reconciliation': {
        'skill': 'existing-ocr-reconciliation',
        'tools': ('native_attach_result', 'native_read_result', 'results_describe', 'results_to_input',
                  'data_create_session', 'data_inspect_input', 'data_import', 'data_read_table',
                  'data_compare', 'data_read_result', 'audit_bind_evidence', 'audit_read_evidence',
                  'data_export', 'delivery_publish'),
        'entry_tools': ('native_attach_result', 'results_describe'),
        'required': ('native_attach_result', 'results_describe', 'results_to_input',
                     'data_create_session', 'data_inspect_input', 'data_import', 'data_read_table',
                     'data_compare', 'data_read_result', 'audit_bind_evidence', 'audit_read_evidence',
                     'data_export', 'delivery_publish'),
        'inputs': ('result', 'reference'),
        'limits': ('已有OCR成果按原生接入、结构衔接、数据核对、证据和交付阶段推进；不重新OCR',
                   '只衔接完整无跨格结构；原值、来源及明确核对规则不改写'),
    },
    'screen_excel': {
        'skill': 'screen-batch-excel',
        'tools': ('ocr_prepare', 'ocr_screen_batch_run', 'ocr_screen_batch_status',
                  'ocr_screen_batch_control', 'ocr_screen_batch_inspect', 'ocr_screen_batch_compare',
                  'ocr_screen_batch_resolve', 'ocr_screen_batch_export', 'delivery_publish'),
        'required': ('ocr_prepare', 'ocr_screen_batch_run', 'ocr_screen_batch_export', 'delivery_publish'),
        'inputs': ('image',), 'limits': ('每图一个XLSX，同图多表分sheet；待复核不是人工确认',),
    },
    'invoice_reconciliation': {
        'skill': 'invoice-ocr-reconciliation',
        'tools': ('ocr_prepare', 'ocr_run', 'ocr_read_results', 'results_describe',
                  'results_to_input', 'data_import', 'data_compare', 'data_export'),
        'required': ('ocr_prepare', 'ocr_run', 'ocr_read_results', 'results_to_input', 'data_compare'),
        'inputs': ('image', 'pdf_page', 'ledger'),
        'limits': ('合并格语义未知或格位不完整不能猜测展开或补录',),
    },
    'payment_reconciliation': {
        'skill': 'payment-reconciliation',
        'tools': ('data_create_session', 'data_inspect_input', 'data_import', 'data_read_table',
                  'data_compare', 'data_read_result', 'data_export'),
        'required': ('data_import', 'data_compare', 'data_export'),
        'inputs': ('table', 'ledger'), 'limits': ('工作表、业务键、金额列、单位及核验规则需明确',),
    },
    'classification': {
        'skill': 'published-image-classification',
        'tools': ('classification_prepare', 'assets_create_set', 'classification_infer',
                  'classification_read_results', 'audit_bind_evidence', 'audit_read_evidence',
                  'classification_export', 'delivery_publish'),
        'entry_tools': ('classification_prepare', 'assets_create_set'),
        'required': ('classification_prepare', 'assets_create_set', 'classification_infer',
                     'classification_read_results', 'audit_bind_evidence', 'audit_read_evidence',
                     'classification_export', 'delivery_publish'),
        'inputs': ('image',), 'limits': ('使用已有发布模型；没有模型不自动创建训练目标',),
    },
    'evidence_workpaper': {
        'skill': 'evidence-workpaper',
        'tools': ('results_describe', 'audit_bind_evidence', 'audit_write_finding',
                  'audit_generate_workpaper', 'delivery_publish'),
        'required': ('audit_bind_evidence', 'audit_generate_workpaper'),
        'inputs': ('result', 'evidence'), 'limits': ('发现与底稿只引用实际结果及证据',),
    },
}


def match_goal(goal, registry, *, skills=None, workspace_id=None, session_id=None, snapshot=None):
    """Recognise explicit common goals; mixed/unknown goals retain discovery."""
    if not isinstance(goal, str) or not 1 <= len(goal) <= 12000:
        raise ValueError('任务目标无效')
    text = goal.casefold()
    # An explicit negation is not permission to execute the mentioned task.
    positive = re.sub(r'(?:不要|无需|不用|不需要|不做|不进行|禁止|不允许|do not|don\x27t)\s*[^，。；;\n]{0,80}', '', text)
    informational = bool(re.search(r'介绍|了解|解释|讨论|如何|怎么|能否|是否|评估|制定.{0,10}计划|提升方案|how\b', positive))
    # A polite request such as “帮我将这些图片转Excel” still expresses action.
    if (re.search(r'帮我|请(?:把|将)|开始(?:处理|识别|转换)|执行这', positive)
            and not re.search(r'介绍|了解|解释|讨论|如何|怎么|评估|制定.{0,10}计划|提升方案|how\b', positive)):
        informational = False
    # An explicit leading conversion instruction remains an action when the
    # user later explains that the output will be used to evaluate quality.
    first_instruction = re.split(r'[。；;\n]', positive.strip(), maxsplit=1)[0]
    if (re.match(r'^(?:(?:请|帮我|麻烦你)\s*)?(?:将|把|开始(?:处理|识别|转换)|处理这|转换这)', first_instruction)
            and re.search(r'(?:转(?:成|为)?\s*(?:excel|xlsx|表格)|转换|识别|处理)', first_instruction)
            and not re.search(r'介绍|了解|解释|讨论|如何|怎么|能否|是否|评估|制定.{0,10}计划|提升方案', first_instruction)):
        informational = False
    image = bool(re.search(r'图片|图像|照片|拍屏|屏拍|screens?hot|images?|photos?', positive))
    excel = bool(re.search(r'excel|xlsx|转表格|转成表格|导出表格', positive))
    invoice = bool(re.search(r'发票|票据|invoice|receipt', positive))
    reconcile = bool(re.search(r'核对|比对|对账|差异|reconcil|compare', positive))
    existing_ocr = bool(re.search(
        r'(?:已有|现有|已完成|已产生|existing|completed)[^，。；;\n]{0,30}ocr[^，。；;\n]{0,12}(?:结果|成果|result)', positive))
    existing_ocr |= bool(re.search(r'application\s*[:=]\s*[\"\x27]?ocr\b', positive)
                         and re.search(r'link_id\s*[:=]\s*\S+', positive)
                         and re.search(r'object_id\s*[:=]\s*\S+', positive))
    new_ocr = bool(re.search(r'(?:重新|新一轮|再次|开始)(?:进行)?\s*(?:ocr|识别)', positive))
    identifiers = []
    if image and excel:
        identifiers.append('screen_excel')
    if existing_ocr and reconcile:
        identifiers.append('existing_ocr_reconciliation')
        if new_ocr:
            # A mixed request must not silently reduce new inference to a
            # read-only existing-result route or activate either skill.
            identifiers.append('invoice_reconciliation')
    elif invoice and (reconcile or re.search(r'ocr|识别|提取', positive)):
        identifiers.append('invoice_reconciliation')
    elif reconcile:
        identifiers.append('payment_reconciliation')
    if re.search(r'分类|classification|classify', positive) and not re.search(r'训练|train', positive):
        identifiers.append('classification')
    if re.search(r'底稿|整理证据|生成证据|workpaper', positive):
        identifiers.append('evidence_workpaper')
    registered = set(registry.tools)
    allowed = (snapshot.allowed_tools(registered) if snapshot is not None else
               skills.allowed_tools(session_id, registered) if skills and session_id else registered)
    live_skills = {}
    if snapshot is not None or skills:
        page = snapshot.matching_catalog if snapshot is not None else skills.catalog(workspace_id, session_id, limit=50)
        live_skills = {item['name']: item for item in page['items']}
    workflows = []
    for identifier in dict.fromkeys(identifiers):
        definition = WORKFLOWS[identifier]
        names = definition['tools']
        missing = [name for name in definition['required'] if name not in registered]
        blocked = [name for name in definition['required'] if name in registered and name not in allowed]
        skill = live_skills.get(definition['skill'])
        workflows.append({'id': identifier, 'suggested_skill': definition['skill'],
            'skill_state': 'compatible' if skill and skill['compatible'] else 'unavailable',
            'skill_version': skill.get('version') if skill else None,
            'tools': [name for name in names if name in registered],
            'entry_tools': [name for name in definition.get('entry_tools', names) if name in registered],
            'missing_required_tools': missing, 'blocked_by_active_skills': blocked,
            'input_kinds': list(definition['inputs']), 'limits': list(definition['limits']),
            'can_select': not missing and not blocked,
            'execution_ready': 'requires_live_capability_check'})
    return {'version': 1, 'workflows': workflows,
        'match': 'mixed' if len(workflows) > 1 else 'single' if workflows else 'unknown',
        'intent': 'information' if informational else 'action',
        'auto_skill_candidate': workflows[0]['suggested_skill'] if not informational and len(workflows) == 1 and
            workflows[0]['can_select'] and workflows[0]['skill_state'] == 'compatible' else None,
        'grants_changed': False, 'schemas_activated': False,
        'next_step': 'use_registered_workflow' if workflows else 'discover_registered_tools'}


def existing_ocr_progress(messages, initial_refs=()):
    """Suggest phases from actual paired receipts; never activate tools.

    The latest controlled bridge and data session anchor downstream progress.
    Pending/error/unsupported business operations remain explicit.
    """
    calls = {call['call_id']: call for message in messages if message.get('role') == 'assistant'
             for call in message.get('calls', ())}
    actual = [(index, calls[message['call_id']], message['result'])
              for index, message in enumerate(messages)
              if message.get('role') == 'tool' and message.get('call_id') in calls
              and calls[message['call_id']]['tool'] in WORKFLOWS['existing_ocr_reconciliation']['tools']]

    def phase(name, tools, **details):
        archived = []
        if name in {'data_session', 'input_columns', 'data_inputs', 'actual_columns', 'comparison',
                    'actual_differences', 'direct_evidence', 'evidence_readback',
                    'downloadable_export', 'verified_delivery'}:
            archived.extend(('native_attach_result', 'native_read_result', 'results_describe'))
        if name in {'actual_columns', 'comparison', 'actual_differences', 'direct_evidence',
                    'evidence_readback', 'downloadable_export', 'verified_delivery'}:
            archived.extend(('data_create_session', 'data_inspect_input'))
        if name in {'comparison', 'actual_differences', 'direct_evidence', 'evidence_readback',
                    'downloadable_export', 'verified_delivery'}:
            archived.extend(('results_to_input', 'data_import'))
        if name in {'comparison', 'actual_differences', 'direct_evidence', 'evidence_readback',
                    'downloadable_export', 'verified_delivery'}:
            archived.append('data_read_table')
        if name in {'direct_evidence', 'evidence_readback', 'downloadable_export', 'verified_delivery'}:
            archived.extend(('data_compare', 'data_read_result'))
        if name in {'evidence_readback', 'downloadable_export', 'verified_delivery'}:
            archived.append('audit_bind_evidence')
        if name in {'downloadable_export', 'verified_delivery'}:
            archived.extend(('data_read_result', 'audit_read_evidence'))
        if name == 'verified_delivery':
            archived.append('data_export')
        route = {'version': 1, 'workflow': 'existing_ocr_reconciliation', 'phase': name,
                'next_tools': list(tools), 'archive_tools': archived,
                'retain_table_columns': name == 'comparison',
                'grants_changed': False, 'schemas_activated': False, **details}
        if name == 'verified_delivery':
            route['delivery_validation'] = 'delivery_publish按live artifact_refs/result_refs核实际文件字节、来源及证据并返回下载；直接交付，勿重复读取同一完整导出回执。'
        return route

    if actual and actual[-1][2].get('status') != 'success':
        _, call, receipt = actual[-1]
        return phase('requires_attention', (call['tool'],), status=receipt.get('status'),
                     call_id=call['call_id'])
    if actual and actual[-1][1]['tool'] == 'audit_read_evidence' and actual[-1][2].get('data', {}).get('stale'):
        return phase('requires_attention', ('audit_bind_evidence',), status='stale_evidence',
                     call_id=actual[-1][1]['call_id'])
    completed = [entry for entry in actual if entry[2].get('status') == 'success']

    def successes(tool, after=-1, session=None):
        return [entry for entry in completed if entry[0] > after and entry[1]['tool'] == tool
                and (session is None or entry[1]['arguments'].get('data_session_id') == session)]

    attached = successes('native_attach_result')
    typed = any(ref.get('kind') == 'result' and ref.get('application') == 'ocr' for ref in initial_refs)
    if not attached and not typed:
        return phase('native_source', ('native_attach_result',))
    described = successes('results_describe', attached[-1][0] if attached else -1)
    if not described:
        return phase('structure_catalog', ('results_describe',))
    converted = successes('results_to_input', described[0][0])
    if not converted:
        return phase('controlled_input', ('results_to_input',))
    created = successes('data_create_session', converted[-1][0])
    if not created:
        return phase('data_session', ('data_create_session',))
    session = created[-1][2].get('data', {}).get('data_session_id')
    if not session:
        return phase('requires_attention', ('data_create_session',), status='missing_actual_session_id')
    anchor = created[-1][0]
    inspected = {entry[1]['arguments'].get('asset_id') for entry in successes('data_inspect_input', converted[-1][0])}
    required_inputs = {ref['object_id'] for ref in initial_refs if ref.get('kind') == 'asset'}
    bridge_asset = converted[-1][2].get('data', {}).get('asset_id')
    if bridge_asset:
        required_inputs.add(bridge_asset)
    if ((required_inputs and not required_inputs <= inspected)
            or not required_inputs and len(inspected - {None}) < 2):
        return phase('input_columns', ('data_inspect_input',))
    imported = {entry[1]['arguments'].get('side') for entry in successes('data_import', anchor, session)}
    if imported != {'left', 'right'}:
        return phase('data_inputs', ('data_import',), imported_sides=sorted(imported - {None}))
    read = {entry[1]['arguments'].get('side') for entry in successes('data_read_table', anchor, session)}
    if read != {'left', 'right'}:
        return phase('actual_columns', ('data_read_table',), read_sides=sorted(read - {None}))
    compared = successes('data_compare', anchor, session)
    if not compared:
        return phase('comparison', ('data_compare',))
    comparison = compared[-1]
    difference_reads = successes('data_read_result', comparison[0], session)
    if not difference_reads:
        return phase('actual_differences', ('data_read_result',))
    if difference_reads[-1][2].get('next_offset') is not None:
        return phase('actual_differences', ('data_read_result',), next_offset=difference_reads[-1][2]['next_offset'])
    bound = successes('audit_bind_evidence', comparison[0])
    if not bound:
        return phase('direct_evidence', ('audit_bind_evidence',))
    if not successes('audit_read_evidence', bound[-1][0]):
        return phase('evidence_readback', ('audit_read_evidence',))
    if not successes('data_export', comparison[0], session):
        return phase('downloadable_export', ('data_export',))
    return phase('verified_delivery', ('delivery_publish',))


def classification_progress(messages):
    """Published-model inference phases from actual receipts, without training."""
    calls = {call['call_id']: call for message in messages if message.get('role') == 'assistant'
             for call in message.get('calls', ())}
    actual = [(index, calls[message['call_id']], message['result'])
              for index, message in enumerate(messages)
              if message.get('role') == 'tool' and message.get('call_id') in calls
              and calls[message['call_id']]['tool'] in WORKFLOWS['classification']['tools']]
    def phase(name, *tools, **details):
        archive = ['classification_prepare'] if name in {
            'selected_inputs', 'published_prediction', 'actual_predictions', 'direct_evidence', 'evidence_readback',
            'downloadable_export', 'verified_delivery'} else []
        if name in {'evidence_readback', 'downloadable_export', 'verified_delivery'}:
            archive.append('audit_bind_evidence')
        if name in {'downloadable_export', 'verified_delivery'}:
            archive.append('audit_read_evidence')
        if name in {'actual_predictions', 'direct_evidence', 'evidence_readback',
                    'downloadable_export', 'verified_delivery'}:
            archive.append('assets_create_set')
        if name in {'direct_evidence', 'evidence_readback', 'downloadable_export', 'verified_delivery'}:
            archive.append('classification_infer')
        if name == 'verified_delivery':
            archive.append('classification_export')
        route = {'version': 1, 'workflow': 'classification', 'phase': name,
                'next_tools': list(tools), 'archive_tools': archive,
                'grants_changed': False, 'schemas_activated': False, **details}
        if name == 'verified_delivery':
            route['delivery_validation'] = 'delivery_publish按live artifact_refs/result_refs核实际文件字节、来源及证据并返回下载；直接交付，勿重复读取同一完整导出回执。'
        return route
    if actual and actual[-1][2].get('status') != 'success':
        return phase('requires_attention', actual[-1][1]['tool'], status=actual[-1][2].get('status'))
    if actual and actual[-1][1]['tool'] == 'audit_read_evidence' and actual[-1][2].get('data', {}).get('stale'):
        return phase('requires_attention', 'audit_bind_evidence', status='stale_evidence')
    completed = [entry for entry in actual if entry[2].get('status') == 'success']
    def successes(tool, after=-1, handoff=None):
        return [entry for entry in completed if entry[0] > after and entry[1]['tool'] == tool
                and (handoff is None or entry[1]['arguments'].get('handoff_id') == handoff)]
    prepared = successes('classification_prepare')
    if not prepared:
        return phase('project_model', 'classification_prepare')
    fixed = successes('assets_create_set')
    if not fixed:
        return phase('selected_inputs', 'assets_create_set')
    inference = successes('classification_infer', max(prepared[-1][0], fixed[-1][0]))
    if not inference:
        return phase('published_prediction', 'classification_infer')
    latest = inference[-1]
    handoff = latest[2].get('data', {}).get('handoff_id')
    if not handoff:
        return phase('requires_attention', 'classification_infer', status='missing_actual_handoff_id')
    reads = successes('classification_read_results', latest[0], handoff)
    if not reads or reads[-1][2].get('next_offset') is not None:
        return phase('actual_predictions', 'classification_read_results',
                     next_offset=reads[-1][2].get('next_offset') if reads else None)
    bound = successes('audit_bind_evidence', latest[0])
    if not bound:
        return phase('direct_evidence', 'audit_bind_evidence')
    if not successes('audit_read_evidence', bound[-1][0]):
        return phase('evidence_readback', 'audit_read_evidence')
    if not successes('classification_export', latest[0], handoff):
        return phase('downloadable_export', 'classification_export')
    return phase('verified_delivery', 'delivery_publish')


def readiness_projection(context, *, names=None, models=None):
    """Expose registration, live service and model evidence separately.

    Queue eligibility never proves file hashes, loaded weights or GPU capacity.
    Use the existing read-only native model projection, then allow-list fields.
    """
    from workbench_platform.application_services.native import model_projection
    registry = context.services.agent.registry
    selected = list(dict.fromkeys(names if names is not None else registry.tools))
    if len(selected) > 200:
        raise ValueError('能力目录范围过大')
    tools = []
    for name in selected:
        if name not in registry.tools:
            tools.append({'name': name, 'registered': False, 'service_state': 'unavailable'})
            continue
        description = registry.describe(name, context.services)
        available = description['availability']
        tools.append({'name': name, 'registered': True, 'tool_version': description['version'],
            'service_state': available.get('state', 'unknown'),
            'service_reason': available.get('reason', ''),
            'applications': available.get('applications', {})})
    rows = models if models is not None else model_projection(context)
    safe = []
    for row in rows[:200]:
        details = row.get('details') or {}
        # A native catalog without an explicit engine readiness field only
        # proves queue eligibility; do not upgrade that to model readiness.
        explicit = details.get('ready') if row.get('role') == 'ocr_engine' else row.get('ready')
        state = 'ready' if row.get('id') and explicit is True else 'unavailable' if explicit is False else 'unknown'
        safe.append({key: row[key] for key in ('id', 'role', 'tool_family', 'workspace_id',
            'project_id', 'link_id', 'version_ref', 'supported_tasks', 'input_kinds') if key in row} |
            {'model_state': state, 'queue_submittable': bool(row.get('ready')),
             'runtime_file_validation_required': True, 'loaded_device_verified': False,
             'reason': row.get('reason', ''), 'basis': 'live-native-catalog',
             'limits': ['目录就绪不等于已加载或实际模型文件/GPU资格已验证']})
    return {'version': 1, 'tools': tools, 'models': safe, 'starts_services': False,
            'probes_model_execution': False}
