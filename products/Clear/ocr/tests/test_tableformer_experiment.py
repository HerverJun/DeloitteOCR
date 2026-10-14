import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compare_tableformer_geometry as experiment
from geometry_eval_common import ensure_lock, write_json, sha


class ExpandedExperimentTests(unittest.TestCase):
    def test_three_providers_receive_identical_ocr_and_reject_foreign_blocks(self):
        blocks = [{'text': '123', 'polygon': [[0, 0], [1, 0], [1, 1], [0, 1]]},
                  {'text': '', 'polygon': []}]
        sample = {'sha256': 'image'}
        for provider in experiment.METHODS.values():
            prediction = {'image_version': 'image', 'ocr_source': 'independent-ppocr:image',
                          'ocr_blocks': blocks[:1] if provider == 'geometry' else blocks,
                          'source_semantics': 'raw_structure'}
            self.assertEqual(experiment.bind_prediction(prediction, blocks, sample, provider)['ocr_blocks'], blocks)
            prediction['ocr_blocks'] = [{'text': 'foreign'}]
            with self.assertRaises(experiment.IncompleteInput):
                experiment.bind_prediction(prediction, blocks, sample, provider)

    def test_text_extent_and_foreign_image_cannot_enter_raw_comparison(self):
        prediction = {'image_version': 'a', 'ocr_source': 'independent-ppocr:a',
                      'ocr_blocks': [], 'source_semantics': 'matched_text_extent'}
        with self.assertRaises(experiment.IncompleteInput):
            experiment.bind_prediction(prediction, [], {'sha256': 'a'}, 'tableformer')
        with self.assertRaises(experiment.IncompleteInput):
            experiment.bind_prediction(prediction, [], {'sha256': 'b'}, 'rapidtable')

    def test_test_requires_seal_before_model_loading(self):
        with patch.object(experiment, 'verify_inputs', return_value={'split': 'test'}):
            with self.assertRaisesRegex(ValueError, 'Seal code'):
                experiment.verify_expanded_seal(Path('test.json'), None)

    def test_changed_extended_seal_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'inputs/test.json'
            write_json(manifest, {})
            write_json(root / 'split-lock.json', {})
            from ocr_workbench.table_matching import default_policy
            good = {'experiment_code': {'runner': 'abc'}, 'tableformer': {'weight': 'abc'},
                    'policy': default_policy(), 'test_manifest_sha256': sha(manifest),
                    'test_split_lock_sha256': sha(root / 'split-lock.json')}
            seal = root / 'seal.json'
            with patch.object(experiment, 'verify_inputs', return_value={'split': 'test'}), \
                 patch.object(experiment, 'verify_test_seal', return_value='base-ok'), \
                 patch.object(experiment, 'experiment_code', return_value=good['experiment_code']), \
                 patch.object(experiment, 'model_lock', return_value=good['tableformer']):
                write_json(seal, good)
                self.assertEqual(experiment.verify_expanded_seal(manifest, seal), 'base-ok')
                for key in good:
                    write_json(seal, {**good, key: 'changed'})
                    with self.assertRaisesRegex(experiment.IncompleteInput, key):
                        experiment.verify_expanded_seal(manifest, seal)

    def test_model_failure_preserved_but_missing_or_tampered_input_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_hash = ensure_lock(root / 'run-lock.json', {'version': 1})
            sample = {'id': 'one'}
            marker = {'status': 'failed', 'error': 'model_failed', 'run_lock_sha256': lock_hash,
                      'artifact_sha256': {}}
            with self.assertRaises(experiment.IncompleteInput):
                experiment.checked_artifact(root, sample, 'prediction.json')
            write_json(root / 'one/evaluation.json', marker)
            with self.assertRaises(experiment.ProviderFailure):
                experiment.checked_artifact(root, sample, 'prediction.json')
            write_json(root / 'one/evaluation.json', {**marker, 'incomplete_input': True})
            with self.assertRaises(experiment.IncompleteInput):
                experiment.checked_artifact(root, sample, 'prediction.json')
            write_json(root / 'one/prediction.json', {})
            marker.update(status='success', artifact_sha256={'prediction.json': sha(root / 'one/prediction.json')})
            write_json(root / 'one/evaluation.json', marker)
            experiment.checked_artifact(root, sample, 'prediction.json')
            write_json(root / 'one/prediction.json', {'tampered': True})
            with self.assertRaises(experiment.IncompleteInput):
                experiment.checked_artifact(root, sample, 'prediction.json')

    def test_verified_inference_failure_keeps_all_targets_in_denominator(self):
        from report_geometry_v2 import score
        sample = {'targets': [{'row': 0, 'column': 0, 'box': [0, 0, 10, 10]},
                              {'row': 0, 'column': 1, 'box': [10, 0, 20, 10]}]}
        rows, extra = score(sample, {'status': 'failed', 'mappings': []})
        self.assertEqual(sum(r['counts']['targets'] for r in rows), 2)
        self.assertEqual(sum(r['counts']['no_output'] for r in rows), 2)
        self.assertEqual(extra, 0)


if __name__ == '__main__':
    unittest.main()
