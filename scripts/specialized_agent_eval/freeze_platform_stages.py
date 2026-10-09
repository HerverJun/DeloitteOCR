"""Freeze owned Agent changes over an immutable platform baseline on F:."""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
from common import digest, fpath, frozen_json
from s07_agent_native_followup import platform_binding


def freeze(baseline, source, output, additional_source_files=()):
    baseline, source, output = map(fpath, (baseline, source, output))
    if output.exists():
        raise ValueError('An immutable overlay freeze needs a new directory')
    originals = platform_binding(baseline)
    names = {'src/workbench_platform/agent/' + name for name in
             ('context.py', 'matching.py', 'preferences.py', 'runtime.py', 'graph.py')}
    for directory in ('config/skills/existing-ocr-reconciliation',
                      'config/skills/published-image-classification', 'tests/agent'):
        names.update(path.relative_to(source).as_posix() for path in (source / directory).rglob('*')
                     if path.is_file() and path.suffix in {'.py', '.json', '.md'})
    changed = {name: digest(source / name) for name in sorted(names)}
    additional = {}
    for name in additional_source_files:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or name in changed:
            raise ValueError('Additional verified source must be a distinct repo-relative file')
        additional[relative.as_posix()] = digest(source / relative)
    target = output / 'platform'
    shutil.copytree(baseline, target)
    for name, expected in {**changed, **additional}.items():
        original, copied = source / name, target / name
        if original.is_symlink() or original.is_junction():
            raise ValueError('Agent overlay source is linked')
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, copied)
        if digest(copied) != expected or digest(original) != expected:
            raise ValueError('Owned overlay changed while freezing: ' + name)
    if platform_binding(baseline) != originals:
        raise ValueError('Immutable platform baseline changed during freeze')
    frozen_json(output / 'freeze.json', {'format': 's07-agent-stage-overlay-freeze-v1',
        'baseline': str(baseline), 'baseline_files': originals, 'owned_overlay_source': str(source),
        'owned_overlay_files': changed, 'frozen': str(target), 'frozen_files': platform_binding(target),
        'additional_verified_source_files': additional,
        'copy_verified': True, 'gpu_calls': 0, 'formal_current_changed': False,
        'helper_sha256': digest(__file__)})
    print('baseline-plus-owned-overlay-frozen', len(changed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'source', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--additional-source-file', action='append', default=[])
    args = parser.parse_args()
    freeze(args.baseline, args.source, args.output, args.additional_source_file)
