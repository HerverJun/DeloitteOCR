"""Add the document workflow to an independent copy of the offline bundle."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1]
bundle=a.bundle.resolve()
if bundle==Path('E:/OCR-fusion-20260912/bundle').resolve():
    raise SystemExit('Do not modify the previous release')
if not (bundle/'runtimes/ppocr/python.exe').exists(): raise SystemExit('Copy the full bundle first')
subprocess.run([sys.executable,str(root/'scripts/sync_bundle.py'),'--bundle',str(bundle),'--skip-audit'],check=True)
pdf_target=(bundle/'runtimes/pdf').resolve()
if not pdf_target.is_relative_to(bundle) or pdf_target.is_symlink() or pdf_target.is_junction():
    raise ValueError('Unsafe PDF runtime destination')
if pdf_target.exists():shutil.rmtree(pdf_target)
with zipfile.ZipFile(root/'build/document-workflow/pdf-runtime-transfer.zip') as archive:
    archive.extractall(bundle/'runtimes/pdf')
for source,target in [(root/'frontend/dist',bundle/'web'),
    (root/'build/document-workflow/bundle/fonts',bundle/'fonts'),
    (root/'build/document-workflow/models',bundle/'models')]:
    shutil.copytree(source,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree(root/'build/document-workflow/wheelhouse',bundle/'wheelhouse/pdf',dirs_exist_ok=True)
shutil.copytree(root/'licenses',bundle/'licenses',dirs_exist_ok=True)
shutil.copytree(root/'build/document-workflow/delivery-evidence',bundle/'audit/document-workflow-20260913',dirs_exist_ok=True)
for script in root.glob('scripts/*.py'):
    shutil.copy2(script,bundle/'tools'/script.name)
print(bundle,flush=True)
