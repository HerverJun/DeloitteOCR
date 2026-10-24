"""A new reader migrates history; a schema19 reader refuses route-capable data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from ocr_workbench.screen_batches import ScreenBatches, fingerprint
from ocr_workbench.store import Store, SCHEMA_VERSION, encoded


def inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob('*') if path.is_file()}


class RouteCompatibilityTests(unittest.TestCase):
    def test_schema19_migration_keeps_legacy_policy_cost_deadline_and_request(self):
        module = runpy.run_path(str(Path(__file__).with_name('test_screen_batches.py')))
        fixture = module['ScreenBatchTests']('runTest'); fixture.setUp()
        try:
            version = fixture.images(1)[0]
            batch = fixture.submit([version], budget_seconds=100)
            fixture.manager.step(); fixture.finish(failure=True); fixture.manager.step()
            fixture.manager.stop()
            policy = fixture.manager._batch(fixture.project, batch)['policy']
            policy.pop('engine_route'); policy.pop('technical_fallback_reserve_seconds')
            signature = fingerprint({'version_ids': [version], 'policy': policy,
                'export_options': None, 'parent_batch_id': None})
            with fixture.store.transaction() as db:
                db.execute('UPDATE screen_batches SET policy=?,payload_hash=? WHERE id=?', (encoded(policy), signature, batch))
                db.execute('UPDATE screen_tasks SET spent_seconds=35')
                db.execute('PRAGMA user_version=19')
            before_batch = deepcopy(fixture.manager._batch(fixture.project, batch))
            before_attempts = fixture.store.rows('SELECT * FROM screen_tasks')
            migrated = Store(fixture.store.root)
            self.assertEqual(migrated.schema_version, SCHEMA_VERSION)
            self.assertTrue(migrated.migration_backup.is_file())
            manager = ScreenBatches(migrated, fixture.queue, fixture.root, prepare=fixture.manager.prepare_override)
            try:
                manager.defaults = lambda: {'primary_engine': 'paddlevl'}
                self.assertEqual(manager._batch(fixture.project, batch), before_batch)
                self.assertEqual(migrated.rows('SELECT * FROM screen_tasks'), before_attempts)
                recovered = manager.submit(fixture.project, {'version_ids': [version],
                    'request_id': 'request-screen-1', 'policy': {'primary_engine': 'glm', 'max_upgrades': 0, 'budget_seconds': 100}})
                self.assertEqual(recovered['batch_id'], batch)
                self.assertEqual(manager._batch(fixture.project, batch)['deadline'], before_batch['deadline'])
            finally:
                manager.stop()
            before_files = inventory(fixture.store.root)
            with patch('ocr_workbench.store.SCHEMA_VERSION', 19), self.assertRaises(ValueError):
                Store(fixture.store.root)
            self.assertEqual(inventory(fixture.store.root), before_files)
        finally:
            fixture.tearDown()
