import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ocr_workbench.windows_paths import ascii_model_directory


@unittest.skipUnless(os.name=='nt','Windows junction API')
class PathTests(unittest.TestCase):
    def test_isolated_runtime_without_programdata_can_alias_unicode_models(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}):
            os.environ.pop('ProgramData', None)
            os.environ.pop('SystemRoot', None)
            models=Path(temp)/'模型';models.mkdir()
            (models/'model.bin').write_bytes(b'model')
            with ascii_model_directory(models) as alias:
                self.assertEqual((alias/'model.bin').read_bytes(),b'model')
            self.assertFalse(alias.exists())

    def test_unicode_model_alias_reads_real_bytes_and_cleans_only_link(self):
        with tempfile.TemporaryDirectory() as temp:
            models=Path(temp)/'中文 路径'/'models';models.mkdir(parents=True)
            weights=models/'inference.pdiparams';weights.write_bytes(b'original model bytes')
            with ascii_model_directory(models) as alias:
                self.assertTrue(str(alias).isascii())
                self.assertEqual((alias/weights.name).read_bytes(),b'original model bytes')
                self.assertEqual(alias.resolve(),models.resolve())
            self.assertFalse(alias.exists())
            self.assertEqual(weights.read_bytes(),b'original model bytes')

    def test_exception_still_removes_alias_and_preserves_model(self):
        with tempfile.TemporaryDirectory() as temp:
            models=Path(temp)/'模型';models.mkdir()
            with self.assertRaisesRegex(RuntimeError,'test failure'):
                with ascii_model_directory(models) as alias:
                    raise RuntimeError('test failure')
            self.assertFalse(alias.exists())
            self.assertTrue(models.exists())
