"""Small same-volume fixture for the optional low-space candidate staging mode."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare_agent_bundle.py'
PACKAGE = SCRIPT.with_name('package_locked_bundle.py')
spec = importlib.util.spec_from_file_location('prepare_agent_bundle_under_test', SCRIPT)
bundle_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle_builder)


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def record(root, relative):
    path = root / relative
    return {'path': relative, 'bytes': path.stat().st_size, 'sha256': bundle_builder.checksum(path)}


class LowSpaceBundleTests(unittest.TestCase):
    def test_stale_frontend_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            built = write(root, 'frontend/dist/index.html', b'built')
            source = write(root, 'frontend/src/AgentArtifact.tsx', b'old')
            os.utime(built, ns=(1_000_000_000, 1_000_000_000))
            os.utime(source, ns=(2_000_000_000, 2_000_000_000))
            with self.assertRaisesRegex(ValueError, 'Frontend dist predates build input'):
                bundle_builder.require_fresh_frontend(root)
            output = root / 'candidate'
            args = ['prepare_agent_bundle.py', '--base-bundle', str(root / 'unused-base'),
                    '--service-runtime', str(root / 'unused-service'), '--output', str(output),
                    '--receipt', str(root / 'receipt.json')]
            with patch.object(bundle_builder, 'ROOT', root), patch.object(sys, 'argv', args):
                with self.assertRaisesRegex(ValueError, 'Frontend dist predates build input'):
                    bundle_builder.main()
            self.assertFalse(output.exists())
            os.utime(built, ns=(3_000_000_000, 3_000_000_000))
            bundle_builder.require_fresh_frontend(root)

    def test_hardlinks_are_limited_and_zip_is_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, service, repository = (root / name for name in ('base', 'service', 'repository'))
            source_files = {
                'models/fixture/weights.bin': b'locked-model-asset',
                'runtimes/control/python.exe': b'locked-control-runtime',
                'runtimes/ppocr/package.bin': b'locked-ocr-runtime',
                'runtimes/service/python.exe': b'old-service-must-not-be-inherited',
                'licenses/NOTICE': b'old-license',
                'LICENSE': b'old-top-license',
            }
            for name, content in source_files.items():
                write(base, name, content)
            records = [record(base, name) for name in source_files]
            write(base, 'manifest.json', json.dumps({'files': records}).encode())
            write(service, 'python.exe', b'new-service-independent')
            for folder in ('src', 'config', 'frontend/dist', 'docs', 'licenses', 'scripts'):
                write(repository, folder + '/fixture.txt', folder.encode())
            write(repository, 'frontend/dist/index.html', b'fixture build')
            write(repository, 'README.md', b'current README')
            write(repository, 'config/runtime-locks/agent.json', b'{"packages": []}')
            for name in ('latest-development-checks.json', 'NEXT.md', 'supported-configurations.json'):
                write(repository, 'audit/ocr-agent-20260921-langgraph/' + name, b'fixture')
            output, receipt = root / 'candidate', root / 'receipt.json'
            args = ['prepare_agent_bundle.py', '--base-bundle', str(base), '--service-runtime', str(service),
                    '--output', str(output), '--receipt', str(receipt), '--hardlink-inherited-assets']
            with patch.object(bundle_builder, 'ROOT', repository), patch.object(sys, 'argv', args):
                bundle_builder.main()
            result = json.loads(receipt.read_text('utf-8'))
            self.assertEqual(result['hardlinked_files'], 3)
            self.assertEqual(result['copied_inherited_files'], 2)
            self.assertEqual(result['staging_storage'], 'shared_hardlinks')
            for relative in ('models/fixture/weights.bin', 'runtimes/control/python.exe', 'runtimes/ppocr/package.bin'):
                self.assertEqual((base / relative).stat().st_ino, (output / relative).stat().st_ino)
            self.assertNotEqual((base / 'licenses/NOTICE').stat().st_ino, (output / 'licenses/NOTICE').stat().st_ino)
            self.assertNotEqual((service / 'python.exe').stat().st_ino, (output / 'runtimes/service/python.exe').stat().st_ino)
            self.assertEqual((output / 'runtimes/service/python.exe').read_bytes(), b'new-service-independent')
            self.assertNotEqual((repository / 'src/fixture.txt').stat().st_ino, (output / 'app/fixture.txt').stat().st_ino)
            listed = json.loads((output / 'manifest.json').read_text('utf-8'))['files']
            self.assertEqual({item['path'] for item in listed},
                             {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()} - {'manifest.json'})
            self.assertTrue(all(record(output, item['path']) == item for item in listed))
            archive = root / 'candidate.zip'
            package_receipt = root / 'package-receipt.json'
            subprocess.run([sys.executable, str(PACKAGE), '--bundle', str(output), '--archive', str(archive),
                            '--receipt', str(package_receipt)], check=True, capture_output=True)
            self.assertTrue(json.loads(package_receipt.read_text('utf-8'))['source_bytes_verified'])
            with zipfile.ZipFile(archive) as packaged:
                self.assertEqual(packaged.read('OfflineOCR/models/fixture/weights.bin'), b'locked-model-asset')
            # A private fixture change illustrates the staging alias risk while
            # confirming that ZIP entries contain their own bytes.
            (base / 'models/fixture/weights.bin').write_bytes(b'mutated-source-fixture')
            self.assertEqual((output / 'models/fixture/weights.bin').read_bytes(), b'mutated-source-fixture')
            with zipfile.ZipFile(archive) as packaged:
                self.assertEqual(packaged.read('OfflineOCR/models/fixture/weights.bin'), b'locked-model-asset')

    def test_rejects_unsafe_paths_and_limits_link_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root, 'models/fixture.bin', b'fixture')
            for name in ('../outside', '/absolute', 'models/../fixture.bin', 'models\\fixture.bin'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    bundle_builder.safe_member(root, name)
            self.assertFalse(bundle_builder.hardlink_eligible('runtimes/service/python.exe'))
            self.assertFalse(bundle_builder.hardlink_eligible('licenses/NOTICE'))
            self.assertTrue(bundle_builder.hardlink_eligible('models/fixture.bin'))
            self.assertTrue(bundle_builder.hardlink_eligible('runtimes/ppocr/python.exe'))
            staging = root / 'copied-candidate'
            staging.mkdir()
            copied = bundle_builder.stage_inherited(root, staging, [record(root, 'models/fixture.bin')])
            self.assertEqual(copied['hardlinked_files'], 0)
            self.assertNotEqual((root / 'models/fixture.bin').stat().st_ino,
                                (staging / 'models/fixture.bin').stat().st_ino)


if __name__ == '__main__':
    unittest.main()
