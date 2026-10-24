"""Admission guards for native screen-task records and product downgrade."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from scripts.product_compatibility import DATA_COMPATIBILITY,check


class ScreenCompatibilityTests(unittest.TestCase):
    def test_schema18_and_reader_feature_reject_old_candidate_without_writing(self):
        root=Path('F:/OCR/output/branch-integration-20261024-001/platform/screen-compatibility')
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            path=Path(folder);native=path/'native/ocr';native.mkdir(parents=True)
            database=native/'workbench.sqlite3'
            with closing(sqlite3.connect(database)) as db,db:db.execute('PRAGMA user_version=18')
            before=database.read_bytes()
            self.assertTrue(check(path,DATA_COMPATIBILITY)['compatible'])
            old=deepcopy(DATA_COMPATIBILITY);old['sqlite']['native/ocr/workbench.sqlite3']['user_versions']=list(range(18))
            with self.assertRaises(ValueError):check(path,old)
            self.assertEqual(database.read_bytes(),before)
            marker=path/'product-reader-requirements.json'
            marker.write_text(json.dumps({'format':'clear-product-reader-requirements-v1','reader_features':['screen-batch-v1']}),encoding='utf-8')
            old=deepcopy(DATA_COMPATIBILITY);old['reader_features'].remove('screen-batch-v1')
            with self.assertRaises(ValueError):check(path,old)
            self.assertEqual(database.read_bytes(),before)
            with closing(sqlite3.connect(database)) as db,db:db.execute('PRAGMA user_version=19')
            before=database.read_bytes()
            self.assertTrue(check(path,DATA_COMPATIBILITY)['compatible'])
            self.assertEqual(database.read_bytes(),before)
            future=max(DATA_COMPATIBILITY['sqlite']['native/ocr/workbench.sqlite3']['user_versions'])+1
            with closing(sqlite3.connect(database)) as db,db:db.execute(f'PRAGMA user_version={future}')
            before=database.read_bytes()
            with self.assertRaises(ValueError):check(path,DATA_COMPATIBILITY)
            self.assertEqual(database.read_bytes(),before)
