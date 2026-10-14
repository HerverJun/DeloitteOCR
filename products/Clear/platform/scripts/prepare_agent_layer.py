"""Build an independent verified Agent layer from existing F-drive dependencies.

This copies installed distributions, not native model payloads. Neither source
runtime is modified. The receipt binds every copied file to its original bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys

from workbench_platform.supervisor.processes import require_f_path


def prepare(source: Path, destination: Path, lock_source: Path):
    source, destination, lock_source = map(require_f_path, (source, destination, lock_source))
    # packaging is read-only from the existing OCR control runtime.
    sys.path.insert(0, str(source))
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    installed = {canonicalize_name(dist.metadata['Name']): dist for dist in metadata.distributions(path=[str(source)])}
    pending = ['langgraph', 'langchain-core', 'langgraph-checkpoint-sqlite']
    chosen = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in chosen:
            continue
        dist = installed[name]
        chosen[name] = dist
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
                continue
            dependency = installed[canonicalize_name(requirement.name)]
            if dependency.version not in requirement.specifier:
                raise ValueError('Installed dependency violates requirement: ' + raw)
            pending.append(requirement.name)
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, dist in sorted(chosen.items()):
        for member in dist.files or ():
            original = require_f_path(Path(dist.locate_file(member)))
            if not original.is_relative_to(source):
                # Scripts outside site-packages are not part of an import layer.
                continue
            if original.suffix == '.pyc' or not original.is_file():
                continue
            target = require_f_path(destination / original.relative_to(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            if not target.exists():
                shutil.copyfile(original, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Existing layer bytes differ: ' + str(target))
            files[target.relative_to(destination).as_posix()] = digest
    selected = {name: dist.version for name, dist in sorted(chosen.items())}
    locked = []
    for line in lock_source.read_text(encoding='utf-8').splitlines():
        match = re.match(r'([^=]+)==([^ ]+)', line)
        if match and canonicalize_name(match[1]) in selected:
            if selected[canonicalize_name(match[1])] != match[2]:
                raise ValueError('Lock version differs')
            locked.append(line)
    if len(locked) != len(selected):
        raise ValueError('Original wheel lock does not cover all dependencies')
    root = Path(__file__).resolve().parents[1] / 'config/runtime-locks'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'agent.txt').write_text('\n'.join(locked) + '\n', encoding='utf-8')
    receipt = {'format': 'verified-installed-layer-v1', 'python': sys.version, 'source': str(source),
               'destination': str(destination), 'dependencies': selected, 'files': files,
               'network_requests': 0, 'source_runtime_modified': False,
               'wheel_lock_sha256': hashlib.sha256((root / 'agent.txt').read_bytes()).hexdigest()}
    (destination / 'layer-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    (root / 'agent.json').write_text(json.dumps({k: v for k, v in receipt.items() if k != 'files'}, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'packages': len(chosen), 'files': len(files), 'destination': str(destination)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--lock-source', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.destination, args.lock_source)
