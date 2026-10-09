"""Full-scope export qualification must reject subsets and duplicate categories."""
import unittest
from s07_agent_native_followup import all_comparison_categories


class ExportScopeTests(unittest.TestCase):
    def test_full_scope_omission_and_explicit_list(self):
        self.assertTrue(all_comparison_categories(None))
        self.assertTrue(all_comparison_categories(['same', 'different', 'only_left', 'only_right', 'ambiguous']))
        self.assertTrue(all_comparison_categories(['ambiguous', 'only_right', 'different', 'same', 'only_left']))

    def test_subset_empty_duplicate_and_wrong_type_are_rejected(self):
        for value in ([], ['same'], ['same', 'different', 'only_left', 'only_right', 'same'],
                      ['same', 'different', 'only_left', 'only_right', 'ambiguous', 'same'], 'all', {}):
            with self.subTest(value=value):
                self.assertFalse(all_comparison_categories(value))


if __name__ == '__main__':
    unittest.main()
