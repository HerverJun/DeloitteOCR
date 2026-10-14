"""Compatibility probe with known native glyph locations and page transforms."""
import json
from pathlib import Path

from fpdf import FPDF
import pikepdf
import pypdfium2 as pdfium
from docling_parse.pdf_parser import DoclingPdfParser

out = Path('build/document-workflow/probe')
out.mkdir(parents=True, exist_ok=True)
pdf = FPDF(unit='pt', format=(300, 400))
pdf.set_auto_page_break(False)
for _ in range(4):
    pdf.add_page()
    pdf.set_font('Helvetica', size=12)
    pdf.text(60, 100, 'Native 001234 $ 123.45')
pdf.output(out / 'base.pdf')
with pikepdf.open(out / 'base.pdf') as source:
    for index, page in enumerate(source.pages):
        page.obj.CropBox = [30, 50, 270, 370]
        page.obj.Rotate = index * 90
    source.save(out / 'crop-rotated.pdf')
parser = DoclingPdfParser(loglevel='fatal')
doc = parser.load(out / 'crop-rotated.pdf')
records = []
with pdfium.PdfDocument(out / 'crop-rotated.pdf') as rendered:
    for n in range(1, 5):
        page = doc.get_page(n)
        records.append({'page_number': n, 'dimension': page.dimension.model_dump(mode='json'),
                        'words': [c.model_dump(mode='json') for c in page.word_cells],
                        'lines': [c.model_dump(mode='json') for c in page.textline_cells]})
        rp = rendered[n-1]
        try:
            bitmap = rp.render(scale=1)
            bitmap.to_pil().save(out / f'page-{n}.png')
            bitmap.close()
        finally:
            rp.close()
        doc.unload_pages((n, n+1))
doc.unload()
(out / 'docling.json').write_text(json.dumps(records, indent=2), 'utf-8')
for r in records:
    print(json.dumps({'page': r['page_number'], 'dimension': r['dimension'],
                      'first_word': r['words'][:1]}))
