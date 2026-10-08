"""Bounded business matching over registered tools; suggestions grant no access."""
from __future__ import annotations

import re


WORKFLOWS = {
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
        'skill': 'classification-training-preparation',
        'tools': ('classification_prepare', 'classification_import_assets', 'classification_infer',
                  'classification_read_results', 'classification_export'),
        'required': ('classification_prepare', 'classification_infer', 'classification_read_results'),
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
    positive = re.sub(r'(?:不要|无需|不用|不需要|不做|不进行|do not|don\x27t)\s*[^，。；;\n]{0,80}', '', text)
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
    identifiers = []
    if image and excel:
        identifiers.append('screen_excel')
    if invoice and (reconcile or re.search(r'ocr|识别|提取', positive)):
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
