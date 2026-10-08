"""Freeze actual F-drive production sources before model and Agent evaluation."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


PARTS = {
    'platform': ('src', 'packages', 'modules', 'config', 'scripts', 'apps/portal/dist',
                 'docs/agent', 'pyproject.toml'),
    'ocr': ('src', 'config', 'scripts', 'frontend/dist'),
    'classification': ('src', 'configs', 'apps/web/dist', 'pyproject.toml'),
}


def checked(value):
    path = Path(value).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('Frozen inputs and outputs must remain on local F:')
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('Frozen sources must contain actual files, without linked paths')
    return path


def sha(path):
    return hashlib.sha256(checked(path).read_bytes()).hexdigest()


def inventory(source, members):
    result = {}
    for name in members:
        member = checked(source / name)
        if not member.exists():
            raise ValueError('Required source member missing: ' + str(member))
        files = member.rglob('*') if member.is_dir() else [member]
        for path in sorted(files):
            checked(path)
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                result[path.relative_to(source).as_posix()] = sha(path)
    return result


def freeze(worktrees, output):
    worktrees, output = checked(worktrees), checked(output)
    if output.exists():
        raise ValueError('Use a new output; existing source snapshots are never overwritten')
    bindings = {}
    for family, members in PARTS.items():
        source = checked(worktrees / family)
        prefix = ['git', '-C', str(source), '-c', 'safe.directory=' + source.as_posix()]
        head = subprocess.check_output(prefix + ['rev-parse', 'HEAD'], text=True).strip()
        common = checked(subprocess.check_output(prefix + [
            'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip())
        bindings[family] = {'source': str(source), 'base_head': head,
            'git_common_dir': str(common), 'files': inventory(source, members),
            'current_working_contents_frozen': True}
    output.mkdir(parents=True)
    for family, binding in bindings.items():
        source = checked(binding['source'])
        for name, expected in binding['files'].items():
            original, target = checked(source / name), checked(output / family / name)
            if sha(original) != expected:
                raise ValueError('Source changed during freeze: ' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            if sha(target) != expected:
                raise ValueError('Frozen member differs: ' + name)
        if inventory(source, PARTS[family]) != binding['files']:
            raise ValueError('Source membership or bytes changed during freeze')
    receipt = {'format': 'specialized-agent-source-freeze-v1',
        'created': datetime.now(timezone.utc).isoformat(), 'sources': bindings,
        'helper_sha256': sha(__file__), 'truth_read': False, 'model_invoked': False,
        'formal_current_changed': False,
        'scope': 'Production source and prepared frontends, no qualification claim'}
    (output / 'freeze.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return {'frozen': str(output), 'files': {k: len(v['files']) for k, v in bindings.items()},
            'model_invoked': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktrees', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.worktrees, args.output)))
