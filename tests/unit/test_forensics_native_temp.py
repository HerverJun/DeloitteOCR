from pathlib import Path
import pytest
from workbench_platform.multimodal.runner import worker_environment


def test_explicit_native_root_contains_all_native_temp_paths(tmp_path, monkeypatch):
    root = tmp_path/'native'
    monkeypatch.setenv('CLEAR_NATIVE_TEMP_ROOT', str(root))
    env = worker_environment(tmp_path/'job', tmp_path/'python.exe')
    paths = [Path(env[key]) for key in ('TEMP','TMP','TMPDIR','CLEAR_PADDLE_TEMP')]
    assert len(set(paths)) == 1
    assert paths[0].is_relative_to(root)
    assert paths[0].is_dir()


@pytest.mark.parametrize('root', ['C:/outside-forensics', 'F:/OCR/中文临时目录'])
def test_invalid_native_root_does_not_fall_back(tmp_path, monkeypatch, root):
    monkeypatch.setenv('CLEAR_NATIVE_TEMP_ROOT', root)
    with pytest.raises(ValueError):
        worker_environment(tmp_path/'job', tmp_path/'python.exe')
