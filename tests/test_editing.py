import unittest
from copy import deepcopy
from ocr_workbench.editing import export_markdown, export_text, validate_edit
from ocr_workbench.tables import parse_tables


class EditingTests(unittest.TestCase):
    table = {
        "rows": 1,
        "columns": 1,
        "caption": "",
        "cells": [
            {
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "text": "00001234567890123456 & 校对",
            }
        ],
    }

    def test_html_replaced_without_stale_duplicate(self):
        result = export_markdown(
            {
                "text": "标题\n<table><tr><td>旧值</td></tr></table>\n备注",
                "tables": [self.table],
            }
        )
        self.assertNotIn("旧值", result)
        self.assertEqual(result.count("<table>"), 1)
        self.assertIn("00001234567890123456 &amp; 校对", result)
        self.assertIn("备注", result)

    def test_markdown_table_replaced(self):
        result = export_markdown(
            {
                "text": "标题\n| 名称 |\n| --- |\n| 旧值 |\n\n备注",
                "tables": [self.table],
            }
        )
        self.assertNotIn("旧值", result)
        self.assertEqual(result.count("<table>"), 1)
        self.assertIn("备注", result)

    def test_unrepresented_table_is_appended(self):
        result = export_markdown(
            {"text": "普通文字", "tables": [self.table, self.table]}
        )
        self.assertEqual(result.count("<table>"), 2)

    def test_plain_text_uses_edited_cells(self):
        result = export_text(
            {"text": "<table><tr><td>旧值</td></tr></table>", "tables": [self.table]}
        )
        self.assertEqual(result, "00001234567890123456 & 校对")

    def test_mixed_html_markdown_document_order(self):
        result = export_markdown(
            {
                "text": "| 名称 |\n| --- |\n| 旧一 |\n\n<table><tr><td>旧二</td></tr></table>",
                "tables": [self.table, self.table],
            }
        )
        self.assertNotIn("旧一", result)
        self.assertNotIn("旧二", result)
        self.assertEqual(result.count("<table>"), 2)

    def test_real_mixed_parse_and_export_preserves_order_and_newlines(self):
        md = "| MD_HEADER |\n| --- |\n| MD_VALUE |"
        html = "<table><tr><td>HTML_VALUE</td></tr></table>"
        for first, second in [(md, html), (html, md)]:
            for newline in ["\n", "\r\n"]:
                with self.subTest(first=first, newline=newline):
                    text = ("BEFORE\n" + first + "\nBETWEEN\n" + second + "\nAFTER").replace("\n", newline)
                    tables = parse_tables(text)
                    self.assertEqual(len(tables), 2)
                    self.assertLess(tables[0]["source"]["start"], tables[1]["source"]["start"])
                    for table in tables:
                        for cell in table["cells"]:
                            cell["text"] += "_EDIT"
                    edit = {"text": text, "tables": tables}
                    plain = export_text(edit)
                    expected = ["MD_HEADER_EDIT\nMD_VALUE_EDIT", "HTML_VALUE_EDIT"]
                    if first == html:
                        expected.reverse()
                    self.assertEqual(plain, "BEFORE" + newline + expected[0] + newline + "BETWEEN" + newline + expected[1] + newline + "AFTER")
                    rendered = export_markdown(edit)
                    self.assertEqual(rendered.count("<table>"), 2)
                    self.assertLess(rendered.index(expected[0].split("\n")[0]), rendered.index("BETWEEN"))
                    self.assertGreater(rendered.index(expected[1].split("\n")[0]), rendered.index("BETWEEN"))

    def test_mismatched_legacy_tables_do_not_delete_or_replace_source(self):
        text = "| MD_HEADER |\n| --- |\n| MD_VALUE |\nBETWEEN\n<table><tr><td>HTML_VALUE</td></tr></table>"
        edit = {"text": text, "tables": [self.table]}
        for render in [export_text, export_markdown]:
            self.assertTrue(render(edit).startswith(text))
            self.assertIn("校对", render(edit))

    def test_source_matching_survives_shift_and_preserves_unmatched_tables(self):
        text = "| MD |\n| --- |\n| VALUE |\nBETWEEN\n<table><tr><td>HTML</td></tr></table>"
        tables = parse_tables(text)
        html = deepcopy(tables[1])
        html["cells"][0]["text"] = "HTML_EDIT"
        rendered = export_text({"text": "NEW PREFIX\n" + text, "tables": [html]})
        self.assertEqual(rendered, "NEW PREFIX\n| MD |\n| --- |\n| VALUE |\nBETWEEN\nHTML_EDIT")

    def test_incomplete_manual_markup_retains_source_and_edited_cells(self):
        text = "<table><tr><td>UNFINISHED"
        self.assertEqual(export_text({"text": text, "tables": [self.table]}), text + "\n\n" + self.table["cells"][0]["text"])

    def test_partial_parse_replaces_usable_table_and_retains_failed_markup(self):
        text = '<table><tr><td>old</td></tr></table>\n<table><tr><td>unfinished'
        tables = parse_tables(text, warnings=[])
        tables[0]['cells'][0]['text'] = 'corrected'
        self.assertEqual(export_text({'text': text, 'tables': tables}),
                         'corrected\n<table><tr><td>unfinished')

    def test_overlap_rejected_and_long_cells_remain_editable(self):
        with self.assertRaises(ValueError):
            validate_edit(
                {
                    "text": "",
                    "tables": [{**self.table, "cells": self.table["cells"] * 2}],
                }
            )
        edit = {"text": "", "tables": [{**self.table,
            "cells": [{**self.table["cells"][0], "text": "a" * 32768}]}]}
        validate_edit(edit)
        self.assertEqual(export_text(edit).strip(), "a" * 32768)

    def test_manual_tables_unbound_deletion_and_literal_pasted_values(self):
        table = deepcopy(self.table)
        table['rows'], table['columns'] = 2, 2
        values = ['00001', '00123456789012345678', '=1+1', '+SUM(A1:A2)']
        table['cells'] = [{"row": i // 2, "column": i % 2, "row_span": 1,
                           "column_span": 1, "text": text} for i, text in enumerate(values)]
        edit = {'text': 'Literal |x|', 'tables': [table]}
        validate_edit(edit)
        self.assertEqual(export_text(edit), 'Literal |x|\n\n00001\t00123456789012345678\n=1+1\t+SUM(A1:A2)')
        edit['tables'] = []
        validate_edit(edit)
        self.assertEqual(export_text(edit), 'Literal |x|')

    def test_plain_pipe_text_remains_literal_in_exports(self):
        text = 'Absolute value:\n|x|\nEnd'
        edit = {'text': text, 'tables': parse_tables(text)}
        self.assertEqual(export_text(edit), text)
        self.assertEqual(export_markdown(edit), text)

    def test_malformed_manual_edits_raise_validation_errors(self):
        for edit in [None, [], {'text': '', 'tables': [None]},
                     {'text': '', 'tables': [{**self.table, 'cells': None}]},
                     {'text': '', 'tables': [{**self.table, 'cells': [None]}]}]:
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                validate_edit(edit)
