"""Atomic PDF request identities and ownership across native Store reopen."""
import json
from pathlib import Path
import tempfile
import unittest

from ocr_workbench.store import Store, Conflict, now
from ocr_workbench.documents import Documents
from ocr_workbench import page_requests


class PlatformPageRequestTests(unittest.TestCase):
    def setUp(self):
        root = Path('F:/OCR/output/platform-agent-dev-20261004/evidence/page-requests')
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(Path(self.temporary.name))
        project = self.store.project('Page identity fixture')['id']
        self.document = 'document_fixture'
        with self.store.transaction() as db:
            db.execute('INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                (self.document, project, 'fixture.pdf', 'pdf', 'fixture.pdf', 'a' * 64, 5, now(), now()))
            for number in range(1, 6):
                db.execute('INSERT INTO pages(id,document_id,page_number,created,updated) VALUES(?,?,?,?,?)',
                           ('page_' + str(number), self.document, number, now(), now()))
        self.documents = Documents(self.store, Path('F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr'))
        self.body = {'page_numbers': [1, 3], 'mode': 'native', 'engine': 'ppocr', 'force': False}

    def tearDown(self):
        self.temporary.cleanup()

    def test_duplicate_receipt_loss_parameter_conflict_and_unowned_cancel(self):
        first = page_requests.submit(self.documents, self.document, 'request_first', self.body)
        second = page_requests.submit(self.documents, self.document, 'request_first', self.body)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 2)
        with self.assertRaises(Conflict):
            page_requests.submit(self.documents, self.document, 'request_first', {**self.body, 'page_numbers': [2]})
        other = page_requests.submit(self.documents, self.document, 'request_other', self.body)
        self.assertTrue(all(not stage['owned'] for stage in other['stages']))
        unowned = page_requests.action(self.documents, self.document, 'request_other', 'cancel')
        self.assertEqual(unowned['affected_stage_ids'], [])
        self.assertTrue(all(stage['status'] == 'queued' for stage in unowned['stages']))
        cancelled = page_requests.action(self.documents, self.document, 'request_first', 'cancel')
        self.assertEqual(len(cancelled['affected_stage_ids']), 2)
        self.assertEqual([stage['page_number'] for stage in cancelled['stages']], [1, 3])

    def test_reopen_owned_resume_keeps_original_stage_identity(self):
        first = page_requests.submit(self.documents, self.document, 'request_resume', self.body)
        self.store.recover_document_stages()
        reopened = Store(self.store.root)
        documents = Documents(reopened, Path('F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr'))
        self.assertTrue(all(stage['status'] == 'paused' for stage in page_requests.read(reopened, self.document, 'request_resume')['stages']))
        resumed = page_requests.action(documents, self.document, 'request_resume', 'resume')
        self.assertEqual([stage['stage_id'] for stage in resumed['stages']], [stage['stage_id'] for stage in first['stages']])
        self.assertTrue(all(stage['status'] == 'queued' for stage in resumed['stages']))
        self.assertEqual(len(reopened.rows('SELECT * FROM document_stages')), 2)


if __name__ == '__main__':
    unittest.main()
