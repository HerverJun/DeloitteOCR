"""Qualification children retain OS services and managed storage boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/portable_acceptance.py'
spec = importlib.util.spec_from_file_location('clear_test_portable_acceptance', SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acceptance
spec.loader.exec_module(acceptance)


@pytest.mark.skipif(os.name != 'nt', reason='Windows Winsock provider regression')
def test_real_child_initializes_asyncio_with_managed_cache_overrides(tmp_path, monkeypatch):
    assert tmp_path.drive.upper() == 'F:'
    assert os.environ.get('SystemRoot') and os.environ.get('WINDIR')
    root = tmp_path / '中文 qualification evidence'
    root.mkdir()
    names = ('TEMP', 'TMP', 'TMPDIR', 'HF_HOME', 'TORCH_HOME', 'LOCALAPPDATA')
    for name in names:
        monkeypatch.setenv(name, 'C:/unmanaged-inherited-cache')
    managed = {name: str(root / name.lower()) for name in names}
    for value in managed.values():
        Path(value).mkdir()
    identity = {name: os.environ[name] for name in ('SystemRoot', 'WINDIR')}
    preserved = {name: os.environ.get(name) for name in ('HOME', 'USERPROFILE', 'CODEX_HOME')}
    code = (
        'import asyncio,json,os,socket; '
        'stream=socket.socket(); stream.close(); '
        'print(json.dumps({name:os.environ.get(name) for name in '
        + repr((*names, *identity, *preserved)) + '}))'
    )
    log = root / 'child.json'
    acceptance.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code], log, managed)
    actual = json.loads(log.read_text('utf-8'))
    assert {name: actual[name] for name in managed} == managed
    assert {name: actual[name] for name in identity} == identity
    assert {name: actual[name] for name in preserved} == preserved
    assert all(os.environ[name] == 'C:/unmanaged-inherited-cache' for name in names)
    assert set(managed) == set(names)  # The caller's override dictionary was not expanded.


def test_failed_qualification_child_keeps_its_local_log(tmp_path):
    log = tmp_path / 'failed-child.txt'
    with pytest.raises(RuntimeError, match='Exit 7; inspect'):
        acceptance.run([sys.executable, '-I', '-B', '-c', 'print("owned failure");raise SystemExit(7)'],
                       log, {'TEMP': str(tmp_path), 'TMP': str(tmp_path)})
    assert log.read_text('utf-8').strip() == 'owned failure'
