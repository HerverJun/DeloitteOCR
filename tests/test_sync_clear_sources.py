"""Integration inventory must preserve main-branch edits and detect lost bytes."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/sync_clear_sources.py'
    spec = importlib.util.spec_from_file_location('clear_source_inventory', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'project'
    destination = root / 'products/Clear'
    metadata = root / '.git'
    metadata.mkdir(parents=True)
    origins = []
    for name in ('platform', 'classification', 'ocr'):
        directory = destination / name
        directory.mkdir(parents=True)
        (directory / 'implementation.py').write_bytes(b'current integrated implementation\r\n')
        origins.append({'name': name, 'source_path': str(tmp_path / 'old' / name),
                        'head': 'old-upstream-head', 'branch': 'legacy',
                        'working_tree_status': [], 'files': []})
    (destination / 'source-manifest.json').write_text(
        json.dumps({'format': 'clear-authored-source-snapshot-v1',
                    'exclusions': {}, 'sources': origins}), encoding='utf-8')
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'DESTINATION', destination)

    def git(source, *args):
        assert source == root, 'Integrated maintenance must not read an upstream worktree'
        return {'branch': 'codex/unified', 'rev-parse': str(metadata) if '--absolute-git-dir' in args else 'base-head'}[args[0]]

    monkeypatch.setattr(module, 'git', git)
    return module, destination


def test_default_refresh_keeps_new_main_branch_edits_and_upstream_history(inventory):
    module, destination = inventory
    first = module.refresh_integrated()
    current = destination / 'ocr/implementation.py'
    current.write_bytes(b'newer main branch work\r\n')
    second = module.capture()
    assert current.read_bytes() == b'newer main branch work\r\n'
    assert first['source_authority']['integration_base_head'] == second['source_authority']['integration_base_head']
    assert second['sources'][2]['upstream_sources'][0]['head'] == 'old-upstream-head'
    assert module.verify() == {'status': 'passed', 'verified_files': 3}


def test_explicit_legacy_import_cannot_overwrite_an_integrated_branch(inventory):
    module, destination = inventory
    module.refresh_integrated()
    current = (destination / 'ocr/implementation.py').read_bytes()
    with pytest.raises(ValueError, match='overwrite merged work'):
        module.capture(Path('F:/old-source'))
    assert (destination / 'ocr/implementation.py').read_bytes() == current


@pytest.mark.parametrize('change', ['tamper', 'missing', 'unexpected'])
def test_verification_rejects_lost_or_changed_source(inventory, change):
    module, destination = inventory
    module.refresh_integrated()
    target = destination / 'platform/implementation.py'
    if change == 'tamper':
        target.write_bytes(b'different bytes')
    elif change == 'missing':
        target.unlink()
    else:
        (destination / 'platform/unrecorded.py').write_bytes(b'unrecorded change')
    with pytest.raises(ValueError, match='mismatch'):
        module.verify()
