"""Synthetic PDF correctness fixtures, never geometry accuracy benchmark data."""
from pathlib import Path
import json
import sys
from fpdf import FPDF
import pikepdf
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
output = root / 'build/document-workflow/fixtures'
output.mkdir(parents=True, exist_ok=True)

def base():
    pdf = FPDF(unit='pt', format=(300, 400))
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font('Helvetica', size=12)
    return pdf

native = base()
native.text(35, 70, 'Native 001234 $ 123.45')
native.text(35, 100, 'Long ID 00123456789012345678')
native.output(output / 'native.pdf')
page = Image.new('RGB', (600, 650), 'white')
draw = ImageDraw.Draw(page)
font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 28)
for n, value in enumerate(['Scanned invoice 001234', 'Amount $ 123.45', 'Account 00123456789012345678']):
    draw.text((20, 70+n*90), value, fill='black', font=font)
page.save(output / 'scan.png')
scan = base()
scan.image(str(output / 'scan.png'), x=10, y=20, w=280, h=330)
scan.output(output / 'scan.pdf')
mixed = base()
mixed.image(str(output / 'scan.png'), x=10, y=20, w=280, h=330)
mixed.text(140, 382, 'Page 001')
mixed.output(output / 'mixed.pdf')
blank = base()
blank.output(output / 'blank.pdf')
with pikepdf.open(output / 'mixed.pdf') as pdf:
    hidden = b'BT /F1 12 Tf 3 Tr 1 0 0 1 30 250 Tm (OLD WRONG OCR) Tj ET'
    pdf.pages[0].contents_add(hidden)
    pdf.save(output / 'old-ocr.pdf')
with pikepdf.open(output / 'native.pdf') as pdf:
    pdf.save(output / 'encrypted.pdf', encryption=pikepdf.Encryption(owner='owner-fixture', user='temporary-fixture-secret', R=6))
    pdf.pages[0].obj.UserUnit = 2
    pdf.save(output / 'user-unit.pdf')
for rotation in (0, 90, 180, 270):
    with pikepdf.open(output / 'native.pdf') as pdf:
        pdf.pages[0].obj.CropBox = [20, 30, 290, 390]
        pdf.pages[0].obj.Rotate = rotation
        pdf.save(output / f'crop-{rotation}.pdf')
with pikepdf.new() as pdf:
    for _ in range(1000):
        pdf.add_blank_page(page_size=(300, 400))
    pdf.save(output / 'thousand.pdf')
page.save(output / 'multipage.tiff', save_all=True, append_images=[Image.new('RGB', (120, 80), 'white')])
(output / 'broken.pdf').write_bytes(b'%PDF-1.7\ncorrupt')
(output / 'manifest.json').write_text(json.dumps({'purpose': 'synthetic correctness and capacity only', 'pdf_count': len(list(output.glob('*.pdf')))}, indent=2), 'utf-8')
print(str(output))
