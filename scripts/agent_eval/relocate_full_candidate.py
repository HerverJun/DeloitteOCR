"""Copy an entire frozen Agent candidate to a new path and audit its startup.

The source candidate is read-only. A same-host run proves a full byte-for-byte
relocation and isolated startup, not clean Windows or physical disconnection.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from target_machine_acceptance import contained_file, digest, verify_manifest, verify_unchanged, write


def utc():
    return datetime.now(timezone.utc).isoformat()


def disjoint(source, destination, output):
    source = source.resolve()
    destination = destination.resolve()
    output = output.resolve()
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError('Source and destination must be disjoint')
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for a, b in ((output, source), (output, destination))):
        raise ValueError('Receipt path must be outside source and destination')
    if destination.exists() or output.exists():
        raise ValueError('Destination and receipt directory must both be new')
    if not source.is_dir() or source.is_symlink() or source.is_junction():
        raise ValueError('Source must be an ordinary candidate directory')
    return source, destination, output


def copy_and_verify(source, destination):
    original = verify_manifest(source)
    records = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))['files']
    destination.mkdir(parents=True)
    copied = 0
    for record in records:
        relative = record['path']
        candidate = contained_file(source, relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target)
        copied += record['bytes']
    shutil.copy2(source / 'manifest.json', destination / 'manifest.json')
    relocated = verify_manifest(destination)
    if relocated['manifest_sha256'] != original['manifest_sha256'] or copied != original['bytes']:
        raise ValueError('Relocated content does not match source manifest')
    unchanged = verify_unchanged(source, original)
    return {'source_manifest': {k: value for k, value in original.items() if k != 'file_stats'},
            'destination_manifest': {k: value for k, value in relocated.items() if k != 'file_stats'},
            'copied_bytes': copied, 'source_unchanged': unchanged}


def self_test(output):
    if output.exists():
        raise ValueError('Self-test output must be new')
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        source = Path(temporary) / '原候选'
        target = Path(temporary) / '新候选'
        source.mkdir()
        payload = source / '中文文件.txt'
        payload.write_text('合成搬迁夹具\n', encoding='utf-8')
        data = payload.read_bytes()
        write(source / 'manifest.json', {'files': [{'path': payload.name, 'bytes': len(data),
                                                   'sha256': hashlib.sha256(data).hexdigest()}]})
        checked = copy_and_verify(source, target)
        refusal = False
        try:
            disjoint(source, target, output / 'unused')
        except ValueError:
            refusal = True
        (target / payload.name).write_text('篡改', encoding='utf-8')
        tamper = False
        try:
            verify_manifest(target)
        except ValueError:
            tamper = True
        result = {'status': 'pass' if refusal and tamper and checked['copied_bytes'] == len(data) else 'fail',
                  'scope': 'small synthetic full-copy/hash, existing-target refusal and destination tamper rejection',
                  'copied_bytes': checked['copied_bytes'], 'existing_target_rejected': refusal,
                  'destination_tamper_rejected': tamper, 'real_candidate': 'not_tested'}
    write(output / 'relocation-selftest.json', result)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] != 'pass':
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        if args.bundle or args.destination:
            parser.error('Self-test does not accept candidate paths')
        self_test(args.output.resolve())
        return
    if not args.bundle or not args.destination:
        parser.error('--bundle and --destination are required')
    if platform.system() != 'Windows':
        parser.error('Full candidate relocation requires Windows')
    source, destination, output = disjoint(args.bundle, args.destination, args.output)
    if not any(ord(ch) > 127 for ch in str(destination)):
        parser.error('Target path must contain Chinese/non-ASCII characters for this G03 run')
    if not (source / 'runtimes/service/python.exe').is_file() \
            or not (source / 'tools/target_machine_acceptance.py').is_file():
        parser.error('Source candidate lacks its portable runtime or target audit entry')
    output.mkdir(parents=True)
    receipt = {'status': 'running', 'started_utc': utc(), 'source': str(source),
               'destination': str(destination), 'receipt': str(output),
               'script_sha256': digest(Path(__file__)),
               'destination_has_non_ascii': any(ord(ch) > 127 for ch in str(destination)),
               'qualification_limits': ['Same Windows host, not a clean target machine.',
                                        'No physical network disconnection or cross-user DPAPI proof.',
                                        'No old-release executable rollback or real-controller accuracy proof.']}
    write(output / 'full-relocation.json', receipt)
    try:
        started = time.monotonic()
        receipt['copy'] = copy_and_verify(source, destination)
        receipt['copy_and_hash_seconds'] = time.monotonic() - started
        runtime = destination / 'runtimes/service/python.exe'
        script = destination / 'tools/target_machine_acceptance.py'
        if not runtime.is_file() or not script.is_file():
            raise ValueError('Relocated candidate lacks its portable runtime or target audit entry')
        target_output = output / 'relocated-startup'
        command = [str(runtime), '-X', 'utf8', '-B', '-I', str(script), '--bundle', str(destination),
                   '--output', str(target_output)]
        with (output / 'relocated-startup.log').open('w', encoding='utf-8') as log:
            completed = subprocess.run(command, cwd=output, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=3600)
        receipt['isolated_startup'] = {'exit_code': completed.returncode,
                                       'command': command, 'log': 'relocated-startup.log',
                                       'receipt': str(target_output / 'target-environment-results.json')}
        if completed.returncode:
            raise RuntimeError('Relocated isolated startup failed')
        target_receipt = json.loads((target_output / 'target-environment-results.json').read_text(encoding='utf-8'))
        if target_receipt['status'] != 'observed_checks_passed_with_qualification_gaps':
            raise RuntimeError('Relocated target audit did not pass its bounded checks')
        receipt['status'] = 'same_host_full_relocation_passed_with_qualification_limits'
    except Exception as error:
        receipt.update(status='fail', error_type=type(error).__name__, error=str(error),
                       partial_destination_retained=destination.exists())
    receipt['completed_utc'] = utc()
    write(output / 'full-relocation.json', receipt)
    print(json.dumps({'status': receipt['status'], 'receipt': str(output / 'full-relocation.json')}, ensure_ascii=False))
    if receipt['status'] == 'fail':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
