"""Additional text/column/invalid-font fixtures for final PDF correctness audit."""
from pathlib import Path
from fpdf import FPDF
import pikepdf
root=Path(__file__).resolve().parents[1];out=root/'build/document-workflow/fixtures'
p=FPDF(unit='pt',format=(420,540));p.set_auto_page_break(False);p.add_page();p.add_font('NotoSC',fname=str(root/'build/document-workflow/bundle/fonts/NotoSansSC-Regular.ttf'));p.set_font('NotoSC',size=13)
for row,value in enumerate(['中文合同 001234','左栏金额 ￥123.45','左栏编号 00001234567890123456']):p.text(20,50+row*28,value)
for row,value in enumerate(['右栏日期 2026-09-13','右栏金额 $987.65','右栏备注 完整保留']):p.text(250,50+row*28,value)
p.output(out/'chinese-two-column.pdf')
with pikepdf.open(out/'chinese-two-column.pdf') as pdf:
    # An explicit damaged ToUnicode maps a used glyph to replacement character.
    # This makes a concrete parser flag fixture, not a claim all font corruption
    # is detectable in arbitrary PDFs.
    for font in pdf.pages[0].Resources.Font.values():
        if '/ToUnicode' in font:
            data=font.ToUnicode.read_bytes()
            import re
            data=re.sub(rb'<4[Ee]2[Dd]>',b'<FFFD>',data,count=1)
            font.ToUnicode=pdf.make_stream(data)
    pdf.save(out/'damaged-unicode.pdf')
print(out)
