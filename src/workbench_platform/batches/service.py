from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from pydantic import ValidationError
from packages.contracts.automation import BatchTemplateCreate, BatchRequest, BatchControl, BatchDisposition
from workbench_platform.data_api.service import DataError
from .data import DataBatchRunner, issue, validate_config
from .store import BatchStore, now

ACTIVE = {'queued', 'running', 'waiting_resource', 'cancelling'}
RETRYABLE = {'failed', 'partial', 'waiting_input', 'needs_verification'}


def validated(model, payload):
    try:
        return model.model_validate(payload).model_dump(mode='json')
    except ValidationError:
        raise DataError('批次参数无效，请检查模板、输入角色与资料身份') from None


class BatchService:
    def __init__(self, app, root: Path):
        self.app = app
        self.store = BatchStore(root)
        self.data = DataBatchRunner(app.state.data_workbench, app.state.asset_library[0],
                                    app.state.artifact_center, root / 'data')
        from .native import NativeBatchRunner
        self.native = NativeBatchRunner(app)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='processing-batch')
        self.closing = threading.Event()
        self.lock = threading.RLock()
        self.running = set()
        for batch in self.store.list():
            if batch['status'] in ACTIVE:
                self.schedule(batch['batch_id'])

    def workspace(self, workspace_id):
        if self.app.state.workspace_store.get_workspace(workspace_id) is None:
            raise DataError('工作空间不存在', 404)

    def create_template(self, payload):
        value = validated(BatchTemplateCreate, payload)
        self.workspace(value['workspace_id'])
        if value['scenario'] == 'data_compare':
            validate_config(value['config'])
        elif not value['config'].get('link_id'):
            raise DataError('模板须绑定当前工作空间的原生项目')
        return self.store.save_template(value)

    def template_from_session(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'workspace_id', 'name', 'session_id'}
                or payload['schema_version'] != '1.0'):
            raise DataError('保存模板参数无效')
        return self.create_template({'schema_version': '1.0', 'workspace_id': payload['workspace_id'],
                                    'name': payload['name'], 'scenario': 'data_compare',
                                    'config': self.data.from_session(payload['session_id'])})

    def preflight(self, payload):
        # Preflight has no durable submission and may omit its request identity.
        value = validated(BatchRequest, {'request_key': 'preflight', **payload})
        self.workspace(value['workspace_id'])
        template = self.store.template(value['workspace_id'], value['template_id'], value['template_version'])
        runner = self.data if template['scenario'] == 'data_compare' else self.native
        issues = runner.preflight(template, value['inputs'])
        return {'ok': not issues, 'issues': issues, 'template': template, 'inputs': value['inputs']}

    def submit(self, payload):
        value = validated(BatchRequest, payload)
        self.workspace(value['workspace_id'])
        template = self.store.template(value['workspace_id'], value['template_id'], value['template_version'])
        batch, created = self.store.accept(value, template)
        if created:
            self.schedule(batch['batch_id'])
        return self.get(batch['batch_id'])

    def schedule(self, batch_id):
        with self.lock:
            if self.closing.is_set() or batch_id in self.running:
                return
            self.running.add(batch_id)
        self.pool.submit(self._run, batch_id)

    def get(self, batch_id):
        batch = self.store.get(batch_id)
        self.workspace(batch['workspace_id'])
        issues = batch['issues']
        counts = {state: sum(item.get('disposition', 'pending') == state for item in issues)
                  for state in ('pending', 'confirmed', 'deferred')}
        batch['summary'] = {'total': len(batch['inputs']), 'processed': 0, 'succeeded': 0, 'failed': 0,
                            'execution_complete': batch['status'] == 'succeeded',
                            **batch['summary'], **counts,
                            'review_complete': batch['status'] in {'succeeded', 'partial'} and not counts['pending'] and not counts['deferred']}
        batch['capabilities'] = ['cancel'] if batch['status'] in ACTIVE else ['retry'] if batch['status'] in RETRYABLE else []
        return batch

    def control(self, batch_id, payload):
        operation = validated(BatchControl, payload)['operation']
        batch = self.get(batch_id)
        if operation == 'cancel':
            if batch['status'] not in ACTIVE:
                raise DataError('本批次当前没有可取消的执行', 409)
            self.store.update(batch_id, cancel_requested=True, status='cancelling')
        else:
            if batch['status'] not in RETRYABLE:
                raise DataError('本批次当前不支持重试', 409)
            # A retry of an uncertain cancellation continues reconciliation;
            # it must not turn the original cancellation into resumed work.
            self.store.update(batch_id, status='queued', cancel_requested=batch['cancel_requested'])
            self.schedule(batch_id)
        return self.get(batch_id)

    def dispositions(self, batch_id, payload):
        value = validated(BatchDisposition, payload)
        batch = self.get(batch_id)
        known = {item['item_id'] for item in batch['issues']}
        if len(set(value['item_ids'])) != len(value['item_ids']) or not set(value['item_ids']) <= known:
            raise DataError('请选择本批次明确存在的结果范围', 409)
        if batch['template']['scenario'] != 'data_compare':
            try:
                self.native.dispose(batch, value['item_ids'], value['action'], value['note'])
            except ValueError as error:
                raise DataError(str(error), 409) from None
        self.store.disposition(batch_id, value['item_ids'], value['action'], value['note'])
        return self.get(batch_id)

    def _run(self, batch_id):
        batch = self.store.get(batch_id)
        try:
            if batch['cancel_requested']:
                # A restart still reconciles an active child operation before terminal cancellation.
                if not batch['receipts']:
                    self.store.update(batch_id, status='cancelled', stage='cancelled')
                    return
            runner = self.data if batch['template']['scenario'] == 'data_compare' else self.native
            if not batch['receipts']:
                self.store.update(batch_id, status='running', stage='preflight')
                problems = runner.preflight(batch['template'], batch['inputs'])
                if problems:
                    self.store.update(batch_id, status='waiting_input', stage='preflight', issues=self._merge_issues(batch_id, problems))
                    return
            self.store.update(batch_id, status='running' if not batch['cancel_requested'] else 'cancelling')

            def checkpoint(stage, receipt):
                batch['receipts'][stage] = receipt
                changes = {'stage': stage, 'receipts': batch['receipts']}
                if stage == 'results':
                    changes.update(summary=receipt['summary'], issues=self._merge_issues(batch_id, receipt.get('issues', [])))
                if stage == 'generated':
                    changes['artifact_state'] = 'generated'
                self.store.update(batch_id, **changes)

            def cancelled():
                return self.closing.is_set() or self.store.get(batch_id)['cancel_requested']
            cancelled.stopping = self.closing.is_set

            result = runner.run(batch, checkpoint, cancelled)
            result['issues'] = self._merge_issues(batch_id, result.get('issues', []))
            self.store.update(batch_id, **result)
        except Exception as error:
            current = self.store.get(batch_id)
            cancelled = current['cancel_requested']
            message = str(error) if isinstance(error, ValueError) else '批次处理未完成，已保留成功阶段和来源'
            status = (getattr(error, 'status', None) if getattr(error, 'status', None) in RETRYABLE else
                      'cancelled' if cancelled else
                      'needs_verification' if '需核实' in message or '恢复点' in message else 'failed')
            if self.closing.is_set() and not cancelled:
                status = 'queued'  # Service recovery owns the remaining work.
            problems = current['issues'] if cancelled else [*current['issues'], issue('execution_failed', '执行阶段未完成', message,
                       {'stage': current['stage']}, key=[batch_id, 'execution_failed', current['stage']])]
            self.store.update(batch_id, status=status, issues=self._merge_issues(batch_id, problems))
        finally:
            with self.lock:
                self.running.discard(batch_id)

    def _merge_issues(self, batch_id, issues):
        existing = {item['item_id']: item for item in self.store.get(batch_id)['issues']}
        merged = []
        for item in issues:
            previous = existing.get(item['item_id'], {})
            merged.append({**item, 'disposition': previous.get('disposition', item.get('disposition', 'pending')),
                           'note': previous.get('note', item.get('note', '')),
                           'updated_at': previous.get('updated_at', now())})
        return list({item['item_id']: item for item in merged}.values())

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True)
