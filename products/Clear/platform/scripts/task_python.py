"""Run project commands with an explicit dependency layer and F-drive storage."""
import os
from pathlib import Path
import runpy
import sys

source = Path(__file__).resolve().parents[1]
layer = Path(os.environ['CLEAR_TASK_LAYER']).resolve()
for key in ('TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'PIP_CACHE_DIR'):
    path = Path(os.environ[key]).resolve()
    if path.drive.upper() != 'F:':
        raise RuntimeError(f'{key} must remain on F:')
sys.dont_write_bytecode = True
sys.path[:0] = [str(source), str(source / 'src'), str(layer)]
if sys.argv[1] == '-':
    sys.argv=sys.argv[1:]
    exec(compile(sys.stdin.read(),'<task-stdin>','exec'),{'__name__':'__main__'})
elif sys.argv[1] == '-m':
    module = sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name='__main__', alter_sys=True)
else:
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name='__main__')
