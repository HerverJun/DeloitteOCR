"""Display decoding must not become financial-value correction or approval."""
from copy import deepcopy
import unittest

from ocr_workbench.screen_text_rendering import render_cell_text, render_screen_table_text


class MathTextRenderingTests(unittest.TestCase):
    def test_explicit_annotations_symbols_and_text(self):
        cases = [(r'0.90$ ^{*} $', '0.90*'), (r'-0.186$ ^{\ast} $', '-0.186*'),
                 (r'$ \beta^{c} $', 'βc'), (r'$ \Delta PaO_{2}/FIO_{2} $', 'Δ PaO2/FIO2'),
                 (r'$ 2,920 \pm 1,172.1 $', '2,920 ± 1,172.1'),
                 (r'$\text{−00001007}$', '−00001007'), (r'$ <0.001 $', '<0.001')]
        for before, after in cases:
            with self.subTest(before=before):
                actual = render_cell_text(before)
                self.assertTrue(actual['changed'])
                self.assertEqual(actual['after'], after)
                self.assertEqual(''.join(r['text'] for r in actual['runs']), after)

    def test_currency_formulas_ids_signs_and_unknown_syntax_abstain(self):
        cases = ['$100.00', '$100.00$', 'Cost $5 and tax $2', r'\$100.00',
                 '00001007', '12345678901234567890', '=1+1', '−20.00', '-20.00',
                 '(20.00)', '二十元', r'$10^{9}$', r'$m.s^{-1}$', r'$\frac{1}{2}$',
                 r'$\beta$ and $\unknown{1}$', r'`$\beta$`', r'$$\beta$$',
                 r'$\beta^{a$', r'$\text{}$', r'$\text{a%hidden}$']
        for before in cases:
            with self.subTest(before=before):
                actual = render_cell_text(before)
                self.assertFalse(actual['changed'])
                self.assertEqual(actual['after'], before)

    def test_superscript_style_and_whole_cell_abstention(self):
        actual = render_cell_text(r'0.90$^{\ast}$')
        self.assertTrue(any(run['style'] == 'superscript' for run in actual['runs']))
        mixed = r'0.90$^{\ast}$ and $10^{-3}$'
        self.assertEqual(render_cell_text(mixed)['after'], mixed)

    def test_bounded_nesting_and_fragment_work(self):
        for value in ['$' + r'\text{' * 10 + '1' + '}' * 10 + '$', r'$\beta$' * 65]:
            self.assertFalse(render_cell_text(value)['changed'])

    def test_original_input_numeric_bytes_and_review_state_survive(self):
        source = {'text': r'<table><tr><td>-1.00$^{\ast}$</td></tr></table>',
                  'project_image_version': 'version1', 'screen': {'input_sha256': 'image-sha',
                  'selection': {'automatic_delivery': False, 'review_required': True}},
                  'tables': [{'rows': 1, 'columns': 1, 'cells': [{'row': 0, 'column': 0,
                              'row_span': 1, 'column_span': 1, 'text': r'-1.00$^{\ast}$'}]}]}
        original = deepcopy(source)
        rendered = render_screen_table_text(source)
        self.assertEqual(source, original)
        self.assertEqual(rendered['text'], original['text'])
        self.assertEqual(rendered['screen']['selection'], original['screen']['selection'])
        cell = rendered['tables'][0]['cells'][0]
        self.assertEqual(cell['text'], '-1.00*')
        self.assertEqual(cell['provenance'][0]['before'], original['tables'][0]['cells'][0]['text'])
        self.assertFalse(cell['provenance'][0]['automatic_delivery_authorized'])
        again = render_screen_table_text(rendered)
        self.assertEqual(again['tables'], rendered['tables'])


if __name__ == '__main__':
    unittest.main()
