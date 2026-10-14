import csv
from email.parser import BytesParser
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


class WheelPatchTests(unittest.TestCase):
    def test_version_and_dependency_repaired_for_lf_and_crlf(self):
        script=Path(__file__).resolve().parents[1]/'scripts/patch_paddle_wheel.py'
        for newline in ['\n','\r\n']:
            with self.subTest(newline=repr(newline)),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                wheel=root/'paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl'
                header=newline.join(['Metadata-Version: 2.1','Name: paddlepaddle-gpu',
                    'Version: 3.3.1','Requires-Dist: nvidia-cudnn-cu12==9.5.1.17','',''])
                with zipfile.ZipFile(wheel,'w') as archive:
                    archive.writestr('paddlepaddle_gpu-3.3.1.dist-info/METADATA',header)
                    archive.writestr('paddlepaddle_gpu-3.3.1.dist-info/RECORD','')
                    archive.writestr('paddle/payload.bin',b'unchanged\x00\xff')
                subprocess.run([sys.executable,str(script),'--wheel',str(wheel),
                    '--output-dir',str(root/'patched')],check=True,capture_output=True)
                with zipfile.ZipFile(next((root/'patched').glob('*.whl'))) as archive:
                    prefix='paddlepaddle_gpu-3.3.1+ocr.1.dist-info/'
                    metadata=BytesParser().parsebytes(archive.read(prefix+'METADATA'))
                    self.assertEqual(metadata['Version'],'3.3.1+ocr.1')
                    self.assertEqual(metadata['Requires-Dist'],'nvidia-cudnn-cu12==9.9.0.52')
                    self.assertEqual(archive.read('paddle/payload.bin'),b'unchanged\x00\xff')
                    records=list(csv.reader(io.StringIO(archive.read(prefix+'RECORD').decode())))
                    self.assertEqual({row[0] for row in records},set(archive.namelist()))
