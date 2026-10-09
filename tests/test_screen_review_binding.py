"""Same-revision human review transitions must reject an older UI draft."""
import unittest
import test_screen_batches as fixture
from ocr_workbench.store import Conflict


class ScreenReviewBindingTests(unittest.TestCase):
    setUp=fixture.ScreenBatchTests.setUp
    tearDown=fixture.ScreenBatchTests.tearDown
    images=fixture.ScreenBatchTests.images
    submit=fixture.ScreenBatchTests.submit
    finish=fixture.ScreenBatchTests.finish
    def test_same_revision_epoch_is_pinned_and_rejection_preserves_human_state(self):
        versions=self.images(1);batch=self.submit(versions)
        self.manager.step();self.finish();self.manager.step()
        detail=self.manager.detail(self.project,batch,versions[0]);result=detail['result']
        body={'action':'review','version_ids':versions,'result_id':result['id'],
              'expected_revision':result['revision'],'expected_review_token':detail['review_binding_token'],'state':'accepted'}
        self.store.set_review(detail['image_id'],'question',result['id'],result['revision'],versions[0])
        with self.assertRaises(Conflict):self.manager.action(self.project,batch,body)
        current=self.manager.detail(self.project,batch,versions[0])
        self.assertNotEqual(current['review_binding_token'],detail['review_binding_token'])
        self.assertEqual(self.store.rows('SELECT status FROM reviews WHERE image_id=?',(detail['image_id'],))[0]['status'],'question')
        body['expected_review_token']=current['review_binding_token']
        self.manager.action(self.project,batch,body)
        self.assertTrue(self.manager.delivery_snapshot(self.project,batch)['fresh'])
