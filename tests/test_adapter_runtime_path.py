import os
from pathlib import Path
import tempfile
import unittest

from ocr_workbench.adapter import runtime_path


class RuntimePathTests(unittest.TestCase):
    def test_only_bundled_cuda_directories_are_added(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / 'runtime' / 'python.exe'
            cudnn = root / 'runtime/Lib/site-packages/nvidia/cudnn/bin'
            cublas = root / 'runtime/Lib/site-packages/nvidia/cublas/bin'
            cudnn.mkdir(parents=True)
            cublas.mkdir(parents=True)
            (root / 'other/bin').mkdir(parents=True)
            actual = runtime_path(runtime, root / 'windows').split(os.pathsep)
            self.assertEqual(actual, [str(runtime.parent), str(cublas), str(cudnn),
                                      str(root / 'windows/System32')])


if __name__ == '__main__':
    unittest.main()
