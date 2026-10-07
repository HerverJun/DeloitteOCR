"""Fixed strategy routing never sees truth and retains regression/incident costs."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path('F:/OCR/scripts/screen_benchmark')
TEMP = Path('F:/OCR/output/ocr-screen-benchmark-20261007/temp')


def raw(value, warnings=None):
    return {'text': value, 'warnings': warnings or [], 'tables': [{
        'rows': 1, 'columns': 1, 'cells': [{
            'row': 0, 'column': 0, 'row_span': 1, 'column_span': 1, 'text': value}]}]}


class ScreenStrategyReportTests(unittest.TestCase):
    def setUp(self):
        self.driver = runpy.run_path(str(ROOT / 'scripts/screen_strategy_report.py'))
        self.qdriver = runpy.run_path(str(ROOT / 'scripts/screen_quality_report.py'))
        self.runtime = self.qdriver['production_runtime'](ROOT, TOOLS)
        from ocr_workbench import screen_quality
        self.runtime.update(select=screen_quality.select_screen_candidates, raw=screen_quality._raw)
        self.scorer = self.qdriver['load_module'](TOOLS / 'score.py')
        self.preparation = {'policy_version': self.runtime['preparation_policy'], 'width': 200, 'height': 100,
                            'quality': {'flags': [], 'unreadable': False},
                            'business_regions': [], 'excluded_regions': []}

    def row(self, label, value, *, status='success', warnings=None):
        return {'record': {'status': status, 'engine': label, 'inference_seconds': 2 if status == 'success' else None,
                           'wall_seconds': 3, 'error_type': 'RuntimeError' if status == 'failed' else None},
                'record_path': 'F:/mock/' + label + '/record.json', 'record_sha256': 'actual-receipt',
                'result': raw(value, warnings), 'preparation': self.preparation, 'evidence': {'blocks': []}}

    def truth(self, value):
        return {'id': 'sample', 'tables': raw(value)['tables'], 'cohort': 'chinese', 'level': 'normal'}

    def freeze(self, primary, secondary):
        return self.driver['freeze_selections']({'sample': {}},
            {'primary': {'sample': primary}, 'secondary': {'sample': secondary}},
            self.runtime, [('primary', 'secondary')])

    def test_silent_wrong_automatic_primary_does_not_upgrade_to_truth_correct_secondary(self):
        receipt = self.freeze(self.row('primary', '100'), self.row('secondary', '1000'))
        frozen = receipt['strategies']['primary→secondary']['rows'][0]
        self.assertFalse(frozen['upgrade_triggered'])
        self.assertEqual(frozen['selection']['selected_id'], 'primary')
        self.assertEqual(len(frozen['attempts']), 1)
        report = self.driver['score_frozen'](receipt, [self.truth('1000')], self.scorer)
        counts = report['strategies']['primary→secondary']['counts']
        self.assertEqual(counts['automatic_silent_critical_error_cells'], 1)
        self.assertFalse(receipt['truth_opened'])

    def test_native_quality_trigger_selects_actual_secondary_without_fixing_digits(self):
        primary = self.row('primary', '100')
        primary['result']['tables'] = []
        secondary = self.row('secondary', '001000')
        before = deepcopy((primary, secondary))
        receipt = self.freeze(primary, secondary)
        frozen = receipt['strategies']['primary→secondary']['rows'][0]
        self.assertTrue(frozen['upgrade_triggered'])
        self.assertEqual(frozen['selection']['selected_id'], 'secondary')
        self.assertEqual(frozen['selection']['selected_result']['tables'][0]['cells'][0]['text'], '001000')
        self.assertEqual(frozen['selection']['modifications'], [])
        self.assertEqual((primary, secondary), before)
        report = self.driver['score_frozen'](receipt, [self.truth('001000')], self.scorer)
        self.assertEqual(report['strategies']['primary→secondary']['change_vs_primary']['counts']['fixed_critical_error_cells'], 1)

    def test_upgrade_retains_new_critical_errors_and_observed_extra_inference_cost(self):
        primary = self.row('primary', '1000', warnings=[{'code': 'table_structure_invalid'}])
        receipt = self.freeze(primary, self.row('secondary', '100'))
        report = self.driver['score_frozen'](receipt, [self.truth('1000')], self.scorer)
        route = report['strategies']['primary→secondary']
        self.assertEqual(route['change_vs_primary']['counts']['new_critical_error_cells'], 1)
        self.assertEqual(route['change_vs_primary']['counts']['new_critical_error_images'], 1)
        self.assertEqual(route['conditional_observed_cost']['extra_secondary_known_inference_seconds'], 2)
        self.assertEqual(route['quality']['totals']['automatic_critical_error_images'], 0)
        self.assertTrue(receipt['strategies']['primary→secondary']['rows'][0]['selection']['conflicts'])

    def test_failed_upgrade_is_no_vote_and_missing_timing_is_visible(self):
        primary = self.row('primary', '1000', warnings=[{'code': 'table_structure_invalid'}])
        receipt = self.freeze(primary, self.row('secondary', '', status='failed'))
        frozen = receipt['strategies']['primary→secondary']['rows'][0]
        self.assertEqual(frozen['selection']['selected_id'], 'primary')
        self.assertEqual(frozen['selection']['conflicts'], [])
        report = self.driver['score_frozen'](receipt, [self.truth('1000')], self.scorer)
        route = report['strategies']['primary→secondary']
        self.assertEqual(route['counts']['secondary_failed_attempts'], 1)
        self.assertEqual(route['conditional_observed_cost']['extra_secondary_inference_timing_unknown_attempts'], 1)
        self.assertIn('0.00（1）', self.driver['markdown'](report))

    def test_offline_breakdown_separates_failed_large_table_from_successful_primary_gain(self):
        large = {'text':'100 200 300','tables':[{'rows':3,'columns':1,'cells':[
            {'row':r,'column':0,'row_span':1,'column_span':1,'text':value}
            for r,value in enumerate(['100','200','300'])]}]}
        recovery = self.row('secondary','100')
        recovery['result'] = deepcopy(large)
        models = {'primary': {'failure': self.row('primary','',status='failed'),
                              'success': self.row('primary','100',warnings=[{'code':'table_structure_invalid'}])},
                  'secondary': {'failure': recovery, 'success': self.row('secondary','200')}}
        receipt = self.driver['freeze_selections']({'failure':{},'success':{}}, models,
                                                   self.runtime,[('primary','secondary')])
        before = deepcopy(receipt)
        truth = [{'id':'failure','tables':large['tables'],'cohort':'public','level':'normal'},
                 {**self.truth('200'),'id':'success'}]
        report = self.driver['score_frozen'](receipt,truth,self.scorer)
        delta = report['strategies']['primary→secondary']['change_vs_primary']
        technical = delta['by_primary_status']['primary_technical_failure']
        successful = delta['by_primary_status']['successful_primary']
        self.assertEqual(technical['delta']['fixed_critical_error_cells'],3)
        self.assertEqual(successful['delta']['fixed_critical_error_cells'],1)
        self.assertEqual(technical['conditional_observed_cost']['primary_inference_timing_unknown_attempts'],1)
        self.assertEqual(technical['conditional_observed_cost']['extra_secondary_known_inference_seconds'],2)
        self.assertEqual(delta['by_cohort']['public']['delta']['critical_exact_cells_net'],3)
        self.assertEqual(delta['by_cohort']['chinese']['delta']['critical_exact_cells_net'],1)
        self.assertEqual(delta['by_cohort_and_primary_status']['public/primary_technical_failure']['images'],1)
        self.assertEqual(sum(p['delta']['fixed_critical_error_cells'] for p in delta['by_primary_status'].values()),
                         delta['counts']['fixed_critical_error_cells'])
        self.assertEqual(receipt,before)

    def test_selection_receives_only_observed_native_arguments_and_report_scores_are_removed(self):
        class ForbiddenScore:
            def __deepcopy__(self, memo):
                raise AssertionError('offline score was accessed')
        sanitized = self.driver['artifact_fields']({'quality': ForbiddenScore(), 'routing': ForbiddenScore(),
                                                   'cohort_level': ForbiddenScore()})
        self.assertNotIn('quality', sanitized)
        original = self.runtime['select']
        calls = []
        def guard(candidates, **kwargs):
            calls.append(candidates)
            self.assertTrue(all(set(candidate) == {'id', 'engine', 'result'} for candidate in candidates))
            self.assertEqual(set(kwargs), {'image_info', 'text_evidence', 'allow_text_fixes'})
            self.assertIs(kwargs['allow_text_fixes'], False)
            return original(candidates, **kwargs)
        self.runtime['select'] = guard
        self.freeze(self.row('primary', '100'), self.row('secondary', '1000'))
        self.assertTrue(calls)

    def test_actual_artifact_loader_rejects_changed_record_hash_and_unmapped_recovery(self):
        # Reuse only the existing immutable evidence fixture; no inference runs.
        fixture_module = runpy.run_path(str(ROOT / 'tests/test_screen_quality_report.py'))
        fixture = fixture_module['ScreenQualityReportTests']()
        fixture.setUp()
        try:
            inputs = fixture.root / 'inputs.json'
            truth = fixture.root / 'truth.json'
            fixture.write(inputs, {'truth_available_to_inference': False, 'samples': [fixture.sample]})
            fixture.write(truth, {'samples': [self.truth('AB100')]})
            binding = {**fixture.binding, 'inputs_sha256': fixture.digest(inputs), 'engine': 'primary',
                       'preprocess_module_sha256': 'frozen-v2', 'first_pass': True,
                       'labels_or_ideal_regions_provided': False}
            fixture.write(fixture.runroot / 'binding.json', binding)
            fixture.write(fixture.runroot / 'sample/result.json', fixture.raw)
            record = {**fixture.record, 'engine': 'primary', 'image_sha256': fixture.sample['image_sha256'],
                      'result_sha256': fixture.digest(fixture.runroot / 'sample/result.json')}
            fixture.write(fixture.runroot / 'sample/record.json', record)
            data = self.qdriver['report'](inputs, truth, {'primary': fixture.runroot}, TOOLS,
                production_source=ROOT, required_preprocess_sha256='frozen-v2')
            path = fixture.root / 'report.json'
            fixture.write(path, data)
            args = (path, inputs, TOOLS, ROOT, 'frozen-v2')
            samples, models, _, _ = self.driver['load_candidates'](*args)
            self.assertEqual(set(samples), {'sample'})
            self.assertEqual(models['primary']['sample']['result'], fixture.raw)
            record['inference_seconds'] = 999
            fixture.write(fixture.runroot / 'sample/record.json', record)
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                self.driver['load_candidates'](*args)
            spec, kwargs, _, _ = fixture.recovery_fixture()
            recovery = fixture.driver['recovered_content'](spec, **kwargs)
            model = {'run': str(fixture.runroot), 'binding': kwargs['binding'],
                     'source_input_incident_recovery': recovery}
            with self.assertRaisesRegex(ValueError, 'explicit bound receipt map'):
                self.driver['checked_recovery'](model, {'sample': kwargs['original_records'][0]},
                                                {'sample': fixture.sample}, None)
            replacements = self.driver['checked_recovery'](model, {'sample': kwargs['original_records'][0]},
                                                           {'sample': fixture.sample}, spec)
            self.assertEqual(replacements['sample'][2]['status'], 'success')
            changed = deepcopy(replacements['sample'][2])
            changed['inference_seconds'] = 10000
            fixture.write(Path(spec['run']) / 'sample/record.json', changed)
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                self.driver['checked_recovery'](model, {'sample': kwargs['original_records'][0]},
                                                {'sample': fixture.sample}, spec)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
