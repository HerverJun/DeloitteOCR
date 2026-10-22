"""Durable one-pair document comparison, page revisions and fixed artifacts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import threading
import uuid
from functools import wraps

from pydantic import ValidationError

from workbench_platform.agent.credentials import CredentialVault
from workbench_platform.data_api.service import DataError
from workbench_platform.supervisor.processes import require_f_path
from .store import ComparisonStore, canonical, digest, identifier, now, write_json


ACTIVE = {'queued', 'running', 'cancelling'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
FILES = {'report.html', 'comparison.json', 'left.json', 'right.json', 'left.md', 'right.md', 'manifest.json'}


class Cancelled(Exception):
    pass


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


def validated(model, body):
    try:
        return model.model_validate(body).model_dump(mode='json')
    except (ValidationError, ValueError, TypeError):
        raise DataError('参数不符合要求，请核对必填字段和数值范围', 400) from None


class ComparisonService:
    def __init__(self, app, root, *, llm_call=None, prepare=None, parse=None, assemble=None, compare=None, vault=None):
        self.app = app
        self.store = ComparisonStore(root)
        self.root = self.store.root
        self.registry = app.state.asset_library[0]
        self.vault = vault or CredentialVault(self.root / 'credentials')
        self.stop = threading.Event()
        self.events = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='clear-comparison')
        from .llm import structured_call
        from .layout import prepare_pages, parse_page, assemble_document
        from .compare import extract_and_compare
        self.llm_call = llm_call or structured_call
        self.prepare = prepare or prepare_pages
        self.parse = parse or parse_page
        self.assemble = assemble or assemble_document
        self.compare = compare or extract_and_compare

    def profiles(self, workspace_id):
        values = self.store.configs(workspace_id, 'profile')
        if not values:
            from .models import ComparisonProfile
            default = ComparisonProfile().model_dump(mode='json')
            default['id'] = 'project_progress'
            self.store.save_config(workspace_id, 'profile', default)
            values = self.store.configs(workspace_id, 'profile')
        return {'items': values}

    @serialized
    def save_profile(self, workspace_id, payload):
        from .models import ComparisonProfile
        body = dict(payload)
        body['id'] = body.get('id') or 'p' + uuid.uuid4().hex
        value = validated(ComparisonProfile, body)
        return self.store.save_config(workspace_id, 'profile', value,
                                      expected_version=payload.get('version') if payload.get('id') else None)

    def connections(self, workspace_id):
        return {'items': self.store.configs(workspace_id, 'connection')}

    @serialized
    def save_connection(self, workspace_id, payload):
        from .models import ProviderConfig
        body = dict(payload)
        body.pop('has_key', None)
        body['id'] = body.get('id') or 'c' + uuid.uuid4().hex
        value = validated(ProviderConfig, body)
        identifier(value['id'])
        secret = payload.get('api_key', '')
        if not isinstance(secret, str) or len(secret) > 8192:
            raise DataError('API Key 格式无效')
        previous = None
        if payload.get('id'):
            try:
                previous = self.store.config(workspace_id, 'connection', body['id'], secret=True)
            except DataError as error:
                if error.status != 404:
                    raise
        key_ref = self.vault.save(secret) if secret else (previous or {}).get('_key_ref')
        return self.store.save_config(workspace_id, 'connection', value, key_ref,
                                      expected_version=payload.get('version') if previous else None)

    def _resolve(self, workspace_id, config_id, version):
        config = self.store.config(workspace_id, 'connection', config_id, version, secret=True)
        key_ref = config.pop('_key_ref', None)
        key = self.vault.read(key_ref) if key_ref else ''
        config.pop('has_key', None)
        return config, key

    def probe(self, workspace_id, config_id, version=None):
        from .models import ProviderConfig
        config, key = self._resolve(workspace_id, config_id, version)
        image = None
        challenge = uuid.uuid4().hex[:12]
        payload = {'challenge': challenge}
        system = 'Connection test. Return only JSON with challenge copied exactly. No tools.'
        schema = {'type': 'object', 'properties': {'challenge': {'type': 'string'}}, 'required': ['challenge'], 'additionalProperties': False}
        if config.get('supports_vision'):
            # Generated on F: by the existing isolated image runtime, never a business page.
            from .layout import probe_image
            image, expected = probe_image(self.root / 'probes' / uuid.uuid4().hex)
            payload = {'challenge': challenge, 'task': 'Read the large verification text in the image.'}
            system = 'Connection test. Return only JSON with challenge and the exact visible text in image_text.'
            schema['properties']['image_text'] = {'type': 'string'}
            schema['required'].append('image_text')
        response = self.llm_call(ProviderConfig.model_validate(config), key, system, payload, image=image, schema=schema)
        data = response['data']
        if data.get('challenge') != challenge or (image is not None and data.get('image_text', '').strip() != expected):
            raise DataError('模型未通过实际图片/结构化输出测试', 422)
        return {'passed': True, 'vision_tested': image is not None, 'usage': response.get('usage'), 'model': response.get('model')}

    def suggest(self, workspace_id, body):
        from .models import ComparisonProfile, ProviderConfig
        instructions = body.get('instructions', '')
        if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 8000:
            raise DataError('请填写比较要求（最多8000字）')
        config, key = self._resolve(workspace_id, body.get('provider_id'), body.get('provider_version'))
        response = self.llm_call(ProviderConfig.model_validate(config), key,
            '将用户的文档比较目标转为配置。仅返回指定JSON。字段key使用ASCII标识符；保留原意，不添加未经要求的业务结论。',
            {'instructions': instructions, 'example': ComparisonProfile().model_dump(mode='json')}, schema=ComparisonProfile)
        result = dict(response['data'])
        result.update(id='p' + uuid.uuid4().hex, version=1, instructions=instructions)
        return validated(ComparisonProfile, result)

    def _side(self, workspace_id, body, name):
        from .models import LayoutParseConfig
        if not isinstance(body, dict) or not isinstance(body.get('assets'), list):
            raise DataError('请选择两侧材料')
        members = body['assets']
        if not 1 <= len(members) <= (1 if name == 'left' else 100):
            raise DataError('左侧须为一个PDF；右侧须为一个PPTX或最多100张页面图片')
        sources = []
        seen = set()
        for member in members:
            if not isinstance(member, dict) or member.get('asset_id') in seen:
                raise DataError('资料重复或参数无效')
            asset = self.registry.get(workspace_id, member.get('asset_id'))
            if (asset.revision, asset.blob_hash) != (member.get('asset_revision'), member.get('blob_hash')):
                raise DataError('资料版本已变化，请刷新后创建任务', 409)
            if asset.size_bytes > 128 * 1024 * 1024:
                raise DataError('单个文件超过128MiB')
            suffix = Path(asset.original_name).suffix.lower()
            allowed = {'.pdf'} if name == 'left' else (IMAGE_SUFFIXES | ({'.pptx'} if len(members) == 1 else set()))
            if suffix not in allowed:
                raise DataError('左侧支持PDF；右侧支持PPTX或有序页面图片')
            seen.add(asset.asset_id)
            sources.append({'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash,
                            'original_name': asset.original_name, 'media_type': asset.media_type, 'size_bytes': asset.size_bytes})
        config = validated(LayoutParseConfig, body.get('layout', {}))
        if config['mode'] == 'multimodal':
            connection = self.store.config(workspace_id, 'connection', config.get('provider_id'), config.get('provider_version'))
            if not connection.get('supports_vision'):
                raise DataError('版面解析连接须启用图片输入能力')
            config['provider_version'] = connection['version']
        return {'assets': sources, 'layout': config, 'pages': [], 'document': None, 'page_epoch': {}}

    @serialized
    def create(self, workspace_id, body):
        if not isinstance(body, dict):
            raise DataError('请求参数无效')
        request_id = identifier(body.get('request_id'))
        title = body.get('title', '文档主题对比')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
            raise DataError('标题需为1至160字')
        payload_hash = digest(body)
        with self.store.transaction() as db:
            existing = db.execute('SELECT body,payload_hash FROM comparison_jobs WHERE workspace_id=? AND request_id=?', (workspace_id, request_id)).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash:
                    raise DataError('请求编号已绑定其他材料或配置', 409)
                return self.public(json.loads(existing['body']))
        left, right = self._side(workspace_id, body.get('left'), 'left'), self._side(workspace_id, body.get('right'), 'right')
        profile = self.store.config(workspace_id, 'profile', body.get('profile_id'), body.get('profile_version'))
        connection = self.store.config(workspace_id, 'connection', body.get('comparison_provider_id'), body.get('comparison_provider_version'))
        value = {'comparison_id': 'cmp' + uuid.uuid4().hex, 'workspace_id': workspace_id, 'request_id': request_id,
                 'title': title.strip(), 'revision': 1, 'generation': 1, 'status': 'queued', 'stage': 'queued',
                 'left': left, 'right': right, 'profile': profile, 'comparison_provider': connection,
                 'result': None, 'files': [], 'artifacts': [], 'error': None,
                 'progress': {'total': 0, 'processed': 0, 'succeeded': 0, 'failed': 0},
                 'created_at': now(), 'updated_at': now(), 'outdated': False}
        from workbench_platform.resources.reader_features import require_features
        require_features(self.root.parent, 'document-comparisons-v1')
        with self.store.transaction() as db:
            db.execute('INSERT INTO comparison_jobs VALUES(?,?,?,?,?)', (value['comparison_id'], workspace_id, request_id, payload_hash, canonical(value)))
        self.submit(value)
        return self.public(value)

    def submit(self, value):
        event = threading.Event()
        with self.lock:
            self.events[value['comparison_id']] = event
        self.executor.submit(self.execute, value['workspace_id'], value['comparison_id'], value['generation'], event)

    def _check(self, workspace_id, job_id, generation, event):
        if self.stop.is_set() or event.is_set():
            raise Cancelled()
        value = self.store.job(workspace_id, job_id)
        if value['generation'] != generation or value['status'] == 'cancelled':
            raise Cancelled()

    def _update(self, workspace_id, job_id, generation, **changes):
        def change(value):
            if value['generation'] != generation or value['status'] == 'cancelled':
                raise Cancelled()
            value.update(changes)
        return self.store.update(workspace_id, job_id, change)

    def _call(self, value, connection, cancel, *, stage='', system='', payload=None, schema=None, image=None, epoch='', retry_epoch='', **options):
        from .models import ProviderConfig
        config, key = self._resolve(value['workspace_id'], connection['id'], connection['version'])
        schema_json = schema.model_json_schema() if hasattr(schema, 'model_json_schema') else schema
        image_identity = None
        if image is not None:
            image_identity = {k: v for k, v in image.items() if k != 'data'}
            image_identity['data_sha256'] = hashlib.sha256(image['data'].encode('ascii')).hexdigest()
        cache_key = digest({'connection': config, 'stage': stage, 'system': system, 'payload': payload,
                            'schema': schema_json, 'image': image_identity, 'epoch': epoch, 'options': options})
        saved = self.store.call(value['comparison_id'], cache_key)
        if saved and saved['status'] == 'succeeded':
            return saved['response']
        if retry_epoch and saved and saved['status'] in {'unknown', 'inflight'}:
            cache_key = digest([cache_key, retry_epoch])
            saved = self.store.call(value['comparison_id'], cache_key)
            if saved and saved['status'] == 'succeeded':
                return saved['response']
        if saved and saved['status'] in {'unknown', 'inflight'}:
            error = DataError('该次模型调用结果未知，已停止自动重发；请查看失败页并显式重跑', 409)
            error.unknown_result = True
            raise error
        cancel()
        receipt = {'stage': stage, 'started_at': now(), 'provider': {'id': config['id'], 'version': config['version'], 'model': config['model']}, 'events': []}
        self.store.save_call(value['comparison_id'], cache_key, 'inflight', receipt)
        def record(event):
            # Only response metadata/text is logged by the client, never credentials or image bytes.
            receipt['events'].append(event)
            self.store.save_call(value['comparison_id'], cache_key, 'inflight', receipt)
        try:
            response = self.llm_call(ProviderConfig.model_validate(config), key, system, payload, image=image,
                                     schema=schema, cancel=cancel, event=record, **options)
            self.store.save_call(value['comparison_id'], cache_key, 'succeeded', {**receipt, 'response': response, 'finished_at': now()})
            cancel()
            return response
        except BaseException as error:
            previous = self.store.call(value['comparison_id'], cache_key)
            if previous and previous['status'] == 'succeeded':
                raise
            code = getattr(error, 'code', '')
            known = code in {'authentication', 'authentication_failed', 'rate_limit', 'rate_limited', 'context_overflow',
                             'context_length', 'context_budget', 'image_budget', 'image_format', 'invalid_json', 'schema_validation',
                             'invalid_response', 'truncated_response', 'unsupported_capability', 'http_error', 'output_budget',
                             'CONTEXT_WINDOW_EXCEEDED', 'refusal', 'provider_unavailable'}
            self.store.save_call(value['comparison_id'], cache_key, 'failed' if known else 'unknown',
                                 {**receipt, 'error': str(error)[:1000], 'error_code': code, 'finished_at': now()})
            if not known:
                error.unknown_result = True
            raise

    def _prepare_side(self, value, side, revision_root, cancel):
        item = value[side]
        if item['pages']:
            if value.get('compare_only'):
                return item
            replacements = {}
            for page in item['pages']:
                override = page.get('layout_override')
                if page.get('status') == 'pending' and override and any(
                    override.get(key) != item['layout'].get(key) for key in ('max_image_edge', 'image_max_bytes')):
                    replacements.setdefault(digest(override), (override, []) )[1].append(page['page_number'])
            for signature, (limits, numbers) in replacements.items():
                sources = []
                for index, source in enumerate(item['assets']):
                    cancel()
                    target = revision_root / side / 'inputs' / (str(index + 1) + Path(source['original_name']).suffix.lower())
                    if not target.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with self.registry.stage_verified(value['workspace_id'], source['asset_id'], revision=source['asset_revision'], blob_hash=source['blob_hash']) as staged:
                            shutil.copyfile(staged, target)
                    sources.append(target)
                refreshed = self.prepare(sources if len(sources) > 1 else sources[0], item['source'],
                    revision_root / side / ('render-' + signature[:16]), max_pages=200 if side == 'left' else 100,
                    cancel=cancel, config=limits)
                mapping = {page['page_number']: page for page in refreshed}
                if not set(numbers) <= set(mapping):
                    raise DataError('重新渲染后页映射不一致，请核查原件')
                item['pages'] = [{**mapping[page['page_number']], 'status': 'pending', 'layout_override': limits}
                                 if page['page_number'] in numbers else page for page in item['pages']]
            return item
        paths = []
        for index, source in enumerate(item['assets']):
            cancel()
            with self.registry.stage_verified(value['workspace_id'], source['asset_id'], revision=source['asset_revision'], blob_hash=source['blob_hash']) as staged:
                target = revision_root / side / 'inputs' / (str(index + 1) + Path(source['original_name']).suffix.lower())
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged, target)
                paths.append(target)
        source = deepcopy(item['assets'][0])
        if len(paths) > 1:
            source.update(assets=deepcopy(item['assets']), original_name='页面图片组')
        pages = self.prepare(paths if len(paths) > 1 else paths[0], source, revision_root / side / 'render',
                             max_pages=200 if side == 'left' else 100, cancel=cancel, config=item['layout'])
        if not pages:
            raise DataError('材料没有可处理的页面')
        item['pages'] = [dict(page, status='pending') for page in pages]
        item['source'] = source
        return item

    def execute(self, workspace_id, job_id, generation, event):
        cancel = lambda: self._check(workspace_id, job_id, generation, event)
        try:
            cancel()
            value = self._update(workspace_id, job_id, generation, status='running', stage='rendering', error=None)
            revision_root = self.root / 'runs' / job_id / ('r' + str(value['revision']))
            revision_root.mkdir(parents=True, exist_ok=True)
            for side in ('left', 'right'):
                item = self._prepare_side(value, side, revision_root, cancel)
                value = self._update(workspace_id, job_id, generation, **{side: item})
            local_parser = None
            def local(page, config, output_dir, cancel=None):
                nonlocal local_parser
                if local_parser is None:
                    from .local_ocr import LocalOcrParser
                    local_parser = LocalOcrParser(self.app)
                return local_parser(page, config, output_dir, cancel)
            def save_page(side, index, page):
                nonlocal value
                cancel()
                value[side]['pages'][index] = page
                write_json(revision_root / side / ('page-' + str(page['page_number']) + '.json'), page)
                value = self._update(workspace_id, job_id, generation, **{side: value[side], 'progress': self._progress(value)})
            def unknown(error):
                return getattr(error, 'code', '') == 'response_unknown' or any(
                    getattr(error, key, False) for key in ('unknown_result', 'outcome_unknown', 'unknown'))
            for side in ('left', 'right'):
                attempted = set()
                for index in range(len(value[side]['pages'])):
                    cancel()
                    page = value[side]['pages'][index]
                    if index in attempted or page.get('status') == 'succeeded' or value.get('compare_only'):
                        continue
                    config = value[side]['layout']
                    page_config = page.get('layout_override') or config
                    progress = self._progress(value, side, page['page_number'])
                    value = self._update(workspace_id, job_id, generation, stage='parsing', progress=progress)
                    if page_config['mode'] == 'local_ocr':
                        if local_parser is None:
                            from .local_ocr import LocalOcrParser
                            local_parser = LocalOcrParser(self.app)
                        batch = []
                        for position in range(index, len(value[side]['pages'])):
                            candidate = value[side]['pages'][position]
                            if position in attempted or candidate.get('status') == 'succeeded':
                                continue
                            if (candidate.get('layout_override') or config) != page_config:
                                break
                            batch.append((position, candidate))
                            if len(batch) == 20:
                                break
                        attempted.update(position for position, _ in batch)
                        positions = {candidate['page_id']: position for position, candidate in batch}
                        committed_ids = set()
                        def committed(candidate, raw):
                            if raw.get('status') == 'failed':
                                parsed = {'status': 'failed', 'engine': page_config['engine'], 'blocks': [], 'tables': [],
                                          'raw_response': raw, 'error': str(raw.get('error') or raw.get('diagnostics') or '本地页面解析失败')[:1000]}
                            else:
                                parsed = self.parse(candidate, page_config,
                                    revision_root / side / ('page-' + str(candidate['page_number'])),
                                    call_local=lambda *_args, **_kwargs: raw, cancel=cancel)
                            merged = {**candidate, **parsed}
                            if merged.get('status') not in {'succeeded', 'failed', 'incomplete', 'partial'}:
                                merged.update(status='incomplete', error='解析器没有返回明确完成状态')
                            save_page(side, positions[candidate['page_id']], merged)
                            committed_ids.add(candidate['page_id'])
                        failure = None
                        try:
                            local_parser.parse_batch([candidate for _, candidate in batch], page_config,
                                revision_root / side / ('batch-' + str(index + 1) + '-g' + str(generation)),
                                cancel=cancel, committed=committed)
                        except Cancelled:
                            raise
                        except Exception as error:
                            cancel()
                            failure = error
                        for position, candidate in batch:
                            if candidate['page_id'] not in committed_ids:
                                save_page(side, position, {**candidate, 'status': 'failed', 'blocks': [], 'tables': [],
                                    'engine': page_config['engine'], 'error': str(failure or '本地批次未完整返回该页')[:1000]})
                        if failure is not None and unknown(failure):
                            raise failure
                        continue
                    def call_vlm(*, system='', payload=None, image=None, schema=None, **kwargs):
                        kwargs.pop('stage', None)
                        page_limits = kwargs.pop('config', page_config)
                        kwargs.setdefault('max_output_tokens', page_limits['max_output_tokens'])
                        connection = {'id': page_config['provider_id'], 'version': page_config['provider_version']}
                        return self._call(value, connection, cancel, stage=f'layout:{side}:{page["page_number"]}', system=system,
                                          payload=payload, image=image, schema=schema,
                                          epoch=value[side].get('page_epoch', {}).get(str(page['page_number']), ''), **kwargs)
                    try:
                        parsed = self.parse(page, page_config, revision_root / side / ('page-' + str(page['page_number'])),
                                            call_vlm=call_vlm, call_local=local, cancel=cancel)
                        merged = {**page, **parsed}
                        if merged.get('status') not in {'succeeded', 'failed', 'incomplete', 'partial'}:
                            merged.update(status='incomplete', error='解析器没有返回明确完成状态')
                    except Cancelled:
                        raise
                    except Exception as error:
                        merged = {**page, 'status': 'failed', 'error': str(error)[:1000], 'blocks': [], 'tables': []}
                        if unknown(error):
                            save_page(side, index, merged)
                            raise
                    save_page(side, index, merged)
                value[side]['document'] = self.assemble(value[side]['pages'], value[side]['source'], job_id + '-' + side, revision=value['revision'])
                from .layout import document_markdown
                exports = revision_root / 'exports'
                exports.mkdir(parents=True, exist_ok=True)
                write_json(exports / (side + '.json'), value[side]['document'])
                (exports / (side + '.md')).write_bytes(document_markdown(value[side]['document']).encode('utf-8'))
                available = list(dict.fromkeys([*value['files'], side + '.json', side + '.md']))
                value = self._update(workspace_id, job_id, generation, **{side: value[side], 'files': available})
            cancel()
            value = self._update(workspace_id, job_id, generation, stage='comparing')
            checkpoint_root = self.root / 'runs' / job_id / 'checkpoints'
            def checkpoint(key, data=None):
                cancel()
                path = checkpoint_root / (digest([value['comparison_provider'], str(key)]) + '.json')
                if data is None:
                    return json.loads(path.read_text('utf-8')) if path.exists() else None
                write_json(path, data)
                return data
            def call(**kwargs):
                return self._call(value, value['comparison_provider'], cancel, retry_epoch=value.get('comparison_epoch', ''), **kwargs)
            call.context_window = value['comparison_provider']['context_window']
            call.max_output_tokens = min(8192, value['comparison_provider']['max_output_tokens'])
            result = value.get('result') or self.compare(value['left']['document'], value['right']['document'], value['profile'], call,
                                  checkpoint=checkpoint, cancel=cancel)
            cancel()
            result.update(title=value['title'], created_at=value['created_at'], comparison_id=job_id, revision=value['revision'],
                          profile=value['profile'], page_progress=self._progress(value),
                          sources={'left': value['left']['assets'], 'right': value['right']['assets']},
                          versions={'template': 'clear-comparison-html-v1', 'comparison_provider': value['comparison_provider'],
                                    'left_layout': value['left']['layout'], 'right_layout': value['right']['layout']})
            value = self._update(workspace_id, job_id, generation, stage='reporting', result=result)
            files, artifacts = self._export(value, revision_root, cancel)
            failed = self._progress(value)['failed']
            coverage = result.get('coverage') or {}
            incomplete = any(isinstance(coverage.get(side), dict) and not coverage[side].get('complete', False) for side in ('a', 'b'))
            incomplete = incomplete or any(d.get('stage') == 'compare' for d in result.get('diagnostics', []) if isinstance(d, dict))
            value = self._update(workspace_id, job_id, generation, status='partial' if failed or incomplete else 'succeeded', stage='completed',
                                 files=files, artifacts=artifacts, progress=self._progress(value), error=None)
            self.store.archive(value)
        except Cancelled:
            pass
        except Exception as error:
            try:
                self._update(workspace_id, job_id, generation, status='failed', error=str(error)[:1500], stage='failed')
            except Cancelled:
                pass

    @staticmethod
    def _progress(value, side=None, page=None):
        pages = value['left']['pages'] + value['right']['pages']
        succeeded = sum(p.get('status') == 'succeeded' for p in pages)
        failed = sum(p.get('status') in {'failed', 'incomplete', 'partial'} for p in pages)
        return {'total': len(pages), 'processed': succeeded + failed, 'succeeded': succeeded, 'failed': failed,
                'current_side': side, 'current_page': page}

    def _export(self, value, output, cancel):
        from .report import render_report
        from .layout import document_markdown
        folder = output / 'exports'
        folder.mkdir(parents=True, exist_ok=True)
        bodies = {'report.html': render_report(value['result']), 'comparison.json': canonical(value['result'])}
        for side in ('left', 'right'):
            bodies[side + '.json'] = canonical(value[side]['document'])
            bodies[side + '.md'] = document_markdown(value[side]['document'])
        manifest = {'format': 'clear-comparison-delivery-v1', 'comparison_id': value['comparison_id'], 'revision': value['revision'],
                    'sources': value['result']['sources'], 'profile': value['profile'], 'versions': value['result']['versions'],
                    'coverage': value['progress'], 'files': {name: hashlib.sha256(text.encode('utf-8')).hexdigest() for name, text in bodies.items()}}
        bodies['manifest.json'] = canonical(manifest)
        for name, text in bodies.items():
            cancel()
            (folder / name).write_bytes(text.encode('utf-8'))
        artifacts = self._publish(value, bodies)
        return list(bodies), artifacts

    def _publish(self, value, bodies):
        center = getattr(self.app.state, 'artifact_center', None)
        if center is None:
            return []
        from .artifacts import publish
        return publish(center, value, bodies)

    def public(self, value, compact=False):
        result = deepcopy(value)
        for side in ('left', 'right'):
            item = result[side]
            pages = [{'page_number': p['page_number'], 'status': p.get('status'), 'engine': p.get('engine'),
                      'error': p.get('error'), 'hidden': p.get('hidden', False)} for p in item['pages']]
            item['pages'] = pages
            if item.get('document'):
                item['document'] = {k: v for k, v in item['document'].items() if k not in {'pages', 'tables'}}
                item['document']['pages'] = pages
            item.pop('page_epoch', None)
        if compact:
            result['result'] = None
        result['capabilities'] = ['cancel'] if result['status'] in ACTIVE else ['resume', 'rerun_pages']
        return result

    def get(self, workspace_id, job_id, revision=None):
        return self.public(self.store.job(workspace_id, job_id, revision))

    def list(self, workspace_id, offset=0, limit=20):
        values, total = self.store.jobs(workspace_id, offset, limit)
        return {'items': [self.public(v, compact=True) for v in values], 'total': total,
                'next_offset': offset + limit if offset + limit < total else None}

    @serialized
    def control(self, workspace_id, job_id, action):
        value = self.store.job(workspace_id, job_id)
        if action == 'cancel':
            if value['status'] not in ACTIVE:
                return self.public(value)
            with self.lock:
                if job_id in self.events:
                    self.events[job_id].set()
            value = self.store.update(workspace_id, job_id, lambda v: v.update(status='cancelled', stage='cancelled'))
        elif action == 'resume':
            if value['status'] in ACTIVE:
                return self.public(value)
            if value['status'] == 'succeeded':
                return self.public(value)
            if 'report.html' in value['files']:
                value = self._new_revision(value)
                with self.store.transaction() as db:
                    self.store.save_job(db, value)
            else:
                value = self.store.update(workspace_id, job_id, lambda v: v.update(status='queued', stage='queued', error=None, generation=v['generation'] + 1))
            self.submit(value)
        else:
            raise DataError('不支持的任务操作')
        return self.public(value)

    def _new_revision(self, value):
        if value['status'] in ACTIVE:
            raise DataError('请先停止当前处理再修订', 409)
        self.store.archive(value)
        value = deepcopy(value)
        value.update(revision=value['revision'] + 1, generation=value['generation'] + 1, status='queued', stage='queued',
                     result=None, files=[], artifacts=[], error=None, outdated=False)
        return value

    @serialized
    def recompare(self, workspace_id, job_id, body):
        current = self.store.job(workspace_id, job_id)
        if body.get('expected_revision') != current['revision']:
            raise DataError('任务修订已变化，请刷新', 409)
        if not all(current[side].get('pages') for side in ('left', 'right')):
            raise DataError('请先完成两侧页面准备')
        profile = self.store.config(workspace_id, 'profile', body.get('profile_id'), body.get('profile_version'))
        provider = self.store.config(workspace_id, 'connection', body.get('comparison_provider_id'), body.get('comparison_provider_version'))
        value = self._new_revision(current)
        value.update(profile=profile, comparison_provider=provider, comparison_epoch=uuid.uuid4().hex, compare_only=True)
        with self.store.transaction() as db:
            self.store.save_job(db, value)
        self.submit(value)
        return self.public(value)

    @serialized
    def rerun(self, workspace_id, job_id, body):
        from .models import LayoutParseConfig
        current = self.store.job(workspace_id, job_id)
        if body.get('expected_revision') != current['revision']:
            raise DataError('任务修订已变化，请刷新', 409)
        side, numbers = body.get('side'), body.get('pages')
        if side not in ('left', 'right') or not isinstance(numbers, list) or not numbers or any(type(n) is not int for n in numbers):
            raise DataError('请选择有效页面')
        available = {p['page_number'] for p in current[side]['pages']}
        if not set(numbers) <= available:
            raise DataError('所选页面不存在')
        override = validated(LayoutParseConfig, body['layout']) if body.get('layout') else current[side]['layout']
        if override['mode'] == 'multimodal':
            connection = self.store.config(workspace_id, 'connection', override['provider_id'], override.get('provider_version'))
            if not connection.get('supports_vision'):
                raise DataError('该连接未启用图片能力')
            override['provider_version'] = connection['version']
        value = self._new_revision(current)
        value['comparison_epoch'] = uuid.uuid4().hex
        value['compare_only'] = False
        for page in value[side]['pages']:
            if page['page_number'] in numbers:
                page.update(status='pending', layout_override=override, blocks=[], tables=[], error=None)
                value[side]['page_epoch'][str(page['page_number'])] = uuid.uuid4().hex
        with self.store.transaction() as db:
            self.store.save_job(db, value)
        self.submit(value)
        return self.public(value)

    def page(self, workspace_id, job_id, side, number, revision=None):
        value = self.store.job(workspace_id, job_id, revision)
        if side not in ('left', 'right'):
            raise DataError('材料侧无效')
        page = next((p for p in value[side]['pages'] if p['page_number'] == number), None)
        if page is None:
            raise DataError('页面不存在', 404)
        result = deepcopy(page)
        for key in ('image_path', 'path', 'output_dir', 'raw_path'):
            result.pop(key, None)
        result['revision'] = value['revision']
        result['image_url'] = f'/api/platform/v1/workspaces/{workspace_id}/comparisons/{job_id}/pages/{side}/{number}/image?revision={value["revision"]}'
        return result

    def image(self, workspace_id, job_id, side, number, revision=None):
        value = self.store.job(workspace_id, job_id, revision)
        if side not in ('left', 'right'):
            raise DataError('材料侧无效')
        page = next((p for p in value[side]['pages'] if p['page_number'] == number), None)
        if page is None:
            raise DataError('页面不存在', 404)
        path = require_f_path(Path(page.get('image_path') or page.get('path') or ''), 'Comparison page')
        if not path.is_relative_to(self.root) or not path.is_file():
            raise DataError('页面图不可用', 404)
        return path

    @serialized
    def review(self, workspace_id, job_id, side, number, body):
        current = self.store.job(workspace_id, job_id)
        if current['revision'] != body.get('expected_revision'):
            raise DataError('页面修订已变化，请刷新', 409)
        if side not in ('left', 'right') or not isinstance(body.get('blocks'), list):
            raise DataError('页面校对参数无效')
        old = next((p for p in current[side]['pages'] if p['page_number'] == number), None)
        if old is None or old.get('status') != 'succeeded':
            raise DataError('请选择已完成识别的页面')
        blocks = {b['block_id']: b for b in old.get('blocks', [])}
        if len(body['blocks']) != len(blocks) or {b.get('block_id') for b in body['blocks'] if isinstance(b, dict)} != set(blocks):
            raise DataError('校对必须保留原有全部块及其身份')
        value = self._new_revision(current)
        value['comparison_epoch'] = uuid.uuid4().hex
        page = next(p for p in value[side]['pages'] if p['page_number'] == number)
        for edited in body['blocks']:
            text = edited.get('text')
            if not isinstance(text, str) or len(text) > 100000:
                raise DataError('块文字格式无效')
            block = next(b for b in page['blocks'] if b['block_id'] == edited['block_id'])
            block.setdefault('original_text', block.get('text', ''))
            block.update(text=text, reviewed=True, reviewed_at=now())
            if 'table' in edited:
                from .layout import _table, _table_text
                table_id = block.get('table_id')
                existing = next((t for t in page.get('tables', []) if t.get('table_id') == table_id), None)
                if existing is None:
                    raise DataError('该文字块没有可校对的表格')
                try:
                    corrected = _table(edited['table'])
                except (ValueError, TypeError, KeyError):
                    raise DataError('表格结构无效，请核对行列、合并格和单元格内容') from None
                existing.setdefault('original_cells', deepcopy(existing.get('cells', [])))
                old_sources = {(cell['row'], cell['column']): cell.get('source') for cell in existing.get('cells', [])}
                for cell in corrected['cells']:
                    cell['source'] = deepcopy(old_sources.get((cell['row'], cell['column'])) or
                        {**block.get('source', {}), 'precision': 'page_only', 'box': None, 'coordinate_space': None, 'coordinate_origin': None})
                existing.update(corrected, reviewed=True, reviewed_at=now())
                # Comparison consumes block text; build it from the reviewed cells as well.
                block['text'] = _table_text(existing)
        page['reviewed'] = True
        with self.store.transaction() as db:
            self.store.save_job(db, value)
        self.submit(value)
        return self.public(value)

    def file(self, workspace_id, job_id, name, revision=None):
        value = self.store.job(workspace_id, job_id, revision)
        if name not in FILES or name not in value['files']:
            raise DataError('成果文件尚未生成', 404)
        path = self.root / 'runs' / job_id / ('r' + str(value['revision'])) / 'exports' / name
        if not path.is_file():
            raise DataError('成果文件不可用', 404)
        return path

    def close(self):
        self.stop.set()
        with self.lock:
            for event in self.events.values():
                event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.store.close()
