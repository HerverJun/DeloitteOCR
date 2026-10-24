"""Native batch API supplies same-batch stage clocks to the conservative ETA."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy
import unittest


class ScreenEtaIntegrationTests(unittest.TestCase):
    def test_api_eta_uses_current_queued_phase_and_does_not_guess_cold_or_paused_time(self):
        fixture_module = runpy.run_path(str(Path(__file__).resolve().with_name('test_screen_batches.py')))
        fixture = fixture_module['ScreenBatchTests']()
        fixture.setUp()
        try:
            batch = fixture.submit(fixture.images(6))
            self.assertIsNone(fixture.manager.get(fixture.project,batch)['eta']['remaining_seconds'])
            fixture.manager.step()
            self.assertEqual(fixture.manager.get(fixture.project,batch)['eta']['reason'],'insufficient_samples')
            begin = datetime(2026,10,7,tzinfo=timezone.utc)
            for index,duration in enumerate((300,10,12,14)):
                task = fixture.store.claim()
                fixture.store.complete(task['id'],{'text':'data','tables':[fixture_module['TABLE']],
                    'engine':task['engine'],'project_image_version':task['version_id']})
                started = begin+timedelta(seconds=index*400)
                with fixture.store.transaction() as db:
                    db.execute('UPDATE tasks SET started=?,finished=? WHERE id=?',
                        (started.isoformat(),(started+timedelta(seconds=duration)).isoformat(),task['id']))
            eta = fixture.manager.get(fixture.project,batch)['eta']
            self.assertEqual(eta['remaining_seconds'],26)
            self.assertEqual((eta['sample_count'],eta['pending_tasks']),(3,2))
            self.assertEqual(eta['stage']['label'],'GLM首轮识别')
            self.assertIn('future_upgrades',eta['not_included'])
            fixture.manager.action(fixture.project,batch,{'action':'pause'})
            paused = fixture.manager.get(fixture.project,batch)['eta']
            self.assertIsNone(paused['remaining_seconds'])
            self.assertEqual(paused['reason'],'paused')
        finally:
            fixture.tearDown()


if __name__=='__main__':
    unittest.main()
