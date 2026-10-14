"""Freeze the dirty source tree and populate an independent offline bundle."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--delivery',type=Path,required=True);a=p.parse_args()
    target=a.delivery.resolve();bundle=target/'bundle'
    if not (bundle/'runtimes/service/python.exe').is_file():raise ValueError('Copy immutable runtimes/models to a NEW bundle first')
    if target in {Path(p).resolve() for p in ('E:/OCR-document-workflow-20260913','E:/OCR-structure-workflow-20260916')}:raise ValueError('Never overwrite an existing release')
    lock_path=target/'release-source-lock.json'
    if lock_path.exists():raise ValueError('Source already frozen; do not mutate the release')
    allowed=['src','config','docs','tests','scripts','fixtures','licenses','frontend/src','frontend/public','frontend/dist','audit/structure-workflow-20260916','audit/public-quality-20260916','audit/generic-tool-fixes-20260917']
    paths=set()
    for folder in allowed:
        paths.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    paths.update(p for p in (ROOT/'frontend').glob('*') if p.is_file() and p.suffix not in ('.tsbuildinfo',))
    paths.update(ROOT/name for name in ('README.md','LICENSE','.gitignore','.gitattributes') if (ROOT/name).is_file())
    records=[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(paths)]
    import sys
    sys.path.insert(0,str(ROOT/'src'))
    from ocr_workbench import __version__
    from ocr_workbench.store import SCHEMA_VERSION
    lock={'version':__version__,'schema':SCHEMA_VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),
        'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'git_status':subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True,encoding='utf-8'),
        'includes_preexisting_dirty_changes':True,'files':records,'default_provider':'paddle','default_matcher':'local-v2','automatic_structure_adoption':False}
    lock_path.write_text(json.dumps(lock,ensure_ascii=False,indent=2),encoding='utf-8')
    with zipfile.ZipFile(target/'structure-workflow-source.zip','x',zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            path=ROOT/record['path']
            if digest(path)!=record['sha256']:raise ValueError('Source changed while freezing')
            archive.write(path,record['path'])
        archive.write(lock_path,'release-source-lock.json')
    copied=[]
    for record in records:
        rel=record['path'];dest=None
        if rel.startswith('src/'):dest='app/'+rel[4:]
        elif rel.startswith('frontend/dist/'):dest='web/'+rel[len('frontend/dist/'):]
        elif rel.startswith(('config/','docs/','fixtures/','licenses/','audit/')):dest=rel
        elif rel.startswith('scripts/'):dest='tools/'+rel[8:]
        elif rel in ('README.md','LICENSE'):dest=rel
        if dest:
            output=bundle/dest;output.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/rel,output)
            if digest(output)!=record['sha256']:raise ValueError('Copied source mismatch')
            copied.append({'source':rel,'bundle_path':dest,'sha256':record['sha256']})
    (bundle/'locks').mkdir(exist_ok=True)
    shutil.copy2(lock_path,bundle/'locks/structure-source-lock.json')
    shutil.copy2(ROOT/'frontend/package-lock.json',bundle/'locks/frontend-package-lock.json')
    # Runtime paths remain relative and exactly match the provider configuration.
    config=json.loads((ROOT/'config/geometry-providers.json').read_text('utf-8'))
    for provider in config['providers'].values():
        for rel,expected in provider['files'].items():
            source=ROOT/rel
            if digest(source)!=expected:raise ValueError('Experimental asset differs: '+rel)
            output=bundle/rel;output.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,output)
    rapid='build/tableformer-next-20260915/runtime/rapid-dependencies'
    shutil.copytree(ROOT/rapid,bundle/rapid,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('model-card.md','model-download.json'):
        shutil.copy2(ROOT/'build/table-matching-v2/n3'/name,bundle/'build/table-matching-v2/n3'/name)
    tool=json.loads((ROOT/'config/pdf-table-tool.json').read_text('utf-8'))
    for relative,expected in tool['files'].items():
        if digest(ROOT/relative)!=expected:raise ValueError('PDF tool source differs: '+relative)
    shutil.copytree(ROOT/tool['source'],bundle/tool['source'],dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc','bin','direct_url.json'))
    receipt={'source_lock_sha256':digest(lock_path),'source_archive_sha256':digest(target/'structure-workflow-source.zip'),
        'source_files':len(records),'copied_files':copied,'experimental_assets':'config/geometry-providers.json',
        'evaluation_PyMuPDF_included':False}
    (target/'source-copy-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'source_files':len(records),'copied':len(copied),'delivery':str(target)}),flush=True)


if __name__=='__main__':main()
