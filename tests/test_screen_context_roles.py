"""Observed business context roles never invent text, geometry or owners."""

from copy import deepcopy
import unittest

from PIL import Image

from ocr_workbench.screen_evidence import (
    normalize_screen_context, organize_screen_business_metadata, reconcile_screen_evidence,
)
from ocr_workbench.screen_imaging import detect_business_regions


TITLE = '审计模拟 development procurement_delivery_v3 01'
TABLE = [155, 408, 1278, 614]
TITLE_BOX = [152, 314, 820, 346]
UNIT_BOX = [155, 361, 370, 382]


def context():
    return {'status': 'success', 'image': {'width': 1400, 'height': 900},
            'layout_blocks': [{'label': 'table', 'box': TABLE, 'confidence': .94}],
            'ocr_blocks': [{'text': TITLE, 'box': TITLE_BOX, 'confidence': .98},
                           {'text': '单位：万元', 'box': UNIT_BOX, 'confidence': .99}],
            'ocr_source': 'PP-OCRv6-auxiliary'}


def prepared():
    return {'width': 1400, 'height': 900, 'excluded_regions': [],
            'business_regions': [{'id': 'business', 'table_box': TABLE,
                                  'box': [145, 395, 1290, 630], 'ancillary': []}]}


class ScreenContextRoleTests(unittest.TestCase):
    def test_real_footnote_alias_survives_new_and_old_saved_evidence(self):
        raw = context()
        raw['layout_blocks'].append({'label': 'vision_footnote', 'box': [155, 635, 800, 655], 'score': .95})
        raw['ocr_blocks'].append({'text': '表注：编号为原始业务编号', 'box': [160, 637, 790, 653], 'confidence': .97})
        evidence = normalize_screen_context(raw)
        self.assertEqual(evidence['layout'][1]['kind'], 'note')
        old = deepcopy(evidence)
        old['layout'][1]['kind'] = 'text'
        result = organize_screen_business_metadata({'tables': [{'cells': []}]}, prepared(), old)
        note = next(part for part in result['items'] if part['text'].startswith('表注'))
        self.assertEqual(note['kind'], 'note')
        self.assertEqual(result['auxiliary_evidence'], old)

    def test_title_unit_chain_keeps_original_pixels_text_and_auditable_role(self):
        raw = context(); source = deepcopy(raw)
        evidence = normalize_screen_context(raw)
        tables = {'tables': [{'rows': 1, 'columns': 1,
                             'cells': [{'row': 0, 'column': 0, 'text': '00001'}]}]}
        untouched = deepcopy((tables, evidence))
        result = organize_screen_business_metadata(tables, prepared(), evidence)
        title = next(part for part in result['items'] if part['text'] == TITLE)
        self.assertEqual(title['kind'], 'title')
        self.assertEqual(title['box'], TITLE_BOX)
        self.assertEqual(title['context_table_box'], TABLE)
        self.assertEqual(title['table'], 0)
        self.assertEqual(title['classification_basis'], 'observed_title_unit_table_chain')
        self.assertTrue(title['semantic_role_review_required'])
        self.assertTrue(title['review_required'])
        self.assertFalse(title['human_confirmed'])
        self.assertEqual((tables, evidence), untouched)
        self.assertEqual(raw, source)
        self.assertEqual(result['auxiliary_evidence'], evidence)

    def test_crop_keeps_observed_title_outside_former_adjacency_margin(self):
        evidence = normalize_screen_context(context())
        image = Image.new('RGB', (1400, 900), 'white')
        regions, _, _ = detect_business_regions(image, evidence)
        owner = next(part for part in regions if 'layout_table' in part['sources'])
        self.assertLessEqual(owner['box'][1], TITLE_BOX[1])
        self.assertGreaterEqual(owner['box'][2], TITLE_BOX[2])
        self.assertTrue(any(part['text'] == TITLE for part in owner['ancillary']))

    def test_bare_heading_or_untrusted_unit_is_insufficient(self):
        variants = [[], [{'text': '单位：万元', 'box': UNIT_BOX, 'confidence': .7}],
                    [{'text': '万元', 'box': UNIT_BOX, 'confidence': .99}],
                    [{'text': '单位：万元', 'box': [155, 100, 370, 121], 'confidence': .99}]]
        for unit in variants:
            with self.subTest(unit=unit):
                raw = context(); raw['ocr_blocks'] = [raw['ocr_blocks'][0]] + unit
                evidence = normalize_screen_context(raw)
                self.assertEqual(evidence['text'][0]['kind'], 'text')
                self.assertNotIn('context_table_box', evidence['text'][0])

    def test_note_caption_and_observed_ui_are_never_inferred_as_title(self):
        for kind in ('vision_footnote', 'caption', 'unit', 'toolbar', 'ruler'):
            with self.subTest(kind=kind):
                raw = context()
                raw['layout_blocks'].append({'label': kind, 'box': TITLE_BOX, 'confidence': .98})
                evidence = normalize_screen_context(raw)
                self.assertNotEqual(evidence['text'][0]['kind'], 'title')
                self.assertNotIn('context_table_box', evidence['text'][0])

    def test_low_confidence_short_or_unaligned_heading_stays_unclassified(self):
        variants = [{'confidence': .8}, {'text': 'A1'}, {'box': [700, 314, 1300, 346]}]
        for update in variants:
            with self.subTest(update=update):
                raw = context(); raw['ocr_blocks'][0].update(update)
                evidence = normalize_screen_context(raw)
                self.assertEqual(evidence['text'][0]['kind'], 'text')
                self.assertNotIn('context_table_box', evidence['text'][0])

    def test_two_possible_headings_do_not_get_an_invented_role(self):
        raw = context()
        raw['ocr_blocks'].insert(1, {'text': '另一段可见业务文字', 'box': [155, 333, 820, 357], 'confidence': .98})
        evidence = normalize_screen_context(raw)
        self.assertTrue(all(part['kind'] == 'text' for part in evidence['text'][:2]))

    def test_multiple_table_owners_are_ambiguous_even_after_prior_reconciliation(self):
        evidence = normalize_screen_context(context())
        self.assertEqual(evidence['text'][0]['kind'], 'title')
        evidence['layout'].append({'kind': 'table', 'box': [160, 405, 1270, 620], 'confidence': .96})
        result = reconcile_screen_evidence(evidence, width=1400, height=900)
        self.assertEqual(result['text'][0]['kind'], 'text')
        self.assertNotIn('context_table_box', result['text'][0])
        self.assertEqual(evidence['text'][0]['kind'], 'title')

    def test_unit_or_heading_inside_another_table_cannot_anchor_context(self):
        for box in ([150, 352, 400, 390], [150, 300, 850, 350]):
            with self.subTest(box=box):
                raw = context()
                raw['layout_blocks'].append({'label': 'table', 'box': box, 'confidence': .98})
                evidence = normalize_screen_context(raw)
                self.assertEqual(evidence['text'][0]['kind'], 'text')

    def test_reprocessing_is_stable_and_does_not_duplicate_business_metadata(self):
        evidence = normalize_screen_context(context())
        once = reconcile_screen_evidence(evidence, width=1400, height=900)
        twice = reconcile_screen_evidence(once, width=1400, height=900)
        self.assertEqual(once, twice)
        raw = {'tables': [{'cells': []}]}
        first = organize_screen_business_metadata(raw, prepared(), once)
        second = organize_screen_business_metadata(raw, prepared(), twice)
        self.assertEqual(first['items'], second['items'])
        self.assertEqual(len(first['items']), 2)


if __name__ == '__main__':
    unittest.main()
