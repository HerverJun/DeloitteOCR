"""Recheck retained PDF visual artifacts using the final isolated package runtime."""
import json
from pathlib import Path
import pypdfium2 as f
from PIL import ImageChops
root=Path(__file__).resolve().parents[1]
records=json.loads((root/'audit/document-workflow-20260913/pdf-visuals/report.json').read_text('utf-8'))
checks=[]
for r in records:
    doc=f.PdfDocument(r['pdf']);original=f.PdfDocument(r['original'])
    page=doc[0];op=original[0];text=page.get_textpage()
    a=page.render(scale=2).to_pil().convert('RGB');b=op.render(scale=2).to_pil().convert('RGB')
    actual=text.get_text_range()
    passed=a.size==b.size and ImageChops.difference(a,b).getbbox() is None and actual==r['independent_text']
    checks.append({'name':r['name'],'identical_visible_pixels_and_readback':passed})
    text.close();page.close();op.close();doc.close();original.close()
report={'passed':all(c['identical_visible_pixels_and_readback'] for c in checks),'scope':'Final packaged PDFium under -I; retained exports rerendered against originals and prior independent text, no OCR rerun','checks':checks}
(root/'audit/document-workflow-20260913/pdf-final-isolated-visual-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(report,ensure_ascii=False));assert report['passed']
