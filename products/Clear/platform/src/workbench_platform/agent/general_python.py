"""A private embedded Python launcher with normal local-project imports.

The sealed Data runtime supplies read-only stdlib and installed packages. Only
the small launcher and its path configuration belong to the Agent host. Extra
packages are installed into the session workspace, never into the sealed base.
"""
from pathlib import Path
import shutil

from workbench_platform.supervisor.processes import require_f_path


def prepare(root, base):
    base = require_f_path(base).parent
    target = require_f_path(root / 'python')
    target.mkdir(parents=True, exist_ok=True)
    from .windows_workspace import prepare_owner_access
    prepare_owner_access(target, writable=False, inherit=True)
    paths = list(base.glob('python[0-9]*._pth'))
    if len(paths) != 1:
        raise ValueError('General Python requires one embedded runtime path configuration')
    version = paths[0].stem
    for source in [base / 'python.exe', *base.glob('python*.dll'), *base.glob('vcruntime*.dll')]:
        destination = require_f_path(target / source.name)
        if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
            shutil.copyfile(source, destination)
    config = '\n'.join([str(base / (version + '.zip')), str(base),
                        str(base / 'Lib/site-packages'), '.', 'import site', ''])
    (target / (version + '._pth')).write_text(config, encoding='utf-8')
    (target / 'sitecustomize.py').write_text(
        'import os, sys\nfrom pathlib import Path\n'
        'cwd = Path.cwd().resolve()\n'
        'if cwd.drive.lower() != "f:":\n    raise RuntimeError("Agent Python requires a local F-drive working directory")\n'
        'sys.path[:0] = [str(cwd), str(cwd / ".clear" / "python")]\n'
        'if sys.argv and sys.argv[0] and not sys.argv[0].startswith("-"):\n'
        '    script = Path(sys.argv[0]).resolve().parent\n'
        '    if script.drive.lower() == "f:":\n        sys.path.insert(0, str(script))\n', encoding='utf-8')
    return target / 'python.exe'
