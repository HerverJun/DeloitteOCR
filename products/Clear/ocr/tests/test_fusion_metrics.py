import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from fusion_metrics import aligned, text_gains
from benchmark_metrics import distance


class FusionMetricsTests(unittest.TestCase):
    def test_corrected_and_introduced_errors_do_not_cancel_out_of_report(self):
        self.assertEqual(text_gains('ABC','XBC','ABX'),{'corrected':1,'introduced':1,'net_corrected':0})
        self.assertEqual(text_gains('ABC','AXBC','ABXC')['net_corrected'],0)

    def test_counts_are_the_same_minimum_edit_distance(self):
        for reference,hypothesis in [('甲乙丙','甲新乙'),('00123','123'),('','extra'),('extra',''),('AAAA','AABA')]:
            chars,insertions,positions,counts=aligned(reference,hypothesis)
            self.assertEqual(sum(counts.values()),distance(reference,hypothesis))
            self.assertEqual(len(chars),len(reference))
            self.assertEqual(len(positions),len(hypothesis))
