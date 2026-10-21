"""A user's formal requirement cannot be weakened by a model tool argument."""
from copy import deepcopy
from dataclasses import replace
import unittest

from workbench_platform.agent.tools import context, multimodal
import test_multimodal_security as fixtures


class FormalDeliveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.MultimodalSecurityTests.setUp
    tearDown = fixtures.MultimodalSecurityTests.tearDown
    prepare = fixtures.MultimodalSecurityTests.prepare

    def require_formal(self):
        self.prepare()
        path = self.store.root / 'fixed-result.json'
        path.write_text('{}', encoding='utf-8')
        self.native.snapshot_export.return_value = (path, {'result.json': path})
        self.native.file_hash.return_value = 'f' * 64
        tasks = self.services.agent.tasks
        task = tasks.get(self.ctx.session_id)
        tasks.update(self.ctx.session_id, {'request_id': 'formal-setting', 'expected_revision': task['revision'],
            'delivery_policy': 'reviewed'})

    def test_model_false_flag_still_waits_for_version_bound_user_confirmation(self):
        self.require_formal()
        result = context.delivery(context.Delivery(summary='正式完成', result_refs=[self.ref], require_review=False), self.ctx)
        self.assertEqual(result['status'], 'needs_user')
        self.assertEqual(result['result_refs'][0]['object_id'], 'ms1')
        self.native.review.assert_not_called()
        self.assertFalse(self.services.agent.tasks.get(self.ctx.session_id).get('delivery_requirements', {}).get('history'))

    def test_draft_preview_does_not_make_formal_delivery_inherit_unreviewed_files(self):
        self.require_formal()
        draft = context.delivery(context.Delivery(summary='未复核预览', finalize=False, result_refs=[self.ref]), self.ctx)
        self.assertTrue(draft['data']['draft'])
        self.assertTrue(draft['data']['provisional'])
        requirements = self.services.agent.tasks.get(self.ctx.session_id)['delivery_requirements']
        self.assertEqual(requirements['stages'], [])
        self.assertEqual(len(requirements['provisional_stages']), 1)
        old = deepcopy(self.snapshot)
        confirmed = deepcopy(self.snapshot)
        confirmed.update(snapshot_id='ms2', revision=2, review_revision=2, snapshot_hash='e' * 64, review_state='reviewed')
        confirmed['analysis'].update(revision=2, review={'assessment': 'confirmed', 'actor': 'Actual user', 'request_id': 'user-confirmation'})
        self.native.snapshot.side_effect = lambda _w, _job, snapshot_id=None: deepcopy(confirmed if snapshot_id == 'ms2' else old)
        ref = multimodal.result_ref(self.ctx, confirmed)
        result = context.delivery(context.Delivery(summary='核准后交付', result_refs=[ref], require_review=False),
                                  replace(self.ctx, operation_id='formal-final'))
        self.assertEqual(result['status'], 'success', result)
        self.assertFalse(result['data']['draft'])
        self.assertEqual([value['object_id'] for value in result['result_refs']], ['ms2'])
        history = self.services.agent.tasks.get(self.ctx.session_id)['delivery_requirements']['history']
        self.assertEqual(history[0]['provisional_stages'][0]['result_refs'][0]['object_id'], 'ms1')

    def test_prepare_result_cannot_be_used_as_business_delivery(self):
        self.prepare()
        self.snapshot['analysis']['purpose'] = 'preparation'
        result = context.delivery(context.Delivery(summary='业务分析完成', result_refs=[self.ref]), self.ctx)
        self.assertEqual(result['status'], 'error')
        self.assertTrue(any(item['name'] == 'business_analysis_required' for item in result['data']['delivery_checks']))
