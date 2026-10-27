"""The offline directory entry point; never imports a development fallback."""
from pathlib import Path
import os
import sys

os.environ.pop('CLEAR_IO_GUARD', None)
source = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(source), str(source / 'src')]

if __name__ == '__main__':
    from packages.runtime.cli import main
    raise SystemExit(main(package_root=source.parents[1]))
