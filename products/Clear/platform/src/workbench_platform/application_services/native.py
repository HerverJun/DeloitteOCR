"""Prepare managed native instances and scoped projects without browser visits."""
from dataclasses import asdict
from pathlib import Path
import uuid

from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.jobs.ocr_projects import OcrProjects
from workbench_platform.jobs.classification_projects import ClassificationProjects
from workbench_platform.application_services.identity import digest
from workbench_platform.application_services.context import require_write


def availability(services, app_id):
    if getattr(services, 'job_service', None) is None or getattr(services, 'launch_service', None) is None:
        return {'state': 'unavailable', 'reason': '原生任务服务未配置'}
    installation = services.supervisor.registry.installations.get(app_id)
    if installation is None or not installation.installed:
        return {'state': 'unavailable', 'reason': '原生工具包未登记'}
    try:
        services.launch_service.current(app_id)
    except LaunchStatus as error:
        if error.status == 404:
            return {'state': 'preparable', 'reason': '可启动已安装服务'}
        return {'state': 'unavailable', 'reason': '原生服务当前身份或运行状态不可核实，请先恢复服务'}
    return {'state': 'ready', 'reason': ''}


def prepare(context, app_id, link_id=None, categories=None):
    require_write(context)
    context.current()
    services = context.services
    if availability(services, app_id)['state'] == 'unavailable':
        raise ValueError('原生工具包未登记或服务不可用')
    try:
        receipt = services.launch_service.current(app_id)
    except LaunchStatus as error:
        if error.status != 404:
            raise
        try:
            receipt, _ = services.launch_service.start(app_id, 'l' + digest([context.operation_id, app_id])[:40])
        except LaunchStatus as raced:
            if raced.status != 409 or raced.code != 'RESOURCE_BUSY':
                raise
            # Another restored task may have completed the same service launch
            # after our read. Reuse its verified receipt, never launch again or
            # equate an unverified process with a ready native service.
            context.current()
            receipt = services.launch_service.current(app_id)
    context.current()
    projects = OcrProjects(services.job_service) if app_id == 'ocr' else ClassificationProjects(services.job_service)
    links = [link for link in services.workspace_store.list_links(context.workspace_id) if link.app_id == app_id]
    if link_id:
        links = [link for link in links if link.link_id == link_id]
        if len(links) != 1:
            raise ValueError('原生项目关联不属于当前项目')
    if len(links) > 1:
        return {'requires_project_selection': True, 'links': [asdict(link) for link in links]}
    if links:
        link = links[0]
        if link.role == 'viewer':
            raise ValueError('当前原生项目为只读')
        # Confirm the current live service still owns this exact native ID;
        # same names in another project are never a substitute for identity.
        if not any(item['project_id'] == link.module_project_id for item in projects.projects(context.workspace_id)['items']):
            raise ValueError('原生关联项目已不存在')
        selected = {'link_id': link.link_id, 'project_id': link.module_project_id}
    else:
        workspace = services.workspace_store.get_workspace(context.workspace_id)
        if app_id == 'classification' and not categories:
            return {'requires_project_selection': True, 'links': [],
                    'reason': '请关联已有分类项目，或明确新项目类别；新建项目没有训练模型'}
        selected = projects.create(context.workspace_id, workspace.name,
            **({'categories': categories} if app_id == 'classification' else {}), request_id=context.operation_id)
    native_context = services.job_service._context(context.workspace_id, app_id, selected['link_id'])
    adapter = services.job_service._adapter(native_context)
    if app_id == 'classification':
        project = adapter.project()
        catalog = {'models': list(adapter.models()), 'labels': project['labels']}
    else:
        state = adapter._request('GET', '/api/state')
        engines = state.get('engines')
        if not isinstance(engines, dict):
            raise ValueError('原生引擎目录不可核实')
        catalog = {'engines': [{'id': identifier, **details} for identifier, details in engines.items()
                                if isinstance(details, dict)], 'capabilities': asdict(adapter.capabilities())}
    return {'link_id': selected['link_id'], 'project_id': selected['project_id'], **catalog,
            'requires_project_selection': False, 'instance_id': receipt.instance_id}


def ensure_read_service(context, app_id, link_id):
    """Reopen only the installed service for an already scoped OCR read.

    The caller first verifies the original handoff/binding and input scope.
    This restores a process, never a project, import, page request or OCR job.
    An uncertain current process is not permission to launch a replacement.
    """
    from .errors import NativeReaderUnavailable
    if app_id != 'ocr':
        return
    services = context.services
    launches = getattr(services, 'launch_service', None)
    # Service-only embeddings can supply their own native reader without the
    # desktop Supervisor. They retain their existing read implementation.
    if launches is None:
        return
    context.current()
    links = [link for link in services.workspace_store.list_links(context.workspace_id)
             if link.app_id == app_id and link.link_id == link_id]
    if len(links) != 1 or not links[0].module_project_id:
        raise ValueError('固定读取的原生项目关联不属于当前项目')
    try:
        launches.current(app_id)
    except LaunchStatus as error:
        if error.status != 404:
            raise NativeReaderUnavailable(app_id, cause_code=error.code) from error
        if links[0].role == 'viewer':
            raise NativeReaderUnavailable(app_id, cause_code='READ_ONLY_LINK') from error
        installation = getattr(getattr(getattr(services, 'supervisor', None), 'registry', None), 'installations', {}).get(app_id)
        if installation is None or not installation.installed:
            raise NativeReaderUnavailable(app_id, cause_code='NOT_INSTALLED') from error
        if not getattr(context, 'grants', frozenset({'read', 'derive'})).intersection({'derive', 'versioned', 'control'}):
            raise NativeReaderUnavailable(app_id, cause_code='READ_ONLY_SNAPSHOT') from error
        require_write(context)
        context.current()
        try:
            # A new service launch identity is intentionally separate from all
            # immutable native business IDs and durable old launch reservations.
            launches.start(app_id, 'l' + uuid.uuid4().hex)
        except LaunchStatus as raced:
            if raced.status != 409 or raced.code != 'RESOURCE_BUSY':
                raise NativeReaderUnavailable(app_id, cause_code=raced.code) from raced
            # The other cold reader may have won the same launch slot. It is
            # useful only if the authoritative current receipt now verifies.
        except (OSError, TimeoutError) as unavailable:
            raise NativeReaderUnavailable(app_id) from unavailable
        context.current()
        try:
            launches.current(app_id)
        except LaunchStatus as unavailable:
            raise NativeReaderUnavailable(app_id, cause_code=unavailable.code) from unavailable
    context.current()


def scoped_set(context, set_id, revision):
    registry, sets, _ = context.services.asset_library
    asset_set = sets.get(context.workspace_id, set_id, revision)
    from .references import asset_allowed
    if any(not asset_allowed(context, item.asset_id) for item in asset_set.items):
        raise ValueError('资料集合包含会话选中范围外的资料')
    for item in asset_set.items:
        asset = registry.get(context.workspace_id, item.asset_id)
        if (asset.revision, asset.blob_hash) != (item.asset_revision, item.blob_hash):
            raise ValueError('资料集合版本已失效')
    return asset_set


def attempt_set(context, asset_set):
    require_write(context)
    context.current()
    # Handoff submission parameters are immutable. A different engine/model
    # gets a new attempt set and native request, preserving its parent lineage.
    parents = {item.item_id: (asset_set.asset_set_id, asset_set.revision, item.item_id) for item in asset_set.items}
    return context.services.asset_library[1].create(context.workspace_id, asset_set.items,
        parents=parents, asset_set_id='s' + context.operation_id[1:])


def read_job(context, job_id):
    jobs = context.services.job_service
    return jobs.refresh(context.workspace_id, jobs.ledger.get(context.workspace_id, job_id))


def scoped_handoff(context, service, handoff_id):
    row = service._row(context.workspace_id, handoff_id)
    scoped_set(context, row['asset_set_id'], row['asset_set_revision'])
    if service is getattr(context.services, 'ocr_handoff', None):
        ensure_read_service(context, 'ocr', row['link_id'])
    return row


def model_projection(context):
    """Query live catalogs only; a catalog read never starts a native service."""
    services = context.services
    items = []
    for family in ('ocr', 'classification'):
        role = 'ocr_engine' if family == 'ocr' else 'classifier'
        state = availability(services, family)
        links = [link for link in services.workspace_store.list_links(context.workspace_id) if link.app_id == family]
        if state['state'] != 'ready' or not links:
            items.append({'id': '', 'role': role, 'tool_family': family, 'ready': False,
                          'workspace_id': context.workspace_id, 'input_kinds': ['image'] if family == 'classification' else ['image', 'pdf_page'],
                          'supported_tasks': ['classification'] if family == 'classification' else ['text_recognition'],
                          'version_ref': None, 'device': None,
                          'limits': ['原生服务及当前项目准备完成后才能核实模型目录'],
                          'reason': state['reason'] or '请先准备并关联当前项目'})
            continue
        for link in links[:20]:
            try:
                bound = services.job_service._context(context.workspace_id, family, link.link_id)
                adapter = services.job_service._adapter(bound)
                if family == 'classification':
                    labels = adapter.project()['labels']
                    models = adapter.models()[:100]
                    if not models:
                        items.append({'id': '', 'role': role, 'tool_family': family,
                                      'workspace_id': context.workspace_id, 'project_id': link.module_project_id,
                                      'link_id': link.link_id, 'labels': labels, 'ready': False,
                                      'input_kinds': ['image'], 'supported_tasks': ['classification'],
                                      'version_ref': None, 'device': None,
                                      'limits': ['仅使用当前原生项目的标签体系'], 'reason': '当前原生项目尚无训练模型'})
                    for model in models:
                        metadata = model.get('metadata') or {}
                        provenance = metadata.get('provenance') or {}
                        items.append({'id': model['id'], 'role': role, 'tool_family': family,
                                      'workspace_id': context.workspace_id, 'project_id': link.module_project_id,
                                      'link_id': link.link_id, 'labels': labels, 'ready': True,
                                      'input': 'image', 'output': 'prediction', 'input_kinds': ['image'],
                                      'supported_tasks': [metadata.get('task_type', 'single_label')],
                                      'version_ref': {'model_version_id': metadata.get('model_version_id', model['id']),
                                                      'model_onnx_sha256': provenance.get('model_onnx_sha256')},
                                      'device': 'selected_at_submission', 'supported_devices': ['cpu', 'cuda'],
                                      'limits': ['仅当前原生项目标签', '预测不修改人工标签',
                                                 '目录登记不替代运行时模型包校验',
                                                 'GPU当前可用性、占用与模型加载由原生运行时核实']})
                else:
                    capabilities = adapter.capabilities()
                    engines = adapter._request('GET', '/api/state').get('engines', {})
                    for key, value in engines.items():
                        if not isinstance(value, dict):
                            continue
                        supported = value.get('capabilities') or {}
                        items.append({'id': key, 'role': role, 'tool_family': family,
                                      'workspace_id': context.workspace_id, 'project_id': link.module_project_id,
                                      'link_id': link.link_id, 'ready': capabilities.recognition and value.get('ready', True) is True,
                                      'reason': capabilities.reason or value.get('reason', ''),
                                      'details': value, 'input': 'image_or_page', 'output': 'text_or_table',
                                      'input_kinds': ['image', 'pdf_page'],
                                      'supported_tasks': [name for name in ('text', 'tables') if supported.get(name) is True],
                                      'version_ref': {'package_id': value.get('package_id'), 'models': value.get('models', [])},
                                      'device': 'managed_native', 'device_route': value.get('route'),
                                      'limits': ['ready表示识别队列可提交，模型加载与文件校验仍由原生执行器完成',
                                                 'GPU当前可用性、占用与模型加载由原生运行时核实',
                                                 '原生目录未提供模型文件哈希或已加载设备；不作推测']})
                    visual = adapter._request('GET', '/api/multimodal/models')
                    for model in visual.get('models', [])[:100]:
                        items.append({'id': model['id'], 'role': 'visual_review', 'tool_family': 'ocr',
                            'workspace_id': context.workspace_id, 'project_id': link.module_project_id, 'link_id': link.link_id,
                            'ready': model.get('available') is True, 'reason': model.get('reason', ''),
                            'backend': model.get('backend', 'local'), 'input_kinds': ['image', 'ocr_result'],
                            'supported_tasks': ['visual_text_review'], 'version_ref': {'model_id': model['id']},
                            'device': 'native_configured', 'limits': ['专业模型建议不自动采用', '外部模型通过原生已配置连接发送图像']})
            except ValueError:
                items.append({'id': '', 'role': role, 'tool_family': family,
                              'workspace_id': context.workspace_id, 'project_id': link.module_project_id,
                              'link_id': link.link_id, 'ready': False, 'version_ref': None, 'device': None,
                              'limits': ['原生目录暂不可核实'], 'reason': '原生目录暂不可核实'})
    if not any(item['role'] == 'visual_review' for item in items):
        items.append({'id': '', 'role': 'visual_review', 'tool_family': 'ocr',
                  'workspace_id': context.workspace_id, 'ready': False,
                  'input_kinds': ['image'], 'supported_tasks': [], 'version_ref': None, 'device': None,
                  'limits': ['通过OCR专业视觉目录核实可用状态'], 'reason': '当前关联项目没有可核实的专业视觉模型目录'})
    return items
