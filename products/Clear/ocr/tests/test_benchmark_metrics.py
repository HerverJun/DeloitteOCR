from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark_metrics import distance, text_metrics, table_metrics


class MetricTests(unittest.TestCase):
    def test_cer_insert_delete_substitute_and_unicode(self):
        self.assertEqual(distance("kitten", "sitting"), 3)
        self.assertEqual(distance("abc", ""), 3)
        self.assertEqual(distance("", "abc"), 3)
        values = text_metrics("中文\nA 00123", "中文 A 00123")
        self.assertEqual(values["edit_distance"], 0)
        self.assertGreater(values["strict_edit_distance"], 0)
        self.assertEqual(text_metrics("é", "e\u0301")["edit_distance"], 0)
        self.assertGreater(text_metrics("A，", "a,")["edit_distance"], 0)

    def test_numeric_fields_preserve_leading_zero_and_token_boundaries(self):
        v = text_metrics("编号001234 编号001234", "编号1234 编号0012345")
        self.assertEqual(v["numeric_fields"], 2)
        self.assertEqual(v["numeric_fields_correct"], 0)
        v = text_metrics("001234 001234", "001234")
        self.assertEqual(v["numeric_fields"], 2)
        self.assertEqual(v["numeric_fields_correct"], 1)

    def test_structure_includes_merges_and_empty_cells(self):
        a = [{"rows":1,"columns":2,"cells":[{"row":0,"column":0,"row_span":1,"column_span":2,"text":"001"}]}]
        b = [{"rows":1,"columns":2,"cells":[{"row":0,"column":0,"row_span":1,"column_span":1,"text":"001"},
             {"row":0,"column":1,"row_span":1,"column_span":1,"text":""}]}]
        v = table_metrics(a,b)
        self.assertEqual(v["span_f1"], 0)
        self.assertFalse(v["structure_exact"])
        self.assertEqual(v["numeric_fields_correct"], 1)
        self.assertTrue(table_metrics(a,a)["structure_exact"])
        self.assertEqual(table_metrics(a,[])["span_f1"], 0)


if __name__ == "__main__":
    unittest.main()
