import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch
from openpyxl import load_workbook
from ocr_workbench.exporting import build_export


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.values = {}
        for key in ["a", "b"]:
            table = {
                "rows": 2,
                "columns": 2,
                "cells": [
                    {
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 2,
                        "text": "校对" + key,
                    },
                    {
                        "row": 1,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "text": "00123456789012345678",
                    },
                    {
                        "row": 1,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "text": "=1+1",
                    },
                ],
            }
            self.values[key] = {
                "id": key,
                "task_id": key,
                "revision": 7 if key == 'a' else 11,
                "original": {"text": "原始", "engine": "glm" if key == 'a' else 'ppocr',
                    "project_image_version": 'version-' + key,
                    "image": {"version": 'sha256-' + key}},
                "edited": {"text": "校对" + key, "tables": [table, table]},
            }
        self.store = SimpleNamespace(root=self.root, result=self.values.__getitem__,
            one=lambda table, key: {"image_id": key} if table == "tasks" else {"name": key + ".png"})

    def tearDown(self):
        self.temp.cleanup()

    def test_default_separate_workbooks_preserve_tables_strings_and_merges(self):
        path = build_export(self.store, ["a", "b", "a"], "xlsx")
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), ["0001.xlsx", "0002.xlsx", "sources.json"])
            sources = json.loads(archive.read('sources.json'))['tables']
            self.assertEqual(len(sources), 4)
            for name, key in zip(archive.namelist()[:2], ["a", "b"]):
                book = load_workbook(io.BytesIO(archive.read(name)))
                self.assertEqual(len(book.worksheets), 3)
                self.assertEqual(book.active["A1"].value, "校对" + key)
                self.assertEqual(
                    str(next(iter(book.active.merged_cells.ranges))), "A1:B1"
                )
                self.assertEqual(book.active["A2"].value, "00123456789012345678")
                self.assertEqual(book.active["B2"].data_type, "s")
                index = book['来源索引']
                self.assertEqual(index['A2'].value, name)
                self.assertEqual(index['B2'].value, 'Table 1')
                self.assertEqual(index['C2'].value, key + '.png')
                self.assertEqual(index['E2'].value, 'version-' + key)
                self.assertEqual(index['K2'].value, key)
                self.assertEqual(index['L2'].value, str(self.values[key]['revision']))
                item = next(row for row in sources if row['result_id'] == key)
                self.assertEqual(item['workbook'], name)
                self.assertEqual(item['image_version'], 'version-' + key)
                self.assertEqual(item['engine'], self.values[key]['original']['engine'])

    def test_aggregate_is_explicit_and_single_remains_workbook(self):
        merged = build_export(self.store, ["a", "b"], "xlsx", True)
        book = load_workbook(merged)
        self.assertEqual(len(book.worksheets), 5)
        self.assertEqual(book['来源索引']['B4'].value, 'Table 3')
        self.assertEqual(book['来源索引']['C4'].value, 'b.png')
        self.assertEqual(book['来源索引']['M4'].value, '1')
        single = build_export(self.store, ["a"], "xlsx")
        self.assertEqual(len(load_workbook(single).worksheets), 3)

    def test_text_json_and_markdown_use_saved_edits(self):
        for format in ["txt", "md", "json"]:
            with zipfile.ZipFile(
                build_export(self.store, ["a", "b"], format)
            ) as archive:
                for name, key in zip(archive.namelist(), ["a", "b"]):
                    content = archive.read(name).decode("utf-8")
                    self.assertIn("校对" + key, content)
                    if format == "json":
                        self.assertEqual(
                            json.loads(content)["original"], self.values[key]['original']
                        )
                    else:
                        self.assertNotIn("原始", content)

    def test_failed_batch_leaves_no_partial_archive(self):
        self.values["b"]["edited"]["tables"] = []
        with self.assertRaisesRegex(ValueError, "没有结构化表格"):
            build_export(self.store, ["a", "b"], "xlsx")
        self.assertEqual(list((self.root / "exports").iterdir()), [])
        self.values["a"]["edited"]["tables"] = []
        with self.assertRaisesRegex(ValueError, "2 张图片没有结构化表格：a.png、b.png"):
            build_export(self.store, ["a", "b"], "xlsx", True)

    def test_excel_limit_does_not_block_text_json_or_markdown_export(self):
        self.values['a']['edited']['tables'][0]['cells'][0]['text'] = 'x' * 32768
        for format in ['txt', 'md', 'json']:
            path = build_export(self.store, ['a'], format)
            self.assertIn('x' * 32768, path.read_text('utf-8'))
        with self.assertRaisesRegex(ValueError, '32,767'):
            build_export(self.store, ['a'], 'xlsx')

    def test_same_unsafe_names_keep_distinct_literal_provenance(self):
        self.store.one = lambda table, key: ({'image_id': key} if table == 'tasks'
                                             else {'name': '=unsafe/同名?.png'})
        with zipfile.ZipFile(build_export(self.store, ['a', 'b'], 'xlsx')) as archive:
            rows = json.loads(archive.read('sources.json'))['tables']
            self.assertEqual({row['image_id'] for row in rows}, {'a', 'b'})
            self.assertEqual({row['workbook'] for row in rows}, {'0001.xlsx', '0002.xlsx'})
            for filename in ['0001.xlsx', '0002.xlsx']:
                index = load_workbook(io.BytesIO(archive.read(filename)))['来源索引']
                self.assertEqual(index['C2'].value, '=unsafe/同名?.png')
                self.assertEqual(index['C2'].data_type, 's')

    def test_missing_table_blocks_every_excel_mode_and_names_image(self):
        self.values["b"]["edited"] = {"text": "NO_TABLE_IMAGE_CONTENT", "tables": []}
        for keys, aggregate in [(["a", "b"], True), (["a", "b"], False), (["b"], False)]:
            with self.subTest(keys=keys, aggregate=aggregate):
                with self.assertRaisesRegex(ValueError, "1 张图片没有结构化表格：b.png"):
                    build_export(self.store, keys, "xlsx", aggregate)
                self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_tables_removed_after_preflight_still_abort_and_clean_up(self):
        for aggregate in [False, True]:
            calls = {}
            def changing_result(key):
                calls[key] = calls.get(key, 0) + 1
                result = self.values[key]
                if key == "b" and calls[key] > 1:
                    return {**result, "edited": {"text": "CHANGED", "tables": []}}
                return result
            self.store.result = changing_result
            with self.subTest(aggregate=aggregate), self.assertRaisesRegex(ValueError, "b.png"):
                build_export(self.store, ["a", "b"], "xlsx", aggregate)
            self.assertEqual(list((self.root / "exports").iterdir()), [])


class ExportSnapshotTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        from ocr_workbench.imaging import add_image
        from ocr_workbench.store import Store

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store(root / 'data')
        self.project = self.store.project('确认导出')
        source = root / 'source.png'
        self.photos, self.keys = [], []
        for name in ['a.png', 'b.png']:
            Image.new('RGB', (3, 3), 'white').save(source)
            photo = add_image(self.store, self.project['id'], name, source)
            task = self.store.enqueue(self.project['id'], [photo['active_version']], ['glm'])[0]
            self.store.claim()
            self.store.complete(task, {'text': name, 'tables': [{
                'rows': 1, 'columns': 1, 'cells': [{'row': 0, 'column': 0,
                'row_span': 1, 'column_span': 1, 'text': '0001'}]}], 'engine': 'glm'})
            key = self.store.one('tasks', task)['result_id']
            self.photos.append(photo)
            self.keys.append(key)
            self.store.set_review(photo['id'], 'confirmed', key, 0, photo['active_version'])

    def test_confirmed_export_captures_one_revision_snapshot_across_files(self):
        from ocr_workbench.store import Store

        other = Store(self.store.root)
        write_text = Path.write_text
        changed = False

        def change_after_first_snapshot(path, content, *args, **kwargs):
            nonlocal changed
            written = write_text(path, content, *args, **kwargs)
            if path.name == '_snapshot-0.json' and not changed:
                changed = True
                record = other.result(self.keys[1])
                other.save(self.keys[1], {**record['edited'], 'text': 'later edit'}, 0)
            return written

        with patch.object(Path, 'write_text', change_after_first_snapshot):
            target = build_export(self.store, self.keys, 'json', confirmed_only=True)
        self.assertTrue(changed)
        self.assertEqual(other.result(self.keys[1])['revision'], 1)
        with zipfile.ZipFile(target) as archive:
            records = [json.loads(archive.read(name)) for name in archive.namelist()]
        self.assertEqual([record['revision'] for record in records], [0, 0])
        self.assertEqual(records[1]['edited']['text'], 'b.png')
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_unconfirmed_stale_or_changed_version_is_rejected(self):
        from ocr_workbench.store import Conflict

        key, photo = self.keys[0], self.photos[0]
        self.store.set_review(photo['id'], 'question', key, 0, photo['active_version'])
        with self.assertRaises(Conflict):
            build_export(self.store, [key], 'json', confirmed_only=True)
        self.store.set_review(photo['id'], 'confirmed', key, 0, photo['active_version'])
        record = self.store.result(key)
        self.store.save(key, {**record['edited'], 'text': 'changed'}, 0)
        with self.assertRaises(Conflict):
            build_export(self.store, [key], 'txt', confirmed_only=True)
        self.store.set_review(photo['id'], 'confirmed', key, 1, photo['active_version'])
        with self.store.transaction() as db:
            db.execute('UPDATE images SET active_version=? WHERE id=?', ('other-version', photo['id']))
        with self.assertRaises(Conflict):
            build_export(self.store, [key], 'xlsx', confirmed_only=True)
        self.assertEqual(list((self.store.root / 'exports').iterdir()), [])

    def test_excel_generation_uses_captured_values_and_provenance(self):
        from ocr_workbench.tables import export_xlsx

        key, photo = self.keys[0], self.photos[0]
        def edit_during_workbook(tables, path, **kwargs):
            record = self.store.result(key)
            record['edited']['tables'][0]['cells'][0]['text'] = 'later cell'
            self.store.save(key, record['edited'], 0)
            with self.store.transaction() as db:
                db.execute('UPDATE images SET name=? WHERE id=?', ('renamed.png', photo['id']))
            return export_xlsx(tables, path, **kwargs)

        with patch('ocr_workbench.exporting.export_xlsx', edit_during_workbook):
            target = build_export(self.store, [key], 'xlsx', confirmed_only=True)
        book = load_workbook(target)
        self.assertEqual(book['Table 1']['A1'].value, '0001')
        self.assertEqual(book['来源索引']['C2'].value, 'a.png')
        self.assertEqual(book['来源索引']['L2'].value, '0')


if __name__ == "__main__":
    unittest.main()
