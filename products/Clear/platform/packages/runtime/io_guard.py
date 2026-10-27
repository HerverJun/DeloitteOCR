"""Optional process-local isolation evidence for portable acceptance runs.

Python audit events do not cover native DLL I/O. Package hashes, relative
embedded import configuration and native loaded-module audits cover that scope.
"""
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, parse_qs
from urllib.request import url2pathname

_installed = False
_configuration = None


def install(config):
    global _installed, _configuration
    _configuration = config
    if _installed:
        return
    from .config import WRITE_ROOTS
    def inside(path, roots):
        return any(path.is_relative_to(root) for root in roots)
    def audit(event, fields):
        if not fields:
            return
        writing, sqlite = False, False
        if event == 'open':
            mode, flags = fields[1:3]
            writing = isinstance(mode, str) and any(char in mode for char in 'wax+')
            writing |= isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            values = fields[:1]
        elif event == 'sqlite3.connect':
            values, sqlite, writing = fields[:1], True, True
        elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'}:
            values, writing = fields[:1], True
        elif event in {'os.rename', 'os.replace'}:
            values, writing = fields[:2], True
        elif event in {'os.link', 'os.symlink'}:
            values, writing = fields[1:2], True
        else:
            return
        active = _configuration
        writes = tuple(active.roots[key] for key in WRITE_ROOTS)
        denied = tuple(Path(value) for value in active.policy.get('deny_read_roots', []))
        for value in values:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            name = os.fsdecode(value)
            if name.casefold() in {':memory:', 'nul', 'nul:', r'\\.\nul'}:
                continue
            if sqlite and name.startswith('file:'):
                uri = urlsplit(name)
                if uri.netloc not in {'', 'localhost'}:
                    raise PermissionError('Network SQLite URI is outside installation storage')
                options = parse_qs(uri.query)
                writing = not (options.get('mode') == ['ro'] or options.get('immutable') == ['1'])
                name = url2pathname(uri.path)
            path = Path(name).absolute().resolve()
            if writing and not inside(path, writes):
                raise PermissionError('Installation write escaped registered roots: ' + str(path))
            if not writing and inside(path, denied):
                raise PermissionError('Acceptance denied a historical development read: ' + str(path))
    sys.addaudithook(audit)
    _installed = True
