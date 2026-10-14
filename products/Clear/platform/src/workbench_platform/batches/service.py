from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from pydantic import ValidationError
from packages.contracts.automation import BatchTemplateCreate, BatchRequest, BatchControl, BatchDisposition
from workbench_platform.data_api.service import DataError
from .data import DataBatchRunner, issue, validate_config
from .store import BatchStore, now, present_batch

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
        app.state.artifact_center.batch_store = self.store
        self.data = DataBatchRunner(app.state.data_workbench, app.state.asset_library[0],
                                    app.state.artifact_center, root / 'data', self.store)
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
        batch, counts, _ = self.store.snapshot(batch_id)
        self.workspace(batch['workspace_id'])
        return self._present(batch, counts)

    def _present(self, batch, counts):
        batch = present_batch(batch, counts)
        if batch['delivery'].get('bundle_id'):
            try:
                review = self.app.state.artifact_center.get(batch['workspace_id'], batch['delivery']['bundle_id'])
                batch['delivery']['status'] = review['status']
                batch['artifacts'] = [*batch['artifacts'], review]
            except ValueError:
                pass
        return batch

    def review_export(self, batch_id, payload):
        from workbench_platform.artifacts.store import ArtifactUnavailable
        self.get(batch_id)
        operation = self.store.begin_review(batch_id, payload)
        import json
        receipt = json.loads(operation['receipt'])
        if receipt:
            return receipt
        try:
            bundle = self.app.state.artifact_center.publish_batch_review(operation)
        except (ArtifactUnavailable, OSError, ValueError) as error:
            if isinstance(error, DataError):
                raise
            reason = '正在生成，请重试同一成果请求' if getattr(error, 'code', None) == 'PUBLISHING' else '复核成果生成未完成；保留原请求和旧成果，可重试同一请求'
            raise DataError(reason, 409) from error
        # The bundle becomes visible before this CAS; a crash can only leave
        # a readable outdated snapshot. Replay attaches this original bundle.
        response = self.store.finish_review(operation['request_id'], bundle)
        return response

    def issue_page(self, batch_id, offset=0, limit=30, disposition=None):
        batch, counts, page = self.store.snapshot(batch_id, offset, limit, disposition)
        self.workspace(batch['workspace_id'])
        batch = self._present(batch, counts)
        return {'schema_version': '1.0', 'batch_id': batch_id, 'revision': batch['revision'],
                **page,
                'summary': batch['summary'], 'coverage': batch['coverage']}

    def disposition_history(self, batch_id, offset=0, limit=30, item_id=None):
        self.get(batch_id)
        return self.store.history(batch_id, offset, limit, item_id=item_id)

    def operation(self, batch_id, operation_id):
        operation = self.store.operation(operation_id)
        self.get(batch_id)
        if operation['batch_id'] != batch_id:
            raise DataError('操作不属于此批次', 404)
        return operation

    def native_revisions(self, batch_id, item_id, offset=0, limit=30):
        batch = self.get(batch_id)
        item = self.store.selected_items(batch_id, [item_id])[0]
        return self.native.revisions(batch, item, offset, limit)

    def recover_operation(self, batch_id, operation_id):
        self.operation(batch_id, operation_id)
        with self.lock:
            operation = self.store.operation(operation_id, private=True)
            return self._finish_operation(self.store.get(batch_id), operation)

    def _finish_operation(self, batch, operation):
        if operation['state'] == 'committed':
            if operation['receipt'].get('outcome') == 'conflict':
                raise DataError(operation['receipt']['reason'], 409)
            return operation['receipt']['batch']
        if operation['intent']['scenario'] == 'data_compare':
            return self.store.commit_operation(operation['operation_id'])['batch']
        return self.native.finish(batch, operation, self.store)['batch']

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
        with self.lock:
            previous = self.store.request_operation(batch_id, value)
            if previous:
                return self._finish_operation(batch, previous)
            if value['expected_revision'] != batch['revision']:
                raise DataError('处置版本冲突，请重新检查所见内容，不能自动补最新版本', 409)
            if batch['summary'].get('pending_operations'):
                raise DataError('已有处置待核实，请先恢复原操作 ' + batch['summary']['pending_operation']['operation_id'], 409)
            if batch['template']['scenario'] == 'data_compare':
                return self.store.disposition(batch_id, value)['batch']
            targets = self.store.selected_items(batch_id, value['item_ids'])
            try:
                origin, specifications = self.native.prepare(batch, targets, value, self.store)
            except DataError:
                raise
            except Exception as error:
                raise DataError('原生来源或所见版本暂不可核对，未派发处置', 409) from error
            operation = self.store.begin_operation(batch_id, value, origin=origin, segments=specifications)
            return self._finish_operation(batch, operation)

    def _run(self, batch_id):
        batch = self.store.get(batch_id)
        try:
            native = batch['template']['scenario'] != 'data_compare'
            business_receipts = (any(key in batch['receipts'] for key in
                ('native_import', 'native_dispatch', 'native_submission')) if native else bool(batch['receipts']))
            if batch['cancel_requested']:
                # A restart still reconciles an active child operation before terminal cancellation.
                if not business_receipts:
                    self.store.update(batch_id, status='cancelled', stage='cancelled')
                    return
            runner = self.native if native else self.data
            self.store.update(batch_id, status='running' if not batch['cancel_requested'] else 'cancelling')

            def checkpoint(stage, receipt):
                batch['receipts'][stage] = receipt
                changes = {'stage': stage, 'receipts': batch['receipts']}
                if stage == 'results':
                    self.store.clear_technical_issues(batch_id)
                    changes['summary'] = receipt['summary']
                    if 'issues' in receipt:
                        if batch['template']['scenario'] != 'data_compare':
                            fixed = receipt['issues']
                            identity = batch['receipts']['native_snapshot']['sha256']
                            # The immutable snapshot covers every returned native
                            # item. Existing human labels remain explicit platform
                            # decisions instead of being auto-confirmed here.
                            self.store.begin_index(batch_id, identity, len(fixed), len(fixed), 0, 0)
                            coverage = self.store.coverage(batch_id, len(batch['inputs']))
                            for start in range(coverage['next_ordinal'], len(fixed), 1000):
                                selected = [{**item, 'source': {**item['source'], 'result_id': identity},
                                    'raw_output': {**item['raw_output'], 'ordinal': start + offset}}
                                    for offset, item in enumerate(fixed[start:start + 1000])]
                                self.store.index_page(batch_id, identity, start,
                                    [{'ordinal': start + offset} for offset in range(len(selected))], selected)
                            if not fixed:
                                self.store.complete_empty_index(batch_id)
                        changes['issues'] = receipt['issues']
                if stage == 'generated':
                    changes['artifact_state'] = 'generated'
                self.store.update(batch_id, **changes)

            def cancelled():
                return self.closing.is_set() or self.store.get(batch_id)['cancel_requested']
            cancelled.stopping = self.closing.is_set

            if native:
                preparation = runner.prepare_runtime(batch, checkpoint, cancelled)
                if preparation:
                    preparation['issues'] = self._merge_issues(batch_id, preparation['issues'])
                    self.store.update(batch_id, **preparation)
                    return
            if not business_receipts:
                self.store.update(batch_id, status='running', stage='preflight')
                problems = runner.preflight({**batch['template'], 'workspace_id': batch['workspace_id']}, batch['inputs'])
                if problems:
                    self.store.update(batch_id, status='waiting_input', stage='preflight', issues=self._merge_issues(batch_id, problems))
                    return
            result = runner.run(batch, checkpoint, cancelled)
            if 'issues' in result:
                result['issues'] = self._merge_issues(batch_id, result['issues'])
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
