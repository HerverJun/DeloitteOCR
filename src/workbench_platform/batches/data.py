from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from workbench_platform.data_api.service import DataError, ACTIVE
from .store import encoded, fingerprint


def issue(category, title, detail='', source=None, raw_output=None, key=None):
    return {'item_id': 'i' + fingerprint(key or [category, title, source, raw_output])[:32],
            'category': category, 'title': title, 'detail': detail, 'source': source or {},
            'raw_output': raw_output or {}, 'disposition': 'pending', 'note': ''}


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {'roles', 'comparison'}:
        raise DataError('两表模板须包含左右资料规则及明确比对政策')
    if not isinstance(config['roles'], dict) or set(config['roles']) != {'left', 'right'}:
        raise DataError('模板须明确左右输入角色')
    final_headers = {}
    for role, value in config['roles'].items():
        if not isinstance(value, dict):
            raise DataError('输入角色规则须为明确配置')
        columns = value.get('columns', [])
        headers = [column.get('header') for column in columns if isinstance(column, dict)]
        if (not columns or len(headers) != len(columns) or any(not isinstance(name, str) or not name for name in headers)
                or len(set(headers)) != len(headers)):
            raise DataError(f'{role} 资料列必须使用明确且唯一的列名')
        if not value.get('formats') or any(fmt not in {'csv', 'xlsx', 'parquet'} for fmt in value['formats']):
            raise DataError('资料格式配置无效')
        steps = value.get('steps', [])
        if not isinstance(steps, list) or len(steps) > 250:
            raise DataError('清洗步骤配置无效')
        known = set(headers)
        for step in steps:
            if step.get('operator') not in {'trim', 'map', 'rename', 'cast', 'filter', 'duplicates'}:
                raise DataError('模板含不支持的清洗步骤')
            parameters = step.get('parameters', {})
            refs = ([parameters['column']] if 'column' in parameters else []) + parameters.get('columns', [])
            if any(name not in known for name in refs):
                raise DataError('清洗步骤须按明确列名映射')
            if step['operator'] == 'rename':
                new_header = parameters.get('header')
                if not isinstance(new_header, str) or not new_header or new_header in known - {parameters['column']}:
                    raise DataError('重命名后的列名须唯一且非空')
                known.remove(parameters['column'])
                known.add(new_header)
        final_headers[role] = known
    spec = config['comparison']
    if not isinstance(spec, dict) or not spec.get('key_pairs'):
        raise DataError('请明确业务匹配键，不默认选择第一列')
    if spec.get('type_policy', 'strict') not in {'strict', 'text'} or spec.get('null_policy', 'never_match') not in {'never_match', 'equal'}:
        raise DataError('类型或空值政策无效')
    from decimal import Decimal, InvalidOperation
    try:
        tolerance = Decimal(spec.get('amount_tolerance', '0'))
        if not tolerance.is_finite() or tolerance < 0:
            raise InvalidOperation()
    except (InvalidOperation, ValueError, TypeError):
        raise DataError('金额容差须为非负十进制字符串') from None
    for field in ('key_pairs', 'compare_pairs', 'amount_pairs'):
        for pair in spec.get(field, []):
            if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(name, str) or not name for name in pair):
                raise DataError('比对列须明确左右列名')
            if pair[0] not in final_headers['left'] or pair[1] not in final_headers['right']:
                raise DataError('比对列不在已确认的清洗后结构中')
    if not {tuple(pair) for pair in spec.get('amount_pairs', [])} <= {tuple(pair) for pair in spec.get('compare_pairs', [])}:
        raise DataError('金额列必须同时属于已确认的比对列')


class DataBatchRunner:
    def __init__(self, data, registry, center, root, store=None):
        self.data, self.registry, self.center = data, registry, center
        self.store = store
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def from_session(self, session_id):
        session = self.data.get(session_id)
        state = session['state']
        if not state.get('result_id') or not state.get('comparison'):
            raise DataError('请先完成并确认一次两表比对，再保存处理模板', 409)
        config = {'roles': {}, 'comparison': dict(state['comparison']['spec'])}
        for side in ('left', 'right'):
            table = state['tables'][side]
            original_revision = table['history'][0]['revision']
            original = self.data.page(session_id, 'table', original_revision, 0, 1000)
            columns = original['metadata']['columns']
            names = {column['column_id']: column['header'] for column in columns}
            if any(not header for header in names.values()) or len(set(names.values())) != len(names):
                raise DataError('原始资料含空列名或重名，请先明确资料结构', 409)
            kinds_by_column = [set() for _ in columns]
            offset = 0
            while True:
                for row in original['rows']:
                    for index, cell in enumerate(row['cells']):
                        if cell['kind'] != 'null':
                            kinds_by_column[index].add(cell['kind'])
                offset += len(original['rows'])
                if offset >= original['total']:
                    break
                if not original['rows']:
                    raise DataError('固定原表分页未覆盖全部资料', 409)
                original = self.data.page(session_id, 'table', original_revision, offset, 1000)
            expected = [{'header': column['header'], 'kinds': sorted(kinds_by_column[index])}
                        for index, column in enumerate(columns)]
            steps = []
            for record in table['history']:
                step = record.get('step')
                if not step:
                    continue
                parameters = dict(step['parameters'])
                if 'column' in parameters:
                    parameters['column'] = names[parameters['column']]
                if 'columns' in parameters:
                    parameters['columns'] = [names[column] for column in parameters['columns']]
                steps.append({'operator': step['operator'], 'parameters': parameters})
                if step['operator'] == 'rename':
                    names[step['parameters']['column']] = parameters['header']
            config['roles'][side] = {'formats': [table['format']], 'columns': expected,
                                      'import_options': table['options'], 'steps': steps}
            final = {column['column_id']: column['header'] for column in table['columns']}
            for field in ('key_pairs', 'compare_pairs', 'amount_pairs'):
                config['comparison'][field] = [list(pair) for pair in config['comparison'].get(field, [])]
                index = 0 if side == 'left' else 1
                for pair in config['comparison'][field]:
                    pair[index] = final[pair[index]]
        validate_config(config)
        return config

    def _probe(self, template, member):
        data = self.data
        if not data or not data.ready():
            raise DataError('数据分析环境尚未配置', 503)
        asset = self.registry.get(template['workspace_id'], member['asset_id'])
        suffix = Path(asset.original_name).suffix.lower()
        options = template['config']['roles'][member['role']].get('import_options', {})
        environment = os.environ.copy()
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        temporary = self.root / 'runtime-temp'
        temporary.mkdir(exist_ok=True)
        for variable in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
            environment[variable] = str(temporary)
        with tempfile.TemporaryDirectory(dir=self.root, prefix='preflight-') as directory:
            folder = Path(directory)
            path = folder / ('input' + suffix)
            with self.registry.stage_verified(template['workspace_id'], member['asset_id'],
                                              revision=member['asset_revision'], blob_hash=member['blob_hash']) as source:
                shutil.copyfile(source, path)
            request_path, response_path = folder / 'request.json', folder / 'response.json'
            request_path.write_text(encoded({'path': str(path), 'hash': member['blob_hash'], 'options': options}), encoding='utf-8')
            # Explicit sys.path also supports the existing embedded Python's fixed _pth.
            script = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_path(sys.argv.pop(1),run_name='__main__')"
            probe_path = Path(__file__).with_name('probe.py')
            result = subprocess.run([str(data.python), '-B', '-c', script, str(data.project_root), str(probe_path),
                                     str(request_path), str(response_path)], cwd=data.project_root, env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode != 0 or not response_path.is_file():
                raise DataError('资料预检进程未完成，请检查数据运行环境', 503)
            value = json.loads(response_path.read_text(encoding='utf-8'))
            if not value.get('ok'):
                raise DataError(value.get('error', '资料预检失败'))
            return value

    def preflight(self, template, inputs):
        problems = []
        by_role = {member['role']: member for member in inputs}
        if len(inputs) != 2 or set(by_role) != {'left', 'right'}:
            return [issue('input_role', '请选择一份左表和一份右表')]
        for side in ('left', 'right'):
            member, role = by_role[side], template['config']['roles'][side]
            try:
                asset = self.registry.get(template['workspace_id'], member['asset_id'])
                fmt = Path(asset.original_name).suffix.lower().lstrip('.')
                if fmt not in role['formats']:
                    problems.append(issue('format_changed', f'{side} 资料类型不符合模板', asset.original_name, member))
                    continue
                actual = self._probe(template, member)
                mapping = {}
                for column in actual['columns']:
                    mapping.setdefault(column['header'], []).append(column)
                for expected in role['columns']:
                    header = expected['header']
                    matches = mapping.get(header, [])
                    if not matches:
                        problems.append(issue('missing_column', f'{side} 缺少列：{header}', '请明确新的列映射并保存模板新版本', member))
                    elif len(matches) > 1:
                        problems.append(issue('duplicate_column', f'{side} 列名重复：{header}', '不能按列位置自动匹配', member))
                    else:
                        kinds = expected.get('kinds', [expected['kind']] if expected.get('kind') else [])
                        found = matches[0]['kinds']
                        if kinds and any(kind not in kinds for kind in found):
                            problems.append(issue('type_changed', f'{side} 列类型变化：{header}',
                                                  f'模板 {kinds}；本次 {found}', member))
                for header, matches in mapping.items():
                    if len(matches) > 1 and header not in {column['header'] for column in role['columns']}:
                        problems.append(issue('duplicate_column', f'{side} 列名重复：{header}', '请明确结构', member))
            except Exception as error:
                problems.append(issue('input_unavailable', f'{side} 资料不可处理', str(error) if isinstance(error, ValueError)
                                      else '输入版本已变化或原件不可用', member))
        return problems

    def _upload(self, batch, session_id, member):
        key = 'u' + fingerprint([batch['batch_id'], member['role']])[:32]
        existing = next((item for item in self.data.get(session_id)['uploads'] if item['id'] == key), None)
        if existing:
            self.data.upload_source(session_id, key)
            return existing
        asset = self.registry.get(batch['workspace_id'], member['asset_id'])
        suffix = Path(asset.original_name).suffix.lower()
        path = self.data.session_dir(session_id) / 'uploads' / (key + suffix)
        temporary = path.with_suffix(suffix + '.incoming')
        with self.registry.stage_verified(batch['workspace_id'], member['asset_id'],
                                          revision=member['asset_revision'], blob_hash=member['blob_hash']) as source:
            shutil.copyfile(source, temporary)
        os.replace(temporary, path)
        return self.data.upload(session_id, key, asset.original_name, suffix, path, member['blob_hash'],
                                asset.size_bytes, source_asset={'workspace_id': batch['workspace_id'],
                                'asset_id': member['asset_id'], 'revision': member['asset_revision'],
                                'blob_hash': member['blob_hash']})

    def run(self, batch, checkpoint, cancelled):
        receipts = batch['receipts']
        if batch['cancel_requested']:
            for stage, receipt in receipts.items():
                if not isinstance(receipt, dict) or not receipt.get('job_id') or not receipt.get('session_id'):
                    continue
                job = self.data.job(receipt['session_id'], receipt['job_id'])
                if job['status'] in ACTIVE:
                    job = self.data.cancel(receipt['session_id'], receipt['job_id'])
                    while job['status'] in ACTIVE:
                        time.sleep(0.1)
                        job = self.data.job(receipt['session_id'], receipt['job_id'])
                checkpoint(stage, {**receipt, 'status': job['status'], 'result': job['result']})
            return {'summary': batch['summary'], 'issues': batch['issues'], 'artifacts': batch['artifacts'],
                    'artifact_state': batch['artifact_state'], 'status': 'cancelled'}
        session = self.data.create(batch['template']['name'], request_key=batch['batch_id'])
        session_id = session['id']
        checkpoint('session', {'session_id': session_id})

        def operation(stage, action, payload):
            request_key = batch['batch_id'] + ':' + stage
            job = self.data.operation(request_key)
            if cancelled() and not (job and job['status'] in ACTIVE):
                raise DataError('批次已取消')
            if job is None:
                current = self.data.get(session_id)
                job = self.data.submit(session_id, action, payload, current['version'], request_key=request_key)
            elif job['status'] in {'failed', 'interrupted'}:
                job = self.data.resume_operation(request_key)
            checkpoint(stage, {'session_id': session_id, 'job_id': job['id'], 'status': job['status']})
            while job['status'] in ACTIVE:
                if cancelled() and not getattr(cancelled, 'stopping', lambda: False)():
                    self.data.cancel(session_id, job['id'])
                time.sleep(0.1)
                job = self.data.job(session_id, job['id'])
            checkpoint(stage, {'session_id': session_id, 'job_id': job['id'], 'status': job['status'], 'result': job['result']})
            if job['status'] != 'succeeded':
                raise DataError(job['error'] or '子操作未成功完成')
            return self.data.get(session_id)

        for side in ('left', 'right'):
            if cancelled():
                raise DataError('批次已取消')
            member = next(item for item in batch['inputs'] if item['role'] == side)
            upload = self._upload(batch, session_id, member)
            checkpoint('upload_' + side, {'upload_id': upload['id'], 'source': member})
            role = batch['template']['config']['roles'][side]
            operation('import_' + side, 'import', {'side': side, 'upload_id': upload['id'], 'options': role.get('import_options', {})})
            for index, step in enumerate(role.get('steps', [])):
                key = batch['batch_id'] + ':' + f'clean_{side}_{index}'
                previous = self.data.operation(key)
                if previous and previous['status'] == 'succeeded':
                    checkpoint(f'clean_{side}_{index}', {'session_id': session_id, 'job_id': previous['id'],
                                                        'status': 'succeeded', 'result': previous['result']})
                    continue
                table = self.data.get(session_id)['state']['tables'][side]
                mapping = {column['header']: column['column_id'] for column in table['columns']}
                parameters = dict(step['parameters'])
                if 'column' in parameters:
                    parameters['column'] = mapping[parameters['column']]
                if 'columns' in parameters:
                    parameters['columns'] = [mapping[name] for name in parameters['columns']]
                operation(f'clean_{side}_{index}', 'step', {'side': side, 'operator': step['operator'], 'parameters': parameters})
        state = self.data.get(session_id)['state']
        mapping = {side: {column['header']: column['column_id'] for column in state['tables'][side]['columns']}
                   for side in ('left', 'right')}
        spec = dict(batch['template']['config']['comparison'])
        for field in ('key_pairs', 'compare_pairs', 'amount_pairs'):
            spec[field] = [[mapping['left'][left], mapping['right'][right]] for left, right in spec.get(field, [])]
        session = operation('compare', 'compare', {'spec': spec})
        result_id = session['state']['result_id']
        metadata = self.index_results(batch, session_id, result_id, checkpoint, cancelled)
        coverage = metadata['coverage']
        total = metadata['row_count']
        summary = {'total': total, 'processed': total, 'succeeded': total, 'failed': 0,
                   'coverage': coverage, 'result_id': result_id, 'session_id': session_id,
                   'execution_complete': True}
        summary.update(total=total, processed=total, succeeded=total)
        checkpoint('results', {'result_id': result_id, 'summary': summary})
        if cancelled():
            return {'summary': summary, 'artifacts': [], 'artifact_state': 'not_generated', 'status': 'cancelled'}
        operation('export', 'export', {'result_id': result_id})
        export_id = receipts['export']['job_id']
        checkpoint('generated', {'export_id': export_id})
        if cancelled():
            return {'summary': summary, 'artifacts': [], 'artifact_state': 'generated', 'status': 'cancelled'}
        try:
            bundle = self.center.publish_data_export(batch['workspace_id'], session_id, export_id)
            checkpoint('collect', {'bundle_id': bundle['bundle_id'], 'export_id': export_id})
            return {'summary': summary, 'artifacts': [bundle], 'artifact_state': 'collected', 'status': 'succeeded'}
        except Exception as error:
            problems = [issue('collection_failed', '成果归集失败', str(error) if isinstance(error, ValueError)
                                      else '固定成果已保留，可重试归集', {'session_id': session_id, 'export_id': export_id})]
            return {'summary': summary, 'issues': problems, 'artifacts': [], 'artifact_state': 'collection_failed', 'status': 'partial'}

    def index_results(self, batch, session_id, result_id, checkpoint, cancelled):
        page = self.data.result_page_after(session_id, result_id, 0)
        metadata, total = page['metadata'], page['total']
        coverage = metadata['coverage']
        expected_total = (coverage['same_pairs'] + coverage['different_pairs'] + coverage['only_left'] +
                          coverage['only_right'] + coverage['ambiguous_left_rows'] + coverage['ambiguous_right_rows'])
        if (total != expected_total or coverage['left_total'] != coverage['left_categorized']
                or coverage['right_total'] != coverage['right_categorized']
                or coverage['left_total'] != coverage['same_pairs'] + coverage['different_pairs'] +
                   coverage['only_left'] + coverage['ambiguous_left_rows']
                or coverage['right_total'] != coverage['same_pairs'] + coverage['different_pairs'] +
                   coverage['only_right'] + coverage['ambiguous_right_rows']):
            raise DataError('固定结果左右覆盖不守恒', 409)
        index = self.store.begin_index(batch['batch_id'], result_id, total, total - coverage['same_pairs'],
                                       coverage['left_total'], coverage['right_total'])
        cursor = index['next_ordinal']
        while cursor < total:
            if cancelled():
                raise DataError('索引中断，已保留恢复点')
            page = self.data.result_page_after(session_id, result_id, cursor)
            if page['metadata'] != metadata or page['total'] != total:
                raise DataError('固定结果身份在索引过程中变化', 409)
            items = []
            for row in page['rows']:
                if row['category'] == 'same':
                    continue
                title = {'different': '左右内容差异', 'only_left': '仅左表存在', 'only_right': '仅右表存在',
                         'ambiguous_left': '左表重复键，未强行配对', 'ambiguous_right': '右表重复键，未强行配对'}.get(row['category'], '需核查结果')
                items.append(issue(row['category'], title, row.get('key_json', ''),
                    {'session_id': session_id, 'result_id': result_id, 'left_row_id': row.get('left_row_id'),
                     'right_row_id': row.get('right_row_id'), 'left_source_row': row.get('left_source_row'),
                     'right_source_row': row.get('right_source_row')}, row,
                    key=[batch['batch_id'], result_id, row['ordinal']]))
            self.store.index_page(batch['batch_id'], result_id, cursor, page['rows'], items)
            cursor += len(page['rows'])
            checkpoint('index', {'result_id': result_id, 'next_ordinal': cursor, 'result_rows': total})
        if total == 0:
            self.store.complete_empty_index(batch['batch_id'])
        if not self.store.coverage(batch['batch_id'], len(batch['inputs']))['index_complete']:
            raise DataError('固定结果索引覆盖未完成，已保留恢复点', 409)
        return metadata
