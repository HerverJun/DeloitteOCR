"""Refresh only docs, tools and selected receipts after immutable component testing."""
import argparse
from pathlib import Path
import shutil
p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1];bundle=a.bundle.resolve()
assert bundle==Path('E:/OCR-document-workflow-20260913/bundle').resolve()
for origin,destination in [('docs','docs'),('build/document-workflow/delivery-evidence','audit/document-workflow-20260913')]:
    shutil.copytree(root/origin,bundle/destination,dirs_exist_ok=True)
shutil.copy2(root/'README.md',bundle/'README.md')
for p in (root/'scripts').glob('*.py'):shutil.copy2(p,bundle/'tools'/p.name)
print(bundle)
