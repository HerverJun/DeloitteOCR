"""Audit Python writes while recognizing SQLite's read-only file URI syntax."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.request import url2pathname


def io_path(value, *, sqlite=False):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None, False
    text = os.fsdecode(value)
    if text.casefold() in {':memory:', 'nul', 'nul:', r'\\.\nul'}:
        return None, False
    read_only = False
    if sqlite and text.startswith('file:'):
        uri = urlsplit(text)
        if uri.netloc not in {'', 'localhost'}:
            raise ValueError('SQLite URI must name a local file')
        if uri.path in {':memory:', '/:memory:'}:
            return None, False
        # url2pathname performs percent decoding itself, exactly once. A
        # literal percent-encoded filename must not become a different path.
        text = url2pathname(uri.path)
        options = parse_qs(uri.query)
        read_only = options.get('mode') == ['ro'] or options.get('immutable') == ['1']
    return Path(text).absolute().resolve(), read_only


def install_guard(*, write_roots, read_roots=(), violations=None):
    writes = tuple(Path(root).resolve() for root in write_roots)
    reads = tuple(Path(root).resolve() for root in (*read_roots, *write_roots))
    violations = [] if violations is None else violations
    def within(path, roots):
        return any(path == root or path.is_relative_to(root) for root in roots)
    def audit(event, fields):
        if not fields:
            return
        if event == 'open':
            mode, flags = fields[1:3]
            writing = isinstance(mode, str) and any(char in mode for char in 'wax+')
            writing |= isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if not writing:
                return
            values = fields[:1]
        elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime', 'sqlite3.connect'}:
            values = fields[:1]
        elif event in {'os.rename', 'os.replace'}:
            values = fields[:2]
        elif event in {'os.link', 'os.symlink'}:
            values = fields[1:2]
        else:
            return
        for value in values:
            path, readonly = io_path(value, sqlite=event == 'sqlite3.connect')
            if path is None:
                continue
            if not within(path, reads if readonly else writes):
                violations.append({'event': event, 'path': str(path), 'read_only': readonly})
                raise RuntimeError('Harness filesystem operation escaped declared roots: ' + str(path))
    sys.addaudithook(audit)
    return violations
