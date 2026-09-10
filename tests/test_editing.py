import unittest
from ocr_workbench.editing import export_markdown, export_text, validate_edit


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

    def test_overlap_and_excel_limits_rejected(self):
        with self.assertRaises(ValueError):
            validate_edit(
                {
                    "text": "",
                    "tables": [{**self.table, "cells": self.table["cells"] * 2}],
                }
            )
        with self.assertRaises(ValueError):
            validate_edit(
                {
                    "text": "",
                    "tables": [
                        {
                            **self.table,
                            "cells": [{**self.table["cells"][0], "text": "a" * 32768}],
                        }
                    ],
                }
            )
