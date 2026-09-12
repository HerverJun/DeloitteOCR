"""Exercise post-recognition publication with synthetic outputs, without models."""

import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from ocr_workbench.editing import validate_edit
from ocr_workbench.worker import process_image


class WorkerResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'config').mkdir()
        (root / 'config/engines.json').write_text(
            json.dumps({'glm': {'name': 'Synthetic engine', 'models': []}}), 'utf-8')
        image = root / '原图.png'
        Image.new('RGB', (3, 3), 'white').save(image)
        output = root / 'output'
        output.mkdir()
        self.args = SimpleNamespace(bundle=root, image=image, output=output, engine='glm')

    def recognize(self, text):
        session = SimpleNamespace(recognize=lambda *args: (
            {'complete_output': text}, [{'text': text, 'confidence': None,
                                        'polygon': None, 'kind': 'text'}], 0.0))
        with patch('ocr_workbench.runtime_audit.inspect_runtime', return_value={'synthetic': True}):
            process_image(self.args, session, time.perf_counter())
        result = json.loads((self.args.output / 'result.json').read_text('utf-8'))
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['text'], text)
        self.assertEqual((self.args.output / 'result.txt').read_text('utf-8'), text)
        self.assertEqual((self.args.output / 'result.md').read_text('utf-8'), text)
        self.assertEqual(json.loads((self.args.output / 'raw.json').read_text('utf-8')),
                         {'complete_output': text})
        validate_edit({'text': result['text'], 'tables': result['tables']})
        return result

    def test_truncated_table_publishes_text_raw_and_warning(self):
        result = self.recognize('Recognized invoice 123\n<table><tr><td>001')
        self.assertEqual(result['tables'], [])
        self.assertEqual(result['warnings'][0]['code'], 'table_parse_failed')
        self.assertEqual(result['artifacts']['xlsx']['status'], 'unavailable')

    def test_excel_limit_keeps_full_editable_table_and_success_result(self):
        text = '<table><tr><td>' + '0' * 32768 + '</td></tr></table>'
        result = self.recognize(text)
        self.assertEqual(result['tables'][0]['cells'][0]['text'], '0' * 32768)
        self.assertEqual(result['warnings'][0]['code'], 'xlsx_export_failed')
        self.assertEqual(result['artifacts']['xlsx']['status'], 'failed')
        self.assertFalse((self.args.output / 'result.xlsx').exists())
        self.assertFalse((self.args.output / 'result.xlsx.tmp').exists())

    def test_optional_excel_write_failure_does_not_fail_recognition(self):
        with patch('ocr_workbench.worker.export_xlsx', side_effect=OSError('file is locked')):
            result = self.recognize('<table><tr><td>001</td></tr></table>')
        self.assertEqual(result['warnings'][0]['detail'], 'file is locked')
        self.assertEqual(result['tables'][0]['cells'][0]['text'], '001')

    def test_valid_table_publishes_workbook_without_warnings(self):
        result = self.recognize('| 编号 |\n| --- |\n| 0001 |')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['artifacts']['xlsx']['status'], 'ready')
        self.assertTrue((self.args.output / 'result.xlsx').is_file())


if __name__ == '__main__':
    unittest.main()
