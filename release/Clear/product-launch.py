"""Detach the owned Windows service with only its F log handles inherited."""
from pathlib import Path
import argparse,json,subprocess,sys
def checked(value):
    path=Path(value).absolute()
    if path.drive.upper()!='F:' or path.resolve().drive.upper()!='F:':
        raise ValueError('Product launch paths must stay on F:')
    if any(item.is_symlink() or item.is_junction() for item in (path,*path.parents)):
        raise ValueError('Product launch does not accept linked paths')
    return path
parser=argparse.ArgumentParser()
for name in ('launcher','data','stdout','stderr'): parser.add_argument('--'+name,required=True,type=checked)
parser.add_argument('--port',required=True,type=int)
args=parser.parse_args()
with args.stdout.open('xb') as out,args.stderr.open('xb') as err:
    child=subprocess.Popen([str(checked(sys.executable)),'-I','-B','-u',str(args.launcher),
        '--data',str(args.data),'--port',str(args.port)],cwd=args.launcher.parent,
        stdin=subprocess.DEVNULL,stdout=out,stderr=err,close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)
print(json.dumps({'pid':child.pid}))
