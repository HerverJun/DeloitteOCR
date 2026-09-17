from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_tableformer_grid_labels import grid_boxes


class OfficialGridConventionTests(unittest.TestCase):
    def test_midpoint_gaps_blank_cell_and_spanning_cell(self):
        canonical = {
            'rows': [{'pdf_row_bbox': [0, 0, 100, 10]}, {'pdf_row_bbox': [0, 30, 100, 40]}],
            'columns': [{'pdf_column_bbox': [0, 0, 20, 40]}, {'pdf_column_bbox': [60, 0, 100, 40]}],
            'cells': [{'row_nums': [0], 'column_nums': [0], 'pdf_text_content': ''},
                      {'row_nums': [0], 'column_nums': [1]},
                      {'row_nums': [1], 'column_nums': [0, 1]}]}
        before = repr(canonical)
        boxes = grid_boxes(canonical)
        self.assertEqual(boxes[(0, 0, 1, 1)], [0, 0, 40, 20])
        self.assertEqual(boxes[(0, 1, 1, 1)], [40, 0, 100, 20])
        self.assertEqual(boxes[(1, 0, 1, 2)], [0, 20, 100, 40])
        self.assertEqual(repr(canonical), before)

    def test_reject_unordered_or_rotated_row_semantics(self):
        canonical = {'rows': [{'pdf_row_bbox': [0, 20, 100, 30]}, {'pdf_row_bbox': [0, 0, 100, 10]}],
                     'columns': [], 'cells': []}
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            grid_boxes(canonical)


if __name__ == '__main__':
    unittest.main()
