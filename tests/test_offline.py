import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class OfflineTests(unittest.TestCase):
    def test_external_network_blocked_loopback_allowed(self):
        code = '''
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
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / 'network.log'
            result = subprocess.run([sys.executable, '-c', code, str(log)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('BLOCKED socket.getaddrinfo example.com', log.read_text())


if __name__ == '__main__':
    unittest.main()
