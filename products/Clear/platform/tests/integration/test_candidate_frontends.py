"""Incomplete native builds must fail before creating or changing a workspace."""
import sys
from pathlib import Path

import pytest

from scripts.agent_native_preview import configure, main
from scripts.package_agent_candidate import package


def write_file(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding='utf-8')


@pytest.fixture
def native_tree(tmp_path):
    assert tmp_path.drive.upper() == 'F:' and tmp_path.resolve().drive.upper() == 'F:'
    source, program = tmp_path / 'worktrees/platform', tmp_path / 'program'
    write_file(source / 'apps/portal/dist/index.html', 'synthetic portal page')
    for family, dist in [('ocr', 'frontend/dist'), ('classification', 'apps/web/dist')]:
        write_file(source.parent / family / dist / 'index.html', 'synthetic native page')
        write_file(source / f'config/apps/{family}.json', '{}')
        write_file(program / f'bundles/{family}/runtimes/control/python.exe', 'synthetic runtime identity')
        write_file(program / f'bundles/{family}/bundle.manifest.json', '{}')
    write_file(source.parent / 'ocr/build/public-quality-20260916/tool/site-packages/pdfplumber/__init__.py',
               '# Synthetic packaging preflight dependency; never imported')
    return source, program, tmp_path


def remove_classification_index(source):
    (source.parent / 'classification/apps/web/dist/index.html').unlink()


def test_missing_classification_build_does_not_create_workspace(native_tree):
    source, program, root = native_tree
    remove_classification_index(source)
    data = root / 'uncreated-data'
    with pytest.raises(ValueError, match='Native production frontend missing: classification'):
        configure(source, program, data)
    assert not data.exists()


def test_missing_classification_build_does_not_replace_existing_ocr_binding(native_tree):
    source, program, root = native_tree
    data = root / 'existing-data'
    for name in ['binding.json', 'server.py', 'bundle.manifest.json']:
        write_file(data / 'agent-native/bundles/ocr' / name, 'preserved old ' + name)
    before_tree = {path.relative_to(data).as_posix() for path in data.rglob('*')}
    before_files = {path.relative_to(data).as_posix(): path.read_bytes()
                    for path in data.rglob('*') if path.is_file()}
    remove_classification_index(source)
    with pytest.raises(ValueError, match='Native production frontend missing: classification'):
        configure(source, program, data)
    assert {path.relative_to(data).as_posix() for path in data.rglob('*')} == before_tree
    assert {path.relative_to(data).as_posix(): path.read_bytes()
            for path in data.rglob('*') if path.is_file()} == before_files


@pytest.mark.parametrize('family', ['ocr', 'classification'])
def test_missing_native_runtime_does_not_create_workspace(native_tree, family):
    source, program, root = native_tree
    (program / f'bundles/{family}/runtimes/control/python.exe').unlink()
    data = root / 'uncreated-data'
    with pytest.raises(ValueError, match='Existing verified native assets missing'):
        configure(source, program, data)
    assert not data.exists()


def test_cli_checks_frontends_before_creating_workspace_or_lease(native_tree, monkeypatch):
    source, program, root = native_tree
    remove_classification_index(source)
    data = root / 'uncreated-cli-data'
    monkeypatch.setattr(sys, 'argv', ['agent-native-preview', '--source', str(source),
                                     '--program', str(program), '--data', str(data)])
    with pytest.raises(ValueError, match='Native production frontend missing: classification'):
        main()
    assert not data.exists()


@pytest.mark.parametrize('family,dist', [('ocr', 'frontend/dist'), ('classification', 'apps/web/dist')])
def test_packaging_missing_native_build_does_not_create_candidate(native_tree, family, dist):
    source, program, root = native_tree
    (source.parent / family / dist / 'index.html').unlink()
    output = root / 'uncreated-candidate'
    with pytest.raises(ValueError, match='Production native build missing: ' + family):
        package(source, program, root / 'unused-agent-layer', output)
    assert not output.exists()
