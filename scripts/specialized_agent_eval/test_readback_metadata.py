"""CPU mutation tests for independent five/eight-column workbook readback."""
import argparse
from copy import deepcopy
import unittest

from common import frozen_json
from readback import check_book, METADATA_CURRENT_FIELDS
from test_contracts import entry, workbook


def modern():
    value = entry()
    book = workbook(value)
    original = deepcopy(value['candidate'])
    metadata = original['screen']['business_metadata']
    for part in metadata:
        part.update(review_required=False, human_confirmed=False)
    primary = {'result_id': 'raw-result', 'task_id': 'raw-task', 'engine': 'fixture', 'revision': 0,
               'image_version': value['input_version_id'], 'input_sha256': value['image_sha256']}
    metadata[0].update(source='primary_transcription', review_required=True,
        review_reason='primary_auxiliary_transcription_disagreement', alternative_text='往来校验',
        alternative_source='fixture-auxiliary', evidence_refs=[{'kind': 'text', 'index': 0}], primary_source=primary,
        transcription_evidence={'primary_text': '往来核验', 'auxiliary_text': '往来校验',
                                'primary_source': primary, 'auxiliary_source': {'source': 'fixture-auxiliary'}})
    original['screen'].update(sources=[primary], business_metadata_evidence={
        'policy_version': 'screen-business-context-v4', 'items': deepcopy(metadata)})
    value['candidate'] = {'original': original, 'edited': {'tables': deepcopy(original['tables'])}}
    notes = book['业务说明']
    for column, text in enumerate(METADATA_CURRENT_FIELDS, 1):
        notes.cell(1, column).value = text
    for row, part in enumerate(metadata, 2):
        notes.cell(row, 3).value = part['source']
        notes.cell(row, 5).value = '需要人工核对' if part['review_required'] else '识别候选，未逐项人工确认'
        notes.cell(row, 6).value = part.get('alternative_text') or None
        notes.cell(row, 7).value = part.get('review_reason') or None
        notes.cell(row, 8).value = part.get('alternative_source') or None
    return value, book


def codes(value, book):
    return {error['code'] for error in check_book(book, value['file_name'], value)['errors']}


class MetadataReadbackTests(unittest.TestCase):
    def test_legacy_five_column_artifacts_still_pass(self):
        value = entry()
        result = check_book(workbook(value), value['file_name'], value)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['metadata_contract'], 'legacy_five_columns')

    def test_eight_columns_bind_candidate_evidence_and_unconfirmed_state(self):
        value, book = modern()
        result = check_book(book, value['file_name'], value)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['metadata_contract'], 'source_bound_eight_columns')

    def test_each_new_field_tamper_is_detected(self):
        for column in (6, 7, 8):
            with self.subTest(column=column):
                value, book = modern()
                book['业务说明'].cell(2, column).value = 'forged'
                self.assertIn('metadata_value', codes(value, book))

    def test_omitted_new_columns_cannot_downgrade_modern_contract(self):
        value, book = modern()
        book['业务说明'].delete_cols(6, 3)
        self.assertIn('metadata_shape', codes(value, book))

    def test_unconfirmed_workbook_cannot_claim_human_confirmation(self):
        value, book = modern()
        book['业务说明'].cell(2, 5).value = '人工已确认'
        self.assertIn('metadata_value', codes(value, book))

    def test_candidate_confirmation_without_current_human_identity_is_rejected(self):
        value, book = modern()
        value['candidate']['original']['screen']['business_metadata'][0]['human_confirmed'] = True
        value['candidate']['original']['screen']['business_metadata_evidence']['items'][0]['human_confirmed'] = True
        book['业务说明'].cell(2, 5).value = '人工已确认'
        self.assertIn('metadata_false_human_confirmation', codes(value, book))

    def test_original_candidate_alternative_must_match_saved_evidence(self):
        value, book = modern()
        value['candidate']['original']['screen']['business_metadata'][0]['alternative_text'] = 'forged'
        book['业务说明'].cell(2, 6).value = 'forged'
        self.assertIn('metadata_original_evidence_binding', codes(value, book))
        self.assertIn('metadata_alternative_source_binding', codes(value, book))

    def test_alternative_primary_source_must_be_an_actual_selected_source(self):
        value, book = modern()
        value['candidate']['original']['screen']['sources'] = []
        self.assertIn('metadata_alternative_source_binding', codes(value, book))

    def test_effective_edited_metadata_requires_a_bound_human_edit_log(self):
        value, book = modern()
        metadata = deepcopy(value['candidate']['original']['screen']['business_metadata'])
        metadata[0].update(text='人工修订', source='human_edit')
        value['candidate']['edited']['screen'] = {'business_metadata': metadata}
        book['业务说明'].cell(2, 2).value = '人工修订'
        book['业务说明'].cell(2, 3).value = 'human_edit'
        self.assertIn('metadata_unbound_human_edit', codes(value, book))

    def test_current_human_edit_and_acceptance_use_effective_metadata(self):
        value, book = modern()
        original = value['candidate']['original']['screen']['business_metadata']
        metadata = deepcopy(original)
        metadata[0].update(text='人工修订', source='human_edit', selected_text_basis='human_metadata_manual',
                           alternative_text=original[0]['text'], alternative_source=original[0]['source'])
        for part in metadata:
            part.update(human_confirmed=True, review_required=False, review_reason=None)
        value.update(result_revision=1, human_confirmed=True, review_state='accepted')
        value['candidate']['edited']['screen'] = {'business_metadata': metadata,
            'metadata_review': {'state': 'accepted', 'actor': 'human', 'input_version_id': 'v1', 'base_revision': 0},
            'metadata_edit_log': [{'item_index': 0, 'actor': 'human', 'after': '人工修订', 'image_version': 'v1',
                                   'choice': 'manual', 'original_selected_text': original[0]['text'],
                                   'base_revision': 0, 'original_evidence_refs': original[0]['evidence_refs'],
                                   'original_transcription_evidence': original[0]['transcription_evidence']}]}
        book['来源索引'].cell(2, 10).value = '1'
        book['业务说明'].cell(2, 2).value = '人工修订'
        book['业务说明'].cell(2, 3).value = 'human_edit'
        book['业务说明'].cell(2, 6).value = original[0]['text']
        book['业务说明'].cell(2, 8).value = original[0]['source']
        for row in range(2, 5):
            book['业务说明'].cell(row, 5).value = '人工已确认'
            book['业务说明'].cell(row, 7).value = None
        self.assertEqual(check_book(book, value['file_name'], value)['status'], 'passed')

    def test_stale_metadata_confirmation_requires_review_again(self):
        value, book = modern()
        metadata = deepcopy(value['candidate']['original']['screen']['business_metadata'])
        for part in metadata:
            part.update(human_confirmed=True, review_required=False, review_reason=None)
        value.update(result_revision=2, human_confirmed=False, review_state='question')
        value['candidate']['edited']['screen'] = {'business_metadata': metadata,
            'metadata_review': {'state': 'accepted', 'actor': 'human', 'input_version_id': 'v1', 'base_revision': 0}}
        book['来源索引'].cell(2, 10).value = '2'
        for row in range(2, 5):
            book['业务说明'].cell(row, 5).value = '需要人工核对'
            book['业务说明'].cell(row, 7).value = 'metadata_confirmation_revision_changed'
        self.assertEqual(check_book(book, value['file_name'], value)['status'], 'passed')
        book['业务说明'].cell(2, 5).value = '人工已确认'
        self.assertIn('metadata_value', codes(value, book))

    def test_new_metadata_formula_and_extra_row_are_detected(self):
        for mutation in ('formula', 'row'):
            value, book = modern()
            if mutation == 'formula':
                book['业务说明'].cell(2, 6).value = '=0.8'
                self.assertIn('formula_in_any_sheet', codes(value, book))
            else:
                book['业务说明'].append(['note', 'invented'])
                self.assertIn('metadata_shape', codes(value, book))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MetadataReadbackTests))
    frozen_json(args.receipt, {'format': 'metadata-readback-eight-column-cpu-regressions-v1',
        'status': 'passed' if result.wasSuccessful() else 'failed', 'tests_run': result.testsRun,
        'errors': len(result.errors), 'failures': len(result.failures),
        'scope': 'Independent in-memory workbook mutations only; no models, Agent, truth or exporter invoked',
        'failure_details': [{'test': str(case), 'traceback': trace} for case, trace in result.errors + result.failures]})
    raise SystemExit(0 if result.wasSuccessful() else 1)
