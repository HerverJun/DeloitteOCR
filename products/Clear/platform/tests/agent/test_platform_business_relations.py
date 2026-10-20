"""Raw inputs and scattered numbers cannot masquerade as difference reports."""
import unittest

from scripts.platform_business_oracle import audit_relationships, extract_entries


class BusinessRelationshipTests(unittest.TestCase):
    def checks(self, scenario, contents, suffix='.csv'):
        files = [{'entries': extract_entries('report'+suffix, content.encode('utf-8'))} for content in contents]
        return {row['name']: row['passed'] for row in audit_relationships(scenario, files)}

    def test_structured_difference_report_passes(self):
        report = ('item,invoice_amount,ledger_amount,difference,contract_limit\n'
                  'INV-1002,800.00,850.00,50.00,\nCT-1024,,,,2000.00\n'
                  'Total,2000.00,2050.00,50.00,\n')
        self.assertTrue(all(self.checks('audit-01', [report]).values()))

    def test_scattered_original_files_and_final_answer_numbers_do_not_pass(self):
        checks = self.checks('audit-01', ['invoice,invoice_amount\nINV-1002,800.00\n',
            'invoice,ledger_amount\nINV-1002,850.00\n', 'text\n"50.00 CT-1024 2000.00 2050.00"\n'])
        self.assertFalse(checks['invoice_amount_relationship'])
        self.assertFalse(checks['aggregate_amount_relationship'])

    def test_same_row_without_amount_roles_is_not_a_verified_difference(self):
        checks = self.checks('audit-01', ['id,a,b,c\nINV-1002,800.00,850.00,50.00\n'])
        self.assertFalse(checks['invoice_amount_relationship'])

    def test_swapped_invoice_ledger_and_wrong_difference_are_rejected(self):
        for invoice, ledger, delta in [('850.00','800.00','50.00'), ('800.00','850.00','25.00')]:
            checks = self.checks('audit-01', [f'id,invoice_amount,ledger_amount,difference\nINV-1002,{invoice},{ledger},{delta}\n'])
            self.assertFalse(checks['invoice_amount_relationship'])

    def test_missing_invoice_requires_state_and_its_own_amount(self):
        checks = self.checks('audit-02', ['id,state,ledger_amount\nINV-2999,missing,200.00\n'])
        self.assertTrue(checks['missing_invoice_relationship'])
        checks = self.checks('audit-02', ['id,state,ledger_amount\nINV-2999,found,200.00\nINV-2001,missing,200.00\n'])
        self.assertFalse(checks['missing_invoice_relationship'])

    def test_duplicate_requires_correct_count_on_the_same_item(self):
        for count, expected in [(2, True), (1, False), (3, False)]:
            checks = self.checks('audit-03', [f'id,state,count\nINV-3001,duplicate,{count}\n'])
            self.assertEqual(checks['duplicate_invoice_relationship'], expected)
        checks = self.checks('audit-03', ['id,state,discount\nINV-3001,duplicate,2\n'])
        self.assertFalse(checks['duplicate_invoice_relationship'])

    def test_json_items_are_not_merged(self):
        checks = self.checks('audit-01', ['[{"id":"INV-1002","invoice_amount":800},{"id":"INV-1002","ledger_amount":850,"difference":50}]'], '.json')
        self.assertFalse(checks['invoice_amount_relationship'])

    def test_markdown_and_html_tables_preserve_amount_roles(self):
        markdown = '| id | 发票金额 | 台账金额 | 差额 |\n|---|---|---|---|\n| INV-1002 | 800.00 | 850.00 | 50.00 |'
        html = '<table><tr><th>id</th><th>invoice_amount</th><th>ledger_amount</th><th>difference</th></tr><tr><td>INV-1002</td><td>800</td><td>850</td><td>50</td></tr></table>'
        self.assertTrue(self.checks('audit-01', [markdown], '.md')['invoice_amount_relationship'])
        self.assertTrue(self.checks('audit-01', [html], '.html')['invoice_amount_relationship'])

    def test_prose_requires_explicit_amount_relationship(self):
        good = 'INV-1002：发票金额800.00元，台账金额850.00元，差额50.00元。'
        bad = 'INV-1002 800.00 850.00 50.00'
        self.assertTrue(self.checks('audit-01', [good], '.txt')['invoice_amount_relationship'])
        self.assertFalse(self.checks('audit-01', [bad], '.txt')['invoice_amount_relationship'])


if __name__ == '__main__':
    unittest.main()
