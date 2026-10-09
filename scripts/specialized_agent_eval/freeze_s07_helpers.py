"""Freeze helper source and its local import closure without moving originals."""
from __future__ import annotations
import argparse
import ast
from pathlib import Path
import shutil
from common import digest, fpath, frozen_json


def freeze(source, output):
    source, output = map(fpath, (source, output))
    if output.exists():
        raise ValueError('Use a new immutable F: helper snapshot')
    names = set(); queue = ['s07_agent_native_followup', 's07_agent_classification', 'context_actual_replay',
        'context_phase_admission', 'context_export_reserve', 'context_receipt_readback',
        'freeze_platform_stages', 'qualify_agent_stages', 'freeze_s07_helpers',
        's07_failed_ledger_archive', 'test_s07_native_validation', 's07_native_export_review']
    while queue:
        name = queue.pop()
        if name in names:
            continue
        path = source / (name + '.py')
        if path.is_symlink() or path.is_junction():
            raise ValueError('Helper source is linked')
        tree = ast.parse(path.read_text('utf8')); names.add(name)
        for node in ast.walk(tree):
            imports = [node.module] if isinstance(node, ast.ImportFrom) and node.module else [item.name for item in node.names] if isinstance(node, ast.Import) else []
            for imported in imports:
                if '.' not in imported and (source / (imported + '.py')).is_file() and imported not in names:
                    queue.append(imported)
    expected = {name + '.py': digest(source / (name + '.py')) for name in sorted(names)}
    output.mkdir(parents=True)
    for name, sha in expected.items():
        shutil.copyfile(source / name, output / name)
        if digest(source / name) != sha or digest(output / name) != sha:
            raise ValueError('Helper changed while freezing: ' + name)
    frozen_json(output / 'helper-source-binding.json', {'format': 's07-helper-local-source-closure-freeze-v1',
        'source': str(source), 'snapshot': str(output), 'files': expected, 'copy_verified': True,
        'original_sources_moved_or_changed': False, 'execution_uses_original_paths': True,
        'reason': 'Original __file__ paths preserve recorded source/program lookup semantics; hashes bind exact execution source bytes.'})
    print('helper_sources_frozen', len(expected))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args(); freeze(args.source, args.output)
