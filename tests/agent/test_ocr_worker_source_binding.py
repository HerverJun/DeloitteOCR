"""Private preview pins OCR Python source without modifying shared runtimes."""
import hashlib
from scripts.agent_native_preview import _ocr_worker_sources


def test_preview_worker_copy_has_exact_source_hashes_and_ignores_later_source_edits(tmp_path):
    native, bundle = tmp_path / 'native-source', tmp_path / 'bundle'
    package = native / 'src/ocr_workbench'
    package.mkdir(parents=True)
    bundle.mkdir()
    for name in ('engine_host.py', 'worker.py', 'adapter.py'):
        (package / name).write_text('# reviewed ' + name, encoding='utf-8')
    worker, files = _ocr_worker_sources(native, bundle)
    assert worker.is_relative_to(bundle)
    assert len(files) == 3
    for item in files:
        original = native / 'src' / item['path']
        copied = worker / item['path']
        assert copied.read_bytes() == original.read_bytes()
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == item['sha256']
    (package / 'worker.py').write_text('# later source edit', encoding='utf-8')
    assert (worker / 'ocr_workbench/worker.py').read_text('utf-8') == '# reviewed worker.py'
