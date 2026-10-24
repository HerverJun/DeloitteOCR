import unittest
from datetime import datetime, timedelta, timezone

from ocr_workbench.screen_eta import estimate_screen_eta


class ScreenEtaTests(unittest.TestCase):
    def rows(self):
        begin = datetime(2026, 10, 7, tzinfo=timezone.utc)
        rows = []
        for index, duration in enumerate((300, 10, 12, 14)):
            started = begin + timedelta(seconds=index * 400)
            rows.append({'id': str(index), 'kind': 'ocr', 'engine': 'hunyuan', 'pass_index': 0,
                         'status': 'succeeded', 'started': started.isoformat(),
                         'finished': (started + timedelta(seconds=duration)).isoformat(), 'spent_seconds': 0})
        rows += [{'id': 'next-1', 'kind': 'ocr', 'engine': 'hunyuan', 'pass_index': 0, 'status': 'queued'},
                 {'id': 'next-2', 'kind': 'ocr', 'engine': 'hunyuan', 'pass_index': 0, 'status': 'queued'}]
        return rows

    def test_cold_load_is_not_multiplied_and_future_stages_are_explicit(self):
        result = estimate_screen_eta(self.rows(), pending_images=200)
        self.assertEqual(result['remaining_seconds'], 26)
        self.assertEqual(result['sample_count'], 3)
        self.assertEqual(result['pending_tasks'], 2)
        self.assertEqual(result['scope'], 'current_queued_stage')
        self.assertIn('future_upgrades', result['not_included'])
        self.assertIn('human_review', result['not_included'])

    def test_caches_failures_and_other_models_do_not_supply_warm_samples(self):
        rows = self.rows()
        rows[3]['kind'] = 'screen_cached'
        rows.extend([{**rows[1], 'id': 'failed', 'status': 'failed'},
                     {**rows[1], 'id': 'other', 'engine': 'glm'},
                     {**rows[-1], 'id': 'cached', 'kind': 'screen_cached'}])
        result = estimate_screen_eta(rows)
        self.assertIsNone(result['remaining_seconds'])
        self.assertEqual(result['sample_count'], 2)
        self.assertEqual(result['cached_pending_tasks'], 1)

    def test_resumed_segments_and_current_elapsed_are_counted_once(self):
        rows = self.rows()
        rows[1].update(spent_seconds=2)
        rows[-1].update(status='running', started='2026-10-07T01:00:00+00:00', spent_seconds=2)
        now = datetime(2026, 10, 7, 1, 0, 3, tzinfo=timezone.utc).timestamp()
        result = estimate_screen_eta(rows, timestamp=now)
        self.assertEqual(result['remaining_seconds'], 21)

    def test_pause_cold_start_and_overdue_live_task_have_no_false_eta(self):
        self.assertIsNone(estimate_screen_eta(self.rows(), batch_status='paused')['remaining_seconds'])
        self.assertIsNone(estimate_screen_eta(self.rows()[2:])['remaining_seconds'])
        rows = self.rows()
        rows[-1].update(status='running', started='2026-10-07T01:00:00+00:00')
        now = datetime(2026, 10, 7, 1, 1, tzinfo=timezone.utc).timestamp()
        result = estimate_screen_eta(rows, timestamp=now)
        self.assertIsNone(result['remaining_seconds'])
        self.assertEqual(result['reason'], 'active_task_exceeds_observed_duration')


if __name__ == '__main__':
    unittest.main()
