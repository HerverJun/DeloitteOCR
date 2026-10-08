"""Controls during real coordinator futures, with SQLite and no OCR model."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import time
import unittest

from ocr_workbench.imaging import transform
import test_screen_batches as fixture


class PreparationControlTests(unittest.TestCase):
    setUp = fixture.ScreenBatchTests.setUp
    tearDown = fixture.ScreenBatchTests.tearDown
    images = fixture.ScreenBatchTests.images
    submit = fixture.ScreenBatchTests.submit

    @contextmanager
    def preparing(self, *, fail=False):
        self.release = threading.Event()
        entered = threading.Event()
        self.prepare_calls = []

        def blocked_prepare(source, folder, **kwargs):
            self.prepare_calls.append(str(source))
            entered.set()
            if not self.release.wait(15):
                raise RuntimeError('Test did not release preparation')
            if fail:
                raise RuntimeError('Controlled preparation failure after user control')
            return fixture.prepare(source, folder, **kwargs)

        self.manager.prepare_override = blocked_prepare
        versions = self.images(1)
        batch = self.submit(versions)
        with ThreadPoolExecutor(max_workers=2) as pool:
            step = pool.submit(self.manager.step)
            try:
                self.assertTrue(entered.wait(2))
                yield batch, versions, step, pool
            finally:
                self.release.set()
                step.result(timeout=5)

    def responsive(self, function, *args):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(function, *args)
            try:
                return future.result(timeout=2)
            except BaseException:
                self.release.set()
                raise

    def tasks(self, batch):
        return self.store.rows('SELECT * FROM screen_tasks WHERE batch_id=?', (batch,))

    def test_status_is_readable_during_actual_preparation(self):
        with self.preparing() as (batch, _, step, _):
            status = self.responsive(self.manager.get, self.project, batch)
            self.assertEqual(status['status'], 'running')
            self.assertEqual(status['counts']['pending'], 1)
            self.assertFalse(step.done())
            self.assertEqual(self.tasks(batch), [])

    def test_pause_returns_and_completed_preparation_is_reused_on_resume(self):
        with self.preparing() as (batch, _, step, _):
            paused = self.responsive(self.manager.action, self.project, batch, {'action': 'pause'})
            self.assertEqual(paused['status'], 'paused')
            self.assertFalse(step.done())
            self.release.set()
            step.result(timeout=5)
            item = self.manager._items(batch)[0]
            self.assertEqual(item['status'], 'paused')
            self.assertTrue(item['preparation'])
            self.assertEqual(self.tasks(batch), [])
            self.manager.action(self.project, batch, {'action': 'resume'})
            self.manager.step()
            self.assertEqual(len(self.prepare_calls), 1)
            self.assertEqual(len(self.tasks(batch)), 1)
            self.manager.step()
            self.assertEqual(len(self.tasks(batch)), 1)

    def test_cancel_returns_without_late_work_restoring_or_enqueuing_item(self):
        with self.preparing() as (batch, _, step, _):
            cancelled = self.responsive(self.manager.action, self.project, batch, {'action': 'cancel'})
            self.assertEqual(cancelled['status'], 'cancelled')
            self.assertFalse(step.done())
            self.release.set()
            step.result(timeout=5)
            self.assertEqual(self.manager._items(batch)[0]['status'], 'cancelled')
            self.assertEqual(self.tasks(batch), [])

    def test_late_preparation_error_does_not_overwrite_pause(self):
        with self.preparing(fail=True) as (batch, _, step, _):
            self.responsive(self.manager.action, self.project, batch, {'action': 'pause'})
            self.release.set()
            step.result(timeout=5)
            item = self.manager._items(batch)[0]
            self.assertEqual(item['status'], 'paused')
            self.assertEqual(item['stage'], 'paused')
            self.assertEqual(self.tasks(batch), [])

    def test_input_version_changed_during_preparation_is_not_enqueued(self):
        with self.preparing() as (batch, versions, step, _):
            newer = self.responsive(transform, self.store, versions[0], {'kind': 'rotate', 'degrees': 90})
            self.assertNotEqual(newer['id'], versions[0])
            self.assertEqual(self.store.one('images', newer['image_id'])['active_version'], newer['id'])
            self.release.set()
            step.result(timeout=5)
            item = self.manager._items(batch)[0]
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['stage'], 'preparation_failed')
            self.assertEqual(self.tasks(batch), [])
            self.assertIn('原图版本或内容已变化', item['error'])

    def test_expired_deadline_after_preparation_does_not_start_model(self):
        with self.preparing() as (batch, _, step, _):
            with self.store.transaction() as db:
                db.execute('UPDATE screen_batches SET deadline=? WHERE id=?', (time.time() - 1, batch))
            self.release.set()
            step.result(timeout=5)
            item = self.manager._items(batch)[0]
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['stage'], 'batch_budget_reached')
            self.assertEqual(self.tasks(batch), [])

    def test_concurrent_coordinators_do_not_prepare_or_enqueue_twice(self):
        with self.preparing() as (batch, _, step, pool):
            second = pool.submit(self.manager.step)
            self.release.set()
            step.result(timeout=5)
            second.result(timeout=5)
            self.assertEqual(len(self.prepare_calls), 1)
            self.assertEqual(len(self.tasks(batch)), 1)


if __name__ == '__main__':
    unittest.main()
