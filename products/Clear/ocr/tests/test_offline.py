import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ocr_workbench.offline import configure


class OfflineTests(unittest.TestCase):
    def temporary_directory(self):
        temporary = Path(os.environ['TEMP']).resolve()
        self.assertEqual(temporary.drive.upper(), 'F:')
        return tempfile.TemporaryDirectory(dir=temporary)

    def test_configured_vendor_paths_use_f_without_changing_user_identity(self):
        identity = {key: os.environ.get(key) for key in
                    ('USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'HOME', 'CODEX_HOME')}
        with self.temporary_directory() as temp, patch.dict(os.environ, {
            'MODELSCOPE_CACHE': 'C:/unused-vendor-cache',
            'MODELSCOPE_HOME': 'C:/unused-vendor-config',
            'MODELSCOPE_CREDENTIALS_PATH': 'C:/unused-vendor-credentials',
            'AISTUDIO_CACHE': 'C:/unused-vendor-downloads',
            'AISTUDIO_CACHE_HOME': 'C:/unused-vendor-auth',
            'PADDLE_EXTENSION_DIR': 'C:/unused-vendor-extensions',
            'HTTP_PROXY': 'http://proxy.invalid',
            'HTTPS_PROXY': 'http://proxy.invalid',
            'ALL_PROXY': 'http://proxy.invalid',
            'PYTHONPATH': 'C:/unused-python-path',
            'PYTHONHOME': 'C:/unused-python-home',
        }):
            root = Path(temp)
            configure(root)
            expected = {
                'MODELSCOPE_CACHE': 'modelscope',
                'MODELSCOPE_HOME': 'modelscope-home',
                'MODELSCOPE_CREDENTIALS_PATH': 'modelscope-home/credentials',
                'AISTUDIO_CACHE': 'aistudio',
                'AISTUDIO_CACHE_HOME': 'aistudio-home',
                'PADDLE_EXTENSION_DIR': 'paddle-extension',
            }
            for key, suffix in expected.items():
                actual = Path(os.environ[key])
                self.assertEqual(actual, (root / 'cache' / suffix).resolve())
                self.assertEqual(actual.resolve().drive.upper(), 'F:', key)
            self.assertEqual({key: os.environ.get(key) for key in identity}, identity)
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'PYTHONPATH', 'PYTHONHOME'):
                self.assertNotIn(key, os.environ)
            self.assertEqual(os.environ['HF_HUB_OFFLINE'], '1')
            self.assertEqual(os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'], 'True')
            self.assertEqual(os.environ['PADDLE_PDX_DISABLE_DEVICE_FALLBACK'], 'True')

    def test_non_f_root_is_rejected_before_writes_or_environment_changes(self):
        before = dict(os.environ)
        with patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError, 'local F drive'):
                configure(Path('C:/must-not-create-ocr-cache'))
            mkdir.assert_not_called()
        self.assertEqual(dict(os.environ), before)

    def test_resolved_vendor_cache_escape_is_rejected_before_writes(self):
        with self.temporary_directory() as temp:
            root = Path(temp)
            original_resolve = Path.resolve
            escaped = root / 'cache' / 'modelscope-home' / 'credentials'

            def resolve(path, *args, **kwargs):
                if path == escaped:
                    return Path('C:/outside-cache-target')
                return original_resolve(path, *args, **kwargs)

            before = dict(os.environ)
            with patch.object(Path, 'resolve', resolve), patch.object(Path, 'mkdir') as mkdir:
                with self.assertRaisesRegex(ValueError, 'local F drive'):
                    configure(root)
                mkdir.assert_not_called()
            self.assertFalse((root / 'cache').exists())
            self.assertEqual(dict(os.environ), before)

    def test_external_network_blocked_loopback_allowed(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[2])
from ocr_workbench.offline import install_guard
from pathlib import Path
import socket,sys
install_guard(Path(sys.argv[1]))
try:
    socket.getaddrinfo('example.com',443)
except RuntimeError:
    pass
else:
    raise AssertionError('External DNS was not blocked')
with socket.socket() as server:
    server.bind(('127.0.0.1',0))
    server.listen()
    with socket.create_connection(server.getsockname(),timeout=1):
        client,_=server.accept()
        client.close()
'''
        with self.temporary_directory() as temp:
            log = Path(temp) / 'network.log'
            result = subprocess.run([sys.executable, '-B', '-c', code, str(log), str(Path(__file__).resolve().parents[1] / 'src')], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('BLOCKED socket.getaddrinfo example.com', log.read_text())


if __name__ == '__main__':
    unittest.main()
