"""Scoped review adapters. All proposals keep the existing human decision UI."""
import asyncio
from collections import Counter
import json

from ocr_workbench.financial_checks import check_tables
from ocr_workbench.structure_store import refresh_proposals_in_transaction
from .contracts import ToolResult
from .policy import AgentPolicy
from .store import canonical


def register_review_tools(registry, services, operations, views, jobs=None):
    store = services.store

    def inspect(args, context):
        run = context['run']
        table_index = int(args.table_id.split(':')[1])
        operation_id = None
        if args.action == 'generate_candidates':
            def effect(db, operation_id):
                refresh_proposals_in_transaction(store, db, args.result_id, args.revision, table_indices=[table_index])
                return {'result_id': args.result_id, 'revision': args.revision, 'table_id': args.table_id}, []
            operation = operations.submit(context, 'inspect_table', args.model_dump(), effect)
            operation_id = operation['operation_id']
        with store.transaction() as db:
            current = registry.policy.agent._run(db, run['project_id'], run['id'])
            registry.policy.agent.require_generation(current, context['generation'])
            resource = AgentPolicy._owned(db, run['project_id'], 'result', args.result_id)
            if resource['revision'] != args.revision:
                from .policy import PolicyDenied
                raise PolicyDenied('stale_revision', '表格版本已改变，请重新读取')
            table = json.loads(resource['edited'])['tables'][table_index]
            financial = check_tables([table])
            kinds = set()
            if 'totals' in args.checks:
                kinds.update(('total_relation', 'total_difference'))
            if 'rounding' in args.checks:
                kinds.update(('decimal_places', 'total_difference'))
            if 'units' in args.checks:
                kinds.update(('total_relation', 'identifier_format'))
            if 'duplicates' in args.checks:
                kinds.add('year_columns')
            issues = []
            for issue in financial['issues']:
                if issue['kind'] in kinds:
                    issues.append({**issue, 'target': {**issue['target'], 'table': table_index}})
            if 'duplicates' in args.checks:
                rows = [tuple(c['text'] for c in sorted(table['cells'], key=lambda c: c['column']) if c['row'] == row)
                        for row in range(1, table['rows'])]
                counts = Counter(rows)
                issues.extend({'kind': 'duplicate_row', 'row': row + 1, 'status': 'uncertain', 'changes_text': False,
                               'message': '重复行内容，需要结合原文确认'} for row, cells in enumerate(rows) if any(cells) and counts[cells] > 1)
            proposals = []
            if 'structure' in args.checks or args.action == 'generate_candidates':
                for row in db.execute("SELECT id,state,payload FROM structure_proposals WHERE result_id=? AND revision=? ORDER BY created,id", (args.result_id, args.revision)):
                    payload = json.loads(row['payload'])
                    if table_index in payload['table_indices']:
                        proposals.append({'proposal_id': row['id'], 'state': row['state'], 'kind': payload['kind'],
                                          'can_apply': payload.get('can_apply', False), 'requires_user_decision': True})
            reference = views.evidence(db, run, resource, table_id=args.table_id)
        # Bound every user-controlled string and count before serializing the wire result.
        kept = []
        for issue in issues:
            if len(canonical(kept + [issue]).encode('utf-8')) > 7000:
                break
            kept.append(issue)
        return ToolResult(status='success', summary=f"已检查表格：{len(issues)} 条核对线索，{len(proposals)} 项结构建议；原文保留",
            data={'operation_id': operation_id, 'issues': kept, 'issue_count': len(issues), 'proposals': proposals[:15],
                  'rows': table['rows'], 'columns': table['columns'], 'checked_relations': financial['checked_relations'],
                  'automatic_correction': False, 'automatic_adoption': False, 'checks': args.checks},
            evidence_refs=[reference], truncated=len(kept) < len(issues) or len(proposals) > 15).model_dump(mode='json')

    async def inspect_async(args, context):
        return await asyncio.to_thread(inspect, args, context)

    registry.register('inspect_table', inspect_async)

    def review(args, context):
        from ocr_workbench.multimodal_store import enqueue_review_in_transaction
        config, queue = services.visual_parameters(args.visual_model_id)
        def effect(db, operation_id):
            resource = AgentPolicy._owned(db, context['run']['project_id'], 'result', args.result_id)
            if config.get('backend') == 'external':
                registry.policy.authorize_outbound(context['run'], role='visual', endpoint=config['base_url'], config_revision=config['revision'],
                    document_ids=[resource['document_id']], page_ids=[resource['page_id']], data_kinds=['image', 'text', 'metadata'])
            task_id = enqueue_review_in_transaction(store, db, args.result_id, {'revision': args.revision, 'version_id': args.version_id,
                'model_id': args.visual_model_id, 'scope': 'page', 'target_ids': args.target_ids, 'request_id': operation_id}, config)
            views.evidence(db, context['run'], resource, target_id='visual-job:' + task_id)
            return {'task_id': task_id}, [{'kind': 'visual_review', 'job_id': task_id, 'ownership': 'created', 'input_revision': args.revision, 'state': 'queued'}]
        operation = operations.submit(context, 'request_visual_review', args.model_dump(), effect, fingerprint=lambda db: config,
                                      cost={'pages': 1, 'jobs': 1})
        queue.wake.set()
        return jobs.operation_result(context['run']['project_id'], context['run']['id'], operation['operation_id'])

    async def review_async(args, context):
        return await asyncio.to_thread(review, args, context)

    if jobs is not None:
        registry.register('request_visual_review', review_async)
