"""Replace only the reviewed Paddle/cuDNN distributions in a stopped runtime."""
import argparse
from pathlib import Path
import shutil
import zipfile

p=argparse.ArgumentParser()
p.add_argument('--bundle',type=Path,required=True)
p.add_argument('--wheels',type=Path,required=True)
a=p.parse_args()
specs=[('paddlepaddle_gpu-3.3.1+ocr.1-cp312-cp312-win_amd64.whl','paddle','paddlepaddle_gpu-*.dist-info'),
       ('nvidia_cudnn_cu12-9.9.0.52-py3-none-win_amd64.whl','nvidia/cudnn','nvidia_cudnn_cu12-*.dist-info')]
for filename,_,_ in specs:
    with zipfile.ZipFile(a.wheels/filename) as z:
        if z.testzip():
            raise ValueError(f'Corrupt wheel: {filename}')
for runtime in ['ppocr','paddlevl']:
    site=(a.bundle/'runtimes'/runtime/'Lib/site-packages').resolve()
    if not site.is_relative_to(a.bundle.resolve()) or not (site/'paddle').is_dir():
        raise ValueError('Expected an existing bundled Paddle runtime')
    for filename,module,pattern in specs:
        targets=[site/module,*site.glob(pattern)]
        for target in targets:
            resolved=target.resolve()
            if not resolved.is_relative_to(site) or resolved==site:
                raise ValueError('Unsafe replacement path')
            if resolved.exists():
                shutil.rmtree(resolved)
        with zipfile.ZipFile(a.wheels/filename) as z:
            for member in z.infolist():
                rel=Path(member.filename)
                if rel.is_absolute() or '..' in rel.parts:
                    raise ValueError('Unsafe wheel member')
                if '.data' in rel.parts[0]:
                    if rel.parts[1] in {'purelib','platlib'}:
                        rel=Path(*rel.parts[2:])
                    else:
                        raise ValueError(f'Unexpected wheel scheme: {rel}')
                dest=site/rel
                if member.is_dir():
                    dest.mkdir(parents=True,exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(member) as src,dest.open('wb') as out:
                        shutil.copyfileobj(src,out)
    shutil.copy2(a.wheels/'paddle-wheel-patch.json',a.bundle/'runtimes'/runtime/'paddle-wheel-patch.json')
    print(f'Repaired {runtime}',flush=True)
