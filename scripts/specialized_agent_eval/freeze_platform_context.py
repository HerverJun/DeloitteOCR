"""Freeze platform production and Agent test sources for independent CPU gates."""
from __future__ import annotations
import argparse
import shutil

from common import frozen_json, fpath
from freeze_sources import PARTS, checked, inventory, sha


def freeze(source, output):
    source, output = checked(source), checked(output)
    if output.exists():
        raise ValueError('An existing source snapshot must never be overwritten')
    members = (*PARTS['platform'], 'tests/agent')
    before = inventory(source, members)
    output.mkdir(parents=True)
    platform = output / 'platform'
    for name, expected in before.items():
        original, target = checked(source / name), checked(platform / name)
        if sha(original) != expected:
            raise ValueError('Source changed before copy: ' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        if sha(target) != expected:
            raise ValueError('Snapshot copy differs: ' + name)
    if inventory(source, members) != before or inventory(platform, members) != before:
        raise ValueError('Source inventory changed during freeze')
    receipt = {'format': 's07-agent-platform-and-tests-freeze-v1', 'source': str(source),
        'frozen': str(platform), 'members': list(members), 'files': before,
        'member_count': len(before), 'copy_verified': True, 'gate_not_yet_executed': True,
        'gpu_calls': 0, 'model_calls': 0, 'formal_current_changed': False,
        'helper_sha256': sha(__file__)}
    frozen_json(output / 'freeze.json', receipt)
    print('source_and_tests_frozen', len(before), str(platform))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    freeze(fpath(args.source), fpath(args.output))
