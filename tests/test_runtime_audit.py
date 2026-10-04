"""Real Windows junctions and integrity failures in managed OCR runtime sources."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ocr_workbench.runtime_audit import audit_module_paths


@unittest.skipUnless(os.name == 'nt', 'Windows runtime junctions')
class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assertEqual(self.root.drive.upper(), 'F:')
        self.bundle, self.asset = self.root / 'private', self.root / 'sealed'
        self.bundle.mkdir()
        self.lib = self.asset / 'runtimes/hunyuan/Lib'
        self.module = self.lib / 'site-packages/PIL/image.pyd'
        self.module.parent.mkdir(parents=True)
        self.module.write_bytes(b'declared module bytes')
        self.alias = self.bundle / 'runtimes/hunyuan/Lib'
        self.alias.parent.mkdir(parents=True)
        self.make_junction(self.alias, self.lib)
        self.binding = {'app_id': 'ocr', 'asset_bundle': str(self.asset),
                        'asset_candidate_sha256': 'a' * 64}
        self.record = {'schema_version': 1, 'source_bundle': str(self.asset),
                       'candidate_sha256': 'a' * 64, 'sources': [{
                           'path': 'runtimes/hunyuan/Lib', 'files': {
                               'site-packages/PIL/image.pyd': {
                                   'sha256': self.digest(self.module), 'bytes': self.module.stat().st_size}}}]}
        self.seal()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def make_junction(alias, target):
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                        f'New-Item -ItemType Junction -Path {quote(alias)} -Target {quote(target)} | Out-Null'],
                       check=True, creationflags=subprocess.CREATE_NO_WINDOW)

    def seal(self):
        files = []
        for name, data in [('native-binding.json', self.binding), ('runtime-sources.json', self.record)]:
            path = self.bundle / name
            path.write_text(json.dumps(data), encoding='utf-8')
            files.append({'path': name, 'sha256': self.digest(path)})
        (self.bundle / 'bundle.manifest.json').write_text(
            json.dumps({'app_id': 'ocr', 'files': files}), encoding='utf-8')

    def audit(self, *paths):
        return audit_module_paths(self.bundle, paths, self.root / 'Windows', self.root / 'Defender')

    def test_real_junction_module_requires_and_records_exact_hash(self):
        result = self.audit(self.alias / 'site-packages/PIL/image.pyd')
        self.assertEqual(result['verified_runtime_source_modules'], [
            {'path': str(self.module.resolve()), 'sha256': self.digest(self.module)}])
        self.assertFalse(result['non_system_modules_inside_bundle'])
        self.assertEqual(self.module.read_bytes(), b'declared module bytes')

    def test_plain_packaged_runtime_preserves_strict_bundle_behavior(self):
        module = self.bundle / 'runtime.dll'
        module.write_bytes(b'packaged')
        (self.bundle / 'runtime-sources.json').unlink()
        result = self.audit(module)
        self.assertTrue(result['non_system_modules_inside_bundle'])
        self.assertEqual(result['verified_runtime_source_modules'], [])

    def test_unregistered_link_remains_rejected(self):
        (self.bundle / 'runtime-sources.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Unbundled'):
            self.audit(self.module)

    def test_undeclared_neighbor_and_unrelated_f_drive_library_are_rejected(self):
        for module in (self.lib / 'extra.dll', self.root / 'unrelated.dll'):
            module.write_bytes(b'not declared')
            with self.subTest(module=module), self.assertRaisesRegex(RuntimeError, 'Unbundled'):
                self.audit(module)

    def test_same_size_tampering_is_detected_by_hash(self):
        self.module.write_bytes(b'x' * self.module.stat().st_size)
        with self.assertRaisesRegex(RuntimeError, 'integrity changed'):
            self.audit(self.module)

    def test_retargeted_junction_is_rejected(self):
        replacement = self.root / 'other'
        replacement.mkdir()
        self.alias.rmdir()
        self.make_junction(self.alias, replacement)
        with self.assertRaisesRegex(RuntimeError, 'alias target changed'):
            self.audit(self.module)

    def test_metadata_change_is_rejected_before_accepting_any_module(self):
        self.record['candidate_sha256'] = 'b' * 64
        (self.bundle / 'runtime-sources.json').write_text(json.dumps(self.record), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'declaration changed'):
            self.audit(self.module)

    def test_sealed_candidate_identity_mismatch_is_rejected(self):
        self.record['candidate_sha256'] = 'b' * 64
        self.seal()
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            self.audit(self.module)

    def test_source_inventory_cannot_register_path_traversal_or_entire_drive(self):
        for field, value in [('path', 'runtimes'), ('path', '../sealed/runtimes/hunyuan/Lib'),
                             ('file', '../outside.dll'), ('file', 'F:/outside.dll')]:
            with self.subTest(field=field, value=value):
                source = self.record['sources'][0]
                old_path, old_files = source['path'], source['files']
                if field == 'path':
                    source['path'] = value
                else:
                    source['files'] = {value: {'sha256': 'a' * 64, 'bytes': 1}}
                self.seal()
                with self.assertRaises(RuntimeError):
                    self.audit(self.module)
                source['path'], source['files'] = old_path, old_files

    def test_system_gpu_runtime_remains_rejected_and_os_security_exception_remains_narrow(self):
        gpu = self.root / 'Windows/System32/cudart64.dll'
        gpu.parent.mkdir(parents=True)
        gpu.write_bytes(b'unbundled CUDA')
        with self.assertRaisesRegex(RuntimeError, 'Unbundled'):
            self.audit(gpu)
        security = self.root / 'Defender/mpoav.dll'
        security.parent.mkdir()
        security.write_bytes(b'OS security module')
        self.assertEqual(self.audit(security)['os_security_modules'], [str(security.resolve())])
        other = security.with_name('extra.dll')
        other.write_bytes(b'not the OS exception')
        with self.assertRaisesRegex(RuntimeError, 'Unbundled'):
            self.audit(other)


if __name__ == '__main__':
    unittest.main()
