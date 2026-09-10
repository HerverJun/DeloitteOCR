"""Create relocatable CPython embedded runtimes, never copy a development venv."""
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile


def prepare(base: Path, archive: Path, requirements: Path, name: str, proxy=None):
    root = base / 'runtimes' / name
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(root)
    (root / 'python312._pth').write_text(
        'python312.zip\n.\nLib/site-packages\n../../app\nimport site\n', encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install',
                    *(['--proxy', proxy] if proxy else []),
                    '--target', str(root / 'Lib/site-packages'),
                    '--only-binary=:all:', '-r', str(requirements),
                    '--report', str(root / 'install-report.json')], check=True)
    subprocess.run([str(root / 'python.exe'), '-I', '-c',
                    'import sys; print(sys.version); print(sys.path)'], check=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--requirements', type=Path, required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--proxy')
    a = p.parse_args()
    prepare(a.bundle, a.archive, a.requirements, a.name, a.proxy)
