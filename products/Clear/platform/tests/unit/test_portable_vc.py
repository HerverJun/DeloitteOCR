"""Pinned app-local VC closure before any product executable can start."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2]/'scripts/package_portable.py'
spec = importlib.util.spec_from_file_location('clear_test_vc_packager', SCRIPT)
packager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = packager
spec.loader.exec_module(packager)


def pe(machine=0x8664, suffix=b''):
    header = bytearray(64)
    header[:2] = b'MZ'
    struct.pack_into('<I', header, 60, 64)
    return bytes(header)+b'PE\0\0'+struct.pack('<H', machine)+suffix


@pytest.fixture
def vc_package(tmp_path):
    output = tmp_path/'Clear'
    source = tmp_path/'source'
    lock_path = source/'platform/config/runtime-locks/vc-runtime.json'
    lock_path.parent.mkdir(parents=True)
    root = output/'runtimes/vc-runtime'
    root.mkdir(parents=True)
    payloads = {name: pe(suffix=name.encode()) for name in
        ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'vcomp140.dll')}
    payloads['LICENSE.txt'] = b'Original redistributable license'
    files = {}
    for name, content in payloads.items():
        (root/name).write_bytes(content)
        files[name] = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    lock = {'format': 'clear-vc-runtime-lock-v1', 'version': '14.51.36247.0',
        'archive': {'url': 'https://download.visualstudio.microsoft.com/fixed/vc_redist.x64.exe',
            'sha256': 'a'*64, 'bytes': 1024}, 'files': files}
    lock_path.write_text(json.dumps(lock), 'utf-8')
    entries = ('tools/postgresql/bin/postgres.exe', 'tools/node/node.exe',
        'tools/libreoffice/program/soffice.exe', 'assets/program/bundles/ocr/runtimes/llama/llama-server.exe',
        'assets/program/runtimes/platform/python.exe', 'runtimes/documents/python.exe',
        'app/platform/agent_harness/node_modules/node-pty/prebuilds/win32-x64/conpty/OpenConsole.exe')
    planned = []
    for name in entries:
        path = output/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pe())
        planned.append(packager.Member(path, name, 'runtime'))
    ignored = 'runtimes/documents/Lib/site-packages/pip/_vendor/distlib/t32.exe'
    path = output/ignored
    path.parent.mkdir(parents=True)
    path.write_bytes(pe(0x14c))
    planned.append(packager.Member(path, ignored, 'runtime'))
    return output, {'source': str(source)}, planned, lock, lock_path


def test_app_local_vc_closure_is_pinned_complete_and_does_not_target_x86_templates(vc_package):
    output, settings, planned, lock, _path = vc_package
    generated = packager.portable_vc_runtime(output, settings, planned)
    assert len(generated) == 7*4
    for member, expected in generated.items():
        assert expected == lock['files'][Path(member).name]
        assert packager.identity(output/member) == expected
    assert packager.portable_vc_runtime(output, settings, planned) == generated
    manifest = json.loads((output/'manifests/vc-runtime.json').read_text('utf-8'))
    assert manifest['resource_id'] == 'vc_runtime' and manifest['system_install_required'] is False
    assert len(manifest['bindings']) == 7
    assert all(chr(92) not in row['directory'] and not row['directory'].startswith('F:') for row in manifest['bindings'])
    ignored = output/'runtimes/documents/Lib/site-packages/pip/_vendor/distlib/vcruntime140.dll'
    assert not ignored.exists()
    sealed = packager.seal(output)
    assert all(sealed['files'][name] == expected for name, expected in generated.items())


def test_corrupt_or_wrong_arch_redist_is_rejected_before_any_app_local_write(vc_package):
    output, settings, planned, lock, lock_path = vc_package
    target = output/'tools/postgresql/bin/vcomp140.dll'
    payload = output/'runtimes/vc-runtime/vcomp140.dll'
    payload.write_bytes(pe(0x14c, b'vcomp140.dll'))
    # Even if somebody updates an input content pin, the architecture boundary
    # rejects an x86 redist before any executable prefix receives a DLL.
    lock['files']['vcomp140.dll'] = packager.identity(payload)
    lock_path.write_text(json.dumps(lock), 'utf-8')
    with pytest.raises(ValueError, match='not x64'):
        packager.portable_vc_runtime(output, settings, planned)
    assert not target.exists()
    payload.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='fixed content pin mismatch'):
        packager.portable_vc_runtime(output, settings, planned)
    assert not target.exists()


def test_product_entry_architecture_is_checked_before_first_write(vc_package):
    output, settings, planned, _lock, _lock_path = vc_package
    (output/'tools/node/node.exe').write_bytes(pe(0xaa64))
    with pytest.raises(ValueError, match='entry point is not x64'):
        packager.portable_vc_runtime(output, settings, planned)
    assert not (output/'tools/postgresql/bin/vcomp140.dll').exists()


def test_vc_lock_requires_redistributable_license_and_full_core_set(vc_package):
    _output, settings, _planned, lock, path = vc_package
    del lock['files']['LICENSE.txt']
    path.write_text(json.dumps(lock), 'utf-8')
    with pytest.raises(ValueError, match='license'):
        packager.vc_runtime_lock(settings)
    del lock['files']['vcomp140.dll']
    path.write_text(json.dumps(lock), 'utf-8')
    with pytest.raises(ValueError, match='incomplete'):
        packager.vc_runtime_lock(settings)
